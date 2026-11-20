#!/usr/bin/env python3
"""A stand-in for `cursor-agent acp`, driven by tests/test_cursor_host.py —
the Cursor twin of fake_opencode_acp.py, with Cursor's own shapes as its
ACP agent builds them (docs/CURSOR_BACKEND.md §3, read from the CLI's
bundle, release 2026.11.19-f005e9d).

Answers `initialize` (Cursor's `agentCapabilities`, the `cursor_login` auth
method), `session/new` (a uuid4 `sessionId` plus `modes` — agent/plan/ask —
`models` and `configOptions`), `session/load` (replaying one user + one
agent chunk per `--replay N` BEFORE it replies, as Cursor awaits its replay;
`-32602` under `--unknown-session`), `session/set_model` (`{}`; an unknown
id is `-32602 Invalid model value` — `auto` included: Cursor's Auto is the
listed `auto-smart` row, named "Auto", as on the real CLI), `session/set_mode`
(`{}`), and the `cursor/list_available_models` extension method. `--signed-out` makes
`session/new` fail as Cursor does without a login.

On `session/prompt`: (no echo — Cursor sends none) two
`agent_message_chunk`s; with `--tool` a `tool_call` whose `rawInput` is
PARTIAL (a truncated command, as `partialToolCall` streams it), a refresh
`tool_call_update` with the whole input, then `in_progress` and
`completed`; `--perms N` `session/request_permission`s (Cursor's card:
title `` `cmd` ``, kind `execute`, no `rawInput`, the reason as `content`,
options `allow-once` / `allow-always` / `reject-once`, raised AFTER the call's
`in_progress`; then `completed` with output when allowed and a BARE
`completed` — no content, not `failed` — when rejected, as observed),
`--perm-edit PATH` an edit card with a diff, `--edit-new PATH` Cursor's
uncarded edit of a NEW file (announced `Edit File` with an empty
`rawInput`, the path in a refresh, and the diff only on `completed`, in the
mangled new-file shape the real CLI sends: `oldText: "-- /dev/null"`,
`newText: "++ b/<path>\n<content>"`), `--question` a question card (`__ask_question_skip__`), `--ext` an
agent→client `cursor/update_todos` request; then replies `{stopReason:
"end_turn"}` (`cancelled` after a `session/cancel`, a JSON-RPC error with
`--error`). `--block-until PATH` holds the prompt open; `--die` exits
mid-prompt. Like the real server, stdin EOF does not end the process (a
SIGTERM does) unless `--exit-on-eof`.

Every inbound message is appended to `--log PATH` as `{"t", "msg"}`, every
outbound one as `{"t", "sent"}`; answers to our own requests also as
`{"answer": ...}`. `--check-echo OUT` logs whether the host's echo was in
out.jsonl when the prompt arrived.
"""
import json
import os
import queue
import signal
import sys
import threading
import time
import uuid

ARGS = sys.argv[1:]


def _flag(name, default=None):
    if name in ARGS:
        i = ARGS.index(name)
        return ARGS[i + 1] if i + 1 < len(ARGS) else default
    return default


LOG = _flag("--log")
SESSION_ID = ""
BLOCK_UNTIL = _flag("--block-until")
REPLAY = int(_flag("--replay", "1"))
WITH_TOOL = "--tool" in ARGS
PERM_EDIT = _flag("--perm-edit")
PERMS = int(_flag("--perms", "1" if ("--perm" in ARGS or PERM_EDIT) else "0"))
QUESTION = "--question" in ARGS
EXT = "--ext" in ARGS
WITH_ERROR = "--error" in ARGS
DIE = "--die" in ARGS
UNKNOWN_SESSION = "--unknown-session" in ARGS
SIGNED_OUT = "--signed-out" in ARGS
CHECK_ECHO = _flag("--check-echo")
DUMP_ENV = _flag("--dump-env")
EXIT_ON_EOF = "--exit-on-eof" in ARGS
EDIT_NEW = _flag("--edit-new")
MODEL = _flag("--current-model", "auto-smart")
#: Shaped like the real list (2026.11.19-f005e9d): Auto is `auto-smart`.
MODELS = ["auto-smart", "composer-2.5", "gpt-5.5", "claude-sonnet-5-5"]
NAMES = {"auto-smart": "Auto", "composer-2.5": "Composer 2.5", "gpt-5.5": "GPT-5.5",
         "claude-sonnet-5-5": "Claude Sonnet 5.5"}
COMMANDS = [{"name": "copy-request-id", "description": "Copy the last request ID"}]

_prompts = queue.Queue()
_answers = {}
_answered = threading.Condition()
_cancel = threading.Event()
_eof = threading.Event()
_in_flight = [0]
_flight_lock = threading.Lock()
_out_lock = threading.Lock()
_log_lock = threading.Lock()
_ids = iter(range(1000, 10**6))
_mode = ["agent"]
_model = [MODEL]


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


def _state():
    modes = {"currentModeId": _mode[0], "availableModes": [
        {"id": "agent", "name": "Agent"}, {"id": "plan", "name": "Plan"},
        {"id": "ask", "name": "Ask"}]}
    models = {"currentModelId": _model[0],
              "availableModels": [{"modelId": m, "name": NAMES[m]} for m in MODELS]}
    options = [{"id": "mode", "name": "Mode", "type": "select",
                "currentValue": _mode[0], "options": []},
               {"id": "model", "name": "Model", "type": "select",
                "currentValue": _model[0],
                "options": [{"value": m, "name": NAMES[m]} for m in MODELS]}]
    return {"modes": modes, "models": models, "configOptions": options}


def _serve(msg):
    global SESSION_ID
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if method is None:
        _log({"answer": msg})
        with _answered:
            _answers[rid] = msg
            _answered.notify_all()
    elif method == "session/cancel":
        with _flight_lock:
            if _in_flight[0]:
                _cancel.set()
    elif method == "session/prompt":
        with _flight_lock:
            if _in_flight[0]:
                _log({"overlap": True})
                _cancel.set()
            _in_flight[0] += 1
        _prompts.put((rid, params))
    elif method == "initialize":
        _reply(rid, {"protocolVersion": 1,
                     "agentCapabilities": {
                         "loadSession": True,
                         "mcpCapabilities": {"http": True, "sse": True},
                         "promptCapabilities": {"audio": False, "embeddedContext": False,
                                                "image": True},
                         "sessionCapabilities": {"list": {}}},
                     "authMethods": [{"id": "cursor_login", "name": "Cursor Login"}]})
    elif method == "session/new":
        if SIGNED_OUT:
            # The observed reply (2026.11.19-f005e9d), detail under `data`.
            _send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32000, "message": "Authentication required",
                "data": {"message": "Authentication required. Please run 'agent login' "
                                    "first, then call authenticate() with methodId "
                                    "'cursor_login'."}}})
            return
        SESSION_ID = str(uuid.uuid4())
        _reply(rid, dict(_state(), sessionId=SESSION_ID))
        _update("available_commands_update", availableCommands=COMMANDS)
    elif method == "session/load":
        if UNKNOWN_SESSION:
            _error(rid, -32602, "Chat not found")
            return
        SESSION_ID = str(params.get("sessionId") or "")
        for i in range(REPLAY):
            # A host prepends its context block to a process's first prompt;
            # Cursor replays the prompt as it was sent.
            prefix = ("<fused_render_context>\nbe brief\n</fused_render_context>\n\n"
                      if i == 0 else "")
            _text("user_message_chunk", prefix + "replayed q%d" % (i + 1))
            _update("tool_call", toolCallId="replay-%d-0" % i, title="ls",
                    kind="execute", status="pending", rawInput={"command": "ls"})
            _update("tool_call_update", toolCallId="replay-%d-0" % i,
                    status="completed",
                    content=[{"type": "content", "content": {"type": "text", "text": "x"}}])
            _text("agent_message_chunk", "replayed a%d" % (i + 1))
        _reply(rid, _state())
    elif method == "session/set_mode":
        if params.get("modeId") not in ("agent", "plan", "ask", "code", "architect", "chat"):
            _error(rid, -32603, "Invalid mode ID")
            return
        _mode[0] = params["modeId"]
        _reply(rid, {})
    elif method == "session/set_model":
        mid = str(params.get("modelId") or "")
        if mid not in MODELS:
            _error(rid, -32602, "Invalid model value: %s" % mid)
            return
        _model[0] = mid
        _reply(rid, {})
    elif method == "cursor/list_available_models":
        _reply(rid, {"models": [{"value": m, "name": NAMES[m], "configOptions": []}
                                for m in MODELS]})
    elif rid is not None:
        _error(rid, -32601, "Method not found: " + str(method))


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
    # Like the real server (observed): stdin EOF does NOT end the process —
    # only a signal does. `--exit-on-eof` restores the polite behaviour.
    if EXIT_ON_EOF:
        _prompts.put(None)


def _await(ids):
    with _answered:
        while not all(i in _answers for i in ids) and not _eof.is_set():
            _answered.wait(0.05)


def _tool():
    """Cursor's rows for one shell call: a `tool_call` while the arguments
    still stream (a TRUNCATED command), a refresh with the whole input,
    `in_progress` when it starts, `completed` with its output."""
    _update("tool_call", toolCallId="call-1", title="ls", kind="execute",
            status="pending", rawInput={"command": "ls -"})
    _update("tool_call_update", toolCallId="call-1", title="ls -la",
            rawInput={"command": "ls -la"})
    _update("tool_call_update", toolCallId="call-1", status="in_progress")
    _update("tool_call_update", toolCallId="call-1", status="completed",
            content=[{"type": "content", "content": {"type": "text", "text": "a.txt"}}],
            rawOutput={"exitCode": 0, "stdout": "a.txt"})


_OPTIONS = [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
            {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]


def _edit_new(path):
    """Cursor's uncarded edit of a new file, row for row as observed."""
    tid = "call-edit-1"
    _update("tool_call", toolCallId=tid, title="Edit File", kind="edit",
            status="pending", rawInput={})
    _update("tool_call_update", toolCallId=tid, title="Edit `%s`" % path,
            rawInput={"path": path}, locations=[{"path": path}])
    _update("tool_call_update", toolCallId=tid, status="in_progress")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("hello")
    _update("tool_call_update", toolCallId=tid, status="completed",
            content=[{"type": "diff", "path": path, "oldText": "-- /dev/null",
                      "newText": "++ b/%s\nhello" % path}])


def _ask(n):
    ids = []
    tids = []
    for i in range(n):
        pid = next(_ids)
        ids.append(pid)
        tid = "call-perm-%d" % i
        if PERM_EDIT:
            call = {"toolCallId": tid, "title": "Edit `%s`" % PERM_EDIT, "kind": "edit",
                    "status": "pending",
                    "content": [{"type": "diff", "path": PERM_EDIT,
                                 "oldText": "one\ntwo\n", "newText": "one\n2\n"}]}
            _update("tool_call", toolCallId=tid, title="Edit", kind="edit",
                    status="pending", rawInput={"path": PERM_EDIT})
        else:
            _update("tool_call", toolCallId=tid, title="`rm -rf build`", kind="execute",
                    status="pending", rawInput={"command": "rm -rf build"})
            # The real CLI starts the call, THEN asks (observed).
            _update("tool_call_update", toolCallId=tid, status="in_progress")
            call = {"toolCallId": tid, "title": "`rm -rf build`", "kind": "execute",
                    "status": "pending",
                    "content": [{"type": "content", "content": {
                        "type": "text", "text": "Not in allowlist: rm"}}]}
        tids.append(tid)
        _send({"jsonrpc": "2.0", "id": pid, "method": "session/request_permission",
               "params": {"sessionId": SESSION_ID, "toolCall": call,
                          "options": _OPTIONS}})
    _await(ids)
    for pid, tid in zip(ids, tids):
        answer = (_answers.get(pid) or {}).get("result") or {}
        picked = (answer.get("outcome") or {}).get("optionId")
        if picked == "allow-once":
            _update("tool_call_update", toolCallId=tid, status="completed",
                    content=[{"type": "content", "content": {"type": "text", "text": "ok"}}],
                    rawOutput={"exitCode": 0, "stdout": "ok"})
        else:
            # A rejected call ends BARE: no content, and `completed`.
            _update("tool_call_update", toolCallId=tid, status="completed")


def _question():
    pid = next(_ids)
    _send({"jsonrpc": "2.0", "id": pid, "method": "session/request_permission",
           "params": {"sessionId": SESSION_ID,
                      "toolCall": {"toolCallId": "ask-1", "title": "Which one?",
                                   "kind": "think", "status": "pending"},
                      "options": [{"optionId": "a", "name": "A", "kind": "allow_once"},
                                  {"optionId": "b", "name": "B", "kind": "allow_once"},
                                  {"optionId": "__ask_question_skip__", "name": "Skip",
                                   "kind": "reject_once"}]}})
    _await([pid])


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
    try:
        blocks = params.get("prompt") or []
        text = blocks[-1].get("text", "") if blocks else ""
        if CHECK_ECHO:
            _log({"echo_seen": _echo_seen(text), "text": text})
        if DIE:
            os._exit(3)
        _text("agent_message_chunk", "Hello")
        _text("agent_message_chunk", " world")
        if EXT:
            pid = next(_ids)
            _send({"jsonrpc": "2.0", "id": pid, "method": "cursor/update_todos",
                   "params": {"toolCallId": "t", "todos": []}})
            _await([pid])
        if WITH_TOOL:
            _tool()
        if EDIT_NEW:
            _edit_new(EDIT_NEW)
        if QUESTION:
            _question()
        if PERMS:
            _ask(PERMS)
        if BLOCK_UNTIL:
            while not os.path.exists(BLOCK_UNTIL) and not _eof.is_set() \
                    and not _cancel.is_set():
                time.sleep(0.05)
        if _cancel.is_set():
            _reply(rid, {"stopReason": "cancelled"})
            return
        if WITH_ERROR:
            _error(rid, -32603, "Rate limit exceeded: too many requests")
            return
        _reply(rid, {"stopReason": "end_turn"})
    finally:
        with _flight_lock:
            _in_flight[0] -= 1
            if not _in_flight[0]:
                _cancel.clear()


def _on_sigterm(*_):
    # Logged so a test can tell the polite stop (SIGTERM) from a SIGKILL,
    # which no handler sees.
    _log({"sigterm": True})
    os._exit(143)


def main():
    signal.signal(signal.SIGTERM, _on_sigterm)
    if DUMP_ENV:
        # Atomically: the test waits for the file to EXIST and then parses
        # it, so a plain open("w") let it read the empty file in the gap
        # before json.dump wrote (a CI flake, PR #2383).
        tmp = DUMP_ENV + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(dict(os.environ), fh)
        os.replace(tmp, DUMP_ENV)
    threading.Thread(target=_reader, daemon=True).start()
    while True:
        item = _prompts.get()
        if item is None:
            return 0
        _prompt(*item)


if __name__ == "__main__":
    sys.exit(main())
