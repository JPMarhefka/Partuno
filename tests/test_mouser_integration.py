"""Opt-in live Mouser smoke tests.

Read tests require MOUSER_INTEGRATION_TESTS=true. Cart mutations additionally
require MOUSER_CART_WRITE_TESTS=true and an explicit part number. No test calls
the Mouser Order API or submits an order.
"""
from __future__ import annotations

import os

import pytest

from config import settings
from distributor_models import (
    MouserCartExecuteRequest,
    MouserCartItem,
    MouserCartOperation,
    MouserCartPreviewRequest,
    MouserOrderHistoryMode,
    MouserOrderHistoryRequest,
    MouserSearchRequest,
)
from mouser_services import (
    execute_mouser_cart_change,
    get_mouser_cart,
    preview_mouser_cart_change,
    search_mouser_order_history,
    search_mouser_products,
)


pytestmark = pytest.mark.skipif(
    os.getenv("MOUSER_INTEGRATION_TESTS") != "true",
    reason="set MOUSER_INTEGRATION_TESTS=true for live Mouser reads",
)


def test_live_mouser_catalog_search() -> None:
    if not settings.mouser_search_enabled:
        pytest.skip("MOUSER_SEARCH_API_KEY is required")
    result = search_mouser_products(
        MouserSearchRequest(query="LM358", records=1),
        principal="live-integration",
    )
    assert result["SearchResults"]["NumberOfResult"] >= 1
    assert result["normalized_offers"]


def test_live_mouser_order_history_read() -> None:
    if not settings.mouser_account_enabled:
        pytest.skip("MOUSER_ACCOUNT_API_KEY is required")
    result = search_mouser_order_history(
        MouserOrderHistoryRequest(
            mode=MouserOrderHistoryMode.date_filter,
            date_filter="All",
        ),
        principal="live-integration",
    )
    assert "OrderHistoryItems" in result


@pytest.mark.skipif(
    os.getenv("MOUSER_CART_WRITE_TESTS") != "true",
    reason="requires separate explicit approval via MOUSER_CART_WRITE_TESTS=true",
)
def test_live_disposable_cart_add_read_remove() -> None:
    if not settings.mouser_account_enabled:
        pytest.skip("MOUSER_ACCOUNT_API_KEY is required")
    part_number = os.getenv("MOUSER_CART_TEST_PART_NUMBER", "").strip()
    if not part_number:
        pytest.skip("MOUSER_CART_TEST_PART_NUMBER is required")

    principal = "live-cart-integration"
    preview = preview_mouser_cart_change(
        MouserCartPreviewRequest(
            operation=MouserCartOperation.add_items,
            items=[
                MouserCartItem(
                    mouser_part_number=part_number,
                    quantity=1,
                )
            ],
        ),
        principal=principal,
    )
    created = execute_mouser_cart_change(
        MouserCartExecuteRequest(
            confirmation_token=preview["confirmation_token"]
        ),
        principal=principal,
    )
    cart_key = created.get("CartKey")
    assert cart_key
    try:
        cart = get_mouser_cart(str(cart_key), principal=principal)
        assert any(
            item.get("MouserPartNumber") == part_number
            for item in cart.get("CartItems") or []
        )
    finally:
        cleanup_preview = preview_mouser_cart_change(
            MouserCartPreviewRequest(
                operation=MouserCartOperation.remove_item,
                cart_key=str(cart_key),
                mouser_part_number=part_number,
            ),
            principal=principal,
        )
        execute_mouser_cart_change(
            MouserCartExecuteRequest(
                confirmation_token=cleanup_preview["confirmation_token"]
            ),
            principal=principal,
        )
