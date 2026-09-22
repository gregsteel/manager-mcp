# Feature Specification: Manager MCP Server — Remote Access, Task Tools, Attachments

**Feature Branch**: `002-manager-mcp-server`

**Created**: 2026-09-22

**Status**: Implemented

**Input**: User description: "Turn the read-only Manager.io MCP server (001) into a fork that a remote agent (e.g. Claude Cowork) can connect to over the network and use to actually do bookkeeping work: authenticated HTTP transport, intent-shaped write tools instead of raw per-resource CRUD, file attachments on purchase invoices, and a curated set of additional read surfaces (line-item search, payment/receipt rules). Out of scope: bank-feed ingestion (see 003)."

## Clarifications

### Session 2026-09-22

- Q: Why not just expose the raw `create_*`/`update_*`/`delete_*` CRUD tools generated per-resource in 001's write-scope escape hatch? → A: An agent given "create a sales invoice" as a bare CRUD call has to independently get validation, persistence, and confirmation right every time; an intent tool (`issue_sales_invoice`) composes validate + persist + verify into one call and is the tool an agent should reach for by default. Raw CRUD stays available (`raw` in `MANAGER_MCP_WRITE_SCOPES`/`MANAGER_MCP_DELETE_SCOPES`) as an advanced escape hatch, deprecated for removal in 0.3.0.
- Q: Why does file-attachment support need a separate UI-session auth path (`MANAGER_UI_USERNAME`/`MANAGER_UI_PASSWORD`) instead of the existing `/api2` key? → A: The Attachments list and legacy Image-field endpoints are undocumented, non-`/api2` Manager routes discovered by testing against a live instance; whether `X-API-KEY` authenticates them at all is unconfirmed for every deployment shape, so the client also supports HTTP Basic Auth as a real logged-in Manager user for the endpoints that need it.
- Q: Why gate the HTTP transport behind an email allowlist rather than accepting any authenticated Google account? → A: `GoogleProvider` (fastmcp) implements MCP OAuth by delegating login to Google but authenticates *any* Google account that completes login. Manager MCP is single-tenant — one business's books — so `AllowlistedGoogleProvider` layers `MANAGER_MCP_ALLOWED_EMAILS` on top and rejects any token whose email isn't listed, even if Google login succeeded.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Remote Agent Connects Over HTTP (Priority: P1)

An operator runs manager-mcp as a long-lived service (e.g. in Docker, behind a TLS-terminating reverse proxy) instead of launching it per-session over stdio, so a remote connector such as Claude Cowork can reach it over the network.

**Why this priority**: Without network transport, every other capability in this spec is unreachable from a remote agent — stdio only works for a locally-spawned client process.

**Independent Test**: Set `MANAGER_MCP_TRANSPORT=http` with OAuth client credentials and an allowlist; start the server; confirm a request with a non-allowlisted Google account is rejected and one with an allowlisted account succeeds.

**Acceptance Scenarios**:

1. **Given** `MANAGER_MCP_TRANSPORT` unset or `stdio`, **When** the server starts, **Then** it behaves exactly as in 001 (no transport-level auth of its own; the host process controls who can launch it).
2. **Given** `MANAGER_MCP_TRANSPORT=http` with valid Google OAuth client credentials and a non-empty `MANAGER_MCP_ALLOWED_EMAILS`, **When** a client completes Google login with an allowlisted account, **Then** the request is authenticated and served.
3. **Given** the same configuration, **When** a client completes Google login with an account not on the allowlist, **Then** the request is rejected even though Google login itself succeeded.
4. **Given** `MANAGER_MCP_TRANSPORT=http` with missing or invalid OAuth configuration, **When** the server starts, **Then** it fails fast with an actionable configuration error rather than serving unauthenticated.

---

### User Story 2 - Compose a Bookkeeping Action in One Call (Priority: P1)

An agent needs to issue a sales invoice, record a supplier payment, transfer funds between accounts, or perform one of the other named bookkeeping actions, without hand-rolling validation and persistence via raw CRUD calls.

**Why this priority**: This is the core value of the fork over 001 — moving from "read the books" to "safely do bookkeeping work" through a curated, intent-shaped surface.

**Independent Test**: Call an intent tool (e.g. `issue_sales_invoice`) with valid input against a configured instance with the relevant write scope enabled; confirm the record is created, persisted, and the tool's response reflects the verified end state (not just the request that was sent).

**Acceptance Scenarios**:

1. **Given** a write scope is enabled for a given intent tool, **When** the agent calls that tool with valid input, **Then** the system validates the input, persists the record in Manager, verifies the persisted result, and returns that verified result in one call.
2. **Given** a write scope is *not* enabled for a given intent tool's domain, **When** the agent attempts to call it, **Then** the call is rejected with an error naming the required scope.
3. **Given** `raw` is not included in `MANAGER_MCP_WRITE_SCOPES`/`MANAGER_MCP_DELETE_SCOPES`, **When** tools are listed, **Then** no per-resource `create_*`/`update_*`/`delete_*` CRUD tool is registered — only intent tools and (if scoped) `create_customer`/`create_supplier` per 001's deprecation carve-out.
4. **Given** the full set of intent tools (`issue_sales_invoice`, `issue_purchase_invoice`, `issue_quote`, `issue_deposit_invoice`, `convert_quote_to_invoice`, `record_customer_payment`, `record_supplier_payment`, `record_customer_deposit`, `transfer_between_accounts`, `record_expense`, `post_journal_entry`, `apply_deposit_to_invoice`, `void_document`, `attach_receipt_to_purchase_invoice`), **When** each is exercised against its documented preconditions, **Then** each completes its single named bookkeeping action end-to-end.

---

### User Story 3 - Attach a Receipt or Supporting File to a Purchase Invoice (Priority: P2)

An agent has a receipt (uploaded by the user, or fetched from a URL) that should be attached to an existing purchase invoice as supporting documentation.

**Why this priority**: Closes the loop between expense capture and bookkeeping records; without it, an agent can create the invoice but not attach its evidence.

**Independent Test**: With `MANAGER_UI_USERNAME`/`MANAGER_UI_PASSWORD` configured, call `attach_receipt_to_purchase_invoice` with a file and a purchase invoice key; confirm the file appears on that invoice in Manager afterward.

**Acceptance Scenarios**:

1. **Given** valid UI-session credentials and a reachable purchase invoice, **When** the agent attaches a file, **Then** the file is written to Manager's general Attachments list for that invoice.
2. **Given** UI-session credentials are not configured, **When** an attachment is attempted, **Then** the system fails with a clear configuration error rather than silently skipping the attachment.
3. **Given** an unexpected response from Manager's undocumented attachment endpoint, **When** the failure occurs, **Then** the error reports what was actually observed (status, key headers, a body snippet) rather than asserting an unverified theory of the cause.
4. **Given** an attachment call fails, **When** the agent inspects the result, **Then** there is no silent fallback — the failure is surfaced, not swallowed.

---

### User Story 4 - Search Line-Item Text and List Categorization Rules (Priority: P3)

An agent needs to find a transaction by wording that only appears in a line-item description (not the document header), or needs to see what payment/receipt auto-categorization rules already exist before proposing a new one.

**Why this priority**: Rounds out the read surface for bookkeeping-adjacent questions that 001's header-only search and curated collections don't cover.

**Independent Test**: Call `search_line_items` with a term that only appears in a line-item description and confirm matching documents are returned; call `list_records`/`get_record` for `payment_rules` and `receipt_rules` and confirm existing rules are returned read-only.

**Acceptance Scenarios**:

1. **Given** a document whose line-item description (not header fields) contains a search term, **When** the agent calls `search_line_items` with that term, **Then** that document is returned, which `list_records`' header-only `term` search would miss.
2. **Given** configured payment or receipt rules in Manager, **When** the agent lists or fetches the `payment_rules`/`receipt_rules` collections, **Then** the existing rules are returned.
3. **Given** the Manager API exposes no create/update/delete endpoint for either collection, **When** any scope configuration is checked, **Then** `payment_rules`/`receipt_rules` remain list/search-only regardless of write-scope settings.

---

### Edge Cases

- HTTP transport requested without OAuth client credentials or base URL: fail at startup with a clear configuration error.
- OAuth login succeeds but the account is not in `MANAGER_MCP_ALLOWED_EMAILS`: reject the request; do not leak which emails *are* allowlisted.
- Intent tool called with a scope that isn't enabled: reject with the specific scope name required, not a generic permission error.
- Intent tool preconditions not met (e.g. no deposit bank account configured for `record_customer_deposit`): return a structured `PreconditionResult` guiding setup, not a raw API error.
- Attachment endpoint returns an unexpected status: log status, key headers, and a body snippet; raise `ApiAttachmentError` without asserting an unverified cause.
- UI-session credentials configured but rejected by Manager: clear authentication error; never log the password.
- `search_line_items` term matches nothing: return an empty result, not an error.
- Legacy `raw` CRUD tools requested via `MANAGER_MCP_WRITE_SCOPES=raw` after 0.3.0 removal: rejected as an unknown scope (forward-looking; tracked as the 001 deprecation carve-out).

## Requirements *(mandatory)*

### Constitution Constraints *(mandatory for this project)*

Specs MUST respect `.specify/memory/constitution.md`:

- Tools/client methods are read-only unless an explicit env-flagged write path is in scope (off by default) — carried forward from 001; this spec's write tools are exactly that explicit, scoped path.
- New tools are hand-curated (no wholesale upstream API exposure).
- Secrets come from environment variables only (`MANAGER_UI_USERNAME`/`PASSWORD`, OAuth client credentials).
- Acceptance tests for network I/O MUST be offline-mockable (respx); no live external service as a gate.
- Docs in scope explain intent/tradeoffs (no filler).

**Feature-specific hardening**: HTTP transport is opt-in (`MANAGER_MCP_TRANSPORT=http`) and always authenticated when enabled — no unauthenticated network listener. Intent tools are the recommended write path; raw per-resource CRUD is deprecated and gated behind an explicit `raw` scope token, targeted for removal in 0.3.0.

### Functional Requirements

- **FR-001**: The system MUST support a Streamable HTTP transport, selected by `MANAGER_MCP_TRANSPORT=http`, in addition to the default stdio transport.
- **FR-002**: When HTTP transport is selected, the system MUST require Google OAuth client credentials (`MANAGER_MCP_OAUTH_GOOGLE_CLIENT_ID`/`_SECRET`) and a non-empty `MANAGER_MCP_ALLOWED_EMAILS` allowlist, and MUST reject any authenticated request whose account email is not on that allowlist.
- **FR-003**: The system MUST provide intent-shaped task tools — `issue_sales_invoice`, `issue_purchase_invoice`, `issue_quote`, `issue_deposit_invoice`, `convert_quote_to_invoice`, `record_customer_payment`, `record_supplier_payment`, `record_customer_deposit`, `transfer_between_accounts`, `record_expense`, `post_journal_entry`, `apply_deposit_to_invoice`, `void_document`, `attach_receipt_to_purchase_invoice` — each composing validate + persist + verify into a single call.
- **FR-004**: Each intent tool MUST be registered only when its required write (or delete) scope is present in `MANAGER_MCP_WRITE_SCOPES`/`MANAGER_MCP_DELETE_SCOPES`.
- **FR-005**: Per-resource `create_*`/`update_*`/`delete_*` CRUD tools MUST be deprecated: they register only when `raw` is included in the relevant scope variable, and MUST be removed entirely in 0.3.0 (carried forward from 001's deprecation notice).
- **FR-006**: The system MUST support attaching a file to a purchase invoice via Manager's undocumented Attachments-list and legacy Image-field endpoints, authenticated by a real logged-in Manager UI session (`MANAGER_UI_USERNAME`/`MANAGER_UI_PASSWORD`, HTTP Basic Auth), separate from the `/api2` key used for all other calls.
- **FR-007**: Attachment failures MUST report what was actually observed from Manager (status code, key headers, a body snippet) and MUST NOT assert an unverified theory of the cause in the error message.
- **FR-008**: The system MUST provide a `search_line_items` tool that matches search terms against line-item description text, which `list_records`' header-only `term` search does not cover.
- **FR-009**: The system MUST expose `payment_rules` and `receipt_rules` as list/search-only collections via `list_records`/`get_record`; neither MUST register create/update/delete tools, because the Manager API has no such endpoint for either.
- **FR-010**: Read-only tools (`list_resources`, `list_records`, `get_record`, `search_line_items`, and report tools including `bank_balances`) MUST declare the `readOnlyHint: true` MCP annotation, so MCP clients that gate unannotated tools behind per-call approval (e.g. Claude Cowork) treat them as pre-confirmed read-only.
- **FR-011**: The system MUST ship Docker packaging (`Dockerfile`, `.dockerignore`, `.env.example`) sufficient to run manager-mcp as a standalone container, in addition to the existing stdio/local-host path.

### Key Entities

- **HTTP Transport Session**: An authenticated remote connection to the MCP server over Streamable HTTP, gated by Google OAuth plus an email allowlist.
- **Intent Task Tool**: A single-call bookkeeping action (e.g. "issue a sales invoice") that composes validation, persistence, and verification, as opposed to a raw per-resource CRUD call.
- **UI-Session Credential**: `MANAGER_UI_USERNAME`/`MANAGER_UI_PASSWORD`, used only for the handful of non-`/api2` endpoints (attachments) that require a logged-in Manager user rather than the API key.
- **Attachment**: A file associated with a purchase invoice via either Manager's general Attachments list or its legacy single-file Image field.
- **Categorization Rule**: A `payment_rules` or `receipt_rules` record describing how a bank transaction is auto-categorized; read-only in this system regardless of scope configuration.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With `MANAGER_MCP_TRANSPORT=http` configured, a request from a non-allowlisted Google account is rejected in 100% of attempts, and one from an allowlisted account is served.
- **SC-002**: For each of the 13 intent tools, a single call against a correctly-scoped, correctly-configured instance produces a verified, persisted result — no intent tool requires a follow-up call to confirm its own effect succeeded.
- **SC-003**: With `raw` absent from both scope variables, 100% of registered tools are either read-only or named intent tools — zero raw `create_*`/`update_*`/`delete_*` tools are exposed.
- **SC-004**: A file attached via `attach_receipt_to_purchase_invoice` is visible on the target purchase invoice in Manager afterward, in a live-instance check.
- **SC-005**: `search_line_items` returns documents whose match exists only in line-item description text, which a `list_records` header-only `term` search on the same input would return zero results for.
- **SC-006**: `payment_rules`/`receipt_rules` remain list/search-only under every scope configuration — no configuration causes a create/update/delete tool to be registered for either.
- **SC-007**: All previously-unannotated read-only tools now report `readOnlyHint: true`, eliminating the per-call approval prompt this fork previously observed in Claude Cowork for tools like `bank_balances`.

## Assumptions

- The remote HTTP transport sits behind a TLS-terminating reverse proxy; this spec does not itself provide TLS termination.
- Manager MCP remains single-tenant (one business's books per deployment); the email allowlist is sized accordingly and is not a multi-tenant access-control system.
- The Attachments-list and legacy Image-field endpoints were verified only against Manager 26.8.27.0 on a specific local business; behavior on other versions/deployments should be re-verified after any Manager upgrade.
- Whether `X-API-KEY` alone can authenticate the attachment endpoints is unconfirmed for every deployment shape; the UI-session credential path is assumed necessary until proven otherwise.
- Raw CRUD tools remain available behind the `raw` scope token only as a transitional escape hatch; downstream automation should not depend on them past 0.3.0.

## Out of Scope (v1 of this spec)

- Bank-feed ingestion and the pluggable provider architecture (see [003-bank-feed-automation](../003-bank-feed-automation/spec.md)).
- Non-Google OAuth identity providers for the HTTP transport.
- Multi-tenant access control (multiple businesses/allowlists behind one server instance).
- Attachments to document types other than purchase invoices.
- Any change to 001's fundamental read-only-by-default posture — write tools remain opt-in per scope.
