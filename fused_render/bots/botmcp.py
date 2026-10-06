"""The bot's stdio MCP server (docs/bots.md §6): spawned by `claude`, it
forwards every tool call to the fused-render server, where the agent engine runs
it against the bot's browser.

    argv: <server origin> <bot id> <task token>

    initialize   -> capabilities.tools
    tools/list   -> GET  <origin>/api/bots/<id>/tools?token=<token>   (cached for the process)
    tools/call   -> POST <origin>/api/bots/<id>/tool {name, args, token}, NO timeout
                    (an approval card can sit for an hour); content/isError passed through
    ping         -> {}

The roster and every rule live in the server (`agent_engine.roster_for` /
`handle_tool`); this file is a pipe. Stdlib only, no `fused_render`
import, no assumption about cwd: the CLI launches it with the app's own
python and an allowlisted environment. Framing is the one
`claude_agent/permission_server.py` speaks: newline-delimited JSON-RPC,
stdout carries protocol only, diagnostics go to stderr, UTF-8 whatever the
locale. Each request is served on its own thread so a `tools/call` blocked on
the user never stalls `ping` or a parallel call.
"""
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "bot"
SERVER_VERSION = "1"

ORIGIN = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
BOT_ID = sys.argv[2] if len(sys.argv) > 2 else ""
TOKEN = sys.argv[3] if len(sys.argv) > 3 else ""

_stdout_lock = threading.Lock()
_tools_lock = threading.Lock()
_tools_cache = None
LIST_TIMEOUT = 30.0


def _log(msg: str) -> None:
    print("botmcp: " + msg, file=sys.stderr, flush=True)


def _utf8_stdio() -> None:
    """UTF-8 on the wire whatever the locale (permission_server._utf8_stdio:
    the CLI is Node and writes raw UTF-8; a cp1252 pipe on Windows dies on the
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
    return "%s/api/bots/%s/%s" % (ORIGIN, urllib.parse.quote(BOT_ID, safe=""), path)


def _http_error(exc: Exception) -> str:
    """One sentence for the model out of whatever the server said."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read().decode("utf-8", "replace") or "{}")
            msg = body.get("error") if isinstance(body, dict) else None
        except (ValueError, OSError):
            msg = None
        return "the bot server refused the call (HTTP %d): %s" % (exc.code, msg or exc.reason)
    return "the bot server could not be reached: %s" % exc


def _tools() -> list:
    """The roster, fetched once: the server builds it per task."""
    global _tools_cache
    with _tools_lock:
        if _tools_cache is None:
            q = urllib.parse.urlencode({"token": TOKEN})
            with urllib.request.urlopen(_url("tools") + "?" + q, timeout=LIST_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
            tools = data.get("tools") if isinstance(data, dict) else None
            _tools_cache = tools if isinstance(tools, list) else []
        return _tools_cache


def _call(name: str, args: dict) -> dict:
    body = json.dumps({"name": name, "args": args, "token": TOKEN}).encode("utf-8")
    req = urllib.request.Request(_url("tool"), data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Fused": "1"})
    try:
        # No timeout on purpose: ask / login / an approval card block for as
        # long as the user takes. The CLI's per-server `timeout` is the ceiling.
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"content": [{"type": "text", "text": "error: " + _http_error(exc)}], "isError": True}
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        return {"content": [{"type": "text", "text": "error: the bot server sent no tool result"}], "isError": True}
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
    if not (ORIGIN and BOT_ID and TOKEN):
        _log("usage: botmcp.py <server origin> <bot id> <token>")
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
