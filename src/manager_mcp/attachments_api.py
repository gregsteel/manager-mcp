"""Attach a file to a Manager purchase invoice via internal (undocumented)
action endpoints, reusing the same authenticated client as /api2 calls — no
headless browser, no UI login session required.

Manager has two independent attachment mechanisms, discovered by testing
against a live instance (Manager 26.8.27.0, local "Test" business):

  * A newer, general-purpose Attachments list (any object can have any
    number of attachments; shows as a paperclip icon on list rows and under
    "Attachments" on the View page). `attach_file_via_api` writes here, via
    the `NewAttachment` action endpoint.
  * The older, single-file-per-document Image field the Edit page's own
    "Choose file" control shows (`#removeImageButton`, `ImageDeleted`).
    `attach_image_field_via_api` writes here.

Both were reverse-engineered, not documented, and verified only against
that one instance/version — re-verify after any Manager upgrade.

`_new_attachment_url`'s query encoding is ported from
https://github.com/isotherm/python-manager-api (`manager_api/__init__.py`'s
`Business._get_url` and `manager_api/object.py`'s `Object.attach_file`);
`attach_image_field_via_api`'s form-state handling was reverse-engineered
directly against a live instance (no prior art) — see its docstring.

Open risk common to both: whether the X-API-KEY header that authenticates
/api2 also authenticates these non-/api2 endpoints is unconfirmed for every
deployment shape. Both functions log the actual response (status, key
headers, a body snippet — see `_log_response`) before raising
`ApiAttachmentError` on anything unexpected, rather than guessing at or
asserting a specific cause in the error message: an early version of this
module asserted "most likely oauth2-proxy's login page" in its errors,
which turned out to just be an unverified guess baked into the text — a
caller (an LLM summarising the error) took it as a confirmed diagnosis and
repeated it as fact. Don't repeat that mistake; if you add a new failure
message here, report what was observed, not a theory of why. There is no
fallback if either function fails — the caller should surface the error
rather than silently give up on the attachment.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from base64 import urlsafe_b64encode
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

if TYPE_CHECKING:
    from manager_mcp.client import ManagerClient

_log = logging.getLogger(__name__)

_DEFAULT_FIELD = b"\xc2\x0c"
_STATE_FIELD_RE = re.compile(
    r'name="([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"'
    r' value="\{\}"'
)


class ApiAttachmentError(RuntimeError):
    """Could not attach the file via one of Manager's undocumented endpoints."""


def _log_response(action: str, method: str, url: str, response: httpx.Response) -> None:
    """On an unexpected status, log what actually came back — status, key
    headers, a body snippet — so a failure can be diagnosed from `docker
    compose logs manager-mcp` instead of guessed at. Silent on success, to
    keep routine operation quiet. In particular: this client never follows
    redirects (httpx's own default), so a 3xx's `Location` header — logged
    here — says definitively where the request was being sent (oauth2-proxy,
    Manager's own login, something else) rather than us inferring a cause
    from a missing body."""
    if response.status_code < 300:
        return
    location = response.headers.get("location")
    snippet = response.text[:500].replace("\n", " ")
    _log.warning(
        "%s: %s %s -> %s%s content-type=%s bytes=%d body(first 500 chars)=%r",
        action,
        method,
        url,
        response.status_code,
        f" location={location}" if location else "",
        response.headers.get("content-type", ""),
        len(response.content),
        snippet,
    )


def _b64encode(data: bytes) -> str:
    return urlsafe_b64encode(data).decode().rstrip("=")


def _action_url(
    *,
    ui_base_url: str,
    action: str,
    business_name: str,
    invoice_key: str,
    field: bytes = _DEFAULT_FIELD,
) -> str:
    """Build an action URL for one purchase invoice record.

    Ported from python-manager-api's `Business._get_url`. The query string
    is a hand-rolled protobuf-ish encoding of the business name and the
    record's key (as raw little-endian UUID bytes), url-safe-base64'd —
    this is what Manager's own UI generates for its "Edit"/action links,
    not a documented format. `field` is the tag byte preceding the record
    reference; it varies by action (`NewAttachment` uses `\\x0a`, most
    others — including `PurchaseInvoiceForm` — use the default `\\xc2\\x0c`).
    """
    action_slug = re.sub(r"(?<!^)(?=[A-Z])", "-", action).lower()
    name = business_name.encode()
    protobuf = b"\xa2\x06" + bytes([len(name)]) + name

    uuid = UUID(invoice_key)
    protobuf += field + b"\x12"
    protobuf += b"\x09" + uuid.bytes_le[:8]
    protobuf += b"\x11" + uuid.bytes_le[8:]

    query = _b64encode(protobuf)
    return f"{ui_base_url.rstrip('/')}/{action_slug}?{query}"


_KNOWN_ERROR_MARKERS = (
    "not authorised",
    "not authorized",
    "you do not have permission",
)


def _describe_unexpected_page(html: str) -> str:
    """A short, loggable description of an HTML page that wasn't the
    expected form. Manager's own error text (e.g. the permissions page
    from a prior real incident here — "You are not authorised to access
    this part of the system") can be tens of KB into the page, well past
    any small head snippet, so this looks for known phrases first and only
    falls back to a head snippet if none match."""
    lower = html.lower()
    for marker in _KNOWN_ERROR_MARKERS:
        idx = lower.find(marker)
        if idx != -1:
            start = max(0, idx - 100)
            return f"found {marker!r} at offset {idx}: {html[start:idx + 300]!r}"
    return f"first 2000 chars: {html[:2000]!r}"


def _resolve_upload(
    *,
    file_path: str | None,
    file_bytes: bytes | None,
    file_name: str | None,
) -> tuple[bytes, str]:
    if file_path is not None:
        path = Path(file_path)
        if not path.is_file():
            raise ApiAttachmentError(f"File not found: {file_path}")
        return path.read_bytes(), (file_name or path.name)
    if file_bytes is not None and file_name:
        return file_bytes, file_name
    raise ApiAttachmentError("Pass either file_path, or both file_bytes and file_name.")


async def attach_file_via_api(
    client: ManagerClient,
    *,
    ui_base_url: str,
    business_name: str,
    invoice_key: str,
    file_path: str | None = None,
    file_bytes: bytes | None = None,
    file_name: str | None = None,
) -> None:
    """Attach a file to a purchase invoice via the NewAttachment action URL
    — Manager's newer, general-purpose Attachments list (paperclip icon on
    list rows, "Attachments" on the View page). Does NOT touch the older
    per-document Image field; see `attach_image_field_via_api` for that.

    Reuses `client`'s underlying authenticated session (same X-API-KEY
    header used for /api2 calls) via `client.raw_post`, so this rides
    whatever auth path already gets /api2 traffic through to Manager rather
    than opening a new, unauthenticated connection.
    """
    data, name = _resolve_upload(file_path=file_path, file_bytes=file_bytes, file_name=file_name)

    url = _action_url(
        ui_base_url=ui_base_url,
        action="NewAttachment",
        business_name=business_name,
        invoice_key=invoice_key,
        field=b"\x0a",
    )
    mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    try:
        response = await client.raw_post(url, files={name: (name, data, mime_type)})
    except httpx.RequestError as exc:
        _log.warning("attach_file_via_api: POST %s raised %s: %s", url, type(exc).__name__, exc)
        raise ApiAttachmentError(f"Request to {url} failed: {exc}") from exc

    _log_response("attach_file_via_api", "POST", url, response)

    content_type = response.headers.get("content-type", "")
    if response.status_code >= 300 or "text/html" in content_type:
        raise ApiAttachmentError(
            f"NewAttachment POST got an unexpected response: HTTP "
            f"{response.status_code}, content-type {content_type!r}. Not "
            "Manager's usual response to this endpoint — see manager-mcp's "
            "logs (search for 'attach_file_via_api') for the actual status, "
            "headers, and body."
        )


def _extract_form_state(html: str) -> str:
    """Pull out the exact JSON text of the edit page's Vue `data: {...}`
    block, unmodified (no re-serialisation, to avoid any float/number
    formatting drift when it's resubmitted).

    Manager's purchase-invoice-form page bootstraps a Vue instance with the
    full current record — `app = new Vue({ el: "#v-model-form", data:
    {...} })` in the page's own inline <script> — then, per
    resources/htmx-extensions/form.js's `htmx:configRequest` handler, sets
    that same object (JSON-stringified, unchanged) as the value of a hidden
    field before every save. There is no smaller/partial update endpoint:
    every save round-trips the entire current record. This finds the
    boundaries of that JSON object by brace-counting (a regex can't handle
    the nesting).
    """
    vue_start = html.find("new Vue(")
    if vue_start == -1:
        raise ApiAttachmentError(
            "Could not find the edit form's Vue data block — the page "
            "returned something other than the expected edit form (this "
            "changed in a newer Manager version, most likely) — see "
            "manager-mcp's logs (search for 'GET edit form') for the "
            "actual body."
        )
    marker = "data: {"
    marker_at = html.index(marker, vue_start)
    start = marker_at + len("data: ")

    depth = 0
    in_string = False
    escaped = False
    end = None
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise ApiAttachmentError(
            "Could not parse the edit form's Vue data block (unbalanced braces)."
        )
    return html[start:end]


def _extract_state_field_name(html: str) -> str:
    """Find the opaque hidden-field name the form posts the state blob
    under (a GUID, e.g. `febb4049-dcdb-4c7a-a395-4b71da72a85b` — a fixed
    per-Manager-version constant embedded in its JS, not per-session, per
    testing, but extracted fresh each call rather than hardcoded in case
    that changes)."""
    match = _STATE_FIELD_RE.search(html)
    if not match:
        raise ApiAttachmentError(
            "Could not find the edit form's hidden state field — the page "
            "returned something other than the expected edit form."
        )
    return match.group(1)


async def attach_image_field_via_api(
    client: ManagerClient,
    *,
    ui_base_url: str,
    business_name: str,
    invoice_key: str,
    file_path: str | None = None,
    file_bytes: bytes | None = None,
    file_name: str | None = None,
) -> None:
    """Set a purchase invoice's legacy single-file Image field via its edit
    form — the field the Edit page's own "Choose file" control shows.
    Distinct from `attach_file_via_api`'s newer Attachments list.

    There is no small action endpoint for this field: Manager's edit page
    resubmits its *entire* current state (see `_extract_form_state`) on
    every save. This GETs the edit page, lifts that state blob unmodified,
    and resubmits it with the new file attached — reusing `client`'s
    authenticated session throughout, same as `attach_file_via_api`.
    """
    data, name = _resolve_upload(file_path=file_path, file_bytes=file_bytes, file_name=file_name)

    url = _action_url(
        ui_base_url=ui_base_url,
        action="PurchaseInvoiceForm",
        business_name=business_name,
        invoice_key=invoice_key,
    )

    try:
        get_response = await client.raw_get(url)
    except httpx.RequestError as exc:
        _log.warning(
            "attach_image_field_via_api: GET %s raised %s: %s", url, type(exc).__name__, exc
        )
        raise ApiAttachmentError(f"Request to {url} failed: {exc}") from exc

    _log_response("attach_image_field_via_api (GET edit form)", "GET", url, get_response)

    if get_response.status_code >= 300:
        raise ApiAttachmentError(
            f"Fetching the edit form got HTTP {get_response.status_code} for "
            f"{url} instead of the form itself — see manager-mcp's logs "
            "(search for 'GET edit form') for the actual status, headers "
            "(especially any redirect Location), and body."
        )
    html = get_response.text
    if "new Vue(" not in html:
        _log.warning(
            "attach_image_field_via_api (GET edit form): HTTP 200 but no "
            "'new Vue(' found (body %d chars); %s",
            len(html),
            _describe_unexpected_page(html),
        )
        raise ApiAttachmentError(
            "The edit form GET returned HTTP 200 but not the expected page "
            "content — see manager-mcp's logs (search for 'GET edit form') "
            "for the actual body."
        )

    state_json = _extract_form_state(html)
    state_field_name = _extract_state_field_name(html)

    mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    try:
        response = await client.raw_post(
            url,
            data={state_field_name: state_json, "ImageDeleted": "false"},
            files={"Image": (name, data, mime_type)},
        )
    except httpx.RequestError as exc:
        _log.warning(
            "attach_image_field_via_api: POST %s raised %s: %s", url, type(exc).__name__, exc
        )
        raise ApiAttachmentError(f"Request to {url} failed: {exc}") from exc

    _log_response("attach_image_field_via_api (POST form)", "POST", url, response)

    content_type = response.headers.get("content-type", "")
    if response.status_code >= 300 or "text/html" in content_type:
        raise ApiAttachmentError(
            f"Image-field POST got an unexpected response: HTTP "
            f"{response.status_code}, content-type {content_type!r}. Not "
            "Manager's usual response to this endpoint — see manager-mcp's "
            "logs (search for 'POST form') for the actual status, headers, "
            "and body."
        )


__all__ = [
    "ApiAttachmentError",
    "attach_file_via_api",
    "attach_image_field_via_api",
]
