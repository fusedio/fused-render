"""A specialist session's stdio MCP server (PLAN B.4 "Session MCP servers"): spawned by `claude` for a main
chat, a breakout or a nightly run, it forwards every tool call to the fused-render server.

    argv: <server origin> <specialist id> <kind> <token> <server: specialist|specialist_services>

    initialize   -> capabilities.tools
    tools/list   -> GET  <origin>/api/specialists/<id>/session-tools?kind=&token=&server=   (cached)
    tools/call   -> POST <origin>/api/specialists/<id>/session-tool {kind, token, server, name, args}, NO timeout
                    (a service call can sit behind an approval card); content/isError passed through
    ping         -> {}

One script, two servers: `specialist` lists the `note` tool, `specialist_services` the specialist's service
roster (`services.roster`). The roster, the token check and every rule live in the server; this file is a pipe,
copied from `bots/botmcp.py`. Stdlib only, no `fused_render` import (the CLI launches it with the app's own python
from a stable copy that outlives an update).
"""
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "1"

ORIGIN = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
SPEC_ID = sys.argv[2] if len(sys.argv) > 2 else ""
KIND = sys.argv[3] if len(sys.argv) > 3 else ""
TOKEN = sys.argv[4] if len(sys.argv) > 4 else ""
SERVER_NAME = sys.argv[5] if len(sys.argv) > 5 else ""

_stdout_lock = threading.Lock()
_tools_lock = threading.Lock()
_tools_cache = None
LIST_TIMEOUT = 30.0


def _log(msg: str) -> None:
    print("sessmcp: " + msg, file=sys.stderr, flush=True)


def _utf8_stdio() -> None:
    """UTF-8 on the wire whatever the locale (permission_server._utf8_stdio:
    the CLI is Node and writes raw UTF-8; a non-UTF-8 locale pipe dies on the
    first curly quote). Never raises."""
    for stream, extra in ((sys.stdin, {"errors": "replace"}),
                          (sys.stdout, {"newline": "\n"})):
        try:
            stream.reconfigure(encoding="utf-8", **extra)
        except (AttributeError, ValueError, OSError) as exc:
            _log("could not force UTF-8 stdio (%s)" % exc)


def _send(payload: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def _url(path: str) -> str:
    return "%s/api/specialists/%s/%s" % (ORIGIN, urllib.parse.quote(SPEC_ID, safe=""), path)


def _http_error(exc: Exception) -> str:
    """One sentence for the model out of whatever the server said."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read().decode("utf-8", "replace") or "{}")
            msg = body.get("error") if isinstance(body, dict) else None
        except (ValueError, OSError):
            msg = None
        return "the fused-render server refused the call (HTTP %d): %s" % (exc.code, msg or exc.reason)
    return "the fused-render server could not be reached: %s" % exc


def _tools() -> list:
    """The roster, fetched once: the server builds it per task."""
    global _tools_cache
    with _tools_lock:
        if _tools_cache is None:
            q = urllib.parse.urlencode({"kind": KIND, "token": TOKEN, "server": SERVER_NAME})
            with urllib.request.urlopen(_url("session-tools") + "?" + q, timeout=LIST_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
            tools = data.get("tools") if isinstance(data, dict) else None
            _tools_cache = tools if isinstance(tools, list) else []
        return _tools_cache


def _call(name: str, args: dict) -> dict:
    body = json.dumps({"kind": KIND, "token": TOKEN, "server": SERVER_NAME, "name": name, "args": args}).encode("utf-8")
    req = urllib.request.Request(_url("session-tool"), data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Fused": "1"})
    try:
        # No timeout on purpose: ask / login / an approval card block for as
        # long as the user takes. The CLI's per-server `timeout` is the ceiling.
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"content": [{"type": "text", "text": "error: " + _http_error(exc)}], "isError": True}
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        return {"content": [{"type": "text", "text": "error: the fused-render server sent no tool result"}], "isError": True}
    return {"content": data["content"], "isError": bool(data.get("isError"))}


def _dispatch(method: str, params: dict) -> dict:
    if method == "initialize":
        v = params.get("protocolVersion")
        return {"protocolVersion": v if isinstance(v, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
    if method == "tools/list":
        return {"tools": _tools()}
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tools/call without a tool name")
        args = params.get("arguments")
        return _call(name, args if isinstance(args, dict) else {})
    if method == "ping":
        return {}
    raise LookupError("unknown method: %s" % method)


def _serve(req_id, method: str, params: dict) -> None:
    try:
        _send({"jsonrpc": "2.0", "id": req_id, "result": _dispatch(method, params)})
    except LookupError as exc:
        _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(exc)}})
    except Exception as exc:  # noqa: BLE001 — a JSON-RPC error, never a dead server
        _send({"jsonrpc": "2.0", "id": req_id,
               "error": {"code": -32603, "message": "%s: %s" % (type(exc).__name__, _http_error(exc)
                                                                if isinstance(exc, (urllib.error.URLError, OSError))
                                                                else exc)}})


def main() -> int:
    _utf8_stdio()
    if not (ORIGIN and SPEC_ID and KIND and TOKEN and SERVER_NAME):
        _log("usage: sessmcp.py <server origin> <specialist id> <kind> <token> <server>")
        return 2
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or msg.get("id") is None:
            continue  # notifications (initialized, cancelled, …) need no answer
        params = msg.get("params")
        threading.Thread(target=_serve, args=(msg["id"], msg.get("method") or "",
                                              params if isinstance(params, dict) else {}),
                         daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
