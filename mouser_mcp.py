from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from distributor_models import (
    ComponentComparisonRequest,
    ComponentRecommendationRequest,
    MouserCartExecuteRequest,
    MouserCartPreviewRequest,
    MouserOrderHistoryRequest,
    MouserOrderLookupRequest,
    MouserSearchRequest,
)
from mouser_services import (
    execute_mouser_cart_change,
    get_mouser_cart,
    get_mouser_order,
    preview_mouser_cart_change,
    search_mouser_order_history,
    search_mouser_products,
)
from multi_distributor import compare_component_offers, recommend_components


def register_mouser_tools(
    mcp: FastMCP,
    *,
    principal: Callable[[], str],
    read_only: dict[str, bool],
    destructive: dict[str, bool],
) -> None:
    @mcp.tool(name="search_mouser_products", annotations=read_only)
    def mcp_search_mouser_products(request: MouserSearchRequest) -> Any:
        """Search Mouser catalog data, availability, lifecycle, compliance, and price breaks. Shipping is unavailable."""
        return search_mouser_products(request, principal=principal())

    @mcp.tool(name="search_mouser_order_history", annotations=read_only)
    def mcp_search_mouser_order_history(
        request: MouserOrderHistoryRequest,
    ) -> Any:
        """Read the authenticated deployment's Mouser order history by filter or date range."""
        return search_mouser_order_history(request, principal=principal())

    @mcp.tool(name="get_mouser_order", annotations=read_only)
    def mcp_get_mouser_order(request: MouserOrderLookupRequest) -> Any:
        """Read one Mouser order by sales-order number or web-order number."""
        return get_mouser_order(request, principal=principal())

    @mcp.tool(name="get_mouser_cart", annotations=read_only)
    def mcp_get_mouser_cart(cart_key: str) -> Any:
        """Read one Mouser cart. This does not modify the cart."""
        return get_mouser_cart(cart_key, principal=principal())

    @mcp.tool(name="preview_mouser_cart_change", annotations=read_only)
    def mcp_preview_mouser_cart_change(
        request: MouserCartPreviewRequest,
    ) -> Any:
        """Read current cart state and preview an exact add, update, remove, replacement, order-copy, or schedule diff."""
        return preview_mouser_cart_change(request, principal=principal())

    @mcp.tool(name="execute_mouser_cart_change", annotations=destructive)
    def mcp_execute_mouser_cart_change(
        request: MouserCartExecuteRequest,
    ) -> Any:
        """Execute only the exact, unexpired Mouser cart preview bound to the supplied one-time token."""
        return execute_mouser_cart_change(request, principal=principal())


def register_comparison_tools(
    mcp: FastMCP,
    *,
    authorization: Callable[[], str],
    principal: Callable[[], str],
    read_only: dict[str, bool],
) -> None:
    @mcp.tool(name="compare_component_offers", annotations=read_only, timeout=180)
    def mcp_compare_component_offers(
        request: ComponentComparisonRequest,
    ) -> Any:
        """Compare strict manufacturer-plus-MPN matches at requested quantities across DigiKey and Mouser."""
        return compare_component_offers(
            request,
            principal=principal(),
            authorization=authorization(),
        )

    @mcp.tool(name="recommend_components", annotations=read_only, timeout=180)
    def mcp_recommend_components(
        request: ComponentRecommendationRequest,
    ) -> Any:
        """Find project candidates after critical requirements are known; return pass/fail/unknown evidence and a Pareto shortlist."""
        return recommend_components(
            request,
            principal=principal(),
            authorization=authorization(),
        )
