"""Bank-feed provider config file: save/load/merge/atomic write."""

from __future__ import annotations

import json

import pytest

from manager_mcp.bank_feed_providers import feeds_config


@pytest.fixture
def config_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    path = tmp_path / "manager" / "feeds.config"
    env = {feeds_config.CONFIG_PATH_ENV: str(path)}
    monkeypatch.setenv(feeds_config.CONFIG_PATH_ENV, str(path))
    return env


def test_load_missing_file_returns_empty(config_env: dict[str, str]) -> None:
    assert feeds_config.load(config_env) == {}


def test_save_then_load_round_trips(config_env: dict[str, str]) -> None:
    path = feeds_config.save({"BASIQ_USERNAME": "u", "BASIQ_PASSWORD": "p"}, config_env)
    assert path == feeds_config.config_path(config_env)
    assert feeds_config.load(config_env) == {"BASIQ_USERNAME": "u", "BASIQ_PASSWORD": "p"}


def test_save_merges_with_existing_and_creates_parents(config_env: dict[str, str]) -> None:
    feeds_config.save({"A": "1"}, config_env)
    feeds_config.save({"B": "2"}, config_env)
    assert feeds_config.load(config_env) == {"A": "1", "B": "2"}


def test_save_empty_value_deletes_key(config_env: dict[str, str]) -> None:
    feeds_config.save({"A": "1", "B": "2"}, config_env)
    feeds_config.save({"A": ""}, config_env)
    assert feeds_config.load(config_env) == {"B": "2"}


def test_file_permissions_are_owner_only(config_env: dict[str, str]) -> None:
    path = feeds_config.save({"SECRET": "x"}, config_env)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_effective_environ_passes_through_unrelated_keys(tmp_path) -> None:
    # An unrelated config path (no file there) shouldn't affect other keys.
    base = {
        "MANAGER_API_URL": "http://example.test",
        feeds_config.CONFIG_PATH_ENV: str(tmp_path / "nope.config"),
    }
    merged = feeds_config.effective_environ(base)
    assert merged["MANAGER_API_URL"] == "http://example.test"


def test_effective_environ_file_overrides_base(config_env: dict[str, str], tmp_path) -> None:
    feeds_config.save({"BASIQ_USERNAME": "from-file"}, config_env)
    base = {**config_env, "BASIQ_USERNAME": "from-env"}
    merged = feeds_config.effective_environ(base)
    assert merged["BASIQ_USERNAME"] == "from-file"


def test_load_rejects_non_object_json(tmp_path, config_env: dict[str, str]) -> None:
    path = feeds_config.config_path(config_env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["not", "an", "object"]))
    assert feeds_config.load(config_env) == {}


def test_load_rejects_invalid_json(tmp_path, config_env: dict[str, str]) -> None:
    path = feeds_config.config_path(config_env)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert feeds_config.load(config_env) == {}
