"""Bank-feed provider config, stored as its own secrets file instead of env
vars -- so a Basiq username/password (or any future provider's credentials)
set through the setup UI don't have to live in `secrets/manager-mcp.env`
and don't require a restart to take effect.

The file is a flat JSON object of the same env-var names the providers
already read (`BASIQ_USERNAME`, `MANAGER_MCP_BASIQ_ACCOUNT_LINKS`, ...), so
nothing downstream needs to know the config came from a file rather than
the process environment: `effective_environ()` overlays it on `os.environ`
and every provider function that takes an `environ` argument reads that.

Where the file lives is itself an env var (`MANAGER_MCP_BANK_FEED_CONFIG_PATH`,
default `/secrets/manager/feeds.config`) since the path isn't secret, only
its contents are.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

_log = logging.getLogger(__name__)

CONFIG_PATH_ENV = "MANAGER_MCP_BANK_FEED_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "/secrets/manager/feeds.config"


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path((env.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH).strip())


def load(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The saved config, or {} if there isn't one yet (first run) or it
    can't be read/parsed (logged, not raised -- a broken config file
    shouldn't take down the whole sync loop, just leave nothing detected)."""
    path = config_path(environ)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        _log.exception("bank-feed config: could not read %s", path)
        return {}
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        _log.error("bank-feed config: %s must be a JSON object of string -> string", path)
        return {}
    return data


def effective_environ(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`os.environ` (or `environ`, for tests) with the saved config layered
    on top -- what every provider should actually read config from."""
    base = os.environ if environ is None else environ
    return {**base, **load(base)}


def save(updates: Mapping[str, str], environ: Mapping[str, str] | None = None) -> Path:
    """Merge `updates` into the existing saved config and write it back.
    An empty string value deletes that key (lets the setup UI clear a field).
    Written atomically with owner-only permissions, since this holds
    provider credentials."""
    path = config_path(environ)
    current = load(environ)
    for key, value in updates.items():
        if value:
            current[key] = value
        else:
            current.pop(key, None)

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path
