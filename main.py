from __future__ import annotations

import copy
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from client import DigiKeyHTTPError, authorization_from_request, client, error_envelope
from config import settings
from distributor_models import (
    ComponentComparisonRequest,
    ComponentRecommendationRequest,
    ComparisonResult,
    MouserCartExecuteRequest,
    MouserCartPreviewRequest,
    MouserOrderHistoryRequest,
    MouserOrderLookupRequest,
    MouserSearchRequest,
)
from distributors import CredentialUnavailableError
from identity import LOCAL_PRINCIPAL
from mouser_client import MouserHTTPError
from mouser_services import (
    execute_mouser_cart_change,
    get_mouser_cart,
    get_mouser_order,
    mouser_adapter,
    preview_mouser_cart_change,
    search_mouser_order_history,
    search_mouser_products,
)
from multi_distributor import (
    compare_component_offers,
    digikey_adapter,
    recommend_components,
)
from rest_auth import AuthenticatedContext, rest_authenticator
from models import (
    AddPartsRequest,
    AddQuoteProductsRequest,
    BatchBarcodeRequest,
    BarcodeListComparisonRequest,
    BulkBOMRequest,
    CreateListRequest,
    CreateQuoteRequest,
    DecodeBarcodeRequest,
    LifecycleAuditRequest,
    ListDiffRequest,
    ListSyncRequest,
    PackingListLookupRequest,
    PricingOptimizationRequest,
    ProductResourcesRequest,
    ProductSearchRequest,
    QuoteFromSourceRequest,
    RenameListRequest,
    UpdateListPartRequest,
)
from services import (
    MYLISTS_BASE,
    QUOTES_BASE,
    add_parts_to_list,
    add_products_to_quote,
    analyze_bom,
    batch_decode_barcodes,
    compare_barcodes_to_list,
    create_my_list,
    create_quote,
    create_quote_from_source,
    decode_barcode,
    delete_my_list,
    diff_my_list,
    get_all_list_parts,
    get_alternate_packaging,
    get_associated_accounts,
    get_associations,
    get_categories,
    get_category,
    get_digireel_pricing,
    get_manufacturers,
    get_my_list,
    get_my_list_part,
    get_pricing_by_quantity,
    get_product_change_notifications,
    get_product_details,
    get_product_media,
    get_product_pricing,
    get_quote,
    get_quote_products,
    get_recommended_products,
    get_substitutions,
    lifecycle_audit,
    list_my_lists,
    list_quotes,
    lookup_packing_list,
    optimize_bom_pricing,
    product_research_bundle,
    present_mylist_parts,
    remove_my_list_part,
    rename_my_list,
    search_products,
    sync_my_list,
    update_my_list_part,
)


app = FastAPI(
    title="Partuno",
    version="4.0.1",
    description=(
        "Open-source MCP server for DigiKey and Mouser electronic component research, "
        "BOM analysis, sourcing comparison, and safe distributor workflows. "
        "Partuno is provider-neutral and local-first. "
        "Partuno connects to user-authorized DigiKey and Mouser integrations, "
        "supports strict offer comparison and evidence-based component recommendations, "
        "plus DigiKey Product Information V4, "
        "Product Change Notifications, MyLists, Order Status, Quote, Barcode, "
        "Packing List, and Reference APIs. Includes high-level workflows for "
        "BOM analysis, parametric search, pricing optimization, MyList syncing, "
        "lifecycle auditing, quote creation, and receiving reconciliation."
    ),
    openapi_url="/full-openapi.json",
    docs_url="/docs",
    redoc_url=None,
)


@app.exception_handler(DigiKeyHTTPError)
def digikey_error_handler(_: Request, exc: DigiKeyHTTPError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_envelope(exc.status_code, exc.detail, exc.meta),
    )


@app.exception_handler(MouserHTTPError)
def mouser_error_handler(_: Request, exc: MouserHTTPError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_envelope(
            exc.status_code,
            exc.detail,
            exc.meta,
            provider="mouser",
        ),
    )


@app.exception_handler(CredentialUnavailableError)
def credential_error_handler(
    _: Request, exc: CredentialUnavailableError
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=error_envelope(
            503,
            {
                "message": str(exc),
                "error_type": exc.error_type,
                "provider": exc.provider.value,
            },
            provider=exc.provider.value,
            retryable=False,
        ),
    )


@app.exception_handler(ValueError)
def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=error_envelope(400, str(exc), category="validation", error_type="value_error", retryable=False),
    )


@app.exception_handler(RequestValidationError)
def request_validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=error_envelope(422, {"validation_errors": exc.errors()}, category="validation", error_type="request_validation_error", retryable=False),
    )


@app.exception_handler(HTTPException)
def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    category = "authentication" if exc.status_code in {401, 403} else "validation" if exc.status_code == 400 else "application"
    return JSONResponse(
        status_code=exc.status_code,
        content=error_envelope(exc.status_code, exc.detail, category=category, retryable=False),
    )


def auth(request: Request) -> str:
    return authorization_from_request(request)


def distributor_auth(request: Request) -> AuthenticatedContext:
    if settings.partuno_mode == "local" and not request.headers.get("Authorization"):
        return AuthenticatedContext("", LOCAL_PRINCIPAL)
    return rest_authenticator.authenticate(request)


@app.get(
    "/health",
    operation_id="healthCheck",
    summary="Check Partuno and its configured provider capabilities.",
    include_in_schema=True,
)
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": app.version,
        "api_base": settings.api_base,
        "locale": {
            "site": settings.site,
            "language": settings.language,
            "currency": settings.currency,
        },
        "providers": {
            "digikey": digikey_adapter.health(),
            "mouser": mouser_adapter.health(),
        },
    }


# Product Information and Reference APIs
@app.post(
    "/products/search",
    operation_id="searchDigiKeyProducts",
    summary="Search DigiKey using complete manufacturer, category, status, package, series, marketplace, tariff, stock, and parametric filters.",
    openapi_extra={"x-openai-isConsequential": False},
)
def product_search(request_body: ProductSearchRequest, request: Request) -> Any:
    return search_products(request_body, auth(request)).public()


@app.get(
    "/products/{product_number}",
    operation_id="getDigiKeyProductDetails",
    summary="Get current specifications, stock, lifecycle, classifications, variations, and account pricing for one product.",
)
def product_details(
    product_number: str,
    request: Request,
    manufacturer_id: str | None = Query(None),
    account_id: str | None = Query(None),
    includes: str | None = Query(None),
) -> Any:
    return get_product_details(
        product_number,
        auth(request),
        manufacturer_id=manufacturer_id,
        account_id=account_id,
        includes=includes,
    ).public()


@app.get(
    "/products/{product_number}/pricing",
    operation_id="getDigiKeyProductPricing",
    summary="Get paginated current product pricing and packaging variations.",
)
def product_pricing(
    product_number: str,
    request: Request,
    account_id: str | None = Query(None),
    limit: int = Query(10, ge=1, le=10),
    offset: int = Query(0, ge=0),
    in_stock: bool = False,
    exclude_marketplace: bool = True,
    exclude_tariff: bool = False,
    includes: str | None = Query(None),
) -> Any:
    return get_product_pricing(
        product_number,
        auth(request),
        account_id=account_id,
        limit=limit,
        offset=offset,
        in_stock=in_stock,
        exclude_marketplace=exclude_marketplace,
        exclude_tariff=exclude_tariff,
        includes=includes,
    ).public()


@app.get(
    "/products/{product_number}/pricing-by-quantity/{requested_quantity}",
    operation_id="getDigiKeyPricingByQuantity",
    summary="Get exact, effective-MOQ, better-value, maximum, and package pricing options; parent availability is distinct from package stock.",
)
def pricing_by_quantity(
    product_number: str,
    requested_quantity: int,
    request: Request,
    account_id: str | None = Query(None),
    includes: str | None = Query(None),
) -> Any:
    if requested_quantity < 1:
        raise HTTPException(status_code=400, detail="requested_quantity must be at least 1")
    return get_pricing_by_quantity(
        product_number,
        requested_quantity,
        auth(request),
        account_id=account_id,
        includes=includes,
    ).public()


@app.get(
    "/products/{product_number}/digireel-pricing",
    operation_id="getDigiKeyDigiReelPricing",
    summary="Calculate DigiReel pricing for a requested quantity.",
)
def digireel_pricing(
    product_number: str,
    request: Request,
    requested_quantity: int = Query(..., ge=1),
    account_id: str | None = Query(None),
) -> Any:
    return get_digireel_pricing(
        product_number,
        requested_quantity,
        auth(request),
        account_id=account_id,
    ).public()


@app.get(
    "/products/{product_number}/media",
    operation_id="getDigiKeyProductMedia",
    summary="Get datasheets, images, documents, and videos for a product.",
)
def product_media(product_number: str, request: Request) -> Any:
    return get_product_media(product_number, auth(request)).public()


@app.get(
    "/products/{product_number}/substitutions",
    operation_id="getDigiKeySubstitutions",
    summary="Find replacement products with an upstream and locally enforced result limit.",
)
def substitutions(
    product_number: str,
    request: Request,
    limit: int = Query(10, ge=1, le=50),
    search_options: str = "InStock,RoHSCompliant",
    exclude_marketplace: bool = True,
) -> Any:
    return get_substitutions(
        product_number,
        auth(request),
        limit=limit,
        search_options=search_options,
        exclude_marketplace=exclude_marketplace,
    ).public()


@app.get(
    "/products/{product_number}/recommended",
    operation_id="getDigiKeyRecommendedProducts",
    summary="Get DigiKey recommendations; explicit filters may be retried once without filters when DigiKey rejects them.",
)
def recommended(
    product_number: str,
    request: Request,
    limit: int = Query(1, ge=1, le=50),
    search_options: str | None = None,
    exclude_marketplace: bool = True,
) -> Any:
    """Persistent upstream failures preserve the original status and attempt diagnostics."""
    return get_recommended_products(
        product_number,
        auth(request),
        limit=limit,
        search_options=search_options,
        exclude_marketplace=exclude_marketplace,
    ).public()


@app.get(
    "/products/{product_number}/associations",
    operation_id="getDigiKeyProductAssociations",
    summary="Get associated products and accessories.",
)
def associations(product_number: str, request: Request) -> Any:
    return get_associations(product_number, auth(request)).public()


@app.get(
    "/products/{product_number}/alternate-packaging",
    operation_id="getDigiKeyAlternatePackaging",
    summary="Get alternate packaging for the same component.",
)
def alternate_packaging(product_number: str, request: Request) -> Any:
    return get_alternate_packaging(product_number, auth(request)).public()


@app.get(
    "/products/{product_number}/change-notifications",
    operation_id="getDigiKeyProductChangeNotifications",
    summary="Get PCNs with raw API dates and additive description-date diagnostics.",
)
def change_notifications(
    product_number: str,
    request: Request,
    includes: str | None = Query(None),
) -> Any:
    return get_product_change_notifications(
        product_number,
        auth(request),
        includes=includes,
    ).public()


@app.post(
    "/products/research-bundle",
    operation_id="getDigiKeyProductResearchBundle",
    summary="Retrieve product details plus optional media, substitutions, recommendations, associations, alternate packaging, and change notifications in one call.",
    openapi_extra={"x-openai-isConsequential": False},
)
def product_bundle(request_body: ProductResourcesRequest, request: Request, account_id: str | None = Query(None)) -> Any:
    """Successful enrichments are retained when an optional DigiKey enrichment fails."""
    return product_research_bundle(request_body, auth(request), account_id=account_id)


@app.get(
    "/manufacturers",
    operation_id="listDigiKeyManufacturers",
    summary="Get DigiKey manufacturers and manufacturer IDs for filtered search.",
)
def manufacturers(
    request: Request,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Any:
    """Return a bounded local page; DigiKey's directory itself is unpaginated."""
    return get_manufacturers(auth(request), limit=limit, offset=offset).public()


@app.get(
    "/categories",
    operation_id="listDigiKeyCategories",
    summary="Get the DigiKey product category tree and category IDs.",
)
def categories(request: Request) -> Any:
    return get_categories(auth(request)).public()


@app.get(
    "/categories/{category_id}",
    operation_id="getDigiKeyCategory",
    summary="Get one DigiKey category and its parametric context.",
)
def category(category_id: int, request: Request) -> Any:
    return get_category(category_id, auth(request)).public()


@app.get(
    "/accounts",
    operation_id="getDigiKeyAssociatedAccounts",
    summary="Get DigiKey Account IDs associated with the authenticated login.",
)
def associated_accounts(request: Request) -> Any:
    return get_associated_accounts(auth(request)).public()


# High-level workflows
@app.post(
    "/workflows/bom/analyze",
    operation_id="analyzeDigiKeyBOM",
    summary="Analyze a multi-part BOM for price, stock, lifecycle, lead time, compliance, PCNs, alternate packaging, and substitutions.",
    openapi_extra={"x-openai-isConsequential": False},
)
def bom_analysis(request_body: BulkBOMRequest, request: Request) -> Any:
    return analyze_bom(request_body, auth(request))


@app.post(
    "/workflows/bom/optimize-pricing",
    operation_id="optimizeDigiKeyBOMPricing",
    summary="Optimize BOM quantities and packaging across exact, MOQ, better-value, alternate package, and DigiReel options.",
    openapi_extra={"x-openai-isConsequential": False},
)
def bom_optimize(request_body: PricingOptimizationRequest, request: Request) -> Any:
    return optimize_bom_pricing(request_body, auth(request))


@app.post(
    "/workflows/lifecycle-audit",
    operation_id="auditDigiKeyLifecycle",
    summary="Audit a BOM or MyList for end-of-life risk, last-buy dates, shortages, long lead times, PCNs, and substitutes.",
    openapi_extra={"x-openai-isConsequential": False},
)
def lifecycle(request_body: LifecycleAuditRequest, request: Request) -> Any:
    return lifecycle_audit(request_body, auth(request))


# MyLists
@app.get(
    "/lists",
    operation_id="listDigiKeyMyLists",
    summary="List the authenticated user's DigiKey MyLists.",
)
def lists(
    request: Request,
    account_id: str | None = Query(None),
    start_index: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    return list_my_lists(
        auth(request),
        account_id=account_id,
        start_index=start_index,
        limit=limit,
    ).public()


@app.get(
    "/lists/{list_id}",
    operation_id="getDigiKeyMyList",
    summary="Get one DigiKey MyList.",
)
def one_list(
    list_id: str,
    request: Request,
    account_id: str | None = Query(None),
) -> Any:
    return get_my_list(
        list_id,
        auth(request),
        account_id=account_id,
    ).public()


@app.get(
    "/lists/{list_id}/parts",
    operation_id="getDigiKeyMyListParts",
    summary="Get every MyList part in compact or raw-full detail, automatically following pagination.",
)
def list_parts(
    list_id: str,
    request: Request,
    account_id: str | None = Query(None),
    assemblies: int = Query(1, ge=1),
    include_attrition: bool = False,
    response_detail: Literal["compact", "full"] = "full",
    include_substitutions: bool = False,
    substitution_limit: int = Query(5, ge=0, le=25),
    include_environmental_docs: bool = False,
    include_images: bool = False,
    include_empty_fields: bool = False,
) -> Any:
    parts = get_all_list_parts(
        list_id,
        auth(request),
        account_id=account_id,
        assemblies=assemblies,
        include_attrition=include_attrition,
    )
    return present_mylist_parts(
        parts,
        list_id=list_id,
        response_detail=response_detail,
        include_substitutions=include_substitutions,
        substitution_limit=substitution_limit,
        include_environmental_docs=include_environmental_docs,
        include_images=include_images,
        include_empty_fields=include_empty_fields,
    )


@app.get(
    "/lists/{list_id}/parts/{unique_id}",
    operation_id="getDigiKeyMyListPart",
    summary="Get one MyList part by its unique list-part ID.",
)
def list_part(
    list_id: str,
    unique_id: str,
    request: Request,
    account_id: str | None = Query(None),
    assemblies: int = Query(1, ge=1),
    created_by: str = "",
) -> Any:
    return get_my_list_part(
        list_id,
        unique_id,
        auth(request),
        account_id=account_id,
        assemblies=assemblies,
        created_by=created_by,
    ).public()


@app.post(
    "/lists",
    operation_id="createDigiKeyMyList",
    summary="Create a DigiKey MyList.",
    openapi_extra={"x-openai-isConsequential": True},
)
def create_list(request_body: CreateListRequest, request: Request) -> Any:
    return create_my_list(
        request_body.list_name,
        auth(request),
        tags=request_body.tags,
        created_by=request_body.created_by,
        account_id=request_body.account_id,
    ).public()


@app.put(
    "/lists/{list_id}/name",
    operation_id="renameDigiKeyMyList",
    summary="Rename a DigiKey MyList.",
    openapi_extra={"x-openai-isConsequential": True},
)
def rename_list(list_id: str, request_body: RenameListRequest, request: Request) -> Any:
    return rename_my_list(
        list_id,
        request_body.new_name,
        auth(request),
        account_id=request_body.account_id,
    ).public()


@app.delete(
    "/lists/{list_id}",
    operation_id="deleteDigiKeyMyList",
    summary="Permanently delete a DigiKey MyList.",
    openapi_extra={"x-openai-isConsequential": True},
)
def remove_list(
    list_id: str,
    request: Request,
    account_id: str | None = Query(None),
) -> Any:
    return delete_my_list(list_id, auth(request), account_id=account_id).public()


@app.post(
    "/lists/{list_id}/parts",
    operation_id="addPartsToDigiKeyMyList",
    summary="Add products and quantities to a DigiKey MyList.",
    openapi_extra={"x-openai-isConsequential": True},
)
def add_parts(list_id: str, request_body: AddPartsRequest, request: Request) -> Any:
    return {
        "list_id": list_id,
        "responses": add_parts_to_list(
            list_id,
            request_body.parts,
            auth(request),
            account_id=request_body.account_id,
            insertion_index=request_body.insertion_index,
        ),
    }


@app.put(
    "/lists/{list_id}/parts/{unique_id}",
    operation_id="updateDigiKeyMyListPart",
    summary="Update a part number, quantity, packaging, notes, target price, or references while preserving omitted fields.",
    openapi_extra={"x-openai-isConsequential": True},
)
def update_part(
    list_id: str,
    unique_id: str,
    request_body: UpdateListPartRequest,
    request: Request,
) -> Any:
    return update_my_list_part(
        list_id,
        unique_id,
        request_body,
        auth(request),
    ).public()


@app.delete(
    "/lists/{list_id}/parts/{unique_id}",
    operation_id="removePartFromDigiKeyMyList",
    summary="Permanently remove one part from a DigiKey MyList.",
    openapi_extra={"x-openai-isConsequential": True},
)
def remove_part(
    list_id: str,
    unique_id: str,
    request: Request,
    created_by: str = "",
    account_id: str | None = Query(None),
) -> Any:
    return remove_my_list_part(
        list_id,
        unique_id,
        auth(request),
        created_by=created_by,
        account_id=account_id,
    ).public()


@app.post(
    "/lists/{list_id}/diff",
    operation_id="diffDigiKeyMyList",
    summary="Dry-run a proposed BOM against a MyList and show exact additions, updates, duplicate consolidation, and optional removals.",
    openapi_extra={"x-openai-isConsequential": False},
)
def list_diff(list_id: str, request_body: ListDiffRequest, request: Request) -> Any:
    return diff_my_list(list_id, request_body, auth(request))


@app.post(
    "/lists/{list_id}/sync",
    operation_id="syncDigiKeyMyList",
    summary="Apply an explicitly approved MyList diff, including additions, updates, duplicate consolidation, and optional removals.",
    openapi_extra={"x-openai-isConsequential": True},
)
def list_sync(list_id: str, request_body: ListSyncRequest, request: Request) -> Any:
    return sync_my_list(list_id, request_body, auth(request))


# Order Status
@app.get(
    "/orders",
    operation_id="searchDigiKeyOrders",
    summary="Search the authenticated user's DigiKey orders by date.",
)
def orders(
    request: Request,
    start_date: str | None = Query(None, description="YYYY-MM-DD"),
    end_date: str | None = Query(None, description="YYYY-MM-DD"),
    shared: bool = False,
    page_number: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=25),
    account_id: str | None = Query(None),
) -> Any:
    params: dict[str, Any] = {
        "Shared": shared,
        "PageNumber": page_number,
        "PageSize": page_size,
    }
    if start_date:
        params["StartDate"] = start_date
    if end_date:
        params["EndDate"] = end_date
    return client.request(
        "GET",
        "/orderstatus/v4/orders",
        auth(request),
        account_id=account_id,
        params=params,
    ).public()


@app.get(
    "/orders/{sales_order_id}",
    operation_id="getDigiKeySalesOrder",
    summary="Get shipment, tracking, backorder, and line-item status for one sales order.",
)
def sales_order(
    sales_order_id: int,
    request: Request,
    account_id: str | None = Query(None),
) -> Any:
    return client.request(
        "GET",
        f"/orderstatus/v4/salesorder/{sales_order_id}",
        auth(request),
        account_id=account_id,
    ).public()


# Quotes
@app.get(
    "/quotes",
    operation_id="listDigiKeyQuotes",
    summary="List DigiKey quotes for an account.",
)
def quotes(
    request: Request,
    account_id: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
) -> Any:
    return list_quotes(
        auth(request),
        account_id=account_id,
        offset=offset,
        limit=limit,
    ).public()


@app.post(
    "/quotes",
    operation_id="createDigiKeyQuote",
    summary="Create an empty DigiKey quote.",
    openapi_extra={"x-openai-isConsequential": True},
)
def new_quote(request_body: CreateQuoteRequest, request: Request) -> Any:
    return create_quote(request_body, auth(request)).public()


@app.get(
    "/quotes/{quote_id}",
    operation_id="getDigiKeyQuote",
    summary="Get one DigiKey quote's metadata.",
)
def quote_details(
    quote_id: int,
    request: Request,
    account_id: str | None = Query(None),
) -> Any:
    return get_quote(quote_id, auth(request), account_id=account_id).public()


@app.get(
    "/quotes/{quote_id}/products",
    operation_id="getDigiKeyQuoteProducts",
    summary="Get the products and locked pricing in a quote.",
)
def quote_products(
    quote_id: int,
    request: Request,
    account_id: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=50),
) -> Any:
    return get_quote_products(
        quote_id,
        auth(request),
        account_id=account_id,
        offset=offset,
        limit=limit,
    ).public()


@app.post(
    "/quotes/{quote_id}/products",
    operation_id="addProductsToDigiKeyQuote",
    summary="Add up to 300 products per request to an existing DigiKey quote.",
    openapi_extra={"x-openai-isConsequential": True},
)
def quote_add_products(
    quote_id: int,
    request_body: AddQuoteProductsRequest,
    request: Request,
) -> Any:
    products = [
        {
            "ProductNumber": item.product_number,
            "CustomerReference": item.customer_reference,
            "Quantities": item.quantities,
        }
        for item in request_body.products
    ]
    return {
        "quote_id": quote_id,
        "responses": add_products_to_quote(
            quote_id,
            products,
            auth(request),
            account_id=request_body.account_id,
        ),
    }


@app.post(
    "/workflows/quotes/from-source",
    operation_id="createDigiKeyQuoteFromSource",
    summary="Create and populate a quote from an approved BOM or an existing MyList.",
    openapi_extra={"x-openai-isConsequential": True},
)
def quote_from_source(request_body: QuoteFromSourceRequest, request: Request) -> Any:
    return create_quote_from_source(request_body, auth(request))


# Barcode and Packing List
@app.post(
    "/barcodes/decode",
    operation_id="decodeDigiKeyBarcode",
    summary="Decode a DigiKey product-bag or packing-list 1D or 2D barcode.",
    openapi_extra={"x-openai-isConsequential": False},
)
def barcode_decode(request_body: DecodeBarcodeRequest, request: Request) -> Any:
    response = decode_barcode(request_body, auth(request))
    return response.public()


@app.post(
    "/barcodes/batch-decode",
    operation_id="batchDecodeDigiKeyBarcodes",
    summary="Decode a batch of product and packing-list barcodes and total received quantities by part number.",
    openapi_extra={"x-openai-isConsequential": False},
)
def barcode_batch(request_body: BatchBarcodeRequest, request: Request) -> Any:
    return batch_decode_barcodes(request_body.barcodes, auth(request))


@app.post(
    "/barcodes/compare-to-list",
    operation_id="compareDigiKeyBarcodesToMyList",
    summary="Compare scanned received quantities against the required parts in a MyList.",
    openapi_extra={"x-openai-isConsequential": False},
)
def barcode_compare(request_body: BarcodeListComparisonRequest, request: Request) -> Any:
    return compare_barcodes_to_list(request_body, auth(request))


@app.post(
    "/packing-lists/lookup",
    operation_id="lookupDigiKeyPackingList",
    summary="Retrieve packing-list information by invoice, sales order, or purchase order number.",
    openapi_extra={"x-openai-isConsequential": False},
)
def packing_list(request_body: PackingListLookupRequest, request: Request) -> Any:
    return lookup_packing_list(request_body, auth(request)).public()


# Mouser and cross-distributor APIs. Remote single-user mode validates the
# DigiKey bearer before using protected Mouser keys; local mode can use a local
# principal when the request has no DigiKey authorization header.
@app.post(
    "/mouser/products/search",
    operation_id="searchMouserProducts",
    summary="Search Mouser catalog, availability, compliance, lifecycle, and price-break data.",
    openapi_extra={"x-openai-isConsequential": False},
)
def mouser_product_search(
    request_body: MouserSearchRequest,
    request: Request,
) -> Any:
    context = distributor_auth(request)
    return search_mouser_products(request_body, principal=context.principal)


@app.post(
    "/components/compare",
    operation_id="compareComponentOffers",
    response_model=ComparisonResult,
    summary="Compare strict manufacturer and MPN matches across DigiKey and Mouser.",
    openapi_extra={"x-openai-isConsequential": False},
)
def component_offer_comparison(
    request_body: ComponentComparisonRequest,
    request: Request,
) -> Any:
    context = distributor_auth(request)
    return compare_component_offers(
        request_body,
        principal=context.principal,
        authorization=context.authorization,
    )


@app.post(
    "/components/recommend",
    operation_id="recommendComponents",
    summary="Evaluate project candidates against explicit engineering requirements and return a Pareto shortlist.",
    openapi_extra={"x-openai-isConsequential": False},
)
def component_recommendation(
    request_body: ComponentRecommendationRequest,
    request: Request,
) -> Any:
    context = distributor_auth(request)
    return recommend_components(
        request_body,
        principal=context.principal,
        authorization=context.authorization,
    )


@app.post(
    "/mouser/order-history/search",
    operation_id="searchMouserOrderHistory",
    summary="Read Mouser order history by a documented date filter or date range.",
    openapi_extra={"x-openai-isConsequential": False},
)
def mouser_order_history_search(
    request_body: MouserOrderHistoryRequest,
    request: Request,
) -> Any:
    context = distributor_auth(request)
    return search_mouser_order_history(
        request_body,
        principal=context.principal,
    )


@app.post(
    "/mouser/order-history/order",
    operation_id="getMouserOrder",
    summary="Read one Mouser order by sales-order number or web-order number.",
    openapi_extra={"x-openai-isConsequential": False},
)
def mouser_order_lookup(
    request_body: MouserOrderLookupRequest,
    request: Request,
) -> Any:
    context = distributor_auth(request)
    return get_mouser_order(request_body, principal=context.principal)


@app.get(
    "/mouser/carts/{cart_key}",
    operation_id="getMouserCart",
    summary="Read a Mouser cart without modifying it.",
    openapi_extra={"x-openai-isConsequential": False},
)
def mouser_cart(cart_key: str, request: Request) -> Any:
    context = distributor_auth(request)
    return get_mouser_cart(cart_key, principal=context.principal)


@app.post(
    "/mouser/carts/preview",
    operation_id="previewMouserCartChange",
    summary="Preview the exact effect of a Mouser cart or schedule mutation and issue a short-lived token.",
    openapi_extra={"x-openai-isConsequential": False},
)
def mouser_cart_change_preview(
    request_body: MouserCartPreviewRequest,
    request: Request,
) -> Any:
    context = distributor_auth(request)
    return preview_mouser_cart_change(
        request_body,
        principal=context.principal,
    )


@app.post(
    "/mouser/carts/execute",
    operation_id="executeMouserCartChange",
    summary="Execute an exact, unexpired, one-time Mouser cart preview.",
    openapi_extra={"x-openai-isConsequential": True},
)
def mouser_cart_change_execute(
    request_body: MouserCartExecuteRequest,
    request: Request,
) -> Any:
    context = distributor_auth(request)
    return execute_mouser_cart_change(
        request_body,
        principal=context.principal,
    )


# Custom GPTs are easier to manage with a compact schema. The full schema remains
# available for debugging and optional imports.
ACTION_OPERATION_IDS = {
    "searchDigiKeyProducts",
    "getDigiKeyProductDetails",
    "getDigiKeyProductResearchBundle",
    "listDigiKeyManufacturers",
    "listDigiKeyCategories",
    "getDigiKeyAssociatedAccounts",
    "analyzeDigiKeyBOM",
    "optimizeDigiKeyBOMPricing",
    "auditDigiKeyLifecycle",
    "listDigiKeyMyLists",
    "getDigiKeyMyList",
    "getDigiKeyMyListParts",
    "createDigiKeyMyList",
    "renameDigiKeyMyList",
    "deleteDigiKeyMyList",
    "addPartsToDigiKeyMyList",
    "updateDigiKeyMyListPart",
    "removePartFromDigiKeyMyList",
    "diffDigiKeyMyList",
    "syncDigiKeyMyList",
    "searchDigiKeyOrders",
    "getDigiKeySalesOrder",
    "listDigiKeyQuotes",
    "getDigiKeyQuote",
    "getDigiKeyQuoteProducts",
    "createDigiKeyQuoteFromSource",
    "decodeDigiKeyBarcode",
    "batchDecodeDigiKeyBarcodes",
    "compareDigiKeyBarcodesToMyList",
    "lookupDigiKeyPackingList",
    "searchMouserProducts",
    "compareComponentOffers",
    "recommendComponents",
    "searchMouserOrderHistory",
    "getMouserOrder",
    "getMouserCart",
    "previewMouserCartChange",
    "executeMouserCartChange",
}


def _with_oauth_security(schema: dict[str, Any], server_url: str) -> dict[str, Any]:
    schema.setdefault("components", {}).setdefault("securitySchemes", {})[
        "DigiKeyOAuth"
    ] = {
        "type": "oauth2",
        "flows": {
            "authorizationCode": {
                "authorizationUrl": "https://api.digikey.com/v1/oauth2/authorize",
                "tokenUrl": "https://api.digikey.com/v1/oauth2/token",
                "scopes": {},
            }
        },
    }
    schema["security"] = [{"DigiKeyOAuth": []}]
    schema["servers"] = [{"url": server_url.rstrip("/")}]
    health = schema.get("paths", {}).get("/health", {}).get("get")
    if isinstance(health, dict):
        health["security"] = []
    return schema


@app.get("/action-openapi.json", include_in_schema=False)
def action_openapi(request: Request) -> JSONResponse:
    schema = copy.deepcopy(app.openapi())
    filtered_paths: dict[str, Any] = {}
    for path, methods in schema.get("paths", {}).items():
        kept: dict[str, Any] = {}
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            if operation.get("operationId") in ACTION_OPERATION_IDS:
                kept[method] = operation
        if kept:
            filtered_paths[path] = kept
    schema["paths"] = filtered_paths
    schema["info"]["title"] = "Partuno, Compact Schema"
    schema["info"]["description"] += (
        " This compact schema includes 38 high-value actions. The full schema is at /full-openapi.json."
    )
    schema = _with_oauth_security(schema, str(request.base_url))
    return JSONResponse(schema)


# Apply OAuth metadata to FastAPI's full schema too.
_original_openapi = app.openapi


def custom_openapi() -> dict[str, Any]:
    schema = _original_openapi()
    server = "https://replace-with-your-deployed-domain.example"
    return _with_oauth_security(schema, server)


app.openapi = custom_openapi  # type: ignore[method-assign]
