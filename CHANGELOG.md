# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

Everything below is fork-specific, added on top of upstream
[flumpiey/manager-mcp](https://github.com/flumpiey/manager-mcp) (diverged at
`c50c4d7`), covering the remote HTTP/OAuth transport, intent-shaped task
tools, file attachments, and bank-feed automation.

### Added

- Remote Streamable HTTP transport (`MANAGER_MCP_TRANSPORT=http`) with
  Google OAuth (`http_auth.py`): `AllowlistedGoogleProvider` layers an email
  allowlist (`MANAGER_MCP_ALLOWED_EMAILS`) on top of fastmcp's
  `GoogleProvider`, which alone would accept any Google account that
  completes login. Meant to sit behind a TLS-terminating reverse proxy, for
  a remote connector such as Claude Cowork.

- Intent-shaped task tools (`task_tools.py`), registered per write scope in
  place of raw per-resource CRUD: `issue_sales_invoice`,
  `issue_purchase_invoice`, `issue_quote`, `issue_deposit_invoice`,
  `convert_quote_to_invoice`, `record_customer_payment`,
  `record_supplier_payment`, `record_customer_deposit`,
  `transfer_between_accounts`, `record_expense`, `post_journal_entry`,
  `apply_deposit_to_invoice`, `void_document`, and
  `attach_receipt_to_purchase_invoice`. Each composes validate + persist +
  verify into one call instead of a bare create/update. Per-resource
  `create_*`/`update_*`/`delete_*` CRUD tools are now deprecated (removed in
  0.3.0) and only register when `raw` is in `MANAGER_MCP_WRITE_SCOPES`/
  `MANAGER_MCP_DELETE_SCOPES`.

- File-attachment support (`attachments_api.py`): reverse-engineered,
  undocumented Manager endpoints for attaching a file to a purchase invoice
  — `attach_file_via_api` (the general Attachments list) and
  `attach_image_field_via_api` (the legacy single-file Image field). These
  need a real logged-in Manager UI session, not just the `/api2` API key.

- `ManagerClient` UI-session auth: `MANAGER_UI_USERNAME`/`MANAGER_UI_PASSWORD`
  (HTTP Basic Auth as a real Manager user) plus `raw_get`/`raw_post`/
  `raw_basic` methods, for the handful of endpoints (attachments, `/api4`
  batch writes) that ignore `X-API-KEY` and require a logged-in session.

- Pluggable bank-feed providers (`bank_feed_providers/`): a
  `BankFeedProvider` interface with `basiq` (Aussie Bank Feeds) built in,
  configured via `MANAGER_MCP_BASIQ_ACCOUNT_LINKS`/`_DEDUP_FIELD`/
  `_TIMEZONE`. `MANAGER_MCP_BANK_FEED_PROVIDER` picks one explicitly; unset,
  the first configured provider wins. Removed the old built-in "Check for
  New Transactions" fallback — it only worked on pre-Aussie-Bank-Feeds
  Manager installs, which nothing here runs against.
  - Hourly sync loop (`MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS`, compose
    uses 3600) plus an on-demand `sync_bank_feeds` tool that returns
    `{"configured": false, "setup_url": ...}` instead of erroring when
    nothing's configured yet.
  - Provider config lives in its own file (`feeds_config.py`, path in
    `MANAGER_MCP_BANK_FEED_CONFIG_PATH`) instead of `manager-mcp.env`, read
    fresh on every call so a saved change needs no restart.
  - Browser setup UI at `/setup/bank-feeds` (http transport only, same OAuth
    allowlist): reads what it can from Manager (accounts, business name,
    custom fields) and only asks for what a provider can't supply.

- `search_line_items` tool: text search inside line-item descriptions,
  which `list_records`' header-only `term` search misses.

- `payment_rules` and `receipt_rules` collections for `list_records` /
  `get_record` — auto-categorization rules for outgoing and incoming bank
  transactions respectively (`payment_rules` was formerly named "Bank
  Rules" in the Manager UI). Both are list/search only; the Manager API has
  no create/update/delete endpoint for either.

- Docker packaging (`Dockerfile`, `.dockerignore`, `.env.example`) for
  running manager-mcp as a container — previously stdio/local-host only.

### Fixed

- Read-only tools (`list_resources`, `list_records`, `get_record`,
  `search_line_items`, and the report tools including `bank_balances`) now
  declare `readOnlyHint: true` MCP annotations. Previously they had none,
  which some MCP clients (e.g. Claude Cowork) treat as "not confirmed
  read-only" and gate behind an approval prompt on every call — seen as
  `bank_balances` repeatedly needing/failing approval even though the
  connection to Manager was fine.

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
