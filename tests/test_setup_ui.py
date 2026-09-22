"""Bank-feed setup UI page render (respx; no live Manager, no live Google).

`_require_session` is monkeypatched rather than forging a real OAuth cookie
-- these tests are about the page rendering correctly given the provider
states, not about the login flow itself.
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx
from starlette.requests import Request

from manager_mcp import setup_ui
from manager_mcp.bank_feed_providers.basiq import (
    ACCOUNT_LINKS_ENV,
    BASIQ_PASSWORD_ENV,
    BASIQ_USERNAME_ENV,
    DEDUP_FIELD_ENV,
)

BASE = "http://example.test/api2"


def _request(path: str = setup_ui.PAGE_PATH) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "client": ("test", 0),
        "server": ("example.test", 80),
        "scheme": "http",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGER_API_URL", BASE)
    monkeypatch.setenv("MANAGER_API_KEY", "k")
    monkeypatch.setenv("MANAGER_UI_USERNAME", "mcp")
    monkeypatch.setenv("MANAGER_UI_PASSWORD", "secret")
    monkeypatch.setattr(setup_ui, "_require_session", lambda request: "ops@example.test")


def _mock_manager(bank_accounts: list[dict] | None = None) -> None:
    respx.get(re.compile(r"http://example\.test/api2/bank-and-cash-accounts")).mock(
        return_value=httpx.Response(
            200,
            json={
                "business": {"name": "Test Co"},
                "bankAndCashAccounts": bank_accounts or [],
            },
        )
    )
    respx.get(re.compile(r"http://example\.test/api4/receipt-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(re.compile(r"http://example\.test/api4/payment-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )


@pytest.mark.asyncio
@respx.mock
async def test_page_renders_with_nothing_configured() -> None:
    _mock_manager()
    response = await setup_ui.bank_feeds_page(_request())
    assert response.status_code == 200
    body = response.body.decode()
    assert "provider-picker" in body
    assert "Not set up" in body
    assert "Aussie Bank Feeds (Basiq)" in body
    assert 'data-provider="basiq"' in body
    # No configured provider to default to -- falls back to the first (and
    # only) real one, with the "Other..." option available for anything not
    # built in.
    assert 'value="__other__"' in body


@pytest.mark.asyncio
@respx.mock
async def test_page_shows_basiq_as_active_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BASIQ_USERNAME_ENV, "someone")
    monkeypatch.setenv(BASIQ_PASSWORD_ENV, "hunter2")
    monkeypatch.setenv(ACCOUNT_LINKS_ENV, '{"acct-1": "basiq-1"}')
    monkeypatch.setenv(DEDUP_FIELD_ENV, "some-guid")
    _mock_manager(bank_accounts=[{"key": "acct-1", "name": "Everyday"}])

    response = await setup_ui.bank_feeds_page(_request())
    body = response.body.decode()
    assert response.status_code == 200
    # Real username shown in place, never the password:
    assert "someone" in body
    assert "hunter2" not in body
    # The linked Basiq account id is prefilled into its row:
    assert 'value="basiq-1"' in body
    assert "Active" in body


@pytest.mark.asyncio
@respx.mock
async def test_dedup_field_falls_back_to_text_input_with_callout_when_nothing_detected() -> None:
    _mock_manager()
    response = await setup_ui.bank_feeds_page(_request())
    body = response.body.decode()
    assert "Nothing to pick from yet" in body
    assert "callout" in body


@pytest.mark.asyncio
@respx.mock
async def test_page_offers_other_option_with_plugin_instructions() -> None:
    _mock_manager()
    response = await setup_ui.bank_feeds_page(_request())
    body = response.body.decode()
    assert 'value="__other__"' in body
    assert 'data-provider="__other__"' in body
    assert "BankFeedProvider" in body
    assert "PROVIDERS" in body
    assert "registry.py" in body


@pytest.mark.asyncio
async def test_page_redirects_to_login_when_not_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_ui, "_require_session", lambda request: None)
    response = await setup_ui.bank_feeds_page(_request())
    assert response.status_code in (302, 307)
    assert response.headers["location"].startswith(setup_ui.LOGIN_PATH)


@pytest.mark.asyncio
@respx.mock
async def test_submit_saves_and_confirms(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from manager_mcp.bank_feed_providers import feeds_config

    config_path = tmp_path / "feeds.config"
    monkeypatch.setenv(feeds_config.CONFIG_PATH_ENV, str(config_path))

    class _FakeForm(dict):
        def multi_items(self):
            return self.items()

    async def fake_form(self):
        return _FakeForm({"provider": "basiq", BASIQ_USERNAME_ENV: "newuser"})

    monkeypatch.setattr(Request, "form", fake_form)
    response = await setup_ui.submit(_request())
    assert response.status_code == 200
    assert "Saved" in response.body.decode()
    assert feeds_config.load({feeds_config.CONFIG_PATH_ENV: str(config_path)}) == {
        "MANAGER_MCP_BANK_FEED_PROVIDER": "basiq",
        BASIQ_USERNAME_ENV: "newuser",
    }
