from __future__ import annotations

from typing import Any

from fastapi import Response, status

from config import settings
from credentials import CredentialPurpose, Provider, provider_configured
from main import app
from mcp_server import build_mcp_server
from mouser_services import mouser_adapter
from multi_distributor import digikey_adapter


mcp = build_mcp_server(local=settings.partuno_mode == "local")
mcp_http_app = (
    mcp.http_app(path="/mcp", stateless_http=True) if mcp is not None else None
)


@app.get("/mcp-health", include_in_schema=False)
def mcp_health() -> dict[str, Any]:
    mcp_active = mcp is not None
    mcp_url = (
        f"{settings.mcp_base_url}/mcp"
        if settings.mcp_base_url
        else "/mcp"
        if mcp_active
        else None
    )
    return {
        "status": "ok" if mcp_active else "disabled",
        "version": "4.0.0",
        "mcp": {
            "enabled": mcp_active,
            "url": mcp_url,
            "callback_url": (
                f"{settings.mcp_base_url}/auth/callback"
                if settings.mcp_enabled
                else None
            ),
            "storage": "ephemeral",
            "stable_signing_key_configured": bool(settings.mcp_jwt_signing_key),
        },
        "providers": {
            "digikey": digikey_adapter.health(),
            "mouser": mouser_adapter.health(),
        },
    }


@app.get("/ready", include_in_schema=False)
def readiness(response: Response) -> dict[str, Any]:
    """Report configuration readiness without exposing secrets or values."""
    digikey_ready = provider_configured(
        Provider.DIGIKEY,
        CredentialPurpose.OAUTH_CLIENT,
    )
    mouser_search_ready = provider_configured(
        Provider.MOUSER,
        CredentialPurpose.SEARCH,
    )
    mouser_account_ready = provider_configured(
        Provider.MOUSER,
        CredentialPurpose.ACCOUNT,
    )
    ready = digikey_ready or mouser_search_ready or mouser_account_ready
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "not_ready",
        "rest": ready,
        "mcp": mcp is not None,
        "providers": {
            "digikey": "configured" if digikey_ready else "disabled",
            "mouser_catalog": (
                "configured" if mouser_search_ready else "disabled"
            ),
            "mouser_account": (
                "configured" if mouser_account_ready else "disabled"
            ),
        },
    }


if mcp_http_app is not None:
    app.router.lifespan_context = mcp_http_app.lifespan
    app.mount("/", mcp_http_app)
