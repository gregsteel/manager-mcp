"""Async httpx client for Manager.io API2 (GET always; writes scope-gated)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from manager_mcp.scopes import WRITE_METHODS, WritePolicy

BASE_QUERY_KEYS = frozenset(
    {"term", "sortBy", "sortByDesc", "skip", "pageSize", "fields"}
)


class ConfigError(ValueError):
    """Missing or invalid Manager connection configuration."""


class ManagerUnavailableError(RuntimeError):
    """Manager API unreachable (process down, wrong URL, network)."""


class ManagerApiError(RuntimeError):
    """Manager returned an HTTP error (especially 5xx from bad field types)."""


class ManagerClient:
    """httpx wrapper. GET always; mutations require WritePolicy authorization."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        extra_query_keys: frozenset[str] | None = None,
        policy: WritePolicy | None = None,
        ui_username: str | None = None,
        ui_password: str | None = None,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ConfigError("MANAGER_API_URL is required")
        if not api_key or not api_key.strip():
            raise ConfigError("MANAGER_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self._extra_query_keys = extra_query_keys or frozenset()
        self.policy = policy or WritePolicy(frozenset(), frozenset())
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-KEY": self._api_key},
            timeout=60.0,
        )
        # X-API-KEY only authenticates /api2 — Manager's own web UI/action
        # endpoints (used by manager_mcp.attachments_api's raw_get/raw_post)
        # need an actual logged-in session, which Manager also accepts as
        # HTTP Basic Auth against a real Manager user (see
        # https://github.com/isotherm/python-manager-api, which authenticates
        # this way). Optional: attachment uploads via the undocumented
        # endpoints simply fail with 302s to "/" without it.
        self._ui_auth = (
            httpx.BasicAuth(ui_username, ui_password) if ui_username and ui_password else None
        )

    @property
    def has_ui_auth(self) -> bool:
        """True when MANAGER_UI_USERNAME/PASSWORD were supplied (the mcp user)."""
        return self._ui_auth is not None

    @classmethod
    def from_env(
        cls,
        *,
        extra_query_keys: frozenset[str] | None = None,
        policy: WritePolicy | None = None,
    ) -> ManagerClient:
        return cls(
            os.environ.get("MANAGER_API_URL", ""),
            os.environ.get("MANAGER_API_KEY", ""),
            extra_query_keys=extra_query_keys,
            policy=policy if policy is not None else WritePolicy.from_env(),
            ui_username=os.environ.get("MANAGER_UI_USERNAME") or None,
            ui_password=os.environ.get("MANAGER_UI_PASSWORD") or None,
        )

    def clean_params(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not params:
            return {}
        allowed = BASE_QUERY_KEYS | self._extra_query_keys
        return {k: v for k, v in params.items() if k in allowed and v is not None}

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._send("GET", path, params=params)

    async def post(self, path: str, *, json: Any = None) -> Any:
        return await self._send("POST", path, json=json)

    async def put(self, path: str, *, json: Any = None) -> Any:
        return await self._send("PUT", path, json=json)

    async def patch(self, path: str, *, json: Any = None) -> Any:
        return await self._send("PATCH", path, json=json)

    async def delete(self, path: str) -> Any:
        return await self._send("DELETE", path)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        method = method.upper()
        url_path = path if path.startswith("/") else f"/{path}"
        if method in WRITE_METHODS:
            self.policy.authorize(method, url_path)
        try:
            response = await self._client.request(
                method,
                url_path,
                params=self.clean_params(params) if method == "GET" else None,
                json=json,
            )
        except httpx.RequestError as exc:
            # Keep MCP alive when Manager desktop/API is closed.
            raise ManagerUnavailableError(
                f"Manager.io is not reachable at {self.base_url}. "
                "Ask the user to open Manager (with API enabled) and retry. "
                f"Detail: {exc}"
            ) from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            snippet = (exc.response.text or "")[:200]
            if status >= 500:
                raise ManagerApiError(
                    f"Manager HTTP {status} for {method} {url_path}. "
                    "Usually a bad field name or type (e.g. PaidBy must be int; "
                    "use ReceivedIn/PaidFrom not BankAccount). "
                    "get_record a template, fix the body, retry once. "
                    f"Body: {snippet}"
                ) from exc
            raise
        if not response.content:
            return None
        return response.json()

    async def raw_get(self, url: str) -> httpx.Response:
        """GET an absolute URL (e.g. a non-/api2 UI page) using this
        client's underlying session, plus HTTP Basic Auth from
        MANAGER_UI_USERNAME/MANAGER_UI_PASSWORD if configured (Manager's own
        web UI ignores X-API-KEY; see the comment on `self._ui_auth`) — see
        `raw_post`."""
        return await self._client.get(url, auth=self._ui_auth)

    async def raw_post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """POST to an absolute URL (e.g. a non-/api2 action endpoint) using
        this client's underlying session — same host, same X-API-KEY header
        plus HTTP Basic Auth if configured (see `raw_get`) — but bypassing
        `_send`'s JSON-only/error-wrapping behaviour so the caller can
        inspect the raw response itself (e.g. to detect a login redirect
        rather than a genuine API failure)."""
        return await self._client.post(url, data=data, files=files, auth=self._ui_auth)

    async def raw_basic(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        """Call a Manager UI/`/api/` path with HTTP Basic Auth only (the mcp user).

        Does not send `X-API-KEY` (that header is `/api2` only). Optional
        extra headers are for UI actions that expect HTMX (`HX-Request`), or
        for `/api4` calls that need `Manager-Business: <name>`. `json_body`
        covers `/api4`'s JSON-body writes (confirmed working via HTTP Basic
        Auth on 2026-08-31 — /api4/receipt-batch and /payment-batch, same
        payload shape Manager's own frontend posts).
        """
        if self._ui_auth is None:
            raise ConfigError(
                "MANAGER_UI_USERNAME and MANAGER_UI_PASSWORD are required "
                "to call Manager UI actions (the mcp user). X-API-KEY only covers /api2."
            )
        async with httpx.AsyncClient(
            timeout=self._client.timeout,
            auth=self._ui_auth,
            follow_redirects=False,
        ) as client:
            return await client.request(method, url, headers=headers, json=json_body)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = [
    "BASE_QUERY_KEYS",
    "ConfigError",
    "ManagerApiError",
    "ManagerClient",
    "ManagerUnavailableError",
]
