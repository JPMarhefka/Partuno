from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from client import (
    DigiKeyClient,
    DigiKeyHTTPError,
    error_envelope,
)
from config import settings
from credentials import CredentialUnavailableError


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: Any,
        *,
        headers: dict[str, str] | None = None,
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.content = b"payload"
        self.text = str(body)
        self.reason = "fake"
        self._json_error = json_error

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, *_args: Any, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def make_client(responses: list[FakeResponse]) -> tuple[DigiKeyClient, FakeSession]:
    test_settings = replace(
        settings,
        safe_retry_attempts=2,
        retry_backoff_seconds=0.1,
        max_retry_after_seconds=1,
    )
    test_client = DigiKeyClient(test_settings)
    session = FakeSession(responses)
    test_client._local.session = session
    return test_client, session


def test_missing_digikey_client_credentials_fail_before_upstream_call() -> None:
    test_settings = replace(settings, client_id=None, client_secret=None)
    test_client = DigiKeyClient(test_settings)
    session = FakeSession([FakeResponse(200, {"unexpected": True})])
    test_client._local.session = session

    with pytest.raises(CredentialUnavailableError) as raised:
        test_client.request("GET", "/test", "Bearer fixture")

    assert raised.value.error_type == "provider_not_configured"
    assert session.calls == []


def test_success_metadata_contains_correlation_and_rate_headers() -> None:
    test_client, _ = make_client([
        FakeResponse(200, {"ok": True}, headers={
            "X-Correlation-Id": "cid",
            "X-RateLimit-Limit": "1000",
            "X-RateLimit-Remaining": "999",
            "X-RateLimit-Reset": "soon",
        })
    ])

    response = test_client.request("GET", "/test", "Bearer fixture")

    assert response.meta == {
        "http_status": 200,
        "attempts": 1,
        "rate_limit": "1000",
        "rate_limit_remaining": "999",
        "rate_limit_reset": "soon",
        "retry_after": None,
        "correlation_id": "cid",
    }


def test_retry_failure_preserves_safe_problem_and_attempt_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("client.time.sleep", lambda *_args: None)
    test_client, _ = make_client([
        FakeResponse(503, {"message": "first", "correlationId": "c1"}, headers={"X-RateLimit-Remaining": "9"}),
        FakeResponse(503, {"message": "second", "correlationId": "c2"}, headers={"X-RateLimit-Remaining": "8"}),
    ])

    with pytest.raises(DigiKeyHTTPError) as raised:
        test_client.request("GET", "/test", "Bearer fixture")

    assert raised.value.detail == {"message": "second", "correlationId": "c2"}
    assert raised.value.meta["attempts"] == 2
    assert raised.value.meta["attempt_history"] == [
        {"status_code": 503, "correlation_id": "c1", "rate_limit_remaining": "9"},
        {"status_code": 503, "correlation_id": "c2", "rate_limit_remaining": "8"},
    ]


def test_non_json_problem_is_truncated_and_does_not_leak_bearer_token() -> None:
    secret = "Bearer fixture-secret-that-must-not-appear"
    response = FakeResponse(400, f"bad input {secret}", json_error=True)
    test_client, _ = make_client([response])

    with pytest.raises(DigiKeyHTTPError) as raised:
        test_client.request("GET", "/test", secret)

    serialized = str(error_envelope(400, raised.value.detail, raised.value.meta))
    assert "fixture-secret-that-must-not-appear" not in serialized
    assert raised.value.detail["message"].startswith("bad input")


def test_write_failure_is_not_automatically_retried() -> None:
    test_client, session = make_client([
        FakeResponse(503, {"message": "write failed"}),
        FakeResponse(200, {"unexpected": True}),
    ])

    with pytest.raises(DigiKeyHTTPError):
        test_client.request("POST", "/test", "Bearer fixture", json_body={"value": 1})

    assert len(session.calls) == 1


def test_daily_limit_429_is_detected_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("client.time.sleep", lambda *_args: None)
    test_settings = replace(
        settings,
        safe_retry_attempts=4,
        retry_backoff_seconds=0.1,
        max_retry_after_seconds=1,
    )
    test_client = DigiKeyClient(test_settings)
    session = FakeSession(
        [
            FakeResponse(
                429,
                {
                    "message": "The daily request limit has been exceeded.",
                    "correlationId": "daily-cid",
                },
                headers={
                    "X-RateLimit-Limit": "30",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "next-day",
                    "Retry-After": "3600",
                },
            )
        ]
    )
    test_client._local.session = session

    with pytest.raises(DigiKeyHTTPError) as raised:
        test_client.request(
            "GET",
            "/ChangeNotifications/v3/Products/ABC",
            "Bearer fixture",
        )

    assert len(session.calls) == 1
    assert raised.value.detail["error_type"] == "digikey_daily_rate_limit"
    assert raised.value.detail["retryable"] is False
    assert raised.value.meta["attempts"] == 1
    assert raised.value.meta["retryable"] is False
    assert raised.value.meta["rate_limit_scope"] == "daily"
    assert raised.value.meta["retry_stopped_reason"] == "daily_rate_limit"
    assert raised.value.meta["correlation_id"] == "daily-cid"
    assert raised.value.meta["rate_limit_remaining"] == "0"
    public = error_envelope(
        raised.value.status_code,
        raised.value.detail,
        raised.value.meta,
    )
    assert public["error"]["retryable"] is False
    assert public["error"]["rate_limit"]["retry_after"] == "3600"
    assert public["error"]["rate_limit"]["scope"] == "daily"
    assert (
        public["error"]["rate_limit"]["retry_stopped_reason"]
        == "daily_rate_limit"
    )


def test_pcn_429_with_exhausted_header_is_treated_as_daily_limit() -> None:
    test_settings = replace(settings, safe_retry_attempts=4)
    test_client = DigiKeyClient(test_settings)
    session = FakeSession(
        [
            FakeResponse(
                429,
                {"message": "Request limit reached"},
                headers={"X-RateLimit-Remaining": "0"},
            )
        ]
    )
    test_client._local.session = session

    with pytest.raises(DigiKeyHTTPError) as raised:
        test_client.request(
            "GET",
            "/ChangeNotifications/v3/Products/ABC",
            "Bearer fixture",
        )

    assert len(session.calls) == 1
    assert raised.value.meta["rate_limit_scope"] == "daily"


def test_non_daily_429_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("client.time.sleep", lambda *_args: None)
    test_client, session = make_client(
        [
            FakeResponse(429, {"message": "Too many requests"}),
            FakeResponse(200, {"ok": True}),
        ]
    )

    response = test_client.request("GET", "/test", "Bearer fixture")

    assert response.data == {"ok": True}
    assert len(session.calls) == 2
