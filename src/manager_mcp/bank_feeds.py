"""Trigger a bank-feed import on a timer, via whichever provider is configured.

This module owns only the generic parts: interval parsing, the hourly
loop/thread, and `sync_or_report` (turning "nothing configured yet" into an
actionable envelope instead of an exception). Which mechanism actually runs
is a pluggable `BankFeedProvider` (see `bank_feed_providers/`); this module
never talks to Basiq or any other provider's API directly.

Set `MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS` (compose: 3600) to enable
the loop; unset or 0 leaves it off. Which provider runs is read fresh from
`bank_feed_providers.feeds_config` (a saved config file, not env vars --
see that module) on every attempt, so configuring or reconfiguring one
through the setup UI takes effect on the next scheduled sync without a
restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from manager_mcp.bank_feed_providers.registry import select_provider
from manager_mcp.client import ConfigError, ManagerClient

_log = logging.getLogger(__name__)

INTERVAL_ENV = "MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS"
_STARTUP_DELAY_SECONDS = 15.0


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


async def sync_or_report(client: ManagerClient, *, setup_url: str | None = None) -> dict[str, Any]:
    """Run whichever bank-feed provider is configured; if nothing is (or the
    mcp user's UI credentials are missing), return a `configured: False`
    envelope pointing at the setup UI instead of raising. Shared by the
    background loop and the `sync_bank_feeds` MCP tool so both give the
    same actionable guidance instead of a bare exception.
    """

    def _not_configured(error: str, provider_name: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"configured": False, "error": error}
        if provider_name:
            out["provider"] = provider_name
        if setup_url:
            out["setup_url"] = setup_url
            out["hint"] = f"Open {setup_url} to configure a bank-feed provider."
        else:
            out["hint"] = (
                "No setup UI is available (needs MANAGER_MCP_TRANSPORT=http). "
                "Save provider config via bank_feed_providers.feeds_config.save() "
                "or its default file directly, then retry -- no restart needed."
            )
        return out

    if not client.has_ui_auth:
        return _not_configured(
            "MANAGER_UI_USERNAME and MANAGER_UI_PASSWORD are required -- every "
            "bank-feed provider needs the mcp user (HTTP Basic Auth)."
        )
    try:
        provider = select_provider()
    except ConfigError as exc:
        return _not_configured(str(exc))
    try:
        result = await provider.sync(client)
    except ConfigError as exc:
        return _not_configured(str(exc), provider.name)
    return {"configured": True, "provider": provider.name, **result}


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


def start_bank_feed_sync_thread(
    interval: float, *, setup_url: str | None = None
) -> threading.Thread:
    """Daemon thread with its own event loop and ManagerClient.

    `setup_url` (the deployment's `/setup/bank-feeds` page, when the http
    transport is running) is only used to make a "not configured" result
    actionable -- point the operator at the config UI. The loop starts
    regardless of whether a provider is configured yet: `sync_or_report`
    just reports "not configured" each attempt until it is.
    """

    def _run() -> None:
        async def _loop() -> None:
            client = ManagerClient.from_env()
            try:
                await run_bank_feed_sync_loop(
                    interval=interval,
                    sync=lambda: sync_or_report(client, setup_url=setup_url),
                )
            finally:
                await client.aclose()

        asyncio.run(_loop())

    thread = threading.Thread(target=_run, name="bank-feed-sync", daemon=True)
    thread.start()
    _log.info(
        "bank-feed sync scheduled every %s seconds (provider re-selected each attempt)",
        interval,
    )
    return thread
