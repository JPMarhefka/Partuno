from __future__ import annotations

import email.utils
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import HTTPException, Request

from config import Settings, settings
from credentials import (
    CredentialPurpose,
    CredentialStore,
    CredentialUnavailableError,
    EnvironmentCredentialStore,
    Provider,
    credential_value,
)
from identity import LOCAL_PRINCIPAL


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;\"']+")
DAILY_LIMIT_PATTERN = re.compile(
    r"(?i)(?:\bdaily\b.{0,40}\b(?:limit|quota|request|call|exceed)"
    r"|\b(?:limit|quota|request|call|exceed)\b.{0,40}\bdaily\b"
    r"|\bper[\s_-]+day\b)"
)


@dataclass(slots=True)
class DigiKeyResponse:
    data: Any
    meta: dict[str, Any]

    def public(self) -> Any:
        if isinstance(self.data, dict):
            result = dict(self.data)
            result["_meta"] = self.meta
            return result
        return {"data": self.data, "_meta": self.meta}


class DigiKeyHTTPError(RuntimeError):
    def __init__(self, status_code: int, detail: Any, meta: dict[str, Any] | None = None):
        super().__init__(f"DigiKey request failed with HTTP {status_code}")
        self.status_code = status_code
        self.detail = detail
        self.meta = meta or {}


def error_envelope(
    status_code: int,
    detail: Any,
    meta: dict[str, Any] | None = None,
    *,
    category: str | None = None,
    error_type: str | None = None,
    retryable: bool | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Return the single public error shape used by REST and MCP boundaries."""
    metadata = meta or {}
    source = detail if isinstance(detail, dict) else {}
    message = (
        source.get("message")
        or source.get("detail")
        or source.get("title")
        or str(detail)
    )
    resolved_provider = (
        provider
        or source.get("provider")
        or metadata.get("provider")
        or "digikey"
    )
    return {
        "success": False,
        "error": {
            "provider": resolved_provider,
            "category": category or source.get("category") or "upstream",
            "status_code": status_code,
            "type": (
                error_type
                or source.get("error_type")
                or source.get("type")
                or f"{resolved_provider}_error"
            ),
            "message": message,
            "detail": detail,
            "retryable": (
                bool(retryable)
                if retryable is not None
                else bool(
                    source.get(
                        "retryable",
                        metadata.get(
                            "retryable",
                            status_code in RETRYABLE_STATUS_CODES,
                        ),
                    )
                )
            ),
            "attempts": metadata.get("attempts"),
            "correlation_id": metadata.get("correlation_id"),
            "rate_limit": {
                "limit": metadata.get("rate_limit"),
                "remaining": metadata.get("rate_limit_remaining"),
                "reset": metadata.get("rate_limit_reset"),
                "retry_after": metadata.get("retry_after"),
                "scope": metadata.get("rate_limit_scope"),
                "retry_stopped_reason": metadata.get("retry_stopped_reason"),
            },
        },
        "_meta": metadata,
    }

class DigiKeyClient:
    def __init__(
        self,
        config: Settings = settings,
        credential_store: CredentialStore | None = None,
    ):
        self.config = config
        # Construct from the supplied settings so tests and embedded callers
        # can use an isolated user-owned credential set without mutating the
        # process-wide defaults.
        self.credential_store = credential_store or EnvironmentCredentialStore(config)
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "DigiKey-GPT-Action/2.0"})
            self._local.session = session
        return session

    def _headers(
        self,
        authorization: str,
        account_id: str | None = None,
        *,
        principal: str = LOCAL_PRINCIPAL,
    ) -> dict[str, str]:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="A DigiKey OAuth bearer token is required")
        values = self.credential_store.get(
            principal=principal,
            provider=Provider.DIGIKEY,
            purpose=CredentialPurpose.OAUTH_CLIENT,
        )
        client_id = credential_value(values or {}, "client_id")
        if not client_id:
            raise CredentialUnavailableError(
                Provider.DIGIKEY,
                CredentialPurpose.OAUTH_CLIENT,
            )
        headers = {
            "Authorization": authorization,
            "X-DIGIKEY-Client-Id": client_id,
            "X-DIGIKEY-Locale-Site": self.config.site,
            "X-DIGIKEY-Locale-Language": self.config.language,
            "X-DIGIKEY-Locale-Currency": self.config.currency,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if account_id:
            headers["X-DIGIKEY-Account-Id"] = str(account_id)
        return headers

    @staticmethod
    def _sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: DigiKeyClient._sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [DigiKeyClient._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return tuple(DigiKeyClient._sanitize(item) for item in value)
        if isinstance(value, str):
            return BEARER_PATTERN.sub("Bearer [REDACTED]", value)
        return value

    @staticmethod
    def _safe_error(response: requests.Response) -> Any:
        try:
            return DigiKeyClient._sanitize(response.json())
        except ValueError:
            return {
                "message": DigiKeyClient._sanitize(response.text[:4000] or response.reason),
                "status_code": response.status_code,
            }

    @staticmethod
    def _error_text(value: Any, *, depth: int = 0) -> str:
        """Collect bounded error text for retry classification, never logging it."""
        if depth > 5:
            return ""
        if isinstance(value, dict):
            values: list[str] = []
            for key, item in value.items():
                values.append(str(key))
                values.append(
                    DigiKeyClient._error_text(item, depth=depth + 1)
                )
            return " ".join(values)
        if isinstance(value, (list, tuple)):
            return " ".join(
                DigiKeyClient._error_text(item, depth=depth + 1)
                for item in value[:25]
            )
        return str(value or "")[:1000]

    @staticmethod
    def _is_daily_limit_response(
        response: requests.Response,
        detail: Any,
        *,
        path: str = "",
    ) -> bool:
        if response.status_code != 429:
            return False
        headers = response.headers
        scope = (
            headers.get("X-RateLimit-Scope")
            or headers.get("RateLimit-Scope")
            or headers.get("X-RateLimit-Period")
            or ""
        ).strip().casefold()
        if scope in {"day", "daily", "1d", "24h"}:
            return True
        error_text = DigiKeyClient._error_text(detail).replace("_", " ").replace(
            "-",
            " ",
        )
        if DAILY_LIMIT_PATTERN.search(error_text):
            return True
        return (
            "/changenotifications/" in path.casefold()
            and str(headers.get("X-RateLimit-Remaining") or "").strip() == "0"
        )

    @staticmethod
    def _rate_meta(
        response: requests.Response,
        attempts: int,
        error_detail: Any = None,
    ) -> dict[str, Any]:
        headers = response.headers
        body_correlation_id = None
        if isinstance(error_detail, dict):
            body_correlation_id = (
                error_detail.get("correlationId")
                or error_detail.get("CorrelationId")
                or error_detail.get("requestId")
                or error_detail.get("RequestId")
            )
        return {
            "http_status": response.status_code,
            "attempts": attempts,
            "rate_limit": headers.get("X-RateLimit-Limit"),
            "rate_limit_remaining": headers.get("X-RateLimit-Remaining"),
            "rate_limit_reset": headers.get("X-RateLimit-Reset"),
            "retry_after": headers.get("Retry-After"),
            "correlation_id": (
                headers.get("X-Correlation-Id")
                or headers.get("Correlation-Id")
                or headers.get("X-Request-Id")
                or body_correlation_id
            ),
        }

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    parsed = email.utils.parsedate_to_datetime(retry_after)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    delay = max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = 0.0
            if delay > 0:
                return min(delay, self.config.max_retry_after_seconds)
        exponential = self.config.retry_backoff_seconds * (2 ** max(0, attempt - 1))
        return min(exponential, self.config.max_retry_after_seconds)

    def request(
        self,
        method: str,
        path: str,
        authorization: str,
        *,
        principal: str = LOCAL_PRINCIPAL,
        account_id: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        safe_retry: bool | None = None,
        timeout: float | None = None,
    ) -> DigiKeyResponse:
        method = method.upper()
        if safe_retry is None:
            safe_retry = method in SAFE_METHODS
        max_attempts = self.config.safe_retry_attempts if safe_retry else 1
        url = f"{self.config.api_base}{path}"
        response: requests.Response | None = None
        attempt_history: list[dict[str, Any]] = []

        for attempt in range(1, max_attempts + 1):
            try:
                response = self._session().request(
                    method,
                    url,
                    headers=self._headers(
                        authorization,
                        account_id,
                        principal=principal,
                    ),
                    params=params,
                    json=json_body,
                    timeout=timeout or self.config.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt < max_attempts:
                    time.sleep(min(self.config.retry_backoff_seconds * attempt, 3.0))
                    continue
                raise HTTPException(status_code=502, detail={"message": "Could not reach DigiKey", "error_type": exc.__class__.__name__}) from exc

            if response.ok:
                meta = self._rate_meta(response, attempt)
                if attempt_history:
                    meta["attempt_history"] = [
                        *attempt_history,
                        {
                            "status_code": response.status_code,
                            "correlation_id": meta.get("correlation_id"),
                            "rate_limit_remaining": meta.get("rate_limit_remaining"),
                        },
                    ]
                if response.status_code == 204 or not response.content:
                    return DigiKeyResponse({"success": True}, meta)
                try:
                    return DigiKeyResponse(response.json(), meta)
                except ValueError:
                    return DigiKeyResponse({"result": response.text}, meta)

            detail = self._safe_error(response)
            response_meta = self._rate_meta(response, attempt, detail)
            daily_limit = self._is_daily_limit_response(
                response,
                detail,
                path=path,
            )
            if daily_limit:
                if isinstance(detail, dict):
                    detail = dict(detail)
                    detail.setdefault("error_type", "digikey_daily_rate_limit")
                    detail["retryable"] = False
                    detail["rate_limit_scope"] = "daily"
                else:
                    detail = {
                        "message": "DigiKey daily request limit exhausted",
                        "detail": detail,
                        "error_type": "digikey_daily_rate_limit",
                        "retryable": False,
                        "rate_limit_scope": "daily",
                    }
                response_meta.update(
                    {
                        "retryable": False,
                        "rate_limit_scope": "daily",
                        "retry_stopped_reason": "daily_rate_limit",
                    }
                )
            attempt_history.append({
                "status_code": response.status_code,
                "correlation_id": response_meta.get("correlation_id"),
                "rate_limit_remaining": response_meta.get("rate_limit_remaining"),
            })

            if (
                safe_retry
                and response.status_code in RETRYABLE_STATUS_CODES
                and not daily_limit
                and attempt < max_attempts
            ):
                time.sleep(self._retry_delay(response, attempt))
                continue

            meta = response_meta
            if len(attempt_history) > 1:
                meta["attempt_history"] = attempt_history
            raise DigiKeyHTTPError(response.status_code, detail, meta)

        raise HTTPException(status_code=502, detail="DigiKey request failed without a response")


def authorization_from_request(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="A DigiKey OAuth bearer token is required")
    return authorization


def translate_digikey_error(exc: DigiKeyHTTPError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"digikey": exc.detail, "_meta": exc.meta},
    )


client = DigiKeyClient()
