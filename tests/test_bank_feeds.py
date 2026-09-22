"""Bank-feed sync interval parsing and the generic retry loop (no provider-specific
logic here -- see test_bank_feed_providers_basiq.py and test_bank_feed_providers_registry.py
for that, and test_sync_bank_feeds_tool.py for sync_or_report)."""

from __future__ import annotations

import asyncio

import pytest

from manager_mcp.bank_feeds import (
    INTERVAL_ENV,
    bank_feed_sync_interval_seconds,
    run_bank_feed_sync_loop,
)


def test_interval_unset_disables() -> None:
    assert bank_feed_sync_interval_seconds({}) is None


def test_interval_hourly() -> None:
    assert bank_feed_sync_interval_seconds({INTERVAL_ENV: "3600"}) == 3600.0


@pytest.mark.asyncio
async def test_loop_logs_failure_and_continues() -> None:
    calls = {"n": 0}

    async def sync() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("nope")
        raise asyncio.CancelledError()

    async def sleep(seconds: float) -> None:
        if calls["n"] >= 2:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_bank_feed_sync_loop(
            interval=1, sync=sync, sleep=sleep, startup_delay=0
        )
    assert calls["n"] >= 2
