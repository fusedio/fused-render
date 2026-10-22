"""A Streamable HTTP MCP client on `httpx` (SPEC-connect-oauth §3, §11).

fused-render is the MCP client for every connected service: bots, app pages,
scheduled tasks and the chat reach a vendor's hosted MCP server through this
one class, with a token the server process holds and app code never sees.

    initialize            -> POST {"method": "initialize"}; keep Mcp-Session-Id
    initialized           -> POST {"method": "notifications/initialized"} (202, no body)
    tools/list            -> POST, followed through `nextCursor`
    tools/call            -> POST; {"content", "isError", "structuredContent"?}
    close                 -> DELETE with the session id (best effort)

Every request carries `Accept: application/json, text/event-stream`,
`MCP-Protocol-Version` and `Authorization: Bearer <token>`. The token may be a
callable, called per request, so a refresh made elsewhere (manager.token) is
picked up by the next request without rebuilding the client. The reply is
either one JSON body or an SSE stream; the stream is read until the message
whose `id` matches the request, and notifications met on the way are noted
(`tools/list_changed` sets `tools_changed`) and otherwise skipped.

Errors are typed because the caller acts on them: `McpAuthError` (401, and
GitHub's hosted server's 400 "Authorization header is badly formatted" that
carries `WWW-Authenticate: Bearer error="invalid_token"`) means refresh once
and retry; `McpScopeError` (403, `insufficient_scope`) names the scope that was
missing. A 404 on a request that carried a session id means the server dropped
the session, and the spec's answer is to initialize again, which happens once.

**Hand-written rather than the `mcp` package**, which is only in the
`[fused]` extra and brings anyio with it; the subset needed here is four
methods over one POST, the way `bots/botmcp.py` is the server side on the
stdlib. The legacy HTTP+SSE transport (GET an endpoint event first) is not
supported: vendors call it deprecated and none of the first wave needs it.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
import threading
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "fused-render", "version": "1"}
#: Generous: a `tools/call` runs the vendor's tool, which may search a
#: workspace. Connect stays short so an unreachable server fails fast.
TIMEOUT = httpx.Timeout(120.0, connect=15.0)


class McpError(Exception):
    """The server answered with a JSON-RPC error, an HTTP error, or nonsense."""


class McpAuthError(McpError):
    """The token was refused (401, or a 400 that says `invalid_token`)."""

    def __init__(self, message: str, www_authenticate: str | None = None):
        super().__init__(message)
        self.www_authenticate = www_authenticate


class McpScopeError(McpError):
    """The token lacks a scope this call needs (403 `insufficient_scope`)."""

    def __init__(self, message: str, scope: str | None = None,
                 www_authenticate: str | None = None):
        super().__init__(message)
        self.scope = scope
        self.www_authenticate = www_authenticate


_PARAM = re.compile(r'([A-Za-z_][A-Za-z0-9_\-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')


def auth_params(header: str | None) -> dict:
    """`Bearer error="invalid_token", scope="a b"` -> {"error": ..., "scope": ...}.

    Lenient on purpose: vendors disagree about quoting and commas, and the
    only consumers are the two branches below and discovery's own copy.
    """
    if not header:
        return {}
    out = {}
    for m in _PARAM.finditer(header):
        value = m.group(2) if m.group(2) is not None else m.group(3)
        out[m.group(1).lower()] = re.sub(r"\\(.)", r"\1", value)
    return out


class McpClient:
    """One connection to one MCP server. Thread-safe: requests may overlap."""

    def __init__(self, url: str, token: str | Callable[[], str] | None,
                 http: httpx.Client | None = None, protocol_version: str = PROTOCOL_VERSION):
        self.url = url
        self._token = token
        self._own_http = http is None
        self._http = http if http is not None else httpx.Client(timeout=TIMEOUT)
        self.protocol_version = protocol_version
        self.session_id: str | None = None
        self.server_info: dict | None = None
        #: Set when the server sent `notifications/tools/list_changed`; the
        #: caller (tools.py) drops its cache and clears it.
        self.tools_changed = False
        self._ids = itertools.count(1)
        self._init_lock = threading.RLock()
        self._initialized = False

    # -- framing --------------------------------------------------------

    def _headers(self) -> dict:
        h = {"Accept": "application/json, text/event-stream",
             "Content-Type": "application/json",
             "MCP-Protocol-Version": self.protocol_version}
        tok = self._token() if callable(self._token) else self._token
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _raise_for_status(r: httpx.Response, body: str) -> None:
        www = r.headers.get("www-authenticate")
        params = auth_params(www)
        snippet = (body or "").strip()[:300]
        if r.status_code == 401 or (r.status_code == 400 and params.get("error") == "invalid_token"):
            raise McpAuthError(
                f"the MCP server refused the token (HTTP {r.status_code})"
                + (f": {snippet}" if snippet else ""), www)
        if r.status_code == 403:
            scope = params.get("scope")
            raise McpScopeError(
                "the token lacks a permission this call needs"
                + (f" (scope: {scope})" if scope else "")
                + (f": {snippet}" if snippet and not scope else ""), scope, www)
        raise McpError(f"the MCP server answered HTTP {r.status_code}"
                       + (f": {snippet}" if snippet else ""))

    def _note(self, msg: dict) -> None:
        """A notification (or a server request) met while waiting for a reply."""
        method = msg.get("method")
        if method == "notifications/tools/list_changed":
            self.tools_changed = True
        logger.debug("mcp %s: skipped %s", self.url, method)

    def _read_sse(self, r: httpx.Response, req_id) -> dict:
        """Read `data:` events until the one answering `req_id`."""
        data: list[str] = []

        def flush() -> dict | None:
            if not data:
                return None
            raw = "\n".join(data)
            data.clear()
            try:
                msg = json.loads(raw)
            except ValueError:
                logger.debug("mcp %s: unparseable SSE event", self.url)
                return None
            for m in msg if isinstance(msg, list) else [msg]:
                if isinstance(m, dict) and "id" in m and m.get("id") == req_id \
                        and ("result" in m or "error" in m):
                    return m
                if isinstance(m, dict):
                    self._note(m)
            return None

        for line in r.iter_lines():
            if line == "":
                found = flush()
                if found is not None:
                    return found
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data.append(value)
        found = flush()
        if found is not None:
            return found
        raise McpError("the MCP server closed the stream without answering")

    def _post(self, payload: dict, *, expect_reply: bool) -> dict | None:
        with self._http.stream("POST", self.url, headers=self._headers(),
                               content=json.dumps(payload).encode("utf-8")) as r:
            if r.status_code >= 400:
                body = r.read().decode("utf-8", "replace")
                if r.status_code == 404 and self.session_id and expect_reply:
                    raise _SessionGone()
                self._raise_for_status(r, body)
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            if not expect_reply:
                r.read()
                return None
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype == "text/event-stream":
                msg = self._read_sse(r, payload["id"])
            else:
                body = r.read().decode("utf-8", "replace")
                try:
                    msg = json.loads(body)
                except ValueError:
                    raise McpError(f"the MCP server sent a non-JSON reply ({ctype or 'no type'})") from None
                if isinstance(msg, list):  # a batch: pick ours
                    msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == payload["id"]), None)
                if not isinstance(msg, dict):
                    raise McpError("the MCP server sent no JSON-RPC response")
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
            raise McpError(f"{err.get('message') or 'error'} (code {err.get('code')})")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    def _request(self, method: str, params: dict | None = None) -> dict:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            payload["params"] = params
        try:
            return self._post(payload, expect_reply=True)
        except _SessionGone:
            if method == "initialize":
                raise McpError("the MCP server answered 404") from None
            # The spec's rule: a 404 for a session id means start a new one.
            logger.debug("mcp %s: session expired, re-initializing", self.url)
            self.session_id = None
            self._initialized = False
            self.initialize()
            payload["id"] = next(self._ids)
            return self._post(payload, expect_reply=True)

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload, expect_reply=False)

    # -- the four methods --------------------------------------------------

    def initialize(self) -> dict:
        """Open the session; returns the server's initialize result."""
        with self._init_lock:
            result = self._request("initialize", {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            })
            negotiated = result.get("protocolVersion")
            if isinstance(negotiated, str) and negotiated:
                self.protocol_version = negotiated
            self.server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
            try:
                self._notify("notifications/initialized")
            except McpAuthError:
                raise
            except McpError as e:  # a server that 4xx's the notification still serves
                logger.debug("mcp %s: initialized notification refused: %s", self.url, e)
            self._initialized = True
            return result

    def _ensure(self) -> None:
        if self._initialized:
            return
        with self._init_lock:  # two first calls racing open one session, not two
            if not self._initialized:
                self.initialize()

    def list_tools(self) -> list[dict]:
        self._ensure()
        tools: list[dict] = []
        cursor = None
        for _ in range(50):  # a bound, not a page size: no server has 50 pages of tools
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            tools.extend(t for t in (page if isinstance(page, list) else [])
                         if isinstance(t, dict) and isinstance(t.get("name"), str))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools_changed = False
        return tools

    def call(self, name: str, args: dict) -> dict:
        self._ensure()
        result = self._request("tools/call", {"name": name, "arguments": args or {}})
        out = {"content": result.get("content") if isinstance(result.get("content"), list) else [],
               "isError": bool(result.get("isError", False))}
        if "structuredContent" in result:
            out["structuredContent"] = result["structuredContent"]
        return out

    def close(self) -> None:
        if self.session_id:
            try:
                self._http.delete(self.url, headers=self._headers(), timeout=5.0)
            except Exception:  # noqa: BLE001 - best effort; the server times sessions out
                pass
            self.session_id = None
        self._initialized = False
        if self._own_http:
            self._http.close()


class _SessionGone(Exception):
    """404 on a request that carried a session id (internal)."""
