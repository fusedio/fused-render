"""Minimal stdio MCP server whose tools are ONE `/api/ai` turn's tools, run by
the caller of that turn (D1460).

`/api/ai` with `tools` runs the model through an agent CLI (`claude -p`,
`copilot -p`, `codex exec`, `gemini -p`) that calls tools natively — the
only way those CLIs call a tool is an MCP server, so this file is that
server, spawned by the CLI with the turn id and the fused-render origin in
its environment. It owns no tool: `tools/list` is answered by the server's
`GET /api/ai/turns/<turn>/tools` (the JSON Schemas the caller sent), and
every `tools/call` is forwarded to `POST /api/ai/turns/<turn>/call`, which
BLOCKS until the caller has posted that call's result (the embedded
workbench runs the tool in the browser and answers through the canvas
host) — the same trick `permission_server.py` plays with a permission
prompt, pointed at a tool. One request at a time, in the order the CLI
sent them: a CLI that issues parallel `tool_use` blocks sees them answered
one after another, which is the serialization the browser-side tool loop
needs anyway (it holds one pending round).

Spawned by the CLI, never by the app: stdlib only, no `fused_render`
import, no assumption about cwd. Configuration arrives in the environment
(the MCP client passes an allowlist plus exactly the dict the config names):

    FUSED_RENDER_ORIGIN    the server, e.g. http://127.0.0.1:1777
    FUSED_AI_TURN          the turn id (the server mints it per request)
    FUSED_AI_TOOL_TIMEOUT  seconds a call may block (default 600)

Wire (MCP over stdio, JSON-RPC 2.0, one message per line): `initialize`,
`notifications/initialized` (no reply), `ping`, `tools/list`, `tools/call`.
A tool's result is one text block; a failed tool (`isError` from the
caller, a server the bridge cannot reach, a turn that closed) is a text
block with `isError: true`, so the model sees a failed tool rather than a
hung one. Verified against claude 2.1.316 and copilot 1.0.102.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Callable, Optional

SERVER_NAME = "fused_tools"
PROTOCOL_VERSION = "2024-11-05"

ORIGIN = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")
TURN = os.environ.get("FUSED_AI_TURN") or ""
CALL_TIMEOUT = float(os.environ.get("FUSED_AI_TOOL_TIMEOUT") or "600")
#: Headroom over the server's own wait, so the server answers (with the
#: timeout it reports) before this socket gives up.
_HTTP_SLACK_S = 30.0

Http = Callable[[str, str, Optional[dict], float], dict]


def http(method: str, path: str, body: Optional[dict], timeout: float) -> dict:
    """One JSON request to the fused-render server (loopback, `X-Fused`
    guard). Raises on transport or HTTP failure; the caller words it."""
    import urllib.request  # lazy: most of this process's life is idle
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        ORIGIN + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-Fused": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _tools_list(fetch: Http) -> list:
    data = fetch("GET", f"/api/ai/turns/{TURN}/tools", None, 30.0)
    out = []
    for tool in data.get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        out.append({
            "name": str(tool["name"]),
            "description": str(tool.get("description") or ""),
            "inputSchema": tool.get("parameters") or tool.get("inputSchema")
            or {"type": "object", "properties": {}},
        })
    return out


def _tools_call(fetch: Http, name: str, arguments) -> dict:
    """The MCP result for one call: the caller's text, or an `isError`
    block wording what went wrong."""
    try:
        data = fetch("POST", f"/api/ai/turns/{TURN}/call",
                     {"name": name, "input": arguments if isinstance(arguments, dict) else {}},
                     CALL_TIMEOUT + _HTTP_SLACK_S)
    except Exception as exc:  # noqa: BLE001 - every failure is a failed tool
        return {"content": [{"type": "text", "text": f"tool call failed: {exc}"}],
                "isError": True}
    text = data.get("content")
    if not isinstance(text, str):
        text = json.dumps(text) if text is not None else ""
    return {"content": [{"type": "text", "text": text}],
            "isError": bool(data.get("isError"))}


def handle(msg: dict, fetch: Http = http) -> Optional[dict]:
    """The JSON-RPC reply for one message, or None for a notification."""
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"}}}
    if method == "tools/list":
        try:
            tools = _tools_list(fetch)
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32000, "message": f"could not read the turn's tools: {exc}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        return {"jsonrpc": "2.0", "id": mid,
                "result": _tools_call(fetch, name, params.get("arguments"))}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is None:
        return None  # a notification (`notifications/initialized`, cancelled)
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    # UTF-8 stdio whatever the locale: the CLI's MCP client writes raw UTF-8.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
