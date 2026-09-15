"""Browser setup page for configuring a bank-feed provider, gated by the
same Google login as the MCP transport itself.

Only meaningful under `MANAGER_MCP_TRANSPORT=http` -- stdio has no HTTP
server to serve this from. Reuses `MANAGER_MCP_OAUTH_GOOGLE_CLIENT_ID`/
`_SECRET`, `MANAGER_MCP_OAUTH_BASE_URL` and `MANAGER_MCP_ALLOWED_EMAILS`
(see `http_auth.py`) rather than a separate login, so the same Google
Cloud OAuth client and email allowlist gate both the MCP endpoint and this
page. That OAuth client needs `{MANAGER_MCP_OAUTH_BASE_URL}/setup/callback`
added to its Authorized redirect URIs in Google Cloud Console -- a manual,
one-time step, same as registering the MCP callback was.

This page never writes to Manager -- it only reads from it (bank accounts,
business name, custom fields already in use). What an operator submits is
saved to the bank-feed config file (`feeds_config.py`, default
`/secrets/manager/feeds.config`), not to `secrets/manager-mcp.env` and not
committed anywhere -- and every provider reads that file live, so a saved
change (a new Basiq login, a corrected account link) takes effect on the
next scheduled sync or the next `sync_bank_feeds` tool call, no restart
needed. Credentials typed into the "detect" step (e.g. the Aussie Bank
Feeds login used to list Basiq accounts) are used for that one live lookup
and are not saved unless the form is actually submitted.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from manager_mcp.bank_feed_providers import feeds_config
from manager_mcp.bank_feed_providers.registry import PROVIDER_ENV, PROVIDERS
from manager_mcp.client import ManagerClient
from manager_mcp.http_auth import (
    ALLOWED_EMAILS_ENV,
    BASE_URL_ENV,
    CLIENT_ID_ENV,
    CLIENT_SECRET_ENV,
    _split_csv,
)

_log = logging.getLogger(__name__)

LOGIN_PATH = "/setup/login"
CALLBACK_PATH = "/setup/callback"
PAGE_PATH = "/setup/bank-feeds"
DETECT_BASIQ_PATH = "/setup/bank-feeds/detect-basiq"

_SESSION_COOKIE = "manager_mcp_setup_session"
_STATE_COOKIE = "manager_mcp_setup_state"
_SESSION_TTL_SECONDS = 3600
_STATE_TTL_SECONDS = 600

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def setup_url(environ: dict[str, str] | None = None) -> str | None:
    """`{base}/setup/bank-feeds`, or None if the http transport isn't
    configured (no base URL to build it from)."""
    env = os.environ if environ is None else environ
    base = (env.get(BASE_URL_ENV) or "").strip().rstrip("/")
    return f"{base}{PAGE_PATH}" if base else None


def _oauth_config() -> tuple[str, str, str, frozenset[str]] | None:
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    client_secret = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    base_url = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    allowed = _split_csv(os.environ.get(ALLOWED_EMAILS_ENV, ""))
    if not client_id or not base_url or not allowed:
        return None
    return client_id, client_secret, base_url, allowed


def _sign(payload: str, secret: str) -> str:
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def _verify(token: str, secret: str) -> str | None:
    payload, _, mac = token.rpartition(".")
    if not payload or not mac:
        return None
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    return payload


def _cookie_secret() -> str:
    # Reuses the OAuth client secret as the HMAC key: it's already the
    # deployment's own secret, unique per install, and never exposed to the
    # browser -- no need for a second signing secret.
    return os.environ.get(CLIENT_SECRET_ENV, "") or os.environ.get(CLIENT_ID_ENV, "")


def _require_session(request: Request) -> str | None:
    token = request.cookies.get(_SESSION_COOKIE, "")
    if not token:
        return None
    payload = _verify(token, _cookie_secret())
    if not payload:
        return None
    email, _, expiry = payload.partition("|")
    try:
        if float(expiry) < time.time():
            return None
    except ValueError:
        return None
    return email or None


def _redirect_to_login(next_path: str) -> RedirectResponse:
    return RedirectResponse(f"{LOGIN_PATH}?next={quote(next_path)}")


async def login(request: Request) -> Response:
    cfg = _oauth_config()
    if cfg is None:
        return HTMLResponse(
            "Bank-feed setup UI needs MANAGER_MCP_TRANSPORT=http plus the same "
            f"{CLIENT_ID_ENV}, {BASE_URL_ENV} and {ALLOWED_EMAILS_ENV} used for MCP OAuth.",
            status_code=503,
        )
    client_id, _client_secret, base_url, _allowed = cfg
    next_path = request.query_params.get("next", PAGE_PATH)
    nonce = secrets.token_urlsafe(16)
    state = _sign(f"{nonce}|{next_path}|{int(time.time())}", _cookie_secret())

    params = {
        "client_id": client_id,
        "redirect_uri": f"{base_url}{CALLBACK_PATH}",
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    response = RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")
    response.set_cookie(
        _STATE_COOKIE,
        nonce,
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        secure=base_url.startswith("https"),
        samesite="lax",
    )
    return response


async def callback(request: Request) -> Response:
    cfg = _oauth_config()
    if cfg is None:
        return HTMLResponse("Bank-feed setup UI is not configured.", status_code=503)
    client_id, client_secret, base_url, allowed = cfg

    error = request.query_params.get("error")
    if error:
        return HTMLResponse(f"Google sign-in failed: {html.escape(error)}", status_code=400)

    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    payload = _verify(state, _cookie_secret())
    cookie_nonce = request.cookies.get(_STATE_COOKIE, "")
    if not payload or not code:
        return HTMLResponse("Invalid or missing OAuth state/code.", status_code=400)
    nonce, _, rest = payload.partition("|")
    next_path, _, issued_at = rest.partition("|")
    if not nonce or nonce != cookie_nonce:
        return HTMLResponse(
            "OAuth state did not match -- please try signing in again.", status_code=400
        )
    try:
        if time.time() - float(issued_at) > _STATE_TTL_SECONDS:
            return HTMLResponse("Sign-in took too long -- please try again.", status_code=400)
    except ValueError:
        return HTMLResponse("Invalid OAuth state.", status_code=400)

    async with httpx.AsyncClient(timeout=15) as http_client:
        token_resp = await http_client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": f"{base_url}{CALLBACK_PATH}",
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code >= 400:
            _log.warning(
                "bank-feed setup: Google token exchange failed: %s", token_resp.text[:300]
            )
            return HTMLResponse("Google sign-in failed (token exchange).", status_code=400)
        access_token = token_resp.json().get("access_token", "")

        userinfo_resp = await http_client.get(
            _GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if userinfo_resp.status_code >= 400:
            return HTMLResponse("Google sign-in failed (userinfo).", status_code=400)
        email = str(userinfo_resp.json().get("email", "")).lower()

    if not email or email not in allowed:
        return HTMLResponse(
            "This Google account is not allowed to configure manager-mcp.", status_code=403
        )

    session_token = _sign(f"{email}|{time.time() + _SESSION_TTL_SECONDS}", _cookie_secret())
    response = RedirectResponse(next_path or PAGE_PATH)
    response.delete_cookie(_STATE_COOKIE)
    response.set_cookie(
        _SESSION_COOKIE,
        session_token,
        max_age=_SESSION_TTL_SECONDS,
        httponly=True,
        secure=base_url.startswith("https"),
        samesite="lax",
    )
    return response


_PAGE_CSS = """
  :root {
    --ink: #1a1d23; --muted: #62697b; --line: #e2e5eb; --card: #ffffff;
    --bg: #f6f7f9; --accent: #4f5fe0; --accent-ink: #ffffff;
    --ok-bg: #ecfaf1; --ok-line: #a9e4bd; --ok-ink: #146c3a;
    --info-bg: #eef1ff; --info-line: #c4cbfb; --info-ink: #3341b8;
    --warn-bg: #fff8e8; --warn-line: #f3d98a; --warn-ink: #8a6416;
    --err-bg: #fdeeee; --err-line: #f3b9b9; --err-ink: #a5251f;
    --radius: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ink: #eef0f4; --muted: #9aa1b2; --line: #2c303a; --card: #1b1e24;
      --bg: #131519; --accent: #7c88f5; --accent-ink: #10121a;
      --ok-bg: #10281a; --ok-line: #235a37; --ok-ink: #7fe0a4;
      --info-bg: #1a1e3c; --info-line: #33397f; --info-ink: #aeb6ff;
      --warn-bg: #302a10; --warn-line: #6b551c; --warn-ink: #ecc25e;
      --err-bg: #331414; --err-line: #6b2727; --err-ink: #f2a5a5;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    max-width: 720px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem;
    background: var(--bg); color: var(--ink); line-height: 1.5;
  }
  a { color: var(--accent); }
  h1 { font-size: 1.5rem; margin: 0 0 0.25rem; }
  h2 { font-size: 1.1rem; margin: 0; display: flex; align-items: center; gap: 0.5rem; }
  .lede { color: var(--muted); margin: 0 0 1.75rem; }
  .topbar {
    display: flex; justify-content: space-between; align-items: baseline;
    flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.25rem;
  }
  .topbar .who { color: var(--muted); font-size: 0.85rem; }
  .picker-card {
    background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 1.1rem 1.25rem; margin-bottom: 1.25rem;
  }
  .picker-card label { display: block; font-weight: 600; margin-bottom: 0.4rem; }
  .picker-card select {
    width: 100%; font-size: 1rem; padding: 0.55rem 0.7rem; border-radius: 8px;
    border: 1px solid var(--line); background: var(--bg); color: var(--ink);
  }
  section.provider {
    background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 1.35rem 1.5rem; margin-bottom: 1.25rem;
  }
  section.provider[hidden] { display: none; }
  .pill {
    font-size: 0.72rem; font-weight: 600; padding: 0.2rem 0.55rem; border-radius: 999px;
    letter-spacing: 0.01em;
  }
  .pill-active { background: var(--ok-bg); color: var(--ok-ink);
                 border: 1px solid var(--ok-line); }
  .pill-configured { background: var(--info-bg); color: var(--info-ink);
                      border: 1px solid var(--info-line); }
  .pill-unconfigured { background: var(--warn-bg); color: var(--warn-ink);
                        border: 1px solid var(--warn-line); }
  .banner {
    border-radius: 8px; padding: 0.85rem 1rem; margin: 0.9rem 0; font-size: 0.9rem;
  }
  .banner-active { background: var(--ok-bg); border: 1px solid var(--ok-line);
                    color: var(--ok-ink); }
  .banner-unconfigured { background: var(--warn-bg); border: 1px solid var(--warn-line);
                          color: var(--warn-ink); }
  .banner-error { background: var(--err-bg); border: 1px solid var(--err-line);
                   color: var(--err-ink); }
  .banner ul { margin: 0.4rem 0 0; padding-left: 1.1rem; }
  .chips { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.75rem 0; }
  .chip {
    font-size: 0.78rem; background: var(--bg); border: 1px solid var(--line);
    border-radius: 999px; padding: 0.2rem 0.65rem; color: var(--muted);
  }
  .note { color: var(--muted); font-size: 0.88rem; margin: 0.5rem 0; }
  details { margin-top: 0.9rem; }
  details > summary {
    cursor: pointer; font-weight: 600; padding: 0.5rem 0; list-style: none;
  }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before { content: "\\25B8  "; }
  details[open] > summary::before { content: "\\25BE  "; }
  .field { margin: 0.9rem 0; }
  .field label { display: block; font-weight: 600; margin-bottom: 0.3rem; font-size: 0.92rem; }
  .field input[type=text], .field input[type=password], .field select {
    width: 100%; padding: 0.5rem 0.6rem; border-radius: 7px; border: 1px solid var(--line);
    background: var(--bg); color: var(--ink); font-size: 0.95rem;
  }
  .callout {
    background: var(--warn-bg); border: 1px solid var(--warn-line); color: var(--warn-ink);
    border-radius: 8px; padding: 0.7rem 0.85rem; font-size: 0.85rem; margin-bottom: 0.5rem;
  }
  .help { color: var(--muted); font-size: 0.82rem; margin: 0.35rem 0 0; }
  table.account-links { width: 100%; border-collapse: collapse; margin: 0.5rem 0; }
  table.account-links th {
    text-align: left; font-size: 0.7rem; letter-spacing: 0.04em; text-transform: uppercase;
    color: var(--muted); font-weight: 600; padding: 0.3rem 0.5rem 0.5rem;
    border-bottom: 1px solid var(--line);
  }
  table.account-links td {
    padding: 0.55rem 0.5rem; border-bottom: 1px solid var(--line); vertical-align: middle;
  }
  table.account-links tr:last-child td { border-bottom: none; }
  table.account-links td:first-child { font-size: 0.9rem; }
  table.account-links select {
    width: 100%; padding: 0.45rem 0.6rem; border-radius: 7px; border: 1px solid var(--line);
    background: var(--bg); color: var(--ink); font-size: 0.9rem;
  }
  pre {
    background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
    padding: 1rem; overflow-x: auto; white-space: pre-wrap; font-size: 0.85rem;
  }
  section.provider ol { padding-left: 1.2rem; font-size: 0.9rem; }
  section.provider ol li { margin: 0.4rem 0; }
  section.provider code {
    background: var(--bg); border: 1px solid var(--line); border-radius: 4px;
    padding: 0.05rem 0.3rem; font-size: 0.85em;
  }
  button, .btn {
    font-size: 0.92rem; padding: 0.55rem 1.1rem; border-radius: 8px; border: 1px solid var(--line);
    background: var(--card); color: var(--ink); cursor: pointer; margin-top: 0.6rem;
    margin-right: 0.5rem;
  }
  button[type=submit] { background: var(--accent); color: var(--accent-ink);
                         border-color: var(--accent); }
  button:hover { filter: brightness(1.05); }
"""

# `__DETECT_BASIQ_PATH__` is substituted in at render time -- kept as a plain
# (non-f) string so the JS's own `{}` don't need doubling.
_PAGE_JS = """
  function selectProvider(name) {
    document.querySelectorAll('.provider').forEach(section => {
      section.hidden = section.dataset.provider !== name;
    });
  }
  const picker = document.getElementById('provider-picker');
  if (picker) {
    picker.addEventListener('change', () => selectProvider(picker.value));
  }

  async function detectBasiq(providerName) {
    const user = document.getElementById(providerName + '__BASIQ_USERNAME').value;
    const pass = document.getElementById(providerName + '__BASIQ_PASSWORD').value;
    if (!user || !pass) {
      alert('Enter the Aussie Bank Feeds username and password first.');
      return;
    }
    const resp = await fetch('__DETECT_BASIQ_PATH__', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: user, password: pass})
    });
    const data = await resp.json();
    if (!resp.ok) {
      alert('Detection failed: ' + (data.error || resp.status));
      return;
    }
    const rowSelector = '#' + providerName
      + '__MANAGER_MCP_BASIQ_ACCOUNT_LINKS_rows select.basiq-source';
    document.querySelectorAll(rowSelector).forEach(select => {
      const current = select.value;
      select.innerHTML = '<option value="">\\u2014 not linked \\u2014</option>';
      data.accounts.forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.id;
        opt.textContent = a.name + ' (' + a.id + ')';
        select.appendChild(opt);
      });
      if (current) {
        const match = Array.from(select.options).find(o => o.value === current);
        if (match) {
          match.selected = true;
        } else {
          // Whatever was linked before isn't in this detected list (e.g. a
          // manually pasted id) -- keep it as an option rather than
          // silently dropping the existing link.
          const opt = document.createElement('option');
          opt.value = current;
          opt.textContent = current;
          opt.selected = true;
          select.appendChild(opt);
        }
      }
    });
    alert('Found ' + data.accounts.length + ' Basiq account(s) -- '
      + 'pick one per Manager account above.');
  }
  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', () => {
      const hidden = form.querySelector(
        'input[type=hidden][name="MANAGER_MCP_BASIQ_ACCOUNT_LINKS"]'
      );
      if (!hidden) return;
      const links = {};
      form.querySelectorAll('select.basiq-source').forEach(select => {
        if (select.value) links[select.dataset.bankKey] = select.value;
      });
      hidden.value = JSON.stringify(links);
    });
  });
"""


_OTHER_PROVIDER_SECTION = """
<section class="provider" data-provider="__other__" hidden>
  <h2>Other <span class="pill pill-configured">Not built in</span></h2>
  <p class="note">Manager-mcp doesn't ship a provider for every bank-feed or aggregator
    service -- only Aussie Bank Feeds (Basiq) so far. If the one you need isn't listed
    above, add it as a plugin. It's a plain Python class, not a fork: nothing else in this
    page, the sync loop, or the MCP tools needs to change.</p>
  <ol>
    <li>Create a module under
      <code>manager-mcp/src/manager_mcp/bank_feed_providers/</code> implementing
      <code>BankFeedProvider</code> (see <code>base.py</code>): a <code>name</code>,
      <code>is_configured(environ)</code>, and <code>async sync(client)</code> that imports
      transactions the way <code>basiq.py</code> does for Basiq.</li>
    <li>Optionally add <code>async setup_state(client, environ)</code> so it gets a panel
      on this page too, the same way Basiq's does.</li>
    <li>Add an instance to <code>PROVIDERS</code> in <code>registry.py</code>, then restart
      manager-mcp -- it'll appear in the dropdown above and be selectable via
      <code>MANAGER_MCP_BANK_FEED_PROVIDER</code>.</li>
  </ol>
  <pre>class MyFeedProvider(BankFeedProvider):
    name = "my-feed"
    display_name = "My Bank Feed"

    def is_configured(self, environ):
        return bool(environ.get("MY_FEED_API_KEY"))

    async def sync(self, client):
        ...  # import transactions into Manager; return a summary dict</pre>
  <p class="help">See <code>manager-mcp/README.md</code>'s "Adding a provider" section, and
    <code>basiq.py</code>'s module docstring for a worked example (third-party login,
    dedup, batch writes into Manager's <code>/api4</code>).</p>
</section>
"""


def _page_shell(title: str, body_html: str) -> str:
    return f"""
    <!doctype html><html><head><meta charset="utf-8">
    <title>{html.escape(title)}</title><style>{_PAGE_CSS}</style></head>
    <body>{body_html}</body></html>
    """


def _field_html(provider_name: str, f) -> str:
    key = html.escape(f.key)
    label = html.escape(f.label)
    help_text = html.escape(f.help)
    callout_html = f'<p class="callout">{html.escape(f.callout)}</p>' if f.callout else ""
    input_id = f"{provider_name}__{f.key}"
    if f.kind == "password":
        placeholder = html.escape(f.placeholder) or "not set"
        input_html = (
            f'<input type="password" id="{input_id}" name="{key}" placeholder="{placeholder}">'
        )
    elif f.kind == "select":
        opts = "".join(
            '<option value="{}" {}>{}</option>'.format(
                html.escape(o["key"]),
                "selected" if o["key"] == f.value else "",
                html.escape(o["name"]),
            )
            for o in f.options
        )
        input_html = (
            f'<select id="{input_id}" name="{key}">'
            f'<option value="">-- choose --</option>{opts}</select>'
        )
    elif f.kind == "account_links":
        row_template = (
            "<tr><td>{name}<br><code>{bank_key}</code></td>"
            '<td><select class="basiq-source" data-bank-key="{bank_key}">'
            '<option value="">— not linked —</option>{current_opt}</select></td></tr>'
        )
        def _current_opt(o: dict[str, str]) -> str:
            if not o.get("value"):
                return ""
            label = f'{o["value_label"]} ({o["value"]})' if o.get("value_label") else o["value"]
            value = html.escape(o["value"])
            return f'<option value="{value}" selected>{html.escape(label)}</option>'

        rows = "".join(
            row_template.format(
                name=html.escape(o["name"]),
                bank_key=html.escape(o["key"]),
                current_opt=_current_opt(o),
            )
            for o in f.options
        )
        input_html = (
            f'<table class="account-links" id="{input_id}_rows">'
            "<thead><tr><th>Manager.io account</th><th>Basiq source</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            f'<input type="hidden" id="{input_id}" name="{key}" value=\'{html.escape(f.value)}\'>'
        )
    else:
        value = html.escape(f.value)
        placeholder = html.escape(f.placeholder)
        input_html = (
            f'<input type="text" id="{input_id}" name="{key}" value="{value}" '
            f'placeholder="{placeholder}">'
        )
    return (
        f'<div class="field"><label for="{input_id}">{label}</label>{callout_html}{input_html}'
        f'<p class="help">{help_text}</p></div>'
    )


def _detected_chips(state) -> str:
    chips = []
    if state.detected.get("business_name"):
        chips.append(f"Business: {html.escape(state.detected['business_name'])}")
    if "bank_accounts" in state.detected:
        n = len(state.detected["bank_accounts"])
        chips.append(f"{n} Manager bank/cash account{'' if n == 1 else 's'} found")
    if "candidate_dedup_fields" in state.detected:
        n = len(state.detected["candidate_dedup_fields"])
        chips.append(
            f"{n} candidate custom field{'' if n == 1 else 's'} found"
            if n
            else "no existing custom fields detected"
        )
    if not chips:
        return ""
    spans = "".join(f'<span class="chip">{c}</span>' for c in chips)
    return f'<div class="chips">{spans}</div>'


async def bank_feeds_page(request: Request) -> Response:
    email = _require_session(request)
    if not email:
        return _redirect_to_login(PAGE_PATH)

    env = feeds_config.effective_environ()
    client = ManagerClient.from_env()
    try:
        states = []
        for provider in PROVIDERS:
            try:
                states.append((provider, await provider.setup_state(client, env)))
            except Exception:
                _log.exception("bank-feed setup: %s.setup_state failed", provider.name)
    finally:
        await client.aclose()

    current_provider = (env.get(PROVIDER_ENV) or "").strip()

    picker_options = []
    sections = []
    default_provider = states[0][0].name if states else ""
    for provider, state in states:
        is_picked = provider.name == current_provider or (
            not current_provider and state.configured
        )
        if is_picked:
            default_provider = provider.name

        if is_picked:
            pill, status_word = "pill-active", "Active"
        elif state.configured:
            pill, status_word = "pill-configured", "Configured"
        else:
            pill, status_word = "pill-unconfigured", "Not set up"
        display = html.escape(provider.display_name or provider.name)
        picker_options.append(
            f'<option value="{html.escape(provider.name)}">{display} — {status_word}</option>'
        )

        if is_picked:
            banner = (
                '<div class="banner banner-active">'
                "✓ This is the active provider -- it's what runs your scheduled "
                "syncs (and what <code>sync_bank_feeds</code> uses) right now.</div>"
            )
        elif not state.configured:
            missing = [f.label for f in state.fields if f.required]
            missing_html = (
                "<ul>" + "".join(f"<li>{html.escape(m)}</li>" for m in missing) + "</ul>"
                if missing
                else ""
            )
            banner = (
                '<div class="banner banner-unconfigured">Not set up yet.'
                + (f" You'll need:{missing_html}" if missing else "")
                + "Fill in the form below and click Save to turn it on.</div>"
            )
        else:
            banner = (
                '<div class="banner banner-active">Configured, but a different '
                "provider is currently active. Save here to switch to this one.</div>"
            )

        fields_html = "".join(_field_html(provider.name, f) for f in state.fields)
        notes_html = "".join(f"<p class='note'>{html.escape(n)}</p>" for n in state.notes)
        detect_button = (
            f'<button type="button" onclick="detectBasiq(\'{provider.name}\')">'
            "Detect Basiq accounts</button>"
            if provider.name == "basiq"
            else ""
        )
        form_summary = "Configure this provider" if not state.configured else "Edit configuration"
        form_html = (
            f"""<details {"open" if not state.configured else ""}>
              <summary>{form_summary}</summary>
              <form method="post" action="{PAGE_PATH}">
                <input type="hidden" name="provider" value="{html.escape(provider.name)}">
                {fields_html}
                {detect_button}
                <button type="submit">Save</button>
              </form>
            </details>"""
            if fields_html
            else f"""<form method="post" action="{PAGE_PATH}">
                <input type="hidden" name="provider" value="{html.escape(provider.name)}">
                <button type="submit">Use this provider</button>
              </form>"""
        )
        sections.append(
            f"""
            <section class="provider" data-provider="{html.escape(provider.name)}"
                      {"" if is_picked else "hidden"}>
              <h2>{html.escape(provider.display_name or provider.name)}
                <span class="pill {pill}">{status_word}</span></h2>
              {_detected_chips(state)}
              {banner}
              {notes_html}
              {form_html}
            </section>
            """
        )

    picker_options.append('<option value="__other__">Other… (add a new provider)</option>')
    sections.append(_OTHER_PROVIDER_SECTION)
    if not default_provider:
        default_provider = "__other__"

    picker_html = f"""
        <div class="picker-card">
          <label for="provider-picker">Bank feed provider</label>
          <select id="provider-picker" onchange="selectProvider(this.value)">
            {"".join(picker_options)}
          </select>
        </div>
        """

    sections_html = "".join(sections)
    init_script = (
        f"selectProvider({default_provider!r});"
        f"document.getElementById('provider-picker').value = {default_provider!r};"
    )
    body = f"""
      <div class="topbar">
        <h1>Bank feed setup</h1>
        <span class="who">{html.escape(email)} &middot;
          <a href="{LOGIN_PATH}?next={quote(PAGE_PATH)}">switch account</a></span>
      </div>
      <p class="lede">Pick a provider below. Anything already detected from Manager is
        shown for you; you only need to fill in what it can't get on its own.</p>
      {picker_html}
      {sections_html}
      <script>
        {_PAGE_JS.replace("__DETECT_BASIQ_PATH__", DETECT_BASIQ_PATH)}
        {init_script}
      </script>
    """
    return HTMLResponse(_page_shell("manager-mcp bank feed setup", body))


async def submit(request: Request) -> Response:
    email = _require_session(request)
    if not email:
        return _redirect_to_login(PAGE_PATH)

    form = await request.form()
    provider_name = str(form.get("provider", ""))
    updates: dict[str, str] = {}
    if provider_name:
        updates[PROVIDER_ENV] = provider_name
    saved_keys: list[str] = []
    for key, value in form.multi_items():
        if key == "provider":
            continue
        value = str(value).strip()
        if not value:
            continue
        updates[key] = value
        saved_keys.append(key)

    try:
        path = feeds_config.save(updates)
    except OSError as exc:
        _log.exception("bank-feed setup: could not save config")
        body = f"""
          <p><a href="{PAGE_PATH}">&larr; Back</a></p>
          <div class="banner banner-error">
            <strong>Could not save.</strong><br>
            Writing {html.escape(str(feeds_config.config_path()))} failed:
            {html.escape(str(exc))}
          </div>
          <p class="note">Check that its parent directory is mounted read-write into the
            manager-mcp container (see compose.yaml).</p>
        """
        return HTMLResponse(_page_shell("manager-mcp bank feed setup", body), status_code=500)

    updated = ", ".join(html.escape(k) for k in saved_keys) or "(only the provider selection)"
    body = f"""
      <p><a href="{PAGE_PATH}">&larr; Back</a></p>
      <div class="banner banner-active">
        <strong>✓ Saved.</strong> Provider set to <code>{html.escape(provider_name)}</code>,
        written to <code>{html.escape(str(path))}</code> -- not to
        <code>secrets/manager-mcp.env</code>. No restart needed: the next
        scheduled sync, or an agent calling the <code>sync_bank_feeds</code>
        tool, will use this.
      </div>
      <p class="note">Updated: {updated}.
        Anything left blank above (e.g. an unchanged password) was left as it was.</p>
    """
    return HTMLResponse(_page_shell("manager-mcp bank feed setup", body))


async def detect_basiq(request: Request) -> Response:
    email = _require_session(request)
    if not email:
        return JSONResponse({"error": "not signed in"}, status_code=401)

    from manager_mcp.bank_feed_providers.basiq import list_basiq_accounts

    body = await request.json()
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    if not username or not password:
        return JSONResponse({"error": "username and password are required"}, status_code=400)
    try:
        accounts = await list_basiq_accounts(username, password)
    except Exception as exc:
        _log.warning("bank-feed setup: Basiq account detection failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"accounts": accounts})
