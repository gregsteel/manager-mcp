# Feature Specification: Bank Feed Automation

**Feature Branch**: `003-bank-feed-automation`

**Created**: 2026-09-22

**Status**: Implemented

**Input**: User description: "Replace Manager's old built-in 'Check for New Transactions' fallback (which only works on pre-Aussie-Bank-Feeds installs) with a pluggable bank-feed provider architecture, ship a Basiq (Aussie Bank Feeds) provider, run it on an hourly background loop plus an on-demand tool, and give an operator a browser UI to set it up without hand-editing env files. Out of scope: providers other than Basiq; this spec only defines the plugin surface and the one built-in implementation."

## Clarifications

### Session 2026-09-22

- Q: Why remove the old built-in "Check for New Transactions" fallback instead of keeping it as a safety net? → A: It only worked on pre-Aussie-Bank-Feeds Manager installs, and every deployment this code actually runs against already uses Aussie Bank Feeds — the fallback never did anything but fail on every attempt. It remains recoverable from version control history if a deployment genuinely needs it.
- Q: Why does bank-feed provider config live in its own JSON file (`feeds_config.py`) instead of `manager-mcp.env`? → A: A Basiq username/password (or any future provider's credentials) set through the setup UI shouldn't have to live in the process's env file, and a saved change should take effect without a restart. `effective_environ()` overlays the file on `os.environ` at call time, so every provider function reads current config on every call.
- Q: Why does `sync_bank_feeds` return `{"configured": false, "setup_url": ...}` instead of raising when nothing is configured? → A: An agent calling this tool before setup is complete needs an actionable next step (a URL to the setup UI), not a bare error it has to interpret and relay itself.
- Q: What determines the config file's location, and why is that itself an env var? → A: `MANAGER_MCP_BANK_FEED_CONFIG_PATH`, default `/app/feeds.config` (this app's own container WORKDIR, writable in a standalone container). The path isn't secret, only its contents are — a deployment that wants the file on a mount outside the container (e.g. a shared secrets volume) points that env var elsewhere.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Bank Transactions Sync Automatically (Priority: P1)

Once a bank-feed provider is configured, new bank transactions flow into Manager on a recurring schedule without the operator or agent doing anything further.

**Why this priority**: This is the core value: "new transactions just show up in Manager," replacing a broken built-in fallback with something that actually works against a real bank-feed aggregator.

**Independent Test**: Configure Basiq end-to-end against a sandbox account, wait for (or trigger) a sync interval, and confirm new transactions appear in the linked Manager bank account with no duplicate entries on a second sync.

**Acceptance Scenarios**:

1. **Given** a configured, valid provider and `MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS` set, **When** the interval elapses, **Then** the background loop calls the configured provider's `sync` and any new transactions appear in the linked Manager bank account(s).
2. **Given** a sync has already imported a transaction, **When** the same transaction is seen again on a later sync, **Then** it is not imported a second time (dedup via the configured custom field).
3. **Given** no provider is configured, **When** the sync interval elapses, **Then** the loop does not raise or crash the server — it reports the "not configured" state and continues running.
4. **Given** a provider is configured but a sync attempt fails transiently (e.g. Basiq API error), **When** the failure occurs, **Then** it is logged and the next scheduled sync still runs; one failure does not stop the loop.

---

### User Story 2 - On-Demand Sync (Priority: P1)

An agent or operator wants transactions synced right now rather than waiting for the next scheduled interval.

**Why this priority**: Equal priority to the background loop — an agent asked "did that payment come through yet?" needs a way to force a check rather than wait up to an hour.

**Independent Test**: With a provider configured, call the `sync_bank_feeds` tool and confirm it performs a sync and returns a result describing what happened; with no provider configured, call it and confirm it returns a setup pointer instead of an error.

**Acceptance Scenarios**:

1. **Given** a configured provider and banking write scope enabled, **When** the agent calls `sync_bank_feeds`, **Then** the tool performs an immediate sync and returns its outcome (imported count or equivalent).
2. **Given** no provider is configured, **When** the agent calls `sync_bank_feeds`, **Then** the tool returns `{"configured": false, "setup_url": ...}` rather than raising an error.
3. **Given** banking write scope is not enabled, **When** the agent calls `sync_bank_feeds`, **Then** the call is rejected with an error naming the required scope.

---

### User Story 3 - Configure a Provider Without Hand-Editing Env Files (Priority: P2)

An operator sets up Basiq (account links, dedup custom field, timezone) through a browser UI rather than by editing `manager-mcp.env` and restarting the container.

**Why this priority**: Lowers the setup barrier for a non-developer operator and avoids requiring a restart for every credential change.

**Independent Test**: With the HTTP transport running, open `/setup/bank-feeds`, fill in the fields the UI could not auto-detect, save, and confirm a sync now succeeds without restarting the server.

**Acceptance Scenarios**:

1. **Given** the HTTP transport is running with the same OAuth allowlist as the main server, **When** an allowlisted user opens `/setup/bank-feeds`, **Then** the page shows what it could auto-detect from Manager (accounts, business name, custom fields) and asks only for what the provider cannot supply itself.
2. **Given** the operator submits valid provider configuration, **When** the save completes, **Then** the configuration is persisted to the bank-feed config file and is used on the very next sync — with no server restart.
3. **Given** a password-type field (e.g. Basiq password) was previously saved, **When** the setup page is reloaded, **Then** the saved password value is never sent back to the browser (the field renders empty, not pre-filled).
4. **Given** the stdio transport (no HTTP), **When** setup is attempted, **Then** the browser setup UI is unavailable, and the operator is informed that HTTP transport is required for it.

---

### User Story 4 - Add a New Provider Without Touching the Sync Loop (Priority: P3)

A future maintainer wants to support a different bank-feed aggregator alongside or instead of Basiq.

**Why this priority**: Not needed for this fork's own use today, but the plugin architecture is the structural point of this spec — it should not require validation via a second real provider to be considered met, only via the interface contract.

**Independent Test**: Confirm the sync loop and setup UI reference only `manager_mcp.bank_feed_providers.registry`, never `basiq` directly, and that `registry.PROVIDERS` is the single list controlling which providers exist and their auto-selection order.

**Acceptance Scenarios**:

1. **Given** the `BankFeedProvider` interface in `base.py`, **When** a new module implementing it is added to `PROVIDERS` in `registry.py`, **Then** no change to `bank_feeds.py` or `server.py` is required for it to be selectable and syncable.
2. **Given** `MANAGER_MCP_BANK_FEED_PROVIDER` is unset and multiple providers are configured, **When** a provider is auto-selected, **Then** the first configured provider in `PROVIDERS` order wins, deterministically.
3. **Given** `MANAGER_MCP_BANK_FEED_PROVIDER` names a provider not in `PROVIDERS`, **When** selection is attempted, **Then** the system raises a `ConfigError` listing the known provider names.

---

### Edge Cases

- No provider configured at all: sync loop and `sync_bank_feeds` both report "not configured" with a setup pointer, never an unhandled exception.
- `MANAGER_MCP_BASIQ_ACCOUNT_LINKS` missing, not valid JSON, not a string-to-string object, or empty: fail configuration with a specific, actionable message (which field, what's wrong).
- `MANAGER_MCP_BASIQ_DEDUP_FIELD` missing: fail configuration — required so re-syncing never double-imports.
- `MANAGER_MCP_BASIQ_TIMEZONE` set to a non-IANA name: fail configuration rather than silently defaulting.
- Bank-feed config file unreadable or not valid JSON: log and treat as empty (first-run state), not a crash — a corrupt file must not take down the whole sync loop.
- Bank-feed config file contains non-string values: rejected as invalid, logged, treated as empty.
- `MANAGER_MCP_BANK_FEED_PROVIDER` names an unknown provider: `ConfigError` listing valid provider names.
- Old built-in "Check for New Transactions" fallback: intentionally removed, not reachable via any configuration in this system.
- Setup UI on stdio transport: unavailable; no route is registered.
- Password-kind setup fields: current value is never echoed to the browser, by construction (`SetupField.value` is documented as never set for `kind="password"`).

## Requirements *(mandatory)*

### Constitution Constraints *(mandatory for this project)*

Specs MUST respect `.specify/memory/constitution.md`:

- Tools/client methods are read-only unless an explicit env-flagged write path is in scope (off by default) — `sync_bank_feeds` requires banking write scope, consistent with [002](../002-manager-mcp-server/spec.md)'s intent-tool scoping model.
- New tools are hand-curated (no wholesale upstream API exposure).
- Secrets come from environment variables (or the equivalent config file overlay) only; the setup UI never round-trips a saved password to the browser.
- Acceptance tests for network I/O MUST be offline-mockable (respx); no live external service as a gate.
- Docs in scope explain intent/tradeoffs (no filler).

**Feature-specific hardening**: Bank-feed sync is opt-in (`MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS` unset disables the loop; `sync_bank_feeds` still requires banking scope). Deduplication is mandatory, not optional, for any provider (`DEDUP_FIELD_ENV` is a required config value for Basiq).

### Functional Requirements

- **FR-001**: The system MUST define a `BankFeedProvider` interface (`base.py`) that any bank-feed aggregator integration implements, so the sync loop and setup UI depend only on that interface, never on a specific provider.
- **FR-002**: The system MUST ship a `basiq` provider implementing that interface for Aussie Bank Feeds, configured via `MANAGER_MCP_BASIQ_ACCOUNT_LINKS`, `MANAGER_MCP_BASIQ_DEDUP_FIELD`, and optionally `MANAGER_MCP_BASIQ_TIMEZONE` (default `Australia/Sydney`).
- **FR-003**: `MANAGER_MCP_BANK_FEED_PROVIDER` MUST select a specific provider by name when set; when unset, the system MUST select the first provider in `registry.PROVIDERS` order whose `is_configured()` returns true, and MUST raise a `ConfigError` if none are configured.
- **FR-004**: The system MUST remove the previous built-in "Check for New Transactions" fallback; no configuration in this system MUST re-enable it.
- **FR-005**: The system MUST run an hourly (configurable via `MANAGER_MCP_BANK_FEED_SYNC_INTERVAL_SECONDS`, `3600` in the shipped compose config) background sync loop that calls the selected provider's `sync`.
- **FR-006**: The system MUST provide an on-demand `sync_bank_feeds` MCP tool that performs an immediate sync, requires banking write scope, and returns `{"configured": false, "setup_url": ...}` instead of raising when no provider is configured.
- **FR-007**: Bank-feed provider configuration (including credentials set via the setup UI) MUST be stored in a dedicated config file (path from `MANAGER_MCP_BANK_FEED_CONFIG_PATH`, default `/app/feeds.config`) that is read fresh on every provider call (`feeds_config.effective_environ()`), so a saved change takes effect without a server restart.
- **FR-008**: A malformed or unreadable bank-feed config file MUST be logged and treated as empty configuration, and MUST NOT crash the sync loop or the server.
- **FR-009**: The system MUST provide a browser setup UI at `/setup/bank-feeds`, available only under the HTTP transport and protected by the same OAuth allowlist as the main server, that pre-fills what it can auto-detect from Manager (accounts, business name, custom fields) and asks only for values a provider cannot supply.
- **FR-010**: The setup UI MUST NOT ever send a previously-saved password-kind field's value back to the browser.
- **FR-011**: The Basiq provider MUST require `MANAGER_MCP_BASIQ_ACCOUNT_LINKS` (non-empty JSON object of string to string) and `MANAGER_MCP_BASIQ_DEDUP_FIELD` (a Manager custom field key), and MUST fail configuration validation with a specific message if either is missing or malformed.
- **FR-012**: Every sync (scheduled or on-demand) MUST use the configured dedup field to avoid re-importing a transaction already present in Manager.

### Key Entities

- **BankFeedProvider**: The plugin interface (name, `is_configured()`, `sync(client)`, setup-field descriptors) that decouples the sync loop and setup UI from any specific aggregator.
- **Provider Registry**: `registry.PROVIDERS`, the ordered list of available providers and the single source of truth for auto-selection order and lookup by name.
- **Bank-Feed Config File**: A JSON file (path via `MANAGER_MCP_BANK_FEED_CONFIG_PATH`) holding provider credentials/settings, overlaid on `os.environ` at read time so changes apply without a restart.
- **Account Link**: An entry in `MANAGER_MCP_BASIQ_ACCOUNT_LINKS` mapping a Basiq bank account to a Manager bank account.
- **Dedup Field**: A Manager custom field (identified by `MANAGER_MCP_BASIQ_DEDUP_FIELD`) used to detect and skip transactions already imported.
- **SetupField**: A single piece of provider configuration the setup UI must collect because it can't be auto-detected, with metadata (label, kind, help text, whether it's required).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With a valid Basiq configuration, transactions present in the linked Basiq account but absent from Manager appear in Manager within one sync interval, with zero duplicate imports across repeated syncs of the same underlying transactions.
- **SC-002**: Calling `sync_bank_feeds` with no provider configured returns a setup pointer in 100% of attempts — never an unhandled exception.
- **SC-003**: A saved bank-feed configuration change (via the setup UI or by editing the config file) takes effect on the next sync with zero server restarts.
- **SC-004**: A corrupt or unreadable bank-feed config file results in "not configured" behavior (logged, non-fatal) in 100% of cases, never a crashed sync loop.
- **SC-005**: The setup UI never transmits a previously-saved password value to the browser, verified by inspecting the rendered page's password-kind inputs.
- **SC-006**: A reviewer can add a second provider by adding one file and one `registry.PROVIDERS` entry, with zero changes required to `bank_feeds.py` or `server.py`.

## Assumptions

- Every deployment this code runs against already uses Manager's Aussie Bank Feeds extension; the removed "Check for New Transactions" fallback is not needed by any current deployment.
- The bank-feed config file's location is not itself secret and may reasonably default to the app's own container WORKDIR; only its contents require protection.
- Basiq sandbox/production credentials and account linkage are established by the operator outside this system (a Basiq account must already exist).
- The setup UI's auto-detection (accounts, business name, custom fields) relies on the same `/api2` credentials already configured for the main server; it does not introduce a separate Manager credential.
- One bank-feed provider is active at a time per server instance; running multiple providers concurrently is not a target of this spec.

## Out of Scope (v1)

- Bank-feed providers other than Basiq (the plugin interface is defined and validated structurally; a second real implementation is not required for this spec to be met).
- Multi-provider concurrent sync within a single server instance.
- Historical backfill beyond whatever range the provider's own API returns by default.
- Editing or reviewing imported transactions after sync (that remains ordinary Manager bank-reconciliation workflow).
- Non-HTTP-transport access to the setup UI.
