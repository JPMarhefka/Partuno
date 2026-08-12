from __future__ import annotations

import email.utils
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from config import Settings, settings
from credentials import (
    CredentialPurpose,
    CredentialStore,
    CredentialUnavailableError,
    EnvironmentCredentialStore,
    Provider,
    credential_value,
)


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
API_KEY_PATTERN = re.compile(
    r"(?i)(apiKey(?:%3[dD]|=))([^&\s,;\"']+)"
)


@dataclass(slots=True)
class MouserResponse:
    data: Any
    meta: dict[str, Any]

    def public(self) -> Any:
        if isinstance(self.data, dict):
            result = dict(self.data)
            result["_meta"] = self.meta
            return result
        return {"data": self.data, "_meta": self.meta}


class MouserHTTPError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        detail: Any,
        meta: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"Mouser request failed with status {status_code}")
        self.status_code = status_code
        self.detail = detail
        self.meta = meta or {}
        self.provider = "mouser"


class MouserRateLimiter:
    """Single-process minute/day guard for a local or single-user deployment."""

    def __init__(self, minute_limit: int, daily_limit: int) -> None:
        self.minute_limit = minute_limit
        self.daily_limit = daily_limit
        self._minute_calls: deque[float] = deque()
        self._day = datetime.now(timezone.utc).date()
        self._daily_calls = 0
        self._lock = threading.Lock()

    def acquire(self) -> dict[str, int]:
        now = time.monotonic()
        utc_day = datetime.now(timezone.utc).date()
        with self._lock:
            if utc_day != self._day:
                self._day = utc_day
                self._daily_calls = 0
            while self._minute_calls and self._minute_calls[0] <= now - 60:
                self._minute_calls.popleft()
            if len(self._minute_calls) >= self.minute_limit:
                raise MouserHTTPError(
                    429,
                    {
                        "message": "Local Mouser per-minute request budget exhausted",
                        "error_type": "local_rate_limit",
                        "retryable": True,
                    },
                    {
                        "rate_limit": self.minute_limit,
                        "rate_limit_remaining": 0,
                        "retry_after": 60,
                    },
                )
            if self._daily_calls >= self.daily_limit:
                raise MouserHTTPError(
                    429,
                    {
                        "message": "Local Mouser daily request budget exhausted",
                        "error_type": "local_daily_limit",
                        "retryable": False,
                    },
                    {
                        "rate_limit": self.daily_limit,
                        "rate_limit_remaining": 0,
                    },
                )
            self._minute_calls.append(now)
            self._daily_calls += 1
            return {
                "minute_remaining": self.minute_limit - len(self._minute_calls),
                "daily_remaining": self.daily_limit - self._daily_calls,
            }


class ProviderHealthTracker:
    def __init__(self) -> None:
        self._last_success: str | None = None
        self._last_error: dict[str, Any] | None = None
        self._degraded = False
        self._lock = threading.Lock()

    def success(self) -> None:
        with self._lock:
            self._last_success = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            self._degraded = False

    def failure(self, status_code: int, detail: Any) -> None:
        source = detail if isinstance(detail, dict) else {}
        with self._lock:
            self._last_error = {
                "at": datetime.now(timezone.utc).isoformat(),
                "status_code": status_code,
                "type": source.get("error_type") or source.get("Code") or "mouser_error",
            }
            self._degraded = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "degraded" if self._degraded else "configured",
                "last_success": self._last_success,
                "last_error": dict(self._last_error) if self._last_error else None,
            }


health_tracker = ProviderHealthTracker()


class MouserClient:
    def __init__(
        self,
        config: Settings = settings,
        credential_store: CredentialStore | None = None,
        *,
        credential_provider: CredentialStore | None = None,
    ) -> None:
        if credential_store is not None and credential_provider is not None:
            raise ValueError("Pass either credential_store or credential_provider, not both")
        self.config = config
        self.credential_store = (
            credential_store
            or credential_provider
            or EnvironmentCredentialStore(config)
        )
        # Compatibility alias for integrations that used the pre-Partuno name.
        self.credential_provider = self.credential_store
        self._local = threading.local()
        self.rate_limiters = {
            purpose: MouserRateLimiter(
                config.mouser_minute_limit, config.mouser_daily_limit
            )
            for purpose in (CredentialPurpose.SEARCH, CredentialPurpose.ACCOUNT)
        }

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": "Partuno-MCP/4.0",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
            )
            self._local.session = session
        return session

    @staticmethod
    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    "[REDACTED]"
                    if key.lower() in {"apikey", "api_key"}
                    else MouserClient.sanitize(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [MouserClient.sanitize(item) for item in value]
        if isinstance(value, tuple):
            return tuple(MouserClient.sanitize(item) for item in value)
        if isinstance(value, str):
            return API_KEY_PATTERN.sub(r"\1[REDACTED]", value)
        return value

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
                    delay = max(
                        0.0, (parsed - datetime.now(timezone.utc)).total_seconds()
                    )
                except (TypeError, ValueError, OverflowError):
                    delay = 0.0
            if delay > 0:
                return min(delay, self.config.max_retry_after_seconds)
        return min(
            self.config.retry_backoff_seconds * (2 ** max(0, attempt - 1)),
            self.config.max_retry_after_seconds,
        )

    @staticmethod
    def _embedded_errors(payload: Any) -> list[Any]:
        if not isinstance(payload, dict):
            return []
        errors = payload.get("Errors")
        return errors if isinstance(errors, list) else []

    @staticmethod
    def _body_correlation_id(
        payload: Any,
        *,
        depth: int = 0,
    ) -> str | None:
        """Read an upstream request ID from common body fields without inventing one."""
        if depth > 5:
            return None
        if isinstance(payload, dict):
            normalized_keys = {
                re.sub(r"[^a-z0-9]", "", str(key).casefold()): value
                for key, value in payload.items()
            }
            for key in ("correlationid", "requestid"):
                value = normalized_keys.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
            for value in payload.values():
                found = MouserClient._body_correlation_id(
                    value,
                    depth=depth + 1,
                )
                if found:
                    return found
        elif isinstance(payload, (list, tuple)):
            for value in payload[:50]:
                found = MouserClient._body_correlation_id(
                    value,
                    depth=depth + 1,
                )
                if found:
                    return found
        return None

    def request(
        self,
        method: str,
        path: str,
        *,
        principal: str,
        purpose: CredentialPurpose,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        safe_retry: bool,
    ) -> MouserResponse:
        values = self.credential_store.get(
            principal=principal,
            provider=Provider.MOUSER,
            purpose=purpose,
        )
        api_key = credential_value(values or {}, "api_key")
        if not api_key:
            raise CredentialUnavailableError(Provider.MOUSER, purpose)
        request_params = dict(params or {})
        request_params["apiKey"] = api_key
        max_attempts = self.config.safe_retry_attempts if safe_retry else 1
        url = f"{self.config.mouser_api_base}{path}"
        history: list[dict[str, Any]] = []

        for attempt in range(1, max_attempts + 1):
            try:
                budget = self.rate_limiters[purpose].acquire()
            except MouserHTTPError as exc:
                health_tracker.failure(exc.status_code, exc.detail)
                raise
            try:
                response = self._session().request(
                    method.upper(),
                    url,
                    params=request_params,
                    json=json_body,
                    timeout=self.config.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt < max_attempts:
                    time.sleep(
                        min(self.config.retry_backoff_seconds * attempt, 3.0)
                    )
                    continue
                detail = {
                    "message": "Could not reach Mouser",
                    "error_type": exc.__class__.__name__,
                }
                health_tracker.failure(502, detail)
                raise MouserHTTPError(
                    502,
                    detail,
                    {"attempts": attempt, **budget},
                ) from exc

            meta = {
                "provider": "mouser",
                "credential_purpose": purpose.value,
                "http_status": response.status_code,
                "attempts": attempt,
                "correlation_id": self.sanitize(
                    response.headers.get("X-Correlation-Id")
                    or response.headers.get("X-Request-Id")
                ),
                "retry_after": response.headers.get("Retry-After"),
                "rate_limit": self.config.mouser_minute_limit,
                "rate_limit_remaining": budget["minute_remaining"],
                "local_minute_remaining": budget["minute_remaining"],
                "daily_limit": self.config.mouser_daily_limit,
                "local_daily_remaining": budget["daily_remaining"],
            }
            try:
                payload = response.json()
            except ValueError:
                payload = {
                    "message": self.sanitize(
                        response.text[:4000] or response.reason
                    ),
                    "status_code": response.status_code,
                }
            if not meta["correlation_id"]:
                meta["correlation_id"] = self.sanitize(
                    self._body_correlation_id(payload)
                )

            embedded_errors = self.sanitize(self._embedded_errors(payload))
            if response.ok and not embedded_errors:
                if history:
                    meta["attempt_history"] = history
                if response.status_code == 204 or not response.content:
                    payload = {"success": True}
                health_tracker.success()
                return MouserResponse(self.sanitize(payload), meta)

            detail: Any = (
                {"message": "Mouser returned API errors", "errors": embedded_errors}
                if response.ok and embedded_errors
                else self.sanitize(payload)
            )
            effective_status = response.status_code if not response.ok else 422
            history.append(
                {
                    "status_code": effective_status,
                    "correlation_id": meta["correlation_id"],
                }
            )
            if (
                safe_retry
                and response.status_code in RETRYABLE_STATUS_CODES
                and attempt < max_attempts
            ):
                time.sleep(self._retry_delay(response, attempt))
                continue
            if len(history) > 1:
                meta["attempt_history"] = history
            health_tracker.failure(effective_status, detail)
            raise MouserHTTPError(effective_status, detail, meta)

        raise MouserHTTPError(502, {"message": "Mouser request had no response"})


client = MouserClient()
