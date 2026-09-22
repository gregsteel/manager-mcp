"""Provider selection: explicit env var, or auto-detect by is_configured()."""

from __future__ import annotations

import pytest

from manager_mcp.bank_feed_providers.basiq import BASIQ_PASSWORD_ENV, BASIQ_USERNAME_ENV
from manager_mcp.bank_feed_providers.registry import PROVIDER_ENV, select_provider
from manager_mcp.client import ConfigError


def test_auto_selects_basiq_when_configured() -> None:
    provider = select_provider({BASIQ_USERNAME_ENV: "u", BASIQ_PASSWORD_ENV: "p"})
    assert provider.name == "basiq"


def test_raises_when_nothing_configured() -> None:
    with pytest.raises(ConfigError, match="No bank-feed provider"):
        select_provider({})


def test_explicit_provider_env_overrides_auto_detection() -> None:
    provider = select_provider(
        {
            PROVIDER_ENV: "basiq",
            BASIQ_USERNAME_ENV: "u",
            BASIQ_PASSWORD_ENV: "p",
        }
    )
    assert provider.name == "basiq"


def test_unknown_explicit_provider_raises() -> None:
    with pytest.raises(ConfigError, match="Unknown"):
        select_provider({PROVIDER_ENV: "not-a-real-provider"})
