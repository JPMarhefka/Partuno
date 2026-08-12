import os

import pytest

from config import Settings


def test_mcp_requires_secret_and_public_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "client")
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("MCP_BASE_URL", "https://example.test")
    assert Settings.from_env().mcp_enabled is False


def test_partuno_mode_defaults_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PARTUNO_MODE", raising=False)
    assert Settings.from_env().partuno_mode == "local"
    assert Settings.from_env().mcp_enabled is False


def test_partuno_mode_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARTUNO_MODE", "hosted_multi_tenant")
    with pytest.raises(RuntimeError, match="PARTUNO_MODE"):
        Settings.from_env()


def test_mcp_requires_stable_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "client")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://example.test")
    monkeypatch.delenv("MCP_JWT_SIGNING_KEY", raising=False)
    assert Settings.from_env().mcp_enabled is False


def test_mcp_base_url_must_be_https_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "client")
    monkeypatch.setenv("MCP_BASE_URL", "http://example.test/mcp")
    with pytest.raises(RuntimeError, match="HTTPS origin"):
        Settings.from_env()


def test_no_secret_values_are_required_for_readiness_contract() -> None:
    assert os.environ["DIGIKEY_CLIENT_ID"].strip()


def test_mouser_credentials_are_optional_and_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "client")
    monkeypatch.delenv("MOUSER_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("MOUSER_ACCOUNT_API_KEY", raising=False)
    unconfigured = Settings.from_env()
    assert unconfigured.mouser_search_enabled is False
    assert unconfigured.mouser_account_enabled is False

    monkeypatch.setenv("MOUSER_SEARCH_API_KEY", "search")
    search_only = Settings.from_env()
    assert search_only.mouser_search_enabled is True
    assert search_only.mouser_account_enabled is False

    monkeypatch.setenv("MOUSER_ACCOUNT_API_KEY", "account")
    configured = Settings.from_env()
    assert configured.mouser_search_enabled is True
    assert configured.mouser_account_enabled is True


def test_provider_credentials_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("MOUSER_SEARCH_API_KEY", "search")
    monkeypatch.delenv("MOUSER_ACCOUNT_API_KEY", raising=False)

    configured = Settings.from_env()

    assert configured.digikey_enabled is False
    assert configured.mouser_search_enabled is True
    assert configured.mouser_account_enabled is False
    assert configured.any_provider_enabled is True


def test_mouser_local_budgets_do_not_exceed_published_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "client")
    monkeypatch.setenv("MOUSER_MINUTE_LIMIT", "999")
    monkeypatch.setenv("MOUSER_DAILY_LIMIT", "999999")
    configured = Settings.from_env()
    assert configured.mouser_minute_limit == 30
    assert configured.mouser_daily_limit == 1000


def test_pcn_cache_ttl_is_bounded_and_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "client")
    monkeypatch.setenv("PCN_CACHE_SECONDS", "9999")
    assert Settings.from_env().pcn_cache_seconds == 900

    monkeypatch.setenv("PCN_CACHE_SECONDS", "0")
    assert Settings.from_env().pcn_cache_seconds == 0
