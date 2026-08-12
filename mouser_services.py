from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from config import settings
from credentials import CredentialPurpose, Provider, provider_configured
from distributor_models import (
    DistributorOffer,
    MouserCartExecuteRequest,
    MouserCartOperation,
    MouserCartPreviewRequest,
    MouserOrderHistoryMode,
    MouserOrderHistoryRequest,
    MouserOrderLookupRequest,
    MouserSearchMode,
    MouserSearchRequest,
)
from mouser_client import MouserResponse, client, health_tracker
from normalization import (
    component_identity,
    effective_purchase_quantity,
    money_string,
    normalize_manufacturer,
    normalize_mpn,
    parse_int,
    select_price_break,
)


def _search_option(request: MouserSearchRequest) -> str:
    if request.in_stock and request.rohs:
        return "RohsAndInStock"
    if request.in_stock:
        return "InStock"
    if request.rohs:
        return "Rohs"
    return "None"


def _mouser_attributes(part: dict[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for item in part.get("ProductAttributes") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("AttributeName")
        if name:
            attributes[str(name)] = item.get("AttributeValue")
    # Important first-class fields are also evaluable requirements.
    for name in (
        "Category",
        "Description",
        "LeadTime",
        "LifecycleStatus",
        "ROHSStatus",
        "Reeling",
        "Min",
        "Mult",
        "AvailabilityInStock",
    ):
        if part.get(name) is not None:
            attributes.setdefault(name, part.get(name))
    rohs_value = next(
        (
            part.get(name)
            for name in ("ROHSStatus", "RoHSStatus", "RoHSCompliant", "RoHSCompliance")
            if part.get(name) is not None
        ),
        None,
    )
    if rohs_value is not None:
        attributes.setdefault("RoHS Compliant", rohs_value)
    return attributes


def _lead_time_days(value: Any) -> float | None:
    text = str(value or "").strip().lower()
    quantity = parse_int(text)
    if quantity is None:
        return None
    if "week" in text:
        return float(quantity * 7)
    if "day" in text:
        return float(quantity)
    return None


def normalize_mouser_offer(
    part: dict[str, Any],
    requested_quantity: int,
) -> DistributorOffer:
    manufacturer = (
        part.get("ActualMfrName")
        or part.get("Manufacturer")
        or ""
    )
    mpn = part.get("ManufacturerPartNumber") or ""
    mouser_number = str(part.get("MouserPartNumber") or "").strip() or None
    minimum = parse_int(part.get("Min"))
    multiple = parse_int(part.get("Mult"))
    purchasable = effective_purchase_quantity(
        requested_quantity, minimum, multiple
    )
    price, currency = select_price_break(part.get("PriceBreaks") or [], purchasable)
    purchasable_offer = mouser_number is not None and price is not None
    total = (
        price * Decimal(purchasable)
        if price is not None and purchasable_offer
        else None
    )
    stock = parse_int(
        part.get("AvailabilityInStock")
        if part.get("AvailabilityInStock") is not None
        else part.get("Availability")
    )
    compliance = {
        "rohs": part.get("ROHSStatus"),
        "reach_svhc": part.get("REACH-SVHC"),
        "product_compliance": part.get("ProductCompliance"),
        "trade_compliance": part.get("TradeCompliance"),
    }
    availability_status = (
        "regional_unavailable"
        if mouser_number is None
        else (
            "pricing_unavailable"
            if price is None
            else (
                "stock_unknown"
                if stock is None
                else "available" if stock > 0 else "out_of_stock"
            )
        )
    )
    return DistributorOffer(
        distributor="mouser",
        identity=component_identity(
            manufacturer,
            mpn,
            source_identifiers=(
                {"mouser_part_number": mouser_number}
                if mouser_number is not None
                else {}
            ),
        ),
        distributor_part_number=mouser_number,
        requested_quantity=requested_quantity,
        purchasable_quantity=purchasable if purchasable_offer else None,
        purchasable=purchasable_offer,
        minimum_order_quantity=minimum,
        order_multiple=multiple,
        unit_price=money_string(price),
        merchandise_total=money_string(total),
        currency=str(currency).upper() if currency else None,
        quantity_available=stock,
        pricing_quantity_available=None,
        variation_quantity_available=stock,
        requested_quantity_in_stock=(
            stock >= requested_quantity if stock is not None else None
        ),
        pricing_requested_quantity_in_stock=None,
        variation_requested_quantity_in_stock=(
            stock >= requested_quantity if stock is not None else None
        ),
        availability_status=availability_status,
        lead_time=str(part.get("LeadTime") or "") or None,
        lead_time_days=_lead_time_days(part.get("LeadTime")),
        lifecycle=str(part.get("LifecycleStatus") or "") or None,
        compliance=compliance,
        packaging=str(part.get("Packaging") or "") or None,
        product_url=str(part.get("ProductDetailUrl") or "") or None,
        datasheet_url=str(part.get("DataSheetUrl") or "") or None,
        attributes=_mouser_attributes(part),
        duty_assumption="mouser_pays_customs_and_duties_false",
        observed_at=datetime.now(timezone.utc).isoformat(),
        raw=part,
    )


def search_mouser_products(
    request: MouserSearchRequest,
    *,
    principal: str,
) -> dict[str, Any]:
    search_option = _search_option(request)
    if request.manufacturer:
        if request.mode == MouserSearchMode.keyword:
            path = "/api/v2/search/keywordandmanufacturer"
            body = {
                "SearchByKeywordMfrNameRequest": {
                    "manufacturerName": request.manufacturer,
                    "keyword": request.query,
                    "records": request.records,
                    "pageNumber": request.starting_record // request.records + 1,
                    "searchOptions": search_option,
                    "searchWithYourSignUpLanguage": "false",
                    "mouserPaysCustomsAndDuties": False,
                }
            }
        else:
            path = "/api/v2/search/partnumberandmanufacturer"
            body = {
                "SearchByPartMfrNameRequest": {
                    "manufacturerName": request.manufacturer,
                    "mouserPartNumber": request.query,
                    "partSearchOptions": "Exact",
                    "mouserPaysCustomsAndDuties": False,
                }
            }
    elif request.mode == MouserSearchMode.keyword:
        path = "/api/v1/search/keyword"
        body = {
            "SearchByKeywordRequest": {
                "keyword": request.query,
                "records": request.records,
                "startingRecord": request.starting_record,
                "searchOptions": search_option,
                "searchWithYourSignUpLanguage": "false",
                "mouserPaysCustomsAndDuties": False,
            }
        }
    else:
        path = "/api/v1/search/partnumber"
        body = {
            "SearchByPartRequest": {
                "mouserPartNumber": request.query,
                "partSearchOptions": "Exact",
                "mouserPaysCustomsAndDuties": False,
            }
        }

    response = client.request(
        "POST",
        path,
        principal=principal,
        purpose=CredentialPurpose.catalog,
        json_body=body,
        safe_retry=True,
    )
    search_results = (
        response.data.get("SearchResults", {})
        if isinstance(response.data, dict)
        else {}
    )
    parts = search_results.get("Parts") or []
    normalized = [
        normalize_mouser_offer(part, 1).model_dump(mode="json")
        for part in parts
        if isinstance(part, dict)
    ]
    result = response.public()
    result["normalized_offers"] = normalized
    result["_meta"].update(
        {
            "provider": "mouser",
            "duty_assumption": "mouser_pays_customs_and_duties_false",
            "shipping_cost": "unavailable",
            "delivery_eta": "unavailable",
        }
    )
    return result


class MouserAdapter:
    name = "mouser"

    def capabilities(self) -> dict[str, bool]:
        search_configured = provider_configured(
            Provider.MOUSER,
            CredentialPurpose.SEARCH,
        )
        account_configured = provider_configured(
            Provider.MOUSER,
            CredentialPurpose.ACCOUNT,
        )
        return {
            "catalog": search_configured,
            "cart": account_configured,
            "order_history": account_configured,
            "order_submission": False,
        }

    def health(self) -> dict[str, Any]:
        capabilities = self.capabilities()
        runtime = health_tracker.snapshot()
        return {
            "provider": self.name,
            "status": (
                runtime["status"]
                if any(
                    capabilities[key] for key in ("catalog", "cart", "order_history")
                )
                else "disabled"
            ),
            "capabilities": capabilities,
            "last_success": runtime["last_success"],
            "last_error": runtime["last_error"],
        }

    def search(
        self,
        request: MouserSearchRequest,
        *,
        principal: str,
        authorization: str | None = None,
    ) -> dict[str, Any]:
        del authorization
        return search_mouser_products(request, principal=principal)

    def exact_offers(
        self,
        manufacturer: str,
        manufacturer_part_number: str,
        quantity: int,
        *,
        principal: str,
        authorization: str | None = None,
    ) -> list[DistributorOffer]:
        del authorization
        response = search_mouser_products(
            MouserSearchRequest(
                query=manufacturer_part_number,
                mode=MouserSearchMode.keyword,
                manufacturer=manufacturer,
                records=50,
            ),
            principal=principal,
        )
        parts = (response.get("SearchResults") or {}).get("Parts") or []
        expected_manufacturer = normalize_manufacturer(manufacturer)
        expected_mpn = normalize_mpn(manufacturer_part_number)
        matches = [
            normalize_mouser_offer(part, quantity)
            for part in parts
            if isinstance(part, dict)
            and normalize_manufacturer(
                part.get("ActualMfrName") or part.get("Manufacturer")
            )
            == expected_manufacturer
            and normalize_mpn(part.get("ManufacturerPartNumber")) == expected_mpn
        ]
        if matches:
            return matches

        # Fall back to the dedicated part-number method, but still accept only
        # a strict manufacturer and MPN response identity.
        fallback = search_mouser_products(
            MouserSearchRequest(
                query=manufacturer_part_number,
                mode=MouserSearchMode.part_number,
                manufacturer=manufacturer,
                records=50,
            ),
            principal=principal,
        )
        fallback_parts = (fallback.get("SearchResults") or {}).get("Parts") or []
        return [
            normalize_mouser_offer(part, quantity)
            for part in fallback_parts
            if isinstance(part, dict)
            and normalize_manufacturer(
                part.get("ActualMfrName") or part.get("Manufacturer")
            )
            == expected_manufacturer
            and normalize_mpn(part.get("ManufacturerPartNumber")) == expected_mpn
        ]


def search_mouser_order_history(
    request: MouserOrderHistoryRequest,
    *,
    principal: str,
) -> dict[str, Any]:
    if request.mode == MouserOrderHistoryMode.date_filter:
        path = "/api/v1/orderhistory/ByDateFilter"
        params = {"dateFilter": request.date_filter}
    else:
        path = "/api/v1/orderhistory/ByDateRange"
        params = {"startDate": request.start_date, "endDate": request.end_date}
    return client.request(
        "GET",
        path,
        principal=principal,
        purpose=CredentialPurpose.account,
        params=params,
        safe_retry=True,
    ).public()


def get_mouser_order(
    request: MouserOrderLookupRequest,
    *,
    principal: str,
) -> dict[str, Any]:
    if request.sales_order_number:
        path = "/api/v1/orderhistory/salesOrderNumber"
        params = {"salesOrderNumber": request.sales_order_number}
    else:
        path = "/api/v1/orderhistory/webOrderNumber"
        params = {"webOrderNumber": request.web_order_number}
    return client.request(
        "GET",
        path,
        principal=principal,
        purpose=CredentialPurpose.account,
        params=params,
        safe_retry=True,
    ).public()


def get_mouser_cart(cart_key: str, *, principal: str) -> dict[str, Any]:
    return client.request(
        "GET",
        "/api/v1/cart",
        principal=principal,
        purpose=CredentialPurpose.account,
        params={
            "cartKey": cart_key,
            "countryCode": "US",
            "currencyCode": "USD",
        },
        safe_retry=True,
    ).public()


def _public_state(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "_meta"}


def _mutable_cart_state(value: dict[str, Any]) -> dict[str, Any]:
    """Hash only user-controlled cart state, not volatile price/ATS fields."""
    items = []
    for item in value.get("CartItems") or []:
        if not isinstance(item, dict):
            continue
        schedules = item.get("ScheduledReleases") or []
        if isinstance(schedules, list):
            schedules = sorted(
                schedules,
                key=lambda schedule: json.dumps(
                    schedule, sort_keys=True, default=str
                ),
            )
        items.append(
            {
                "MouserPartNumber": item.get("MouserPartNumber"),
                "Quantity": item.get("Quantity"),
                "PackagingChoice": item.get("PackagingChoice"),
                "CartItemCustPartNumber": item.get("CartItemCustPartNumber"),
                "ScheduledReleases": schedules,
            }
        )
    items.sort(key=lambda item: str(item.get("MouserPartNumber") or ""))
    return {"CartKey": value.get("CartKey"), "CartItems": items}


def _canonical_hash(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _cart_items(cart: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("MouserPartNumber")): item
        for item in cart.get("CartItems") or []
        if isinstance(item, dict) and item.get("MouserPartNumber")
    }


def _item_payload(request: MouserCartPreviewRequest) -> list[dict[str, Any]]:
    return [
        {
            "MouserPartNumber": item.mouser_part_number,
            "Quantity": item.quantity,
            "CustomerPartNumber": item.customer_part_number,
            "PackagingChoice": item.packaging_choice.value,
        }
        for item in request.items
    ]


def _schedule_payload(request: MouserCartPreviewRequest) -> list[dict[str, Any]]:
    return [
        {
            "MouserPartNumber": item.mouser_part_number,
            "ScheduledReleases": [
                {"key": release.date, "value": release.quantity}
                for release in item.scheduled_releases
            ],
        }
        for item in request.schedule_items
    ]


def _preview_diff(
    request: MouserCartPreviewRequest,
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    operation = request.operation
    current_items = _cart_items(current or {})
    proposed_items = {
        item["MouserPartNumber"]: item for item in _item_payload(request)
    }
    if operation == MouserCartOperation.add_items:
        return {
            "additions": [
                {
                    "mouser_part_number": number,
                    "existing_quantity": (
                        current_items.get(number, {}).get("Quantity")
                    ),
                    "quantity_to_add": item["Quantity"],
                }
                for number, item in proposed_items.items()
            ]
        }
    if operation == MouserCartOperation.update_items:
        missing = sorted(set(proposed_items) - set(current_items))
        if missing:
            raise ValueError(
                f"Cannot update parts absent from the cart: {', '.join(missing)}"
            )
        return {
            "updates": [
                {
                    "mouser_part_number": number,
                    "before": current_items[number],
                    "after": item,
                }
                for number, item in proposed_items.items()
            ]
        }
    if operation == MouserCartOperation.remove_item:
        number = str(request.mouser_part_number)
        if number not in current_items:
            raise ValueError(f"{number} is not present in cart {request.cart_key}")
        return {"removals": [current_items[number]]}
    if operation == MouserCartOperation.replace_cart:
        additions = sorted(set(proposed_items) - set(current_items))
        removals = sorted(set(current_items) - set(proposed_items))
        common = sorted(set(current_items) & set(proposed_items))
        updates = [
            number
            for number in common
            if (
                parse_int(current_items[number].get("Quantity"))
                != proposed_items[number]["Quantity"]
                or str(current_items[number].get("PackagingChoice") or "None")
                != proposed_items[number]["PackagingChoice"]
            )
        ]
        return {
            "additions": [proposed_items[number] for number in additions],
            "updates": [
                {
                    "mouser_part_number": number,
                    "before": current_items[number],
                    "after": proposed_items[number],
                }
                for number in updates
            ],
            "removals": [current_items[number] for number in removals],
            "unchanged": [
                current_items[number] for number in common if number not in updates
            ],
        }
    if operation == MouserCartOperation.create_from_order:
        return {"create_cart_from_order": request.order_number}
    if operation in {
        MouserCartOperation.add_schedule,
        MouserCartOperation.update_schedule,
    }:
        missing = sorted(
            {
                item.mouser_part_number for item in request.schedule_items
            }
            - set(current_items)
        )
        if missing:
            raise ValueError(
                f"Cannot schedule parts absent from the cart: {', '.join(missing)}"
            )
        return {
            "schedule_changes": [
                {
                    "mouser_part_number": item.mouser_part_number,
                    "before": current_items[item.mouser_part_number].get(
                        "ScheduledReleases"
                    ),
                    "after": [
                        release.model_dump(mode="json")
                        for release in item.scheduled_releases
                    ],
                }
                for item in request.schedule_items
            ]
        }
    return {
        "schedule_deletions": [
            {
                "mouser_part_number": number,
                "scheduled_releases": item.get("ScheduledReleases") or [],
            }
            for number, item in current_items.items()
            if item.get("ScheduledReleases")
        ]
    }


@dataclass(slots=True)
class CartPreview:
    principal: str
    request: MouserCartPreviewRequest
    state_hash: str
    payload_hash: str
    expires_at: float
    consumed: bool = False


class CartConfirmationStore:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, CartPreview] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        principal: str,
        request: MouserCartPreviewRequest,
        state_hash: str,
    ) -> tuple[str, CartPreview]:
        token = secrets.token_urlsafe(32)
        preview = CartPreview(
            principal=principal,
            request=request.model_copy(deep=True),
            state_hash=state_hash,
            payload_hash=_canonical_hash(request.model_dump(mode="json")),
            expires_at=time.time() + self.ttl_seconds,
        )
        with self._lock:
            self._purge_locked()
            self._entries[token] = preview
        return token, preview

    def consume(self, token: str, principal: str) -> CartPreview:
        with self._lock:
            self._purge_locked()
            preview = self._entries.get(token)
            if preview is None:
                raise ValueError("Cart confirmation token is invalid or expired")
            if preview.consumed:
                raise ValueError("Cart confirmation token has already been used")
            if preview.principal != principal:
                raise ValueError(
                    "Cart confirmation token belongs to another authenticated principal"
                )
            if preview.payload_hash != _canonical_hash(
                preview.request.model_dump(mode="json")
            ):
                raise ValueError("Cart confirmation payload failed integrity checking")
            preview.consumed = True
            return preview

    def _purge_locked(self) -> None:
        now = time.time()
        for token in [
            token
            for token, preview in self._entries.items()
            if preview.expires_at <= now
        ]:
            self._entries.pop(token, None)


cart_confirmations = CartConfirmationStore(settings.cart_preview_ttl_seconds)


def preview_mouser_cart_change(
    request: MouserCartPreviewRequest,
    *,
    principal: str,
) -> dict[str, Any]:
    current: dict[str, Any] | None = None
    if request.cart_key:
        current = _public_state(get_mouser_cart(request.cart_key, principal=principal))
    state = (
        _mutable_cart_state(current)
        if current is not None
        else {"new_cart": True}
    )
    diff = _preview_diff(request, current)
    token, preview = cart_confirmations.issue(
        principal, request, _canonical_hash(state)
    )
    return {
        "status": "preview",
        "provider": "mouser",
        "operation": request.operation.value,
        "cart_key": request.cart_key,
        "diff": diff,
        "confirmation_token": token,
        "expires_at": datetime.fromtimestamp(
            preview.expires_at, tz=timezone.utc
        ).isoformat(),
        "warning": (
            "Review this exact diff. Execute accepts only this token and rejects "
            "changed cart state, expired tokens, and replay."
        ),
    }


def _cart_body(request: MouserCartPreviewRequest) -> dict[str, Any]:
    return {
        "CartKey": request.cart_key or "00000000-0000-0000-0000-000000000000",
        "MouserPaysCustomsAndDuties": False,
        "CartItems": _item_payload(request),
    }


def _schedule_body(request: MouserCartPreviewRequest) -> dict[str, Any]:
    return {
        "CartKey": request.cart_key,
        "ScheduleCartItems": _schedule_payload(request),
    }


def _execute_cart_request(
    request: MouserCartPreviewRequest,
    *,
    principal: str,
) -> MouserResponse:
    common_params = {"countryCode": "US", "currencyCode": "USD"}
    if request.operation == MouserCartOperation.add_items:
        return client.request(
            "POST",
            "/api/v1/cart/items/insert",
            principal=principal,
            purpose=CredentialPurpose.account,
            params=common_params,
            json_body=_cart_body(request),
            safe_retry=False,
        )
    if request.operation == MouserCartOperation.update_items:
        return client.request(
            "POST",
            "/api/v1/cart/items/update",
            principal=principal,
            purpose=CredentialPurpose.account,
            params=common_params,
            json_body=_cart_body(request),
            safe_retry=False,
        )
    if request.operation == MouserCartOperation.remove_item:
        return client.request(
            "POST",
            "/api/v1/cart/item/remove",
            principal=principal,
            purpose=CredentialPurpose.account,
            params={
                **common_params,
                "cartKey": request.cart_key,
                "mouserPartNumber": request.mouser_part_number,
            },
            safe_retry=False,
        )
    if request.operation == MouserCartOperation.replace_cart:
        return client.request(
            "POST",
            "/api/v1/cart",
            principal=principal,
            purpose=CredentialPurpose.account,
            params=common_params,
            json_body=_cart_body(request),
            safe_retry=False,
        )
    if request.operation == MouserCartOperation.create_from_order:
        return client.request(
            "POST",
            "/api/v1/order/item/CreateCartFromOrder",
            principal=principal,
            purpose=CredentialPurpose.account,
            params={
                **common_params,
                "orderNumber": request.order_number,
            },
            safe_retry=False,
        )
    if request.operation == MouserCartOperation.add_schedule:
        return client.request(
            "POST",
            "/api/v1/cart/insert/schedule",
            principal=principal,
            purpose=CredentialPurpose.account,
            json_body=_schedule_body(request),
            safe_retry=False,
        )
    if request.operation == MouserCartOperation.update_schedule:
        return client.request(
            "POST",
            "/api/v1/cart/update/schedule",
            principal=principal,
            purpose=CredentialPurpose.account,
            json_body=_schedule_body(request),
            safe_retry=False,
        )
    return client.request(
        "POST",
        "/api/v1/cart/deleteall/schedule",
        principal=principal,
        purpose=CredentialPurpose.account,
        params={"cartKey": request.cart_key},
        safe_retry=False,
    )


def _nested_cart_errors(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    errors: list[dict[str, Any]] = []
    for item in payload.get("CartItems") or []:
        if isinstance(item, dict):
            errors.extend(
                error for error in item.get("Errors") or [] if isinstance(error, dict)
            )
    return errors


def execute_mouser_cart_change(
    request: MouserCartExecuteRequest,
    *,
    principal: str,
) -> dict[str, Any]:
    preview = cart_confirmations.consume(request.confirmation_token, principal)
    cart_request = preview.request
    if cart_request.cart_key:
        current_public = _public_state(
            get_mouser_cart(cart_request.cart_key, principal=principal)
        )
        current = _mutable_cart_state(current_public)
    else:
        current = {"new_cart": True}
    if _canonical_hash(current) != preview.state_hash:
        raise ValueError(
            "Cart changed after preview; create a new preview before executing"
        )

    response = _execute_cart_request(cart_request, principal=principal)
    result = response.public()
    nested_errors = _nested_cart_errors(response.data)
    result["_meta"].update(
        {
            "operation": cart_request.operation.value,
            "confirmation_consumed": True,
            "automatic_retries": False,
        }
    )
    if nested_errors:
        result["status"] = "partial"
        result["cart_item_errors"] = nested_errors
    else:
        result["status"] = "success"
    return result


mouser_adapter = MouserAdapter()
