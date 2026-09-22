# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

Everything below is fork-specific, added on top of upstream
[flumpiey/manager-mcp](https://github.com/flumpiey/manager-mcp) (diverged at
`c50c4d7`), covering the remote HTTP/OAuth transport, intent-shaped task
tools, file attachments, and bank-feed automation.

### Added

- Remote Streamable HTTP transport with Google OAuth and email allowlisting
- Intent-shaped task tools (issue/record/post/void/etc.) replacing raw CRUD as the recommended write path
- File-attachment support for purchase invoices via undocumented Manager endpoints
- `ManagerClient` UI-session auth (HTTP Basic) for endpoints that ignore the API key
- Pluggable bank-feed providers with Basiq (Aussie Bank Feeds) built in, hourly sync, and a browser setup UI
- `search_line_items` tool for text search inside line-item descriptions
- `payment_rules` and `receipt_rules` collections for transaction auto-categorization
- Docker packaging for running manager-mcp as a container

### Fixed

- Read-only tools now declare `readOnlyHint: true`, fixing spurious approval prompts in some MCP clients

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
