"""sync_bank_feeds MCP tool: banking-scope gating, and directing the agent
to the setup UI when nothing is configured yet."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from manager_mcp.bank_feed_providers.basiq import (
    _BASIQ_API_BASE,
    _COGNITO_URL,
    ACCOUNT_LINKS_ENV,
    BASIQ_PASSWORD_ENV,
    BASIQ_USERNAME_ENV,
    DEDUP_FIELD_ENV,
)
from manager_mcp.server import mcp, register_task_tools, reset_client

BASE = "http://example.test/api2"


async def _call(name: str, arguments: dict | None = None) -> dict:
    result = await mcp.call_tool(name, arguments or {})
    assert not result.is_error, result
    assert result.structured_content is not None
    return result.structured_content


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGER_API_URL", BASE)
    monkeypatch.setenv("MANAGER_API_KEY", "k")
    monkeypatch.setenv("MANAGER_MCP_WRITE_SCOPES", "banking")
    for name in ("MANAGER_MCP_ALLOW_WRITES", "ALLOW_WRITES", "MANAGER_MCP_WRITES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MANAGER_MCP_DELETE_SCOPES", raising=False)
    monkeypatch.delenv("BASIQ_USERNAME", raising=False)
    monkeypatch.delenv("BASIQ_PASSWORD", raising=False)
    monkeypatch.delenv("MANAGER_MCP_BANK_FEED_PROVIDER", raising=False)
    monkeypatch.delenv("MANAGER_MCP_OAUTH_BASE_URL", raising=False)
    reset_client()
    register_task_tools()
    yield
    reset_client()


def test_registered_only_with_banking_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANAGER_MCP_WRITE_SCOPES", raising=False)
    reset_client()
    captured: list[str] = []

    def fake_tool(*_args: object, **kwargs: object):
        def deco(fn: object) -> object:
            captured.append(str(kwargs.get("name") or getattr(fn, "__name__", "")))
            return fn

        return deco

    monkeypatch.setattr(mcp, "tool", fake_tool)
    register_task_tools()
    assert "sync_bank_feeds" not in captured

    monkeypatch.setenv("MANAGER_MCP_WRITE_SCOPES", "banking")
    reset_client()
    captured.clear()
    register_task_tools()
    assert "sync_bank_feeds" in captured


@pytest.mark.asyncio
async def test_sync_without_mcp_user_directs_to_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANAGER_UI_USERNAME", raising=False)
    monkeypatch.delenv("MANAGER_UI_PASSWORD", raising=False)
    monkeypatch.setenv("MANAGER_MCP_OAUTH_BASE_URL", "https://manager-mcp.example.test")
    reset_client()
    out = await _call("sync_bank_feeds")
    assert out["configured"] is False
    assert "MANAGER_UI_USERNAME" in out["error"]
    assert out["setup_url"] == "https://manager-mcp.example.test/setup/bank-feeds"
    assert "setup" in out["hint"].casefold()


@pytest.mark.asyncio
async def test_sync_without_setup_url_falls_back_to_env_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MANAGER_UI_USERNAME", raising=False)
    monkeypatch.delenv("MANAGER_UI_PASSWORD", raising=False)
    monkeypatch.delenv("MANAGER_MCP_OAUTH_BASE_URL", raising=False)
    reset_client()
    out = await _call("sync_bank_feeds")
    assert out["configured"] is False
    assert "setup_url" not in out
    assert "feeds_config" in out["hint"]


@pytest.mark.asyncio
async def test_sync_reports_not_configured_when_no_provider_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the mcp user is present but no provider's own config is set (no
    Basiq credentials, nothing else registered), sync_bank_feeds should
    report configured=false rather than silently doing nothing or raising."""
    monkeypatch.setenv("MANAGER_UI_USERNAME", "mcp")
    monkeypatch.setenv("MANAGER_UI_PASSWORD", "secret")
    monkeypatch.setenv("MANAGER_MCP_OAUTH_BASE_URL", "https://manager-mcp.example.test")
    reset_client()

    out = await _call("sync_bank_feeds")
    assert out["configured"] is False
    assert "provider" not in out
    assert out["setup_url"] == "https://manager-mcp.example.test/setup/bank-feeds"


@pytest.mark.asyncio
@respx.mock
async def test_sync_runs_basiq_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGER_UI_USERNAME", "mcp")
    monkeypatch.setenv("MANAGER_UI_PASSWORD", "secret")
    monkeypatch.setenv(BASIQ_USERNAME_ENV, "u")
    monkeypatch.setenv(BASIQ_PASSWORD_ENV, "p")
    monkeypatch.setenv(ACCOUNT_LINKS_ENV, json.dumps({"acct-key": "basiq-acct-1"}))
    monkeypatch.setenv(DEDUP_FIELD_ENV, "dedup-field-guid")
    reset_client()

    respx.get(f"{BASE}/bank-and-cash-accounts").mock(
        return_value=httpx.Response(
            200, json={"business": {"name": "Test Co"}, "bankAndCashAccounts": []}
        )
    )
    respx.post(_COGNITO_URL).mock(
        return_value=httpx.Response(200, json={"AuthenticationResult": {"AccessToken": "tok"}})
    )
    respx.get(f"{_BASIQ_API_BASE}/accounts/basiq-acct-1/transactions").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("http://example.test/api4/receipt-batch").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get("http://example.test/api4/payment-batch").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    out = await _call("sync_bank_feeds")
    assert out["configured"] is True
    assert out["provider"] == "basiq"
