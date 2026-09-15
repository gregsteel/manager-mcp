# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- Hourly bank-feed sync: when `MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS`
  is set (compose uses 3600), run whichever bank-feed provider is configured
  as the mcp user. Unset or 0 leaves the loop off.

- Pluggable bank-feed providers (`src/manager_mcp/bank_feed_providers/`):
  a `BankFeedProvider` interface (`is_configured`, `sync`, optional
  `setup_state`) with `basiq` (Aussie Bank Feeds) built in, now configured
  via `MANAGER_MCP_BASIQ_ACCOUNT_LINKS` / `_DEDUP_FIELD` / `_TIMEZONE`
  instead of hardcoded account links and a hardcoded business name.
  `MANAGER_MCP_BANK_FEED_PROVIDER` selects a provider explicitly; unset,
  the first configured provider wins. The setup UI's provider picker has
  an "Other…" entry with instructions for adding a new one.

- Removed the built-in "Check for New Transactions" fallback provider (GET
  `/check-for-new-transactions` as the mcp user). It only ever worked
  against older Manager installs that predate the Aussie Bank Feeds
  extension; every deployment this runs against already has that
  extension, so the fallback never actually imported anything.

- Bank-feed provider config now lives in its own file
  (`bank_feed_providers/feeds_config.py`, default
  `/secrets/manager/feeds.config`, path overridable via
  `MANAGER_MCP_BANK_FEED_CONFIG_PATH`) instead of `secrets/manager-mcp.env`.
  `feeds_config.effective_environ()` overlays it on `os.environ`, read
  fresh on every provider call, so a saved change takes effect on the next
  scheduled sync or `sync_bank_feeds` call with no restart. Compose mounts
  `./secrets/manager` read-write into manager-mcp for this; it's
  `.gitignore`'d.

- Browser setup UI at `/setup/bank-feeds` (`MANAGER_MCP_TRANSPORT=http`
  only), gated by the same Google OAuth client/allowlist as the MCP
  transport (needs `{MANAGER_MCP_OAUTH_BASE_URL}/setup/callback` added to
  the OAuth client's redirect URIs). Detects Manager bank accounts,
  business name, and custom fields already in use from `/api2`/`/api4`;
  only prompts for what a provider can't read from Manager (and, given a
  Basiq login, live-detects its accounts too). Saves straight to the
  config file above; never writes to Manager or to `manager-mcp.env`.

- `sync_bank_feeds` MCP tool (banking scope): triggers a sync immediately
  instead of waiting for the timer. Returns `{"configured": false,
  "setup_url": ..., "hint": ...}` rather than raising when nothing is
  configured yet, so an agent can point the user at the setup UI.

- `search_line_items` tool: searches text inside line-item descriptions
  (`Lines[].Description`) for sales_invoices, purchase_invoices, and other
  form-backed collections. `list_records`' `term` only matches header fields
  (Reference, Customer/Supplier, header Description) and misses text that
  only appears on a line.

## [0.2.6] - 2026-08-03

### Fixed

- Claude Desktop runtime: use `uv tool run manager-mcp` in mcpb mcp_config.
  Claude maps `server.type: uv` to `uv.exe` and was running `uv manager-mcp`
  instead of `uvx manager-mcp`.

## [0.2.5] - 2026-08-03

### Fixed

- Claude Desktop extension install on Windows: drop manager-mcp from mcpb
  pyproject dependencies (install ran uv sync and failed on cffi without win
  wheels). Runtime still uses uvx; add mcpb/.mcpbignore for .venv and uv.lock.

## [0.2.4] - 2026-08-03

### Fixed

- Claude Desktop extension: migrate MCPB to manifest 0.4 / `uv` server type and drop
  system Python runtime check (uses `uvx`; only requires `uv` on PATH)

## [0.2.3] - 2026-08-03

### Added

- Claude Desktop extension pack (`mcpb.mcpb`) for one-click install via Claude Desktop
- Serena project config (`.serena/`) for symbol-aware agent editing

### Changed

- Refactor: reduce complexity from ponytail audit (-56 lines)

### Fixed

- CI: resolve ruff lint failures from refactor commit (F401, I001, E501)

## [0.2.0] - 2026-07-29

### Added

- 13 intent-shaped task tools replacing generated CRUD as the recommended write path
- Customer deposit workflow (`record_customer_deposit`, `issue_deposit_invoice`, `apply_deposit_to_invoice`)
- `PreconditionResult` pattern for structured setup guidance when instance preconditions fail
- `raw` escape-hatch scope restoring the full CRUD set for advanced use
- `server.json` for MCP Registry discoverability
- Explicit `[tool.hatch.build.targets.sdist]` include list
- Live sandbox integration test suite (`pytest -m integration`, `TEST_MANAGER_API_*` env vars)

### Changed

- `pyproject.toml` description reflects read-first with opt-in scoped writes
- `skills/manager-accounting/SKILL.md`: task tool guidance, deposit is-not-revenue statement
- `README.md`: task tool table, deposit docs, recommended scope config, migration notice

### Deprecated

- All per-resource CRUD `create_*` / `update_*` / `delete_*` tools except `create_customer` and `create_supplier`. Removal target: **0.3.0**.

## [0.1.1] - 2026-07-28

### Fixed

- Banking write hardening, persistence warnings, and scope registration fixes

## [0.1.0] - 2026-07-28

### Added

- Initial release: 10 read tools, 72 scoped CRUD write tools, denylist, agent skill
