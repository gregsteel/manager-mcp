"""Aussie Bank Feeds / Basiq provider (respx; no live Manager or Basiq)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

import manager_mcp.bank_feed_providers.basiq as basiq
from manager_mcp.bank_feed_providers.basiq import (
    ACCOUNT_LINKS_ENV,
    BASIQ_PASSWORD_ENV,
    BASIQ_USERNAME_ENV,
    DEDUP_FIELD_ENV,
    AussieBankFeedsSyncError,
    BasiqProvider,
    _existing_dedup_keys,
    _normalize_description,
    _sync_start_date,
    account_links,
    dedup_field,
    sync_aussie_bank_feeds,
)
from manager_mcp.client import ConfigError, ManagerClient

BASE = "http://example.test/api2"
DEDUP_FIELD = "ad18c1cc-aa92-49f9-ba18-8c98f9a76d29"


def _client() -> ManagerClient:
    return ManagerClient(BASE, "k", ui_username="mcp", ui_password="secret")


def _env(monkeypatch, **extra: str) -> None:
    monkeypatch.setenv(BASIQ_USERNAME_ENV, "u")
    monkeypatch.setenv(BASIQ_PASSWORD_ENV, "p")
    monkeypatch.setenv(ACCOUNT_LINKS_ENV, json.dumps({"acct-key": "basiq-acct-1"}))
    monkeypatch.setenv(DEDUP_FIELD_ENV, DEDUP_FIELD)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_account_links_requires_valid_json() -> None:
    with pytest.raises(ConfigError, match="required"):
        account_links({})
    with pytest.raises(ConfigError, match="not valid JSON"):
        account_links({ACCOUNT_LINKS_ENV: "not json"})
    with pytest.raises(ConfigError, match="string -> string"):
        account_links({ACCOUNT_LINKS_ENV: json.dumps({"a": 1})})
    assert account_links({ACCOUNT_LINKS_ENV: json.dumps({"a": "b"})}) == {"a": "b"}


def test_dedup_field_required() -> None:
    with pytest.raises(ConfigError, match=DEDUP_FIELD_ENV):
        dedup_field({})
    assert dedup_field({DEDUP_FIELD_ENV: "abc"}) == "abc"


def test_sync_start_date_is_lookback_days_before_latest() -> None:
    assert _sync_start_date("2026-08-26", 7) == "2026-08-19"


def test_sync_start_date_none_when_no_prior_transactions() -> None:
    assert _sync_start_date(None, 7) is None


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
    ids, fuzzy, latest_date = await _existing_dedup_keys(
        _client(), "acct-key", DEDUP_FIELD, "Test Co"
    )
    assert ids == {"basiq-txn-1", "basiq-txn-2"}
    assert ("2026-08-20", 10.0, "two") in fuzzy["receipt-batch"]
    assert ("2026-08-21", 122.39, "transfer to sav #") in fuzzy["payment-batch"]
    assert latest_date == "2026-08-21"


@pytest.mark.asyncio
@respx.mock
async def test_existing_dedup_keys_falls_back_to_lines_sum_when_fixed_total_is_zero() -> None:
    """`fixedTotalAmount`/`fixedTotal` are /api4-only; an /api2 PUT (e.g.
    `update_payment` reconciling the row) resets them to 0/false on the
    underlying record even though `Lines`/`CustomFields2` survive the merge.
    Trusting `fixedTotalAmount` alone made an update-touched bank-feed
    payment fuzzy-invisible and it duplicated on the next sync (live
    incident, 2026-09-20) -- the fuzzy amount must fall back to summing
    `lines[*].amount` when `fixedTotalAmount` reads back as 0."""
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
                            "date": "2026-09-18T00:00:00",
                            "fixedTotal": False,
                            "fixedTotalAmount": 0,
                            "description": "VISA-LINKT MELBOURNE MELBOURNE AU#9435(Ref.xxxxxxxx1863)",
                            "customFields2": None,
                            "lines": [{"amount": 18.67}],
                        },
                    }
                ]
            },
        )
    )
    _, fuzzy, _ = await _existing_dedup_keys(_client(), "acct-key", DEDUP_FIELD, "Test Co")
    key = (
        "2026-09-18",
        18.67,
        _normalize_description("VISA-LINKT MELBOURNE MELBOURNE AU#9435(Ref.xxxxxxxx1863)"),
    )
    assert key in fuzzy["payment-batch"]


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
                            "description": (
                                "Transfer to SAV 12114075 to G J Steel - Reimburse petrol"
                            ),
                            "customFields2": None,
                        },
                    }
                ]
            },
        )
    )
    _, fuzzy, _ = await _existing_dedup_keys(_client(), "acct-key", DEDUP_FIELD, "Test Co")
    key = (
        "2026-08-19",
        122.39,
        _normalize_description("Transfer to SAV xxxx4075 to G J Steel - Reimburse petrol"),
    )
    assert key in fuzzy["payment-batch"]


@pytest.mark.asyncio
async def test_sync_requires_basiq_credentials(monkeypatch) -> None:
    monkeypatch.delenv(BASIQ_USERNAME_ENV, raising=False)
    monkeypatch.delenv(BASIQ_PASSWORD_ENV, raising=False)
    with pytest.raises(ConfigError, match=BASIQ_USERNAME_ENV):
        await sync_aussie_bank_feeds(_client())


@pytest.mark.asyncio
@respx.mock
async def test_sync_ignores_basiq_history_before_lookback_window(monkeypatch) -> None:
    """A Basiq transaction that predates the existing bank account history by more
    than the lookback window must never reach the write step, even if it doesn't
    match any existing dedup key -- this is what caused hundreds of already-entered
    historical transactions to get recreated live 2026-08-31."""
    _env(monkeypatch)

    respx.get(re.compile(r"http://example\.test/api2/bank-and-cash-accounts")).mock(
        return_value=httpx.Response(200, json={"business": {"name": "Test Co"}})
    )
    respx.post(basiq._COGNITO_URL).mock(
        return_value=httpx.Response(
            200, json={"AuthenticationResult": {"AccessToken": "tok"}}
        )
    )
    respx.get(f"{basiq._BASIQ_API_BASE}/accounts/basiq-acct-1/transactions").mock(
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
    body = json.loads(written)
    assert len(body["values"]) == 1
    assert body["values"][0]["customFields2"]["strings"][DEDUP_FIELD] == "new-1"


@pytest.mark.asyncio
@respx.mock
async def test_list_basiq_accounts() -> None:
    respx.post(basiq._COGNITO_URL).mock(
        return_value=httpx.Response(200, json={"AuthenticationResult": {"AccessToken": "tok"}})
    )
    respx.get(f"{basiq._BASIQ_API_BASE}/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "acct-1", "name": "Everyday", "institution": {"shortName": "ANZ"}},
                    {"id": "acct-2", "accountName": "Savings"},
                ]
            },
        )
    )
    accounts = await basiq.list_basiq_accounts("u", "p")
    assert accounts == [
        {"id": "acct-1", "name": "Everyday (ANZ)"},
        {"id": "acct-2", "name": "Savings"},
    ]


@pytest.mark.asyncio
@respx.mock
async def test_list_basiq_accounts_raises_on_failure() -> None:
    respx.post(basiq._COGNITO_URL).mock(
        return_value=httpx.Response(200, json={"AuthenticationResult": {"AccessToken": "tok"}})
    )
    respx.get(f"{basiq._BASIQ_API_BASE}/accounts").mock(return_value=httpx.Response(404))
    with pytest.raises(AussieBankFeedsSyncError):
        await basiq.list_basiq_accounts("u", "p")


@pytest.mark.asyncio
@respx.mock
async def test_setup_state_shows_real_username_but_never_the_password() -> None:
    """The username field must pre-fill the *actual* value (it isn't secret)
    so an operator can see and edit it in place; the password field must
    never carry the real secret back to the browser -- only a placeholder
    hint via `placeholder`, with `value` left empty. Regression test for a
    bug where the username field was pre-filled with the literal string
    "(already set)" as its submittable value, which would silently save
    that text as the username if the operator didn't notice and clear it."""
    respx.get(re.compile(r"http://example\.test/api2/bank-and-cash-accounts")).mock(
        return_value=httpx.Response(
            200, json={"business": {"name": "Test Co"}, "bankAndCashAccounts": []}
        )
    )
    respx.get(re.compile(r"http://example\.test/api4/receipt-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(re.compile(r"http://example\.test/api4/payment-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    env = {
        BASIQ_USERNAME_ENV: "real-username",
        BASIQ_PASSWORD_ENV: "super-secret",
    }
    state = await BasiqProvider().setup_state(_client(), env)
    by_key = {f.key: f for f in state.fields}

    username_field = by_key[BASIQ_USERNAME_ENV]
    assert username_field.kind == "text"
    assert username_field.value == "real-username"

    password_field = by_key[BASIQ_PASSWORD_ENV]
    assert password_field.kind == "password"
    assert password_field.value == ""
    assert "super-secret" not in password_field.value
    assert "super-secret" not in password_field.placeholder
    assert password_field.placeholder == "•" * 12


@pytest.mark.asyncio
@respx.mock
async def test_setup_state_pins_saved_dedup_field_even_if_not_a_candidate() -> None:
    """A previously saved dedup field key should still show as selected in
    the dropdown even when the live sample of receipts/payments didn't
    happen to surface it as a detected candidate -- otherwise the page
    looks like nothing is configured when something is."""
    respx.get(re.compile(r"http://example\.test/api2/bank-and-cash-accounts")).mock(
        return_value=httpx.Response(
            200, json={"business": {"name": "Test Co"}, "bankAndCashAccounts": []}
        )
    )
    respx.get(re.compile(r"http://example\.test/api4/receipt-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(re.compile(r"http://example\.test/api4/payment-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    env = {DEDUP_FIELD_ENV: DEDUP_FIELD}
    state = await BasiqProvider().setup_state(_client(), env)
    dedup = next(f for f in state.fields if f.key == DEDUP_FIELD_ENV)
    assert dedup.value == DEDUP_FIELD
    pinned = next(o for o in dedup.options if o["key"] == DEDUP_FIELD)
    # Selected-ness already conveys "this is what's saved" -- no extra
    # "(currently saved)" annotation needed on top.
    assert pinned["name"] == DEDUP_FIELD


@pytest.mark.asyncio
@respx.mock
async def test_setup_state_defaults_dedup_field_when_exactly_one_candidate() -> None:
    """Nothing saved yet, but only one custom field is in use on existing
    receipts/payments -- there's no real choice to make, so it should be
    pre-selected rather than left on '-- choose --'."""
    respx.get(re.compile(r"http://example\.test/api2/bank-and-cash-accounts")).mock(
        return_value=httpx.Response(
            200, json={"business": {"name": "Test Co"}, "bankAndCashAccounts": []}
        )
    )
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
                    }
                ]
            },
        )
    )
    respx.get(re.compile(r"http://example\.test/api4/payment-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    state = await BasiqProvider().setup_state(_client(), {})
    dedup = next(f for f in state.fields if f.key == DEDUP_FIELD_ENV)
    assert dedup.value == DEDUP_FIELD


@pytest.mark.asyncio
@respx.mock
async def test_setup_state_prefills_account_link_rows_from_saved_config() -> None:
    respx.get(re.compile(r"http://example\.test/api2/bank-and-cash-accounts")).mock(
        return_value=httpx.Response(
            200,
            json={
                "business": {"name": "Test Co"},
                "bankAndCashAccounts": [{"key": "acct-1", "name": "Everyday"}],
            },
        )
    )
    respx.get(re.compile(r"http://example\.test/api4/receipt-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(re.compile(r"http://example\.test/api4/payment-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    env = {ACCOUNT_LINKS_ENV: json.dumps({"acct-1": "basiq-1"})}
    state = await BasiqProvider().setup_state(_client(), env)
    links = next(f for f in state.fields if f.key == ACCOUNT_LINKS_ENV)
    row = next(o for o in links.options if o["key"] == "acct-1")
    assert row["value"] == "basiq-1"


@pytest.mark.asyncio
@respx.mock
async def test_setup_state_looks_up_linked_basiq_account_names_when_credentials_saved() -> None:
    """When Basiq credentials and an account link are already saved, the
    friendly Basiq account name should be looked up server-side on page
    load -- not only after the operator manually clicks 'Detect Basiq
    accounts', which they'd have no reason to do for a link that already
    works."""
    respx.get(re.compile(r"http://example\.test/api2/bank-and-cash-accounts")).mock(
        return_value=httpx.Response(
            200,
            json={
                "business": {"name": "Test Co"},
                "bankAndCashAccounts": [{"key": "acct-1", "name": "Everyday"}],
            },
        )
    )
    respx.get(re.compile(r"http://example\.test/api4/receipt-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(re.compile(r"http://example\.test/api4/payment-batch")).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.post(basiq._COGNITO_URL).mock(
        return_value=httpx.Response(200, json={"AuthenticationResult": {"AccessToken": "tok"}})
    )
    respx.get(f"{basiq._BASIQ_API_BASE}/accounts").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "basiq-1", "name": "Lilith Pty Ltd"}]}
        )
    )

    env = {
        BASIQ_USERNAME_ENV: "u",
        BASIQ_PASSWORD_ENV: "p",
        ACCOUNT_LINKS_ENV: json.dumps({"acct-1": "basiq-1"}),
    }
    state = await BasiqProvider().setup_state(_client(), env)
    links = next(f for f in state.fields if f.key == ACCOUNT_LINKS_ENV)
    row = next(o for o in links.options if o["key"] == "acct-1")
    assert row["value_label"] == "Lilith Pty Ltd"


