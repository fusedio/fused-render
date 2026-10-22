"""The tools a connected service offers, from wherever they come (SPEC-connect-oauth §4, §11).

    list_tools(key)            -> [ToolSpec]   connected providers only; cached 60 s per key
    call_tool(key, name, args) -> {"ok", "text", "content", "is_error"}
    invalidate(key)            -> drop the cache and the open MCP session

Two sources, merged, remote first:

- **remote**: the provider's `tools.remote` MCP server through `McpClient`,
  with `manager.token(key)` as a per-request callable. A refused token
  (`McpAuthError`) forces one refresh and one retry; a second refusal marks
  the provider `reconnect` and comes back as a sentence that says so. A 403
  `insufficient_scope` comes back naming the scope (no step-up in this wave).
- **native**: `providers/<key>/tools.py`, loaded by path, exposing
  `TOOLS: list[dict]` and `call(ctx, name, args) -> str | dict`, where `ctx`
  carries `.token`, `.http` (an `httpx.Client`, 30 s timeout) and `.record`.
  A native tool raises `ToolError(msg)` for a vendor error, which becomes
  `{ok: False, text: "<key>: <msg>"}`, and `ToolAuthError` for a refused
  token, which gets the same one-refresh-then-reconnect treatment as MCP.

`read_only` comes from MCP `annotations.readOnlyHint` (default False) and
`destructive` from `destructiveHint` (default True): the MCP spec's own
defaults, so a tool that says nothing about itself goes through the bots'
approval gate.

One `McpClient` is kept per key so a session is initialized once rather than
per call; the token callable means a refresh never needs a new client. The
token never appears in a result, a log line or an exception message here.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass

import httpx

from fused_render.connect import manager, registry
from fused_render.connect.mcp_client import (
    McpAuthError,
    McpClient,
    McpError,
    McpScopeError,
)

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60.0
NATIVE_TIMEOUT = 30.0


@dataclass
class ToolSpec:
    provider: str
    name: str
    description: str
    input_schema: dict
    read_only: bool
    destructive: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("provider")
        return d


class ToolError(Exception):
    """A native tool's vendor said no; the message is the vendor's own word."""


class ToolAuthError(ToolError):
    """A native tool's vendor refused the token (refresh once, then reconnect)."""


class _Ctx:
    """What a native tool's `call` gets: the token, an HTTP client, the record."""

    def __init__(self, token: str, http: httpx.Client, record: dict | None):
        self.token = token
        self.http = http
        self.record = record or {}


_lock = threading.Lock()
_cache: dict[str, tuple[float, list[ToolSpec]]] = {}
_clients: dict[str, McpClient] = {}
_native: dict[str, tuple[str, float, object]] = {}   # key -> (path, mtime, module)
_warming: set[str] = set()


def _http() -> httpx.Client:
    """The HTTP client for one native call or one MCP connection (patched by tests)."""
    return httpx.Client(timeout=NATIVE_TIMEOUT)


def _mcp_http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))


# -- sources ------------------------------------------------------------------

def _native_module(p):
    """The pack's tools.py, loaded by path and reloaded when it changes."""
    path = os.path.join(p.dir, "tools.py")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _lock:
        hit = _native.get(p.key)
        if hit and hit[0] == path and hit[1] == mtime:
            return hit[2]
    spec = importlib.util.spec_from_file_location(f"fused_render_connect_native_{p.key}", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with _lock:
        _native[p.key] = (path, mtime, mod)
    return mod


def _spec_from_native(key: str, t: dict) -> ToolSpec:
    return ToolSpec(provider=key, name=t["name"], description=t.get("description") or "",
                    input_schema=t.get("input_schema") or {"type": "object", "properties": {}},
                    read_only=bool(t.get("read_only", False)),
                    destructive=bool(t.get("destructive", True)))


def _spec_from_mcp(key: str, t: dict) -> ToolSpec:
    ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
    return ToolSpec(provider=key, name=t["name"], description=t.get("description") or "",
                    input_schema=t.get("inputSchema") if isinstance(t.get("inputSchema"), dict)
                    else {"type": "object", "properties": {}},
                    read_only=bool(ann.get("readOnlyHint", False)),
                    destructive=bool(ann.get("destructiveHint", True)))


def _client(key: str, url: str) -> McpClient:
    with _lock:
        c = _clients.get(key)
        if c is None or c.url != url:
            c = McpClient(url, token=lambda: manager.token(key), http=_mcp_http())
            _clients[key] = c
        return c


def _drop_client(key: str) -> None:
    with _lock:
        c = _clients.pop(key, None)
    if c is not None:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


class _Reconnect(Exception):
    """The token was refused twice, or could not be refreshed."""


def _with_auth_retry(key: str, fn):
    """Run `fn()`; on a refused token refresh once and run it again.

    A second refusal (or a refresh that fails) marks the provider `reconnect`.
    """
    try:
        return fn()
    except (McpAuthError, ToolAuthError):
        logger.debug("connect %s: token refused, refreshing once", key)
    try:
        manager.token(key, force_refresh=True)
    except manager.NotConnected:
        raise _Reconnect() from None
    try:
        return fn()
    except (McpAuthError, ToolAuthError):
        manager.mark_reconnect(key)
        raise _Reconnect() from None


def _remote_tools(key: str, url: str) -> list[ToolSpec]:
    def go():
        try:
            return _client(key, url).list_tools()
        except McpAuthError:
            _drop_client(key)
            raise
    return [_spec_from_mcp(key, t) for t in _with_auth_retry(key, go)]


# -- public -------------------------------------------------------------------

def _connected(key: str) -> bool:
    try:
        return manager.is_connected(key)
    except Exception:  # noqa: BLE001 - an unreadable store is "not connected" here
        return False


def list_tools(key: str) -> list[ToolSpec]:
    """Every tool `key` offers right now; [] unless connected."""
    p = registry.get(key)
    if p is None or not _connected(key):
        return []
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        client = _clients.get(key)
    if hit and now - hit[0] < CACHE_SECONDS and not (client and client.tools_changed):
        return list(hit[1])
    specs: list[ToolSpec] = []
    ok = True
    remote = (p.tools or {}).get("remote")
    if remote:
        try:
            specs.extend(_remote_tools(key, remote))
        except (_Reconnect, manager.NotConnected):
            ok = False
        except Exception as e:  # noqa: BLE001 - an unreachable server lists nothing
            logger.info("connect %s: tools/list failed: %s", key, e)
            ok = False
    try:
        mod = _native_module(p)
    except Exception as e:  # noqa: BLE001 - a broken pack must not break the page
        logger.warning("connect %s: native tools.py failed to load: %s", key, e)
        mod = None
    if mod is not None:
        seen = {s.name for s in specs}
        for t in getattr(mod, "TOOLS", None) or []:
            if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"] not in seen:
                specs.append(_spec_from_native(key, t))
    if ok:
        with _lock:
            _cache[key] = (now, specs)
    return list(specs)


def cached_count(key: str) -> int | None:
    """How many tools the cache holds for `key`, without any network (status polls)."""
    with _lock:
        hit = _cache.get(key)
    return len(hit[1]) if hit else None


def warm(key: str) -> None:
    """List `key`'s tools on a daemon thread, once at a time (fills `cached_count`)."""
    with _lock:
        if key in _warming:
            return
        _warming.add(key)

    def run():
        try:
            list_tools(key)
        finally:
            with _lock:
                _warming.discard(key)
    threading.Thread(target=run, name=f"connect-tools-{key}", daemon=True).start()


def invalidate(key: str) -> None:
    with _lock:
        _cache.pop(key, None)
    _drop_client(key)


def _text_of(content: list) -> str:
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(parts)


def _result(content: list, is_error: bool, text: str | None = None, **extra) -> dict:
    out = {"ok": not is_error, "text": _text_of(content) if text is None else text,
           "content": content, "is_error": is_error}
    out.update(extra)
    return out


def _fail(text: str, **extra) -> dict:
    return _result([{"type": "text", "text": text}], True, text, **extra)


def call_tool(key: str, name: str, args: dict) -> dict:
    """Run one tool. Never raises for a vendor's answer; the text says what happened."""
    p = registry.get(key)
    if p is None:
        return _fail(f"no connected service called {key!r}", error="unknown_provider")
    if not _connected(key):
        return _fail(f"{p.name} is not connected. Connect it in Preferences > Connections.",
                     error="not_connected")
    args = args if isinstance(args, dict) else {}
    try:
        mod = _native_module(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("connect %s: native tools.py failed to load: %s", key, e)
        mod = None
    native_names = {t.get("name") for t in (getattr(mod, "TOOLS", None) or []) if isinstance(t, dict)}
    remote = (p.tools or {}).get("remote")
    reconnect = (f"{p.name} refused the saved sign-in. Reconnect it in Preferences > Connections.")
    try:
        if remote and name not in native_names:
            def go():
                try:
                    return _client(key, remote).call(name, args)
                except McpAuthError:
                    _drop_client(key)
                    raise
            r = _with_auth_retry(key, go)
            with _lock:
                client = _clients.get(key)
            if client is not None and client.tools_changed:
                with _lock:
                    _cache.pop(key, None)
            extra = {"structured_content": r["structuredContent"]} if "structuredContent" in r else {}
            return _result(r["content"], bool(r.get("isError")), **extra)
        if mod is None or name not in native_names:
            return _fail(f"{p.name} has no tool called {name!r}", error="unknown_tool")

        def run_native():
            record = manager.record(key)
            with _http() as http:
                return mod.call(_Ctx(manager.token(key), http, record), name, args)
        out = _with_auth_retry(key, run_native)
        if isinstance(out, str):
            return _result([{"type": "text", "text": out}], False)
        text = json.dumps(out, ensure_ascii=False, default=str)
        return _result([{"type": "text", "text": text}], False)
    except (_Reconnect, manager.NotConnected):
        return _fail(reconnect, error="reconnect")
    except McpScopeError as e:
        scope = f" ({e.scope})" if e.scope else ""
        return _fail(f"{p.name} did not grant the permission this tool needs{scope}. "
                     f"Disconnect and connect again to grant it.", error="insufficient_scope")
    except ToolError as e:
        return _fail(f"{key}: {e}")
    except McpError as e:
        return _fail(f"{p.name}: {e}")
    except httpx.HTTPError as e:
        return _fail(f"{p.name} could not be reached: {e.__class__.__name__}")
    except Exception as e:  # noqa: BLE001 - a tool's bug is a failed call, not a 500
        logger.warning("connect %s: tool %s raised %s", key, name, e.__class__.__name__)
        return _fail(f"{p.name}: {name} failed: {e}")
