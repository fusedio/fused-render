#!/usr/bin/env python3
"""A stand-in for `copilot --acp`, driven by tests/test_copilot_host.py.

Speaks just enough ACP (JSON-RPC, newline-delimited, over stdio) for the host
to run a session against it: answers `initialize`, `session/new`,
`session/load` (replaying one user + one agent chunk per `--replay N`, with
the noise a real replay carries), `session/set_mode` and
`session/set_config_option`, and on `session/prompt` streams two
`agent_message_chunk`s, optionally a thought, a `tool_call` +
`tool_call_update`, and `--perms N` `session/request_permission`s it waits
on, then replies `end_turn` — or `cancelled` when a `session/cancel` arrived
meanwhile, or a JSON-RPC error with `--error`. `--block-until PATH` holds the
prompt open until the file exists (the way to keep a turn open from a test);
`--die` exits mid-prompt without replying; `--signed-out` refuses
`session/new` the way a signed-out Copilot does (verified live).

Everything but a prompt is answered on the READER thread, so a mode switch
or a cancel sent mid-turn is seen at once; prompts run one at a time on the
main thread. A `session/prompt` that arrives while another is still running
is logged `{"overlap": true}` and — as the real CLI does — cancels the first:
the host must never send one (invariant 4).

Every inbound message is appended to `--log PATH` as `{"t": monotonic,
"msg": ...}`, every outbound one as `{"t", "sent": ...}`, so a test can order
"second prompt arrived" against "first reply sent". `--check-echo OUT` makes
each prompt log `{"echo_seen": bool}`: whether OUT (the host's out.jsonl)
already held that prompt's user echo when the prompt arrived (invariant 3).
`--dump-env PATH` writes this process's environment there at start.

The host passes the request's `argv` through verbatim, so the real `copilot`
flags it would carry (`--acp`, `--model`, ...) arrive here too and are
ignored — except `--session-id` beside `--acp`, which this refuses at start
exactly as the real CLI does (verified live 2026-10-16: the message below on
stderr, exit 2). The session id is the fake's own: `session/new` mints a
uuid4 and the replies and updates name it; `session/load` adopts the id it
is asked to load. Permission option ids deliberately differ
from their kinds (`o-once`/`o-always`/`o-no`) so a host that picked by id
would fail the test.
"""
import json
import os
import queue
import sys
import threading
import time
import uuid

ARGS = sys.argv[1:]
if "--acp" in ARGS and "--session-id" in ARGS:
    sys.stderr.write("error: '--acp' cannot be used with '--session-id <id>'\n")
    sys.exit(2)


def _flag(name, default=None):
    if name in ARGS:
        i = ARGS.index(name)
        return ARGS[i + 1] if i + 1 < len(ARGS) else default
    return default


LOG = _flag("--log")
SESSION_ID = ""   # minted by session/new, adopted by session/load
BLOCK_UNTIL = _flag("--block-until")
REPLAY = int(_flag("--replay", "1"))
WITH_TOOL = "--tool" in ARGS
PERM_EDIT = _flag("--perm-edit")   # the card is an edit of this (relative) path
PERMS = int(_flag("--perms", "1" if ("--perm" in ARGS or PERM_EDIT) else "0"))
WITH_ERROR = "--error" in ARGS
WITH_THOUGHT = "--think" in ARGS
DIE = "--die" in ARGS
SIGNED_OUT = "--signed-out" in ARGS
LATE_COMMANDS = "--late-commands" in ARGS
COMMANDS_AFTER_REPLY = "--commands-after-reply" in ARGS
IGNORE_EOF = "--ignore-eof" in ARGS
IGNORE_CANCEL = "--ignore-cancel" in ARGS        # a prompt that never stops
PERM_AFTER_CANCEL = "--perm-after-cancel" in ARGS
REPLAY_GAP = "--replay-gap" in ARGS
CHECK_ECHO = _flag("--check-echo")
DUMP_ENV = _flag("--dump-env")
TEXT = _flag("--text", "Hello")
if "--surrogate" in ARGS:
    TEXT += "\ud83d"   # half an emoji: what a chunk boundary can leave
TOOL_KIND = _flag("--tool-kind", "execute")
TOOL_TITLE = _flag("--tool-title", "Run ls")
COMMANDS = [{"name": "help"}, {"name": "model"}]

_prompts = queue.Queue()
_answers = {}                 # our request id -> the host's reply
_answered = threading.Condition()
_cancel = threading.Event()   # a session/cancel for the running prompt
_eof = threading.Event()
_in_flight = [0]              # prompts received and not yet replied to
_flight_lock = threading.Lock()
_out_lock = threading.Lock()
_log_lock = threading.Lock()
_ids = iter(range(1000, 10**6))


def _log(entry):
    if not LOG:
        return
    entry["t"] = time.monotonic()
    with _log_lock, open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _send(msg):
    with _out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()
    _log({"sent": msg})


def _reply(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _update(update_kind, **fields):
    body = {"sessionUpdate": update_kind}
    body.update(fields)
    _send({"jsonrpc": "2.0", "method": "session/update",
           "params": {"sessionId": SESSION_ID, "update": body}})


def _text(kind, text):
    _update(kind, content={"type": "text", "text": text})


def _load(rid):
    """The replay a real `session/load` streams before its reply — prompts,
    answers, and the rest a turn leaves (thoughts, a tool, usage)."""
    if REPLAY_GAP:
        # A prompt whose turn left no text (a tool, nothing said), then the
        # next prompt: two turns, never one prompt "q0q1".
        _text("user_message_chunk", "gap q0")
        _update("tool_call", toolCallId="gap", title="Run ls", kind="execute",
                status="completed", rawInput={"command": "ls"})
    for i in range(REPLAY):
        _text("user_message_chunk", "replayed q%d" % (i + 1))
        _text("agent_thought_chunk", "thinking about q%d" % (i + 1))
        _update("tool_call", toolCallId="old-%d" % i, title="Run ls",
                kind="execute", status="completed", rawInput={"command": "ls"})
        _update("tool_call_update", toolCallId="old-%d" % i, status="completed",
                content=[{"type": "content",
                          "content": {"type": "text", "text": "x"}}])
        _text("agent_message_chunk", "replayed a%d" % (i + 1))
    _update("usage_update", used=50, size=200000)
    _reply(rid, {})


def _serve(msg):
    """One inbound message, on the reader thread."""
    global SESSION_ID
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if method is None:
        with _answered:
            _answers[rid] = msg
            _answered.notify_all()
    elif method == "session/cancel":
        with _flight_lock:
            if _in_flight[0]:
                _cancel.set()   # an idle session ignores it, as the CLI does
    elif method == "session/prompt":
        with _flight_lock:
            if _in_flight[0]:
                _log({"overlap": True})
                _cancel.set()
            _in_flight[0] += 1
        _prompts.put((rid, params))
    elif method == "initialize":
        _reply(rid, {"protocolVersion": 1, "agentCapabilities": {}})
    elif method == "session/new":
        if SIGNED_OUT:
            _error(rid, -32000, "Authentication required")
            return
        SESSION_ID = str(uuid.uuid4())
        if not LATE_COMMANDS:
            _update("available_commands_update", availableCommands=COMMANDS)
        _update("session_info_update", title="A fake session")
        _reply(rid, {"sessionId": SESSION_ID})
        if LATE_COMMANDS:
            time.sleep(0.2)
            _update("available_commands_update", availableCommands=COMMANDS)
    elif method == "session/load":
        SESSION_ID = str(params.get("sessionId") or "")
        _load(rid)
    elif method in ("session/set_config_option", "session/set_mode"):
        _reply(rid, {})
    elif rid is not None:
        _error(rid, -32601, "unknown " + method)


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
        _serve(msg)
    _log({"eof": True})
    _eof.set()
    with _answered:
        _answered.notify_all()
    if not IGNORE_EOF:
        _prompts.put(None)


def _tool_input():
    if TOOL_KIND == "execute":
        return {"command": "ls -la", "description": "list files"}
    if TOOL_KIND == "edit":
        return {"path": "new_file.txt", "content": "hi"}
    return {}


def _ask(n):
    """Raise `n` permission requests at once and wait for every answer —
    or a cancel, which a real agent answers by giving up on the call."""
    ids = []
    for i in range(n):
        pid = next(_ids)
        ids.append(pid)
        call = {"toolCallId": "call-perm" + ("-%d" % i if i else ""),
                "title": "Run rm", "kind": "execute",
                "rawInput": {"command": "rm -rf build"}}
        if PERM_EDIT:
            # How Copilot sends an edit: no diff in rawInput, the change
            # as an ACP `diff` content entry.
            call = {"toolCallId": "call-perm", "title": "Edit " + PERM_EDIT,
                    "kind": "edit", "locations": [{"path": PERM_EDIT}],
                    "content": [{"type": "diff", "path": PERM_EDIT,
                                 "oldText": "one\ntwo\nthree\n",
                                 "newText": "one\n2\nthree\n"}]}
        _send({"jsonrpc": "2.0", "id": pid, "method": "session/request_permission",
               "params": {"sessionId": SESSION_ID,
                          "toolCall": call,
                          "options": [
                              {"optionId": "o-once", "name": "Allow",
                               "kind": "allow_once"},
                              {"optionId": "o-always", "name": "Always",
                               "kind": "allow_always"},
                              {"optionId": "o-no", "name": "Deny",
                               "kind": "reject_once"}]}})
    with _answered:
        while not all(i in _answers for i in ids) and not _eof.is_set():
            _answered.wait(0.05)


def _echo_seen(text):
    try:
        with open(CHECK_ECHO, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return False
    return any(r.get("type") == "user"
               and r.get("message", {}).get("content") == [{"type": "text", "text": text}]
               for r in rows)


def _prompt(rid, params):
    """One turn, on the main thread."""
    try:
        if CHECK_ECHO:
            text = "".join(b.get("text", "") for b in params.get("prompt") or [])
            _log({"echo_seen": _echo_seen(text), "text": text})
        if DIE:
            os._exit(3)
        _update("usage_update", used=1234, size=200000)
        if WITH_THOUGHT:
            _text("agent_thought_chunk", "Let me think")
        _text("agent_message_chunk", TEXT)
        _text("agent_message_chunk", " world")
        if WITH_TOOL:
            _update("tool_call", toolCallId="call-1", title=TOOL_TITLE,
                    kind=TOOL_KIND, status="pending", rawInput=_tool_input(),
                    locations=[{"path": "new_file.txt"}] if TOOL_KIND == "edit" else [])
            _update("tool_call_update", toolCallId="call-1", status="completed",
                    content=[{"type": "content",
                              "content": {"type": "text", "text": "a.txt\nb.txt"}}],
                    rawOutput={"stdout": "a.txt\nb.txt"})
        if PERMS:
            _ask(PERMS)
        if BLOCK_UNTIL:
            while not os.path.exists(BLOCK_UNTIL) and not _eof.is_set() \
                    and (IGNORE_CANCEL or not _cancel.is_set()):
                time.sleep(0.05)
        if _cancel.is_set():
            if PERM_AFTER_CANCEL:
                # Winding down still asks: a card raised mid-cancel.
                _ask(1)
            _reply(rid, {"stopReason": "cancelled"})
            return
        if WITH_ERROR:
            _error(rid, -32000, "credits exhausted")
            return
        _text("agent_message_chunk", "!")
        _reply(rid, {"stopReason": "end_turn",
                     "usage": {"inputTokens": 1234, "outputTokens": 7,
                               "cachedReadTokens": 100}})
        if COMMANDS_AFTER_REPLY:
            _update("available_commands_update",
                    availableCommands=COMMANDS + [{"name": "after"}])
    finally:
        with _flight_lock:
            _in_flight[0] -= 1
            if not _in_flight[0]:
                _cancel.clear()


def main():
    if DUMP_ENV:
        with open(DUMP_ENV, "w", encoding="utf-8") as fh:
            json.dump(dict(os.environ), fh)
    threading.Thread(target=_reader, daemon=True).start()
    while True:
        item = _prompts.get()
        if item is None:
            return 0
        _prompt(*item)


if __name__ == "__main__":
    sys.exit(main())
