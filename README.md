<p align="center">
  <a href="https://www.manager.io/">
    <img src="docs/manager-icon.svg" alt="Manager.io" width="72" height="72">
  </a>
</p>

# manager-mcp

<!-- mcp-name: io.github.flumpiey/manager-mcp -->

> This repository is a fork of the upstream [flumpiey/manager-mcp](https://github.com/flumpiey/manager-mcp) project. The work on this branch adds Manager.io MCP support for remote HTTP hosting, OAuth-backed access, and file attachment workflows beyond the upstream stdio-first implementation.

**MCP server for self-hosted [Manager.io](https://www.manager.io/): ask your AI about invoices, balances, and books.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio-green.svg)](https://modelcontextprotocol.io/)
[![CI](https://github.com/flumpiey/manager-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/flumpiey/manager-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/manager-mcp.svg)](https://pypi.org/project/manager-mcp/)

## What is Manager.io?

[Manager.io](https://www.manager.io/) is free, self-hosted accounting software for Windows, macOS, and Linux (also available as [Cloud Edition](https://www.manager.io/cloud-edition)). It covers sales, purchases, banking, payroll, and the full ledger, with an HTTP API (`/api2`) for automation.

This project wires that API into the [Model Context Protocol](https://modelcontextprotocol.io/) so Cursor, Claude, VS Code Copilot, and other MCP hosts can query your live books in natural language.

Useful Manager.io links:

- [Download](https://www.manager.io/download)
- [Guides](https://www.manager.io/guides)
- [Forum](https://forum.manager.io)
- [Releases](https://www.manager.io/releases)

## What this server does

Default is **read-only**. You get:

- **10 read tools** - discovery, six searchable collections, seven report shortcuts
- **Task tools (opt-in)** - intent-shaped writes such as `record_customer_payment`, `issue_sales_invoice`, `record_customer_deposit` (register when matching write scopes are set)
- **Deprecated CRUD tools** - per-resource `create_*` / `update_*` / `delete_*` still register under scopes until **0.3.0**; prefer task tools
- **`raw` escape hatch** - restores the full CRUD set for advanced use
- **Hard denylist** - access tokens, chart of accounts forms, tax/currency, email templates, and similar high-risk paths stay blocked even when writes are on

Transport is **stdio** by default. No HTTP server. No global install required if you use [`uv`](https://docs.astral.sh/uv/) / `uvx`.

## Fork-specific changes on this branch

This fork adds a few capabilities that are not part of the upstream stdio-first server:

- **Streamable HTTP transport** with `MANAGER_MCP_TRANSPORT=http` for remote MCP hosting
- **Google OAuth session handling** for browser-based remote access, with email allowlisting and callback enforcement
- **Health-check endpoint support** for deployment readiness and uptime monitoring
- **File attachment support** using Manager's undocumented `NewAttachment` action flow for invoice and other supported attachments
- **Purchase invoice attachment handling** and client-side support for Manager attachment payloads
- **Line-item search improvements** via `search_line_items`, including fixes for description and key-name normalization
- **Auth timeout configuration** for HTTP-backed sessions so remote deployments can tune idle expiry
- **Hourly bank-feed sync** via a pluggable provider (Aussie Bank Feeds/Basiq built in; add your own for anything else), enabled with `MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS` — see [Bank-feed providers](#bank-feed-providers) below
- **Browser setup UI** at `/setup/bank-feeds` (same Google login as the MCP transport) for configuring a bank-feed provider without hand-editing env vars

These additions are designed for hosted or browser-connected deployments while preserving the original read-only defaults and write-scope model for local stdio use.

### New tools and capabilities in this fork

The branch adds features that are especially relevant for hosted deployments and document-heavy accounting workflows:

- `search_line_items`
  - searches text inside line item descriptions on Manager records such as invoices and quotes
  - works around the limitation of `list_records` / `term`, which only searches header-level fields and misses text stored in individual line items
  - returns paged results by fetching the underlying record forms and scanning the line descriptions

- `attach_receipt_to_purchase_invoice`
  - attaches a receipt image or PDF to a purchase invoice using Manager's undocumented `NewAttachment` action endpoint
  - accepts a local file path, raw base64 content, or a remote HTTP(S) URL
  - is intended for purchase invoice document workflows where the attachment has to be associated with the correct Manager record

- HTTP transport mode for remote MCP hosting
  - exposes the server over Streamable HTTP instead of stdio
  - integrates with Google OAuth, a public base URL, and a permitted-email allowlist
  - suitable for hosted MCP connectors that need browser-based login rather than a local machine process

- Auth/session management for remote deployments
  - supports configurable session timeout values for the HTTP transport
  - keeps the local stdio workflow unchanged when no remote transport is configured

- Health-check support
  - includes a lightweight readiness/health endpoint so the service can be monitored by a reverse proxy or deployment system

- Bank-feed sync on a timer
  - runs inside this process when `MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS` is a positive number; unset or 0 disables it
  - always authenticates as the mcp user (`MANAGER_UI_USERNAME` / `MANAGER_UI_PASSWORD`, HTTP Basic Auth) — every provider needs it
  - which mechanism runs is pluggable; see [Bank-feed providers](#bank-feed-providers)

## Branding / icons

- **stdio hosts (Cursor, Claude Desktop via `mcp.json`):** the server advertises Manager branding in MCP `serverInfo.icons` (embedded PNG data URI, plus a GitHub raw HTTPS fallback).
- **Cursor plugin:** [`.cursor-plugin/plugin.json`](.cursor-plugin/plugin.json) uses [`docs/manager-icon.svg`](docs/manager-icon.svg).
- **Claude Desktop Extension:** pack [`mcpb/`](mcpb/) (includes `icon.png`). See Installation → Claude Desktop below.
- **Claude.ai remote connectors:** Claude.ai ignores `serverInfo.icons` and uses the **root-domain favicon** of the connector URL. If you host a remote MCP later, serve [`docs/favicon.ico`](docs/favicon.ico) at the registrable domain root (e.g. `https://acme.com/favicon.ico` for `https://mcp.acme.com/...`).

## Requirements

- Python ≥ 3.10 (pulled in automatically by `uvx`)
- [uv](https://docs.astral.sh/uv/) (provides `uvx`)
- A reachable Manager.io API: `MANAGER_API_URL` + `MANAGER_API_KEY`

### Access token

1. In Manager, open **Settings → Access Tokens**.
2. Create a token and copy the value into `MANAGER_API_KEY`.
3. Set `MANAGER_API_URL` to your API base (desktop often `http://127.0.0.1:55667/api2`).

`manager-mcp` sends the token as the `X-API-KEY` header. Full walkthrough: [Access Tokens](https://www.manager.io/guides/access-tokens).

## Quick start

Run the [PyPI](https://pypi.org/project/manager-mcp/) package with [`uvx`](https://docs.astral.sh/uv/guides/tools/):

```bash
uvx manager-mcp
```

Paste a client config below, set `MANAGER_API_URL` / `MANAGER_API_KEY`, restart the host, then ask: *“Who owes me money?”* or *“Show bank balances.”*

From a git clone (dev): `uvx --from git+https://github.com/flumpiey/manager-mcp manager-mcp` or `uv run --directory /path/to/manager-mcp manager-mcp`.

## Installation

Configs below pull [`manager-mcp`](https://pypi.org/project/manager-mcp/) from PyPI. Leave write-scope env vars unset for read-only.

<details>
<summary><strong>Cursor</strong></summary>

**Plugin (Configure UI for URL, key, and scopes):** this repo is a Cursor plugin via [`.cursor-plugin/plugin.json`](.cursor-plugin/plugin.json) + root [`mcp.json`](mcp.json).

1. Symlink or copy the clone to `~/.cursor/plugins/local/manager-mcp` (Windows: `%USERPROFILE%\.cursor\plugins\local\manager-mcp`).
2. Reload the window.
3. Open **Plugins → Configure** on `manager-mcp`. Set **Manager API URL** and **Manager API key**. Leave **Write scopes** / **Delete scopes** empty for read-only, or paste a CSV such as `quotes` or `quotes,orders`.
4. Confirm the `manager` MCP server is enabled under Customize / MCP.

Marketplace listing is a separate submit at [cursor.com/marketplace/publish](https://cursor.com/marketplace/publish).

**Manual `mcp.json`:** project [`.cursor/mcp.json`](.cursor/mcp.json) or user-wide `~/.cursor/mcp.json`.

From PyPI:

```json
{
  "mcpServers": {
    "manager": {
      "type": "stdio",
      "command": "uvx",
      "args": ["manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

Local editable (dev):

```json
{
  "mcpServers": {
    "manager": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/manager-mcp", "manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

Optional scoped writes in the `env` block:

```json
"MANAGER_MCP_WRITE_SCOPES": "quotes",
"MANAGER_MCP_DELETE_SCOPES": "quotes"
```

Restart Cursor after saving. Confirm `manager` under MCP settings.

</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

**Desktop Extension (`.mcpb`):** download [`mcpb.mcpb`](https://github.com/flumpiey/manager-mcp/releases/latest/download/mcpb.mcpb) from [GitHub Releases](https://github.com/flumpiey/manager-mcp/releases). Use **v0.2.6** or later.

1. Open Claude Desktop → **Settings → Extensions**.
2. Open **Advanced settings** → **Install Extension…**
3. Select `mcpb.mcpb`. Review permissions, enter **Manager API URL** and **Manager API key**, then click **Install**.
4. Leave **Write scopes** and **Delete scopes** empty for read-only.
5. Restart Claude Desktop if tools do not appear.

Build your own bundle from a clone:

```bash
npx @anthropic-ai/mcpb pack mcpb
```

On Windows, double-click often does nothing and dragging the file into chat attaches it to the conversation instead of installing it. Use **Install Extension…** in Settings.

**Manual `mcp.json` config:** edit the Claude Desktop config, then restart the app.

| OS | Path |
|----|------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "manager": {
      "command": "uvx",
      "args": ["manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

Local clone:

```json
{
  "mcpServers": {
    "manager": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/manager-mcp", "manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

</details>

<details>
<summary><strong>Claude Code</strong></summary>

Add via CLI:

```bash
claude mcp add manager --env MANAGER_API_URL=http://127.0.0.1:55667/api2 --env MANAGER_API_KEY=your-token -- uvx manager-mcp
```

Or edit `~/.claude.json` / project MCP config:

```json
{
  "mcpServers": {
    "manager": {
      "command": "uvx",
      "args": ["manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

</details>

<details>
<summary><strong>VS Code / GitHub Copilot</strong></summary>

Create [`.vscode/mcp.json`](.vscode/mcp.json) in the project root:

```json
{
  "servers": {
    "manager": {
      "type": "stdio",
      "command": "uvx",
      "args": ["manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

Local editable:

```json
{
  "servers": {
    "manager": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/manager-mcp", "manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

Reload the window. Open Copilot Chat and confirm the `manager` tools are available.

</details>

<details>
<summary><strong>Windsurf</strong></summary>

Edit `~/.codeium/windsurf/mcp_config.json` (macOS/Linux) or the Windsurf MCP settings UI:

```json
{
  "mcpServers": {
    "manager": {
      "command": "uvx",
      "args": ["manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

Restart Windsurf after saving.

</details>

<details>
<summary><strong>Zed</strong></summary>

Add under `context_servers` in Zed `settings.json` (Agent Panel → settings also works):

```json
{
  "context_servers": {
    "manager": {
      "command": "uvx",
      "args": ["manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

</details>

<details>
<summary><strong>Cline</strong></summary>

Edit the Cline MCP settings file (`cline_mcp_settings.json` via the Cline MCP UI):

```json
{
  "mcpServers": {
    "manager": {
      "command": "uvx",
      "args": ["manager-mcp"],
      "env": {
        "MANAGER_API_URL": "http://127.0.0.1:55667/api2",
        "MANAGER_API_KEY": "your-token"
      }
    }
  }
}
```

</details>

<details>
<summary><strong>Continue</strong></summary>

In `.continue/config.yaml`:

```yaml
mcpServers:
  - name: manager
    command: uvx
    args:
      - manager-mcp
    env:
      MANAGER_API_URL: http://127.0.0.1:55667/api2
      MANAGER_API_KEY: your-token
```

</details>

<details>
<summary><strong>Generic / any stdio MCP host</strong></summary>

Any host that can spawn a stdio MCP server:

| Field | Value |
|-------|-------|
| Command | `uvx` |
| Args | `manager-mcp` |
| Env | `MANAGER_API_URL`, `MANAGER_API_KEY` (+ optional write scopes) |

```bash
uvx manager-mcp
```

Dev from a clone: `uv run --directory /path/to/manager-mcp manager-mcp`.

`npx` only runs npm packages. This is a Python package; use `uvx`.

</details>

## Environment

| Variable | Required | Notes |
|----------|----------|-------|
| `MANAGER_API_URL` | yes | Opaque base URL (include `/api2` when needed) |
| `MANAGER_API_KEY` | yes | Sent as `X-API-KEY`; never logged |
| `MANAGER_MCP_WRITE_SCOPES` | no | Comma-separated domains for create/update. Empty = no writes. |
| `MANAGER_MCP_DELETE_SCOPES` | no | Comma-separated domains for delete only. Never implied by WRITE_SCOPES. |
| `MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS` | no | Seconds between bank-feed imports, via whichever provider is configured (mcp user Basic Auth). Unset or 0 disables (default). Compose sets `3600`. |
| `MANAGER_MCP_BANK_FEED_CONFIG_PATH` | no | Where provider config (below) is saved/read. Default `/app/feeds.config` (this app's `WORKDIR`); compose overrides it to the shared `/secrets/manager/feeds.config`. See [Config lives in a file, not env vars](#config-lives-in-a-file-not-env-vars). |

The rest of a provider's config (`MANAGER_MCP_BANK_FEED_PROVIDER`, `BASIQ_USERNAME`/`BASIQ_PASSWORD`, `MANAGER_MCP_BASIQ_ACCOUNT_LINKS`, `MANAGER_MCP_BASIQ_DEDUP_FIELD`, `MANAGER_MCP_BASIQ_TIMEZONE`, `MANAGER_MCP_BASIQ_LOOKBACK_DAYS`) is normally set via `/setup/bank-feeds`, saved to the file above, not this table's env vars — see [Bank-feed providers](#bank-feed-providers). They're still read as plain env vars too (e.g. for local dev), same names, if you'd rather set them that way.

Valid scopes: `quotes`, `orders`, `parties`, `items`, `sales`, `purchases`, `banking`, `payroll`, `ledger`, `raw`. No wildcards (`*`, `all`).

**Recommended** (covers most bookkeeping without 82 tools):

```json
"MANAGER_MCP_WRITE_SCOPES": "banking,sales,parties",
"MANAGER_MCP_DELETE_SCOPES": "sales,banking"
```

Default with no scopes: **10 tools**. All nine domain scopes plus every CRUD verb: up to **82 tools**. Use `raw` only when you need the full CRUD escape hatch.

Legacy `MANAGER_MCP_ALLOW_WRITES` / `ALLOW_WRITES` / `MANAGER_MCP_WRITES` hard-fail if set. Use the scoped vars instead.

See [`.env.example`](.env.example). Prefer a secret manager for the API key in production configs.

### Remote access (Streamable HTTP + Google OAuth)

By default `manager-mcp` runs over stdio and has no transport-level auth of its own —
whatever launches the process controls access. Set `MANAGER_MCP_TRANSPORT=http` to
run Streamable HTTP instead, for a remote connector such as Claude Cowork. This
requires Google OAuth config and is meant to sit behind a reverse proxy that
terminates TLS.

| Variable | Required for `http` | Notes |
|----------|----------|-------|
| `MANAGER_MCP_TRANSPORT` | — | `stdio` (default) or `http`. |
| `MANAGER_MCP_HTTP_HOST` | no | Default `0.0.0.0`. |
| `MANAGER_MCP_HTTP_PORT` | no | Default `8080`. |
| `MANAGER_MCP_OAUTH_GOOGLE_CLIENT_ID` | yes | Google OAuth client, separate from any other service's. |
| `MANAGER_MCP_OAUTH_GOOGLE_CLIENT_SECRET` | recommended | Omit only for a PKCE public client. |
| `MANAGER_MCP_OAUTH_BASE_URL` | yes | Public HTTPS URL, e.g. `https://manager-mcp.example.com`. Redirect URI is `{base_url}/auth/callback`. |
| `MANAGER_MCP_ALLOWED_EMAILS` | yes | Comma-separated. Google OAuth alone accepts any Google account that logs in; this allowlist is enforced on top of it. |

## Write scopes and task tools

When a scope is listed in `MANAGER_MCP_WRITE_SCOPES`, the server registers **task tools** for that domain plus deprecated CRUD twins. `MANAGER_MCP_DELETE_SCOPES` enables `void_document` and `delete_*` per domain.

### Task tools (preferred)

| Tool | Scopes | Purpose |
|------|--------|---------|
| `create_customer`, `create_supplier` | parties | Single-resource party setup |
| `issue_sales_invoice` | sales | Issue invoice with inline lines |
| `issue_purchase_invoice` | purchases | Issue purchase invoice |
| `issue_quote` | quotes | Issue sales or purchase quote |
| `convert_quote_to_invoice` | quotes + sales | Convert quote to invoice |
| `record_customer_payment` | banking | Receipt + invoice allocation |
| `record_supplier_payment` | banking | Payment + invoice allocation |
| `record_expense` | payroll and/or purchases | Expense claim or purchase invoice |
| `transfer_between_accounts` | banking | Inter-account transfer |
| `post_journal_entry` | ledger | Generic journal entry |
| `void_document` | matching delete scope | Void by resource name + key |
| `record_customer_deposit` | banking | Deposit before invoice exists |
| `issue_deposit_invoice` | quotes | Deposit document (quote) |
| `apply_deposit_to_invoice` | ledger | Apply deposit via journal |

Bodies for composite tools use Manager-native JSON where noted. Clone `get_record` templates; do not invent field names.

### Deprecated CRUD (0.2.0, removed 0.3.0)

Per-resource `create_*` / `update_*` / `delete_*` still register when their domain scope is enabled. Descriptions are prefixed `[DEPRECATED in 0.2.0; use task tools]` except `create_customer` / `create_supplier`. Set `raw` in `MANAGER_MCP_WRITE_SCOPES` to register CRUD without deprecation prefixes.

| Scope | Resources (CRUD when enabled) |
|-------|-------------------------------|
| `quotes` | sales_quotes, purchase_quotes |
| `orders` | sales_orders, purchase_orders |
| `parties` | customers, suppliers |
| `items` | inventory_items, non_inventory_items |
| `sales` | sales_invoices, credit_notes, delivery_notes |
| `purchases` | purchase_invoices, debit_notes, goods_receipts |
| `banking` | receipts, payments, inter_account_transfers, bank_accounts |
| `payroll` | employees, payslips, expense_claims |
| `ledger` | journal_entries, depreciation_entries, amortization_entries |

Example with recommended scopes only:

```json
"MANAGER_MCP_WRITE_SCOPES": "banking,sales,parties",
"MANAGER_MCP_DELETE_SCOPES": "sales"
```

**Denylist (always blocked):** access-token forms, chart-of-accounts / `*-account-form` (except bank-or-cash), bank reconciliation, customer portal, starting balances, tax codes, exchange rates, currencies, custom fields/buttons, themes, email templates/settings.

## Customer deposit workflow

A **deposit is not revenue**. Money received before delivery must not be booked to an income account. Confirm tax/VAT treatment with your accountant.

1. Ensure a **Customer deposits** bank/cash account exists in Manager (Settings → Bank and Cash Accounts).
2. `record_customer_deposit` - posts cash to that account. If the account is missing, the tool returns `precondition_failed` with exact setup steps (Option A: guide only, no auto-create).
3. `issue_deposit_invoice` (optional) - quote styled as a deposit document for the customer.
4. `issue_sales_invoice` when the real invoice is raised.
5. `apply_deposit_to_invoice` - journal entry moving deposit balance to the invoice (clone an existing journal via `get_record`).

Required scopes: `banking`, `quotes` (deposit doc), `ledger` (apply), `sales` (final invoice via MCP).

## Migration from 0.1.x

- **0.2.0**: Task tools added; CRUD tools deprecated but still present under scopes.
- **0.3.0**: CRUD tools removed (except `create_customer` / `create_supplier`). Use task tools or `raw` scope.
- Update `MANAGER_MCP_WRITE_SCOPES` to the recommended narrow set above instead of enabling all domains.

## Tools

### Read tools

| Tool | Purpose | Period (`from_date` / `to_date`) |
|------|---------|----------------------------------|
| `list_resources` | Discovery; reports `read_only` + live write/delete scopes | n/a |
| `list_records` | Search/page a curated collection | n/a |
| `get_record` | Fetch one record via `{path}-form/{key}` | n/a |
| `aged_receivables` | Outstanding / aging customers | Accepted; may be unsupported on this view |
| `aged_payables` | Aging suppliers | Accepted; may be unsupported on this view |
| `bank_balances` | Bank/cash **balances snapshot** | Accepted; may be unsupported on this view |
| `trial_balance` | Trial balance | Forwarded as `fromDate` / `toDate` |
| `profit_and_loss` | P&L | Forwarded as `fromDate` / `toDate` |
| `balance_sheet` | Balance sheet | Forwarded as `fromDate` / `toDate` |
| `tax_summary` | Tax summary | Accepted; may be unsupported on this view |

Collections for `list_records` / `get_record`: `customers`, `suppliers`, `sales_invoices`, `purchase_invoices`, `chart_of_accounts`, `bank_accounts`.

`chart_of_accounts` is list/search only (no single-form GET).

**Bank dual path (intentional):** `bank_balances` answers “what are my balances?”; `list_records` / `get_record` on `bank_accounts` answers “find account X and show detail.”

### Write tools (deprecated)

Registered only for resources in enabled scopes. Prefer task tools above.

| Pattern | Requires | Notes |
|---------|----------|-------|
| `create_{stem}` | write scope | Deprecated in 0.2.0 |
| `update_{stem}` | write scope | Deprecated in 0.2.0 |
| `delete_{stem}` | delete scope | Deprecated in 0.2.0; use `void_document` |

## Agent Skill

Companion skill: [`skills/manager-accounting/SKILL.md`](skills/manager-accounting/SKILL.md).

The Cursor plugin discovers this skill from `skills/`. Without the plugin, copy or symlink that folder into your agent skills path. It tells the model to call `list_resources` first, verify after writes, and which report tools to prefer.

## Bank-feed providers

Getting new bank transactions into Manager is provider-agnostic: `bank_feeds.py` only owns the timer and retry loop, and asks `bank_feed_providers.registry.select_provider()` for whichever provider is configured. One ships built in (`src/manager_mcp/bank_feed_providers/`):

- **`basiq`** — Aussie Bank Feeds / Basiq. Auto-selected once `BASIQ_USERNAME`/`BASIQ_PASSWORD` are set. Needs `MANAGER_MCP_BASIQ_ACCOUNT_LINKS` (JSON, Manager bank account key → Basiq account id) and `MANAGER_MCP_BASIQ_DEDUP_FIELD` (a Manager custom field's key, used to avoid re-importing the same transaction). See `bank_feed_providers/basiq.py`'s module docstring for the reverse-engineering notes.

`MANAGER_MCP_BANK_FEED_PROVIDER` picks one explicitly; left unset, the first configured provider wins (in `PROVIDERS` order in `registry.py`). If nothing is configured, `select_provider()` raises and callers (the sync loop, the `sync_bank_feeds` tool) turn that into a "not configured, here's the setup UI" result rather than a bare error.

There used to be a second built-in provider using Manager's own "Check for New Transactions" control, kept as an always-available fallback. It was removed: that control only exists on older Manager installs that never got the Aussie Bank Feeds extension, and every deployment this runs against already uses Aussie Bank Feeds, so it never actually imported anything -- just failed on every attempt. See git history if a deployment genuinely needs it back. Manager ships other extensions (VAT/GST/BAS return preparation, various countries' e-invoicing, etc.) but none of them are bank-feed imports, so there's nothing else to add here yet -- see "Adding a provider" below and the setup UI's "Other" option.

### Config lives in a file, not env vars

Every provider's config (`BASIQ_USERNAME`, `MANAGER_MCP_BASIQ_ACCOUNT_LINKS`, `MANAGER_MCP_BANK_FEED_PROVIDER`, ...) is read through `bank_feed_providers.feeds_config.effective_environ()`, which overlays a saved JSON config file (path in `MANAGER_MCP_BANK_FEED_CONFIG_PATH`) on top of `os.environ`. Plain env vars still work (e.g. for local dev without the setup UI), but the file takes precedence, and it's what the setup UI writes to — so provider credentials never need to go into `secrets/manager-mcp.env`.

The default path, `/app/feeds.config`, is this app's own writable `WORKDIR` — fine for a standalone container with no shared secrets mount. In the compose cluster, `compose.yaml` overrides it to `/secrets/manager/feeds.config`, backed by `./secrets/manager`, a separate read-write bind mount (see `compose.yaml`) and `.gitignore`'d entirely, so the config survives container recreation and can be shared/inspected from the host.

Because it's read fresh on every attempt, saving a change (new credentials, a corrected account link, switching provider) takes effect on the next scheduled sync or the next `sync_bank_feeds` tool call — no restart needed.

### Setup UI

Under `MANAGER_MCP_TRANSPORT=http`, `/setup/bank-feeds` is a browser page — gated by the same Google OAuth client and `MANAGER_MCP_ALLOWED_EMAILS` allowlist used for the MCP endpoint itself — for configuring a provider without hand-editing anything. It reads what it can straight from Manager (bank & cash accounts, business name, custom fields already in use on existing receipts/payments) and only asks for what a provider says it can't get from Manager (e.g. the Aussie Bank Feeds login). For Basiq, entering that login lets the page also look up your linked Basiq accounts live. Submitting the form saves to the config file above.

The Google OAuth client needs `{MANAGER_MCP_OAUTH_BASE_URL}/setup/callback` added to its Authorized redirect URIs in Google Cloud Console, alongside the existing `/auth/callback` used by the MCP transport — a manual, one-time step.

### `sync_bank_feeds` tool

A banking-scope MCP tool that runs a sync immediately, the same as the timer would. If nothing is configured yet, it returns `{"configured": false, "setup_url": ..., "hint": ...}` instead of raising — an agent should read that and tell the user to open `setup_url`, rather than treating it as a failure to retry.

### Adding a provider

Implement `BankFeedProvider` (`bank_feed_providers/base.py`): `name`, `is_configured(environ)`, `async sync(client)`, and optionally `async setup_state(client, environ)` to plug into the setup UI. Add an instance to `PROVIDERS` in `registry.py` — nothing else needs to change.

## Development

```bash
uv sync --extra dev
uv run manager-mcp
```

Offline tests only (respx). No live Manager required:

```bash
uv run ruff check src tests
uv run pytest
```

GitHub Actions matrix: Python 3.10 and 3.12.

## Caveats

- One process ↔ one `MANAGER_API_URL`. Multi-instance routing is out of scope.
- Multi-business disambiguation on a shared host is **unverified**. Do not claim multi-business support until validated against a live multi-business setup.
- Vendored `src/manager_mcp/spec/api2.json` is provenance only; runtime always hits the live URL.
- ChatGPT Apps need a hosted HTTP MCP endpoint. This package is stdio-only.

## License

MIT. See [LICENSE](LICENSE).
