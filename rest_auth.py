from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

from fastapi import Request

from client import authorization_from_request, client
from identity import digikey_subject


@dataclass(frozen=True, slots=True)
class AuthenticatedContext:
    authorization: str
    principal: str


class DigiKeyRESTAuthenticator:
    """Validate REST bearer tokens before using server-owned provider secrets."""

    def __init__(self, cache_seconds: int = 45) -> None:
        self.cache_seconds = max(5, cache_seconds)
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def authenticate(self, request: Request) -> AuthenticatedContext:
        authorization = authorization_from_request(request)
        token = authorization.split(" ", 1)[1]
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(token_hash)
            if cached and cached[0] > now:
                return AuthenticatedContext(authorization, cached[1])

        # DigiKey opaque tokens have no introspection endpoint. The same
        # associated-accounts read used by MCP token verification proves the
        # token before this server uses its protected Mouser credentials.
        response = client.request(
            "GET",
            "/CustomerResource/v1/associatedaccounts",
            authorization,
        )
        principal = digikey_subject(response.data)
        with self._lock:
            self._cache[token_hash] = (now + self.cache_seconds, principal)
            for key in [
                key for key, value in self._cache.items() if value[0] <= now
            ]:
                self._cache.pop(key, None)
        return AuthenticatedContext(authorization, principal)


rest_authenticator = DigiKeyRESTAuthenticator()
