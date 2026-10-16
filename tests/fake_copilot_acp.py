#!/usr/bin/env python3
"""A stand-in for `copilot --acp`, driven by tests/test_copilot_host.py.

Speaks just enough ACP (JSON-RPC, newline-delimited, over stdio) for the host
to run a session against it: answers `initialize`, `session/new`,
`session/load` (replaying one user + one agent chunk per `--replay N`),
`session/set_config_option`, and on `session/prompt` streams two
`agent_message_chunk`s, optionally a `tool_call` + `tool_call_update`,
optionally a `session/request_permission` it waits on, then replies
`end_turn` — or `cancelled` when a `session/cancel` arrived meanwhile, or a
JSON-RPC error with `--error`. `--block-until PATH` holds the prompt open
until the file exists (the way to keep a turn open from a test);
`--die` exits mid-prompt without replying.

Every inbound message is appended to `--log PATH` as `{"t": monotonic,
"msg": ...}`, and every outbound reply to a prompt as `{"t", "sent": ...}`,
so a test can order "second prompt arrived" against "first reply sent".

The host passes the request's `argv` through verbatim, so the real `copilot`
flags it would carry (`--acp`, `--model`, `--session-id`, ...) arrive here
too and are ignored, except `--session-id`, whose value is what
`session/new` names the session. Permission option ids deliberately differ
from their kinds (`o-once`/`o-always`/`o-no`) so a host that picked by id
would fail the test.
"""
import json
import os
import queue
import sys
import threading
import time

ARGS = sys.argv[1:]


def _flag(name, default=None):
    if name in ARGS:
        i = ARGS.index(name)
        return ARGS[i + 1] if i + 1 < len(ARGS) else default
    return default


LOG = _flag("--log")
SESSION_ID = _flag("--session-id", "fake-sid")
BLOCK_UNTIL = _flag("--block-until")
REPLAY = int(_flag("--replay", "1"))
WITH_TOOL = "--tool" in ARGS
WITH_PERM = "--perm" in ARGS
WITH_ERROR = "--error" in ARGS
DIE = "--die" in ARGS
TEXT = _flag("--text", "Hello")
TOOL_KIND = _flag("--tool-kind", "execute")
TOOL_TITLE = _flag("--tool-title", "Run ls")

_inbox = queue.Queue()
_out_lock = threading.Lock()
_ids = iter(range(1000, 10**6))


def _log(entry):
    if not LOG:
        return
    entry["t"] = time.monotonic()
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _send(msg):
    with _out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()
    _log({"sent": msg})


def _reply(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _update(update_kind, **fields):
    body = {"sessionUpdate": update_kind}
    body.update(fields)
    _send({"jsonrpc": "2.0", "method": "session/update",
           "params": {"sessionId": SESSION_ID, "update": body}})


def _reader():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        _log({"msg": msg})
        _inbox.put(msg)
    _inbox.put(None)


def _tool_input():
    if TOOL_KIND == "execute":
        return {"command": "ls -la", "description": "list files"}
    if TOOL_KIND == "edit":
        return {"path": "new_file.txt", "content": "hi"}
    return {}


def _prompt(rid, params):
    """One turn. Pulls from the inbox while it waits so a `session/cancel`
    (or the permission answer) is seen mid-turn."""
    cancelled = False
    if DIE:
        os._exit(3)
    _update("usage_update", used=1234, size=200000)
    _update("agent_message_chunk", content={"type": "text", "text": TEXT})
    _update("agent_message_chunk", content={"type": "text", "text": " world"})
    if WITH_TOOL:
        _update("tool_call", toolCallId="call-1", title=TOOL_TITLE,
                kind=TOOL_KIND, status="pending", rawInput=_tool_input(),
                locations=[{"path": "new_file.txt"}] if TOOL_KIND == "edit" else [])
        _update("tool_call_update", toolCallId="call-1", status="completed",
                content=[{"type": "content",
                          "content": {"type": "text", "text": "a.txt\nb.txt"}}],
                rawOutput={"stdout": "a.txt\nb.txt"})
    if WITH_PERM:
        pid = next(_ids)
        _send({"jsonrpc": "2.0", "id": pid, "method": "session/request_permission",
               "params": {"sessionId": SESSION_ID,
                          "toolCall": {"toolCallId": "call-perm", "title": "Run rm",
                                       "kind": "execute",
                                       "rawInput": {"command": "rm -rf build"}},
                          "options": [
                              {"optionId": "o-once", "name": "Allow",
                               "kind": "allow_once"},
                              {"optionId": "o-always", "name": "Always",
                               "kind": "allow_always"},
                              {"optionId": "o-no", "name": "Deny",
                               "kind": "reject_once"}]}})
        while True:
            msg = _inbox.get()
            if msg is None:
                return
            if msg.get("id") == pid and "method" not in msg:
                break
            if msg.get("method") == "session/cancel":
                cancelled = True
    if BLOCK_UNTIL:
        while not os.path.exists(BLOCK_UNTIL) and not cancelled:
            try:
                msg = _inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if msg is None:
                return
            if msg.get("method") == "session/cancel":
                cancelled = True
    # Anything that arrived without us asking (a cancel after the perm).
    while not cancelled:
        try:
            msg = _inbox.get_nowait()
        except queue.Empty:
            break
        if msg is not None and msg.get("method") == "session/cancel":
            cancelled = True
    if cancelled:
        _reply(rid, {"stopReason": "cancelled"})
        return
    if WITH_ERROR:
        _send({"jsonrpc": "2.0", "id": rid,
               "error": {"code": -32000, "message": "credits exhausted"}})
        return
    _update("agent_message_chunk", content={"type": "text", "text": "!"})
    _reply(rid, {"stopReason": "end_turn",
                 "usage": {"inputTokens": 1234, "outputTokens": 7,
                           "cachedReadTokens": 100}})


def main():
    threading.Thread(target=_reader, daemon=True).start()
    while True:
        msg = _inbox.get()
        if msg is None:
            return 0
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            _reply(rid, {"protocolVersion": 1, "agentCapabilities": {}})
        elif method == "session/new":
            _update("available_commands_update",
                    availableCommands=[{"name": "help"}, {"name": "model"}])
            _update("session_info_update", title="A fake session")
            _reply(rid, {"sessionId": SESSION_ID})
        elif method == "session/load":
            for i in range(REPLAY):
                _update("user_message_chunk",
                        content={"type": "text", "text": "replayed q%d" % (i + 1)})
                _update("agent_message_chunk",
                        content={"type": "text", "text": "replayed a%d" % (i + 1)})
            _reply(rid, {})
        elif method == "session/prompt":
            _prompt(rid, params)
        elif method == "session/set_config_option":
            _reply(rid, {})
        elif rid is not None and method:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": "unknown " + method}})


if __name__ == "__main__":
    sys.exit(main())
