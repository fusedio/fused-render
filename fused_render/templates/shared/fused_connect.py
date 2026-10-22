"""A Python client for the local Connections API (SPEC-connect-oauth.md §4, §11).

Mirrors `fused.connect` from `fused_render/static/runtime.js` for Python code
— a starter's data file, a scheduled script — that wants a connected
service's tools (Notion, GitHub, Slack...) without holding its token. The
token never reaches this module: it asks the running server to make the call
(`POST /api/connect/<key>/tools/call`) and gets the tool's result back, the
same door an app page's JS uses. Signing in is not here either; that is a
person at Preferences → Connections, and a call to a provider nobody has
connected raises a `RuntimeError` that says so.

**Travels with `appenv.py`.** Stdlib only (`json`, `os`, `urllib`) and never
`import fused_render` — a `.py` data file runs in a subprocess with
`PYTHONPATH` stripped (SPEC PY-15). The one channel back to the app is
`appenv.origin()` (`FUSED_RENDER_ORIGIN`, which the server exports for every
process it starts), loaded by PATH as `fused_ai.py` does, so a user's own
`appenv.py` beside their script cannot shadow it. Rejected: `fused_ai.py`'s
`server.json` fallback for processes the server did not start. A tool call
acts on a person's account at a vendor; guessing a server from a file on disk
is the wrong default for that, and a script outside the server can export
`FUSED_RENDER_ORIGIN` itself.

    import fused_connect
    fused_connect.providers()                      # [{key, name, state, account, ...}]
    fused_connect.tools("notion")                  # [{name, description, input_schema, ...}]
    r = fused_connect.call("notion", "search", {"query": "roadmap"})
    print(r["text"])                               # r: {ok, text, content, is_error}
"""
from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote


def _load_sibling_appenv():
    """THIS file's own `appenv.py`, by path — see the
    `adding-a-shared-template-utility` skill: the shared dir is APPENDED to a
    user module's `sys.path`, so a bare `import appenv` can resolve a user's
    same-named module instead."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appenv.py")
    spec = importlib.util.spec_from_file_location("_fused_connect_appenv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


appenv = _load_sibling_appenv()

# A tool call runs at the vendor (an MCP server over the network), so this is
# generous; it bounds "the server stopped answering", not a slow tool.
_TIMEOUT_S = 120.0


def _origin() -> str:
    origin = appenv.origin()
    if not origin:
        raise RuntimeError(
            "fused_connect: no Fused server to ask — FUSED_RENDER_ORIGIN is not "
            "set. Run this from a page or task the Fused app started, or set "
            "FUSED_RENDER_ORIGIN to the app's address (http://127.0.0.1:<port>).")
    return origin.rstrip("/")


def _path(key: str, tail: str = "") -> str:
    return "/api/connect/" + quote(str(key), safe="") + tail


def _said(raw: bytes, status: int) -> str:
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        data = None
    if isinstance(data, dict):
        for field in ("error", "detail"):
            if isinstance(data.get(field), str) and data[field]:
                return data[field]
    return f"HTTP {status}"


def _request(method: str, path: str, body: dict | None = None, *, key: str | None = None):
    url = _origin() + path
    headers = {"Accept": "application/json"}
    data = None
    if method != "GET":
        # Not authentication (D3): the marker every mutation on this server
        # requires, so a foreign page cannot write blind.
        headers["X-Fused"] = "1"
        headers["Content-Type"] = "application/json"
        data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        said = _said(e.read() or b"", e.code)
        if e.code == 409 and key is not None:
            raise RuntimeError(
                f"fused_connect: {key} is not connected ({said}). Open Fused → "
                f"Preferences → Connections and connect it, then try again.") from None
        raise RuntimeError(f"fused_connect: {method} {path} failed: {said}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"fused_connect: could not reach the Fused server at {_origin()}: {e.reason}") from None
    return json.loads(raw.decode("utf-8") or "null")


def providers() -> list:
    """Every provider the app knows: `{key, name, state, account, auth, tools,
    privacy, ...}` — `state` is disconnected | pending | connected | reconnect
    | error."""
    out = _request("GET", "/api/connect")
    return out if isinstance(out, list) else (out or {}).get("providers", [])


def tools(key: str) -> list:
    """The tools a connected provider offers: `[{name, description,
    input_schema, read_only, destructive}]`; `[]` when it is not connected."""
    out = _request("GET", _path(key, "/tools"), key=key)
    return out if isinstance(out, list) else (out or {}).get("tools", [])


def call(key: str, name: str, args: dict | None = None) -> dict:
    """Run one tool on the provider's side: `{ok, text, content, is_error}`.
    A tool that ran and failed comes back with `is_error: True` (read
    `text`); a provider nobody has connected raises `RuntimeError`."""
    return _request("POST", _path(key, "/tools/call"), {"name": name, "args": args or {}}, key=key)
