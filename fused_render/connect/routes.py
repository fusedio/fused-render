"""/api/connect/* — the Connections page's and app pages' view of connected services
(SPEC-connect-oauth §4, §11).

    GET  /api/connect                       manager.list()
    GET  /api/connect/{key}                 manager.status(key)
    GET  /api/connect/{key}/icon            the pack's icon.svg
    POST /api/connect/{key}/login           {kind?, client_id?, value?}
    POST /api/connect/{key}/key             {value}  (api-key; the SPEC §4 spelling of login)
    POST /api/connect/{key}/callback        {url}    the paste fallback
    POST /api/connect/{key}/cancel
    POST /api/connect/{key}/logout
    GET  /api/connect/{key}/tools           [{name, description, input_schema, read_only, destructive}]
    POST /api/connect/{key}/tools/call      {name, args} -> {ok, text, content, is_error, isError}

A thin layer: every rule lives in `manager` and `tools`. Reads are unguarded
like every other GET (WF-5) and carry no credential — status names the account,
never a token. Every POST carries the D3 `X-Fused: 1` guard: a login opens a
browser and binds a port, a tool call acts on the user's account at a vendor,
and none of that may be fired blind by another website. Unknown key -> 404,
a malformed body -> 400, a tool call for a service that is not connected -> 409
with `code: "not_connected"` (runtime.js rejects on it).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Body, Header
from fastapi.responses import FileResponse, JSONResponse

from fused_render.connect import manager, registry, store, tools
from fused_render.server.common import _error, _require_fused

router = APIRouter()


def _unknown(key: str) -> JSONResponse:
    return _error(f"no connected service called {key!r}", status=404)


def _body(body) -> dict | None:
    if body is None:
        return {}
    return body if isinstance(body, dict) else None


def _str(body: dict, name: str) -> str | None | bool:
    """The string field `name`, None when absent, False when present and not a string."""
    v = body.get(name)
    if v is None:
        return None
    return v if isinstance(v, str) else False


@router.get("/api/connect")
def api_connect_list():
    return manager.list()


@router.get("/api/connect/{key}")
def api_connect_status(key: str):
    if registry.get(key) is None:
        return _unknown(key)
    return manager.status(key)


@router.get("/api/connect/{key}/icon")
def api_connect_icon(key: str):
    p = registry.get(key)
    if p is None:
        return _unknown(key)
    if not p.icon or not os.path.isfile(p.icon):
        return _error(f"{p.name} has no icon", status=404)
    return FileResponse(p.icon, media_type="image/svg+xml")


@router.post("/api/connect/{key}/login")
def api_connect_login(key: str, body=Body(default=None), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if registry.get(key) is None:
        return _unknown(key)
    b = _body(body)
    if b is None:
        return _error("body must be a JSON object")
    fields = {n: _str(b, n) for n in ("kind", "client_id", "value")}
    bad = [n for n, v in fields.items() if v is False]
    if bad:
        return _error(f"{bad[0]} must be a string")
    try:
        return manager.login(key, kind=fields["kind"] or None, client_id=fields["client_id"] or None,
                             value=fields["value"])
    except ValueError as e:
        return _error(str(e))


@router.post("/api/connect/{key}/key")
def api_connect_key(key: str, body=Body(default=None), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if registry.get(key) is None:
        return _unknown(key)
    b = _body(body)
    value = _str(b, "value") if b is not None else False
    if not value:
        return _error("body must be {\"value\": \"<token>\"}")
    try:
        return manager.login(key, kind="api-key", value=value)
    except ValueError as e:
        return _error(str(e))


@router.post("/api/connect/{key}/callback")
def api_connect_callback(key: str, body=Body(default=None), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if registry.get(key) is None:
        return _unknown(key)
    b = _body(body)
    url = _str(b, "url") if b is not None else False
    if not url:
        return _error("body must be {\"url\": \"<the address your browser ended up on>\"}")
    try:
        return manager.callback(key, url)
    except manager.NoFlow as e:
        return _error(str(e), status=409)
    except ValueError as e:
        return _error(str(e))


@router.post("/api/connect/{key}/cancel")
def api_connect_cancel(key: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if registry.get(key) is None:
        return _unknown(key)
    return manager.cancel(key)


@router.post("/api/connect/{key}/logout")
def api_connect_logout(key: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if registry.get(key) is None:
        return _unknown(key)
    return manager.logout(key)


@router.get("/api/connect/{key}/tools")
def api_connect_tools(key: str):
    if registry.get(key) is None:
        return _unknown(key)
    return [t.to_dict() for t in tools.list_tools(key)]


@router.post("/api/connect/{key}/tools/call")
def api_connect_call(key: str, body=Body(default=None), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    p = registry.get(key)
    if p is None:
        return _unknown(key)
    b = _body(body)
    if b is None or not isinstance(b.get("name"), str) or not b["name"]:
        return _error("body must be {\"name\": \"<tool>\", \"args\": {...}}")
    args = b.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _error("args must be a JSON object")
    if store.get(key) is None:
        return JSONResponse({"error": f"{p.name} is not connected", "code": "not_connected"},
                            status_code=409)
    out = tools.call_tool(key, b["name"], args)
    # runtime.js spells it isError (the MCP name); tools.py spells it is_error.
    return {**out, "isError": out.get("is_error", False)}
