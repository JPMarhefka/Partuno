from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.auth import AccessToken, OAuthProxy, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.base import ToolResult

from client import DigiKeyHTTPError, client, error_envelope
from config import settings
from credentials import (
    CredentialPurpose,
    CredentialUnavailableError,
    Provider,
    credential_store,
    credential_value,
)
from identity import LOCAL_PRINCIPAL, digikey_subject
from mouser_client import MouserHTTPError
from mouser_mcp import register_comparison_tools, register_mouser_tools
from models import (
    AddPartsRequest,
    AddQuoteProductsRequest,
    BatchBarcodeRequest,
    BarcodeListComparisonRequest,
    BulkBOMRequest,
    CreateListRequest,
    CreateQuoteRequest,
    DecodeBarcodeRequest,
    ConfirmedAddPartsRequest,
    ConfirmedAddQuoteProductsRequest,
    ConfirmedCreateListRequest,
    ConfirmedCreateQuoteRequest,
    ConfirmedRenameListRequest,
    ConfirmedUpdateListPartRequest,
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
    get_associated_accounts,
    get_categories,
    get_category,
    get_product_change_notifications,
    get_my_list,
    get_product_media,
    get_product_pricing,
    get_pricing_by_quantity,
    get_digireel_pricing,
    get_substitutions,
    get_recommended_products,
    get_associations,
    get_alternate_packaging,
    get_product_details,
    get_quote,
    get_quote_products,
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


READ_ONLY = {
    "readOnlyHint": True,
    "idempotentHint": True,
    "openWorldHint": True,
}
WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
UPDATE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


class ReferenceResponseCache:
    """Small process-local cache for shared, non-account DigiKey reference data.

    It intentionally never stores bearer tokens or account-specific pricing.  A
    A local Partuno restart simply clears it and safely falls back to DigiKey.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_or_load(self, key: str, loader: Any) -> Any:
        now = time.monotonic()
        async with self._lock:
            cached = self._entries.get(key)
            if cached and cached[0] > now:
                return cached[1]
        value = await asyncio.to_thread(loader)
        async with self._lock:
            self._entries[key] = (time.monotonic() + self.ttl_seconds, value)
        return value


class DigiKeyErrorMiddleware(Middleware):
    """Keep distributor diagnostics visible in MCP errors instead of masking them."""

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext) -> Any:
        try:
            return await call_next(context)
        except DigiKeyHTTPError as exc:
            return _mcp_error_result(exc)
        except MouserHTTPError as exc:
            return _mcp_error_result(exc)
        except CredentialUnavailableError as exc:
            return _mcp_error_result(exc)
        except HTTPException as exc:
            return _mcp_error_result(exc)
        except ValueError as exc:
            return _mcp_error_result(exc)
        except ToolError as exc:
            # FastMCP executes the tool before control returns to middleware and
            # wraps ordinary exceptions in ToolError.  Unwrap the direct cause so
            # a DigiKey 400/404 (and its correlation/rate-limit metadata) is not
            # replaced by FastMCP's generic INVALID_ARGUMENT message.
            cause = exc.__cause__
            if isinstance(
                cause,
                (
                    DigiKeyHTTPError,
                    MouserHTTPError,
                    CredentialUnavailableError,
                    HTTPException,
                    ValueError,
                ),
            ):
                return _mcp_error_result(cause)
            raise


def _mcp_error_result(
    exc: (
        DigiKeyHTTPError
        | MouserHTTPError
        | CredentialUnavailableError
        | HTTPException
        | ValueError
    ),
) -> ToolResult:
    """Return expected errors as MCP text and structured content."""
    if isinstance(exc, DigiKeyHTTPError):
        payload = error_envelope(
            exc.status_code, exc.detail, exc.meta, provider="digikey"
        )
    elif isinstance(exc, MouserHTTPError):
        payload = error_envelope(
            exc.status_code, exc.detail, exc.meta, provider="mouser"
        )
    elif isinstance(exc, CredentialUnavailableError):
        provider = exc.provider.value
        payload = error_envelope(
            503,
            {
                "message": str(exc),
                "error_type": exc.error_type,
                "provider": provider,
            },
            provider=provider,
            retryable=False,
        )
    elif isinstance(exc, HTTPException):
        category = "authentication" if exc.status_code in {401, 403} else "validation"
        payload = error_envelope(
            exc.status_code,
            exc.detail,
            category=category,
            retryable=False,
        )
    else:
        payload = error_envelope(
            400,
            str(exc),
            category="validation",
            error_type="value_error",
            retryable=False,
        )
    # Preserve the JSON text error while also exposing the same shape in
    # structuredContent for MCP clients that do not parse text content.
    return ToolResult(
        content=json.dumps(payload, default=str),
        structured_content=payload,
        is_error=True,
    )


class DigiKeyOpaqueTokenVerifier(TokenVerifier):
    """Validate DigiKey opaque OAuth tokens against the Reference API.

    DigiKey issues opaque access tokens and does not publish a token-introspection
    endpoint. A small positive-result cache prevents every MCP protocol request
    from consuming another DigiKey API call.
    """

    def __init__(self, *, cache_seconds: int = 45) -> None:
        super().__init__(required_scopes=[])
        self.cache_seconds = max(5, cache_seconds)
        self._cache: dict[str, tuple[float, AccessToken]] = {}
        self._lock = asyncio.Lock()

    async def verify_token(self, token: str) -> AccessToken | None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.monotonic()
        cached = self._cache.get(token_hash)
        if cached and cached[0] > now:
            return cached[1]

        values = credential_store.get(
            principal=LOCAL_PRINCIPAL,
            provider=Provider.DIGIKEY,
            purpose=CredentialPurpose.OAUTH_CLIENT,
        )
        client_id = credential_value(values or {}, "client_id")
        if not client_id:
            return None
        headers = {
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": client_id,
            "X-DIGIKEY-Locale-Site": settings.site,
            "X-DIGIKEY-Locale-Language": settings.language,
            "X-DIGIKEY-Locale-Currency": settings.currency,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=settings.request_timeout_seconds,
                follow_redirects=False,
            ) as http:
                response = await http.get(
                    f"{settings.api_base}/CustomerResource/v1/associatedaccounts",
                    headers=headers,
                )
        except httpx.HTTPError:
            return None

        if response.status_code in {401, 403}:
            self._cache.pop(token_hash, None)
            return None
        if response.status_code != 200:
            return None

        try:
            accounts = response.json()
        except ValueError:
            return None

        verified = AccessToken(
            token=token,
            client_id=client_id,
            scopes=[],
            # DigiKey access tokens expire after ten minutes. Keep the MCP
            # assertion below that limit so clients refresh before DigiKey
            # rejects a valid-looking but expired opaque token.
            expires_at=int(time.time()) + 540,
            subject=digikey_subject(accounts),
            claims={"associated_accounts": accounts},
        )
        async with self._lock:
            self._cache[token_hash] = (now + self.cache_seconds, verified)
            expired = [key for key, value in self._cache.items() if value[0] <= now]
            for key in expired:
                self._cache.pop(key, None)
        return verified


def _authorization() -> str:
    access_token = get_access_token()
    if access_token is not None and access_token.token:
        return f"Bearer {access_token.token}"
    if settings.partuno_mode == "local" and settings.digikey_access_token:
        return f"Bearer {settings.digikey_access_token}"
    raise HTTPException(status_code=401, detail="DigiKey authorization is required")


def _principal() -> str:
    access_token = get_access_token()
    if settings.partuno_mode == "local" and (
        access_token is None or not access_token.token
    ):
        return LOCAL_PRINCIPAL
    if access_token is None or not access_token.token:
        raise HTTPException(status_code=401, detail="DigiKey authorization is required")
    if not access_token.subject:
        raise HTTPException(
            status_code=401,
            detail="Authenticated DigiKey principal is unavailable",
        )
    return access_token.subject


def build_mcp_server(*, local: bool = False) -> FastMCP | None:
    """Build the shared Partuno tool catalog for remote or local transport."""
    if not local and not settings.mcp_enabled:
        return None

    reference_cache = ReferenceResponseCache(settings.reference_cache_seconds)
    auth_provider: OAuthProxy | None = None
    if not local:
        oauth_values = credential_store.get(
            principal=LOCAL_PRINCIPAL,
            provider=Provider.DIGIKEY,
            purpose=CredentialPurpose.OAUTH_CLIENT,
        )
        client_id = credential_value(oauth_values or {}, "client_id")
        client_secret = credential_value(oauth_values or {}, "client_secret")
        if not client_id or not client_secret:
            return None

        verifier = DigiKeyOpaqueTokenVerifier()
        auth_provider = OAuthProxy(
            upstream_authorization_endpoint="https://api.digikey.com/v1/oauth2/authorize",
            upstream_token_endpoint="https://api.digikey.com/v1/oauth2/token",
            upstream_client_id=client_id,
            upstream_client_secret=client_secret,
            token_verifier=verifier,
            base_url=settings.mcp_base_url,
            resource_base_url=settings.mcp_base_url,
            redirect_path="/auth/callback",
            service_documentation_url=f"{settings.mcp_base_url}/docs",
            valid_scopes=[],
            forward_pkce=False,
            forward_resource=False,
            token_endpoint_auth_method="client_secret_post",
            fallback_access_token_expiry_seconds=540,
            fallback_refresh_token_expiry_seconds=90 * 24 * 60 * 60,
            fastmcp_access_token_expiry_seconds=540,
            token_expiry_threshold_seconds=60,
            jwt_signing_key=settings.mcp_jwt_signing_key,
            require_authorization_consent=True,
        )

    mcp = FastMCP(
        name="Partuno",
        version="4.0.1",
        website_url=None if local else settings.mcp_base_url,
        auth=auth_provider,
        instructions=(
            "Partuno is an open-source, provider-neutral DigiKey and Mouser MCP server for "
            "electronic component research, BOM analysis, sourcing comparison, and safe "
            "distributor workflows. Use DigiKey and Mouser "
            "for authoritative component research, live inventory, "
            "pricing, and exact offer comparison. Ask for missing critical engineering "
            "requirements before recommending a component, and distinguish suitability "
            "evidence from distributor offer quality. Manufacturer lead time is not shipping. "
            "Use DigiKey for BOM analysis, MyLists, quotes, order status, barcodes, and packing lists. "
            "Prefer exact DigiKey product numbers for exact lookups. Read-only tools may run "
            "without confirmation. Obtain explicit approval before any MyList or quote change. "
            "Preview every Mouser cart change and show the exact diff before executing its "
            "one-time token. This server cannot place orders."
        ),
    )
    mcp.add_middleware(DigiKeyErrorMiddleware())

    @mcp.tool(name="search_products", annotations=READ_ONLY)
    def mcp_search_products(request: ProductSearchRequest) -> Any:
        """Search DigiKey with native filters; a marked fallback removes explicitly non-matching tariff or Marketplace variations only when DigiKey returns them."""
        return search_products(request, _authorization()).public()

    @mcp.tool(name="get_product_details", annotations=READ_ONLY)
    def mcp_get_product_details(
        product_number: str,
        manufacturer_id: str | None = None,
        account_id: str | None = None,
        includes: str | None = None,
    ) -> Any:
        """Get current specifications, inventory, lifecycle, classifications, variations, and account pricing for one product."""
        return get_product_details(
            product_number,
            _authorization(),
            manufacturer_id=manufacturer_id,
            account_id=account_id,
            includes=includes,
        ).public()

    @mcp.tool(name="get_product_pricing", annotations=READ_ONLY)
    def mcp_get_product_pricing(
        product_number: str,
        account_id: str | None = None,
        limit: int = 10,
        offset: int = 0,
        in_stock: bool = False,
        exclude_marketplace: bool = True,
        exclude_tariff: bool = False,
        includes: str | None = None,
    ) -> Any:
        """Get current pricing and packaging variations for one DigiKey product."""
        return get_product_pricing(
            product_number,
            _authorization(),
            account_id=account_id,
            limit=limit,
            offset=offset,
            in_stock=in_stock,
            exclude_marketplace=exclude_marketplace,
            exclude_tariff=exclude_tariff,
            includes=includes,
        ).public()

    @mcp.tool(name="get_pricing_by_quantity", annotations=READ_ONLY)
    def mcp_get_pricing_by_quantity(
        product_number: str,
        requested_quantity: int,
        account_id: str | None = None,
        includes: str | None = None,
    ) -> Any:
        """Compare exact, effective-MOQ, BetterValue, and maximum-order options; parent availability remains distinct from package stock."""
        return get_pricing_by_quantity(
            product_number,
            requested_quantity,
            _authorization(),
            account_id=account_id,
            includes=includes,
        ).public()

    @mcp.tool(name="get_digireel_pricing", annotations=READ_ONLY)
    def mcp_get_digireel_pricing(
        product_number: str,
        requested_quantity: int,
        account_id: str | None = None,
    ) -> Any:
        """Calculate DigiReel pricing for a requested quantity."""
        return get_digireel_pricing(
            product_number,
            requested_quantity,
            _authorization(),
            account_id=account_id,
        ).public()

    @mcp.tool(name="get_product_media", annotations=READ_ONLY)
    def mcp_get_product_media(product_number: str) -> Any:
        """Get a product's datasheets, images, documents, and video links."""
        return get_product_media(product_number, _authorization()).public()

    @mcp.tool(name="get_substitutions", annotations=READ_ONLY)
    def mcp_get_substitutions(
        product_number: str,
        limit: int = 10,
        search_options: str = "InStock,RoHSCompliant",
        exclude_marketplace: bool = True,
    ) -> Any:
        """Find replacement products with the requested limit enforced upstream and locally."""
        return get_substitutions(
            product_number,
            _authorization(),
            limit=limit,
            search_options=search_options,
            exclude_marketplace=exclude_marketplace,
        ).public()

    @mcp.tool(name="get_recommended_products", annotations=READ_ONLY)
    def mcp_get_recommended_products(
        product_number: str,
        limit: int = 1,
        search_options: str | None = None,
        exclude_marketplace: bool = True,
    ) -> Any:
        """Get DigiKey recommendations; explicit filters rejected with 404/500 are retried once without filters and marked."""
        return get_recommended_products(
            product_number,
            _authorization(),
            limit=limit,
            search_options=search_options,
            exclude_marketplace=exclude_marketplace,
        ).public()

    @mcp.tool(name="get_product_associations", annotations=READ_ONLY)
    def mcp_get_product_associations(product_number: str) -> Any:
        """Get associated products and accessories."""
        return get_associations(product_number, _authorization()).public()

    @mcp.tool(name="get_alternate_packaging", annotations=READ_ONLY)
    def mcp_get_alternate_packaging(product_number: str) -> Any:
        """Get alternate packaging for the same component."""
        return get_alternate_packaging(product_number, _authorization()).public()

    @mcp.tool(name="get_product_change_notifications", annotations=READ_ONLY)
    def mcp_get_product_change_notifications(
        product_number: str, includes: str | None = None
    ) -> Any:
        """Get PCNs with raw API dates and additive description-date diagnostics."""
        return get_product_change_notifications(
            product_number, _authorization(), includes=includes
        ).public()

    @mcp.tool(name="research_product", annotations=READ_ONLY)
    def mcp_research_product(
        request: ProductResourcesRequest,
        account_id: str | None = None,
    ) -> Any:
        """Get product details plus optional enrichments; partial failures are returned under errors without discarding successful results."""
        return product_research_bundle(request, _authorization(), account_id=account_id)

    @mcp.tool(name="list_manufacturers", annotations=READ_ONLY)
    async def mcp_list_manufacturers(limit: int = 100, offset: int = 0) -> Any:
        """List a bounded page of DigiKey manufacturers and their manufacturer IDs."""
        from services import get_manufacturers

        return get_manufacturers(_authorization(), limit=limit, offset=offset).public()

    @mcp.tool(name="list_categories", annotations=READ_ONLY)
    async def mcp_list_categories() -> Any:
        """Get the DigiKey product category tree and category IDs."""
        return await reference_cache.get_or_load(
            "categories", lambda: get_categories(_authorization()).public()
        )

    @mcp.tool(name="get_category", annotations=READ_ONLY)
    def mcp_get_category(category_id: int) -> Any:
        """Get one DigiKey category and its parametric-search context."""
        return get_category(category_id, _authorization()).public()

    @mcp.tool(name="get_associated_accounts", annotations=READ_ONLY)
    def mcp_get_associated_accounts() -> Any:
        """Get DigiKey Account IDs associated with the authenticated login."""
        return get_associated_accounts(_authorization()).public()

    @mcp.tool(name="analyze_bom", annotations=READ_ONLY, timeout=180)
    def mcp_analyze_bom(request: BulkBOMRequest) -> Any:
        """Analyze a BOM for cost, stock, lifecycle, lead time, compliance, PCNs, alternate packaging, and substitutes."""
        return analyze_bom(request, _authorization())

    @mcp.tool(name="optimize_bom_pricing", annotations=READ_ONLY, timeout=180)
    def mcp_optimize_bom_pricing(request: PricingOptimizationRequest) -> Any:
        """Optimize BOM pricing across package choices; unpriceable alternate packages are isolated in candidate diagnostics."""
        return optimize_bom_pricing(request, _authorization())

    @mcp.tool(name="audit_lifecycle", annotations=READ_ONLY, timeout=180)
    def mcp_audit_lifecycle(request: LifecycleAuditRequest) -> Any:
        """Audit a BOM or MyList for end-of-life risk, last-buy dates, shortages, long lead times, PCNs, and substitutes."""
        return lifecycle_audit(request, _authorization())

    @mcp.tool(name="list_mylists", annotations=READ_ONLY)
    def mcp_list_mylists(
        account_id: str | None = None,
        start_index: int = 0,
        limit: int = 50,
    ) -> Any:
        """List the authenticated user's DigiKey MyLists."""
        return list_my_lists(
            _authorization(),
            account_id=account_id,
            start_index=start_index,
            limit=limit,
        ).public()

    @mcp.tool(name="get_mylist", annotations=READ_ONLY)
    def mcp_get_mylist(list_id: str, account_id: str | None = None) -> Any:
        """Get one DigiKey MyList."""
        return get_my_list(list_id, _authorization(), account_id=account_id).public()

    @mcp.tool(name="get_mylist_parts", annotations=READ_ONLY)
    def mcp_get_mylist_parts(
        list_id: str,
        account_id: str | None = None,
        assemblies: int = 1,
        include_attrition: bool = False,
        response_detail: Literal["compact", "full"] = "compact",
        include_substitutions: bool = False,
        substitution_limit: int = 5,
        include_environmental_docs: bool = False,
        include_images: bool = False,
        include_empty_fields: bool = False,
    ) -> Any:
        """Get every MyList part; compact is bounded and default, while full preserves the raw DigiKey shape."""
        parts = get_all_list_parts(
            list_id,
            _authorization(),
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

    @mcp.tool(name="create_mylist", annotations=WRITE)
    def mcp_create_mylist(request: ConfirmedCreateListRequest) -> Any:
        """Create a DigiKey MyList after the user approves the exact name."""
        return create_my_list(
            request.list_name,
            _authorization(),
            tags=request.tags,
            created_by=request.created_by,
            account_id=request.account_id,
        ).public()

    @mcp.tool(name="rename_mylist", annotations=UPDATE)
    def mcp_rename_mylist(list_id: str, request: ConfirmedRenameListRequest) -> Any:
        """Rename a DigiKey MyList after the user approves the exact change."""
        return rename_my_list(
            list_id,
            request.new_name,
            _authorization(),
            account_id=request.account_id,
        ).public()

    @mcp.tool(name="delete_mylist", annotations=DESTRUCTIVE)
    def mcp_delete_mylist(
        list_id: str, confirm: Literal[True], account_id: str | None = None
    ) -> Any:
        """Permanently delete a DigiKey MyList after identifying it and obtaining explicit approval."""
        return delete_my_list(list_id, _authorization(), account_id=account_id).public()

    @mcp.tool(name="add_parts_to_mylist", annotations=WRITE)
    def mcp_add_parts_to_mylist(list_id: str, request: ConfirmedAddPartsRequest) -> Any:
        """Add approved products, packaging, and quantities to a DigiKey MyList."""
        return {
            "list_id": list_id,
            "responses": add_parts_to_list(
                list_id,
                request.parts,
                _authorization(),
                account_id=request.account_id,
                insertion_index=request.insertion_index,
            ),
        }

    @mcp.tool(name="update_mylist_part", annotations=UPDATE)
    def mcp_update_mylist_part(
        list_id: str,
        unique_id: str,
        request: ConfirmedUpdateListPartRequest,
    ) -> Any:
        """Update an approved MyList part while preserving every field omitted from the request."""
        return update_my_list_part(
            list_id,
            unique_id,
            request,
            _authorization(),
        ).public()

    @mcp.tool(name="remove_mylist_part", annotations=DESTRUCTIVE)
    def mcp_remove_mylist_part(
        list_id: str,
        unique_id: str,
        confirm: Literal[True],
        created_by: str = "",
        account_id: str | None = None,
    ) -> Any:
        """Permanently remove one identified part from a DigiKey MyList after explicit approval."""
        return remove_my_list_part(
            list_id,
            unique_id,
            _authorization(),
            created_by=created_by,
            account_id=account_id,
        ).public()

    @mcp.tool(name="diff_mylist", annotations=READ_ONLY, timeout=180)
    def mcp_diff_mylist(list_id: str, request: ListDiffRequest) -> Any:
        """Dry-run a proposed BOM against a MyList and show additions, updates, removals, duplicates, and unchanged rows."""
        return diff_my_list(list_id, request, _authorization())

    @mcp.tool(name="sync_mylist", annotations=DESTRUCTIVE, timeout=180)
    def mcp_sync_mylist(list_id: str, request: ListSyncRequest) -> Any:
        """Apply an explicitly approved MyList diff. The request must contain confirm=true."""
        return sync_my_list(list_id, request, _authorization())

    @mcp.tool(name="search_orders", annotations=READ_ONLY)
    def mcp_search_orders(
        start_date: str | None = None,
        end_date: str | None = None,
        shared: bool = False,
        page_number: int = 1,
        page_size: int = 10,
        account_id: str | None = None,
    ) -> Any:
        """Search the authenticated user's DigiKey orders by date."""
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
            _authorization(),
            account_id=account_id,
            params=params,
        ).public()

    @mcp.tool(name="get_sales_order", annotations=READ_ONLY)
    def mcp_get_sales_order(
        sales_order_id: int,
        account_id: str | None = None,
    ) -> Any:
        """Get shipment, tracking, backorder, and line-item status for one DigiKey sales order."""
        return client.request(
            "GET",
            f"/orderstatus/v4/salesorder/{sales_order_id}",
            _authorization(),
            account_id=account_id,
        ).public()

    @mcp.tool(name="list_quotes", annotations=READ_ONLY)
    def mcp_list_quotes(
        account_id: str | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> Any:
        """List DigiKey quotes for an account."""
        return list_quotes(
            _authorization(),
            account_id=account_id,
            offset=offset,
            limit=limit,
        ).public()

    @mcp.tool(name="get_quote", annotations=READ_ONLY)
    def mcp_get_quote(quote_id: int, account_id: str | None = None) -> Any:
        """Get one DigiKey quote's metadata."""
        return get_quote(quote_id, _authorization(), account_id=account_id).public()

    @mcp.tool(name="get_quote_products", annotations=READ_ONLY)
    def mcp_get_quote_products(
        quote_id: int,
        account_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Any:
        """Get the products and locked pricing in a DigiKey quote."""
        return get_quote_products(
            quote_id,
            _authorization(),
            account_id=account_id,
            offset=offset,
            limit=limit,
        ).public()

    @mcp.tool(name="create_quote", annotations=WRITE)
    def mcp_create_quote(request: ConfirmedCreateQuoteRequest) -> Any:
        """Create an empty DigiKey quote after explicit approval."""
        return create_quote(
            CreateQuoteRequest(quote_name=request.quote_name, account_id=request.account_id),
            _authorization(),
        ).public()

    @mcp.tool(name="add_products_to_quote", annotations=WRITE)
    def mcp_add_products_to_quote(
        quote_id: int, request: ConfirmedAddQuoteProductsRequest
    ) -> Any:
        """Add explicitly approved product quantities to a DigiKey quote."""
        products = [
            {
                "ProductNumber": item.product_number,
                "CustomerReference": item.customer_reference,
                "Quantities": item.quantities,
            }
            for item in request.products
        ]
        return {
            "quote_id": quote_id,
            "responses": add_products_to_quote(
                quote_id,
                products,
                _authorization(),
                account_id=request.account_id,
            ),
        }

    @mcp.tool(name="create_quote_from_source", annotations=WRITE, timeout=180)
    def mcp_create_quote_from_source(request: QuoteFromSourceRequest) -> Any:
        """Create and populate a quote from an explicitly approved BOM or MyList. The request must contain confirm=true."""
        return create_quote_from_source(request, _authorization())

    @mcp.tool(name="decode_barcode", annotations=READ_ONLY)
    def mcp_decode_barcode(request: DecodeBarcodeRequest) -> Any:
        """Decode a DigiKey product-bag or packing-list 1D or 2D barcode."""
        return decode_barcode(request, _authorization()).public()

    @mcp.tool(name="batch_decode_barcodes", annotations=READ_ONLY, timeout=180)
    def mcp_batch_decode_barcodes(request: BatchBarcodeRequest) -> Any:
        """Decode a batch of DigiKey barcodes and total received quantities by product number."""
        return batch_decode_barcodes(request.barcodes, _authorization())

    @mcp.tool(name="compare_barcodes_to_mylist", annotations=READ_ONLY, timeout=180)
    def mcp_compare_barcodes_to_mylist(request: BarcodeListComparisonRequest) -> Any:
        """Compare scanned received quantities against the required quantities in a DigiKey MyList."""
        return compare_barcodes_to_list(request, _authorization())

    @mcp.tool(name="lookup_packing_list", annotations=READ_ONLY)
    def mcp_lookup_packing_list(request: PackingListLookupRequest) -> Any:
        """Retrieve packing-list information by invoice, sales order, or purchase order number."""
        return lookup_packing_list(request, _authorization()).public()

    register_mouser_tools(
        mcp,
        principal=_principal,
        read_only=READ_ONLY,
        destructive=DESTRUCTIVE,
    )
    register_comparison_tools(
        mcp,
        authorization=_authorization,
        principal=_principal,
        read_only=READ_ONLY,
    )

    return mcp
