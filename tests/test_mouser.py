from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from config import settings
from client import DigiKeyResponse
from distributor_models import (
    ComponentComparisonRequest,
    ComponentRecommendationRequest,
    ComponentRequest,
    ComponentRequirement,
    DistributorOffer,
    MouserCartExecuteRequest,
    MouserCartItem,
    MouserCartOperation,
    MouserCartPreviewRequest,
    MouserOrderHistoryMode,
    MouserOrderHistoryRequest,
    MouserOrderLookupRequest,
    MouserScheduleItem,
    MouserScheduledRelease,
    MouserSearchRequest,
    RequirementOperator,
    SourceResult,
)
from distributors import EnvironmentCredentialProvider
from mouser_client import (
    MouserClient,
    MouserHTTPError,
    MouserRateLimiter,
    MouserResponse,
)
import mouser_services
import multi_distributor
from normalization import (
    component_identity,
    effective_purchase_quantity,
    evaluate_requirement,
    normalize_mpn,
    select_price_break,
)


class FakeCredentialProvider:
    def get(self, **_kwargs: Any) -> str:
        return "secret-mouser-key"


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.content = b"payload"
        self.text = str(body)
        self.reason = "fake"

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, *_args: Any, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def make_mouser_client(
    responses: list[FakeResponse],
) -> tuple[MouserClient, FakeSession]:
    config = replace(
        settings,
        safe_retry_attempts=2,
        retry_backoff_seconds=0.1,
        max_retry_after_seconds=1,
        mouser_minute_limit=100,
        mouser_daily_limit=1000,
    )
    result = MouserClient(config, FakeCredentialProvider())
    session = FakeSession(responses)
    result._local.session = session
    return result, session


def test_mouser_configuration_is_optional_and_capability_specific() -> None:
    config = replace(
        settings,
        mouser_search_api_key="search",
        mouser_account_api_key=None,
    )
    assert config.mouser_search_enabled is True
    assert config.mouser_account_enabled is False
    provider = EnvironmentCredentialProvider(config)
    assert provider.get(
        principal="one",
        distributor="mouser",
        purpose=mouser_services.CredentialPurpose.catalog,
    ) == "search"


def test_mouser_user_agent_identifies_partuno() -> None:
    result = MouserClient(settings)

    assert result._session().headers["User-Agent"] == "Partuno-MCP/4.0"


def test_mouser_client_adds_key_without_returning_it() -> None:
    result, session = make_mouser_client(
        [FakeResponse(200, {"Errors": [], "SearchResults": {"Parts": []}})]
    )
    response = result.request(
        "POST",
        "/api/v1/search/keyword",
        principal="principal",
        purpose=mouser_services.CredentialPurpose.catalog,
        json_body={"query": "LM358"},
        safe_retry=True,
    )
    assert response.data["SearchResults"]["Parts"] == []
    assert session.calls[0]["params"]["apiKey"] == "secret-mouser-key"
    assert "secret-mouser-key" not in str(response.public())


def test_mouser_http_200_with_errors_is_a_failure() -> None:
    result, _ = make_mouser_client(
        [
            FakeResponse(
                200,
                {
                    "Errors": [
                        {"Code": "BadKey", "Message": "Authorization denied"}
                    ]
                },
            )
        ]
    )
    with pytest.raises(MouserHTTPError) as raised:
        result.request(
            "POST",
            "/api/v1/search/keyword",
            principal="principal",
            purpose=mouser_services.CredentialPurpose.catalog,
            safe_retry=True,
        )
    assert raised.value.status_code == 422
    assert raised.value.detail["errors"][0]["Code"] == "BadKey"


def test_mouser_correlation_header_takes_precedence_over_body() -> None:
    result, _ = make_mouser_client(
        [
            FakeResponse(
                200,
                {"Errors": [], "requestId": "body-id"},
                headers={"X-Correlation-Id": "header-id"},
            )
        ]
    )
    response = result.request(
        "GET",
        "/api/v1/orderhistory/ByDateFilter",
        principal="principal",
        purpose=mouser_services.CredentialPurpose.account,
        safe_retry=True,
    )
    assert response.meta["correlation_id"] == "header-id"


def test_mouser_correlation_reads_common_nested_body_field() -> None:
    result, _ = make_mouser_client(
        [
            FakeResponse(
                200,
                {"Errors": [], "Response": {"Request_ID": "body-id"}},
            )
        ]
    )
    response = result.request(
        "GET",
        "/api/v1/orderhistory/ByDateFilter",
        principal="principal",
        purpose=mouser_services.CredentialPurpose.account,
        safe_retry=True,
    )
    assert response.meta["correlation_id"] == "body-id"


def test_mouser_correlation_remains_null_when_upstream_provides_none() -> None:
    result, _ = make_mouser_client(
        [FakeResponse(200, {"Errors": [], "result": {"ok": True}})]
    )
    response = result.request(
        "GET",
        "/api/v1/orderhistory/ByDateFilter",
        principal="principal",
        purpose=mouser_services.CredentialPurpose.account,
        safe_retry=True,
    )
    assert response.meta["correlation_id"] is None


def test_mouser_cart_write_is_never_retried() -> None:
    result, session = make_mouser_client(
        [
            FakeResponse(503, {"Message": "try later"}),
            FakeResponse(200, {"unexpected": True}),
        ]
    )
    with pytest.raises(MouserHTTPError):
        result.request(
            "POST",
            "/api/v1/cart/items/insert",
            principal="principal",
            purpose=mouser_services.CredentialPurpose.account,
            safe_retry=False,
        )
    assert len(session.calls) == 1


def test_mouser_local_minute_budget_fails_closed() -> None:
    limiter = MouserRateLimiter(minute_limit=1, daily_limit=10)
    limiter.acquire()
    with pytest.raises(MouserHTTPError) as raised:
        limiter.acquire()
    assert raised.value.status_code == 429
    assert raised.value.detail["error_type"] == "local_rate_limit"


def test_mouser_sanitizer_redacts_query_and_mapping_keys() -> None:
    assert (
        MouserClient.sanitize(
            "https://api.mouser.com/test?apiKey=super-secret&x=1"
        )
        == "https://api.mouser.com/test?apiKey=[REDACTED]&x=1"
    )
    assert MouserClient.sanitize({"apiKey": "secret"}) == {
        "apiKey": "[REDACTED]"
    }


def test_mouser_keyword_search_uses_documented_v1_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def execute(method: str, path: str, **kwargs: Any) -> MouserResponse:
        captured.update({"method": method, "path": path, **kwargs})
        return MouserResponse(
            {"SearchResults": {"NumberOfResult": 0, "Parts": []}}, {}
        )

    monkeypatch.setattr(mouser_services.client, "request", execute)
    mouser_services.search_mouser_products(
        MouserSearchRequest(
            query="op amp",
            records=12,
            starting_record=24,
            in_stock=True,
            rohs=True,
        ),
        principal="principal",
    )
    assert captured["path"] == "/api/v1/search/keyword"
    assert captured["safe_retry"] is True
    assert captured["json_body"] == {
        "SearchByKeywordRequest": {
            "keyword": "op amp",
            "records": 12,
            "startingRecord": 24,
            "searchOptions": "RohsAndInStock",
            "searchWithYourSignUpLanguage": "false",
            "mouserPaysCustomsAndDuties": False,
        }
    }


def test_mouser_manufacturer_search_uses_documented_v2_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def execute(method: str, path: str, **kwargs: Any) -> MouserResponse:
        captured.update({"method": method, "path": path, **kwargs})
        return MouserResponse(
            {"SearchResults": {"NumberOfResult": 0, "Parts": []}}, {}
        )

    monkeypatch.setattr(mouser_services.client, "request", execute)
    mouser_services.search_mouser_products(
        MouserSearchRequest(
            query="LM358",
            manufacturer="Texas Instruments",
            records=10,
            starting_record=20,
        ),
        principal="principal",
    )
    assert captured["path"] == "/api/v2/search/keywordandmanufacturer"
    assert captured["json_body"]["SearchByKeywordMfrNameRequest"][
        "pageNumber"
    ] == 3


def test_mouser_order_history_and_lookup_are_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def execute(method: str, path: str, **kwargs: Any) -> MouserResponse:
        calls.append({"method": method, "path": path, **kwargs})
        return MouserResponse({"Errors": []}, {})

    monkeypatch.setattr(mouser_services.client, "request", execute)
    mouser_services.search_mouser_order_history(
        MouserOrderHistoryRequest(
            mode=MouserOrderHistoryMode.date_range,
            start_date="07/01/2026",
            end_date="07/31/2026",
        ),
        principal="principal",
    )
    mouser_services.get_mouser_order(
        MouserOrderLookupRequest(sales_order_number="SO-1"),
        principal="principal",
    )
    assert calls[0]["path"] == "/api/v1/orderhistory/ByDateRange"
    assert calls[0]["safe_retry"] is True
    assert calls[1]["path"] == "/api/v1/orderhistory/salesOrderNumber"
    assert all(call["method"] == "GET" for call in calls)


def test_strict_identity_preserves_mpn_punctuation_and_manufacturer() -> None:
    first = component_identity("Texas Instruments", "LM-358 DR")
    second = component_identity("Texas Instruments", "LM358-DR")
    third = component_identity("Other", "LM-358 DR")
    assert first.canonical_key != second.canonical_key
    assert first.canonical_key != third.canonical_key
    assert normalize_mpn("  Ab-C  ") == "ab-c"


def test_effective_quantity_and_price_break_use_decimal_math() -> None:
    quantity = effective_purchase_quantity(11, 10, 5)
    assert quantity == 15
    price, currency = select_price_break(
        [
            {"Quantity": 1, "Price": "$1.25", "Currency": "USD"},
            {"Quantity": 10, "Price": "$0.95", "Currency": "USD"},
        ],
        quantity,
    )
    assert price == Decimal("0.95")
    assert currency == "USD"


def test_requirement_evaluation_converts_compatible_units() -> None:
    evidence = evaluate_requirement(
        ComponentRequirement(
            name="Supply Voltage",
            operator=RequirementOperator.gte,
            value=3.3,
            unit="V",
        ),
        {"Supply Voltage": "5000 mV"},
    )
    assert evidence.status == "meets"
    assert evidence.normalized_value == "5"
    assert evidence.normalized_unit == "v"
    equality = evaluate_requirement(
        ComponentRequirement(
            name="Supply Voltage",
            operator=RequirementOperator.eq,
            value=5,
            unit="V",
        ),
        {"Supply Voltage": "5000 mV"},
    )
    assert equality.status == "meets"


def test_requirement_evaluation_never_guesses_missing_or_range_values() -> None:
    requirement = ComponentRequirement(
        name="Bandwidth",
        operator=RequirementOperator.gte,
        value=1,
        unit="MHz",
    )
    assert evaluate_requirement(requirement, {}).status == "unknown"
    assert (
        evaluate_requirement(
            requirement, {"Bandwidth": "1 MHz to 5 MHz"}
        ).status
        == "unknown"
    )


def test_provider_requirement_aliases_normalize_voltage_span_and_rohs() -> None:
    attributes = multi_distributor._digikey_attributes(
        {
            "Parameters": [
                {
                    "ParameterText": "Voltage - Supply Span (Min/Max)",
                    "ValueText": "3 to 32",
                    "ValueUnit": "V",
                }
            ],
            "Classifications": {"RoHSStatus": "RoHS Compliant"},
        }
    )

    minimum = evaluate_requirement(
        ComponentRequirement(
            name="Supply Voltage - Min",
            operator=RequirementOperator.lte,
            value=3.3,
            unit="V",
        ),
        attributes,
    )
    maximum = evaluate_requirement(
        ComponentRequirement(
            name="Supply Voltage - Max",
            operator=RequirementOperator.gte,
            value=3.3,
            unit="V",
        ),
        attributes,
    )
    rohs = evaluate_requirement(
        ComponentRequirement(
            name="RoHS Compliant",
            operator=RequirementOperator.boolean,
            value=True,
        ),
        attributes,
    )

    assert attributes["Supply Voltage - Min"] == "3 V"
    assert attributes["Supply Voltage - Max"] == "32 V"
    assert minimum.status == "meets"
    assert maximum.status == "meets"
    assert rohs.status == "meets"


def test_provider_requirement_aliases_normalize_separate_voltage_span_and_rohs3() -> None:
    attributes = multi_distributor._digikey_attributes(
        {
            "Parameters": [
                {
                    "ParameterText": "Voltage - Supply Span (Min)",
                    "ValueText": "3",
                    "ValueUnit": "V",
                },
                {
                    "ParameterText": "Voltage - Supply Span (Max)",
                    "ValueText": "32",
                    "ValueUnit": "V",
                },
                {
                    "ParameterText": "ROHS3 Compliant",
                    "ValueText": "ROHS3 Compliant",
                },
            ]
        }
    )

    minimum = evaluate_requirement(
        ComponentRequirement(
            name="Supply Voltage - Min",
            operator=RequirementOperator.lte,
            value=3.3,
            unit="V",
        ),
        attributes,
    )
    maximum = evaluate_requirement(
        ComponentRequirement(
            name="Supply Voltage - Max",
            operator=RequirementOperator.gte,
            value=3.3,
            unit="V",
        ),
        attributes,
    )
    rohs = evaluate_requirement(
        ComponentRequirement(
            name="RoHS Compliant",
            operator=RequirementOperator.boolean,
            value=True,
        ),
        attributes,
    )
    negative_rohs = evaluate_requirement(
        ComponentRequirement(
            name="RoHS Compliant",
            operator=RequirementOperator.boolean,
            value=True,
        ),
        {"RoHS Compliant": "ROHS3 Non-Compliant"},
    )

    assert attributes["Supply Voltage - Min"] == "3 V"
    assert attributes["Supply Voltage - Max"] == "32 V"
    assert attributes["RoHS Compliant"] == "ROHS3 Compliant"
    assert minimum.status == "meets"
    assert maximum.status == "meets"
    assert rohs.status == "meets"
    assert rohs.normalized_value == "true"
    assert negative_rohs.status == "does_not_meet"


def offer(
    provider: str,
    *,
    total: str = "1.0000",
    currency: str = "USD",
) -> DistributorOffer:
    return DistributorOffer(
        distributor=provider,
        identity=component_identity("Maker", "PART-1"),
        distributor_part_number=f"{provider}-1",
        requested_quantity=10,
        purchasable_quantity=10,
        unit_price="0.1000",
        merchandise_total=total,
        currency=currency,
        quantity_available=100,
        requested_quantity_in_stock=True,
        duty_assumption="test",
        observed_at="2026-01-01T00:00:00+00:00",
    )


def test_comparison_partial_response_never_declares_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def source(provider: str, *_args: Any, **_kwargs: Any) -> SourceResult:
        if provider == "digikey":
            return SourceResult(
                provider="digikey", status="success", results=[offer("digikey")]
            )
        return SourceResult(
            provider="mouser",
            status="failed",
            warnings=[
                {
                    "code": "provider_rate_limited",
                    "retry_after": None,
                }
            ],
            error={
                "message": "quota",
                "provider": "mouser",
                "retryable": False,
            },
        )

    monkeypatch.setattr(multi_distributor, "_run_provider_exact", source)
    result = multi_distributor.compare_component_offers(
        ComponentComparisonRequest(
            items=[
                ComponentRequest(
                    manufacturer="Maker",
                    manufacturer_part_number="PART-1",
                    quantity=10,
                )
            ]
        ),
        principal="principal",
        authorization="Bearer test",
    )
    assert result["status"] == "partial"
    assert result["comparisons"][0]["best_offer"] is None
    assert result["comparisons"][0]["coverage_complete"] is False
    assert result["sources"]["mouser"]["error"]["provider"] == "mouser"
    assert (
        result["sources"]["mouser"]["warnings"][0]["code"]
        == "provider_rate_limited"
    )


def test_comparison_requires_usd_for_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def source(provider: str, *_args: Any, **_kwargs: Any) -> SourceResult:
        return SourceResult(
            provider=provider,
            status="success",
            results=[
                offer(provider, currency="EUR" if provider == "mouser" else "USD")
            ],
        )

    monkeypatch.setattr(multi_distributor, "_run_provider_exact", source)
    result = multi_distributor.compare_component_offers(
        ComponentComparisonRequest(
            items=[
                ComponentRequest(
                    manufacturer="Maker",
                    manufacturer_part_number="PART-1",
                    quantity=10,
                )
            ]
        ),
        principal="principal",
        authorization="Bearer test",
    )
    assert result["status"] == "partial"
    assert result["comparisons"][0]["best_offer"] is None


def test_comparison_keeps_two_quantities_for_same_part_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def source(provider: str, *_args: Any, **_kwargs: Any) -> SourceResult:
        first = offer(provider)
        second = first.model_copy(
            update={
                "requested_quantity": 20,
                "purchasable_quantity": 20,
                "merchandise_total": "1.5000",
            }
        )
        return SourceResult(
            provider=provider,
            status="success",
            results=[first, second],
        )

    monkeypatch.setattr(multi_distributor, "_run_provider_exact", source)
    result = multi_distributor.compare_component_offers(
        ComponentComparisonRequest(
            items=[
                ComponentRequest(
                    manufacturer="Maker",
                    manufacturer_part_number="PART-1",
                    quantity=10,
                ),
                ComponentRequest(
                    manufacturer="Maker",
                    manufacturer_part_number="PART-1",
                    quantity=20,
                ),
            ]
        ),
        principal="principal",
        authorization="Bearer test",
    )
    assert {
        comparison["requested"]["quantity"]: {
            item["requested_quantity"]
            for offers in comparison["offers"].values()
            for item in offers
        }
        for comparison in result["comparisons"]
    } == {10: {10}, 20: {20}}


def test_provider_with_mixed_success_and_error_is_structured_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exact_offers(
        _manufacturer: str,
        manufacturer_part_number: str,
        _quantity: int,
        **_kwargs: Any,
    ) -> list[DistributorOffer]:
        if manufacturer_part_number == "FAIL":
            raise multi_distributor.DigiKeyHTTPError(
                429,
                {
                    "message": "Daily request limit exceeded",
                    "error_type": "digikey_daily_rate_limit",
                    "retryable": False,
                },
                {
                    "attempts": 1,
                    "correlation_id": "pcn-daily",
                    "rate_limit_remaining": "0",
                    "retryable": False,
                },
            )
        return [offer("digikey")]

    monkeypatch.setattr(
        multi_distributor.digikey_adapter,
        "exact_offers",
        exact_offers,
    )
    source = multi_distributor._run_provider_exact(
        "digikey",
        ComponentComparisonRequest(
            items=[
                ComponentRequest(
                    manufacturer="Maker",
                    manufacturer_part_number="PART-1",
                    quantity=10,
                ),
                ComponentRequest(
                    manufacturer="Maker",
                    manufacturer_part_number="FAIL",
                    quantity=10,
                ),
            ]
        ),
        principal="principal",
        authorization="Bearer test",
    )

    assert source.status == "partial"
    assert len(source.results) == 1
    failure = source.warnings[0]["error"]["error"]
    assert failure["provider"] == "digikey"
    assert failure["retryable"] is False
    assert failure["attempts"] == 1
    assert failure["correlation_id"] == "pcn-daily"


def test_region_restricted_offer_makes_comparison_partial_without_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unavailable = mouser_services.normalize_mouser_offer(
        {
            "ActualMfrName": "Maker",
            "ManufacturerPartNumber": "PART-1",
            "MouserPartNumber": None,
            "PriceBreaks": [],
            "AvailabilityInStock": None,
        },
        10,
    )

    def source(provider: str, *_args: Any, **_kwargs: Any) -> SourceResult:
        return SourceResult(
            provider=provider,
            status="success",
            results=[offer("digikey")] if provider == "digikey" else [unavailable],
        )

    monkeypatch.setattr(multi_distributor, "_run_provider_exact", source)
    result = multi_distributor.compare_component_offers(
        ComponentComparisonRequest(
            items=[
                ComponentRequest(
                    manufacturer="Maker",
                    manufacturer_part_number="PART-1",
                    quantity=10,
                )
            ]
        ),
        principal="principal",
        authorization="Bearer test",
    )

    assert result["status"] == "partial"
    assert result["comparisons"][0]["best_offer"] is None
    assert result["comparisons"][0]["coverage_complete"] is False
    assert result["unmatched"][0]["unavailable_from"] == ["mouser"]
    mouser_offer = result["unmatched"][0]["offers"]["mouser"][0]
    assert mouser_offer["availability_status"] == "regional_unavailable"
    assert mouser_offer["distributor_part_number"] is None


def test_cart_preview_model_rejects_missing_operation_fields() -> None:
    with pytest.raises(ValueError, match="requires cart_key"):
        MouserCartPreviewRequest(
            operation=MouserCartOperation.replace_cart,
            items=[MouserCartItem(mouser_part_number="1-ABC", quantity=1)],
        )


def test_replace_preview_explicitly_lists_omitted_removals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        "CartKey": "cart",
        "CartItems": [
            {"MouserPartNumber": "KEEP", "Quantity": 1},
            {"MouserPartNumber": "REMOVE", "Quantity": 2},
        ],
        "_meta": {},
    }
    monkeypatch.setattr(
        mouser_services,
        "get_mouser_cart",
        lambda *_args, **_kwargs: current,
    )
    monkeypatch.setattr(
        mouser_services,
        "cart_confirmations",
        mouser_services.CartConfirmationStore(600),
    )
    result = mouser_services.preview_mouser_cart_change(
        MouserCartPreviewRequest(
            operation=MouserCartOperation.replace_cart,
            cart_key="cart",
            items=[MouserCartItem(mouser_part_number="KEEP", quantity=1)],
        ),
        principal="principal",
    )
    assert [
        item["MouserPartNumber"] for item in result["diff"]["removals"]
    ] == ["REMOVE"]


def test_cart_token_is_one_time_and_bound_to_unchanged_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        "CartKey": "cart",
        "CartItems": [{"MouserPartNumber": "PART", "Quantity": 1}],
        "_meta": {},
    }
    monkeypatch.setattr(
        mouser_services,
        "get_mouser_cart",
        lambda *_args, **_kwargs: current,
    )
    monkeypatch.setattr(
        mouser_services,
        "cart_confirmations",
        mouser_services.CartConfirmationStore(600),
    )
    monkeypatch.setattr(
        mouser_services,
        "_execute_cart_request",
        lambda *_args, **_kwargs: MouserResponse(
            {"CartKey": "cart", "CartItems": []}, {}
        ),
    )
    preview = mouser_services.preview_mouser_cart_change(
        MouserCartPreviewRequest(
            operation=MouserCartOperation.remove_item,
            cart_key="cart",
            mouser_part_number="PART",
        ),
        principal="principal",
    )
    request = MouserCartExecuteRequest(
        confirmation_token=preview["confirmation_token"]
    )
    result = mouser_services.execute_mouser_cart_change(
        request, principal="principal"
    )
    assert result["status"] == "success"
    with pytest.raises(ValueError, match="already been used"):
        mouser_services.execute_mouser_cart_change(
            request, principal="principal"
        )


def test_cart_token_rejects_stale_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "value": {
            "CartKey": "cart",
            "CartItems": [{"MouserPartNumber": "PART", "Quantity": 1}],
            "_meta": {},
        }
    }
    monkeypatch.setattr(
        mouser_services,
        "get_mouser_cart",
        lambda *_args, **_kwargs: state["value"],
    )
    monkeypatch.setattr(
        mouser_services,
        "cart_confirmations",
        mouser_services.CartConfirmationStore(600),
    )
    preview = mouser_services.preview_mouser_cart_change(
        MouserCartPreviewRequest(
            operation=MouserCartOperation.remove_item,
            cart_key="cart",
            mouser_part_number="PART",
        ),
        principal="principal",
    )
    state["value"] = {
        "CartKey": "cart",
        "CartItems": [{"MouserPartNumber": "PART", "Quantity": 2}],
        "_meta": {},
    }
    with pytest.raises(ValueError, match="changed after preview"):
        mouser_services.execute_mouser_cart_change(
            MouserCartExecuteRequest(
                confirmation_token=preview["confirmation_token"]
            ),
            principal="principal",
        )


@pytest.mark.parametrize(
    ("cart_request", "expected_path"),
    [
        (
            MouserCartPreviewRequest(
                operation=MouserCartOperation.add_items,
                items=[MouserCartItem(mouser_part_number="P", quantity=1)],
            ),
            "/api/v1/cart/items/insert",
        ),
        (
            MouserCartPreviewRequest(
                operation=MouserCartOperation.update_items,
                cart_key="cart",
                items=[MouserCartItem(mouser_part_number="P", quantity=2)],
            ),
            "/api/v1/cart/items/update",
        ),
        (
            MouserCartPreviewRequest(
                operation=MouserCartOperation.remove_item,
                cart_key="cart",
                mouser_part_number="P",
            ),
            "/api/v1/cart/item/remove",
        ),
        (
            MouserCartPreviewRequest(
                operation=MouserCartOperation.replace_cart,
                cart_key="cart",
                items=[MouserCartItem(mouser_part_number="P", quantity=1)],
            ),
            "/api/v1/cart",
        ),
        (
            MouserCartPreviewRequest(
                operation=MouserCartOperation.create_from_order,
                order_number=123,
            ),
            "/api/v1/order/item/CreateCartFromOrder",
        ),
        (
            MouserCartPreviewRequest(
                operation=MouserCartOperation.add_schedule,
                cart_key="cart",
                schedule_items=[
                    MouserScheduleItem(
                        mouser_part_number="P",
                        scheduled_releases=[
                            MouserScheduledRelease(
                                date="2026-12-01", quantity=1
                            )
                        ],
                    )
                ],
            ),
            "/api/v1/cart/insert/schedule",
        ),
        (
            MouserCartPreviewRequest(
                operation=MouserCartOperation.update_schedule,
                cart_key="cart",
                schedule_items=[
                    MouserScheduleItem(
                        mouser_part_number="P",
                        scheduled_releases=[
                            MouserScheduledRelease(
                                date="2026-12-02", quantity=1
                            )
                        ],
                    )
                ],
            ),
            "/api/v1/cart/update/schedule",
        ),
        (
            MouserCartPreviewRequest(
                operation=MouserCartOperation.delete_all_schedules,
                cart_key="cart",
            ),
            "/api/v1/cart/deleteall/schedule",
        ),
    ],
)
def test_every_cart_mutation_maps_to_documented_endpoint_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    cart_request: MouserCartPreviewRequest,
    expected_path: str,
) -> None:
    captured: dict[str, Any] = {}

    def execute(method: str, path: str, **kwargs: Any) -> MouserResponse:
        captured.update({"method": method, "path": path, **kwargs})
        return MouserResponse({"ok": True}, {})

    monkeypatch.setattr(mouser_services.client, "request", execute)
    mouser_services._execute_cart_request(cart_request, principal="principal")
    assert captured["path"] == expected_path
    assert captured["safe_retry"] is False


def test_expired_cart_token_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = mouser_services.CartConfirmationStore(60)
    token, preview = store.issue(
        "principal",
        MouserCartPreviewRequest(
            operation=MouserCartOperation.create_from_order,
            order_number=123,
        ),
        "state",
    )
    preview.expires_at = 0
    with pytest.raises(ValueError, match="invalid or expired"):
        store.consume(token, "principal")


def recommendation_offer(
    provider: str,
    mpn: str,
    *,
    voltage: str,
    total: str,
    stock: bool,
    lead_days: float,
) -> DistributorOffer:
    return DistributorOffer(
        distributor=provider,
        identity=component_identity("Maker", mpn),
        distributor_part_number=f"{provider}-{mpn}",
        requested_quantity=10,
        purchasable_quantity=10,
        unit_price=str(Decimal(total) / 10),
        merchandise_total=total,
        currency="USD",
        quantity_available=100 if stock else 0,
        requested_quantity_in_stock=stock,
        lead_time=f"{lead_days} days",
        lead_time_days=lead_days,
        attributes={"Supply Voltage": voltage},
        duty_assumption="test",
        observed_at="2026-01-01T00:00:00+00:00",
    )


def test_recommendations_classify_evidence_and_return_pareto_shortlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = {
        "digikey": SourceResult(
            provider="digikey",
            status="success",
            results=[
                recommendation_offer(
                    "digikey",
                    "QUALIFIED",
                    voltage="5 V",
                    total="2.0000",
                    stock=True,
                    lead_days=14,
                ),
                recommendation_offer(
                    "digikey",
                    "REJECTED",
                    voltage="1.8 V",
                    total="1.0000",
                    stock=True,
                    lead_days=7,
                ),
            ],
        ),
        "mouser": SourceResult(
            provider="mouser",
            status="success",
            results=[
                recommendation_offer(
                    "mouser",
                    "QUALIFIED",
                    voltage="5 V",
                    total="1.9000",
                    stock=True,
                    lead_days=21,
                )
            ],
        ),
    }
    monkeypatch.setattr(
        multi_distributor,
        "_search_recommendation_sources",
        lambda *_args, **_kwargs: sources,
    )
    result = multi_distributor.recommend_components(
        ComponentRecommendationRequest(
            project_summary="A five volt controller for a test fixture",
            search_terms=["controller"],
            quantity=10,
            hard_requirements=[
                ComponentRequirement(
                    name="Supply Voltage",
                    operator=RequirementOperator.gte,
                    value=3.3,
                    unit="V",
                )
            ],
        ),
        principal="principal",
        authorization="Bearer test",
    )
    assert result["status"] == "success"
    assert result["qualified_count"] == 1
    assert result["rejected_count"] == 1
    assert len(result["pareto_shortlist"]) == 1
    assert (
        result["pareto_shortlist"][0]["identity"]["manufacturer_part_number"]
        == "QUALIFIED"
    )


def test_recommendations_return_no_shortlist_when_source_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = {
        "digikey": SourceResult(
            provider="digikey",
            status="success",
            results=[
                recommendation_offer(
                    "digikey",
                    "QUALIFIED",
                    voltage="5 V",
                    total="2.0000",
                    stock=True,
                    lead_days=14,
                )
            ],
        ),
        "mouser": SourceResult(
            provider="mouser",
            status="failed",
            error={"message": "quota"},
        ),
    }
    monkeypatch.setattr(
        multi_distributor,
        "_search_recommendation_sources",
        lambda *_args, **_kwargs: sources,
    )
    result = multi_distributor.recommend_components(
        ComponentRecommendationRequest(
            project_summary="A five volt controller for a test fixture",
            search_terms=["controller"],
            quantity=10,
            hard_requirements=[
                ComponentRequirement(
                    name="Supply Voltage",
                    operator=RequirementOperator.gte,
                    value=3.3,
                    unit="V",
                )
            ],
        ),
        principal="principal",
        authorization="Bearer test",
    )
    assert result["status"] == "partial"
    assert result["pareto_shortlist"] == []


def test_recommendations_return_no_shortlist_when_provider_has_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = {
        "digikey": SourceResult(
            provider="digikey",
            status="success",
            results=[
                recommendation_offer(
                    "digikey",
                    "QUALIFIED",
                    voltage="5 V",
                    total="2.0000",
                    stock=True,
                    lead_days=14,
                )
            ],
        ),
        "mouser": SourceResult(
            provider="mouser",
            status="success",
            results=[],
            warnings=["No candidates were available in the selected region"],
        ),
    }
    monkeypatch.setattr(
        multi_distributor,
        "_search_recommendation_sources",
        lambda *_args, **_kwargs: sources,
    )
    result = multi_distributor.recommend_components(
        ComponentRecommendationRequest(
            project_summary="A five volt controller for a test fixture",
            search_terms=["controller"],
            quantity=10,
            hard_requirements=[
                ComponentRequirement(
                    name="Supply Voltage",
                    operator=RequirementOperator.gte,
                    value=3.3,
                    unit="V",
                )
            ],
        ),
        principal="principal",
        authorization="Bearer test",
    )

    assert result["status"] == "partial"
    assert result["pareto_shortlist"] == []
    assert result["sources"]["mouser"]["warnings"]


def test_conflicting_distributor_specs_are_unverified_not_silently_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = {
        "digikey": SourceResult(
            provider="digikey",
            status="success",
            results=[
                recommendation_offer(
                    "digikey",
                    "CONFLICT",
                    voltage="5 V",
                    total="2.0000",
                    stock=True,
                    lead_days=14,
                )
            ],
        ),
        "mouser": SourceResult(
            provider="mouser",
            status="success",
            results=[
                recommendation_offer(
                    "mouser",
                    "CONFLICT",
                    voltage="1.8 V",
                    total="1.9000",
                    stock=True,
                    lead_days=14,
                )
            ],
        ),
    }
    monkeypatch.setattr(
        multi_distributor,
        "_search_recommendation_sources",
        lambda *_args, **_kwargs: sources,
    )
    result = multi_distributor.recommend_components(
        ComponentRecommendationRequest(
            project_summary="A five volt controller for a test fixture",
            search_terms=["controller"],
            quantity=10,
            hard_requirements=[
                ComponentRequirement(
                    name="Supply Voltage",
                    operator=RequirementOperator.gte,
                    value=3.3,
                    unit="V",
                )
            ],
        ),
        principal="principal",
        authorization="Bearer test",
    )
    assert result["unverified_count"] == 1
    evidence = result["candidates"][0]["hard_requirement_evidence"][0]
    assert evidence["status"] == "unknown"
    assert evidence["reason"] == "Distributor specification evidence conflicts"


def test_digikey_exact_offer_uses_quantity_pricing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        multi_distributor,
        "search_products",
        lambda *_args, **_kwargs: DigiKeyResponse(
            {
                "Products": [
                    {
                        "Manufacturer": {"Name": "Maker"},
                        "ManufacturerProductNumber": "PART-1",
                        "ProductVariations": [
                            {
                                "DigiKeyProductNumber": "DK-PART-1",
                                "MinimumOrderQuantity": 1,
                                "QuantityAvailableforPackageType": 12,
                                "StandardPricing": [
                                    {"BreakQuantity": 1, "UnitPrice": 1.0}
                                ],
                            }
                        ],
                    }
                ]
            },
            {},
        ),
    )
    monkeypatch.setattr(
        multi_distributor,
        "get_pricing_by_quantity",
        lambda *_args, **_kwargs: DigiKeyResponse(
            {
                "StandardPricingOptions": [
                    {
                        "QuantityAvailable": 100,
                        "Products": [
                            {
                                "DigiKeyProductNumber": "DK-PART-1",
                                "Quantity": 10,
                                "UnitPrice": 0.25,
                                "TotalPrice": 2.5,
                                "PackageType": "CutTape",
                            }
                        ],
                    }
                ]
            },
            {},
        ),
    )
    offers = multi_distributor.digikey_adapter.exact_offers(
        "Maker",
        "PART-1",
        10,
        principal="principal",
        authorization="Bearer test",
    )
    assert len(offers) == 1
    assert offers[0].merchandise_total == "2.5000"
    assert offers[0].unit_price == "0.2500"
    assert offers[0].pricing_quantity_available == 100
    assert offers[0].variation_quantity_available == 12
    assert offers[0].pricing_requested_quantity_in_stock is True
    assert offers[0].variation_requested_quantity_in_stock is True
    assert offers[0].quantity_available == 12


def test_region_restricted_mouser_result_remains_unavailable() -> None:
    normalized = mouser_services.normalize_mouser_offer(
        {
            "ActualMfrName": "Maker",
            "ManufacturerPartNumber": "PART-1",
            "MouserPartNumber": None,
            "PriceBreaks": [],
            "AvailabilityInStock": None,
        },
        15,
    )

    assert normalized.distributor_part_number is None
    assert normalized.unit_price is None
    assert normalized.merchandise_total is None
    assert normalized.currency is None
    assert normalized.quantity_available is None
    assert normalized.pricing_quantity_available is None
    assert normalized.variation_quantity_available is None
    assert normalized.purchasable_quantity is None
    assert normalized.purchasable is False
    assert normalized.availability_status == "regional_unavailable"
    assert multi_distributor._priced_usd(normalized) is False
