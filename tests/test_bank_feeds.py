"""Bank-feed sync trigger and interval parsing (respx; no live Manager)."""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest
import respx

import manager_mcp.bank_feeds as bank_feeds
from manager_mcp.bank_feeds import (
    DEDUP_FIELD,
    INTERVAL_ENV,
    BankFeedSyncError,
    _discover_feed_action_urls,
    _existing_dedup_keys,
    _is_inert_list_page,
    _normalize_description,
    _sync_start_date,
    bank_feed_sync_interval_seconds,
    bank_feed_sync_url,
    check_for_new_transactions,
    run_bank_feed_sync_loop,
    sync_aussie_bank_feeds,
)
from manager_mcp.client import ConfigError, ManagerClient

BASE = "http://example.test/api2"
LIST_URL = "http://example.test/api2/bank-and-cash-accounts"
SYNC_URL = "http://example.test/check-for-new-transactions"
SYNC_URL_RE = re.compile(r"http://example\.test/check-for-new-transactions")
TAB_URL_RE = re.compile(r"http://example\.test/bank-and-cash-accounts(?:\?|$)")
LIST_HTML = (
    "<!DOCTYPE html><html><head>"
    "<title>Lilith Pty Ltd | Bank and Cash Accounts</title></head>"
    "<body>list</body></html>"
)
IMPORTED_HTML = "<html><body>Done. 3 new transactions imported.</body></html>"


def _client() -> ManagerClient:
    return ManagerClient(BASE, "k", ui_username="mcp", ui_password="secret")


def _mock_business_list() -> None:
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json={"business": {"name": "Test Co"}, "bankAndCashAccounts": []},
        )
    )


def test_interval_unset_disables() -> None:
    assert bank_feed_sync_interval_seconds({}) is None


def test_interval_hourly() -> None:
    assert bank_feed_sync_interval_seconds({INTERVAL_ENV: "3600"}) == 3600.0


def test_sync_url_is_root_action_not_nested_tab() -> None:
    assert bank_feed_sync_url("http://example.test/api2") == SYNC_URL
    assert (
        bank_feed_sync_url("http://manager:8080/api2")
        == "http://manager:8080/check-for-new-transactions"
    )
    with_file = bank_feed_sync_url("http://example.test/api2", business_name="Test Co")
    assert with_file.startswith(SYNC_URL + "?")
    assert "/bank-and-cash-accounts/" not in with_file


def test_inert_list_page_detected() -> None:
    response = httpx.Response(
        200, headers={"content-type": "text/html; charset=UTF-8"}, text=LIST_HTML
    )
    assert _is_inert_list_page(response)
    assert _is_inert_list_page(httpx.Response(200, text=LIST_HTML))
    imported = httpx.Response(
        200,
        headers={"content-type": "text/html"},
        text="<title>x</title> 3 new transactions imported",
    )
    assert not _is_inert_list_page(imported)


def test_discover_feed_action_urls() -> None:
    html = """
    <a href="/check-for-new-transactions?abc">Check</a>
    <button hx-post="/api4/custom-button-html?q=1">Aussie Bank Feeds</button>
    <a href="https://basiq.manager.io/sync">external</a>
    """
    urls = _discover_feed_action_urls(html, "http://example.test")
    assert "http://example.test/check-for-new-transactions?abc" in urls
    assert "http://example.test/api4/custom-button-html?q=1" in urls
    assert "https://basiq.manager.io/sync" in urls


@pytest.mark.asyncio
@respx.mock
async def test_check_gets_root_action_as_mcp_user() -> None:
    _mock_business_list()
    route = respx.get(SYNC_URL_RE).mock(
        return_value=httpx.Response(200, text=IMPORTED_HTML)
    )
    out = await check_for_new_transactions(_client())
    assert out["status"] == "ok"
    assert out["method"] == "GET"
    request = route.calls.last.request
    assert request.headers.get("Authorization", "").startswith("Basic ")
    assert request.headers.get("HX-Request") == "true"
    assert "x-api-key" not in {k.lower() for k in request.headers}
    assert request.url.path == "/check-for-new-transactions"


@pytest.mark.asyncio
async def test_check_requires_mcp_user() -> None:
    with pytest.raises(ConfigError, match="MANAGER_UI_USERNAME"):
        await check_for_new_transactions(ManagerClient(BASE, "k"))


@pytest.mark.asyncio
@respx.mock
async def test_check_posts_when_get_returns_list_page() -> None:
    _mock_business_list()
    respx.get(SYNC_URL_RE).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, text=LIST_HTML
        )
    )
    post_route = respx.post(SYNC_URL_RE).mock(
        return_value=httpx.Response(200, text=IMPORTED_HTML)
    )
    out = await check_for_new_transactions(_client())
    assert out["method"] == "POST"
    assert post_route.called


@pytest.mark.asyncio
@respx.mock
async def test_list_page_only_is_failure() -> None:
    _mock_business_list()
    respx.get(SYNC_URL_RE).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, text=LIST_HTML
        )
    )
    respx.post(SYNC_URL_RE).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, text=LIST_HTML
        )
    )
    respx.get(TAB_URL_RE).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, text=LIST_HTML
        )
    )
    with pytest.raises(BankFeedSyncError, match="did not import"):
        await check_for_new_transactions(_client())


@pytest.mark.asyncio
@respx.mock
async def test_follows_same_origin_button_from_tab() -> None:
    _mock_business_list()
    respx.get(SYNC_URL_RE).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, text=LIST_HTML
        )
    )
    respx.post(SYNC_URL_RE).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, text=LIST_HTML
        )
    )
    tab_html = LIST_HTML.replace(
        "list",
        '<a href="/api4/custom-button-html?q=feed">Aussie Bank Feeds</a>',
    )
    respx.get(TAB_URL_RE).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=tab_html)
    )
    respx.get(re.compile(r"http://example\.test/api4/custom-button-html")).mock(
        return_value=httpx.Response(200, text=IMPORTED_HTML)
    )
    out = await check_for_new_transactions(_client())
    assert out["status"] == "ok"
    assert "custom-button-html" in out["url"]


@pytest.mark.asyncio
@respx.mock
async def test_check_raises_on_login_redirect() -> None:
    _mock_business_list()
    respx.get(SYNC_URL_RE).mock(
        return_value=httpx.Response(302, headers={"location": "/login"})
    )
    with pytest.raises(BankFeedSyncError, match="login"):
        await check_for_new_transactions(_client())


@pytest.mark.asyncio
@respx.mock
async def test_existing_dedup_keys_parses_nested_item_shape() -> None:
    """`/api4/{entity}` list responses wrap each record as {"key": ..., "item": {...}}
    -- the real fields (and customFields2) are nested under "item", not at the top
    level of each list entry. A flatter mock here would hide the regression this
    guards: a wrong shape assumption made every transaction look new every sync,
    duplicating every transaction on every scheduled run."""
    respx.get(re.compile(r"http://example\.test/api4/receipt-batch")).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "key": "r1",
                        "item": {
                            "date": "2026-08-19T00:00:00",
                            "fixedTotalAmount": 4290.0,
                            "description": "one",
                            "customFields2": {"strings": {DEDUP_FIELD: "basiq-txn-1"}},
                        },
                    },
                    {
                        "key": "r2",
                        "item": {
                            "date": "2026-08-20T00:00:00",
                            "fixedTotalAmount": 10.0,
                            "description": "two",
                            "customFields2": None,
                        },
                    },
                ]
            },
        )
    )
    respx.get(re.compile(r"http://example\.test/api4/payment-batch")).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "key": "p1",
                        "item": {
                            "date": "2026-08-21T00:00:00",
                            "fixedTotalAmount": 122.39,
                            "description": "Transfer to SAV xxxx4075",
                            "customFields2": {"strings": {DEDUP_FIELD: "basiq-txn-2"}},
                        },
                    }
                ]
            },
        )
    )
    ids, fuzzy, latest_date = await _existing_dedup_keys(_client(), "acct-key")
    assert ids == {"basiq-txn-1", "basiq-txn-2"}
    assert ("2026-08-20", 10.0, "two") in fuzzy["receipt-batch"]
    assert ("2026-08-21", 122.39, "transfer to sav #") in fuzzy["payment-batch"]
    assert latest_date == "2026-08-21"


@pytest.mark.asyncio
@respx.mock
async def test_existing_dedup_keys_matches_despite_different_masking() -> None:
    """The same real transaction can come back with a differently masked account
    number depending on which login pulled the feed -- confirmed live 2026-08-31
    ("SAV xxxx4075" vs "SAV 12114075" for the same transfer). Masked-digit runs
    must normalize to the same fuzzy key regardless of how they're masked."""
    respx.get(re.compile(r"http://example\.test/api4/receipt-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(re.compile(r"http://example\.test/api4/payment-batch")).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "key": "p1",
                        "item": {
                            "date": "2026-08-19T00:00:00",
                            "fixedTotalAmount": 122.39,
                            "description": "Transfer to SAV 12114075 to G J Steel - Reimburse petrol",
                            "customFields2": None,
                        },
                    }
                ]
            },
        )
    )
    _, fuzzy, _ = await _existing_dedup_keys(_client(), "acct-key")
    key = (
        "2026-08-19",
        122.39,
        _normalize_description("Transfer to SAV xxxx4075 to G J Steel - Reimburse petrol"),
    )
    assert key in fuzzy["payment-batch"]


def test_sync_start_date_is_lookback_days_before_latest() -> None:
    assert _sync_start_date("2026-08-26") == "2026-08-19"


def test_sync_start_date_none_when_no_prior_transactions() -> None:
    assert _sync_start_date(None) is None


@pytest.mark.asyncio
@respx.mock
async def test_sync_ignores_basiq_history_before_lookback_window(monkeypatch) -> None:
    """A Basiq transaction that predates the existing bank account history by more
    than LOOKBACK_DAYS must never reach the write step, even if it doesn't match
    any existing dedup key -- this is what caused hundreds of already-entered
    historical transactions to get recreated live 2026-08-31."""
    monkeypatch.setenv("BASIQ_USERNAME", "u")
    monkeypatch.setenv("BASIQ_PASSWORD", "p")
    monkeypatch.setattr(bank_feeds, "BANK_ACCOUNT_LINKS", {"acct-key": "basiq-acct-1"})

    respx.post(bank_feeds._COGNITO_URL).mock(
        return_value=httpx.Response(
            200, json={"AuthenticationResult": {"AccessToken": "tok"}}
        )
    )
    respx.get(f"{bank_feeds._BASIQ_API_BASE}/accounts/basiq-acct-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "old-1",
                        "postDate": "2026-08-01T00:00:00.000Z",
                        "amount": "50.00",
                        "description": "old transaction outside window",
                        "status": "posted",
                    },
                    {
                        "id": "new-1",
                        "postDate": "2026-08-27T00:00:00.000Z",
                        "amount": "100.00",
                        "description": "new transaction inside window",
                        "status": "posted",
                    },
                ]
            },
        )
    )
    respx.get(re.compile(r"http://example\.test/api4/receipt-batch\?")).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "key": "existing",
                        "item": {
                            "date": "2026-08-26T00:00:00",
                            "fixedTotalAmount": 5.0,
                            "description": "unrelated existing receipt",
                            "customFields2": None,
                        },
                    }
                ]
            },
        )
    )
    respx.get(re.compile(r"http://example\.test/api4/payment-batch\?")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    write_route = respx.post("http://example.test/api4/receipt-batch").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await sync_aussie_bank_feeds(_client())

    assert result["accounts"][0]["fetched"] == 1
    assert result["accounts"][0]["new"] == 1
    assert write_route.called
    written = write_route.calls.last.request.content
    import json as _json

    body = _json.loads(written)
    assert len(body["values"]) == 1
    assert body["values"][0]["customFields2"]["strings"][DEDUP_FIELD] == "new-1"


@pytest.mark.asyncio
async def test_loop_logs_failure_and_continues() -> None:
    calls = {"n": 0}

    async def sync() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise BankFeedSyncError("nope")
        raise asyncio.CancelledError()

    async def sleep(seconds: float) -> None:
        if calls["n"] >= 2:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_bank_feed_sync_loop(
            interval=1, sync=sync, sleep=sleep, startup_delay=0
        )
    assert calls["n"] >= 2
