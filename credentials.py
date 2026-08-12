from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol

from config import Settings, settings
from identity import LOCAL_PRINCIPAL


class Provider(StrEnum):
    DIGIKEY = "digikey"
    MOUSER = "mouser"


class CredentialPurpose(StrEnum):
    SEARCH = "search"
    ACCOUNT = "account"
    OAUTH_CLIENT = "oauth_client"

    # Compatibility aliases for the pre-Partuno credential interface.
    catalog = SEARCH
    account = ACCOUNT


class CredentialStore(Protocol):
    def get(
        self,
        *,
        principal: str,
        provider: Provider,
        purpose: CredentialPurpose,
    ) -> Mapping[str, str] | None:
        """Return provider credentials without exposing secret values to tools."""


class CredentialUnavailableError(RuntimeError):
    """Raised when a provider capability has no configured user-owned credential."""

    error_type = "provider_not_configured"

    def __init__(self, provider: Provider | str, purpose: CredentialPurpose) -> None:
        resolved_provider = (
            provider if isinstance(provider, Provider) else Provider(str(provider))
        )
        resolved_purpose = (
            purpose
            if isinstance(purpose, CredentialPurpose)
            else CredentialPurpose(str(purpose))
        )
        super().__init__(
            f"{resolved_provider.value} {resolved_purpose.value} credentials are not configured"
        )
        self.provider = resolved_provider
        # Keep the old field available to callers that still use distributor
        # terminology while the public error contract uses provider.
        self.distributor = resolved_provider.value
        self.purpose = resolved_purpose


@dataclass(frozen=True, slots=True)
class EnvironmentCredentialStore:
    """Load user-owned credentials from the process environment via Settings."""

    config: Settings = settings

    def get(
        self,
        *,
        principal: str,
        provider: Provider | str | None = None,
        purpose: CredentialPurpose,
        distributor: str | None = None,
    ) -> Mapping[str, str] | None:
        # principal is intentionally retained even though local and
        # remote_single_user modes currently use one environment-backed store.
        del principal
        resolved_provider = provider if provider is not None else distributor
        if resolved_provider is None:
            raise ValueError("provider is required")
        if not isinstance(resolved_provider, Provider):
            resolved_provider = Provider(str(resolved_provider))
        if not isinstance(purpose, CredentialPurpose):
            purpose = CredentialPurpose(str(purpose))

        if resolved_provider is Provider.DIGIKEY:
            if purpose is not CredentialPurpose.OAUTH_CLIENT:
                # DigiKey access/refresh tokens belong to the authenticated
                # authorization session and are supplied to request methods;
                # they are never stored in this environment store.
                return None
            values = {
                key: value
                for key, value in {
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                }.items()
                if value
            }
            return values or None

        if resolved_provider is Provider.MOUSER:
            if purpose is CredentialPurpose.SEARCH:
                value = self.config.mouser_search_api_key
            elif purpose is CredentialPurpose.ACCOUNT:
                value = self.config.mouser_account_api_key
            else:
                return None
            return {"api_key": value} if value else None

        return None


def require_credentials(
    store: CredentialStore,
    *,
    principal: str,
    provider: Provider,
    purpose: CredentialPurpose,
) -> Mapping[str, str]:
    values = store.get(
        principal=principal,
        provider=provider,
        purpose=purpose,
    )
    if not values:
        raise CredentialUnavailableError(provider, purpose)
    return values


def credential_value(
    values: Mapping[str, str] | str,
    field: str,
) -> str | None:
    """Read a field while tolerating legacy single-string test providers."""
    if isinstance(values, str):
        return values
    value = values.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def provider_configured(
    provider: Provider,
    purpose: CredentialPurpose,
    *,
    principal: str = LOCAL_PRINCIPAL,
    store: CredentialStore | None = None,
) -> bool:
    active_store = store or credential_store
    return bool(
        active_store.get(
            principal=principal,
            provider=provider,
            purpose=purpose,
        )
    )


credential_store = EnvironmentCredentialStore(settings)
