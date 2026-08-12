from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from credentials import CredentialPurpose


class MouserSearchMode(str, Enum):
    keyword = "keyword"
    part_number = "part_number"


class MouserSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=400)
    mode: MouserSearchMode = MouserSearchMode.keyword
    manufacturer: str | None = Field(default=None, min_length=1, max_length=150)
    records: int = Field(10, ge=1, le=50)
    starting_record: int = Field(0, ge=0)
    in_stock: bool = False
    rohs: bool = False

    @model_validator(mode="after")
    def validate_part_numbers(self) -> "MouserSearchRequest":
        if self.mode == MouserSearchMode.part_number:
            values = self.query.split("|")
            if len(values) > 10:
                raise ValueError("Mouser part-number search accepts at most 10 values")
            if any(not 3 <= len(value.strip()) <= 40 for value in values):
                raise ValueError(
                    "Each Mouser part-number search value must be 3 to 40 characters"
                )
        return self


class ComponentRequest(BaseModel):
    manufacturer: str = Field(..., min_length=1, max_length=150)
    manufacturer_part_number: str = Field(..., min_length=1, max_length=100)
    quantity: int = Field(..., ge=1, le=10_000_000)


class ComponentComparisonRequest(BaseModel):
    items: list[ComponentRequest] = Field(..., min_length=1, max_length=10)

    @model_validator(mode="after")
    def reject_duplicate_rows(self) -> "ComponentComparisonRequest":
        keys = [
            (
                item.manufacturer.strip().casefold(),
                item.manufacturer_part_number.strip().casefold(),
                item.quantity,
            )
            for item in self.items
        ]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "Comparison rows must be unique by manufacturer, MPN, and quantity"
            )
        return self


class RequirementOperator(str, Enum):
    eq = "eq"
    ne = "ne"
    gt = "gt"
    gte = "gte"
    lt = "lt"
    lte = "lte"
    between = "between"
    contains = "contains"
    boolean = "boolean"


class ComponentRequirement(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    operator: RequirementOperator
    value: str | float | int | bool | None = None
    maximum: float | int | None = None
    unit: str | None = Field(default=None, max_length=30)
    rationale: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def validate_requirement(self) -> "ComponentRequirement":
        if self.operator == RequirementOperator.between:
            if not isinstance(self.value, (int, float)) or self.maximum is None:
                raise ValueError("between requirements need numeric value and maximum")
            if float(self.value) > float(self.maximum):
                raise ValueError("between requirement value cannot exceed maximum")
        elif self.value is None:
            raise ValueError(f"{self.operator.value} requirements need a value")
        return self


class ComponentRecommendationRequest(BaseModel):
    project_summary: str = Field(..., min_length=10, max_length=2000)
    search_terms: list[str] = Field(..., min_length=1, max_length=4)
    quantity: int = Field(..., ge=1, le=10_000_000)
    hard_requirements: list[ComponentRequirement] = Field(
        ..., min_length=1, max_length=30
    )
    preferences: list[ComponentRequirement] = Field(
        default_factory=list, max_length=20
    )
    candidates_per_source: int = Field(10, ge=1, le=20)

    @model_validator(mode="after")
    def validate_unique_inputs(self) -> "ComponentRecommendationRequest":
        terms = [term.strip().casefold() for term in self.search_terms]
        if len(terms) != len(set(terms)):
            raise ValueError("search_terms must be unique")
        hard_names = [
            requirement.name.strip().casefold()
            for requirement in self.hard_requirements
        ]
        if len(hard_names) != len(set(hard_names)):
            raise ValueError("hard requirement names must be unique")
        return self


class ComponentIdentity(BaseModel):
    manufacturer: str
    manufacturer_part_number: str
    canonical_key: str
    source_identifiers: dict[str, str] = Field(default_factory=dict)


class DistributorOffer(BaseModel):
    distributor: Literal["digikey", "mouser"]
    identity: ComponentIdentity
    distributor_part_number: str | None
    requested_quantity: int
    purchasable_quantity: int | None = None
    purchasable: bool = False
    minimum_order_quantity: int | None = None
    order_multiple: int | None = None
    unit_price: str | None = None
    merchandise_total: str | None = None
    currency: str | None = None
    # `quantity_available` remains as a compatibility view of variation/package
    # stock. New consumers should use the two explicit fields below.
    quantity_available: int | None = None
    pricing_quantity_available: int | None = None
    variation_quantity_available: int | None = None
    requested_quantity_in_stock: bool | None = None
    pricing_requested_quantity_in_stock: bool | None = None
    variation_requested_quantity_in_stock: bool | None = None
    availability_status: Literal[
        "available",
        "out_of_stock",
        "stock_unknown",
        "pricing_unavailable",
        "regional_unavailable",
    ] = "stock_unknown"
    lead_time: str | None = None
    lead_time_days: float | None = None
    lifecycle: str | None = None
    compliance: dict[str, Any] = Field(default_factory=dict)
    packaging: str | None = None
    product_url: str | None = None
    datasheet_url: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    duty_assumption: str
    observed_at: str
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def derive_purchasable_for_legacy_callers(self) -> "DistributorOffer":
        if "purchasable" not in self.model_fields_set:
            self.purchasable = bool(
                self.distributor_part_number
                and self.purchasable_quantity is not None
                and self.unit_price is not None
            )
        return self


class RequirementEvidence(BaseModel):
    requirement: ComponentRequirement
    matched_attribute: str | None = None
    source_value: Any = None
    normalized_value: Any = None
    normalized_unit: str | None = None
    status: Literal["meets", "does_not_meet", "unknown"]
    reason: str


class SourceResult(BaseModel):
    provider: Literal["digikey", "mouser"]
    status: Literal["success", "partial", "failed"]
    results: list[DistributorOffer] = Field(default_factory=list)
    warnings: list[dict[str, Any] | str] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class ComparisonResult(BaseModel):
    status: Literal["success", "partial", "failed"]
    sources: dict[str, SourceResult]
    comparisons: list[dict[str, Any]] = Field(default_factory=list)
    unmatched: list[dict[str, Any]] = Field(default_factory=list)
    ambiguities: list[dict[str, Any]] = Field(default_factory=list)
    shipping: dict[str, str] = Field(
        default_factory=lambda: {
            "cost": "unavailable",
            "delivery_eta": "unavailable",
        }
    )


class MouserOrderHistoryMode(str, Enum):
    date_filter = "date_filter"
    date_range = "date_range"


class MouserOrderHistoryRequest(BaseModel):
    mode: MouserOrderHistoryMode
    date_filter: Literal[
        "None",
        "All",
        "Today",
        "Yesterday",
        "ThisWeek",
        "LastWeek",
        "ThisMonth",
        "LastMonth",
        "ThisQuarter",
        "LastQuarter",
        "ThisYear",
        "LastYear",
        "YearToDate",
    ] | None = None
    start_date: str | None = Field(default=None, max_length=20)
    end_date: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def validate_mode_fields(self) -> "MouserOrderHistoryRequest":
        if self.mode == MouserOrderHistoryMode.date_filter:
            if not self.date_filter or self.start_date or self.end_date:
                raise ValueError("date_filter mode requires only date_filter")
        elif not self.start_date or not self.end_date or self.date_filter:
            raise ValueError("date_range mode requires only start_date and end_date")
        else:
            try:
                start = datetime.strptime(self.start_date, "%m/%d/%Y").date()
                end = datetime.strptime(self.end_date, "%m/%d/%Y").date()
            except ValueError as exc:
                raise ValueError(
                    "start_date and end_date must use MM/DD/YYYY"
                ) from exc
            if start > end:
                raise ValueError("start_date cannot be after end_date")
        return self


class MouserOrderLookupRequest(BaseModel):
    sales_order_number: str | None = Field(default=None, min_length=1, max_length=50)
    web_order_number: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def exactly_one_number(self) -> "MouserOrderLookupRequest":
        if bool(self.sales_order_number) == bool(self.web_order_number):
            raise ValueError(
                "Provide exactly one of sales_order_number or web_order_number"
            )
        return self


class MouserPackagingChoice(str, Enum):
    none = "None"
    cut_tape = "Cut_Tape"
    mouser_reel = "MouseReel"
    full_reel = "FullReel"


class MouserCartItem(BaseModel):
    mouser_part_number: str = Field(..., min_length=1, max_length=80)
    quantity: int = Field(..., ge=1)
    customer_part_number: str = Field(default="", max_length=21, pattern=r"^[^*]*$")
    packaging_choice: MouserPackagingChoice = MouserPackagingChoice.none


class MouserScheduledRelease(BaseModel):
    date: str = Field(..., min_length=1, max_length=40)
    quantity: int = Field(..., ge=1)


class MouserScheduleItem(BaseModel):
    mouser_part_number: str = Field(..., min_length=1, max_length=80)
    scheduled_releases: list[MouserScheduledRelease] = Field(
        ..., min_length=1, max_length=100
    )


class MouserCartOperation(str, Enum):
    add_items = "add_items"
    update_items = "update_items"
    remove_item = "remove_item"
    replace_cart = "replace_cart"
    create_from_order = "create_from_order"
    add_schedule = "add_schedule"
    update_schedule = "update_schedule"
    delete_all_schedules = "delete_all_schedules"


class MouserCartPreviewRequest(BaseModel):
    operation: MouserCartOperation
    cart_key: str | None = Field(default=None, max_length=80)
    items: list[MouserCartItem] = Field(default_factory=list, max_length=100)
    mouser_part_number: str | None = Field(default=None, max_length=80)
    order_number: int | None = Field(default=None, ge=1)
    schedule_items: list[MouserScheduleItem] = Field(
        default_factory=list, max_length=100
    )

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "MouserCartPreviewRequest":
        item_ops = {
            MouserCartOperation.add_items,
            MouserCartOperation.update_items,
            MouserCartOperation.replace_cart,
        }
        cart_required = {
            MouserCartOperation.update_items,
            MouserCartOperation.remove_item,
            MouserCartOperation.replace_cart,
            MouserCartOperation.add_schedule,
            MouserCartOperation.update_schedule,
            MouserCartOperation.delete_all_schedules,
        }
        if self.operation in cart_required and not self.cart_key:
            raise ValueError(f"{self.operation.value} requires cart_key")
        if self.operation in item_ops and not self.items:
            raise ValueError(f"{self.operation.value} requires items")
        item_numbers = [item.mouser_part_number for item in self.items]
        if len(item_numbers) != len(set(item_numbers)):
            raise ValueError("Cart item part numbers must be unique within a preview")
        if self.operation not in item_ops and self.items:
            raise ValueError(f"{self.operation.value} does not accept items")
        if self.operation == MouserCartOperation.remove_item:
            if not self.mouser_part_number:
                raise ValueError("remove_item requires mouser_part_number")
        elif self.mouser_part_number:
            raise ValueError(
                f"{self.operation.value} does not accept mouser_part_number"
            )
        if self.operation == MouserCartOperation.create_from_order:
            if self.order_number is None:
                raise ValueError("create_from_order requires order_number")
        elif self.order_number is not None:
            raise ValueError(f"{self.operation.value} does not accept order_number")
        schedule_ops = {
            MouserCartOperation.add_schedule,
            MouserCartOperation.update_schedule,
        }
        if self.operation in schedule_ops and not self.schedule_items:
            raise ValueError(f"{self.operation.value} requires schedule_items")
        schedule_numbers = [
            item.mouser_part_number for item in self.schedule_items
        ]
        if len(schedule_numbers) != len(set(schedule_numbers)):
            raise ValueError(
                "Schedule item part numbers must be unique within a preview"
            )
        if self.operation not in schedule_ops and self.schedule_items:
            raise ValueError(f"{self.operation.value} does not accept schedule_items")
        return self


class MouserCartExecuteRequest(BaseModel):
    confirmation_token: str = Field(..., min_length=32, max_length=500)
