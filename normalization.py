from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from distributor_models import (
    ComponentIdentity,
    ComponentRequirement,
    RequirementEvidence,
    RequirementOperator,
)


NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")
SPACE_PATTERN = re.compile(r"\s+")
ATTRIBUTE_PATTERN = re.compile(r"[^a-z0-9]+")

# Only verified aliases belong here. The intentionally empty table means a
# provider spelling must otherwise match after case/whitespace normalization.
VERIFIED_MANUFACTURER_ALIASES: dict[str, str] = {}


def normalized_text(value: Any) -> str:
    return SPACE_PATTERN.sub(" ", str(value or "").strip()).casefold()


def normalize_mpn(value: Any) -> str:
    # Preserve punctuation and suffixes; only case and surrounding whitespace
    # are normalized for strict identity matching.
    return str(value or "").strip().casefold()


def normalize_manufacturer(value: Any) -> str:
    normalized = normalized_text(value)
    return VERIFIED_MANUFACTURER_ALIASES.get(normalized, normalized)


def component_identity(
    manufacturer: Any,
    manufacturer_part_number: Any,
    *,
    source_identifiers: dict[str, str] | None = None,
) -> ComponentIdentity:
    manufacturer_text = str(manufacturer or "").strip()
    mpn_text = str(manufacturer_part_number or "").strip()
    return ComponentIdentity(
        manufacturer=manufacturer_text,
        manufacturer_part_number=mpn_text,
        canonical_key=(
            f"{normalize_manufacturer(manufacturer_text)}|{normalize_mpn(mpn_text)}"
        ),
        source_identifiers=source_identifiers or {},
    )


def parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    match = NUMBER_PATTERN.search(str(value or ""))
    if not match:
        return None
    try:
        return int(Decimal(match.group(0).replace(",", "")))
    except (InvalidOperation, ValueError):
        return None


def parse_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    match = NUMBER_PATTERN.search(str(value))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def money_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")


def effective_purchase_quantity(
    requested_quantity: int,
    minimum_order_quantity: int | None,
    order_multiple: int | None,
) -> int:
    minimum = max(1, minimum_order_quantity or 1)
    multiple = max(1, order_multiple or 1)
    base = max(requested_quantity, minimum)
    return int(math.ceil(base / multiple) * multiple)


def select_price_break(
    price_breaks: list[dict[str, Any]],
    quantity: int,
) -> tuple[Decimal | None, str | None]:
    candidates: list[tuple[int, Decimal, str | None]] = []
    for item in price_breaks:
        if not isinstance(item, dict):
            continue
        break_quantity = parse_int(
            item.get("Quantity")
            if "Quantity" in item
            else item.get("BreakQuantity")
        )
        price = parse_decimal(
            item.get("Price")
            if "Price" in item
            else item.get("UnitPrice")
        )
        currency = item.get("Currency") or item.get("CurrencyCode")
        if break_quantity is not None and break_quantity > 0 and price is not None:
            candidates.append((break_quantity, price, str(currency) if currency else None))
    applicable = [candidate for candidate in candidates if candidate[0] <= quantity]
    if not applicable:
        return None, None
    _, price, currency = max(applicable, key=lambda candidate: candidate[0])
    return price, currency


def normalize_attribute_name(value: Any) -> str:
    return ATTRIBUTE_PATTERN.sub("", normalized_text(value))


def boolean_value(value: Any) -> bool | None:
    """Normalize explicit boolean and compliance-style distributor values."""
    if isinstance(value, bool):
        return value
    text = normalized_text(value)
    if text in {"yes", "true", "1"}:
        return True
    if text in {"no", "false", "0"}:
        return False

    compact = normalize_attribute_name(text)
    if any(
        marker in compact
        for marker in ("noncompliant", "notcompliant", "nonrohs", "notrohs")
    ):
        return False
    if "compliant" in compact:
        return True
    return None


def attributes_by_name(attributes: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    return {
        normalize_attribute_name(name): (str(name), value)
        for name, value in attributes.items()
        if normalize_attribute_name(name)
    }


UNIT_ALIASES = {
    "µv": "uv",
    "μv": "uv",
    "µa": "ua",
    "μa": "ua",
    "µs": "us",
    "μs": "us",
    "ω": "ohm",
    "kohms": "kohm",
    "mohms": "megaohm",
    "mohm": "megaohm",
    "volts": "v",
    "volt": "v",
    "amps": "a",
    "amp": "a",
}

UNIT_TABLE: dict[str, tuple[str, Decimal]] = {
    "v": ("voltage", Decimal("1")),
    "mv": ("voltage", Decimal("0.001")),
    "uv": ("voltage", Decimal("0.000001")),
    "a": ("current", Decimal("1")),
    "ma": ("current", Decimal("0.001")),
    "ua": ("current", Decimal("0.000001")),
    "ohm": ("resistance", Decimal("1")),
    "kohm": ("resistance", Decimal("1000")),
    "megaohm": ("resistance", Decimal("1000000")),
    "milliohm": ("resistance", Decimal("0.001")),
    "f": ("capacitance", Decimal("1")),
    "uf": ("capacitance", Decimal("0.000001")),
    "nf": ("capacitance", Decimal("0.000000001")),
    "pf": ("capacitance", Decimal("0.000000000001")),
    "hz": ("frequency", Decimal("1")),
    "khz": ("frequency", Decimal("1000")),
    "mhz": ("frequency", Decimal("1000000")),
    "ghz": ("frequency", Decimal("1000000000")),
    "s": ("time", Decimal("1")),
    "ms": ("time", Decimal("0.001")),
    "us": ("time", Decimal("0.000001")),
    "ns": ("time", Decimal("0.000000001")),
    "w": ("power", Decimal("1")),
    "mw": ("power", Decimal("0.001")),
    "m": ("length", Decimal("1")),
    "cm": ("length", Decimal("0.01")),
    "mm": ("length", Decimal("0.001")),
    "db": ("decibel", Decimal("1")),
    "%": ("percent", Decimal("1")),
    "c": ("temperature_c", Decimal("1")),
    "°c": ("temperature_c", Decimal("1")),
}


def normalize_unit(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip().replace(" ", "")
    if raw in {"MΩ", "MOhm", "MOhms"}:
        return "megaohm"
    if raw in {"mΩ", "mOhm", "mOhms"}:
        return "milliohm"
    unit = normalized_text(raw)
    return UNIT_ALIASES.get(unit, unit)


def _numeric_source(value: Any) -> tuple[Decimal | None, str | None]:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value)), None
    text = str(value or "").strip()
    matches = NUMBER_PATTERN.findall(text)
    if len(matches) != 1:
        return None, None
    number = parse_decimal(matches[0])
    suffix = text[text.find(matches[0]) + len(matches[0]) :].strip()
    unit_match = re.match(r"^([%°µμA-Za-zΩ]+)", suffix)
    return number, normalize_unit(unit_match.group(1) if unit_match else None)


def _convert(
    value: Decimal,
    source_unit: str | None,
    target_unit: str | None,
) -> tuple[Decimal | None, str | None]:
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    if not target:
        return value, source
    if not source:
        return None, None
    source_entry = UNIT_TABLE.get(source)
    target_entry = UNIT_TABLE.get(target)
    if not source_entry or not target_entry or source_entry[0] != target_entry[0]:
        return None, None
    base = value * source_entry[1]
    return base / target_entry[1], target


def evaluate_requirement(
    requirement: ComponentRequirement,
    attributes: dict[str, Any],
) -> RequirementEvidence:
    indexed = attributes_by_name(attributes)
    names = [requirement.name, *requirement.aliases]
    matched: tuple[str, Any] | None = None
    for name in names:
        candidate = indexed.get(normalize_attribute_name(name))
        if candidate:
            matched = candidate
            break
    if matched is None:
        return RequirementEvidence(
            requirement=requirement,
            status="unknown",
            reason="No matching distributor attribute was returned",
        )

    attribute_name, source_value = matched
    operator = requirement.operator
    numeric_comparison = operator in {
        RequirementOperator.gt,
        RequirementOperator.gte,
        RequirementOperator.lt,
        RequirementOperator.lte,
        RequirementOperator.between,
    } or (
        operator in {RequirementOperator.eq, RequirementOperator.ne}
        and isinstance(requirement.value, (int, float))
        and not isinstance(requirement.value, bool)
    )
    if numeric_comparison:
        source_number, source_unit = _numeric_source(source_value)
        if source_number is None:
            return RequirementEvidence(
                requirement=requirement,
                matched_attribute=attribute_name,
                source_value=source_value,
                status="unknown",
                reason="Attribute is not an unambiguous scalar numeric value",
            )
        converted, normalized_unit_value = _convert(
            source_number, source_unit, requirement.unit
        )
        if converted is None:
            return RequirementEvidence(
                requirement=requirement,
                matched_attribute=attribute_name,
                source_value=source_value,
                status="unknown",
                reason="Attribute unit is missing, unsupported, or incompatible",
            )
        target = Decimal(str(requirement.value))
        if operator == RequirementOperator.eq:
            result = converted == target
        elif operator == RequirementOperator.ne:
            result = converted != target
        elif operator == RequirementOperator.gt:
            result = converted > target
        elif operator == RequirementOperator.gte:
            result = converted >= target
        elif operator == RequirementOperator.lt:
            result = converted < target
        elif operator == RequirementOperator.lte:
            result = converted <= target
        else:
            result = target <= converted <= Decimal(str(requirement.maximum))
        return RequirementEvidence(
            requirement=requirement,
            matched_attribute=attribute_name,
            source_value=source_value,
            normalized_value=str(converted.normalize()),
            normalized_unit=normalized_unit_value,
            status="meets" if result else "does_not_meet",
            reason="Numeric comparison completed",
        )

    source_normalized = normalized_text(source_value)
    target_normalized = normalized_text(requirement.value)
    normalized_value = source_normalized
    if operator == RequirementOperator.contains:
        result = target_normalized in source_normalized
    elif operator == RequirementOperator.ne:
        result = source_normalized != target_normalized
    elif operator == RequirementOperator.boolean or isinstance(
        requirement.value, bool
    ):
        source_boolean = boolean_value(source_value)
        target_boolean = boolean_value(requirement.value)
        if source_boolean is None or target_boolean is None:
            return RequirementEvidence(
                requirement=requirement,
                matched_attribute=attribute_name,
                source_value=source_value,
                normalized_value=source_normalized,
                status="unknown",
                reason="Attribute is not an explicit boolean or compliance value",
            )
        result = source_boolean == target_boolean
        normalized_value = str(source_boolean).lower()
    elif operator == RequirementOperator.eq:
        result = source_normalized == target_normalized
    else:
        return RequirementEvidence(
            requirement=requirement,
            matched_attribute=attribute_name,
            source_value=source_value,
            status="unknown",
            reason="Operator is not supported for this attribute",
        )
    return RequirementEvidence(
        requirement=requirement,
        matched_attribute=attribute_name,
        source_value=source_value,
        normalized_value=normalized_value,
        status="meets" if result else "does_not_meet",
        reason="Text or boolean comparison completed",
    )
