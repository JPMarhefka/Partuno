from __future__ import annotations

from dataclasses import replace

import pytest

from config import settings
from credentials import (
    CredentialPurpose,
    CredentialUnavailableError,
    EnvironmentCredentialStore,
    Provider,
    provider_configured,
    require_credentials,
)


def test_environment_store_scopes_credentials_by_provider_and_purpose() -> None:
    config = replace(
        settings,
        client_id="owner-client-id",
        client_secret="owner-client-secret",
        mouser_search_api_key="owner-search-key",
        mouser_account_api_key="owner-account-key",
    )
    store = EnvironmentCredentialStore(config)

    assert store.get(
        principal="owner",
        provider=Provider.DIGIKEY,
        purpose=CredentialPurpose.OAUTH_CLIENT,
    ) == {
        "client_id": "owner-client-id",
        "client_secret": "owner-client-secret",
    }
    assert store.get(
        principal="owner",
        provider=Provider.DIGIKEY,
        purpose=CredentialPurpose.SEARCH,
    ) is None
    assert store.get(
        principal="owner",
        provider=Provider.MOUSER,
        purpose=CredentialPurpose.SEARCH,
    ) == {"api_key": "owner-search-key"}
    assert store.get(
        principal="owner",
        provider=Provider.MOUSER,
        purpose=CredentialPurpose.ACCOUNT,
    ) == {"api_key": "owner-account-key"}


def test_mouser_search_and_account_credentials_can_be_enabled_separately() -> None:
    config = replace(
        settings,
        mouser_search_api_key=None,
        mouser_account_api_key="account-only-key",
    )
    store = EnvironmentCredentialStore(config)

    assert provider_configured(
        Provider.MOUSER,
        CredentialPurpose.SEARCH,
        store=store,
    ) is False
    assert provider_configured(
        Provider.MOUSER,
        CredentialPurpose.ACCOUNT,
        store=store,
    ) is True
    assert store.get(
        principal="owner",
        provider=Provider.MOUSER,
        purpose=CredentialPurpose.OAUTH_CLIENT,
    ) is None


def test_missing_credentials_have_a_stable_provider_error() -> None:
    config = replace(
        settings,
        client_id=None,
        client_secret=None,
        mouser_search_api_key=None,
        mouser_account_api_key=None,
    )
    store = EnvironmentCredentialStore(config)

    with pytest.raises(CredentialUnavailableError) as raised:
        require_credentials(
            store,
            principal="owner",
            provider=Provider.MOUSER,
            purpose=CredentialPurpose.SEARCH,
        )

    assert raised.value.error_type == "provider_not_configured"
    assert raised.value.provider is Provider.MOUSER
    assert raised.value.distributor == "mouser"
