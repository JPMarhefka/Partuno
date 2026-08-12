from __future__ import annotations

import copy
import hashlib
import math
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable, Literal, Sequence
from urllib.parse import quote

from client import DigiKeyHTTPError, DigiKeyResponse, client, error_envelope
from config import settings
from models import (
    AddPartsRequest,
    BarcodeInput,
    BarcodeListComparisonRequest,
    BarcodeType,
    BOMItem,
    BulkBOMRequest,
    CreateQuoteRequest,
    DecodeBarcodeRequest,
    LifecycleAuditRequest,
    ListDiffRequest,
    ListPartInput,
    ListSyncRequest,
    MarketPlaceFilter,
    PackingListLookupRequest,
    PackingListLookupType,
    ParametricFilter,
    PricingOptimizationRequest,
    ProductResourcesRequest,
    ProductSearchRequest,
    QuoteFromSourceRequest,
    TariffFilter,
    UpdateListPartRequest,
)


PRODUCT_BASE = "/products/v4/search"
MYLISTS_BASE = "/mylists/v1/lists"
QUOTES_BASE = "/quoting/v4/quotes"
BARCODE_BASE = "/Barcoding/v3"
PCN_BASE = "/ChangeNotifications/v3/Products"
PACKING_LIST_BASE = "/packinglist/v1"
PCN_DATE_MISMATCH_DAYS = 30
PCN_CACHE_MAX_ENTRIES = 256
_pcn_cache: dict[
    tuple[str, str | None, str],
    tuple[float, DigiKeyResponse],
] = {}
_pcn_cache_lock = threading.Lock()


@dataclass(slots=True)
class WorkflowError:
    product_number: str
    status_code: int | None
    detail: Any
    meta: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "product_number": self.product_number,
            "status_code": self.status_code,
            "detail": self.detail,
            "_meta": self.meta or {},
        }


def q(value: str | int) -> str:
    return quote(str(value), safe="")


def chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def normalize_part_number(value: str | None) -> str:
    return "".join((value or "").strip().upper().split())


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        if isinstance(value, str):
            match = re.search(r"-?\d+", value.replace(",", ""))
            if match:
                try:
                    return int(match.group(0))
                except ValueError:
                    pass
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def first_present(mapping: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def filter_ids(values: Sequence[str]) -> list[dict[str, str]]:
    return [{"Id": str(value)} for value in values if str(value).strip()]


def build_keyword_body(request: ProductSearchRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "Keywords": request.keywords,
        "Limit": request.limit,
        "Offset": request.offset,
    }

    filters: dict[str, Any] = {
        "MarketPlaceFilter": enum_value(request.marketplace_filter),
        "TariffFilter": enum_value(request.tariff_filter),
    }
    optional_id_filters = {
        "ManufacturerFilter": request.manufacturer_ids,
        "CategoryFilter": request.category_ids,
        "StatusFilter": request.status_ids,
        "PackagingFilter": request.packaging_ids,
        "SeriesFilter": request.series_ids,
    }
    for api_name, values in optional_id_filters.items():
        if values:
            filters[api_name] = filter_ids(values)

    if request.minimum_quantity_available is not None:
        filters["MinimumQuantityAvailable"] = request.minimum_quantity_available
    if request.search_options:
        filters["SearchOptions"] = request.search_options
    if request.parametric_filters:
        filters["ParameterFilterRequest"] = {
            "CategoryFilter": {"Id": str(request.parametric_category_id)},
            "ParameterFilters": [
                {
                    "ParameterId": item.parameter_id,
                    "FilterValues": filter_ids(item.value_ids),
                }
                for item in request.parametric_filters
            ],
        }

    body["FilterOptionsRequest"] = filters
    if request.sort_field:
        body["SortOptions"] = {
            "Field": request.sort_field,
            "SortOrder": enum_value(request.sort_order),
        }
    return body


def search_products(
    request: ProductSearchRequest,
    authorization: str,
) -> DigiKeyResponse:
    params = {"includes": request.includes} if request.includes else None
    initial = client.request(
        "POST", f"{PRODUCT_BASE}/keyword", authorization, params=params,
        json_body=build_keyword_body(request), safe_retry=True,
    )
    if not _keyword_response_violates_filters(initial.data, request):
        initial.meta.update({
            "filter_enforcement": "native",
            "results_complete": True,
            "source_page_count": 1,
        })
        return initial
    return _rebuild_filtered_keyword_page(initial, request, authorization, params)


def _explicit_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _keyword_flag(record: dict[str, Any], *names: str) -> bool | None:
    for name in names:
        if name in record:
            value = _explicit_bool(record.get(name))
            if value is not None:
                return value
    return None


def _keyword_record_matches(record: dict[str, Any], request: ProductSearchRequest) -> bool:
    tariff = _keyword_flag(record, "TariffActive", "IsTariffActive")
    if request.tariff_filter is TariffFilter.only and tariff is False:
        return False
    if request.tariff_filter is TariffFilter.exclude and tariff is True:
        return False

    marketplace = _keyword_flag(
        record,
        "IsMarketplace",
        "IsMarketPlace",
        "Marketplace",
        "MarketPlace",
        "MarketPlaceProduct",
        "IsMarketPlaceProduct",
    )
    if request.marketplace_filter is MarketPlaceFilter.only and marketplace is False:
        return False
    if request.marketplace_filter is MarketPlaceFilter.exclude and marketplace is True:
        return False
    return True


def _keyword_product_matches(product: dict[str, Any], request: ProductSearchRequest) -> bool:
    variations = _detail_variations(product)
    return any(_keyword_record_matches(variation, request) for variation in variations) if variations else _keyword_record_matches(product, request)


def _keyword_response_violates_filters(payload: Any, request: ProductSearchRequest) -> bool:
    if request.tariff_filter is TariffFilter.none and request.marketplace_filter is MarketPlaceFilter.no_filter:
        return False
    if not isinstance(payload, dict):
        return False
    products = payload.get("Products") or []
    exact_matches = payload.get("ExactMatches") or []
    if not isinstance(products, list):
        products = []
    if not isinstance(exact_matches, list):
        exact_matches = []
    for product in [*products, *exact_matches]:
        if not isinstance(product, dict):
            continue
        variations = _detail_variations(product)
        records = variations or [product]
        if any(not _keyword_record_matches(record, request) for record in records):
            return True
    return False


def _filtered_keyword_product(product: dict[str, Any], request: ProductSearchRequest) -> dict[str, Any] | None:
    variations = _detail_variations(product)
    if not variations:
        return dict(product) if _keyword_record_matches(product, request) else None
    retained = [variation for variation in variations if _keyword_record_matches(variation, request)]
    if not retained:
        return None
    filtered = dict(product)
    variation_key = "ProductVariations" if isinstance(product.get("ProductVariations"), list) else "Variations"
    filtered[variation_key] = retained
    quantities = [
        first_present(variation, "QuantityAvailableforPackageType", "QuantityAvailable")
        for variation in retained
    ]
    if quantities and all(quantity is not None for quantity in quantities):
        filtered["QuantityAvailable"] = sum(as_int(quantity) for quantity in quantities)
    return filtered


def _rebuild_filtered_keyword_page(
    initial: DigiKeyResponse,
    request: ProductSearchRequest,
    authorization: str,
    params: dict[str, Any] | None,
) -> DigiKeyResponse:
    """Use the documented filters first, then protect callers from a bad upstream page."""
    page_size = 50
    body = build_keyword_body(request)
    all_products: list[dict[str, Any]] = []
    source_product_count = 0
    source_variation_count = 0
    offset = 0
    pages: list[DigiKeyResponse] = []
    seen_products: dict[str, dict[str, Any]] = {}
    target_count = request.offset + request.limit
    results_complete = False
    fallback_reason = "safety_cap"

    for _ in range(settings.search_fallback_max_pages):
        page_body = dict(body)
        page_body.update({"Limit": page_size, "Offset": offset})
        page = client.request(
            "POST", f"{PRODUCT_BASE}/keyword", authorization, params=params,
            json_body=page_body, safe_retry=True,
        )
        pages.append(page)
        payload = page.data if isinstance(page.data, dict) else {}
        source = payload.get("Products") or []
        source = [product for product in source if isinstance(product, dict)] if isinstance(source, list) else []
        source_product_count += len(source)
        source_variation_count += sum(len(_detail_variations(product)) for product in source)
        for product in source:
            filtered = _filtered_keyword_product(product, request)
            if filtered is None:
                continue
            product_key = normalize_part_number(str(first_present(
                filtered,
                "DigiKeyProductNumber",
                "DigiKeyPartNumber",
                "ManufacturerProductNumber",
                default=f"source-{offset}-{len(seen_products)}",
            )))
            existing = seen_products.get(product_key)
            if existing is None:
                seen_products[product_key] = filtered
                all_products.append(filtered)
                continue
            existing_variations = _detail_variations(existing)
            new_variations = _detail_variations(filtered)
            if existing_variations and new_variations:
                variation_key = (
                    "ProductVariations"
                    if isinstance(existing.get("ProductVariations"), list)
                    else "Variations"
                )
                seen_variations = {
                    normalize_part_number(str(first_present(
                        variation,
                        "DigiKeyProductNumber",
                        "DigiKeyPartNumber",
                        "ProductNumber",
                        default="",
                    )))
                    for variation in existing_variations
                }
                existing[variation_key] = [
                    *existing_variations,
                    *[
                        variation for variation in new_variations
                        if normalize_part_number(str(first_present(
                            variation,
                            "DigiKeyProductNumber",
                            "DigiKeyPartNumber",
                            "ProductNumber",
                            default="",
                        ))) not in seen_variations
                    ],
                ]
        declared_total = as_int(payload.get("ProductsCount"), 0)
        offset += len(source)
        if len(all_products) >= target_count:
            results_complete = True
            fallback_reason = "requested_window_filled"
            break
        if not source or len(source) < page_size or (declared_total and offset >= declared_total):
            results_complete = True
            fallback_reason = "upstream_exhausted"
            break
        if str(page.meta.get("rate_limit_remaining") or "").strip() == "0":
            fallback_reason = "rate_limit"
            break

    start = request.offset
    selected = all_products[start : start + request.limit]
    result = dict(initial.data) if isinstance(initial.data, dict) else {}
    result["Products"] = selected
    result["ProductsCount"] = len(all_products)
    result["ProductVariationsCount"] = sum(len(_detail_variations(product)) for product in all_products)
    if isinstance(result.get("ExactMatches"), list):
        result["ExactMatches"] = [
            filtered for product in result["ExactMatches"]
            if isinstance(product, dict)
            if (filtered := _filtered_keyword_product(product, request)) is not None
        ]
    meta = dict(initial.meta)
    meta.update({
        "filter_enforcement": "local_fallback",
        "source_product_count": source_product_count,
        "source_variation_count": source_variation_count,
        "removed_product_count": source_product_count - len(all_products),
        "removed_variation_count": source_variation_count - result["ProductVariationsCount"],
        "source_page_count": len(pages),
        "results_complete": results_complete,
        "fallback_reason": fallback_reason,
    })
    return DigiKeyResponse(result, meta)


def get_product_details(
    product_number: str,
    authorization: str,
    *,
    manufacturer_id: str | None = None,
    account_id: str | None = None,
    includes: str | None = None,
) -> DigiKeyResponse:
    params: dict[str, Any] = {}
    if manufacturer_id:
        params["manufacturerId"] = manufacturer_id
    if includes:
        params["includes"] = includes
    return client.request(
        "GET",
        f"{PRODUCT_BASE}/{q(product_number)}/productdetails",
        authorization,
        account_id=account_id,
        params=params or None,
    )


def _detail_variations(product: dict[str, Any]) -> list[dict[str, Any]]:
    values = product.get("ProductVariations") or product.get("Variations") or []
    return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []


def get_product_pricing(
    product_number: str,
    authorization: str,
    *,
    account_id: str | None = None,
    limit: int = 10,
    offset: int = 0,
    in_stock: bool = False,
    exclude_marketplace: bool = True,
    exclude_tariff: bool = False,
    includes: str | None = None,
) -> DigiKeyResponse:
    params: dict[str, Any] = {
        "limit": max(1, min(10, limit)),
        "offset": max(0, offset),
        "inStock": in_stock,
        "excludeMarketplace": exclude_marketplace,
        "excludeTariff": exclude_tariff,
    }
    if includes:
        params["includes"] = includes
    response = client.request(
        "GET",
        f"{PRODUCT_BASE}/{q(product_number)}/pricing",
        authorization,
        account_id=account_id,
        params=params,
    )
    if isinstance(response.data, dict):
        response.data = normalize_product_pricing_payload(response.data)
    return response


def get_pricing_by_quantity(
    product_number: str,
    requested_quantity: int,
    authorization: str,
    *,
    account_id: str | None = None,
    includes: str | None = None,
) -> DigiKeyResponse:
    params = {"includes": includes} if includes else None
    return client.request(
        "GET",
        f"{PRODUCT_BASE}/{q(product_number)}/pricingbyquantity/{requested_quantity}",
        authorization,
        account_id=account_id,
        params=params,
    )


def get_digireel_pricing(
    product_number: str,
    requested_quantity: int,
    authorization: str,
    *,
    account_id: str | None = None,
) -> DigiKeyResponse:
    return client.request(
        "GET",
        f"{PRODUCT_BASE}/{q(product_number)}/digireelpricing",
        authorization,
        account_id=account_id,
        params={"requestedQuantity": requested_quantity},
    )


def get_product_media(product_number: str, authorization: str) -> DigiKeyResponse:
    return client.request(
        "GET",
        f"{PRODUCT_BASE}/{q(product_number)}/media",
        authorization,
    )


def get_substitutions(
    product_number: str,
    authorization: str,
    *,
    limit: int = 10,
    search_options: str = "InStock,RoHSCompliant",
    exclude_marketplace: bool = True,
) -> DigiKeyResponse:
    bounded_limit = max(1, min(50, limit))
    response = client.request(
        "GET",
        f"{PRODUCT_BASE}/{q(product_number)}/substitutions",
        authorization,
        params={"limit": bounded_limit},
    )
    data = dict(response.data) if isinstance(response.data, dict) else {}
    substitutes = data.get("ProductSubstitutes") or []
    if not isinstance(substitutes, list):
        substitutes = []
    available = len(substitutes)
    data["ProductSubstitutes"] = substitutes[:bounded_limit]
    data["ProductSubstitutesCount"] = len(data["ProductSubstitutes"])
    data["total_available"] = available
    data["returned_count"] = len(data["ProductSubstitutes"])
    data["requested_limit"] = bounded_limit
    data["limit_enforced_locally"] = True
    meta = dict(response.meta)
    meta.update({
        "limit_enforced_locally": True,
        "requested_limit": bounded_limit,
    })
    return DigiKeyResponse(data, meta)


def get_recommended_products(
    product_number: str,
    authorization: str,
    *,
    limit: int = 1,
    search_options: str | Sequence[str] | None = None,
    exclude_marketplace: bool = True,
) -> DigiKeyResponse:
    bounded_limit = max(1, min(50, int(limit)))
    if isinstance(search_options, str):
        normalized_options = [
            option.strip() for option in search_options.split(",") if option.strip()
        ]
    else:
        normalized_options = [
            str(option).strip() for option in (search_options or []) if str(option).strip()
        ]
    params: dict[str, Any] = {
        "limit": bounded_limit,
        "excludeMarketPlaceProducts": bool(exclude_marketplace),
    }
    if normalized_options:
        params["searchOptionList"] = ",".join(normalized_options)
    try:
        response = client.request(
            "GET", f"{PRODUCT_BASE}/{q(product_number)}/recommendedproducts",
            authorization, params=params, safe_retry=False,
        )
        response.meta.update({
            "requested_limit": bounded_limit,
            "limit_semantics": "upstream_recommendation_records",
            "nested_recommended_products_truncated": False,
        })
        return response
    except DigiKeyHTTPError as exc:
        if not normalized_options or exc.status_code not in {404, 500}:
            raise
        fallback_params = dict(params)
        fallback_params.pop("searchOptionList", None)
        try:
            response = client.request(
                "GET", f"{PRODUCT_BASE}/{q(product_number)}/recommendedproducts",
                authorization, params=fallback_params, safe_retry=False,
            )
            attempts = [
                _compatibility_attempt(exc.status_code, exc.meta),
                _compatibility_attempt(
                    as_int(response.meta.get("http_status"), 200), response.meta
                ),
            ]
            response.meta["compatibility"] = {
                "fallback_used": True,
                "reason": "recommendation_filters_rejected",
                "attempts": attempts,
            }
            data = dict(response.data) if isinstance(response.data, dict) else {
                "Recommendations": response.data
            }
            data.setdefault("warnings", []).append({
                "code": "recommendation_filters_rejected",
                "message": (
                    "DigiKey rejected recommendation search filters; "
                    "results were retried without searchOptionList."
                ),
                "requested_search_options": normalized_options,
            })
            response.data = data
            response.meta.update({
                "requested_limit": bounded_limit,
                "limit_semantics": "upstream_recommendation_records",
                "nested_recommended_products_truncated": False,
            })
            return response
        except DigiKeyHTTPError as retry_exc:
            meta = dict(retry_exc.meta)
            meta["compatibility"] = {
                "fallback_used": True,
                "reason": "recommendation_filters_rejected",
                "attempts": [
                    _compatibility_attempt(exc.status_code, exc.meta),
                    _compatibility_attempt(retry_exc.status_code, retry_exc.meta),
                ],
            }
            raise DigiKeyHTTPError(
                retry_exc.status_code,
                retry_exc.detail,
                meta,
            ) from retry_exc


def _compatibility_attempt(status_code: int, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "status_code": status_code,
        "correlation_id": meta.get("correlation_id"),
        "rate_limit": {
            "limit": meta.get("rate_limit"),
            "remaining": meta.get("rate_limit_remaining"),
            "reset": meta.get("rate_limit_reset"),
            "retry_after": meta.get("retry_after"),
        },
    }


def get_associations(product_number: str, authorization: str) -> DigiKeyResponse:
    return client.request(
        "GET",
        f"{PRODUCT_BASE}/{q(product_number)}/associations",
        authorization,
    )


def get_alternate_packaging(product_number: str, authorization: str) -> DigiKeyResponse:
    return client.request(
        "GET",
        f"{PRODUCT_BASE}/{q(product_number)}/alternatepackaging",
        authorization,
    )


def get_product_change_notifications(
    product_number: str,
    authorization: str,
    *,
    includes: str | None = None,
) -> DigiKeyResponse:
    token_fingerprint = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
    cache_key = (
        normalize_part_number(product_number),
        includes,
        token_fingerprint,
    )
    now = time.monotonic()
    if settings.pcn_cache_seconds > 0:
        with _pcn_cache_lock:
            cached = _pcn_cache.get(cache_key)
            if cached and cached[0] > now:
                cached_response = copy.deepcopy(cached[1])
                cached_response.meta["cache"] = {
                    "scope": "pcn",
                    "hit": True,
                    "ttl_seconds": settings.pcn_cache_seconds,
                }
                return cached_response
            if cached:
                _pcn_cache.pop(cache_key, None)

    params = {"Includes": includes} if includes else None
    response = client.request(
        "GET",
        f"{PCN_BASE}/{q(product_number)}",
        authorization,
        params=params,
    )
    if isinstance(response.data, dict):
        response.data = normalize_pcn_payload(response.data)
    response.meta["cache"] = {
        "scope": "pcn",
        "hit": False,
        "ttl_seconds": settings.pcn_cache_seconds,
    }
    if settings.pcn_cache_seconds > 0:
        with _pcn_cache_lock:
            expired = [
                key for key, value in _pcn_cache.items() if value[0] <= now
            ]
            for key in expired:
                _pcn_cache.pop(key, None)
            if len(_pcn_cache) >= PCN_CACHE_MAX_ENTRIES:
                oldest_key = min(
                    _pcn_cache,
                    key=lambda key: _pcn_cache[key][0],
                )
                _pcn_cache.pop(oldest_key, None)
            _pcn_cache[cache_key] = (
                time.monotonic() + settings.pcn_cache_seconds,
                copy.deepcopy(response),
            )
    return response


def parse_pcn_description_date(description: str) -> str | None:
    """Return the first valid human-readable or ISO date embedded in a PCN."""
    if not isinstance(description, str):
        return None
    for match in re.finditer(r"\b\d{1,2}/[A-Za-z]{3}/\d{4}\b", description):
        try:
            return datetime.strptime(match.group(0).title(), "%d/%b/%Y").date().isoformat()
        except ValueError:
            continue
    for match in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b", description):
        try:
            return date.fromisoformat(match.group(0)).isoformat()
        except ValueError:
            continue
    return None


def _parse_api_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None


def normalize_pcn_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add non-destructive diagnostics when PCN API and description dates differ."""
    normalized = copy.deepcopy(payload)
    notifications = normalized.get("ProductChangeNotifications")
    if not isinstance(notifications, list):
        return normalized
    for notification in notifications:
        if not isinstance(notification, dict):
            continue
        api_change_date = notification.get("PcnChangeDate")
        description_date = parse_pcn_description_date(
            notification.get("PcnDescription", "")
        )
        parsed_api_date = _parse_api_date(api_change_date)
        parsed_description_date = (
            date.fromisoformat(description_date) if description_date else None
        )
        mismatch_days = (
            abs((parsed_api_date - parsed_description_date).days)
            if parsed_api_date and parsed_description_date
            else None
        )
        date_warning = None
        if mismatch_days is not None and mismatch_days > PCN_DATE_MISMATCH_DAYS:
            date_warning = (
                "The API change date differs materially from the date embedded "
                "in the PCN description."
            )
        notification.update({
            "api_change_date": api_change_date,
            "description_date": description_date,
            "date_mismatch_days": mismatch_days,
            "date_warning": date_warning,
        })
    return normalized


def get_manufacturers(
    authorization: str, *, limit: int = 100, offset: int = 0
) -> DigiKeyResponse:
    response = client.request("GET", f"{PRODUCT_BASE}/manufacturers", authorization)
    data = dict(response.data) if isinstance(response.data, dict) else {}
    manufacturers = data.get("Manufacturers") or []
    if not isinstance(manufacturers, list):
        manufacturers = []
    start = max(0, offset)
    window = manufacturers[start : start + max(1, min(100, limit))]
    data["Manufacturers"] = window
    data["total_count"] = len(manufacturers)
    data["returned_count"] = len(window)
    data["offset"] = start
    data["limit"] = max(1, min(100, limit))
    meta = dict(response.meta)
    meta["pagination_enforced_locally"] = True
    return DigiKeyResponse(data, meta)


def get_categories(authorization: str) -> DigiKeyResponse:
    return client.request("GET", f"{PRODUCT_BASE}/categories", authorization)


def get_category(category_id: int, authorization: str) -> DigiKeyResponse:
    return client.request("GET", f"{PRODUCT_BASE}/categories/{category_id}", authorization)


def get_associated_accounts(authorization: str) -> DigiKeyResponse:
    return client.request(
        "GET",
        "/CustomerResource/v1/associatedaccounts",
        authorization,
    )


def product_research_bundle(
    request: ProductResourcesRequest,
    authorization: str,
    *,
    account_id: str | None = None,
) -> dict[str, Any]:
    tasks: dict[str, Callable[[], DigiKeyResponse]] = {
        "details": lambda: get_product_details(request.product_number, authorization, account_id=account_id, includes=request.includes),
    }
    if request.include_media:
        tasks["media"] = lambda: get_product_media(request.product_number, authorization)
    if request.include_substitutions:
        tasks["substitutions"] = lambda: get_substitutions(
            request.product_number,
            authorization,
            limit=request.limit,
        )
    if request.include_recommended:
        tasks["recommended"] = lambda: get_recommended_products(
            request.product_number,
            authorization,
            limit=request.limit,
        )
    if request.include_associations:
        tasks["associations"] = lambda: get_associations(request.product_number, authorization)
    if request.include_alternate_packaging:
        tasks["alternate_packaging"] = lambda: get_alternate_packaging(
            request.product_number,
            authorization,
        )
    if request.include_change_notifications:
        tasks["change_notifications"] = lambda: get_product_change_notifications(
            request.product_number,
            authorization,
        )

    output: dict[str, Any] = {"product_number": request.product_number, "results": {}, "errors": {}}
    with ThreadPoolExecutor(max_workers=min(settings.workflow_concurrency, len(tasks))) as executor:
        future_map = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                output["results"][name] = future.result().public()
            except DigiKeyHTTPError as exc:
                output["errors"][name] = error_envelope(exc.status_code, exc.detail, exc.meta)
            except Exception as exc:  # pragma: no cover, defensive boundary
                output["errors"][name] = {
                    "status_code": None,
                    "detail": f"{exc.__class__.__name__}: {exc}",
                }
    if output["results"] and output["errors"]:
        output["status"] = "partial"
    elif output["results"]:
        output["status"] = "success"
    else:
        output["status"] = "failed"
    return output


def consolidate_bom_items(items: Sequence[BOMItem]) -> list[BOMItem]:
    merged: dict[tuple[str, str | None], BOMItem] = {}
    for item in items:
        key = (normalize_part_number(item.product_number), item.preferred_package)
        if key not in merged:
            merged[key] = item.model_copy(deep=True)
            continue
        current = merged[key]
        current.quantity += item.quantity
        refs = [value for value in (current.reference_designator, item.reference_designator) if value]
        current.reference_designator = ", ".join(dict.fromkeys(refs))
        notes = [value for value in (current.notes, item.notes) if value]
        current.notes = " | ".join(dict.fromkeys(notes))
        if not current.customer_reference:
            current.customer_reference = item.customer_reference
    return list(merged.values())


def _pricing_option_groups(payload: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for name in ("MyPricingOptions", "StandardPricingOptions"):
        values = payload.get(name)
        if isinstance(values, list) and values:
            groups.append((name, values))
    if groups:
        return groups

    for key, value in payload.items():
        if "PricingOptions" in key and isinstance(value, list):
            groups.append((key, value))
    return groups


def _positive_int(value: Any) -> int | None:
    parsed = as_int(value, 0)
    return parsed if parsed > 0 else None


def normalize_effective_moq(
    variation: dict[str, Any],
    *,
    product_standard_package: Any = None,
) -> dict[str, Any]:
    raw_moq = variation.get("MinimumOrderQuantity")
    raw_positive = _positive_int(raw_moq)
    if raw_positive is not None:
        return {
            "raw_minimum_order_quantity": raw_moq,
            "effective_minimum_order_quantity": raw_positive,
            "effective_moq_source": "variation_minimum_order_quantity",
        }

    pricing = variation.get("StandardPricing") or []
    breaks = [
        value
        for item in pricing
        if isinstance(item, dict)
        if (value := _positive_int(item.get("BreakQuantity"))) is not None
    ] if isinstance(pricing, list) else []
    if breaks:
        return {
            "raw_minimum_order_quantity": raw_moq,
            "effective_minimum_order_quantity": min(breaks),
            "effective_moq_source": "first_price_break",
        }

    variation_package = _positive_int(variation.get("StandardPackage"))
    if variation_package is not None:
        return {
            "raw_minimum_order_quantity": raw_moq,
            "effective_minimum_order_quantity": variation_package,
            "effective_moq_source": "variation_standard_package",
        }

    product_package = _positive_int(product_standard_package)
    return {
        "raw_minimum_order_quantity": raw_moq,
        "effective_minimum_order_quantity": product_package,
        "effective_moq_source": (
            "product_standard_package" if product_package is not None else "unknown"
        ),
    }


def normalize_product_pricing_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    product = normalized.get("Product")
    product_mapping = product if isinstance(product, dict) else normalized
    product_standard_package = first_present(
        product_mapping, "StandardPackage", default=normalized.get("StandardPackage")
    )
    variations = (
        product_mapping.get("ProductVariations")
        or product_mapping.get("Variations")
        or normalized.get("ProductVariations")
        or normalized.get("Variations")
        or []
    )
    if isinstance(variations, list):
        for variation in variations:
            if isinstance(variation, dict):
                variation.update(normalize_effective_moq(
                    variation,
                    product_standard_package=product_standard_package,
                ))
    return normalized


def flatten_pricing_options(
    payload: dict[str, Any],
    requested_quantity: int,
) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for source, options in _pricing_option_groups(payload):
        for option in options:
            if not isinstance(option, dict):
                continue
            products = option.get("Products")
            if not isinstance(products, list):
                products = option.get("PricingOptionsForQuantityProducts")
            if not isinstance(products, list):
                products = [option]

            option_type = first_present(
                option,
                "PricingOption",
                "OptionType",
                "PricingOptionType",
                default="Unknown",
            )
            option_total = as_float(first_present(option, "TotalPrice", "ExtendedPrice"), 0)
            option_quantity = as_int(
                first_present(option, "TotalQuantityPriced", "Quantity", "RequestedQuantity"),
                requested_quantity,
            )

            for product in products:
                if not isinstance(product, dict):
                    continue
                quantity = as_int(
                    first_present(
                        product,
                        "Quantity",
                        "RequestedQuantity",
                        "QuantityPriced",
                        "TotalQuantityPriced",
                    ),
                    option_quantity,
                )
                total = as_float(
                    first_present(product, "TotalPrice", "ExtendedPrice", "Price"),
                    option_total,
                )
                unit = as_float(
                    first_present(product, "UnitPrice", "PriceEach", "UnitPriceWithTariff"),
                    total / quantity if quantity else 0,
                )
                if total <= 0 and unit > 0 and quantity > 0:
                    total = unit * quantity
                if unit <= 0 and total > 0 and quantity > 0:
                    unit = total / quantity

                package = first_present(
                    product,
                    "PackageType",
                    "Packaging",
                    "PackageTypeName",
                    default="",
                )
                if isinstance(package, dict):
                    package = first_present(package, "Name", "Id", default="")
                tariff = first_present(product, "TariffInformation", default={})
                marketplace = bool(
                    first_present(product, "IsMarketplace", "Marketplace", default=False)
                )
                product_container = payload.get("Product")
                product_standard_package = first_present(
                    product_container if isinstance(product_container, dict) else payload,
                    "StandardPackage",
                    default=first_present(option, "StandardPackage"),
                )
                moq = normalize_effective_moq(
                    product,
                    product_standard_package=product_standard_package,
                )
                flattened.append(
                    {
                        "source": source,
                        "option_type": option_type,
                        "digi_key_part_number": first_present(
                            product,
                            "DigiKeyProductNumber",
                            "DigiKeyPartNumber",
                            "ProductNumber",
                            default=first_present(
                                option,
                                "DigiKeyProductNumber",
                                "DigiKeyPartNumber",
                                "ProductNumber",
                            ),
                        ),
                        "manufacturer_part_number": first_present(
                            product,
                            "ManufacturerPartNumber",
                            default=first_present(option, "ManufacturerPartNumber"),
                        ),
                        "package_type": package,
                        "quantity": quantity,
                        "requested_quantity": requested_quantity,
                        "unit_price": round(unit, 8),
                        "total_price": round(total, 4),
                        # PricingOptionsByQuantity places availability on the
                        # parent pricing option, not its nested product row.
                        "quantity_available": (
                            as_int(option.get("QuantityAvailable"))
                            if option.get("QuantityAvailable") is not None
                            else None
                        ),
                        "variation_quantity_available": None,
                        **moq,
                        "is_marketplace": marketplace,
                        "tariff": tariff,
                        "raw": product,
                    }
                )
    return flattened


def _has_tariff(option: dict[str, Any]) -> bool:
    tariff = option.get("tariff")
    if isinstance(tariff, dict):
        for key in ("Tariff", "TariffPrice", "TariffAmount", "TariffPercentage"):
            if as_float(tariff.get(key), 0) > 0:
                return True
        return bool(
            tariff.get("TariffActive")
            or tariff.get("IsTariffActive")
            or tariff.get("IsTariffApplied")
            or tariff.get("HasTariff")
        )
    return bool(tariff)


def choose_pricing_option(
    payload: dict[str, Any],
    requested_quantity: int,
    *,
    allow_marketplace: bool,
    allow_tariff: bool,
    allow_quantity_increase: bool,
    preferred_package: str | None = None,
) -> dict[str, Any]:
    options = flatten_pricing_options(payload, requested_quantity)
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    preferred_norm = normalize_part_number(preferred_package)

    for option in options:
        reasons: list[str] = []
        if not allow_marketplace and option["is_marketplace"]:
            reasons.append("marketplace")
        if not allow_tariff and _has_tariff(option):
            reasons.append("tariff")
        if option["quantity"] < requested_quantity:
            reasons.append("insufficient_quantity")
        effective_moq = option.get("effective_minimum_order_quantity")
        if effective_moq is not None and option["quantity"] < effective_moq:
            reasons.append("below_effective_moq")
        if not allow_quantity_increase and option["quantity"] != requested_quantity:
            reasons.append("quantity_change")
        if option["total_price"] <= 0:
            reasons.append("no_price")
        if reasons:
            rejected.append({**option, "rejection_reasons": reasons})
        else:
            eligible.append(option)

    def ranking(option: dict[str, Any]) -> tuple[Any, ...]:
        package_norm = normalize_part_number(str(option.get("package_type") or ""))
        package_penalty = 0 if preferred_norm and preferred_norm in package_norm else (1 if preferred_norm else 0)
        exact_penalty = 0 if option["quantity"] == requested_quantity else 1
        source_penalty = 0 if option["source"] == "MyPricingOptions" else 1
        return (
            package_penalty,
            round(option["total_price"], 8),
            exact_penalty,
            option["quantity"],
            source_penalty,
        )

    eligible.sort(key=ranking)
    recommendation = eligible[0] if eligible else None
    exact_candidates = [
        item for item in eligible if item["quantity"] == requested_quantity
    ]
    exact_candidates.sort(key=ranking)
    exact = exact_candidates[0] if exact_candidates else None

    savings = None
    if recommendation and exact and exact["total_price"] > 0:
        savings = round(exact["total_price"] - recommendation["total_price"], 4)

    return {
        "requested_quantity": requested_quantity,
        "recommendation": recommendation,
        "exact_quantity_option": exact,
        "savings_vs_exact": savings,
        "eligible_options": eligible,
        "rejected_options": rejected,
        "option_count": len(options),
    }


def attach_variation_availability(
    decision: dict[str, Any], variation_stock: dict[str, int | None]
) -> None:
    """Add real-time ProductDetails package stock without conflating sources."""
    for collection in ("eligible_options", "rejected_options"):
        for option in decision.get(collection, []):
            if isinstance(option, dict):
                option["variation_quantity_available"] = variation_stock.get(
                    normalize_part_number(str(option.get("digi_key_part_number") or ""))
                )


def _alternate_product_numbers(payload: Any, original_number: str) -> list[str]:
    """Extract alternate DigiKey product numbers from API response variants."""
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("DigiKeyProductNumber", "DigiKeyPartNumber", "ProductNumber"):
                number = value.get(key)
                if isinstance(number, str) and number.strip():
                    found.append(number)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    original = normalize_part_number(original_number)
    return list(dict.fromkeys(number for number in found if normalize_part_number(number) != original))


def _rank_pricing_candidate(option: dict[str, Any], preferred_package: str | None) -> tuple[Any, ...]:
    preferred = normalize_part_number(preferred_package)
    package = normalize_part_number(str(option.get("package_type") or ""))
    return (
        0 if preferred and preferred in package else (1 if preferred else 0),
        round(as_float(option.get("total_price"), 0), 8),
        0 if option.get("quantity") == option.get("requested_quantity") else 1,
        as_int(option.get("quantity"), 0),
        0 if option.get("source") == "MyPricingOptions" else 1,
    )


def _alternate_details_with_retry(product_number: str, authorization: str, account_id: str | None) -> DigiKeyResponse:
    try:
        return get_product_details(product_number, authorization, account_id=account_id)
    except DigiKeyHTTPError as exc:
        if exc.status_code not in {500, 503}:
            raise
        try:
            response = get_product_details(product_number, authorization, account_id=account_id)
            response.meta["alternate_details_attempts"] = [exc.meta, response.meta]
            return response
        except DigiKeyHTTPError as retry_exc:
            retry_exc.meta["alternate_details_attempts"] = [exc.meta, retry_exc.meta]
            raise


def _is_active_purchasable(product: dict[str, Any]) -> bool:
    status = _status_text(product).lower()
    if any(token in status for token in ("obsolete", "discontinued", "end of life", "not for new")):
        return False
    identifiers = [
        first_present(product, "DigiKeyProductNumber", "DigiKeyPartNumber", "ProductNumber", default=""),
        *[
            first_present(variation, "DigiKeyProductNumber", "DigiKeyPartNumber", "ProductNumber", default="")
            for variation in _detail_variations(product)
        ],
    ]
    return any(str(identifier).strip() for identifier in identifiers)


def _extract_product(payload: dict[str, Any]) -> dict[str, Any]:
    product = payload.get("Product")
    if isinstance(product, dict):
        return product
    products = payload.get("Products")
    if isinstance(products, list) and products and isinstance(products[0], dict):
        return products[0]
    return payload if isinstance(payload, dict) else {}


def _status_text(product: dict[str, Any]) -> str:
    status = product.get("ProductStatus") or product.get("Status") or ""
    if isinstance(status, dict):
        return str(first_present(status, "Status", "Name", "Id", default=""))
    return str(status or "")


def summarize_product_detail(
    payload: dict[str, Any],
    *,
    requested_quantity: int,
    maximum_lead_weeks: int,
    requested_product_number: str | None = None,
) -> dict[str, Any]:
    product = _extract_product(payload)
    description = product.get("Description")
    if isinstance(description, dict):
        description_text = first_present(
            description,
            "ProductDescription",
            "DetailedDescription",
            "SeoDescription",
            default="",
        )
    else:
        description_text = str(description or "")

    manufacturer = product.get("Manufacturer")
    manufacturer_name = (
        first_present(manufacturer, "Name", "Value", default="")
        if isinstance(manufacturer, dict)
        else str(manufacturer or "")
    )
    category = product.get("Category")
    category_name = (
        first_present(category, "Name", "Description", default="")
        if isinstance(category, dict)
        else str(category or "")
    )
    classifications = product.get("Classifications") or {}
    lead_weeks = as_int(product.get("ManufacturerLeadWeeks"), 0)
    stock = as_int(
        first_present(
            product,
            "QuantityAvailable",
            "ManufacturerPublicQuantity",
            "QuantityOnHand",
        ),
        0,
    )
    status = _status_text(product)
    status_lower = status.lower()
    last_buy = product.get("DateLastBuyChance")
    normally_stocking = bool(first_present(product, "NormallyStocking", default=False))
    ncnr = bool(first_present(product, "Ncnr", "NCNR", default=False))
    discontinued = bool(first_present(product, "Discontinued", "IsDiscontinued", default=False))
    end_of_life = bool(first_present(product, "EndOfLife", "IsObsolete", default=False))

    variations = product.get("ProductVariations") or product.get("Variations") or []
    resolved_digi_key_number = str(
        first_present(
            product,
            "DigiKeyProductNumber",
            "DigiKeyPartNumber",
            "ProductNumber",
            default="",
        )
        or ""
    )
    if not resolved_digi_key_number and isinstance(variations, list):
        requested_norm = normalize_part_number(requested_product_number)
        variation_numbers = [
            str(first_present(v, "DigiKeyProductNumber", "DigiKeyPartNumber", default=""))
            for v in variations
            if isinstance(v, dict)
        ]
        resolved_digi_key_number = next(
            (number for number in variation_numbers if normalize_part_number(number) == requested_norm),
            next((number for number in variation_numbers if number), ""),
        )

    risks: list[str] = []
    if discontinued or end_of_life or any(
        token in status_lower for token in ("discontinued", "obsolete", "end of life", "eol")
    ):
        risks.append("lifecycle_status")
    if last_buy:
        risks.append("last_buy_date")
    if stock < requested_quantity:
        risks.append("insufficient_stock")
    if lead_weeks > maximum_lead_weeks:
        risks.append("long_lead_time")
    if not normally_stocking and "active" not in status_lower:
        risks.append("not_normally_stocked")

    return {
        "digi_key_part_number": resolved_digi_key_number,
        "manufacturer_part_number": product.get("ManufacturerProductNumber") or "",
        "manufacturer": manufacturer_name,
        "description": description_text,
        "category": category_name,
        "product_status": status,
        "quantity_available": stock,
        "requested_quantity": requested_quantity,
        "manufacturer_lead_weeks": lead_weeks,
        "normally_stocking": normally_stocking,
        "ncnr": ncnr,
        "date_last_buy_chance": last_buy,
        "classifications": classifications,
        "parameters": product.get("Parameters") or [],
        "variations": variations,
        "risks": risks,
    }


def _workflow_call(
    product_number: str,
    fn: Callable[[], Any],
) -> tuple[str, Any, WorkflowError | None]:
    try:
        return product_number, fn(), None
    except DigiKeyHTTPError as exc:
        return product_number, None, WorkflowError(
            product_number, exc.status_code, exc.detail, exc.meta
        )
    except Exception as exc:  # pragma: no cover, defensive boundary
        return product_number, None, WorkflowError(
            product_number,
            None,
            f"{exc.__class__.__name__}: {exc}",
        )


def _limited_items(items: Sequence[BOMItem]) -> list[BOMItem]:
    consolidated = consolidate_bom_items(items)
    if len(consolidated) > settings.max_bulk_items:
        raise ValueError(
            f"This deployment allows at most {settings.max_bulk_items} unique BOM items per workflow"
        )
    return consolidated


def optimize_one_item(
    item: BOMItem,
    authorization: str,
    request: PricingOptimizationRequest,
) -> dict[str, Any]:
    pricing = get_pricing_by_quantity(
        item.product_number,
        item.quantity,
        authorization,
        account_id=request.account_id,
    )
    decision = choose_pricing_option(
        pricing.data if isinstance(pricing.data, dict) else {},
        item.quantity,
        allow_marketplace=request.allow_marketplace,
        allow_tariff=request.allow_tariff,
        allow_quantity_increase=request.allow_quantity_increase,
        preferred_package=item.preferred_package,
    )

    alternate_packaging: dict[str, Any] | None = None
    alternate_errors: list[dict[str, Any]] = []
    skipped_alternates: list[dict[str, Any]] = []
    alternate_candidates: list[dict[str, Any]] = []
    try:
        alternate_response = get_alternate_packaging(item.product_number, authorization)
        alternate_packaging = alternate_response.public()
        for alternate_number in _alternate_product_numbers(
            alternate_response.data, item.product_number
        ):
            try:
                details = _alternate_details_with_retry(
                    alternate_number, authorization, request.account_id
                )
                candidate_product = _extract_product(
                    details.data if isinstance(details.data, dict) else {}
                )
                if not _is_active_purchasable(candidate_product):
                    skipped_alternates.append({
                        "product_number": alternate_number,
                        "status": "skipped",
                        "reason": "no_active_purchasable_variation",
                        "_meta": details.meta,
                    })
                    continue
                alternate_pricing = get_pricing_by_quantity(
                    alternate_number, item.quantity, authorization, account_id=request.account_id
                )
                alternate_decision = choose_pricing_option(
                    alternate_pricing.data if isinstance(alternate_pricing.data, dict) else {},
                    item.quantity,
                    allow_marketplace=request.allow_marketplace,
                    allow_tariff=request.allow_tariff,
                    allow_quantity_increase=request.allow_quantity_increase,
                    preferred_package=item.preferred_package,
                )
                for option in alternate_decision["eligible_options"]:
                    alternate_candidates.append({
                        **option,
                        "source": "AlternatePackaging",
                        "alternate_product_number": alternate_number,
                    })
                if not alternate_decision["eligible_options"]:
                    skipped_alternates.append({
                        "product_number": alternate_number,
                        "status": "skipped",
                        "reason": "no_eligible_pricing",
                        "_meta": alternate_pricing.meta,
                    })
            except DigiKeyHTTPError as exc:
                alternate_errors.append({
                    "product_number": alternate_number,
                    "status": "upstream_error",
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                    "attempts": exc.meta.get("alternate_details_attempts") or exc.meta.get("recommendation_attempts") or [exc.meta],
                    "_meta": exc.meta,
                })
    except DigiKeyHTTPError as exc:
        alternate_errors.append({
            "status_code": exc.status_code, "detail": exc.detail, "_meta": exc.meta,
        })

    if alternate_candidates:
        all_eligible = [*decision["eligible_options"], *alternate_candidates]
        all_eligible.sort(key=lambda candidate: _rank_pricing_candidate(candidate, item.preferred_package))
        decision["eligible_options"] = all_eligible
        decision["recommendation"] = all_eligible[0]
        exact = [candidate for candidate in all_eligible if candidate["quantity"] == item.quantity]
        decision["exact_quantity_option"] = exact[0] if exact else None
        if decision["exact_quantity_option"]:
            decision["savings_vs_exact"] = round(
                as_float(decision["exact_quantity_option"].get("total_price"))
                - as_float(decision["recommendation"].get("total_price")), 4
            )

    digireel: Any = None
    if request.include_digireel:
        candidates = [
            option
            for option in decision["eligible_options"]
            if "DIGI" in normalize_part_number(str(option.get("package_type") or ""))
            or "DIGI" in normalize_part_number(str(option.get("digi_key_part_number") or ""))
        ]
        candidate_number = (
            candidates[0].get("digi_key_part_number") if candidates else item.product_number
        )
        try:
            digireel_response = get_digireel_pricing(
                str(candidate_number),
                item.quantity,
                authorization,
                account_id=request.account_id,
            )
            digireel = digireel_response.public()
        except DigiKeyHTTPError as exc:
            if exc.status_code not in {400, 404, 422}:
                digireel = {
                    "error": exc.detail,
                    "status_code": exc.status_code,
                    "_meta": exc.meta,
                }

    return {
        "requested": item.model_dump(),
        "pricing_decision": decision,
        "digireel_pricing": digireel,
        "alternate_packaging": alternate_packaging,
        "alternate_packaging_errors": alternate_errors,
        "skipped_alternates": skipped_alternates,
        "_meta": pricing.meta,
    }


def optimize_bom_pricing(
    request: PricingOptimizationRequest,
    authorization: str,
) -> dict[str, Any]:
    items = _limited_items(request.items)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=min(settings.workflow_concurrency, len(items))) as executor:
        futures = {
            executor.submit(
                _workflow_call,
                item.product_number,
                lambda item=item: optimize_one_item(item, authorization, request),
            ): item
            for item in items
        }
        for future in as_completed(futures):
            _, value, error = future.result()
            if error:
                errors.append(error.public())
            else:
                results.append(value)

    results.sort(key=lambda row: normalize_part_number(row["requested"]["product_number"]))
    total = round(
        sum(
            as_float(
                first_present(
                    row.get("pricing_decision", {}).get("recommendation", {}),
                    "total_price",
                ),
                0,
            )
            for row in results
        ),
        4,
    )
    unresolved = [
        row["requested"]["product_number"]
        for row in results
        if not row.get("pricing_decision", {}).get("recommendation")
    ]
    return {
        "summary": {
            "unique_items": len(items),
            "optimized_items": len(results),
            "failed_items": len(errors),
            "estimated_total": total,
            "currency": settings.currency,
            "unresolved_items": unresolved,
        },
        "items": results,
        "errors": errors,
    }


def analyze_one_bom_item(
    item: BOMItem,
    authorization: str,
    request: BulkBOMRequest,
) -> dict[str, Any]:
    details = get_product_details(
        item.product_number,
        authorization,
        manufacturer_id=item.manufacturer_id,
        account_id=request.account_id,
    )
    summary = summarize_product_detail(
        details.data if isinstance(details.data, dict) else {},
        requested_quantity=item.quantity,
        maximum_lead_weeks=request.maximum_lead_weeks,
        requested_product_number=item.product_number,
    )
    actual_number = summary["digi_key_part_number"] or item.product_number

    pricing_request = PricingOptimizationRequest(
        items=[item],
        account_id=request.account_id,
        allow_marketplace=not request.exclude_marketplace,
        allow_tariff=not request.exclude_tariff,
        allow_quantity_increase=True,
        include_digireel=False,
    )
    pricing = optimize_one_item(item, authorization, pricing_request)
    variation_stock = {
        normalize_part_number(str(first_present(variation, "DigiKeyProductNumber", "DigiKeyPartNumber", default=""))): (
            as_int(variation.get("QuantityAvailableforPackageType"))
            if variation.get("QuantityAvailableforPackageType") is not None
            else None
        )
        for variation in _detail_variations(_extract_product(details.data if isinstance(details.data, dict) else {}))
    }
    attach_variation_availability(pricing["pricing_decision"], variation_stock)

    output: dict[str, Any] = {
        "requested": item.model_dump(),
        "product": summary,
        "pricing": pricing["pricing_decision"],
        "variation_stock": variation_stock,
        "product_details_meta": details.meta,
    }

    risky = bool(summary["risks"])
    if request.include_product_change_notifications:
        try:
            pcn = get_product_change_notifications(actual_number, authorization)
            output["change_notifications"] = pcn.public()
            notifications = (
                pcn.data.get("ProductChangeNotifications", [])
                if isinstance(pcn.data, dict)
                else []
            )
            if notifications:
                summary["risks"].append("product_change_notifications")
                risky = True
        except DigiKeyHTTPError as exc:
            output["change_notifications_error"] = {
                "status_code": exc.status_code,
                "detail": exc.detail,
                "_meta": exc.meta,
            }

    if request.include_alternate_packaging:
        try:
            output["alternate_packaging"] = get_alternate_packaging(
                actual_number,
                authorization,
            ).public()
        except DigiKeyHTTPError as exc:
            output["alternate_packaging_error"] = {
                "status_code": exc.status_code,
                "detail": exc.detail,
                "_meta": exc.meta,
            }

    if risky and request.include_substitutions_for_risky_parts:
        try:
            output["substitutions"] = get_substitutions(
                actual_number,
                authorization,
                limit=10,
                exclude_marketplace=request.exclude_marketplace,
            ).public()
        except DigiKeyHTTPError as exc:
            output["substitutions_error"] = {
                "status_code": exc.status_code,
                "detail": exc.detail,
                "_meta": exc.meta,
            }
    return output


def analyze_bom(request: BulkBOMRequest, authorization: str) -> dict[str, Any]:
    items = _limited_items(request.items)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=min(settings.workflow_concurrency, len(items))) as executor:
        futures = {
            executor.submit(
                _workflow_call,
                item.product_number,
                lambda item=item: analyze_one_bom_item(item, authorization, request),
            ): item
            for item in items
        }
        for future in as_completed(futures):
            _, value, error = future.result()
            if error:
                errors.append(error.public())
            else:
                results.append(value)

    results.sort(key=lambda row: normalize_part_number(row["requested"]["product_number"]))
    total_cost = round(
        sum(
            as_float(
                first_present(row.get("pricing", {}).get("recommendation", {}), "total_price"),
                0,
            )
            for row in results
        ),
        4,
    )
    risk_items = [
        {
            "product_number": row["requested"]["product_number"],
            "risks": row["product"].get("risks", []),
        }
        for row in results
        if row["product"].get("risks")
    ]
    return {
        "summary": {
            "unique_items": len(items),
            "analyzed_items": len(results),
            "failed_items": len(errors),
            "estimated_total": total_cost,
            "currency": settings.currency,
            "risk_item_count": len(risk_items),
        },
        "risk_items": risk_items,
        "items": results,
        "errors": errors,
    }


# MyLists helpers

def list_my_lists(
    authorization: str,
    *,
    account_id: str | None = None,
    start_index: int = 0,
    limit: int = 50,
) -> DigiKeyResponse:
    return client.request(
        "GET",
        MYLISTS_BASE,
        authorization,
        account_id=account_id,
        params={"startIndex": start_index, "limit": max(1, min(100, limit))},
    )


def get_my_list(
    list_id: str,
    authorization: str,
    *,
    account_id: str | None = None,
) -> DigiKeyResponse:
    try:
        response = client.request(
            "GET",
            f"{MYLISTS_BASE}/{q(list_id)}",
            authorization,
            account_id=account_id,
        )
    except DigiKeyHTTPError as exc:
        if exc.status_code != 403:
            raise
        raise DigiKeyHTTPError(
            403,
            {
                "category": "authorization_or_absence",
                "resource_state": "deleted_or_inaccessible",
                "upstream_status": 403,
                "list_id": list_id,
                "upstream_problem": exc.detail,
            },
            exc.meta,
        ) from exc
    if isinstance(response.data, dict):
        response.data = normalize_mylist_access(response.data)
    return response


def normalize_mylist_access(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    settings_value = normalized.get("ListSettings")
    list_settings = settings_value if isinstance(settings_value, dict) else {}
    raw_visibility = list_settings.get("Visibility")
    visibility = normalize_part_number(str(raw_visibility or "")).replace("_", "")
    can_edit = normalized.get("CanEdit")
    warnings: list[str] = []
    if can_edit is True:
        effective_access = "editable"
        if visibility == "READONLY":
            warnings.append("raw visibility conflicts with CanEdit=true")
    elif visibility == "READONLY":
        effective_access = "read_only"
    else:
        effective_access = "unknown"
    normalized.update({
        "raw_visibility": raw_visibility,
        "effective_access": effective_access,
        "access_warnings": warnings,
    })
    return normalized


def get_all_list_parts(
    list_id: str,
    authorization: str,
    *,
    account_id: str | None = None,
    assemblies: int = 1,
    include_attrition: bool = False,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    start = 0
    page_size = 100
    while True:
        response = client.request(
            "GET",
            f"{MYLISTS_BASE}/{q(list_id)}/parts",
            authorization,
            account_id=account_id,
            params={
                "countryIso": settings.site,
                "currencyIso": settings.currency,
                "languageIso": settings.language,
                "startIndex": start,
                "limit": page_size,
                "assemblies": assemblies,
                "includeAttrition": include_attrition,
            },
        )
        data = response.data
        page = data.get("PartsList", []) if isinstance(data, dict) else []
        if not isinstance(page, list):
            page = []
        parts.extend(item for item in page if isinstance(item, dict))
        total = as_int(data.get("TotalParts"), len(parts)) if isinstance(data, dict) else len(parts)
        if not page or len(parts) >= total or len(page) < page_size:
            break
        start += len(page)
    return parts


def _compact_pack_options(part: dict[str, Any], selected: dict[str, Any]) -> list[dict[str, Any]]:
    raw_options = (
        selected.get("PackOptions")
        or part.get("PackOptions")
        or part.get("ProductVariations")
        or []
    )
    if not isinstance(raw_options, list):
        raw_options = []
    selected_quantity = as_int(first_present(
        selected, "QuantityRequested", "CalculatedQuantity", "Quantity", default=0
    ), 0)
    product_standard_package = first_present(part, "StandardPackage")
    options: list[dict[str, Any]] = []
    for raw in raw_options:
        if not isinstance(raw, dict):
            continue
        moq = normalize_effective_moq(
            raw, product_standard_package=product_standard_package
        )
        unit_price = as_float(first_present(raw, "UnitPrice", "Price", default=0), 0)
        options.append({
            "digikey_part_number": first_present(
                raw, "DigiKeyPartNumber", "DigiKeyProductNumber", "ProductNumber"
            ),
            "package": first_present(
                raw, "PackageType", "SelectedPackType", "Packaging", default=""
            ),
            "effective_moq": moq["effective_minimum_order_quantity"],
            "quantity_available": first_present(
                raw, "QuantityAvailable", "QuantityAvailableforPackageType"
            ),
            "unit_price": unit_price if unit_price > 0 else None,
            "extended_price": (
                round(unit_price * selected_quantity, 4)
                if unit_price > 0 and selected_quantity > 0
                else None
            ),
        })
    return options


def _without_empty_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, item in value.items()
            if (cleaned := _without_empty_fields(item)) not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [
            cleaned for item in value
            if (cleaned := _without_empty_fields(item)) not in (None, "", [], {})
        ]
    return value


def present_mylist_parts(
    parts: Sequence[dict[str, Any]],
    *,
    list_id: str | None = None,
    response_detail: Literal["compact", "full"] = "compact",
    include_substitutions: bool = False,
    substitution_limit: int = 5,
    include_environmental_docs: bool = False,
    include_images: bool = False,
    include_empty_fields: bool = False,
) -> dict[str, Any]:
    raw_parts = list(parts)
    base = {
        **({"list_id": list_id} if list_id is not None else {}),
        "total_parts": len(raw_parts),
    }
    if response_detail == "full":
        return {**base, "parts": raw_parts}

    limit = max(0, min(25, substitution_limit))
    compact_parts: list[dict[str, Any]] = []
    omitted = {"substitutions": 0, "environmental_documents": 0, "images": 0}
    for part in raw_parts:
        quantities = part.get("Quantities") or []
        quantities = [item for item in quantities if isinstance(item, dict)] if isinstance(quantities, list) else []
        selected_index = as_int(part.get("SelectedQuantityIndex"), 0)
        selected = quantities[selected_index] if 0 <= selected_index < len(quantities) else {}
        substitutions = (
            part.get("Substitutions")
            or part.get("ProductSubstitutes")
            or []
        )
        substitutions = substitutions if isinstance(substitutions, list) else []
        returned_substitutions = substitutions[:limit] if include_substitutions else []
        environmental = part.get("EnvironmentalDocuments") or []
        environmental_count = len(environmental) if isinstance(environmental, list) else int(bool(environmental))
        images = first_present(part, "Images", "Image", "ImageMetadata")
        image_count = len(images) if isinstance(images, list) else int(bool(images))
        omitted["substitutions"] += len(substitutions) - len(returned_substitutions)
        omitted["environmental_documents"] += 0 if include_environmental_docs else environmental_count
        omitted["images"] += 0 if include_images else image_count

        description = part.get("Description")
        if isinstance(description, dict):
            description = first_present(
                description,
                "ProductDescription",
                "DetailedDescription",
                "Description",
                default="",
            )
        compact: dict[str, Any] = {
            "unique_id": part.get("UniqueId"),
            "requested_part_number": part.get("RequestedPartNumber"),
            "digikey_part_number": first_present(
                part, "DigiKeyPartNumber", "DigiKeyProductNumber"
            ),
            "manufacturer_part_number": part.get("ManufacturerPartNumber"),
            "manufacturer": first_present(
                part, "ManufacturerName", "RequestedManufacturerName", "Manufacturer"
            ),
            "description": description,
            "customer_reference": part.get("CustomerReference"),
            "reference_designator": part.get("ReferenceDesignator"),
            "notes": part.get("Notes"),
            "selected_quantity": first_present(
                selected, "QuantityRequested", "CalculatedQuantity", "Quantity"
            ),
            "selected_package": first_present(
                selected, "SelectedPackType", "PackageType"
            ),
            "quantity_available": first_present(
                part, "QuantityAvailable", "ManufacturerPublicQuantity"
            ),
            "status": first_present(part, "ProductStatus", "Status"),
            "lead_weeks": part.get("ManufacturerLeadWeeks"),
            "tariff_code": first_present(part, "TariffCode", "Tariff"),
            "marketplace": first_present(
                part, "IsMarketplace", "IsMarketPlace", default=False
            ),
            "pack_options": _compact_pack_options(part, selected),
            "substitutions_total": len(substitutions),
            "substitutions_returned": len(returned_substitutions),
            "substitutions_truncated": len(returned_substitutions) < len(substitutions),
        }
        if include_substitutions:
            compact["substitutions"] = returned_substitutions
        if include_environmental_docs:
            compact["environmental_documents"] = environmental
        if include_images:
            compact["images"] = images
        compact_parts.append(
            compact if include_empty_fields else _without_empty_fields(compact)
        )
    return {
        **base,
        "parts": compact_parts,
        "_meta": {
            "response_detail": "compact",
            "omitted_nested_fields": omitted,
        },
    }


def get_my_list_part(
    list_id: str,
    unique_id: str,
    authorization: str,
    *,
    account_id: str | None = None,
    assemblies: int = 1,
    created_by: str = "",
) -> DigiKeyResponse:
    return client.request(
        "GET",
        f"{MYLISTS_BASE}/{q(list_id)}/parts/{q(unique_id)}",
        authorization,
        account_id=account_id,
        params={
            "countryIso": settings.site,
            "currencyIso": settings.currency,
            "languageIso": settings.language,
            "pricingCountryIso": settings.site,
            "assemblies": assemblies,
            "createdBy": created_by,
        },
    )


def requested_part_from_list_part(existing: dict[str, Any]) -> dict[str, Any]:
    quantities: list[dict[str, Any]] = []
    for quantity in existing.get("Quantities") or []:
        if not isinstance(quantity, dict):
            continue
        requested = first_present(
            quantity,
            "QuantityRequested",
            "CalculatedQuantity",
            "Quantity",
            default=1,
        )
        quantities.append(
            {
                "SelectedPackType": first_present(
                    quantity,
                    "SelectedPackType",
                    "PackageType",
                    default="CutTape",
                ),
                "SelectedSubPackType": quantity.get("SelectedSubPackType") or "",
                "Quantity": max(1, as_int(requested, 1)),
                "TargetPrice": max(0, as_float(quantity.get("TargetPrice"), 0)),
            }
        )
    if not quantities:
        quantities = [
            {
                "SelectedPackType": "CutTape",
                "SelectedSubPackType": "",
                "Quantity": 1,
                "TargetPrice": 0,
            }
        ]

    alternate_parts: list[str] = []
    for option in existing.get("AlternateParts") or []:
        if isinstance(option, str):
            alternate_parts.append(option)
        elif isinstance(option, dict):
            number = first_present(
                option,
                "DigiKeyPartNumber",
                "ManufacturerPartNumber",
                "RequestedPartNumber",
            )
            if number:
                alternate_parts.append(str(number))

    selected_index = as_int(existing.get("SelectedQuantityIndex"), 0)
    if selected_index < 0 or selected_index >= len(quantities):
        selected_index = 0
    requested_number = str(
        first_present(
            existing,
            "RequestedPartNumber",
            "DigiKeyPartNumber",
            "ManufacturerPartNumber",
            default="",
        )
    )
    return {
        "UniqueId": existing.get("UniqueId") or "",
        "PartId": as_int(existing.get("PartId"), 0),
        "RequestedPartNumber": requested_number,
        "OriginalPartNumber": existing.get("OriginalPartNumber") or requested_number,
        "ManufacturerName": first_present(
            existing,
            "RequestedManufacturerName",
            "ManufacturerName",
            "Manufacturer",
            default="",
        ),
        "CustomerReference": existing.get("CustomerReference") or "",
        "ReferenceDesignator": existing.get("ReferenceDesignator") or "",
        "Notes": existing.get("Notes") or "",
        "SelectedQuantityIndex": selected_index,
        "Attrition": max(0, as_float(existing.get("Attrition"), 0)),
        "AlternateParts": alternate_parts,
        "Quantities": quantities,
    }


def requested_part_from_input(part: ListPartInput) -> dict[str, Any]:
    return {
        "UniqueId": "",
        "PartId": 0,
        "RequestedPartNumber": part.product_number,
        "OriginalPartNumber": part.product_number,
        "ManufacturerName": "",
        "CustomerReference": part.customer_reference,
        "ReferenceDesignator": part.reference_designator,
        "Notes": part.notes,
        "SelectedQuantityIndex": 0,
        "Attrition": 0,
        "AlternateParts": [],
        "Quantities": [
            {
                "SelectedPackType": part.package_type,
                "SelectedSubPackType": part.sub_package_type,
                "Quantity": part.quantity,
                "TargetPrice": part.target_price,
            }
        ],
    }


def merge_part_update(payload: dict[str, Any], request: UpdateListPartRequest) -> dict[str, Any]:
    if request.product_number is not None:
        changed = normalize_part_number(request.product_number) != normalize_part_number(
            payload.get("RequestedPartNumber")
        )
        payload["RequestedPartNumber"] = request.product_number
        payload["OriginalPartNumber"] = request.product_number
        if changed:
            payload["PartId"] = 0
            payload["ManufacturerName"] = request.manufacturer_name or ""
            payload["AlternateParts"] = []
    if request.manufacturer_name is not None:
        payload["ManufacturerName"] = request.manufacturer_name
    for key, value in (
        ("CustomerReference", request.customer_reference),
        ("ReferenceDesignator", request.reference_designator),
        ("Notes", request.notes),
        ("Attrition", request.attrition),
        ("AlternateParts", request.alternate_parts),
    ):
        if value is not None:
            payload[key] = value

    quantities = payload.setdefault("Quantities", [])
    selected_index = (
        request.selected_quantity_index
        if request.selected_quantity_index is not None
        else as_int(payload.get("SelectedQuantityIndex"), 0)
    )
    while selected_index >= len(quantities):
        quantities.append(
            {
                "SelectedPackType": "CutTape",
                "SelectedSubPackType": "",
                "Quantity": 1,
                "TargetPrice": 0,
            }
        )
    payload["SelectedQuantityIndex"] = selected_index
    selected = quantities[selected_index]
    if request.quantity is not None:
        selected["Quantity"] = request.quantity
    if request.package_type is not None:
        selected["SelectedPackType"] = request.package_type
    if request.sub_package_type is not None:
        selected["SelectedSubPackType"] = request.sub_package_type
    if request.target_price is not None:
        selected["TargetPrice"] = request.target_price
    return payload


def create_my_list(
    list_name: str,
    authorization: str,
    *,
    tags: Sequence[str] = (),
    created_by: str = "",
    account_id: str | None = None,
) -> DigiKeyResponse:
    body = {
        "ListName": list_name,
        "CreatedBy": created_by,
        "Tags": list(tags),
        "Source": "other",
        "ListSettings": {
            "Visibility": "Private",
            "PackagePreference": "CutTapeOrTR",
            "ColumnPreferences": [],
            "AutoCorrectQuantities": True,
            "AttritionEnabled": False,
            "AutoPopulateCref": True,
        },
    }
    return client.request(
        "POST",
        MYLISTS_BASE,
        authorization,
        account_id=account_id,
        json_body=body,
        safe_retry=False,
    )


def rename_my_list(
    list_id: str,
    new_name: str,
    authorization: str,
    *,
    account_id: str | None = None,
) -> DigiKeyResponse:
    return client.request(
        "PUT",
        f"{MYLISTS_BASE}/{q(list_id)}/listName/{q(new_name)}",
        authorization,
        account_id=account_id,
        safe_retry=False,
    )


def delete_my_list(
    list_id: str,
    authorization: str,
    *,
    account_id: str | None = None,
) -> DigiKeyResponse:
    return client.request(
        "DELETE",
        f"{MYLISTS_BASE}/{q(list_id)}",
        authorization,
        account_id=account_id,
        safe_retry=False,
    )


def add_parts_to_list(
    list_id: str,
    parts: Sequence[ListPartInput],
    authorization: str,
    *,
    account_id: str | None = None,
    insertion_index: int = 0,
) -> list[Any]:
    responses: list[Any] = []
    index = insertion_index
    for page in chunks(list(parts), 100):
        response = client.request(
            "POST",
            f"{MYLISTS_BASE}/{q(list_id)}/parts",
            authorization,
            account_id=account_id,
            params={"index": index},
            json_body=[requested_part_from_input(item) for item in page],
            safe_retry=False,
        )
        responses.append(response.public())
        index += len(page)
    return responses


def update_my_list_part(
    list_id: str,
    unique_id: str,
    request: UpdateListPartRequest,
    authorization: str,
) -> DigiKeyResponse:
    current = get_my_list_part(
        list_id,
        unique_id,
        authorization,
        account_id=request.account_id,
        created_by=request.created_by,
    )
    if not isinstance(current.data, dict):
        raise ValueError("DigiKey returned an unexpected MyLists part response")
    payload = requested_part_from_list_part(current.data)
    payload["UniqueId"] = unique_id
    payload = merge_part_update(payload, request)
    params = {"createdBy": request.created_by} if request.created_by else None
    return client.request(
        "PUT",
        f"{MYLISTS_BASE}/{q(list_id)}/parts/{q(unique_id)}",
        authorization,
        account_id=request.account_id,
        params=params,
        json_body=payload,
        safe_retry=False,
    )


def remove_my_list_part(
    list_id: str,
    unique_id: str,
    authorization: str,
    *,
    created_by: str = "",
    account_id: str | None = None,
) -> DigiKeyResponse:
    params = {"createdBy": created_by} if created_by else None
    return client.request(
        "DELETE",
        f"{MYLISTS_BASE}/{q(list_id)}/parts/{q(unique_id)}",
        authorization,
        account_id=account_id,
        params=params,
        safe_retry=False,
    )


def _selected_list_quantity(part: dict[str, Any]) -> dict[str, Any]:
    quantities = part.get("Quantities") or []
    if not isinstance(quantities, list) or not quantities:
        return {
            "quantity": 1,
            "package_type": "CutTape",
            "sub_package_type": "",
            "target_price": 0,
        }
    index = as_int(part.get("SelectedQuantityIndex"), 0)
    if index < 0 or index >= len(quantities):
        index = 0
    selected = quantities[index] if isinstance(quantities[index], dict) else {}
    return {
        "quantity": max(
            1,
            as_int(
                first_present(
                    selected,
                    "QuantityRequested",
                    "CalculatedQuantity",
                    "Quantity",
                    default=1,
                ),
                1,
            ),
        ),
        "package_type": first_present(
            selected,
            "SelectedPackType",
            "PackageType",
            default="CutTape",
        ),
        "sub_package_type": selected.get("SelectedSubPackType") or "",
        "target_price": max(0, as_float(selected.get("TargetPrice"), 0)),
    }


def _list_part_aliases(part: dict[str, Any]) -> set[str]:
    """Return every stable identifier exposed by MyLists for one part."""
    aliases = {
        normalize_part_number(str(part.get(key) or ""))
        for key in (
            "DigiKeyProductNumber",
            "DigiKeyPartNumber",
            "RequestedPartNumber",
            "ManufacturerPartNumber",
            "OriginalPartNumber",
        )
    }
    return aliases - {""}


def _list_part_key(part: dict[str, Any]) -> str:
    aliases = _list_part_aliases(part)
    return next(iter(sorted(aliases)), "")


def consolidate_list_inputs(items: Sequence[ListPartInput]) -> list[ListPartInput]:
    merged: dict[tuple[str, str, str], ListPartInput] = {}
    for item in items:
        key = (
            normalize_part_number(item.product_number),
            normalize_part_number(item.package_type),
            normalize_part_number(item.sub_package_type),
        )
        if key not in merged:
            merged[key] = item.model_copy(deep=True)
            continue
        current = merged[key]
        current.quantity += item.quantity
        refs = [value for value in (current.reference_designator, item.reference_designator) if value]
        current.reference_designator = ", ".join(dict.fromkeys(refs))
        notes = [value for value in (current.notes, item.notes) if value]
        current.notes = " | ".join(dict.fromkeys(notes))
    return list(merged.values())


def build_list_diff(
    existing_parts: Sequence[dict[str, Any]],
    proposed_items: Sequence[ListPartInput],
    *,
    remove_unlisted: bool,
    consolidate_duplicates: bool,
) -> dict[str, Any]:
    proposed = (
        consolidate_list_inputs(proposed_items)
        if consolidate_duplicates
        else [item.model_copy(deep=True) for item in proposed_items]
    )
    existing_by_alias: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for part in existing_parts:
        for alias in _list_part_aliases(part):
            existing_by_alias[alias].append(part)

    additions: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    removals: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    ambiguities: list[dict[str, Any]] = []
    matched_ids: set[str] = set()

    for proposed_item in proposed:
        key = normalize_part_number(proposed_item.product_number)
        candidates = [
            candidate
            for candidate in existing_by_alias.get(key, [])
            if str(candidate.get("UniqueId") or "") not in matched_ids
        ]
        # Prefer the current package when a list holds multiple package rows for
        # the same component.  Falling back permits an intentional package edit.
        package_matches = [
            candidate for candidate in candidates
            if normalize_part_number(str(_selected_list_quantity(candidate)["package_type"]))
            == normalize_part_number(proposed_item.package_type)
        ]
        if package_matches:
            candidates = package_matches
        canonical_keys = {_list_part_key(candidate) for candidate in candidates}
        if len(canonical_keys) > 1:
            ambiguities.append(
                {
                    "product_number": proposed_item.product_number,
                    "package_type": proposed_item.package_type,
                    "candidate_unique_ids": [str(item.get("UniqueId") or "") for item in candidates],
                    "candidate_part_numbers": [
                        first_present(item, "DigiKeyPartNumber", "RequestedPartNumber", "ManufacturerPartNumber", default="")
                        for item in candidates
                    ],
                    "reason": "multiple_distinct_list_parts_match_product_alias",
                }
            )
            continue
        existing = next((candidate for candidate in candidates), None)
        if existing is None:
            additions.append(proposed_item.model_dump())
            continue

        unique_id = str(existing.get("UniqueId") or "")
        if unique_id:
            matched_ids.add(unique_id)
        selected = _selected_list_quantity(existing)
        before = {
            "product_number": first_present(
                existing,
                "DigiKeyProductNumber",
                "DigiKeyPartNumber",
                "RequestedPartNumber",
                "ManufacturerPartNumber",
                default="",
            ),
            **selected,
            "customer_reference": existing.get("CustomerReference") or "",
            "reference_designator": existing.get("ReferenceDesignator") or "",
            "notes": existing.get("Notes") or "",
        }
        after = proposed_item.model_dump()
        changes: dict[str, dict[str, Any]] = {}
        comparisons = {
            "quantity": (before["quantity"], after["quantity"]),
            "package_type": (before["package_type"], after["package_type"]),
            "sub_package_type": (before["sub_package_type"], after["sub_package_type"]),
            "target_price": (before["target_price"], after["target_price"]),
            "customer_reference": (before["customer_reference"], after["customer_reference"]),
            "reference_designator": (before["reference_designator"], after["reference_designator"]),
            "notes": (before["notes"], after["notes"]),
        }
        for field, (old, new) in comparisons.items():
            if field == "quantity":
                different = as_int(old, 0) != as_int(new, 0)
            elif field == "target_price":
                different = abs(as_float(old, 0) - as_float(new, 0)) > 1e-9
            else:
                different = str(old or "") != str(new or "")
            if different:
                changes[field] = {"before": old, "after": new}

        entry = {
            "unique_id": unique_id,
            "product_number": proposed_item.product_number,
            "before": before,
            "after": after,
            "changes": changes,
        }
        if changes:
            updates.append(entry)
        else:
            unchanged.append(entry)

    seen_existing: set[str] = set()
    for candidates in existing_by_alias.values():
        for index, existing in enumerate(candidates):
            unique_id = str(existing.get("UniqueId") or "")
            identity = unique_id or f"anonymous:{id(existing)}"
            if identity in seen_existing:
                continue
            seen_existing.add(identity)
            same_key = [
                item for item in existing_parts
                if _list_part_key(item) == _list_part_key(existing)
                and normalize_part_number(str(_selected_list_quantity(item)["package_type"]))
                == normalize_part_number(str(_selected_list_quantity(existing)["package_type"]))
            ]
            duplicate = same_key.index(existing) > 0 and consolidate_duplicates
            unlisted = not (_list_part_aliases(existing) & {
                normalize_part_number(item.product_number) for item in proposed
            })
            if unique_id in matched_ids:
                continue
            if duplicate or (remove_unlisted and unlisted):
                removals.append(
                    {
                        "unique_id": unique_id,
                        "product_number": first_present(
                            existing,
                            "DigiKeyPartNumber",
                            "RequestedPartNumber",
                            "ManufacturerPartNumber",
                            default="",
                        ),
                        "reason": "duplicate" if duplicate else "not_in_proposed_list",
                        "current": _selected_list_quantity(existing),
                    }
                )

    return {
        "summary": {
            "additions": len(additions),
            "updates": len(updates),
            "removals": len(removals),
            "unchanged": len(unchanged),
            "ambiguities": len(ambiguities),
        },
        "additions": additions,
        "updates": updates,
        "removals": removals,
        "unchanged": unchanged,
        "ambiguities": ambiguities,
    }


def diff_my_list(
    list_id: str,
    request: ListDiffRequest,
    authorization: str,
) -> dict[str, Any]:
    existing = get_all_list_parts(
        list_id,
        authorization,
        account_id=request.account_id,
    )
    diff = build_list_diff(
        existing,
        request.proposed_items,
        remove_unlisted=request.remove_unlisted,
        consolidate_duplicates=request.consolidate_duplicates,
    )
    diff["list_id"] = list_id
    diff["dry_run"] = True
    return diff


def sync_my_list(
    list_id: str,
    request: ListSyncRequest,
    authorization: str,
) -> dict[str, Any]:
    diff = diff_my_list(list_id, request, authorization)
    applied: dict[str, list[Any]] = {"additions": [], "updates": [], "removals": []}
    errors: list[dict[str, Any]] = []
    if diff["ambiguities"]:
        return {
            "list_id": list_id,
            "diff": diff,
            "applied": applied,
            "errors": [{
                "stage": "identity_resolution",
                "detail": "MyList sync aborted because proposed rows match multiple distinct list parts",
                "ambiguities": diff["ambiguities"],
            }],
            "complete": False,
        }

    additions = [ListPartInput.model_validate(item) for item in diff["additions"]]
    if additions:
        try:
            applied["additions"].extend(
                add_parts_to_list(
                    list_id,
                    additions,
                    authorization,
                    account_id=request.account_id,
                )
            )
        except Exception as exc:
            errors.append({"stage": "additions", "detail": str(exc)})
            if request.stop_on_error:
                return {"list_id": list_id, "diff": diff, "applied": applied, "errors": errors}

    for update in diff["updates"]:
        after = update["after"]
        update_request = UpdateListPartRequest(
            product_number=after["product_number"],
            quantity=after["quantity"],
            package_type=after["package_type"],
            sub_package_type=after.get("sub_package_type", ""),
            customer_reference=after.get("customer_reference", ""),
            reference_designator=after.get("reference_designator", ""),
            notes=after.get("notes", ""),
            target_price=after.get("target_price", 0),
            account_id=request.account_id,
        )
        try:
            response = update_my_list_part(
                list_id,
                update["unique_id"],
                update_request,
                authorization,
            )
            applied["updates"].append(
                {"unique_id": update["unique_id"], "response": response.public()}
            )
        except Exception as exc:
            errors.append(
                {
                    "stage": "update",
                    "unique_id": update["unique_id"],
                    "detail": str(exc),
                }
            )
            if request.stop_on_error:
                return {"list_id": list_id, "diff": diff, "applied": applied, "errors": errors}

    for removal in diff["removals"]:
        try:
            response = remove_my_list_part(
                list_id,
                removal["unique_id"],
                authorization,
                account_id=request.account_id,
            )
            applied["removals"].append(
                {"unique_id": removal["unique_id"], "response": response.public()}
            )
        except Exception as exc:
            errors.append(
                {
                    "stage": "removal",
                    "unique_id": removal["unique_id"],
                    "detail": str(exc),
                }
            )
            if request.stop_on_error:
                break

    return {
        "list_id": list_id,
        "diff": diff,
        "applied": applied,
        "errors": errors,
        "complete": not errors,
    }


def bom_items_from_list_parts(parts: Sequence[dict[str, Any]]) -> list[BOMItem]:
    items: list[BOMItem] = []
    for part in parts:
        number = first_present(
            part,
            "DigiKeyPartNumber",
            "RequestedPartNumber",
            "ManufacturerPartNumber",
        )
        if not number:
            continue
        selected = _selected_list_quantity(part)
        items.append(
            BOMItem(
                product_number=str(number),
                quantity=selected["quantity"],
                customer_reference=part.get("CustomerReference") or "",
                reference_designator=part.get("ReferenceDesignator") or "",
                notes=part.get("Notes") or "",
                preferred_package=str(selected["package_type"] or ""),
            )
        )
    return consolidate_bom_items(items)


def lifecycle_audit(
    request: LifecycleAuditRequest,
    authorization: str,
) -> dict[str, Any]:
    if request.list_id:
        parts = get_all_list_parts(
            request.list_id,
            authorization,
            account_id=request.account_id,
        )
        items = bom_items_from_list_parts(parts)
    else:
        items = _limited_items(request.items or [])

    bom_request = BulkBOMRequest(
        items=items,
        account_id=request.account_id,
        include_product_change_notifications=True,
        include_substitutions_for_risky_parts=request.include_substitutions,
        include_alternate_packaging=False,
        exclude_marketplace=True,
        exclude_tariff=False,
        maximum_lead_weeks=request.maximum_lead_weeks,
    )
    analysis = analyze_bom(bom_request, authorization)
    alerts: list[dict[str, Any]] = []
    for item in analysis["items"]:
        notifications = item.get("change_notifications", {}).get(
            "ProductChangeNotifications", []
        )
        risks = item["product"].get("risks", [])
        if risks or notifications:
            alerts.append(
                {
                    "product_number": item["requested"]["product_number"],
                    "resolved_digi_key_part_number": item["product"].get(
                        "digi_key_part_number"
                    ),
                    "risks": risks,
                    "quantity_available": item["product"].get("quantity_available"),
                    "required_quantity": item["requested"].get("quantity"),
                    "lead_weeks": item["product"].get("manufacturer_lead_weeks"),
                    "last_buy_date": item["product"].get("date_last_buy_chance"),
                    "change_notifications": notifications,
                    "substitutions": item.get("substitutions"),
                }
            )
    return {
        "source": {"list_id": request.list_id} if request.list_id else {"bom": True},
        "summary": {
            "checked_items": analysis["summary"]["analyzed_items"],
            "alert_count": len(alerts),
            "failed_items": analysis["summary"]["failed_items"],
        },
        "alerts": alerts,
        "errors": analysis["errors"],
    }


# Quotes

def list_quotes(
    authorization: str,
    *,
    account_id: str | None = None,
    offset: int = 0,
    limit: int = 10,
) -> DigiKeyResponse:
    return client.request(
        "GET",
        QUOTES_BASE,
        authorization,
        account_id=account_id,
        params={"offset": max(0, offset), "limit": max(1, min(50, limit))},
    )


def create_quote(
    request: CreateQuoteRequest,
    authorization: str,
) -> DigiKeyResponse:
    return client.request(
        "POST",
        QUOTES_BASE,
        authorization,
        account_id=request.account_id,
        json_body={"QuoteName": request.quote_name},
        safe_retry=False,
    )


def get_quote(
    quote_id: int,
    authorization: str,
    *,
    account_id: str | None = None,
) -> DigiKeyResponse:
    return client.request(
        "GET",
        f"{QUOTES_BASE}/{quote_id}",
        authorization,
        account_id=account_id,
    )


def get_quote_products(
    quote_id: int,
    authorization: str,
    *,
    account_id: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> DigiKeyResponse:
    return client.request(
        "GET",
        f"{QUOTES_BASE}/{quote_id}/details",
        authorization,
        account_id=account_id,
        params={"offset": max(0, offset), "limit": max(1, min(50, limit))},
    )


def add_products_to_quote(
    quote_id: int,
    products: Sequence[dict[str, Any]],
    authorization: str,
    *,
    account_id: str,
) -> list[Any]:
    responses: list[Any] = []
    for page in chunks(list(products), 300):
        response = client.request(
            "POST",
            f"{QUOTES_BASE}/{quote_id}/details",
            authorization,
            account_id=account_id,
            json_body=list(page),
            safe_retry=False,
            timeout=max(settings.request_timeout_seconds, 60),
        )
        responses.append(response.public())
    return responses


def _quote_id_from_response(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    for key in ("QuoteId", "quoteId", "Id", "id"):
        value = data.get(key)
        if value is not None:
            return as_int(value, 0) or None
    quote_data = data.get("Quote")
    if isinstance(quote_data, dict):
        return _quote_id_from_response(quote_data)
    return None


def create_quote_from_source(
    request: QuoteFromSourceRequest,
    authorization: str,
) -> dict[str, Any]:
    if request.list_id:
        items = bom_items_from_list_parts(
            get_all_list_parts(
                request.list_id,
                authorization,
                account_id=request.account_id,
            )
        )
    else:
        items = _limited_items(request.items or [])

    created = create_quote(
        CreateQuoteRequest(quote_name=request.quote_name, account_id=request.account_id),
        authorization,
    )
    quote_id = _quote_id_from_response(created.data)
    if quote_id is None:
        return {
            "created_quote": created.public(),
            "products_added": False,
            "error": "DigiKey did not return a recognizable QuoteId",
        }

    products = [
        {
            "ProductNumber": item.product_number,
            "CustomerReference": item.customer_reference,
            "Quantities": [item.quantity],
        }
        for item in items
    ]
    try:
        added = add_products_to_quote(
            quote_id,
            products,
            authorization,
            account_id=request.account_id,
        )
        add_error = None
    except DigiKeyHTTPError as exc:
        added = []
        add_error = {
            "status_code": exc.status_code,
            "detail": exc.detail,
            "_meta": exc.meta,
        }

    return {
        "quote_id": quote_id,
        "quote_name": request.quote_name,
        "source": {"list_id": request.list_id} if request.list_id else {"bom": True},
        "product_count": len(products),
        "created_quote": created.public(),
        "add_products_responses": added,
        "add_products_error": add_error,
        "complete": add_error is None,
    }


# Barcode and packing lists
BARCODE_PATHS = {
    BarcodeType.product_1d: "ProductBarcodes",
    BarcodeType.product_2d: "Product2DBarcodes",
    BarcodeType.pack_list_1d: "PackListBarcodes",
    BarcodeType.pack_list_2d: "PackList2DBarcodes",
}


def decode_barcode(
    request: DecodeBarcodeRequest | BarcodeInput,
    authorization: str,
) -> DigiKeyResponse:
    path = BARCODE_PATHS[request.barcode_type]
    includes = getattr(request, "includes", None)
    params = {"includes": includes} if includes else None
    return client.request(
        "GET",
        f"{BARCODE_BASE}/{path}/{q(request.barcode)}",
        authorization,
        params=params,
    )


def _barcode_lines(barcode_type: BarcodeType, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if barcode_type in {BarcodeType.product_1d, BarcodeType.product_2d}:
        number = first_present(
            payload, "DigiKeyProductNumber", "DigiKeyPartNumber", "ProductNumber"
        )
        if not number:
            return []
        return [
            {
                "digi_key_part_number": str(number),
                "manufacturer_part_number": payload.get("ManufacturerPartNumber"),
                "manufacturer": payload.get("Manufacturer"),
                "description": payload.get("Description"),
                "quantity": max(1, as_int(payload.get("Quantity"), 1)),
            }
        ]

    details = payload.get("PackListDetails") or []
    lines: list[dict[str, Any]] = []
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            number = first_present(
                detail, "DigiKeyProductNumber", "DigiKeyPartNumber", "ProductNumber"
            )
            if number:
                lines.append(
                    {
                        "digi_key_part_number": str(number),
                        "manufacturer_part_number": detail.get("ManufacturerPartNumber"),
                        "quantity": max(1, as_int(detail.get("Quantity"), 1)),
                    }
                )
    return lines


def batch_decode_barcodes(
    barcodes: Sequence[BarcodeInput],
    authorization: str,
) -> dict[str, Any]:
    if len(barcodes) > settings.max_bulk_items:
        raise ValueError(
            f"This deployment allows at most {settings.max_bulk_items} barcodes per batch"
        )
    decoded: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def run(item: BarcodeInput) -> dict[str, Any]:
        response = decode_barcode(item, authorization)
        return {
            "input": item.model_dump(),
            "response": response.public(),
            "lines": _barcode_lines(item.barcode_type, response.data),
        }

    with ThreadPoolExecutor(max_workers=min(settings.workflow_concurrency, len(barcodes))) as executor:
        futures = {executor.submit(run, item): item for item in barcodes}
        for future in as_completed(futures):
            item = futures[future]
            try:
                decoded.append(future.result())
            except DigiKeyHTTPError as exc:
                errors.append(
                    {
                        "input": item.model_dump(),
                        "status_code": exc.status_code,
                        "detail": exc.detail,
                        "_meta": exc.meta,
                        # Keep each failed scan useful to a receiving workflow
                        # without aborting the rest of the batch.  DigiKeyHTTPError
                        # deliberately has no response-style ``public`` method;
                        # errors use the shared boundary envelope instead.
                        "error": error_envelope(
                            exc.status_code,
                            exc.detail,
                            exc.meta,
                        ),
                    }
                )

    totals: dict[str, int] = defaultdict(int)
    for entry in decoded:
        for line in entry["lines"]:
            totals[normalize_part_number(line["digi_key_part_number"])] += as_int(
                line["quantity"], 0
            )
    return {
        "summary": {
            "decoded": len(decoded),
            "failed": len(errors),
            "unique_parts": len(totals),
        },
        "received_quantities": dict(sorted(totals.items())),
        "decoded": decoded,
        "errors": errors,
    }


def compare_barcodes_to_list(
    request: BarcodeListComparisonRequest,
    authorization: str,
) -> dict[str, Any]:
    decoded = batch_decode_barcodes(request.barcodes, authorization)
    parts = get_all_list_parts(
        request.list_id,
        authorization,
        account_id=request.account_id,
    )
    required: dict[str, int] = defaultdict(int)
    display_numbers: dict[str, str] = {}
    for part in parts:
        key = _list_part_key(part)
        if not key:
            continue
        selected = _selected_list_quantity(part)
        required[key] += selected["quantity"]
        display_numbers[key] = str(
            first_present(
                part,
                "DigiKeyProductNumber",
                "DigiKeyPartNumber",
                "RequestedPartNumber",
                "ManufacturerPartNumber",
                default=key,
            )
        )

    received = decoded["received_quantities"]
    all_keys = sorted(set(required) | set(received))
    has_decode_failures = bool(decoded["errors"])
    comparison: list[dict[str, Any]] = []
    for key in all_keys:
        req = required.get(key, 0)
        got = as_int(received.get(key), 0)
        # A failed scan makes only the corresponding unresolved list line
        # uncertain.  A successfully decoded line remains authoritative even
        # when its received quantity is below the required quantity.
        if req and key not in received and has_decode_failures:
            comparison_status = "unknown_due_to_decode_failure"
        elif got == req:
            comparison_status = "complete"
        elif got < req:
            comparison_status = "short"
        else:
            comparison_status = "extra"
        comparison.append(
            {
                "product_number": display_numbers.get(key, key),
                "required_quantity": req,
                "received_quantity": got,
                "difference": got - req,
                "status": comparison_status,
            }
        )
    warnings = []
    if has_decode_failures:
        warnings.append({
            "code": "barcode_decode_failed",
            "failed_count": len(decoded["errors"]),
            "message": (
                "Shortage conclusions are incomplete because one or more "
                "barcodes failed to decode."
            ),
        })
    return {
        "list_id": request.list_id,
        "status": "partial" if has_decode_failures else "success",
        "warnings": warnings,
        "summary": {
            "complete": sum(item["status"] == "complete" for item in comparison),
            "short": sum(item["status"] == "short" for item in comparison),
            "unknown": sum(
                item["status"] == "unknown_due_to_decode_failure"
                for item in comparison
            ),
            "extra": sum(item["status"] == "extra" for item in comparison),
        },
        "comparison": comparison,
        "barcode_decode": decoded,
    }


def lookup_packing_list(
    request: PackingListLookupRequest,
    authorization: str,
) -> DigiKeyResponse:
    path_by_type = {
        PackingListLookupType.invoice: "invoice",
        PackingListLookupType.sales_order: "salesorderid",
        PackingListLookupType.purchase_order: "purchaseordernumber",
    }
    return client.request(
        "GET",
        f"{PACKING_LIST_BASE}/{path_by_type[request.lookup_type]}/{q(request.value)}",
        authorization,
        params={"includePdf": request.include_pdf},
    )
