from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class MarketPlaceFilter(str, Enum):
    no_filter = "NoFilter"
    exclude = "ExcludeMarketPlace"
    only = "MarketPlaceOnly"


class TariffFilter(str, Enum):
    none = "None"
    exclude = "ExcludeTariff"
    only = "TariffOnly"


class SortOrder(str, Enum):
    ascending = "Ascending"
    descending = "Descending"


class ParametricFilter(BaseModel):
    parameter_id: int = Field(..., ge=1)
    value_ids: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "ValueId strings returned in FilterOptions.ParametricFilters from an earlier broad search."
        ),
    )


class ProductSearchRequest(BaseModel):
    keywords: str = Field(..., min_length=1, max_length=250)
    limit: int = Field(10, ge=1, le=50)
    offset: int = Field(0, ge=0)
    manufacturer_ids: list[str] = Field(default_factory=list)
    category_ids: list[str] = Field(default_factory=list)
    status_ids: list[str] = Field(default_factory=list)
    packaging_ids: list[str] = Field(default_factory=list)
    series_ids: list[str] = Field(default_factory=list)
    marketplace_filter: MarketPlaceFilter = MarketPlaceFilter.exclude
    tariff_filter: TariffFilter = TariffFilter.none
    minimum_quantity_available: int | None = Field(default=None, ge=0)
    search_options: list[str] = Field(
        default_factory=lambda: ["InStock", "RohsCompliant", "NormallyStocking"],
        description=(
            "Supported values include ChipOutpost, Has3DModel, HasCadModel, HasDatasheet, "
            "HasProductPhoto, InStock, NewProduct, NonRohsCompliant, NormallyStocking, "
            "and RohsCompliant."
        ),
    )
    parametric_category_id: str | None = Field(
        default=None,
        description="Category ID used with parametric filters.",
    )
    parametric_filters: list[ParametricFilter] = Field(default_factory=list)
    sort_field: str | None = Field(
        default=None,
        description=(
            "Optional DigiKey sort field, such as Price, QuantityAvailable, Manufacturer, "
            "ManufacturerProductNumber, MinimumQuantity, Packaging, ProductStatus, or Supplier."
        ),
    )
    sort_order: SortOrder = SortOrder.ascending
    includes: str | None = Field(
        default=None,
        description="Optional DigiKey includes expression used to reduce returned fields.",
    )

    @model_validator(mode="after")
    def validate_parametrics(self) -> "ProductSearchRequest":
        if self.parametric_filters and not self.parametric_category_id:
            raise ValueError("parametric_category_id is required when parametric_filters are supplied")
        return self


class BOMItem(BaseModel):
    product_number: str = Field(..., min_length=1)
    quantity: int = Field(..., ge=1)
    manufacturer_id: str | None = None
    customer_reference: str = Field(default="", max_length=80)
    reference_designator: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=1000)
    preferred_package: str | None = None


class BulkBOMRequest(BaseModel):
    items: list[BOMItem] = Field(..., min_length=1, max_length=100)
    account_id: str | None = None
    include_product_change_notifications: bool = True
    include_substitutions_for_risky_parts: bool = True
    include_alternate_packaging: bool = True
    exclude_marketplace: bool = True
    exclude_tariff: bool = False
    maximum_lead_weeks: int = Field(20, ge=0, le=104)


class PricingOptimizationRequest(BaseModel):
    items: list[BOMItem] = Field(..., min_length=1, max_length=100)
    account_id: str | None = None
    allow_marketplace: bool = False
    allow_tariff: bool = True
    allow_quantity_increase: bool = True
    include_digireel: bool = True


class CreateListRequest(BaseModel):
    list_name: str = Field(..., min_length=1, max_length=150)
    tags: list[str] = Field(default_factory=list)
    created_by: str = ""
    account_id: str | None = None


class RenameListRequest(BaseModel):
    new_name: str = Field(..., min_length=1, max_length=150)
    account_id: str | None = None


class ListPartInput(BaseModel):
    product_number: str = Field(..., min_length=1)
    quantity: int = Field(..., ge=1)
    package_type: str = "CutTape"
    sub_package_type: str = ""
    customer_reference: str = ""
    reference_designator: str = ""
    notes: str = ""
    target_price: float = Field(0, ge=0)


class AddPartsRequest(BaseModel):
    parts: list[ListPartInput] = Field(..., min_length=1, max_length=100)
    insertion_index: int = Field(0, ge=0)
    account_id: str | None = None


class UpdateListPartRequest(BaseModel):
    product_number: str | None = Field(default=None, min_length=1)
    quantity: int | None = Field(default=None, ge=1)
    package_type: str | None = None
    sub_package_type: str | None = None
    customer_reference: str | None = None
    reference_designator: str | None = None
    notes: str | None = None
    target_price: float | None = Field(default=None, ge=0)
    selected_quantity_index: int | None = Field(default=None, ge=0)
    attrition: float | None = Field(default=None, ge=0)
    alternate_parts: list[str] | None = None
    manufacturer_name: str | None = None
    created_by: str = ""
    account_id: str | None = None


class ListDiffRequest(BaseModel):
    proposed_items: list[ListPartInput] = Field(..., min_length=1, max_length=100)
    account_id: str | None = None
    remove_unlisted: bool = False
    consolidate_duplicates: bool = True


class ListSyncRequest(ListDiffRequest):
    confirm: Literal[True] = Field(
        ...,
        description="Must be true after the user explicitly approves the exact diff.",
    )
    stop_on_error: bool = True


class LifecycleAuditRequest(BaseModel):
    items: list[BOMItem] | None = Field(default=None, max_length=100)
    list_id: str | None = None
    account_id: str | None = None
    maximum_lead_weeks: int = Field(20, ge=0, le=104)
    include_substitutions: bool = True

    @model_validator(mode="after")
    def exactly_one_source(self) -> "LifecycleAuditRequest":
        if bool(self.items) == bool(self.list_id):
            raise ValueError("Provide exactly one of items or list_id")
        return self


class QuoteProductInput(BaseModel):
    product_number: str = Field(..., min_length=1)
    quantities: list[int] = Field(..., min_length=1, max_length=20)
    customer_reference: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def quantities_positive(self) -> "QuoteProductInput":
        if any(q < 1 for q in self.quantities):
            raise ValueError("Every quote quantity must be at least 1")
        return self


class CreateQuoteRequest(BaseModel):
    quote_name: str = Field(..., min_length=1, max_length=40)
    account_id: str = Field(..., min_length=1)


class AddQuoteProductsRequest(BaseModel):
    products: list[QuoteProductInput] = Field(..., min_length=1, max_length=300)
    account_id: str = Field(..., min_length=1)


# These request types are used only by the MCP transport.  The REST/OpenAPI
# fallback remains backward compatible while MCP write tools require an exact
# confirmation field in their input schema.
class ConfirmedCreateListRequest(CreateListRequest):
    confirm: Literal[True]


class ConfirmedRenameListRequest(RenameListRequest):
    confirm: Literal[True]


class ConfirmedAddPartsRequest(AddPartsRequest):
    confirm: Literal[True]


class ConfirmedUpdateListPartRequest(UpdateListPartRequest):
    confirm: Literal[True]


class ConfirmedCreateQuoteRequest(CreateQuoteRequest):
    confirm: Literal[True]


class ConfirmedAddQuoteProductsRequest(AddQuoteProductsRequest):
    confirm: Literal[True]


class QuoteFromSourceRequest(BaseModel):
    quote_name: str = Field(..., min_length=1, max_length=40)
    account_id: str = Field(..., min_length=1)
    items: list[BOMItem] | None = Field(default=None, max_length=100)
    list_id: str | None = None
    confirm: Literal[True]

    @model_validator(mode="after")
    def exactly_one_source(self) -> "QuoteFromSourceRequest":
        if bool(self.items) == bool(self.list_id):
            raise ValueError("Provide exactly one of items or list_id")
        return self


class BarcodeType(str, Enum):
    product_1d = "product_1d"
    product_2d = "product_2d"
    pack_list_1d = "pack_list_1d"
    pack_list_2d = "pack_list_2d"


class BarcodeInput(BaseModel):
    barcode_type: BarcodeType
    barcode: str = Field(..., min_length=1, max_length=4000)


class DecodeBarcodeRequest(BaseModel):
    barcode_type: BarcodeType
    barcode: str = Field(..., min_length=1, max_length=4000)
    includes: str | None = None


class BatchBarcodeRequest(BaseModel):
    barcodes: list[BarcodeInput] = Field(..., min_length=1, max_length=100)


class BarcodeListComparisonRequest(BatchBarcodeRequest):
    list_id: str = Field(..., min_length=1)
    account_id: str | None = None


class PackingListLookupType(str, Enum):
    invoice = "invoice"
    sales_order = "sales_order"
    purchase_order = "purchase_order"


class PackingListLookupRequest(BaseModel):
    lookup_type: PackingListLookupType
    value: str = Field(..., min_length=1, max_length=100)
    include_pdf: bool = False


class ProductResourcesRequest(BaseModel):
    product_number: str = Field(..., min_length=1)
    # Product details are always returned. Enrichment is opt-in so ordinary
    # research remains one DigiKey request instead of a seven-call fan-out.
    include_media: bool = False
    include_substitutions: bool = False
    include_recommended: bool = False
    include_associations: bool = False
    include_alternate_packaging: bool = False
    include_change_notifications: bool = False
    limit: int = Field(10, ge=1, le=50)
    includes: str | None = None


class RawRequestBody(BaseModel):
    body: dict
