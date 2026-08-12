"""Opt-in, read-only live smoke tests."""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("DIGIKEY_INTEGRATION_TESTS") != "true",
    reason="set DIGIKEY_INTEGRATION_TESTS=true to run live DigiKey integration tests",
)


@pytest.fixture
def integration_authorization() -> str:
    token = os.getenv("DIGIKEY_INTEGRATION_ACCESS_TOKEN", "")
    if not token:
        pytest.skip("DIGIKEY_INTEGRATION_ACCESS_TOKEN is required")
    return f"Bearer {token}"


def test_live_read_only_product_smoke(integration_authorization: str) -> None:
    from models import ProductSearchRequest
    from services import get_alternate_packaging, get_product_pricing, get_recommended_products, search_products

    result = search_products(ProductSearchRequest(keywords="LM358", limit=1), integration_authorization)
    products = result.data.get("Products", []) if isinstance(result.data, dict) else []
    if not products:
        pytest.skip("LM358 search returned no products")
    number = products[0].get("DigiKeyProductNumber")
    if not number:
        pytest.skip("search result did not include a DigiKey product number")
    get_product_pricing(str(number), integration_authorization)
    get_recommended_products(str(number), integration_authorization)
    get_alternate_packaging(str(number), integration_authorization)
