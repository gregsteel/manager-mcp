"""Trigger Manager.io's bank-feed import on a timer.

Two generations of mechanism live here:

1. **Aussie Bank Feeds / Basiq (current, working)** -- `sync_aussie_bank_feeds`.
   Reverse-engineered 2026-08-31 from the real requests Manager's own
   frontend fires when you click "Sync All Linked" in the Aussie Bank Feeds
   modal (Bank and Cash Accounts > an account > Aussie Bank Feeds, which is
   just an `<iframe src="https://aussiebankfeeds.com/sync">`). That page
   authenticates against its own AWS Cognito user pool (unrelated to
   Manager's own login) and, once you click sync, its parent-window
   JS reads new transactions from Basiq then posts them straight into
   Manager's `/api4/receipt-batch` (credits) and `/api4/payment-batch`
   (debits) using the browser's own Manager session. Confirmed end-to-end
   live: Cognito USER_PASSWORD_AUTH login works with real credentials,
   `/api4` accepts the same HTTP Basic Auth `raw_basic` already uses for UI
   actions (see `ManagerClient.raw_basic`'s `json_body` param), and Basiq's
   API 403s on a bare urllib/httpx request (Cloudflare bot detection) unless
   given a browser-like User-Agent/Referer.

2. **Built-in `CheckForNewTransactions` control (superseded, kept for
   fallback)** -- `check_for_new_transactions`. Manager UI actions are
   rooted at `/{action-slug}?{FileID}`, the same shape as `attachments_api`
   (`/new-attachment?…`). They are **not** nested under the tab path: GET
   `/bank-and-cash-accounts/check-for-new-transactions` returns 200 HTML of
   the Bank and Cash Accounts *list* and does not import anything. The
   built-in control was `CheckForNewTransactions` -> `/check-for-new-transactions?{FileID}`.
   Later Manager versions replaced it with the Aussie Bank Feeds custom
   button on that tab -- which is exactly mechanism 1 above; this fallback
   is what's left for a business that hasn't set up Aussie Bank Feeds and
   still has the old built-in control.

The hourly loop is an operator job inside this process, not an MCP tool.
Set `MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS` (compose: 3600). Unset
or 0 leaves it off. It runs `sync_aussie_bank_feeds` when `BASIQ_USERNAME`/
`BASIQ_PASSWORD` are set, else falls back to `check_for_new_transactions`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from base64 import urlsafe_b64encode
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

from manager_mcp.client import ConfigError, ManagerClient

_log = logging.getLogger(__name__)

INTERVAL_ENV = "MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS"
SYNC_PATH = "/check-for-new-transactions"
TAB_PATH = "/bank-and-cash-accounts"
_STARTUP_DELAY_SECONDS = 15.0
_HX_HEADERS = {"HX-Request": "true", "Accept": "text/html, application/xhtml+xml"}

# --- Aussie Bank Feeds / Basiq -------------------------------------------

BASIQ_USERNAME_ENV = "BASIQ_USERNAME"
BASIQ_PASSWORD_ENV = "BASIQ_PASSWORD"
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

# customFields2 string-field key Manager's own frontend uses to stash the
# Basiq transaction id on each created Receipt/Payment line -- this is the
# dedup key, confirmed from a live captured POST body.
DEDUP_FIELD = "ad18c1cc-aa92-49f9-ba18-8c98f9a76d29"

# Manager bank-and-cash-account key -> linked Basiq account id, as shown on
# https://aussiebankfeeds.com/sync (2026-08-31). Add a row here for every
# account linked on that page -- there's no API to discover the mapping
# automatically, it's read off the Sync page's own table.
BANK_ACCOUNT_LINKS: dict[str, str] = {
    # Lilith Bank Account (1-1110)
    "019fbc7e-1d33-7c57-aabd-5547c28e0f44": "2339309b-a357-4a95-85f0-abe33a7b098f",
}

MANAGER_BUSINESS = "Lilith Pty Ltd"

# Basiq's `postDate` is UTC. Manager transaction dates need the business's
# own calendar date, not UTC's -- naively truncating the ISO string
# (`postDate[:10]`) dated transactions a day early whenever they posted
# after ~2pm UTC (local midnight in Melbourne, UTC+10/+11), confirmed live
# 2026-09-03 by comparing Manager's imported dates against the real bank
# statement (a 01/09 transaction landed in Manager as 31/8).
MANAGER_BUSINESS_TZ = ZoneInfo("Australia/Melbourne")


def _local_date(iso_timestamp: str) -> str:
    """Basiq `postDate` (UTC ISO 8601) -> the business's local calendar date."""
    return (
        datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        .astimezone(MANAGER_BUSINESS_TZ)
        .date()
        .isoformat()
    )


class AussieBankFeedsSyncError(RuntimeError):
    """Basiq/Cognito or Manager /api4 call failed during an Aussie Bank Feeds sync."""


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


# How far before the latest existing transaction date to still pull Basiq
# transactions from, to catch anything backdated/posted late.
LOOKBACK_DAYS = 7


async def _existing_dedup_keys(
    client: ManagerClient, bank_account_key: str
) -> tuple[set[str], dict[str, set[FuzzyKey]], str | None]:
    """`/api4/{entity}` list responses are `{"items": [{"key": ..., "item": {...fields...}}]}`
    -- the actual record (and its `customFields2`) is nested under `item`, not at the
    top level of each list entry. Confirmed live 2026-08-31 after a dedup miss caused
    a full duplicate re-post (see incident note above).

    Also returns a fuzzy `(date, amount, normalized description)` key per entity, to
    catch transactions that were already entered manually (or by an earlier, differently
    masked sync) before the dedup custom field existed on them -- those have no
    `DEDUP_FIELD` to match on at all. And the latest transaction date seen (across both
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
                headers={"Accept": "application/json", "Manager-Business": MANAGER_BUSINESS},
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
                if DEDUP_FIELD in cf:
                    ids.add(cf[DEDUP_FIELD])
                date = str(record.get("date") or "")[:10]
                amount = record.get("fixedTotalAmount")
                if date and amount is not None:
                    fuzzy[entity].add(
                        (date, round(abs(float(amount)), 2), _normalize_description(record.get("description")))
                    )
                if date and (latest_date is None or date > latest_date):
                    latest_date = date
            if len(entries) < 500:
                break
            skip += 500
    return ids, fuzzy, latest_date


def _sync_start_date(latest_date: str | None) -> str | None:
    """`latest_date` minus `LOOKBACK_DAYS`, or None if there's no prior transaction
    to anchor to (an empty account -- pull everything Basiq has)."""
    if latest_date is None:
        return None
    return (date.fromisoformat(latest_date) - timedelta(days=LOOKBACK_DAYS)).isoformat()


def _build_line(bank_account_key: str, direction_field: str, txn: dict) -> dict:
    """Shape matches a live captured POST body exactly."""
    amount = abs(float(txn["amount"]))
    return {
        "date": _local_date(txn["postDate"]),
        "reference": "",
        "description": txn["description"],
        direction_field: bank_account_key,
        "fixedTotal": True,
        "fixedTotalAmount": amount,
        "lines": [{"amount": amount}],
        "customFields2": {"strings": {DEDUP_FIELD: txn["id"]}},
    }


async def _sync_one_account(
    client: ManagerClient, bank_account_key: str, basiq_account_id: str, access_token: str
) -> dict[str, Any]:
    txns = await _fetch_basiq_transactions(access_token, basiq_account_id)
    seen_ids, seen_fuzzy, latest_date = await _existing_dedup_keys(client, bank_account_key)

    start_date = _sync_start_date(latest_date)
    before_start_date_filter = len(txns)
    if start_date is not None:
        txns = [t for t in txns if _local_date(t["postDate"]) >= start_date]
    _log.info(
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
            _local_date(txn["postDate"]),
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
            _log.info(
                "Aussie Bank Feeds skip account=%s id=%s date=%s amount=%s reason=%s",
                bank_account_key,
                txn["id"],
                _local_date(txn["postDate"]),
                txn["amount"],
                skip_reason[txn["id"]],
            )
    credits = [
        _build_line(bank_account_key, "receivedIn", t) for t in new if float(t["amount"]) > 0
    ]
    debits = [_build_line(bank_account_key, "paidFrom", t) for t in new if float(t["amount"]) < 0]

    ui_base = client.base_url.removesuffix("/api2")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Manager-Business": MANAGER_BUSINESS,
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
    _log.info(
        "Aussie Bank Feeds sync account=%s new=%d written_receipts=%d written_payments=%d",
        bank_account_key,
        len(new),
        written["receipts"],
        written["payments"],
    )
    return {"bank_account": bank_account_key, "fetched": len(txns), "new": len(new), **written}


async def sync_aussie_bank_feeds(client: ManagerClient) -> dict[str, Any]:
    """Run the Aussie Bank Feeds / Basiq sync for every linked account.

    Requires MANAGER_UI_USERNAME/PASSWORD (the mcp user needs write
    permission on the business -- /api4 returns 403, not 401, if it
    doesn't) and BASIQ_USERNAME/PASSWORD (the AussieBankFeeds login).
    """
    if not client.has_ui_auth:
        raise ConfigError(
            "MANAGER_UI_USERNAME and MANAGER_UI_PASSWORD are required "
            "to call Manager UI actions (the mcp user). X-API-KEY only covers /api2."
        )
    basiq_user = os.environ.get(BASIQ_USERNAME_ENV, "")
    basiq_pass = os.environ.get(BASIQ_PASSWORD_ENV, "")
    if not basiq_user or not basiq_pass:
        raise ConfigError(f"{BASIQ_USERNAME_ENV} and {BASIQ_PASSWORD_ENV} are required")

    access_token = await _cognito_login(basiq_user, basiq_pass)
    results = [
        await _sync_one_account(client, bank_account_key, basiq_account_id, access_token)
        for bank_account_key, basiq_account_id in BANK_ACCOUNT_LINKS.items()
    ]
    return {"status": "ok", "accounts": results}


_LIST_TITLE_RE = re.compile(r"<title>[^<]*bank and cash acc", re.I)
_ATTR_URL_RE = re.compile(
    r"""(?:href|hx-get|hx-post|hx-put|action)\s*=\s*["']([^"']+)["']""",
    re.I,
)
_FEED_URL_HINTS = (
    "check-for-new-transaction",
    "custom-button-html",
    "aussie",
    "basiq",
    "bank-feed",
    "bankfeed",
)
_RAN_MARKERS = (
    "transactions imported",
    "new transactions imported",
    "no new transactions",
)


class BankFeedSyncError(RuntimeError):
    """Manager rejected or failed the bank-feed sync request."""


def bank_feed_sync_interval_seconds(
    environ: Mapping[str, str] | None = None,
) -> float | None:
    """Seconds between syncs, or None if the loop should not run."""
    env = os.environ if environ is None else environ
    raw = (env.get(INTERVAL_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{INTERVAL_ENV} must be a number of seconds (got {raw!r})") from exc
    if value < 0:
        raise ValueError(f"{INTERVAL_ENV} must be >= 0 (got {value})")
    if value == 0:
        return None
    return value


def _ui_base_url(api_base_url: str) -> str:
    base = api_base_url.rstrip("/")
    if base.endswith("/api2"):
        base = base[: -len("/api2")]
    return base


def bank_feed_sync_url(api_base_url: str, *, business_name: str | None = None) -> str:
    """Root UI action URL (`/check-for-new-transactions`), not nested under the tab."""
    url = f"{_ui_base_url(api_base_url)}{SYNC_PATH}"
    if business_name:
        url = f"{url}?{_business_file_query(business_name)}"
    return url


def _tab_url(api_base_url: str, *, business_name: str | None = None) -> str:
    url = f"{_ui_base_url(api_base_url)}{TAB_PATH}"
    if business_name:
        url = f"{url}?{_business_file_query(business_name)}"
    return url


def _business_file_query(business_name: str) -> str:
    """FileID query Manager's own UI puts on tab/action links."""
    name = business_name.encode()
    protobuf = b"\xa2\x06" + bytes([len(name)]) + name
    return urlsafe_b64encode(protobuf).decode().rstrip("=")


def _body_preview(response: httpx.Response, *, limit: int = 200) -> str:
    text = (response.text or "").replace("\n", " ")
    return text[:limit]


def _is_login_redirect(response: httpx.Response) -> bool:
    if response.status_code not in {301, 302, 303, 307, 308}:
        return False
    loc = (response.headers.get("location") or "").strip().lower()
    path = loc.split("?", 1)[0].rstrip("/")
    if "login" in loc or "signin" in loc:
        return True
    return path in {"", "/"}


def _is_inert_list_page(response: httpx.Response) -> bool:
    """True when Manager served the Bank and Cash Accounts index, not the action.

    Observed live: GET …/bank-and-cash-accounts/check-for-new-transactions
    returned 200 text/html titled "… | Bank and Cash Acc" and imported
    nothing.
    """
    if response.status_code != 200:
        return False
    html = response.text or ""
    if not _LIST_TITLE_RE.search(html):
        return False
    lower = html.lower()
    return not any(marker in lower for marker in _RAN_MARKERS)


def _discover_feed_action_urls(html: str, ui_base: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _ATTR_URL_RE.finditer(html):
        raw = unescape(match.group(1).strip())
        if not raw or raw.startswith("#") or raw.lower().startswith("javascript:"):
            continue
        lower = raw.lower()
        if not any(hint in lower for hint in _FEED_URL_HINTS):
            continue
        absolute = urljoin(ui_base.rstrip("/") + "/", raw)
        if absolute not in seen:
            seen.add(absolute)
            found.append(absolute)
    return found


def _same_origin(url: str, ui_base: str) -> bool:
    left, right = urlparse(url), urlparse(ui_base)
    return (left.scheme, left.netloc) == (right.scheme, right.netloc)


async def _business_name(client: ManagerClient) -> str:
    try:
        envelope = await client.get("/bank-and-cash-accounts", params={"skip": 0, "pageSize": 1})
    except Exception:
        _log.exception("bank-feed sync: could not read business name from /api2")
        return ""
    if isinstance(envelope, dict):
        return str((envelope.get("business") or {}).get("name") or "")
    return ""


def _failure_detail(response: httpx.Response) -> str:
    location = response.headers.get("location")
    www = response.headers.get("www-authenticate")
    parts = [f"HTTP {response.status_code}"]
    if location:
        parts.append(f"location={location}")
    if www:
        parts.append(f"www-authenticate={www}")
    parts.append(f"content-type={response.headers.get('content-type', '')!r}")
    parts.append(f"Body: {_body_preview(response)!r}")
    return " ".join(parts)


async def _request_action(client: ManagerClient, method: str, url: str) -> httpx.Response:
    try:
        return await client.raw_basic(method, url, headers=_HX_HEADERS)
    except httpx.RequestError as exc:
        raise BankFeedSyncError(f"bank-feed sync {method} {url} failed: {exc}") from exc


def _outcome(method: str, url: str, response: httpx.Response) -> dict[str, Any] | None:
    """Return an ok envelope, or None if this response did not run the action."""
    if _is_login_redirect(response):
        raise BankFeedSyncError(
            f"bank-feed sync {method} {url} redirected to login. {_failure_detail(response)}"
        )
    if 300 <= response.status_code < 400:
        _log.info(
            "bank-feed sync %s %s redirected (treated as success): %s",
            method,
            url,
            _failure_detail(response),
        )
        return _ok(method, url, response)
    if response.status_code >= 300:
        return None
    if _is_inert_list_page(response):
        _log.info(
            "bank-feed sync %s %s returned the Bank and Cash Accounts list "
            "page (action did not run)",
            method,
            url,
        )
        return None
    _log.info(
        "bank-feed sync %s %s -> %s content-type=%s body=%r",
        method,
        url,
        response.status_code,
        response.headers.get("content-type", ""),
        _body_preview(response),
    )
    return _ok(method, url, response)


def _ok(method: str, url: str, response: httpx.Response) -> dict[str, Any]:
    return {
        "status": "ok",
        "method": method,
        "url": url,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "body_preview": _body_preview(response),
    }


async def _try_action(client: ManagerClient, url: str) -> dict[str, Any] | None:
    response = await _request_action(client, "GET", url)
    if response.status_code not in {404, 405}:
        out = _outcome("GET", url, response)
        if out is not None:
            return out
    _log.info("bank-feed sync GET %s did not run the action; retrying POST", url)
    response = await _request_action(client, "POST", url)
    return _outcome("POST", url, response)


async def _scrape_tab_actions(client: ManagerClient, ui_base: str, tab_url: str) -> list[str]:
    response = await _request_action(client, "GET", tab_url)
    if response.status_code >= 300:
        _log.warning(
            "bank-feed sync could not load tab %s: %s",
            tab_url,
            _failure_detail(response),
        )
        return []
    urls = _discover_feed_action_urls(response.text or "", ui_base)
    _log.info("bank-feed sync tab %s discovered action urls: %s", tab_url, urls)
    return urls


async def check_for_new_transactions(client: ManagerClient) -> dict[str, Any]:
    """Run Manager's bank-feed import as the mcp user."""
    if not client.has_ui_auth:
        raise ConfigError(
            "MANAGER_UI_USERNAME and MANAGER_UI_PASSWORD are required "
            "to call Manager UI actions (the mcp user). X-API-KEY only covers /api2."
        )

    name = await _business_name(client)
    ui_base = _ui_base_url(client.base_url)
    primary = bank_feed_sync_url(client.base_url, business_name=name or None)
    tried: set[str] = set()
    last_error_bits: list[str] = []

    for url in (primary,):
        tried.add(url)
        out = await _try_action(client, url)
        if out is not None:
            return out
        last_error_bits.append(url)

    discovered = await _scrape_tab_actions(
        client, ui_base, _tab_url(client.base_url, business_name=name or None)
    )
    same_origin = [u for u in discovered if _same_origin(u, ui_base) and u not in tried]
    external = [u for u in discovered if not _same_origin(u, ui_base)]
    for url in same_origin:
        tried.add(url)
        out = await _try_action(client, url)
        if out is not None:
            return out
        last_error_bits.append(url)

    extra = ""
    if external:
        extra = (
            f" Tab also has off-origin feed links (Aussie Bank Feeds extension, "
            f"not triggerable with the mcp user alone): {external}."
        )
    raise BankFeedSyncError(
        "bank-feed sync did not import transactions. Tried "
        f"{last_error_bits or [primary]} and got the Bank and Cash Accounts "
        f"list page (or HTTP error) instead of the action running.{extra}"
    )


async def run_bank_feed_sync_loop(
    *,
    interval: float,
    sync: Callable[[], Awaitable[Any]],
    sleep: Callable[[float], Awaitable[None]] | None = None,
    startup_delay: float = _STARTUP_DELAY_SECONDS,
) -> None:
    """Run `sync` forever, waiting `interval` seconds between attempts."""
    nap = sleep or asyncio.sleep
    if startup_delay > 0:
        await nap(startup_delay)
    while True:
        try:
            result = await sync()
        except Exception:
            _log.exception("bank-feed sync failed")
        else:
            _log.info("bank-feed sync result: %s", result)
        await nap(interval)


def start_bank_feed_sync_thread(interval: float) -> threading.Thread:
    """Daemon thread with its own event loop and ManagerClient."""

    def _run() -> None:
        async def _loop() -> None:
            client = ManagerClient.from_env()
            try:
                if not client.has_ui_auth:
                    _log.error(
                        "bank-feed sync is enabled but MANAGER_UI_USERNAME/"
                        "MANAGER_UI_PASSWORD are not set. The UI action needs "
                        "the mcp user (HTTP Basic Auth); X-API-KEY only covers "
                        "/api2. Not starting the loop."
                    )
                    return
                use_basiq = bool(
                    os.environ.get(BASIQ_USERNAME_ENV) and os.environ.get(BASIQ_PASSWORD_ENV)
                )
                sync_fn = sync_aussie_bank_feeds if use_basiq else check_for_new_transactions
                _log.info(
                    "bank-feed sync using %s",
                    "Aussie Bank Feeds/Basiq"
                    if use_basiq
                    else "built-in CheckForNewTransactions (fallback)",
                )
                await run_bank_feed_sync_loop(
                    interval=interval,
                    sync=lambda: sync_fn(client),
                )
            finally:
                await client.aclose()

        asyncio.run(_loop())

    thread = threading.Thread(target=_run, name="bank-feed-sync", daemon=True)
    thread.start()
    _log.info(
        "bank-feed sync scheduled every %s seconds (%s)",
        interval,
        bank_feed_sync_url(os.environ.get("MANAGER_API_URL", "")),
    )
    return thread
