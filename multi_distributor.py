from __future__ import annotations

import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from client import DigiKeyHTTPError, error_envelope
from config import settings
from credentials import CredentialPurpose, CredentialUnavailableError, Provider, provider_configured
from distributor_models import (
    ComponentComparisonRequest,
    ComponentRecommendationRequest,
    ComparisonResult,
    DistributorOffer,
    MouserSearchMode,
    MouserSearchRequest,
    SourceResult,
)
from models import ProductSearchRequest
from mouser_client import MouserHTTPError
from mouser_services import mouser_adapter, normalize_mouser_offer
from normalization import (
    component_identity,
    effective_purchase_quantity,
    evaluate_requirement,
    money_string,
    normalize_manufacturer,
    normalize_attribute_name,
    normalize_mpn,
    parse_decimal,
    parse_int,
    select_price_break,
)
from services import (
    flatten_pricing_options,
    get_pricing_by_quantity,
    search_products,
)


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _digikey_manufacturer(product: dict[str, Any]) -> str:
    value = product.get("Manufacturer") or product.get("ManufacturerName") or ""
    if isinstance(value, dict):
        return str(_first(value, "Name", "Value", default=""))
    return str(value)


_DIGIKEY_VOLTAGE_RANGE_NAMES = {
    "voltagesupplyspanminmax",
    "supplyvoltagespanminmax",
}
_DIGIKEY_VOLTAGE_MIN_NAMES = {
    "voltagesupplyspanmin",
    "supplyvoltagespanmin",
}
_DIGIKEY_VOLTAGE_MAX_NAMES = {
    "voltagesupplyspanmax",
    "supplyvoltagespanmax",
}
_DIGIKEY_ROHS_NAMES = {
    "rohs",
    "rohsstatus",
    "rohscompliance",
    "rohscompliant",
    "rohs3status",
    "rohs3compliance",
    "rohs3compliant",
}
_RANGE_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")
_RANGE_CONNECTOR_WORDS = {"to", "through", "and", "or"}


def _range_unit_after(text: str, match: re.Match[str]) -> str:
    suffix = text[match.end() :]
    unit_match = re.match(r"\s*([%°µμA-Za-zΩ]+)", suffix)
    if not unit_match:
        return ""
    unit = unit_match.group(1)
    return "" if unit.casefold() in _RANGE_CONNECTOR_WORDS else unit


def _range_endpoints(value: Any, unit: Any) -> tuple[str, str] | None:
    text = str(value or "").strip()
    matches = list(_RANGE_NUMBER_PATTERN.finditer(text))
    if len(matches) != 2:
        return None
    shared_unit = str(unit or "").strip()
    if not shared_unit:
        shared_unit = _range_unit_after(text, matches[1]) or _range_unit_after(
            text, matches[0]
        )
    values = [match.group(0) for match in matches]
    if shared_unit:
        return values[0] + " " + shared_unit, values[1] + " " + shared_unit
    return values[0], values[1]


def _digikey_attributes(product: dict[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for item in product.get("Parameters") or []:
        if not isinstance(item, dict):
            continue
        name = _first(
            item,
            "ParameterText",
            "Parameter",
            "Name",
            default="",
        )
        value = _first(item, "ValueText", "Value", default=None)
        unit = _first(item, "ValueUnit", "Unit", default="")
        if name:
            attribute_name = str(name)
            attribute_value = (
                f"{value} {unit}".strip() if unit and value is not None else value
            )
            attributes[attribute_name] = attribute_value
            normalized_name = normalize_attribute_name(attribute_name)
            if normalized_name in _DIGIKEY_VOLTAGE_RANGE_NAMES:
                endpoints = _range_endpoints(value, unit)
                if endpoints:
                    attributes.setdefault("Supply Voltage - Min", endpoints[0])
                    attributes.setdefault("Supply Voltage - Max", endpoints[1])
            elif normalized_name in _DIGIKEY_VOLTAGE_MIN_NAMES:
                attributes.setdefault("Supply Voltage - Min", attribute_value)
            elif normalized_name in _DIGIKEY_VOLTAGE_MAX_NAMES:
                attributes.setdefault("Supply Voltage - Max", attribute_value)
            if normalized_name in _DIGIKEY_ROHS_NAMES:
                attributes.setdefault("RoHS Compliant", attribute_value)
    classifications = product.get("Classifications") or {}
    if isinstance(classifications, dict):
        for name, value in classifications.items():
            if value is None:
                continue
            classification_name = str(name)
            attributes.setdefault(classification_name, value)
            if normalize_attribute_name(classification_name) in _DIGIKEY_ROHS_NAMES:
                attributes.setdefault("RoHS Compliant", value)
    for name in (
        "ROHSStatus",
        "RoHSStatus",
        "ROHSCompliant",
        "RoHSCompliant",
        "ROHSCompliance",
        "RoHSCompliance",
        "ROHS3Status",
        "ROHS3Compliant",
        "ROHS3Compliance",
    ):
        value = product.get(name)
        if value is not None:
            attributes.setdefault("RoHS Compliant", value)
    for name in (
        "ProductStatus",
        "ManufacturerLeadWeeks",
        "NormallyStocking",
        "Description",
        "Category",
    ):
        value = product.get(name)
        if value is not None:
            if isinstance(value, dict):
                value = _first(value, "Name", "Value", "ProductDescription", default=value)
            attributes.setdefault(name, value)
    return attributes


def _digikey_variations(product: dict[str, Any]) -> list[dict[str, Any]]:
    values = product.get("ProductVariations") or product.get("Variations") or []
    return [value for value in values if isinstance(value, dict)]


def normalize_digikey_offers(
    product: dict[str, Any],
    requested_quantity: int,
) -> list[DistributorOffer]:
    manufacturer = _digikey_manufacturer(product)
    mpn = str(
        _first(
            product,
            "ManufacturerProductNumber",
            "ManufacturerPartNumber",
            default="",
        )
    )
    attributes = _digikey_attributes(product)
    lifecycle_value = product.get("ProductStatus") or product.get("Status")
    if isinstance(lifecycle_value, dict):
        lifecycle_value = _first(lifecycle_value, "Status", "Name", default="")
    lead_weeks = parse_int(product.get("ManufacturerLeadWeeks"))
    lead_days = float(lead_weeks * 7) if lead_weeks is not None else None
    compliance = product.get("Classifications") or {}
    offers: list[DistributorOffer] = []
    variations = _digikey_variations(product)
    if not variations:
        variations = [product]
    for variation in variations:
        sku = str(
            _first(
                variation,
                "DigiKeyProductNumber",
                "DigiKeyPartNumber",
                "ProductNumber",
                default=_first(
                    product,
                    "DigiKeyProductNumber",
                    "DigiKeyPartNumber",
                    "ProductNumber",
                    default="",
                ),
            )
        )
        if not sku:
            continue
        minimum = parse_int(variation.get("MinimumOrderQuantity"))
        if minimum is None:
            breaks = [
                parse_int(item.get("BreakQuantity"))
                for item in variation.get("StandardPricing") or []
                if isinstance(item, dict)
            ]
            minimum = min(
                (value for value in breaks if value is not None and value > 0),
                default=None,
            )
        multiple = parse_int(
            _first(
                variation,
                "OrderQuantityMultiple",
                "QuantityMultiple",
                default=1,
            )
        )
        purchasable = effective_purchase_quantity(
            requested_quantity, minimum, multiple
        )
        price, currency = select_price_break(
            variation.get("StandardPricing") or [], purchasable
        )
        if price is None:
            direct_price = parse_decimal(
                _first(variation, "UnitPrice", "Price", default=None)
            )
            price = direct_price
        if price is not None and not currency:
            currency = settings.currency
        total = price * Decimal(purchasable) if price is not None else None
        stock = parse_int(
            _first(
                variation,
                "QuantityAvailableforPackageType",
                "QuantityAvailable",
                default=product.get("QuantityAvailable"),
            )
        )
        tariff = _first(
            variation,
            "TariffInformation",
            "TariffActive",
            default=None,
        )
        packaging = variation.get("PackageType") or variation.get("Packaging")
        if isinstance(packaging, dict):
            packaging = _first(packaging, "Name", "Value", default="")
        offers.append(
            DistributorOffer(
                distributor="digikey",
                identity=component_identity(
                    manufacturer,
                    mpn,
                    source_identifiers={"digikey_part_number": sku},
                ),
                distributor_part_number=sku,
                requested_quantity=requested_quantity,
                purchasable_quantity=purchasable,
                purchasable=price is not None,
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
                availability_status=(
                    "pricing_unavailable"
                    if price is None
                    else (
                        "stock_unknown"
                        if stock is None
                        else "available" if stock > 0 else "out_of_stock"
                    )
                ),
                lead_time=(
                    f"{lead_weeks} weeks" if lead_weeks is not None else None
                ),
                lead_time_days=lead_days,
                lifecycle=str(lifecycle_value or "") or None,
                compliance=compliance if isinstance(compliance, dict) else {},
                packaging=str(packaging or "") or None,
                product_url=str(
                    _first(
                        variation,
                        "ProductUrl",
                        default=product.get("ProductUrl") or "",
                    )
                )
                or None,
                datasheet_url=str(product.get("DatasheetUrl") or "") or None,
                attributes=attributes,
                duty_assumption=(
                    f"digikey_tariff_reported:{tariff}"
                    if tariff is not None
                    else "digikey_tariff_unknown"
                ),
                observed_at=datetime.now(timezone.utc).isoformat(),
                raw={"product": product, "variation": variation},
            )
        )
    return offers


class DigiKeyAdapter:
    name = "digikey"

    def capabilities(self) -> dict[str, bool]:
        configured = provider_configured(
            Provider.DIGIKEY,
            CredentialPurpose.OAUTH_CLIENT,
        )
        return {
            "catalog": configured,
            "account": configured,
            "order_submission": False,
        }

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "status": "configured" if self.capabilities()["catalog"] else "disabled",
            "capabilities": self.capabilities(),
        }

    def search(
        self,
        request: MouserSearchRequest,
        *,
        principal: str,
        authorization: str | None = None,
    ) -> dict[str, Any]:
        del principal
        if not authorization:
            raise ValueError("DigiKey authorization is required")
        response = search_products(
            ProductSearchRequest(
                keywords=request.query,
                limit=request.records,
                offset=request.starting_record,
                search_options=(
                    ["InStock"] if request.in_stock else []
                ),
            ),
            authorization,
        )
        return response.public()

    def exact_offers(
        self,
        manufacturer: str,
        manufacturer_part_number: str,
        quantity: int,
        *,
        principal: str,
        authorization: str | None = None,
    ) -> list[DistributorOffer]:
        del principal
        if not authorization:
            raise ValueError("DigiKey authorization is required")
        response = search_products(
            ProductSearchRequest(
                keywords=manufacturer_part_number,
                limit=50,
                search_options=[],
            ),
            authorization,
        )
        products = (
            response.data.get("Products") or []
            if isinstance(response.data, dict)
            else []
        )
        expected_manufacturer = normalize_manufacturer(manufacturer)
        expected_mpn = normalize_mpn(manufacturer_part_number)
        offers: list[DistributorOffer] = []
        for product in products:
            if not isinstance(product, dict):
                continue
            actual_manufacturer = normalize_manufacturer(
                _digikey_manufacturer(product)
            )
            actual_mpn = normalize_mpn(
                _first(
                    product,
                    "ManufacturerProductNumber",
                    "ManufacturerPartNumber",
                    default="",
                )
            )
            if (
                actual_manufacturer == expected_manufacturer
                and actual_mpn == expected_mpn
            ):
                offers.extend(normalize_digikey_offers(product, quantity))
        enriched: list[DistributorOffer] = []
        for offer in offers:
            if not offer.distributor_part_number:
                enriched.append(offer)
                continue
            pricing = get_pricing_by_quantity(
                offer.distributor_part_number,
                quantity,
                authorization,
            )
            options = flatten_pricing_options(pricing.data, quantity)
            matching = [
                option
                for option in options
                if normalize_mpn(option.get("digi_key_part_number"))
                == normalize_mpn(offer.distributor_part_number)
                and parse_decimal(option.get("total_price")) is not None
                and parse_decimal(option.get("total_price")) > 0
            ]
            if not matching:
                enriched.append(offer)
                continue
            selected = min(
                matching,
                key=lambda option: Decimal(str(option["total_price"])),
            )
            purchasable = parse_int(selected.get("quantity"))
            stock = selected.get("quantity_available")
            stock_value = parse_int(stock) if stock is not None else None
            enriched.append(
                offer.model_copy(
                    update={
                        "purchasable_quantity": purchasable,
                        "purchasable": True,
                        "minimum_order_quantity": parse_int(
                            selected.get("effective_minimum_order_quantity")
                        ),
                        "unit_price": money_string(
                            parse_decimal(selected.get("unit_price"))
                        ),
                        "merchandise_total": money_string(
                            parse_decimal(selected.get("total_price"))
                        ),
                        "currency": settings.currency.upper(),
                        "pricing_quantity_available": stock_value,
                        "pricing_requested_quantity_in_stock": (
                            stock_value >= quantity
                            if stock_value is not None
                            else None
                        ),
                        "packaging": str(
                            selected.get("package_type")
                            or offer.packaging
                            or ""
                        )
                        or None,
                        "duty_assumption": (
                            f"digikey_tariff_reported:{selected.get('tariff')}"
                        ),
                    }
                )
            )
        return enriched


digikey_adapter = DigiKeyAdapter()


def _provider_error(exc: Exception, provider: str) -> dict[str, Any]:
    if isinstance(exc, DigiKeyHTTPError):
        return error_envelope(
            exc.status_code,
            exc.detail,
            exc.meta,
            provider="digikey",
        )
    if isinstance(exc, MouserHTTPError):
        return error_envelope(
            exc.status_code,
            exc.detail,
            exc.meta,
            provider="mouser",
        )
    if isinstance(exc, CredentialUnavailableError):
        provider_name = exc.provider.value
        return error_envelope(
            503,
            {
                "message": str(exc),
                "error_type": exc.error_type,
                "provider": provider_name,
            },
            provider=provider_name,
            retryable=False,
        )
    return error_envelope(
        500,
        {
            "message": str(exc),
            "error_type": exc.__class__.__name__,
            "provider": provider,
        },
        provider=provider,
        retryable=False,
    )


def _run_provider_exact(
    provider: str,
    request: ComponentComparisonRequest,
    *,
    principal: str,
    authorization: str,
) -> SourceResult:
    adapter = digikey_adapter if provider == "digikey" else mouser_adapter
    results: list[DistributorOffer] = []
    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=min(settings.workflow_concurrency, len(request.items))
    ) as executor:
        tasks = {
            executor.submit(
                adapter.exact_offers,
                item.manufacturer,
                item.manufacturer_part_number,
                item.quantity,
                principal=principal,
                authorization=authorization,
            ): item
            for item in request.items
        }
        for future in as_completed(tasks):
            item = tasks[future]
            try:
                results.extend(future.result())
            except Exception as exc:  # provider boundary intentionally catches all
                errors.append(
                    {
                        "item": item.model_dump(mode="json"),
                        "error": _provider_error(exc, provider),
                    }
                )
    return SourceResult(
        provider=provider,
        status=(
            "partial"
            if errors and results
            else "failed" if errors else "success"
        ),
        results=results,
        warnings=errors,
        error=(
            {
                "message": "One or more provider calls failed",
                "failures": errors,
            }
            if errors
            else None
        ),
    )


def _priced_usd(offer: DistributorOffer) -> bool:
    return (
        offer.purchasable
        and bool(offer.distributor_part_number)
        and offer.purchasable_quantity is not None
        and offer.currency == "USD"
        and offer.merchandise_total is not None
        and parse_decimal(offer.merchandise_total) is not None
    )


def compare_component_offers(
    request: ComponentComparisonRequest,
    *,
    principal: str,
    authorization: str,
) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            provider: executor.submit(
                _run_provider_exact,
                provider,
                request,
                principal=principal,
                authorization=authorization,
            )
            for provider in ("digikey", "mouser")
        }
        sources = {provider: future.result() for provider, future in futures.items()}

    failed = [source for source in sources.values() if source.status == "failed"]
    source_incomplete = any(
        source.status != "success" for source in sources.values()
    )
    comparisons: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    rows: list[
        tuple[
            Any,
            dict[str, list[DistributorOffer]],
            dict[str, DistributorOffer | None],
            list[str],
            list[str],
        ]
    ] = []
    for item in request.items:
        key = component_identity(
            item.manufacturer, item.manufacturer_part_number
        ).canonical_key
        by_provider = {
            provider: [
                offer
                for offer in source.results
                if offer.identity.canonical_key == key
                and offer.requested_quantity == item.quantity
            ]
            for provider, source in sources.items()
        }
        missing = [
            provider for provider, offers in by_provider.items() if not offers
        ]
        eligible = {
            provider: min(
                (offer for offer in offers if _priced_usd(offer)),
                key=lambda offer: Decimal(str(offer.merchandise_total)),
                default=None,
            )
            for provider, offers in by_provider.items()
        }
        unavailable = [
            provider
            for provider, offers in by_provider.items()
            if offers and eligible[provider] is None
        ]
        if missing or unavailable:
            unmatched.append(
                {
                    "requested": item.model_dump(mode="json"),
                    "missing_from": missing,
                    "unavailable_from": unavailable,
                    "offers": {
                        provider: [
                            offer.model_dump(mode="json") for offer in offers
                        ]
                        for provider, offers in by_provider.items()
                    },
                }
            )
        rows.append((item, by_provider, eligible, missing, unavailable))

    coverage_incomplete = any(
        missing or unavailable
        for _item, _offers, _eligible, missing, unavailable in rows
    )
    status = (
        "failed"
        if len(failed) == 2
        else "partial"
        if source_incomplete or coverage_incomplete
        else "success"
    )
    for item, by_provider, eligible, missing, unavailable in rows:
        complete = (
            status == "success"
            and all(eligible.values())
            and not missing
            and not unavailable
        )
        winner: str | None = None
        if complete:
            totals = {
                provider: Decimal(str(offer.merchandise_total))
                for provider, offer in eligible.items()
                if offer is not None
            }
            lowest = min(totals.values())
            winners = [
                provider for provider, total in totals.items() if total == lowest
            ]
            winner = winners[0] if len(winners) == 1 else "tie"
        comparisons.append(
            {
                "requested": item.model_dump(mode="json"),
                "coverage_complete": bool(complete),
                "best_offer": winner,
                "offers": {
                    provider: [
                        offer.model_dump(mode="json") for offer in offers
                    ]
                    for provider, offers in by_provider.items()
                },
                "ranking_note": (
                    "Lowest USD merchandise total at the purchasable quantity"
                    if complete
                    else "No winner: provider coverage, strict identity, price, or currency is incomplete"
                ),
            }
        )
    return ComparisonResult(
        status=status,
        sources={
            provider: source.model_dump(mode="json")
            for provider, source in sources.items()
        },
        comparisons=comparisons,
        unmatched=unmatched,
        ambiguities=[],
        shipping={"cost": "unavailable", "delivery_eta": "unavailable"},
    ).model_dump(mode="json")


def _search_digikey_candidates(
    term: str,
    limit: int,
    authorization: str,
) -> list[DistributorOffer]:
    response = search_products(
        ProductSearchRequest(
            keywords=term,
            limit=limit,
            search_options=[],
        ),
        authorization,
    )
    products = response.data.get("Products") or []
    return [
        offer
        for product in products
        if isinstance(product, dict)
        for offer in normalize_digikey_offers(product, 1)
    ]


def _search_mouser_candidates(
    term: str,
    limit: int,
    quantity: int,
    principal: str,
) -> list[DistributorOffer]:
    response = mouser_adapter.search(
        MouserSearchRequest(
            query=term,
            mode=MouserSearchMode.keyword,
            records=limit,
        ),
        principal=principal,
    )
    parts = (response.get("SearchResults") or {}).get("Parts") or []
    return [
        normalize_mouser_offer(part, quantity)
        for part in parts
        if isinstance(part, dict)
    ]


def _offer_with_quantity(
    offer: DistributorOffer,
    quantity: int,
) -> DistributorOffer:
    if offer.distributor == "mouser":
        return normalize_mouser_offer(offer.raw, quantity)
    product = offer.raw.get("product") or {}
    variation_sku = offer.distributor_part_number
    candidates = [
        value
        for value in normalize_digikey_offers(product, quantity)
        if value.distributor_part_number == variation_sku
    ]
    return candidates[0] if candidates else offer.model_copy(
        update={"requested_quantity": quantity}
    )


def _search_recommendation_sources(
    request: ComponentRecommendationRequest,
    *,
    principal: str,
    authorization: str,
) -> dict[str, SourceResult]:
    provider_results: dict[str, list[DistributorOffer]] = {
        "digikey": [],
        "mouser": [],
    }
    provider_errors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    jobs: list[tuple[str, str, Callable[[], list[DistributorOffer]]]] = []
    for term in request.search_terms:
        jobs.append(
            (
                "digikey",
                term,
                lambda term=term: _search_digikey_candidates(
                    term, request.candidates_per_source, authorization
                ),
            )
        )
        jobs.append(
            (
                "mouser",
                term,
                lambda term=term: _search_mouser_candidates(
                    term,
                    request.candidates_per_source,
                    request.quantity,
                    principal,
                ),
            )
        )
    with ThreadPoolExecutor(
        max_workers=min(settings.workflow_concurrency, len(jobs))
    ) as executor:
        futures = {
            executor.submit(job): (provider, term)
            for provider, term, job in jobs
        }
        for future in as_completed(futures):
            provider, term = futures[future]
            try:
                provider_results[provider].extend(future.result())
            except Exception as exc:
                provider_errors[provider].append(
                    {"search_term": term, "error": _provider_error(exc, provider)}
                )

    sources: dict[str, SourceResult] = {}
    for provider in ("digikey", "mouser"):
        unique: dict[tuple[str, str], DistributorOffer] = {}
        for offer in provider_results[provider]:
            normalized_offer = _offer_with_quantity(offer, request.quantity)
            unique[
                (
                    normalized_offer.identity.canonical_key,
                    normalized_offer.distributor_part_number or "",
                )
            ] = normalized_offer
        ordered = sorted(
            unique.values(),
            key=lambda offer: (
                offer.identity.canonical_key,
                offer.distributor_part_number or "",
            ),
        )
        cap_warning: list[dict[str, Any]] = []
        if len(ordered) > 40:
            cap_warning.append(
                {
                    "code": "candidate_cap",
                    "total_unique_offers": len(ordered),
                    "returned_offers": 40,
                }
            )
            ordered = ordered[:40]
        errors = provider_errors[provider]
        sources[provider] = SourceResult(
            provider=provider,
            status=(
                "partial"
                if errors and ordered
                else "failed" if errors else "success"
            ),
            results=ordered,
            warnings=[*errors, *cap_warning],
            error=(
                {
                    "message": "One or more discovery searches failed",
                    "failures": errors,
                }
                if errors
                else None
            ),
        )
    return sources


def _candidate_evidence(
    offers: list[DistributorOffer],
    request: ComponentRecommendationRequest,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    def aggregate(requirement: Any) -> dict[str, Any]:
        source_evidence = []
        evaluated = []
        for offer in offers:
            evidence = evaluate_requirement(requirement, offer.attributes)
            evaluated.append(evidence)
            source_evidence.append(
                {
                    "provider": offer.distributor,
                    "distributor_part_number": offer.distributor_part_number,
                    "evidence": evidence.model_dump(mode="json"),
                }
            )
        decisive = {
            evidence.status
            for evidence in evaluated
            if evidence.status != "unknown"
        }
        if len(decisive) > 1:
            selected = next(
                evidence for evidence in evaluated if evidence.status != "unknown"
            )
            result = selected.model_dump(mode="json")
            result.update(
                {
                    "status": "unknown",
                    "reason": "Distributor specification evidence conflicts",
                    "source_evidence": source_evidence,
                }
            )
            return result
        selected = next(
            (
                evidence
                for evidence in evaluated
                if evidence.status != "unknown"
            ),
            evaluated[0],
        )
        result = selected.model_dump(mode="json")
        result["source_evidence"] = source_evidence
        return result

    hard = [aggregate(requirement) for requirement in request.hard_requirements]
    preferences = [aggregate(requirement) for requirement in request.preferences]
    if any(item["status"] == "does_not_meet" for item in hard):
        classification = "rejected"
    elif any(item["status"] == "unknown" for item in hard):
        classification = "unverified"
    else:
        classification = "qualified"
    return hard, preferences, classification


def _candidate_metrics(candidate: dict[str, Any]) -> tuple[Decimal, int, float, int]:
    offers = [
        DistributorOffer.model_validate(offer) for offer in candidate["offers"]
    ]
    prices = [
        Decimal(str(offer.merchandise_total))
        for offer in offers
        if _priced_usd(offer)
    ]
    price = min(prices, default=Decimal("Infinity"))
    stock = 1 if any(offer.requested_quantity_in_stock for offer in offers) else 0
    leads = [
        offer.lead_time_days
        for offer in offers
        if offer.lead_time_days is not None
    ]
    lead = min(leads, default=float("inf"))
    preference_meets = sum(
        1
        for evidence in candidate["preference_evidence"]
        if evidence["status"] == "meets"
    )
    return price, stock, lead, preference_meets


def _dominates(left: tuple[Decimal, int, float, int], right: tuple[Decimal, int, float, int]) -> bool:
    no_worse = (
        left[0] <= right[0]
        and left[1] >= right[1]
        and left[2] <= right[2]
        and left[3] >= right[3]
    )
    strictly_better = (
        left[0] < right[0]
        or left[1] > right[1]
        or left[2] < right[2]
        or left[3] > right[3]
    )
    return no_worse and strictly_better


def recommend_components(
    request: ComponentRecommendationRequest,
    *,
    principal: str,
    authorization: str,
) -> dict[str, Any]:
    sources = _search_recommendation_sources(
        request, principal=principal, authorization=authorization
    )
    failed = [source for source in sources.values() if source.status == "failed"]
    incomplete = any(
        source.status != "success" or not source.results
        for source in sources.values()
    )
    status = (
        "failed"
        if len(failed) == 2
        else "partial" if incomplete else "success"
    )
    grouped: dict[str, list[DistributorOffer]] = defaultdict(list)
    for source in sources.values():
        for offer in source.results:
            grouped[offer.identity.canonical_key].append(offer)

    candidates: list[dict[str, Any]] = []
    for key, offers in grouped.items():
        hard, preference, classification = _candidate_evidence(offers, request)
        candidates.append(
            {
                "identity": offers[0].identity.model_dump(mode="json"),
                "classification": classification,
                "hard_requirement_evidence": hard,
                "preference_evidence": preference,
                "offers": [
                    offer.model_dump(mode="json")
                    for offer in sorted(
                        offers,
                        key=lambda value: (
                            value.distributor,
                            value.distributor_part_number or "",
                        ),
                    )
                ],
                "shipping": {
                    "cost": "unavailable",
                    "delivery_eta": "unavailable",
                },
            }
        )
    candidates.sort(
        key=lambda candidate: (
            candidate["classification"],
            candidate["identity"]["canonical_key"],
        )
    )
    qualified = [
        candidate for candidate in candidates if candidate["classification"] == "qualified"
    ]
    rankable = [
        candidate
        for candidate in qualified
        if any(
            _priced_usd(DistributorOffer.model_validate(offer))
            for offer in candidate["offers"]
        )
    ]
    pareto: list[dict[str, Any]] = []
    if status == "success":
        metrics = [_candidate_metrics(candidate) for candidate in rankable]
        pareto = [
            candidate
            for index, candidate in enumerate(rankable)
            if not any(
                _dominates(other, metrics[index])
                for other_index, other in enumerate(metrics)
                if other_index != index
            )
        ]
        pareto.sort(
            key=lambda candidate: (
                _candidate_metrics(candidate)[0],
                -_candidate_metrics(candidate)[1],
                _candidate_metrics(candidate)[2],
                -_candidate_metrics(candidate)[3],
                candidate["identity"]["canonical_key"],
            )
        )
        pareto = pareto[:5]

    unknown_requirements = sorted(
        {
            evidence["requirement"]["name"]
            for candidate in candidates
            if candidate["classification"] == "unverified"
            for evidence in candidate["hard_requirement_evidence"]
            if evidence["status"] == "unknown"
        }
    )
    return {
        "status": status,
        "project_summary": request.project_summary,
        "sources": {
            provider: source.model_dump(mode="json")
            for provider, source in sources.items()
        },
        "pareto_shortlist": pareto,
        "candidates": candidates,
        "qualified_count": len(qualified),
        "qualified_without_usd_price_count": len(qualified) - len(rankable),
        "unverified_count": sum(
            candidate["classification"] == "unverified"
            for candidate in candidates
        ),
        "rejected_count": sum(
            candidate["classification"] == "rejected"
            for candidate in candidates
        ),
        "missing_evidence": unknown_requirements,
        "ranking_note": (
            "No shortlist is declared while a distributor source is incomplete"
            if status != "success"
            else (
                "Pareto shortlist across USD merchandise total, requested-quantity "
                "stock coverage, manufacturer lead time, and explicit preferences"
            )
        ),
        "shipping": {"cost": "unavailable", "delivery_eta": "unavailable"},
    }
