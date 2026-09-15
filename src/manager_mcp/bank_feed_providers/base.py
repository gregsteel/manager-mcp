"""Common interface every bank-feed provider implements.

A provider knows how to get new bank transactions into Manager on demand.
The hourly loop in `manager_mcp.bank_feeds` doesn't know or care which one
runs -- it asks `registry.select_provider()` for whichever provider is
configured (or explicitly named via `MANAGER_MCP_BANK_FEED_PROVIDER`) and
awaits `provider.sync(client)`.

To add a new provider (e.g. a different bank-feed aggregator): create a
module here with a class implementing `BankFeedProvider`, then add an
instance to `PROVIDERS` in `registry.py`. Nothing else needs to change --
`bank_feeds.py` and `server.py` only ever talk to the registry.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from manager_mcp.client import ManagerClient

_log = logging.getLogger(__name__)


@dataclass
class SetupField:
    """One piece of config the setup UI needs to collect for a provider,
    because it could not be auto-detected from Manager (or from the
    provider's own service once partial credentials are known)."""

    key: str  # env var name this fills in
    label: str
    kind: str  # "text" | "password" | "select" | "account_links"
    value: str = ""  # real current value, pre-filled into the input. NEVER set this
    # for kind="password" -- a saved password must never be sent back to the browser.
    placeholder: str = ""  # hint text shown in an empty box (kind="password" or "text").
    # For kind="password" this is never a submittable value -- the box stays empty.
    # For kind="text" it's just a normal HTML placeholder (goes away once you type).
    help: str = ""
    callout: str = ""  # shown as a highlighted box above the input, e.g. "nothing
    # detected yet -- here's how to create one" -- for when help text alone would
    # bury an action the operator actually needs to take before they can fill this in.
    required: bool = True  # drives the "what you'll need" summary when unconfigured
    options: list[dict[str, str]] = field(default_factory=list)  # for "select"/"account_links"


@dataclass
class ProviderSetupState:
    """What the setup UI shows for one provider: what was auto-detected,
    and what still needs the operator's input."""

    provider: str
    configured: bool
    detected: dict[str, Any] = field(default_factory=dict)
    fields: list[SetupField] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class BankFeedProvider(ABC):
    """One mechanism for getting bank transactions into Manager."""

    #: Short, stable identifier -- used as the value of
    #: `MANAGER_MCP_BANK_FEED_PROVIDER` to select this provider explicitly.
    name: str

    #: Human-readable name for the setup UI.
    display_name: str = ""

    def is_configured(self, environ: Mapping[str, str]) -> bool:
        """Whether this provider has what it needs to run, judged from env
        vars alone (no network calls). Used to auto-select a provider when
        `MANAGER_MCP_BANK_FEED_PROVIDER` is unset: the registry picks the
        first configured provider, in the order `PROVIDERS` lists them."""
        return True

    @abstractmethod
    async def sync(self, client: ManagerClient) -> dict[str, Any]:
        """Import new transactions into Manager. Raise on failure; the
        caller logs and retries next interval, it does not crash the loop."""
        raise NotImplementedError

    async def setup_state(
        self, client: ManagerClient, environ: Mapping[str, str]
    ) -> ProviderSetupState:
        """Best-effort: what this provider can read straight from Manager
        (or, given partial credentials, from its own service), and what it
        still needs typed in by hand. Default: nothing to configure --
        override this in a provider that needs setup input."""
        return ProviderSetupState(provider=self.name, configured=self.is_configured(environ))


async def business_name(client: ManagerClient) -> str:
    """Best-effort business name from `/api2`, for the `Manager-Business`
    header some Manager UI/undocumented endpoints expect. Empty string if it
    can't be read -- callers should treat that as "omit the header", not a
    fatal error."""
    try:
        envelope = await client.get("/bank-and-cash-accounts", params={"skip": 0, "pageSize": 1})
    except Exception:
        _log.exception("bank-feed sync: could not read business name from /api2")
        return ""
    if isinstance(envelope, dict):
        return str((envelope.get("business") or {}).get("name") or "")
    return ""


async def list_bank_accounts(client: ManagerClient) -> list[dict[str, str]]:
    """Manager's own Bank and Cash Accounts, as [{"key", "name"}] -- always
    readable from `/api2`, so every provider's setup UI can offer a picker
    instead of asking the operator to copy account keys out of a URL."""
    try:
        envelope = await client.get("/bank-and-cash-accounts", params={"skip": 0, "pageSize": 500})
    except Exception:
        _log.exception("bank-feed setup: could not list bank-and-cash-accounts from /api2")
        return []
    if not isinstance(envelope, dict):
        return []
    accounts = envelope.get("bankAndCashAccounts") or envelope.get("bank-and-cash-accounts") or []
    out: list[dict[str, str]] = []
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        key = str(acct.get("key") or "")
        name = str(acct.get("name") or key)
        if key:
            out.append({"key": key, "name": name})
    return out
