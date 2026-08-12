from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


def _public_base_url() -> str | None:
    explicit = os.getenv("MCP_BASE_URL", "").strip()
    if explicit:
        candidate = explicit.rstrip("/")
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise RuntimeError("MCP_BASE_URL must be an HTTPS origin without a path")
        return candidate

    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if render_url:
        return render_url.rstrip("/")

    render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if render_hostname:
        return f"https://{render_hostname}".rstrip("/")

    return None


@dataclass(frozen=True, slots=True)
class Settings:
    partuno_mode: str
    client_id: str | None
    client_secret: str | None
    digikey_access_token: str | None
    api_base: str
    site: str
    language: str
    currency: str
    request_timeout_seconds: float
    safe_retry_attempts: int
    retry_backoff_seconds: float
    max_retry_after_seconds: float
    workflow_concurrency: int
    max_bulk_items: int
    search_fallback_max_pages: int
    mcp_base_url: str | None
    mcp_jwt_signing_key: str | None
    reference_cache_seconds: int
    pcn_cache_seconds: int
    mouser_search_api_key: str | None
    mouser_account_api_key: str | None
    mouser_api_base: str
    mouser_minute_limit: int
    mouser_daily_limit: int
    cart_preview_ttl_seconds: int

    @property
    def mcp_enabled(self) -> bool:
        return bool(
            self.partuno_mode == "remote_single_user"
            and self.client_id
            and self.client_secret
            and self.mcp_base_url
            and self.mcp_jwt_signing_key
        )

    @property
    def digikey_enabled(self) -> bool:
        return bool(self.client_id)

    @property
    def mouser_search_enabled(self) -> bool:
        return bool(self.mouser_search_api_key)

    @property
    def mouser_account_enabled(self) -> bool:
        return bool(self.mouser_account_api_key)

    @property
    def any_provider_enabled(self) -> bool:
        return bool(
            self.digikey_enabled
            or self.mouser_search_enabled
            or self.mouser_account_enabled
        )

    @classmethod
    def from_env(cls) -> "Settings":
        partuno_mode = os.getenv("PARTUNO_MODE", "local").strip().lower() or "local"
        if partuno_mode not in {"local", "remote_single_user"}:
            raise RuntimeError(
                "PARTUNO_MODE must be local or remote_single_user"
            )

        client_id = os.getenv("DIGIKEY_CLIENT_ID", "").strip() or None

        client_secret = os.getenv("DIGIKEY_CLIENT_SECRET", "").strip() or None

        digikey_access_token = (
            os.getenv("DIGIKEY_ACCESS_TOKEN", "").strip() or None
        )

        return cls(
            partuno_mode=partuno_mode,
            client_id=client_id,
            client_secret=client_secret,
            digikey_access_token=digikey_access_token,
            api_base=os.getenv("DIGIKEY_API_BASE", "https://api.digikey.com").rstrip("/"),
            site=os.getenv("DIGIKEY_SITE", "US").strip() or "US",
            language=os.getenv("DIGIKEY_LANGUAGE", "en").strip() or "en",
            currency=os.getenv("DIGIKEY_CURRENCY", "USD").strip() or "USD",
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
            safe_retry_attempts=max(1, int(os.getenv("SAFE_RETRY_ATTEMPTS", "4"))),
            retry_backoff_seconds=max(0.1, float(os.getenv("RETRY_BACKOFF_SECONDS", "0.75"))),
            max_retry_after_seconds=max(1.0, float(os.getenv("MAX_RETRY_AFTER_SECONDS", "8"))),
            workflow_concurrency=max(1, min(12, int(os.getenv("WORKFLOW_CONCURRENCY", "6")))),
            max_bulk_items=max(1, min(100, int(os.getenv("MAX_BULK_ITEMS", "30")))),
            search_fallback_max_pages=max(
                1, min(20, int(os.getenv("SEARCH_FALLBACK_MAX_PAGES", "5")))
            ),
            mcp_base_url=_public_base_url(),
            mcp_jwt_signing_key=os.getenv("MCP_JWT_SIGNING_KEY", "").strip() or None,
            reference_cache_seconds=max(
                30, min(3600, int(os.getenv("REFERENCE_CACHE_SECONDS", "600")))
            ),
            pcn_cache_seconds=max(
                0, min(900, int(os.getenv("PCN_CACHE_SECONDS", "120")))
            ),
            mouser_search_api_key=os.getenv("MOUSER_SEARCH_API_KEY", "").strip() or None,
            mouser_account_api_key=os.getenv("MOUSER_ACCOUNT_API_KEY", "").strip() or None,
            mouser_api_base=os.getenv(
                "MOUSER_API_BASE", "https://api.mouser.com"
            ).rstrip("/"),
            mouser_minute_limit=max(
                1, min(30, int(os.getenv("MOUSER_MINUTE_LIMIT", "30")))
            ),
            mouser_daily_limit=max(
                1, min(1000, int(os.getenv("MOUSER_DAILY_LIMIT", "1000")))
            ),
            cart_preview_ttl_seconds=max(
                60, min(1800, int(os.getenv("CART_PREVIEW_TTL_SECONDS", "600")))
            ),
        )


settings = Settings.from_env()
