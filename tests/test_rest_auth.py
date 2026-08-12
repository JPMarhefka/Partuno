from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from client import DigiKeyResponse
from config import settings
import rest_auth


def request_with_authorization(value: str | None) -> Request:
    headers = []
    if value is not None:
        headers.append((b"authorization", value.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mouser/products/search",
            "headers": headers,
        }
    )


def test_distributor_rest_auth_validates_digikey_token_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        rest_auth.client,
        "request",
        lambda *args, **_kwargs: (
            calls.append(args)
            or DigiKeyResponse({"Accounts": [{"Id": "one"}]}, {})
        ),
    )
    authenticator = rest_auth.DigiKeyRESTAuthenticator(cache_seconds=45)
    request = request_with_authorization("Bearer fixture")
    first = authenticator.authenticate(request)
    second = authenticator.authenticate(request)
    assert first.principal == second.principal
    assert first.authorization == "Bearer fixture"
    assert len(calls) == 1
    assert calls[0][1] == "/CustomerResource/v1/associatedaccounts"


def test_distributor_rest_auth_rejects_unverified_header() -> None:
    authenticator = rest_auth.DigiKeyRESTAuthenticator()
    with pytest.raises(HTTPException) as raised:
        authenticator.authenticate(request_with_authorization(None))
    assert raised.value.status_code == 401


def test_local_mode_can_use_mouser_without_digikey_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main

    monkeypatch.setattr(
        main,
        "settings",
        replace(settings, partuno_mode="local", client_id=None),
    )
    context = main.distributor_auth(request_with_authorization(None))

    assert context.authorization == ""
    assert context.principal == "local"


def test_principal_is_stable_across_refreshed_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rest_auth.client,
        "request",
        lambda *_args, **_kwargs: DigiKeyResponse(
            {"Accounts": [{"Id": "stable-account"}]}, {}
        ),
    )
    authenticator = rest_auth.DigiKeyRESTAuthenticator()
    first = authenticator.authenticate(
        request_with_authorization("Bearer first-token")
    )
    second = authenticator.authenticate(
        request_with_authorization("Bearer refreshed-token")
    )
    assert first.principal == second.principal
