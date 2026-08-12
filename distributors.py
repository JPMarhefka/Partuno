from __future__ import annotations

from typing import Any, Protocol

from config import Settings, settings
from credentials import (
    CredentialPurpose,
    CredentialStore,
    CredentialUnavailableError,
    EnvironmentCredentialStore,
    credential_store,
)
from distributor_models import DistributorOffer, MouserSearchRequest

CredentialProvider = CredentialStore


class EnvironmentCredentialProvider:
    """Compatibility adapter for the pre-Partuno single-string interface."""

    def __init__(self, config: Settings = settings) -> None:
        self.store = EnvironmentCredentialStore(config)

    def get(
        self,
        *,
        principal: str,
        distributor: str | None = None,
        provider: str | None = None,
        purpose: CredentialPurpose,
    ) -> Any:
        values = self.store.get(
            principal=principal,
            distributor=distributor,
            provider=provider,
            purpose=purpose,
        )
        if isinstance(values, dict) and set(values) == {"api_key"}:
            return values["api_key"]
        return values


credentials = credential_store


class DistributorAdapter(Protocol):
    name: str

    def capabilities(self) -> dict[str, bool]:
        ...

    def health(self) -> dict[str, Any]:
        ...

    def search(
        self,
        request: MouserSearchRequest,
        *,
        principal: str,
        authorization: str | None = None,
    ) -> dict[str, Any]:
        ...

    def exact_offers(
        self,
        manufacturer: str,
        manufacturer_part_number: str,
        quantity: int,
        *,
        principal: str,
        authorization: str | None = None,
    ) -> list[DistributorOffer]:
        ...
