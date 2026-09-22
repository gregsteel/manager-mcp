"""Which bank-feed provider runs.

`PROVIDERS` is the whole plugin surface: add an instance here (earlier in
the list = tried first for auto-selection) and everything else -- the
hourly sync loop, the setup UI -- picks it up with no further wiring. See
`base.py`'s `BankFeedProvider` for the interface a new provider implements.
"""

from __future__ import annotations

from collections.abc import Mapping

from manager_mcp.bank_feed_providers import feeds_config
from manager_mcp.bank_feed_providers.base import BankFeedProvider
from manager_mcp.bank_feed_providers.basiq import BasiqProvider
from manager_mcp.client import ConfigError

PROVIDER_ENV = "MANAGER_MCP_BANK_FEED_PROVIDER"

# Order matters: when MANAGER_MCP_BANK_FEED_PROVIDER is unset, the first
# provider whose is_configured() returns True wins.
#
# There used to be a second entry here -- Manager's built-in "Check for New
# Transactions" control -- kept as an always-available fallback. It was
# removed: that control only exists on older Manager installs that never
# got the Aussie Bank Feeds extension, and every deployment this code
# actually runs against already uses Aussie Bank Feeds, so the fallback
# never did anything but fail on every attempt. If a deployment genuinely
# needs it back, it's still in version control history.
PROVIDERS: list[BankFeedProvider] = [
    BasiqProvider(),
]


def provider_by_name(name: str) -> BankFeedProvider:
    for provider in PROVIDERS:
        if provider.name == name:
            return provider
    known = ", ".join(p.name for p in PROVIDERS)
    raise ConfigError(f"Unknown {PROVIDER_ENV}={name!r}; known providers: {known}")


def select_provider(environ: Mapping[str, str] | None = None) -> BankFeedProvider:
    """Explicit `MANAGER_MCP_BANK_FEED_PROVIDER`, else the first configured
    provider in `PROVIDERS` order. Reads from the saved bank-feed config
    file layered on `os.environ` (see `feeds_config.py`) unless a specific
    `environ` is given (tests). Raises `ConfigError` if nothing is
    configured -- callers (the sync loop, the `sync_bank_feeds` tool) turn
    that into a "not configured, here's the setup UI" result rather than
    letting it propagate as a bare error."""
    env = feeds_config.effective_environ() if environ is None else environ
    explicit = (env.get(PROVIDER_ENV) or "").strip()
    if explicit:
        return provider_by_name(explicit)
    for provider in PROVIDERS:
        if provider.is_configured(env):
            return provider
    raise ConfigError("No bank-feed provider is configured")
