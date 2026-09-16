"""Aussie Bank Feeds / Basiq provider.

Reverse-engineered 2026-08-31 from the real requests Manager's own frontend
fires when you click "Sync All Linked" in the Aussie Bank Feeds modal (Bank
and Cash Accounts > an account > Aussie Bank Feeds, which is just an
`<iframe src="https://aussiebankfeeds.com/sync">`). That page authenticates
against its own AWS Cognito user pool (unrelated to Manager's own login)
and, once you click sync, its parent-window JS reads new transactions from
Basiq then posts them straight into Manager's `/api4/receipt-batch`
(credits) and `/api4/payment-batch` (debits) using the browser's own
Manager session. Confirmed end-to-end live: Cognito USER_PASSWORD_AUTH
login works with real credentials, `/api4` accepts the same HTTP Basic Auth
`raw_basic` already uses for UI actions (see `ManagerClient.raw_basic`'s
`json_body` param), and Basiq's API 403s on a bare urllib/httpx request
(Cloudflare bot detection) unless given a browser-like User-Agent/Referer.

Every business using this provider must configure:

- `BASIQ_USERNAME` / `BASIQ_PASSWORD` -- the Aussie Bank Feeds login.
- `MANAGER_MCP_BASIQ_ACCOUNT_LINKS` -- a JSON object mapping each Manager
  bank-and-cash-account key to its linked Basiq account id, read off the
  table at https://aussiebankfeeds.com/sync. There's no API to discover
  this mapping automatically.
- `MANAGER_MCP_BASIQ_DEDUP_FIELD` -- the key (GUID) of a Manager custom
  field (Settings > Custom Fields, string type, applied to Receipts and
  Payments) used to stash each imported transaction's Basiq id so re-runs
  don't duplicate it. This is per-business: create the custom field once in
  Manager, then paste its key here.

Optional: `MANAGER_MCP_BASIQ_TIMEZONE` (IANA name, default
`Australia/Sydney` -- must match the business's own calendar, since Basiq's
`postDate` is UTC and naively truncating it dates transactions a day early
whenever they post after local midnight) and `MANAGER_MCP_BASIQ_LOOKBACK_DAYS`
(default 7).

All of the above are ordinary env-var names, but every function here reads
them through `environ`, which defaults to
`feeds_config.effective_environ()` -- `os.environ` overlaid with the saved
bank-feed config file (see that module). In practice that means
these normally come from the setup UI's saved config file, not from
`secrets/manager-mcp.env`; env vars still work (e.g. for local dev) but the
file wins if both are set.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from manager_mcp.bank_feed_providers import feeds_config
from manager_mcp.bank_feed_providers.base import (
    BankFeedProvider,
    ProviderSetupState,
    SetupField,
    business_name,
    list_bank_accounts,
)
from manager_mcp.client import ConfigError, ManagerClient

_log = logging.getLogger(__name__)


class _SuppressDedupPaginationLogs(logging.Filter):
    """Drop httpx's per-request INFO log for the /api4 dedup-key pagination
    GETs in `_existing_dedup_keys` -- dozens per sync as accounts grow, and
    already summarized by our own fetch/result log lines. The POST calls
    that actually write new receipts/payments use the same paths, so only
    GET is filtered -- writes should stay visible."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "GET" not in msg:
            return True
        return "/api4/receipt-batch" not in msg and "/api4/payment-batch" not in msg


logging.getLogger("httpx").addFilter(_SuppressDedupPaginationLogs())

BASIQ_USERNAME_ENV = "BASIQ_USERNAME"
BASIQ_PASSWORD_ENV = "BASIQ_PASSWORD"
ACCOUNT_LINKS_ENV = "MANAGER_MCP_BASIQ_ACCOUNT_LINKS"
DEDUP_FIELD_ENV = "MANAGER_MCP_BASIQ_DEDUP_FIELD"
TIMEZONE_ENV = "MANAGER_MCP_BASIQ_TIMEZONE"
LOOKBACK_DAYS_ENV = "MANAGER_MCP_BASIQ_LOOKBACK_DAYS"

DEFAULT_TIMEZONE = "Australia/Sydney"
DEFAULT_LOOKBACK_DAYS = 7

_COGNITO_REGION = "ap-southeast-2"
_COGNITO_CLIENT_ID = "4gkkcphd738s0njfetqb4htq7u"
_COGNITO_URL = f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/"
_BASIQ_API_BASE = "https://aussiebankfeeds.com/api/basiq"
# Cloudflare in front of aussiebankfeeds.com 403s a bare/no-UA request.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
    "Referer": "https://aussiebankfeeds.com/sync",
    "Origin": "https://aussiebankfeeds.com",
}


class AussieBankFeedsSyncError(RuntimeError):
    """Basiq/Cognito or Manager /api4 call failed during an Aussie Bank Feeds sync."""


async def list_basiq_accounts(username: str, password: str) -> list[dict[str, str]]:
    """Best-effort: log in and list the Basiq accounts available to this
    Aussie Bank Feeds login, as [{"id", "name"}], so the setup UI can offer
    a picker instead of asking the operator to read ids off
    https://aussiebankfeeds.com/sync by hand.

    Unlike `/accounts/{id}/transactions` (used by the sync itself and
    confirmed live), this hits `/accounts` -- the natural sibling listing
    endpoint, but not one anyone has captured a real request for. If it
    doesn't exist or its shape differs, this raises and the setup UI falls
    back to asking for the account id directly.
    """
    access_token = await _cognito_login(username, password)
    url = f"{_BASIQ_API_BASE}/accounts"
    headers = {"Accept": "*/*", "Authorization": f"Bearer {access_token}", **_BROWSER_HEADERS}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code >= 400:
        raise AussieBankFeedsSyncError(
            f"Basiq accounts listing failed: HTTP {resp.status_code}: {resp.text[:500]}"
        )
    data = resp.json().get("data", [])
    out: list[dict[str, str]] = []
    for acct in data:
        if not isinstance(acct, dict):
            continue
        acct_id = str(acct.get("id") or "")
        name = str(
            acct.get("name") or acct.get("accountName") or acct.get("nickname") or acct_id
        )
        institution = acct.get("institution") or {}
        if isinstance(institution, dict) and institution.get("shortName"):
            name = f"{name} ({institution['shortName']})"
        if acct_id:
            out.append({"id": acct_id, "name": name})
    return out


async def _candidate_dedup_fields(
    client: ManagerClient, business: str, *, sample_size: int = 50
) -> list[str]:
    """customFields2 string keys already in use on a sample of existing
    receipts/payments -- candidates for the dedup field, so an operator who
    already created (or previously used) the custom field doesn't have to
    go find its GUID by hand. Best-effort: an empty result just means
    nothing has that field set yet (e.g. first-time setup)."""
    keys: set[str] = set()
    for entity in ("receipt-batch", "payment-batch"):
        try:
            resp = await client.raw_basic(
                "GET",
                f"{client.base_url.removesuffix('/api2')}/api4/{entity}"
                f"?Skip=0&PageSize={sample_size}",
                headers={"Accept": "application/json", "Manager-Business": business},
            )
        except Exception:
            _log.exception(
                "bank-feed setup: could not sample /api4/%s for candidate dedup fields", entity
            )
            continue
        if resp.status_code >= 400:
            continue
        for entry in resp.json().get("items") or []:
            record = entry.get("item") or {}
            cf = (record.get("customFields2") or {}).get("strings") or {}
            keys.update(cf.keys())
    return sorted(keys)


def account_links(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Manager bank-and-cash-account key -> linked Basiq account id, from
    `MANAGER_MCP_BASIQ_ACCOUNT_LINKS` (a JSON object). Read the mapping off
    https://aussiebankfeeds.com/sync's own table -- there's no API to
    discover it."""
    env = feeds_config.effective_environ() if environ is None else environ
    raw = (env.get(ACCOUNT_LINKS_ENV) or "").strip()
    if not raw:
        raise ConfigError(
            f"{ACCOUNT_LINKS_ENV} is required "
            "(a JSON object of {bank-account-key: basiq-account-id})"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{ACCOUNT_LINKS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ConfigError(f"{ACCOUNT_LINKS_ENV} must be a JSON object of string -> string")
    if not parsed:
        raise ConfigError(f"{ACCOUNT_LINKS_ENV} is empty -- link at least one bank account")
    return parsed


def dedup_field(environ: Mapping[str, str] | None = None) -> str:
    env = feeds_config.effective_environ() if environ is None else environ
    value = (env.get(DEDUP_FIELD_ENV) or "").strip()
    if not value:
        raise ConfigError(
            f"{DEDUP_FIELD_ENV} is required -- the key of a Manager custom field "
            "(Settings > Custom Fields, string type, on Receipts and Payments) "
            "used to record each imported transaction's Basiq id"
        )
    return value


def timezone(environ: Mapping[str, str] | None = None) -> ZoneInfo:
    env = feeds_config.effective_environ() if environ is None else environ
    name = (env.get(TIMEZONE_ENV) or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"{TIMEZONE_ENV}={name!r} is not a known IANA timezone") from exc


def lookback_days(environ: Mapping[str, str] | None = None) -> int:
    env = feeds_config.effective_environ() if environ is None else environ
    raw = (env.get(LOOKBACK_DAYS_ENV) or "").strip()
    if not raw:
        return DEFAULT_LOOKBACK_DAYS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{LOOKBACK_DAYS_ENV} must be an integer (got {raw!r})") from exc
    if value < 0:
        raise ConfigError(f"{LOOKBACK_DAYS_ENV} must be >= 0 (got {value})")
    return value


def _local_date(iso_timestamp: str, tz: ZoneInfo) -> str:
    """Basiq `postDate` (UTC ISO 8601) -> the business's local calendar date."""
    dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    return dt.astimezone(tz).date().isoformat()


async def _cognito_login(username: str, password: str) -> str:
    """USER_PASSWORD_AUTH against the AussieBankFeeds Cognito user pool."""
    body = {
        "AuthFlow": "USER_PASSWORD_AUTH",
        "ClientId": _COGNITO_CLIENT_ID,
        "AuthParameters": {"USERNAME": username, "PASSWORD": password},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _COGNITO_URL,
            json=body,
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
            },
        )
    if resp.status_code >= 400:
        raise AussieBankFeedsSyncError(
            f"Cognito login failed: HTTP {resp.status_code}: {resp.text[:500]}"
        )
    result = resp.json().get("AuthenticationResult")
    if not result:
        raise AussieBankFeedsSyncError(f"Cognito login did not return tokens: {resp.json()}")
    return result["AccessToken"]


async def _fetch_basiq_transactions(access_token: str, basiq_account_id: str) -> list[dict]:
    """Only `status == "posted"` transactions -- a "pending" transaction has a
    generic "AUTHORISATION" description and gets a *different* id once it
    posts (confirmed by comparing a live pending/posted pair for the same
    real-world charge), so pending ones must be skipped, not just deduped."""
    url = f"{_BASIQ_API_BASE}/accounts/{basiq_account_id}/transactions"
    headers = {"Accept": "*/*", "Authorization": f"Bearer {access_token}", **_BROWSER_HEADERS}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code >= 400:
        raise AussieBankFeedsSyncError(
            f"Basiq transactions fetch failed: HTTP {resp.status_code}: {resp.text[:500]}"
        )
    data = resp.json().get("data", [])
    posted = [t for t in data if t.get("status") == "posted"]
    _log.info(
        "Aussie Bank Feeds fetch account=%s total=%d posted=%d pending=%d",
        basiq_account_id,
        len(data),
        len(posted),
        len(data) - len(posted),
    )
    return posted


# Basiq/Manager mask account numbers in transaction descriptions inconsistently
# depending on which login/session pulled the feed (confirmed live 2026-08-31:
# the same real transaction showed up as both "SAV xxxx4075" and "SAV 12114075").
# Any run of 4+ digits/x's is collapsed to a placeholder so descriptions compare
# equal regardless of masking.
_MASKED_RUN_RE = re.compile(r"[0-9xX]{4,}")


def _normalize_description(text: str) -> str:
    return _MASKED_RUN_RE.sub("#", text or "").strip().lower()


FuzzyKey = tuple[str, float, str]


async def _existing_dedup_keys(
    client: ManagerClient, bank_account_key: str, dedup_field_key: str, business: str
) -> tuple[set[str], dict[str, set[FuzzyKey]], str | None]:
    """`/api4/{entity}` list responses are `{"items": [{"key": ..., "item": {...fields...}}]}`
    -- the actual record (and its `customFields2`) is nested under `item`, not at the
    top level of each list entry. Confirmed live 2026-08-31 after a dedup miss caused
    a full duplicate re-post (see incident note above).

    Also returns a fuzzy `(date, amount, normalized description)` key per entity, to
    catch transactions that were already entered manually (or by an earlier, differently
    masked sync) before the dedup custom field existed on them -- those have no
    dedup field to match on at all. And the latest transaction date seen (across both
    entities), so the caller can bound how far back into Basiq history it needs to look
    -- pulling all of Basiq's history every sync is what caused the fuzzy-match misses
    in the first place, since old manually entered transactions don't always describe
    the same real-world transaction closely enough to match."""
    ids: set[str] = set()
    fuzzy: dict[str, set[FuzzyKey]] = {"receipt-batch": set(), "payment-batch": set()}
    latest_date: str | None = None
    for entity in ("receipt-batch", "payment-batch"):
        skip = 0
        while True:
            resp = await client.raw_basic(
                "GET",
                f"{client.base_url.removesuffix('/api2')}/api4/{entity}"
                f"?BankOrCashAccount={bank_account_key}&Skip={skip}&PageSize=500",
                headers={"Accept": "application/json", "Manager-Business": business},
            )
            if resp.status_code >= 400:
                raise AussieBankFeedsSyncError(
                    f"/api4/{entity} read failed: HTTP {resp.status_code}: {resp.text[:500]}"
                )
            page = resp.json()
            entries = page.get("items") or []
            for entry in entries:
                record = entry.get("item") or {}
                cf = (record.get("customFields2") or {}).get("strings") or {}
                if dedup_field_key in cf:
                    ids.add(cf[dedup_field_key])
                entry_date = str(record.get("date") or "")[:10]
                amount = record.get("fixedTotalAmount")
                if entry_date and amount is not None:
                    fuzzy[entity].add(
                        (
                            entry_date,
                            round(abs(float(amount)), 2),
                            _normalize_description(record.get("description")),
                        )
                    )
                if entry_date and (latest_date is None or entry_date > latest_date):
                    latest_date = entry_date
            if len(entries) < 500:
                break
            skip += 500
    return ids, fuzzy, latest_date


def _sync_start_date(latest_date: str | None, days: int) -> str | None:
    """`latest_date` minus `days`, or None if there's no prior transaction to
    anchor to (an empty account -- pull everything Basiq has)."""
    if latest_date is None:
        return None
    return (date.fromisoformat(latest_date) - timedelta(days=days)).isoformat()


def _build_line(
    bank_account_key: str, direction_field: str, txn: dict, tz: ZoneInfo, dedup_field_key: str
) -> dict:
    """Shape matches a live captured POST body exactly."""
    amount = abs(float(txn["amount"]))
    return {
        "date": _local_date(txn["postDate"], tz),
        "reference": "",
        "description": txn["description"],
        direction_field: bank_account_key,
        "fixedTotal": True,
        "fixedTotalAmount": amount,
        "lines": [{"amount": amount}],
        "customFields2": {"strings": {dedup_field_key: txn["id"]}},
    }


async def _sync_one_account(
    client: ManagerClient,
    bank_account_key: str,
    basiq_account_id: str,
    access_token: str,
    *,
    dedup_field_key: str,
    tz: ZoneInfo,
    days: int,
    business: str,
) -> dict[str, Any]:
    txns = await _fetch_basiq_transactions(access_token, basiq_account_id)
    seen_ids, seen_fuzzy, latest_date = await _existing_dedup_keys(
        client, bank_account_key, dedup_field_key, business
    )

    start_date = _sync_start_date(latest_date, days)
    before_start_date_filter = len(txns)
    if start_date is not None:
        txns = [t for t in txns if _local_date(t["postDate"], tz) >= start_date]
    _log.debug(
        "Aussie Bank Feeds sync account=%s latest_existing_date=%s start_date=%s "
        "posted_fetched=%d in_window=%d excluded_by_lookback=%d",
        bank_account_key,
        latest_date,
        start_date,
        before_start_date_filter,
        len(txns),
        before_start_date_filter - len(txns),
    )

    skip_reason: dict[str, str] = {}

    def _is_new(txn: dict) -> bool:
        if txn["id"] in seen_ids:
            skip_reason[txn["id"]] = "dedup_id"
            return False
        entity = "receipt-batch" if float(txn["amount"]) > 0 else "payment-batch"
        key = (
            _local_date(txn["postDate"], tz),
            round(abs(float(txn["amount"])), 2),
            _normalize_description(txn["description"]),
        )
        if key in seen_fuzzy[entity]:
            skip_reason[txn["id"]] = "fuzzy_match"
            return False
        return True

    new = [t for t in txns if _is_new(t)]
    for txn in txns:
        if txn["id"] in skip_reason:
            _log.debug(
                "Aussie Bank Feeds skip account=%s id=%s date=%s amount=%s reason=%s",
                bank_account_key,
                txn["id"],
                _local_date(txn["postDate"], tz),
                txn["amount"],
                skip_reason[txn["id"]],
            )
    credits = [
        _build_line(bank_account_key, "receivedIn", t, tz, dedup_field_key)
        for t in new
        if float(t["amount"]) > 0
    ]
    debits = [
        _build_line(bank_account_key, "paidFrom", t, tz, dedup_field_key)
        for t in new
        if float(t["amount"]) < 0
    ]

    ui_base = client.base_url.removesuffix("/api2")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Manager-Business": business,
    }
    written = {"receipts": 0, "payments": 0}
    if credits:
        resp = await client.raw_basic(
            "POST", f"{ui_base}/api4/receipt-batch", headers=headers, json_body={"values": credits}
        )
        if resp.status_code >= 400:
            raise AussieBankFeedsSyncError(
                f"/api4/receipt-batch write failed: HTTP {resp.status_code}: {resp.text[:500]}"
            )
        written["receipts"] = len(credits)
    if debits:
        resp = await client.raw_basic(
            "POST", f"{ui_base}/api4/payment-batch", headers=headers, json_body={"values": debits}
        )
        if resp.status_code >= 400:
            raise AussieBankFeedsSyncError(
                f"/api4/payment-batch write failed: HTTP {resp.status_code}: {resp.text[:500]}"
            )
        written["payments"] = len(debits)
    _log.debug(
        "Aussie Bank Feeds sync account=%s new=%d written_receipts=%d written_payments=%d",
        bank_account_key,
        len(new),
        written["receipts"],
        written["payments"],
    )
    return {"bank_account": bank_account_key, "fetched": len(txns), "new": len(new), **written}


async def sync_aussie_bank_feeds(
    client: ManagerClient, *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Run the Aussie Bank Feeds / Basiq sync for every account configured in
    `MANAGER_MCP_BASIQ_ACCOUNT_LINKS`.

    Requires MANAGER_UI_USERNAME/PASSWORD (the mcp user needs write
    permission on the business -- /api4 returns 403, not 401, if it
    doesn't) and BASIQ_USERNAME/PASSWORD (the AussieBankFeeds login).
    """
    if not client.has_ui_auth:
        raise ConfigError(
            "MANAGER_UI_USERNAME and MANAGER_UI_PASSWORD are required "
            "to call Manager UI actions (the mcp user). X-API-KEY only covers /api2."
        )
    env = feeds_config.effective_environ() if environ is None else environ
    basiq_user = env.get(BASIQ_USERNAME_ENV, "")
    basiq_pass = env.get(BASIQ_PASSWORD_ENV, "")
    if not basiq_user or not basiq_pass:
        raise ConfigError(f"{BASIQ_USERNAME_ENV} and {BASIQ_PASSWORD_ENV} are required")

    links = account_links(env)
    dedup_field_key = dedup_field(env)
    tz = timezone(env)
    days = lookback_days(env)
    business = await business_name(client)

    access_token = await _cognito_login(basiq_user, basiq_pass)
    results = [
        await _sync_one_account(
            client,
            bank_account_key,
            basiq_account_id,
            access_token,
            dedup_field_key=dedup_field_key,
            tz=tz,
            days=days,
            business=business,
        )
        for bank_account_key, basiq_account_id in links.items()
    ]
    return {"status": "ok", "accounts": results}


class BasiqProvider(BankFeedProvider):
    """Aussie Bank Feeds / Basiq, selected automatically once
    `BASIQ_USERNAME`/`BASIQ_PASSWORD` are set."""

    name = "basiq"
    display_name = "Aussie Bank Feeds (Basiq)"

    def is_configured(self, environ: Mapping[str, str]) -> bool:
        return bool(environ.get(BASIQ_USERNAME_ENV)) and bool(environ.get(BASIQ_PASSWORD_ENV))

    async def sync(self, client: ManagerClient) -> dict[str, Any]:
        return await sync_aussie_bank_feeds(client)

    async def setup_state(
        self, client: ManagerClient, environ: Mapping[str, str]
    ) -> ProviderSetupState:
        business = await business_name(client)
        bank_accounts = await list_bank_accounts(client)
        candidates = await _candidate_dedup_fields(client, business)

        try:
            current_links = account_links(environ)
        except ConfigError:
            current_links = {}
        current_dedup = (environ.get(DEDUP_FIELD_ENV) or "").strip()
        current_tz = (environ.get(TIMEZONE_ENV) or "").strip() or DEFAULT_TIMEZONE

        current_username = environ.get(BASIQ_USERNAME_ENV) or ""
        current_password = environ.get(BASIQ_PASSWORD_ENV) or ""
        has_password = bool(current_password)

        # If credentials are already saved, look up the linked accounts'
        # real names now rather than making the operator click "Detect
        # Basiq accounts" just to see what a GUID they already linked
        # actually refers to. Best-effort: any failure (bad/rotated
        # credentials, Basiq being unreachable) just falls back to showing
        # the bare id, same as before.
        basiq_names: dict[str, str] = {}
        if current_username and current_password and current_links:
            try:
                basiq_names = {
                    a["id"]: a["name"]
                    for a in await list_basiq_accounts(current_username, current_password)
                    if a["name"] != a["id"]  # list_basiq_accounts falls back to the bare id
                    # when Basiq's response had no real name -- skip those, no point
                    # showing "<id> (<id>)".
                }
            except Exception:
                _log.warning(
                    "bank-feed setup: could not look up Basiq account names with saved credentials",
                    exc_info=True,
                )

        dedup_options = [{"key": k, "name": k} for k in candidates]
        if current_dedup and current_dedup not in candidates:
            # The saved value isn't among the detected candidates (e.g. no
            # sampled receipt/payment happened to carry it yet) -- pin it in
            # anyway so the dropdown still shows what's actually configured,
            # instead of silently falling back to "-- choose --".
            dedup_options.insert(0, {"key": current_dedup, "name": current_dedup})
        elif not current_dedup and len(candidates) == 1:
            # Nothing saved yet, but there's exactly one candidate -- no
            # real choice to make, so default to it instead of making the
            # operator pick from a dropdown with one item in it.
            current_dedup = candidates[0]

        fields = [
            SetupField(
                key=BASIQ_USERNAME_ENV,
                label="Aussie Bank Feeds username",
                kind="text",
                value=current_username,
                help="Can't be read from Manager -- the login for https://aussiebankfeeds.com. "
                "Not secret, shown as-is; edit in place to change it.",
            ),
            SetupField(
                key=BASIQ_PASSWORD_ENV,
                label="Aussie Bank Feeds password",
                kind="password",
                placeholder="•" * 12 if has_password else "not set",
                help="Can't be read from Manager, and this page never shows the saved "
                "password back to you. Leave blank to keep the current one; type a new "
                "one to replace it when you click Save. Also used once, live, if you "
                "click 'Detect Basiq accounts' below.",
            ),
            SetupField(
                key=ACCOUNT_LINKS_ENV,
                label="Bank account -> Basiq account links",
                kind="account_links",
                value=json.dumps(current_links) if current_links else "",
                options=[
                    {
                        **acct,
                        "value": current_links.get(acct["key"], ""),
                        "value_label": basiq_names.get(current_links.get(acct["key"], ""), ""),
                    }
                    for acct in bank_accounts
                ],
                callout=(
                    "No Manager bank or cash accounts found. Add one on the "
                    "Bank and Cash Accounts tab in Manager first, then reload this page."
                    if not bank_accounts
                    else ""
                ),
                help="Manager's own accounts are listed from /api2 automatically. Enter your "
                "Aussie Bank Feeds username/password above and click 'Detect Basiq accounts' "
                "to fill in the matching side; if detection doesn't work, read the ids off "
                "https://aussiebankfeeds.com/sync instead. Rows already linked show their "
                "current Basiq account id -- leave a row as-is to keep that link.",
            ),
            SetupField(
                key=DEDUP_FIELD_ENV,
                label="Hidden Basiq Transaction ID Custom Field GUID",
                kind="select" if dedup_options else "text",
                value=current_dedup,
                placeholder="paste the custom field's key here" if not dedup_options else "",
                options=dedup_options,
                callout=(
                    ""
                    if dedup_options
                    else (
                        "Nothing to pick from yet -- create one in Manager: Settings "
                        "→ Custom Fields → New Custom Field → type String "
                        "→ tick both Receipts and Payments (name it anything, e.g. "
                        "“Basiq Transaction ID”). It just needs to exist; the "
                        "sync fills it in itself. Then reload this page, or paste its "
                        "key below if you already know it."
                    )
                ),
                help=(
                    "Stops the sync creating the same bank transaction twice: it writes "
                    "Basiq's transaction id into this Manager custom field on every "
                    "Receipt/Payment it imports, then checks it before importing again. "
                    "Manager's API won't tell us a custom field's actual name -- only "
                    "this raw internal key -- so that's all there is to pick from below. "
                    "If there's only one option, it's almost certainly the right one."
                ),
            ),
            SetupField(
                key=TIMEZONE_ENV,
                label="Business timezone",
                kind="text",
                value=current_tz,
                required=False,
                help="IANA timezone name. Basiq's transaction dates are UTC; this converts them to "
                f"the business's own calendar date. Default {DEFAULT_TIMEZONE!r} if left as-is.",
            ),
        ]
        return ProviderSetupState(
            provider=self.name,
            configured=self.is_configured(environ),
            detected={
                "business_name": business,
                "bank_accounts": bank_accounts,
                "candidate_dedup_fields": candidates,
            },
            fields=fields,
        )
