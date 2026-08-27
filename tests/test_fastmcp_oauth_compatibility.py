from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth import OAuthProxy, TokenVerifier
from fastmcp.server.auth.cimd import CIMDDocument
from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient


PUBLIC_ORIGIN = "https://digikey-gpt.onrender.com"
CLIENT_ID = "https://chatgpt.com/cimd"
REDIRECT_URI = "http://localhost:43123/callback"


def _jwk_for_public_key(public_key: rsa.RSAPublicKey, key_id: str) -> dict[str, object]:
    numbers = public_key.public_numbers()

    def encode(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": key_id,
                "use": "sig",
                "alg": "RS256",
                "n": encode(numbers.n),
                "e": encode(numbers.e),
            }
        ]
    }


def test_cimd_private_key_jwt_accepts_advertised_bare_origin_token_endpoint() -> None:
    """A ChatGPT-style assertion must use the advertised one-slash audience."""

    async def exercise_token_route() -> httpx.Response:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_id = "chatgpt-test-key"
        cimd_document = CIMDDocument(
            client_id=CLIENT_ID,
            redirect_uris=[REDIRECT_URI],
            token_endpoint_auth_method="private_key_jwt",
            jwks=_jwk_for_public_key(private_key.public_key(), key_id),
        )
        client = ProxyDCRClient(
            client_id=CLIENT_ID,
            redirect_uris=[REDIRECT_URI],
            token_endpoint_auth_method="private_key_jwt",
            cimd_document=cimd_document,
        )
        proxy = OAuthProxy(
            upstream_authorization_endpoint="https://idp.example/authorize",
            upstream_token_endpoint="https://idp.example/token",
            upstream_client_id="upstream-client",
            token_verifier=TokenVerifier(),
            base_url=PUBLIC_ORIGIN,
            jwt_signing_key="oauth-compatibility-test-key",
            require_authorization_consent=False,
        )

        async def get_client(_client_id: str) -> ProxyDCRClient:
            return client

        proxy.get_client = get_client  # type: ignore[method-assign]
        routes = proxy.get_routes("/mcp")
        token_route = next(route for route in routes if route.path == "/token")
        metadata_route = next(
            route
            for route in routes
            if route.path == "/.well-known/oauth-authorization-server"
        )
        metadata_transport = httpx.ASGITransport(app=metadata_route.endpoint)
        async with httpx.AsyncClient(
            transport=metadata_transport, base_url=PUBLIC_ORIGIN
        ) as metadata_client:
            metadata_response = await metadata_client.get(metadata_route.path)
        advertised_token_endpoint = metadata_response.json()["token_endpoint"]
        assert advertised_token_endpoint == "https://digikey-gpt.onrender.com/token"
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": CLIENT_ID,
                "sub": CLIENT_ID,
                "aud": advertised_token_endpoint,
                "iat": now,
                "exp": now + 60,
                "jti": "chatgpt-test-assertion",
            },
            private_key,
            algorithm="RS256",
            headers={"kid": key_id},
        )
        form = {
            "grant_type": "authorization_code",
            "code": "unused-code",
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": "unused-verifier",
            "client_assertion_type": (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            ),
            "client_assertion": assertion,
        }
        transport = httpx.ASGITransport(app=token_route.endpoint)
        async with httpx.AsyncClient(
            transport=transport, base_url=PUBLIC_ORIGIN
        ) as http_client:
            return await http_client.post("/token", data=form)

    response = asyncio.run(exercise_token_route())

    # The assertion is valid, so the route reaches authorization-code handling.
    # It must not reject the client as invalid because of a doubled-slash aud.
    assert response.status_code == 401
    assert json.loads(response.content)["error"] == "invalid_grant"
