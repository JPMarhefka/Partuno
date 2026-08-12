import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

from client import DigiKeyHTTPError, DigiKeyResponse, error_envelope
from models import (
    BOMItem,
    BarcodeInput,
    BarcodeListComparisonRequest,
    BarcodeType,
    ConfirmedCreateQuoteRequest,
    ListPartInput,
    ListSyncRequest,
    PricingOptimizationRequest,
    ParametricFilter,
    ProductResourcesRequest,
    ProductSearchRequest,
    MarketPlaceFilter,
    SortOrder,
    TariffFilter,
)
import services


def test_product_research_enrichment_is_opt_in() -> None:
    request = ProductResourcesRequest(product_number="ABC-1")
    assert request.include_media is False
    assert request.include_substitutions is False
    assert request.include_change_notifications is False


def test_confirmed_quote_rejects_unapproved_request() -> None:
    try:
        ConfirmedCreateQuoteRequest(quote_name="test", account_id="1", confirm=False)
    except Exception:
        pass
    else:  # pragma: no cover - makes the expectation explicit without pytest helpers
        raise AssertionError("confirm=false must not validate")


def test_mcp_catalog_exposes_research_and_confirmed_writes() -> None:
    import app

    async def inspect_tools() -> None:
        tools = {tool.name: tool for tool in await app.mcp.list_tools()}
        assert "get_product_pricing" in tools
        assert "get_product_media" in tools
        assert "get_category" in tools
        assert "create_quote" in tools
        assert "search_mouser_products" in tools
        assert "compare_component_offers" in tools
        assert "recommend_components" in tools
        assert "preview_mouser_cart_change" in tools
        assert "execute_mouser_cart_change" in tools
        assert tools["search_products"].annotations.readOnlyHint is True
        assert tools["preview_mouser_cart_change"].annotations.readOnlyHint is True
        assert tools["execute_mouser_cart_change"].annotations.destructiveHint is True
        assert tools["get_mylist_parts"].parameters["properties"]["response_detail"]["default"] == "compact"
        assert tools["get_recommended_products"].parameters["properties"]["search_options"]["default"] is None
        delete_schema = tools["delete_mylist"].parameters
        assert delete_schema["properties"]["confirm"]["const"] is True

    asyncio.run(inspect_tools())


def test_health_reports_provider_capabilities_without_secret_values() -> None:
    from main import health

    result = health()
    assert result["providers"]["digikey"]["provider"] == "digikey"
    assert result["providers"]["mouser"]["provider"] == "mouser"
    serialized = json.dumps(result)
    assert "api_key" not in serialized.lower()
    assert "client_secret" not in serialized.lower()


def test_local_mcp_server_enumerates_tools_without_remote_oauth() -> None:
    from mcp_server import build_mcp_server

    local_server = build_mcp_server(local=True)
    assert local_server is not None

    async def inspect_tools() -> None:
        tools = await local_server.list_tools()
        names = {tool.name for tool in tools}
        assert "search_products" in names
        assert "search_mouser_products" in names
        assert "compare_component_offers" in names
        assert "recommend_components" in names

    asyncio.run(inspect_tools())


def test_local_cli_defaults_to_stdio_and_loopback_http() -> None:
    from partuno import build_parser

    args = build_parser().parse_args([])
    assert args.transport == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 8000


def test_public_metadata_uses_partuno_brand_and_version() -> None:
    from app import mcp_health
    from main import app

    schema = app.openapi()

    assert app.title == "Partuno"
    assert app.version == "4.0.1"
    assert schema["info"]["title"] == "Partuno"
    assert "Open-source MCP server" in schema["info"]["description"]
    assert mcp_health()["version"] == "4.0.1"


def test_openapi_operation_ids_are_unique_and_expose_no_ordering_api() -> None:
    from main import app

    schema = app.openapi()
    operations = [
        (path, method, operation)
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
        if isinstance(operation, dict) and operation.get("operationId")
    ]
    operation_ids = [operation["operationId"] for _, _, operation in operations]
    assert len(operation_ids) == 52
    assert len(operation_ids) == len(set(operation_ids))
    assert all(not path.lower().startswith("/ordering") for path, _, _ in operations)
    assert all(
        not operation_id.lower().startswith(("placeorder", "submitorder", "createorder"))
        for operation_id in operation_ids
    )
    assert {
        "searchMouserProducts",
        "compareComponentOffers",
        "recommendComponents",
        "searchMouserOrderHistory",
        "getMouserOrder",
        "getMouserCart",
        "previewMouserCartChange",
        "executeMouserCartChange",
    }.issubset(operation_ids)
    assert (
        schema["paths"]["/mouser/carts/execute"]["post"][
            "x-openai-isConsequential"
        ]
        is True
    )


def test_mcp_has_no_order_placement_and_write_tools_require_confirmation() -> None:
    import app

    def contains_confirm_true(schema: object) -> bool:
        if isinstance(schema, dict):
            if schema.get("const") is True:
                return True
            return any(contains_confirm_true(value) for value in schema.values())
        if isinstance(schema, list):
            return any(contains_confirm_true(value) for value in schema)
        return False

    async def inspect_tools() -> None:
        tools = {tool.name: tool for tool in await app.mcp.list_tools()}
        assert not {
            "place_order",
            "submit_order",
            "create_order",
            "add_order_products",
        }.intersection(tools)
        write_tools = {
            name: tool
            for name, tool in tools.items()
            if tool.annotations.readOnlyHint is False
        }
        assert write_tools
        for tool in write_tools.values():
            assert (
                contains_confirm_true(tool.parameters)
                or (
                    tool.name == "execute_mouser_cart_change"
                    and "confirmation_token"
                    in str(tool.parameters)
                )
            )

    asyncio.run(inspect_tools())


def test_keyword_search_builds_the_documented_v4_filter_payload() -> None:
    request = ProductSearchRequest(
        keywords="LM358",
        limit=5,
        offset=10,
        manufacturer_ids=["296"],
        category_ids=["687"],
        status_ids=["1"],
        packaging_ids=["2"],
        series_ids=["3"],
        marketplace_filter=MarketPlaceFilter.only,
        tariff_filter=TariffFilter.only,
        minimum_quantity_available=25,
        search_options=["InStock"],
        parametric_category_id="687",
        parametric_filters=[ParametricFilter(parameter_id=7, value_ids=["8"])],
        sort_field="Price",
        sort_order=SortOrder.descending,
    )

    assert services.build_keyword_body(request) == {
        "Keywords": "LM358",
        "Limit": 5,
        "Offset": 10,
        "FilterOptionsRequest": {
            "MarketPlaceFilter": "MarketPlaceOnly",
            "TariffFilter": "TariffOnly",
            "ManufacturerFilter": [{"Id": "296"}],
            "CategoryFilter": [{"Id": "687"}],
            "StatusFilter": [{"Id": "1"}],
            "PackagingFilter": [{"Id": "2"}],
            "SeriesFilter": [{"Id": "3"}],
            "MinimumQuantityAvailable": 25,
            "SearchOptions": ["InStock"],
            "ParameterFilterRequest": {
                "CategoryFilter": {"Id": "687"},
                "ParameterFilters": [{
                    "ParameterId": 7,
                    "FilterValues": [{"Id": "8"}],
                }],
            },
        },
        "SortOptions": {"Field": "Price", "SortOrder": "Descending"},
    }


def test_native_search_response_reports_native_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = {"Products": [{"DigiKeyProductNumber": "B", "ProductVariations": [{"TariffActive": True}]}], "ProductsCount": 1}
    captured: dict[str, object] = {}

    def request(*_args: object, **_kwargs: object) -> DigiKeyResponse:
        captured.update(_kwargs)
        return DigiKeyResponse(upstream, {"correlation_id": "search"})

    monkeypatch.setattr(services.client, "request", request)
    response = services.search_products(
        ProductSearchRequest(
            keywords="resistor", limit=1, tariff_filter=TariffFilter.only, search_options=[]
        ), "Bearer test"
    )
    assert response.data is upstream
    assert captured["json_body"]["FilterOptionsRequest"]["TariffFilter"] == "TariffOnly"
    assert response.meta["filter_enforcement"] == "native"
    assert response.meta["results_complete"] is True


def test_exclude_tariff_uses_a_local_conformance_fallback_when_upstream_mixes_variations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        services.client, "request",
        lambda *_args, **_kwargs: DigiKeyResponse({"Products": [{"ProductVariations": [
            {"DigiKeyProductNumber": "CT", "TariffActive": False, "QuantityAvailable": 2},
            {"DigiKeyProductNumber": "TR", "TariffActive": True, "QuantityAvailable": 8},
        ]}], "ProductsCount": 1}, {}),
    )
    response = services.search_products(
        ProductSearchRequest(keywords="part", tariff_filter=TariffFilter.exclude, search_options=[]), "Bearer test"
    )
    assert len(response.data["Products"][0]["ProductVariations"]) == 1
    assert response.data["Products"][0]["ProductVariations"][0]["DigiKeyProductNumber"] == "CT"
    assert response.meta["filter_enforcement"] == "local_fallback"


def test_search_fallback_rebuilds_filtered_offset_across_native_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def product(number: str, tariff: bool) -> dict[str, object]:
        return {"DigiKeyProductNumber": number, "ProductVariations": [{
            "DigiKeyProductNumber": number, "TariffActive": tariff, "QuantityAvailableforPackageType": 4,
        }]}

    first_page = [product("GOOD-0", True), *[product(f"BAD-{n}", False) for n in range(49)]]
    second_page = [product("GOOD-1", True)]

    def request(*_args: object, **kwargs: object) -> DigiKeyResponse:
        body = kwargs["json_body"]
        offset = body["Offset"]
        calls.append(offset)
        if offset == 1:
            return DigiKeyResponse({"Products": [product("BAD-1", False)], "ProductsCount": 51}, {})
        if offset == 0:
            return DigiKeyResponse({"Products": first_page, "ProductsCount": 51}, {})
        return DigiKeyResponse({"Products": second_page, "ProductsCount": 51}, {})

    monkeypatch.setattr(services.client, "request", request)
    response = services.search_products(
        ProductSearchRequest(keywords="LM358", limit=1, offset=1, tariff_filter=TariffFilter.only, search_options=[]),
        "Bearer test",
    )
    assert calls == [1, 0, 50]
    assert response.data["Products"][0]["DigiKeyProductNumber"] == "GOOD-1"
    assert response.data["ProductsCount"] == 2
    assert response.meta["removed_variation_count"] == 49
    assert response.meta["results_complete"] is True


def test_search_fallback_stops_when_requested_window_is_filled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    good = {"DigiKeyProductNumber": "GOOD", "ProductVariations": [{"TariffActive": True}]}
    bad = {"DigiKeyProductNumber": "BAD", "ProductVariations": [{"TariffActive": False}]}

    def request(*_args: object, **kwargs: object) -> DigiKeyResponse:
        offset = kwargs["json_body"]["Offset"]
        calls.append(offset)
        if len(calls) == 1:
            return DigiKeyResponse({"Products": [bad], "ProductsCount": 100}, {})
        return DigiKeyResponse(
            {"Products": [good, *[bad for _ in range(49)]], "ProductsCount": 100},
            {"rate_limit_remaining": "100"},
        )

    monkeypatch.setattr(services.client, "request", request)
    response = services.search_products(
        ProductSearchRequest(keywords="LM358", limit=1, tariff_filter=TariffFilter.only),
        "Bearer test",
    )
    assert calls == [0, 0]
    assert response.data["Products"] == [good]
    assert response.meta["results_complete"] is True
    assert response.meta["fallback_reason"] == "requested_window_filled"


def test_search_fallback_reports_incomplete_when_safety_cap_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services,
        "settings",
        replace(services.settings, search_fallback_max_pages=1),
    )
    bad = {"DigiKeyProductNumber": "BAD", "ProductVariations": [{"TariffActive": False}]}
    page = [bad for _ in range(50)]
    monkeypatch.setattr(
        services.client,
        "request",
        lambda *_args, **_kwargs: DigiKeyResponse(
            {"Products": page, "ProductsCount": 100},
            {"rate_limit_remaining": "100"},
        ),
    )
    response = services.search_products(
        ProductSearchRequest(keywords="LM358", limit=5, tariff_filter=TariffFilter.only),
        "Bearer test",
    )
    assert response.data["Products"] == []
    assert response.meta["source_page_count"] == 1
    assert response.meta["results_complete"] is False
    assert response.meta["fallback_reason"] == "safety_cap"


def test_search_fallback_reports_complete_when_upstream_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = {"DigiKeyProductNumber": "BAD", "ProductVariations": [{"TariffActive": False}]}
    monkeypatch.setattr(
        services.client,
        "request",
        lambda *_args, **_kwargs: DigiKeyResponse(
            {"Products": [bad], "ProductsCount": 1},
            {"rate_limit_remaining": "100"},
        ),
    )
    response = services.search_products(
        ProductSearchRequest(keywords="LM358", limit=5, tariff_filter=TariffFilter.only),
        "Bearer test",
    )
    assert response.meta["results_complete"] is True
    assert response.meta["fallback_reason"] == "upstream_exhausted"


def test_marketplace_conformance_fallback_removes_nonmatching_exact_variations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        services.client,
        "request",
        lambda *_args, **_kwargs: DigiKeyResponse({"Products": [{"ProductVariations": [
            {"DigiKeyProductNumber": "DIRECT", "IsMarketplace": False},
            {"DigiKeyProductNumber": "MARKET", "IsMarketplace": True},
        ]}], "ProductsCount": 1}, {}),
    )
    response = services.search_products(ProductSearchRequest(keywords="LM358", search_options=[]), "Bearer test")
    assert response.data["Products"][0]["ProductVariations"] == [{"DigiKeyProductNumber": "DIRECT", "IsMarketplace": False}]
    assert response.meta["filter_enforcement"] == "local_fallback"


def test_marketplace_conformance_fallback_filters_exact_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        services.client,
        "request",
        lambda *_args, **_kwargs: DigiKeyResponse({
            "Products": [],
            "ExactMatches": [
                {"DigiKeyProductNumber": "DIRECT", "IsMarketplace": False},
                {"DigiKeyProductNumber": "MARKET", "IsMarketplace": True},
            ],
            "ProductsCount": 0,
        }, {}),
    )
    response = services.search_products(ProductSearchRequest(keywords="LM358", search_options=[]), "Bearer test")
    assert response.data["ExactMatches"] == [{"DigiKeyProductNumber": "DIRECT", "IsMarketplace": False}]
    assert response.meta["filter_enforcement"] == "local_fallback"


def test_substitutions_are_locally_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    substitutes = [
        {"id": number, "ProductVariations": [{"DigiKeyProductNumber": f"SUB-{number}"}]}
        for number in range(367)
    ]

    def request(*_args: object, **kwargs: object) -> DigiKeyResponse:
        seen.update(kwargs)
        return DigiKeyResponse({"ProductSubstitutes": substitutes}, {})

    monkeypatch.setattr(services.client, "request", request)
    response = services.get_substitutions("ABC", "Bearer test", limit=2)
    assert seen["params"] == {"limit": 2}
    assert response.data["ProductSubstitutes"] == substitutes[:2]
    assert response.data["ProductSubstitutes"][0]["ProductVariations"] == [
        {"DigiKeyProductNumber": "SUB-0"}
    ]
    assert response.data["total_available"] == 367
    assert response.data["returned_count"] == 2
    assert response.data["requested_limit"] == 2
    assert response.data["limit_enforced_locally"] is True
    assert response.meta["requested_limit"] == 2
    assert response.meta["limit_enforced_locally"] is True


def test_recommendations_omit_search_options_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def request(*_args: object, **kwargs: object) -> DigiKeyResponse:
        calls.append(kwargs)
        return DigiKeyResponse({"Recommendations": []}, {"correlation_id": "only"})

    monkeypatch.setattr(services.client, "request", request)
    response = services.get_recommended_products("ABC", "Bearer test", limit=10)
    assert calls == [{
        "params": {"limit": 10, "excludeMarketPlaceProducts": True},
        "safe_retry": False,
    }]
    assert response.data == {"Recommendations": []}
    assert response.meta["requested_limit"] == 10
    assert response.meta["limit_semantics"] == "upstream_recommendation_records"
    assert response.meta["nested_recommended_products_truncated"] is False


@pytest.mark.parametrize(
    "product_number",
    ["1528-1830-ND", "LM358DR2GOSCT-ND", "LM358DR2GOSTR-ND"],
)
def test_recommendation_limit_preserves_nested_upstream_products(
    monkeypatch: pytest.MonkeyPatch,
    product_number: str,
) -> None:
    payload = {
        "Recommendations": [{
            "ProductNumber": product_number,
            "RecommendedProducts": [
                {"DigiKeyProductNumber": "REC-1"},
                {"DigiKeyProductNumber": "REC-2"},
            ],
        }]
    }
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        services.client,
        "request",
        lambda *_args, **kwargs: (
            calls.append(kwargs), DigiKeyResponse(payload, {})
        )[1],
    )

    response = services.get_recommended_products(product_number, "Bearer test", limit=1)

    assert calls[0]["params"] == {"limit": 1, "excludeMarketPlaceProducts": True}
    assert response.data == payload
    assert len(response.data["Recommendations"][0]["RecommendedProducts"]) == 2
    assert response.meta["limit_semantics"] == "upstream_recommendation_records"
    assert response.meta["nested_recommended_products_truncated"] is False


@pytest.mark.parametrize("search_options", ["", [], None])
def test_recommendations_omit_empty_search_options(
    monkeypatch: pytest.MonkeyPatch,
    search_options: object,
) -> None:
    calls: list[dict[str, object]] = []

    def request(*_args: object, **kwargs: object) -> DigiKeyResponse:
        calls.append(kwargs["params"])
        return DigiKeyResponse({"Recommendations": []}, {})

    monkeypatch.setattr(services.client, "request", request)
    services.get_recommended_products("ABC", "Bearer test", search_options=search_options)
    assert "searchOptionList" not in calls[0]


@pytest.mark.parametrize("status_code", [404, 500])
def test_recommendations_retry_once_without_explicit_filters(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    calls: list[dict[str, object]] = []

    def request(*_args: object, **kwargs: object) -> DigiKeyResponse:
        calls.append(kwargs["params"])
        if len(calls) == 1:
            raise DigiKeyHTTPError(
                status_code,
                {"message": "filters rejected"},
                {"http_status": status_code, "correlation_id": "first", "rate_limit_remaining": "9"},
            )
        return DigiKeyResponse(
            {"Recommendations": [{"ProductNumber": "ABC"}]},
            {"http_status": 200, "correlation_id": "second", "rate_limit_remaining": "8"},
        )

    monkeypatch.setattr(services.client, "request", request)
    response = services.get_recommended_products(
        "ABC", "Bearer test", search_options=["InStock", "RoHSCompliant"]
    )

    assert calls[0]["searchOptionList"] == "InStock,RoHSCompliant"
    assert "searchOptionList" not in calls[1]
    assert calls[0]["excludeMarketPlaceProducts"] is True
    assert calls[1]["excludeMarketPlaceProducts"] is True
    assert response.data["warnings"][0]["code"] == "recommendation_filters_rejected"
    assert response.meta["compatibility"]["fallback_used"] is True
    assert [attempt["correlation_id"] for attempt in response.meta["compatibility"]["attempts"]] == ["first", "second"]


@pytest.mark.parametrize("status_code", [400, 401, 403, 429, 503])
def test_recommendations_do_not_remove_filters_for_other_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    calls = 0

    def request(*_args: object, **_kwargs: object) -> DigiKeyResponse:
        nonlocal calls
        calls += 1
        raise DigiKeyHTTPError(status_code, {"message": "failed"}, {"correlation_id": "only"})

    monkeypatch.setattr(services.client, "request", request)
    with pytest.raises(DigiKeyHTTPError):
        services.get_recommended_products("ABC", "Bearer test", search_options="InStock")
    assert calls == 1


def test_recommendations_without_filters_do_not_use_compatibility_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def request(*_args: object, **_kwargs: object) -> DigiKeyResponse:
        nonlocal calls
        calls += 1
        raise DigiKeyHTTPError(500, {"message": "upstream"}, {"correlation_id": "only"})

    monkeypatch.setattr(services.client, "request", request)
    with pytest.raises(DigiKeyHTTPError):
        services.get_recommended_products("ABC", "Bearer test")
    assert calls == 1


def test_recommendations_preserve_both_failed_filter_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def request(*_args: object, **_kwargs: object) -> DigiKeyResponse:
        nonlocal calls
        calls += 1
        raise DigiKeyHTTPError(500, {"detail": "upstream"}, {"http_status": 500, "correlation_id": f"c{calls}"})

    monkeypatch.setattr(services.client, "request", request)
    with pytest.raises(DigiKeyHTTPError) as raised:
        services.get_recommended_products(
            "1528-1830-ND", "Bearer test", limit=1, search_options="InStock"
        )
    assert calls == 2
    assert raised.value.status_code == 500
    assert raised.value.meta["compatibility"]["fallback_used"] is True
    assert [attempt["correlation_id"] for attempt in raised.value.meta["compatibility"]["attempts"]] == ["c1", "c2"]


def test_research_bundle_keeps_successes_and_records_recommendation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        services, "get_product_details",
        lambda *_args, **_kwargs: DigiKeyResponse({"Product": {"DigiKeyProductNumber": "ABC"}}, {}),
    )
    monkeypatch.setattr(
        services, "get_recommended_products",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DigiKeyHTTPError(500, {"error_type": "digikey_upstream_error"}, {"correlation_id": "retry"})),
    )
    result = services.product_research_bundle(
        ProductResourcesRequest(product_number="ABC", include_recommended=True), "Bearer test"
    )
    assert "details" in result["results"]
    assert result["status"] == "partial"
    assert result["errors"]["recommended"]["success"] is False
    assert result["errors"]["recommended"]["error"]["correlation_id"] == "retry"


def test_research_bundle_preserves_daily_pcn_limit_as_structured_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services,
        "get_product_details",
        lambda *_args, **_kwargs: DigiKeyResponse(
            {"Product": {"DigiKeyProductNumber": "ABC"}},
            {},
        ),
    )
    monkeypatch.setattr(
        services,
        "get_product_change_notifications",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DigiKeyHTTPError(
                429,
                {
                    "message": "Daily request limit exceeded",
                    "error_type": "digikey_daily_rate_limit",
                    "retryable": False,
                },
                {
                    "attempts": 1,
                    "correlation_id": "pcn-daily",
                    "rate_limit": "30",
                    "rate_limit_remaining": "0",
                    "rate_limit_reset": "next-day",
                    "retry_after": "3600",
                    "retryable": False,
                    "rate_limit_scope": "daily",
                },
            )
        ),
    )

    result = services.product_research_bundle(
        ProductResourcesRequest(
            product_number="ABC",
            include_change_notifications=True,
        ),
        "Bearer test",
    )

    assert result["status"] == "partial"
    error = result["errors"]["change_notifications"]["error"]
    assert error["provider"] == "digikey"
    assert error["retryable"] is False
    assert error["attempts"] == 1
    assert error["correlation_id"] == "pcn-daily"
    assert error["rate_limit"] == {
        "limit": "30",
        "remaining": "0",
        "reset": "next-day",
        "retry_after": "3600",
        "scope": "daily",
        "retry_stopped_reason": None,
    }


def test_research_bundle_status_is_success_when_every_section_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services, "get_product_details",
        lambda *_args, **_kwargs: DigiKeyResponse({"Product": {"DigiKeyProductNumber": "ABC"}}, {}),
    )
    result = services.product_research_bundle(
        ProductResourcesRequest(product_number="ABC"), "Bearer test"
    )
    assert result["status"] == "success"
    assert result["errors"] == {}


def test_research_bundle_status_is_failed_when_no_section_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services, "get_product_details",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DigiKeyHTTPError(500, {"message": "failed"}, {"correlation_id": "cid"})
        ),
    )
    result = services.product_research_bundle(
        ProductResourcesRequest(product_number="ABC"), "Bearer test"
    )
    assert result["status"] == "failed"
    assert result["results"] == {}


def test_manufacturers_are_locally_paginated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        services.client, "request",
        lambda *_args, **_kwargs: DigiKeyResponse({"Manufacturers": [{"id": n} for n in range(4)]}, {}),
    )
    response = services.get_manufacturers("Bearer test", offset=1, limit=2)
    assert response.data["Manufacturers"] == [{"id": 1}, {"id": 2}]
    assert response.data["total_count"] == 4


def test_bom_optimizer_considers_cheaper_alternate_packaging(monkeypatch: pytest.MonkeyPatch) -> None:
    def pricing(number: str, *_args: object, **_kwargs: object) -> DigiKeyResponse:
        total = 10 if number == "ORIGINAL" else 5
        return DigiKeyResponse({"StandardPricingOptions": [{"Products": [{
            "DigiKeyProductNumber": number, "Quantity": 10, "TotalPrice": total,
            "PackageType": "CutTape",
        }]}]}, {})

    monkeypatch.setattr(services, "get_pricing_by_quantity", pricing)
    monkeypatch.setattr(
        services, "get_alternate_packaging",
        lambda *_args, **_kwargs: DigiKeyResponse({"AlternatePackaging": [{"DigiKeyProductNumber": "ALTERNATE"}]}, {}),
    )
    monkeypatch.setattr(
        services, "get_product_details",
        lambda number, *_args, **_kwargs: DigiKeyResponse({"Product": {"DigiKeyProductNumber": number, "ProductStatus": "Active"}}, {}),
    )
    result = services.optimize_one_item(
        BOMItem(product_number="ORIGINAL", quantity=10), "Bearer test",
        PricingOptimizationRequest(items=[BOMItem(product_number="ORIGINAL", quantity=10)], include_digireel=False),
    )
    assert result["pricing_decision"]["recommendation"]["digi_key_part_number"] == "ALTERNATE"
    assert result["pricing_decision"]["recommendation"]["source"] == "AlternatePackaging"


def test_bom_optimizer_isolates_an_unpriceable_alternate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        services, "get_pricing_by_quantity",
        lambda *_args, **_kwargs: DigiKeyResponse({"StandardPricingOptions": [{"Products": [{
            "DigiKeyProductNumber": "ORIGINAL", "Quantity": 10, "TotalPrice": 10, "PackageType": "CutTape",
        }]}]}, {}),
    )
    monkeypatch.setattr(
        services, "get_alternate_packaging",
        lambda *_args, **_kwargs: DigiKeyResponse({"AlternatePackaging": [{"DigiKeyProductNumber": "296-1014-5-ND"}]}, {}),
    )
    attempts = 0

    def details(*_args: object, **_kwargs: object) -> DigiKeyResponse:
        nonlocal attempts
        attempts += 1
        raise DigiKeyHTTPError(500, {"message": "NullReferenceException"}, {"correlation_id": str(attempts)})

    monkeypatch.setattr(services, "get_product_details", details)
    result = services.optimize_one_item(
        BOMItem(product_number="ORIGINAL", quantity=10), "Bearer test",
        PricingOptimizationRequest(items=[BOMItem(product_number="ORIGINAL", quantity=10)], include_digireel=False),
    )
    assert attempts == 2
    assert result["pricing_decision"]["recommendation"]["digi_key_part_number"] == "ORIGINAL"
    assert result["alternate_packaging_errors"][0]["product_number"] == "296-1014-5-ND"
    assert result["alternate_packaging_errors"][0]["attempts"][0]["correlation_id"] == "1"


def test_mylist_diff_matches_requested_and_digikey_aliases() -> None:
    existing = [{
        "UniqueId": "one", "RequestedPartNumber": "LM358DR2G", "DigiKeyPartNumber": "LM358DR2GOSCT-ND",
        "Quantities": [{"SelectedPackType": "CutTape", "Quantity": 10, "TargetPrice": 0}],
        "SelectedQuantityIndex": 0,
    }]
    diff = services.build_list_diff(
        existing, [ListPartInput(product_number="LM358DR2GOSCT-ND", quantity=10)],
        remove_unlisted=False, consolidate_duplicates=True,
    )
    assert diff["summary"] == {"additions": 0, "updates": 0, "removals": 0, "unchanged": 1, "ambiguities": 0}


def _large_mylist_part_fixture() -> dict[str, object]:
    return {
        "UniqueId": "part-1",
        "RequestedPartNumber": "LM358DR2G",
        "DigiKeyPartNumber": "LM358DR2GOSCT-ND",
        "ManufacturerPartNumber": "LM358DR2G",
        "ManufacturerName": "onsemi",
        "Description": "Dual operational amplifier",
        "CustomerReference": "AMP",
        "ReferenceDesignator": "U1",
        "Notes": "prototype",
        "SelectedQuantityIndex": 0,
        "Quantities": [{
            "Quantity": 10,
            "SelectedPackType": "CutTape",
            "PackOptions": [{
                "DigiKeyPartNumber": "LM358DR2GOSCT-ND",
                "PackageType": "CutTape",
                "MinimumOrderQuantity": 1,
                "QuantityAvailable": 1000,
                "UnitPrice": 0.25,
            }],
        }],
        "ProductStatus": "Active",
        "ManufacturerLeadWeeks": 12,
        "TariffCode": 0,
        "IsMarketplace": False,
        "Substitutions": [
            {"DigiKeyPartNumber": f"SUB-{index}", "Description": "replacement" * 20}
            for index in range(20)
        ],
        "EnvironmentalDocuments": [{"Name": "RoHS", "Body": "x" * 1000}],
        "Image": {"Url": "https://example.test/image.jpg", "Metadata": "x" * 1000},
        "EmptyArray": [],
    }


def test_mylist_compact_default_returns_essential_bounded_fields() -> None:
    raw = [_large_mylist_part_fixture()]
    compact = services.present_mylist_parts(raw)
    part = compact["parts"][0]

    assert part["unique_id"] == "part-1"
    assert part["selected_quantity"] == 10
    assert part["selected_package"] == "CutTape"
    assert part["pack_options"][0]["effective_moq"] == 1
    assert part["substitutions_total"] == 20
    assert part["substitutions_returned"] == 0
    assert part["substitutions_truncated"] is True
    assert "substitutions" not in part
    assert "EnvironmentalDocuments" not in part
    assert "Image" not in part
    assert compact["_meta"]["response_detail"] == "compact"
    assert compact["_meta"]["omitted_nested_fields"]["substitutions"] == 20


def test_mylist_compact_substitution_opt_in_honors_limit() -> None:
    compact = services.present_mylist_parts(
        [_large_mylist_part_fixture()],
        include_substitutions=True,
        substitution_limit=3,
    )
    part = compact["parts"][0]
    assert len(part["substitutions"]) == 3
    assert part["substitutions_returned"] == 3
    assert part["substitutions_truncated"] is True


def test_mylist_full_detail_preserves_existing_shape() -> None:
    raw = [_large_mylist_part_fixture()]
    assert services.present_mylist_parts(raw, response_detail="full", list_id="list") == {
        "list_id": "list",
        "total_parts": 1,
        "parts": raw,
    }


def test_mylist_compact_payload_is_materially_smaller() -> None:
    raw = [_large_mylist_part_fixture()]
    full_size = len(json.dumps(services.present_mylist_parts(raw, response_detail="full")))
    compact_size = len(json.dumps(services.present_mylist_parts(raw)))
    assert compact_size < full_size * 0.4


@pytest.mark.parametrize(
    ("visibility", "can_edit", "expected_access", "warning"),
    [
        ("readOnly", True, "editable", True),
        ("readOnly", False, "read_only", False),
        ("Private", True, "editable", False),
        ("Unknown", False, "unknown", False),
    ],
)
def test_mylist_access_normalization(
    visibility: str,
    can_edit: bool,
    expected_access: str,
    warning: bool,
) -> None:
    raw = {"ListSettings": {"Visibility": visibility}, "CanEdit": can_edit}
    normalized = services.normalize_mylist_access(raw)
    assert normalized["ListSettings"]["Visibility"] == visibility
    assert normalized["CanEdit"] is can_edit
    assert normalized["raw_visibility"] == visibility
    assert normalized["effective_access"] == expected_access
    assert bool(normalized["access_warnings"]) is warning


def test_pricing_option_uses_parent_availability_and_null_when_missing() -> None:
    options = services.flatten_pricing_options({"StandardPricingOptions": [{
        "QuantityAvailable": 42,
        "Products": [{"DigiKeyProductNumber": "A", "Quantity": 10, "TotalPrice": 1, "UnitPrice": .1}],
    }, {"Products": [{"DigiKeyProductNumber": "B", "Quantity": 10, "TotalPrice": 1, "UnitPrice": .1}]}]}, 10)
    assert options[0]["quantity_available"] == 42
    assert options[1]["quantity_available"] is None


def test_variation_availability_is_attached_without_replacing_option_availability() -> None:
    decision = {"eligible_options": [{"digi_key_part_number": "LM358DR2GOSCT-ND", "quantity_available": 42}], "rejected_options": []}
    services.attach_variation_availability(decision, {"LM358DR2GOSCT-ND": 12})
    assert decision["eligible_options"][0]["quantity_available"] == 42
    assert decision["eligible_options"][0]["variation_quantity_available"] == 12


@pytest.mark.parametrize(
    ("variation", "product_standard_package", "expected_value", "expected_source"),
    [
        ({"MinimumOrderQuantity": 5}, 100, 5, "variation_minimum_order_quantity"),
        (
            {
                "MinimumOrderQuantity": 0,
                "StandardPricing": [
                    {"BreakQuantity": 5000},
                    {"BreakQuantity": 2500},
                ],
            },
            100,
            2500,
            "first_price_break",
        ),
        (
            {"MinimumOrderQuantity": -1, "StandardPackage": 100},
            2500,
            100,
            "variation_standard_package",
        ),
        ({}, 2500, 2500, "product_standard_package"),
        ({"MinimumOrderQuantity": "bad", "StandardPackage": 0}, None, None, "unknown"),
    ],
)
def test_effective_moq_precedence(
    variation: dict[str, object],
    product_standard_package: int | None,
    expected_value: int | None,
    expected_source: str,
) -> None:
    normalized = services.normalize_effective_moq(
        variation, product_standard_package=product_standard_package
    )
    assert normalized == {
        "raw_minimum_order_quantity": variation.get("MinimumOrderQuantity"),
        "effective_minimum_order_quantity": expected_value,
        "effective_moq_source": expected_source,
    }


def test_lm358_tape_and_reel_effective_moq_uses_first_price_break() -> None:
    normalized = services.normalize_effective_moq({
        "DigiKeyProductNumber": "LM358DR2GOSTR-ND",
        "MinimumOrderQuantity": 0,
        "StandardPackage": 2500,
        "StandardPricing": [
            {"BreakQuantity": 2500, "UnitPrice": 0.10},
            {"BreakQuantity": 5000, "UnitPrice": 0.08},
        ],
    })
    assert normalized["raw_minimum_order_quantity"] == 0
    assert normalized["effective_minimum_order_quantity"] == 2500
    assert normalized["effective_moq_source"] == "first_price_break"


def test_pricing_payload_adds_moq_fields_without_mutating_raw_input() -> None:
    payload = {
        "StandardPackage": 2500,
        "ProductVariations": [{
            "DigiKeyProductNumber": "TR",
            "MinimumOrderQuantity": 0,
            "StandardPricing": [{"BreakQuantity": 2500}],
        }],
    }
    normalized = services.normalize_product_pricing_payload(payload)
    variation = normalized["ProductVariations"][0]
    assert payload["ProductVariations"][0] == {
        "DigiKeyProductNumber": "TR",
        "MinimumOrderQuantity": 0,
        "StandardPricing": [{"BreakQuantity": 2500}],
    }
    assert variation["raw_minimum_order_quantity"] == 0
    assert variation["effective_minimum_order_quantity"] == 2500
    assert variation["effective_moq_source"] == "first_price_break"


def test_pricing_selection_rejects_quantity_below_effective_moq() -> None:
    payload = {"StandardPricingOptions": [{
        "Products": [{
            "DigiKeyProductNumber": "TR",
            "Quantity": 10,
            "TotalPrice": 1,
            "MinimumOrderQuantity": 0,
            "StandardPricing": [{"BreakQuantity": 2500}],
        }],
    }]}
    decision = services.choose_pricing_option(
        payload,
        10,
        allow_marketplace=True,
        allow_tariff=True,
        allow_quantity_increase=True,
    )
    assert decision["eligible_options"] == []
    assert decision["rejected_options"][0]["rejection_reasons"] == ["below_effective_moq"]


def test_mylist_sync_aborts_before_writes_when_alias_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = [
        {"UniqueId": "one", "RequestedPartNumber": "SHARED", "DigiKeyPartNumber": "DK-ONE", "ManufacturerPartNumber": "M1", "Quantities": []},
        {"UniqueId": "two", "RequestedPartNumber": "SHARED", "DigiKeyPartNumber": "DK-TWO", "ManufacturerPartNumber": "M2", "Quantities": []},
    ]
    monkeypatch.setattr(services, "get_all_list_parts", lambda *_args, **_kwargs: existing)
    monkeypatch.setattr(services, "add_parts_to_list", lambda *_args, **_kwargs: pytest.fail("must not write"))
    result = services.sync_my_list(
        "list", ListSyncRequest(proposed_items=[ListPartInput(product_number="SHARED", quantity=1)], confirm=True),
        "Bearer test",
    )
    assert result["complete"] is False
    assert result["applied"] == {"additions": [], "updates": [], "removals": []}
    assert result["diff"]["ambiguities"]


def test_deleted_mylist_cleanup_does_not_require_a_follow_up_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        services.client, "request",
        lambda method, path, *_args, **_kwargs: (calls.append(f"{method}:{path}"), DigiKeyResponse({"success": True}, {}))[1],
    )
    services.delete_my_list("gone", "Bearer test")
    assert calls == ["DELETE:/mylists/v1/lists/gone"]


def test_list_specific_403_is_classified_without_rewriting_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services.client,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DigiKeyHTTPError(
                403,
                {"message": "forbidden"},
                {"correlation_id": "cid", "http_status": 403},
            )
        ),
    )
    with pytest.raises(DigiKeyHTTPError) as raised:
        services.get_my_list("gone", "Bearer test")
    assert raised.value.status_code == 403
    assert raised.value.detail == {
        "category": "authorization_or_absence",
        "resource_state": "deleted_or_inaccessible",
        "upstream_status": 403,
        "list_id": "gone",
        "upstream_problem": {"message": "forbidden"},
    }
    assert raised.value.meta["correlation_id"] == "cid"
    envelope = error_envelope(
        raised.value.status_code, raised.value.detail, raised.value.meta
    )
    assert envelope["error"]["category"] == "authorization_or_absence"


def test_unrelated_403_keeps_original_authorization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services.client,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DigiKeyHTTPError(403, {"message": "forbidden"}, {"correlation_id": "cid"})
        ),
    )
    with pytest.raises(DigiKeyHTTPError) as raised:
        services.get_product_details("ABC", "Bearer test")
    assert raised.value.detail == {"message": "forbidden"}


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Mult MSL1 Pkg Rev 20/Dec/2018", "2018-12-20"),
        ("Tape Design Chg 25/Oct/2019", "2019-10-25"),
        ("Alternative packaging 02/AUG/2024", "2024-08-02"),
        ("LM324/LM358/LM393 08/Feb/2023", "2023-02-08"),
        ("Published 2024-07-02", "2024-07-02"),
        ("No embedded date", None),
    ],
)
def test_parse_pcn_description_date(description: str, expected: str | None) -> None:
    assert services.parse_pcn_description_date(description) == expected


def test_pcn_normalization_preserves_raw_dates_and_adds_mismatch_warning() -> None:
    payload = {"ProductChangeNotifications": [{
        "PcnChangeDate": "2026-07-15T19:22:00Z",
        "PcnDescription": "Tape Design Chg 25/Oct/2019",
    }]}
    normalized = services.normalize_pcn_payload(payload)
    item = normalized["ProductChangeNotifications"][0]
    assert payload["ProductChangeNotifications"][0] == {
        "PcnChangeDate": "2026-07-15T19:22:00Z",
        "PcnDescription": "Tape Design Chg 25/Oct/2019",
    }
    assert item["PcnChangeDate"] == "2026-07-15T19:22:00Z"
    assert item["api_change_date"] == "2026-07-15T19:22:00Z"
    assert item["description_date"] == "2019-10-25"
    assert item["date_mismatch_days"] > 30
    assert "differs materially" in item["date_warning"]


def test_pcn_parse_failure_does_not_fail_or_invent_a_date() -> None:
    item = services.normalize_pcn_payload({
        "ProductChangeNotifications": [{
            "PcnChangeDate": "not-a-date",
            "PcnDescription": "No useful date",
        }]
    })["ProductChangeNotifications"][0]
    assert item["api_change_date"] == "not-a-date"
    assert item["description_date"] is None
    assert item["date_mismatch_days"] is None
    assert item["date_warning"] is None


def test_pcn_success_is_cached_briefly_without_sharing_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def request(*_args: object, **_kwargs: object) -> DigiKeyResponse:
        nonlocal calls
        calls += 1
        return DigiKeyResponse(
            {
                "ProductChangeNotifications": [
                    {
                        "PcnChangeDate": "2026-07-01",
                        "PcnDescription": "Effective 01/Jul/2026",
                    }
                ]
            },
            {"correlation_id": "pcn-cid"},
        )

    monkeypatch.setattr(services.client, "request", request)
    services._pcn_cache.clear()

    first = services.get_product_change_notifications(
        "ABC",
        "Bearer principal-one",
    )
    first.data["ProductChangeNotifications"][0]["PcnDescription"] = "mutated"
    second = services.get_product_change_notifications(
        "ABC",
        "Bearer principal-one",
    )

    assert calls == 1
    assert first.meta["cache"]["hit"] is False
    assert second.meta["cache"]["hit"] is True
    assert second.meta["correlation_id"] == "pcn-cid"
    assert (
        second.data["ProductChangeNotifications"][0]["PcnDescription"]
        == "Effective 01/Jul/2026"
    )


def test_pcn_cache_is_scoped_by_token_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def request(*_args: object, **_kwargs: object) -> DigiKeyResponse:
        nonlocal calls
        calls += 1
        return DigiKeyResponse({"ProductChangeNotifications": []}, {})

    monkeypatch.setattr(services.client, "request", request)
    services._pcn_cache.clear()

    services.get_product_change_notifications("ABC", "Bearer one")
    services.get_product_change_notifications("ABC", "Bearer two")

    assert calls == 2


def test_client_error_metadata_uses_body_correlation_id() -> None:
    class Response:
        status_code = 500
        headers: dict[str, str] = {}

    meta = services.client._rate_meta(Response(), 1, {"correlationId": "body-id"})
    assert meta["correlation_id"] == "body-id"


def test_error_envelope_includes_upstream_diagnostics() -> None:
    payload = error_envelope(400, {"message": "bad barcode"}, {"attempts": 1, "correlation_id": "cid", "rate_limit_remaining": "9"})
    assert payload["success"] is False
    assert payload["error"]["status_code"] == 400
    assert payload["error"]["correlation_id"] == "cid"
    assert payload["error"]["rate_limit"]["remaining"] == "9"


def test_batch_decode_keeps_invalid_barcode_as_a_structured_partial_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = BarcodeInput(barcode_type=BarcodeType.product_1d, barcode="not-a-barcode")
    upstream = DigiKeyHTTPError(
        400,
        {"message": "invalid barcode"},
        {"attempts": 1, "correlation_id": "barcode-cid", "rate_limit_remaining": "8"},
    )
    monkeypatch.setattr(
        services,
        "decode_barcode",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(upstream),
    )

    result = services.batch_decode_barcodes([item], "Bearer test")

    assert result["summary"] == {"decoded": 0, "failed": 1, "unique_parts": 0}
    error = result["errors"][0]["error"]
    assert error["success"] is False
    assert error["error"]["status_code"] == 400
    assert error["error"]["correlation_id"] == "barcode-cid"


def test_compare_barcodes_to_mylist_returns_partial_results_for_invalid_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = BarcodeInput(barcode_type=BarcodeType.product_1d, barcode="not-a-barcode")
    request = BarcodeListComparisonRequest(barcodes=[item], list_id="list-1")
    monkeypatch.setattr(
        services,
        "decode_barcode",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DigiKeyHTTPError(400, {"message": "invalid barcode"}, {"correlation_id": "barcode-cid"})
        ),
    )
    monkeypatch.setattr(
        services,
        "get_all_list_parts",
        lambda *_args, **_kwargs: [{
            "DigiKeyPartNumber": "ABC-ND",
            "Quantities": [{"Quantity": 2, "SelectedPackType": "CutTape"}],
        }],
    )

    result = services.compare_barcodes_to_list(request, "Bearer test")

    assert result["status"] == "partial"
    assert result["warnings"][0]["code"] == "barcode_decode_failed"
    assert result["comparison"] == [{
        "product_number": "ABC-ND",
        "required_quantity": 2,
        "received_quantity": 0,
        "difference": -2,
        "status": "unknown_due_to_decode_failure",
    }]
    assert result["summary"] == {"complete": 0, "short": 0, "unknown": 1, "extra": 0}
    assert result["barcode_decode"]["summary"]["failed"] == 1
    assert result["barcode_decode"]["errors"][0]["error"]["error"]["correlation_id"] == "barcode-cid"


def test_compare_barcodes_to_mylist_keeps_confirmed_lines_with_failed_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = BarcodeInput(barcode_type=BarcodeType.product_1d, barcode="valid")
    invalid = BarcodeInput(barcode_type=BarcodeType.product_1d, barcode="invalid")
    request = BarcodeListComparisonRequest(barcodes=[valid, invalid], list_id="list-1")

    def decode(item: BarcodeInput, *_args: object, **_kwargs: object) -> DigiKeyResponse:
        if item.barcode == "invalid":
            raise DigiKeyHTTPError(400, {"message": "invalid barcode"}, {})
        return DigiKeyResponse({"DigiKeyProductNumber": "ABC-ND", "Quantity": 2}, {})

    monkeypatch.setattr(services, "decode_barcode", decode)
    monkeypatch.setattr(
        services,
        "get_all_list_parts",
        lambda *_args, **_kwargs: [
            {"DigiKeyPartNumber": "ABC-ND", "Quantities": [{"Quantity": 3}]},
            {"DigiKeyPartNumber": "XYZ-ND", "Quantities": [{"Quantity": 3}]},
        ],
    )

    result = services.compare_barcodes_to_list(request, "Bearer test")

    assert result["status"] == "partial"
    assert result["summary"] == {"complete": 0, "short": 1, "unknown": 1, "extra": 0}
    assert [row["status"] for row in result["comparison"]] == [
        "short", "unknown_due_to_decode_failure",
    ]


def test_compare_barcodes_to_mylist_preserves_normal_semantics_for_valid_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = BarcodeInput(barcode_type=BarcodeType.product_1d, barcode="valid")
    request = BarcodeListComparisonRequest(barcodes=[item], list_id="list-1")
    monkeypatch.setattr(
        services,
        "decode_barcode",
        lambda *_args, **_kwargs: DigiKeyResponse(
            {"DigiKeyProductNumber": "ABC-ND", "Quantity": 2}, {}
        ),
    )
    monkeypatch.setattr(
        services,
        "get_all_list_parts",
        lambda *_args, **_kwargs: [
            {"DigiKeyPartNumber": "ABC-ND", "Quantities": [{"Quantity": 2}]}
        ],
    )

    result = services.compare_barcodes_to_list(request, "Bearer test")

    assert result["status"] == "success"
    assert result["warnings"] == []
    assert result["summary"] == {"complete": 1, "short": 0, "unknown": 0, "extra": 0}
    assert result["comparison"][0]["status"] == "complete"


def test_rest_error_envelope_keeps_digikey_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    monkeypatch.setattr(
        main, "search_products",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DigiKeyHTTPError(400, {"message": "invalid barcode", "correlationId": "body"}, {"correlation_id": "header", "attempts": 1})
        ),
    )
    response = TestClient(main.app).post(
        "/products/search",
        headers={"Authorization": "Bearer test"},
        json={"keywords": "LM358"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["error"]["correlation_id"] == "header"
    assert body["error"]["message"] == "invalid barcode"


def test_rest_validation_error_uses_the_same_envelope() -> None:
    import main

    response = TestClient(main.app).post("/products/search", json={"keywords": ""})
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["category"] == "validation"
    assert body["error"]["type"] == "request_validation_error"


def test_mcp_error_envelope_keeps_digikey_diagnostics() -> None:
    from mcp_server import DigiKeyErrorMiddleware

    async def fail(_: object) -> None:
        raise DigiKeyHTTPError(500, {"message": "upstream"}, {"correlation_id": "cid"})

    async def verify() -> None:
        result = await DigiKeyErrorMiddleware().on_call_tool(None, fail)
        assert result.is_error is True
        payload = result.structured_content
        assert payload["success"] is False
        assert payload["error"]["correlation_id"] == "cid"

    asyncio.run(verify())


def test_mcp_error_middleware_unwraps_fastmcp_tool_errors() -> None:
    from fastmcp import FastMCP
    from mcp_server import DigiKeyErrorMiddleware

    mcp = FastMCP("test", mask_error_details=True)
    mcp.add_middleware(DigiKeyErrorMiddleware())

    @mcp.tool
    def unavailable() -> None:
        raise DigiKeyHTTPError(
            404,
            {"message": "quote not found"},
            {"attempts": 1, "correlation_id": "quote-cid", "rate_limit_remaining": "7"},
        )

    async def verify() -> None:
        result = await mcp.call_tool("unavailable")
        assert result.is_error is True
        payload = result.structured_content
        assert payload["error"]["status_code"] == 404
        assert payload["error"]["message"] == "quote not found"
        assert payload["error"]["correlation_id"] == "quote-cid"
        assert payload["error"]["rate_limit"]["remaining"] == "7"
        wire_result = result.to_mcp_result()
        assert wire_result.isError is True
        assert wire_result.structuredContent == payload
        assert json.loads(wire_result.content[0].text) == payload

    asyncio.run(verify())


@pytest.mark.parametrize(
    ("scenario", "status_code"),
    [
        ("invalid_barcode", 400),
        ("fake_packing_list", 400),
        ("missing_quote", 404),
        ("missing_order", 404),
        ("missing_mylist", 404),
    ],
)
def test_mcp_structured_errors_preserve_upstream_status(
    scenario: str,
    status_code: int,
) -> None:
    from mcp_server import DigiKeyErrorMiddleware

    async def fail(_: object) -> None:
        raise DigiKeyHTTPError(
            status_code,
            {"message": scenario, "correlationId": "body-correlation"},
            {
                "attempts": 1,
                "correlation_id": "body-correlation",
                "rate_limit": "1000",
                "rate_limit_remaining": "9",
                "rate_limit_reset": "60",
                "retry_after": "2",
            },
        )

    async def verify() -> None:
        result = await DigiKeyErrorMiddleware().on_call_tool(None, fail)
        assert result.is_error is True
        assert result.structured_content["error"]["status_code"] == status_code
        assert result.structured_content["error"]["provider"] == "digikey"
        assert result.structured_content["error"]["type"] == "digikey_error"
        assert result.structured_content["error"]["retryable"] is False
        assert result.structured_content["error"]["attempts"] == 1
        assert result.structured_content["error"]["correlation_id"] == "body-correlation"
        assert result.structured_content["error"]["rate_limit"] == {
            "limit": "1000",
            "remaining": "9",
            "reset": "60",
            "retry_after": "2",
            "scope": None,
            "retry_stopped_reason": None,
        }
        assert result.structured_content["error"]["rate_limit"]["remaining"] == "9"

    asyncio.run(verify())
