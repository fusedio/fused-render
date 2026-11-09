#!/usr/bin/env python3
"""A stand-in for `opencode acp`, driven by tests/test_opencode_host.py — the
OpenCode twin of fake_gemini_acp.py, with OpenCode's own shapes
(docs/OPENCODE_BACKEND.md, verified on the wire against 1.19.5 where §10
says so).

Speaks just enough ACP for the host to run a session against it: answers
`initialize` (OpenCode's `agentInfo`, `authMethods` and `agentCapabilities`),
`session/new` (a `ses_<id>` `sessionId` plus the `configOptions` list the
real reply carries — a `model` select and a `mode` select, NO `modes` or
`models` blocks), `session/load` (replaying one user + one agent chunk per
`--replay N`, `-32602 "session not found"` under `--unknown-session`),
`session/set_model` and `session/set_mode` (both `{}`), and
`session/set_config_option` (answered with the `configOptions` list as the
real CLI does — the host must never send it, and a test pins that); and on
`session/prompt` echoes the prompt as a `user_message_chunk` (the real CLI
does), re-sends `available_commands_update` (the real CLI does, on every
prompt), streams two `agent_message_chunk`s, with `--tool` a `tool_call`
whose `rawInput` is PARTIAL (`{cwd}` alone, verified) followed by the
`tool_call_update`s that complete it (`in_progress` with the full
`rawInput`, then `completed` with `content` and `rawOutput`), `--perms N`
`session/request_permission`s it waits on (the verified card: `optionId`s
`once` / `always` / `reject`, kinds `allow_once` / `allow_always` /
`reject_once`), a `usage_update {used, size, cost}`, then replies
`{stopReason: "end_turn", usage: {inputTokens, outputTokens, totalTokens},
_meta: {}}` — or `cancelled` when a `session/cancel` arrived meanwhile, or a
JSON-RPC error with `--error`. `--block-until PATH` holds the prompt open
until the file exists; `--die` exits mid-prompt without replying.

Everything but a prompt is answered on the READER thread, so a model switch
or a cancel sent mid-turn is seen at once; prompts run one at a time on the
main thread. A `session/prompt` that arrives while another is still running
is logged `{"overlap": true}` and — as a real ACP agent does — cancels the
first: the host must never send one (invariant 4).

Every inbound message is appended to `--log PATH` as `{"t": monotonic,
"msg": ...}`, every outbound one as `{"t", "sent": ...}`. `--check-echo OUT`
makes each prompt log `{"echo_seen": bool}` (invariant 3). `--dump-env PATH`
writes this process's environment there at start.

The host passes the request's `argv` through verbatim (`opencode acp` for
the real binary), so whatever flags ride along arrive here and are ignored.
The session id is the fake's own: `session/new` mints `ses_<hex>` and the
replies and updates name it; `session/load` adopts the id it is asked to
load. `-m` sets what `configOptions[id=model].currentValue` answers.
"""
import json
import os
import queue
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
SESSION_ID = ""   # minted by session/new, adopted by session/load
BLOCK_UNTIL = _flag("--block-until")
REPLAY = int(_flag("--replay", "1"))
WITH_TOOL = "--tool" in ARGS
#: The verified first row of an execute carries `rawInput: {cwd}` only;
#: `--complete-input` sends the whole input on the `tool_call` instead.
COMPLETE_INPUT = "--complete-input" in ARGS
PERM_EDIT = _flag("--perm-edit")   # the card is an edit of this (relative) path
#: A card for a call NEVER announced by `tool_call` rows (an edge the host
#: must absorb; the race tests drive it in process, where a held tool
#: mapping on the reader thread would otherwise wedge the fake's own card).
PERM_BARE = "--perm-bare" in ARGS
PERMS = int(_flag("--perms", "1" if ("--perm" in ARGS or PERM_EDIT) else "0"))
WITH_ERROR = "--error" in ARGS
DIE = "--die" in ARGS
UNKNOWN_SESSION = "--unknown-session" in ARGS
LATE_COMMANDS = "--late-commands" in ARGS
COMMANDS_AFTER_REPLY = "--commands-after-reply" in ARGS
IGNORE_EOF = "--ignore-eof" in ARGS
IGNORE_CANCEL = "--ignore-cancel" in ARGS        # a prompt that never stops
PERM_AFTER_CANCEL = "--perm-after-cancel" in ARGS
REPLAY_GAP = "--replay-gap" in ARGS
#: Gemini's ordering, which the host absorbs for OpenCode too: `session/load`
#: replies first, the history streams after it.
REPLAY_AFTER_REPLY = "--replay-after-reply" in ARGS
CHECK_ECHO = _flag("--check-echo")
DUMP_ENV = _flag("--dump-env")
TEXT = _flag("--text", "Hello")
if "--surrogate" in ARGS:
    TEXT += "\ud83d"   # half an emoji: what a chunk boundary can leave
TOOL_KIND = _flag("--tool-kind", "execute")
TOOL_TITLE = _flag("--tool-title", "ls -la")
MODEL = _flag("-m", "anthropic/claude-sonnet-5-5")   # `currentValue` of the model select
COMMANDS = [{"name": "help", "description": "Show help"},
            {"name": "init", "description": "Create AGENTS.md"}]
MODEL_OPTIONS = [{"value": "anthropic/claude-sonnet-5-5", "name": "Anthropic/Claude Sonnet 5.5"},
                 {"value": "anthropic/claude-opus-5-5", "name": "Anthropic/Claude Opus 5.5"},
                 {"value": "openai/gpt-5.5", "name": "OpenAI/GPT-5.5"},
                 {"value": "fused/m1", "name": "Fused Render (local)/m1"}]
MODE_OPTIONS = [{"value": "build", "name": "Build"}, {"value": "plan", "name": "Plan"}]

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
_msg_ids = iter(range(1, 10**6))
_mode = ["build"]
_model = [MODEL]
_commands = [list(COMMANDS)]   # what the next `available_commands_update` says


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


def _text(kind, text, message_id=None):
    _update(kind, messageId=message_id or ("msg_%06d" % next(_msg_ids)),
            content={"type": "text", "text": text})


def _config_options():
    """The verified `session/new` reply body: a `model` select (the
    provider-wide list, `currentValue` the session's model) and a `mode`
    select (OpenCode's AGENT: `build` / `plan`)."""
    return [{"id": "model", "name": "Model", "category": "model", "type": "select",
             "currentValue": _model[0], "options": MODEL_OPTIONS},
            {"id": "mode", "name": "Mode", "category": "mode", "type": "select",
             "currentValue": _mode[0], "options": MODE_OPTIONS}]


def _load(rid):
    """The replay a real `session/load` streams — prompts and answers as
    `user_message_chunk` / `agent_message_chunk`, plus the rest a turn
    leaves (a tool, usage). Before its reply by default; AFTER it, spread
    over a few beats, under `--replay-after-reply`."""
    def stream(pause):
        if REPLAY_GAP:
            _text("user_message_chunk", "gap q0")
            _update("tool_call", toolCallId="gap", title="ls", kind="execute",
                    status="completed", content=[], locations=[],
                    rawInput={"command": "ls"})
        for i in range(REPLAY):
            _text("user_message_chunk", "replayed q%d" % (i + 1))
            time.sleep(pause)
            _update("tool_call", toolCallId="old-%d" % i, title="ls",
                    kind="execute", status="completed", content=[], locations=[],
                    rawInput={"command": "ls"})
            _update("tool_call_update", toolCallId="old-%d" % i, status="completed",
                    content=[{"type": "content",
                              "content": {"type": "text", "text": "x"}}])
            time.sleep(pause)
            _text("agent_message_chunk", "replayed a%d" % (i + 1))
        _update("usage_update", used=50, size=200000, cost={"amount": 0, "currency": "USD"})
    if REPLAY_AFTER_REPLY:
        _reply(rid, {"configOptions": _config_options()})
        time.sleep(0.05)
        stream(0.08)
    else:
        stream(0)
        _reply(rid, {"configOptions": _config_options()})


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
        # Verified 1.19.5 reply.
        _reply(rid, {"protocolVersion": 1,
                     "agentCapabilities": {
                         "loadSession": True,
                         "mcpCapabilities": {"http": True, "sse": True},
                         "promptCapabilities": {"embeddedContext": True, "image": True},
                         "sessionCapabilities": {"close": {}, "fork": {}, "list": {},
                                                 "resume": {}}},
                     "authMethods": [{"id": "opencode-login", "name": "Login with opencode",
                                      "description": "Run `opencode auth login` in the terminal"}],
                     "agentInfo": {"name": "OpenCode", "version": "1.19.5"}})
    elif method == "session/new":
        SESSION_ID = "ses_" + uuid.uuid4().hex[:26]
        if not LATE_COMMANDS:
            _update("available_commands_update", availableCommands=COMMANDS)
        _reply(rid, {"sessionId": SESSION_ID, "configOptions": _config_options()})
        if LATE_COMMANDS:
            time.sleep(0.2)
            _update("available_commands_update", availableCommands=COMMANDS)
    elif method == "session/load":
        if UNKNOWN_SESSION:
            _error(rid, -32602, "session not found")   # verified wording
            return
        SESSION_ID = str(params.get("sessionId") or "")
        _update("available_commands_update", availableCommands=COMMANDS)
        _load(rid)
    elif method == "session/set_mode":
        mode_id = params.get("modeId")
        if mode_id not in [m["value"] for m in MODE_OPTIONS]:
            _error(rid, -32602, "unknown mode: %s" % mode_id)
            return
        _mode[0] = mode_id
        _reply(rid, {})
    elif method == "session/set_model":
        _model[0] = str(params.get("modelId") or "")
        _update("config_option_update", configOptions=_config_options())
        _reply(rid, {})
    elif method == "session/set_config_option":
        # Served by the real CLI (verified) — and never to be sent by the
        # host, which the test reads off the log.
        if params.get("configId") == "model":
            _model[0] = str(params.get("value") or "")
        elif params.get("configId") == "mode":
            _mode[0] = str(params.get("value") or "")
        _reply(rid, {"configOptions": _config_options()})
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


def _tool_rows(cwd):
    """OpenCode's rows for one tool call, in the verified order: a
    `tool_call` (partial `rawInput`), an `in_progress` update carrying the
    whole input, a `completed` update with `content` and `rawOutput`."""
    if TOOL_KIND == "edit":
        full = {"filePath": "new_file.txt", "content": "hi"}
        title = TOOL_TITLE if TOOL_TITLE != "ls -la" else "new_file.txt"
        return ([{"toolCallId": "call-1", "title": "write", "kind": "edit",
                  "status": "pending", "locations": [{"path": "new_file.txt"}],
                  "rawInput": full if COMPLETE_INPUT else {}},
                 {"toolCallId": "call-1", "status": "in_progress", "kind": "edit",
                  "title": title, "locations": [{"path": "new_file.txt"}],
                  "rawInput": full}],
                {"toolCallId": "call-1", "status": "completed", "title": title,
                 "content": [{"type": "content",
                              "content": {"type": "text", "text": "Wrote new_file.txt"}}],
                 "rawOutput": {"output": "Wrote new_file.txt", "metadata": {}}})
    if TOOL_KIND == "mcp":
        return ([{"toolCallId": "call-1", "title": "fused_approvals_app_state",
                  "kind": "other", "status": "pending", "locations": [],
                  "rawInput": {"reason": "look"}}],
                {"toolCallId": "call-1", "status": "completed",
                 "title": "fused_approvals_app_state",
                 "content": [{"type": "content",
                              "content": {"type": "text", "text": "{}"}}]})
    full = {"command": TOOL_TITLE, "description": "list files", "cwd": cwd}
    return ([{"toolCallId": "call-1", "title": "bash" if not COMPLETE_INPUT else TOOL_TITLE,
              "kind": TOOL_KIND, "status": "pending",
              "locations": [{"path": cwd}],
              "rawInput": full if COMPLETE_INPUT else {"cwd": cwd}},
             {"toolCallId": "call-1", "status": "in_progress", "kind": TOOL_KIND,
              "title": TOOL_TITLE, "locations": [{"path": cwd}], "rawInput": full}],
            {"toolCallId": "call-1", "status": "completed", "title": TOOL_TITLE,
             "content": [{"type": "content",
                          "content": {"type": "text", "text": "a.txt\nb.txt"}}],
             "rawOutput": {"output": "a.txt\nb.txt",
                           "metadata": {"output": "a.txt\nb.txt", "exit": 0,
                                        "truncated": False}}})


def _ask(n):
    """Raise `n` permission requests at once and wait for every answer —
    or a cancel, which a real agent answers by giving up on the call."""
    ids = []
    for i in range(n):
        pid = next(_ids)
        ids.append(pid)
        tid = "call-perm" + ("-%d" % i if i else "")
        # The verified card: `kind`, `title` (the command line), `locations`
        # empty, `rawInput` {command} ALONE — the description came on the
        # `tool_call` rows, which the host merges by id.
        if not PERM_BARE and not PERM_EDIT:
            _update("tool_call", toolCallId=tid, title="bash", kind="execute",
                    status="pending", locations=[], rawInput={"cwd": os.getcwd()})
            _update("tool_call_update", toolCallId=tid, status="in_progress",
                    kind="execute", title="rm -rf build", locations=[],
                    rawInput={"command": "rm -rf build", "description": "clean the build dir",
                              "cwd": os.getcwd()})
        call = {"toolCallId": tid, "title": "rm -rf build", "kind": "execute",
                "status": "pending", "locations": [],
                "rawInput": {"command": "rm -rf build"}}
        if PERM_EDIT:
            # An edit card: the path in `rawInput.filePath` and `locations`,
            # the change as oldString/newString (assumed from the edit
            # tool's schema) and as an ACP `diff` content entry.
            call = {"toolCallId": "call-perm", "title": PERM_EDIT,
                    "kind": "edit", "status": "pending",
                    "locations": [{"path": PERM_EDIT}],
                    "rawInput": {"filePath": PERM_EDIT, "oldString": "two\n",
                                 "newString": "2\n"},
                    "content": [{"type": "diff", "path": PERM_EDIT,
                                 "oldText": "one\ntwo\nthree\n",
                                 "newText": "one\n2\nthree\n"}]}
        _send({"jsonrpc": "2.0", "id": pid, "method": "session/request_permission",
               "params": {"sessionId": SESSION_ID,
                          "toolCall": call,
                          "options": [
                              {"optionId": "once", "kind": "allow_once",
                               "name": "Allow once"},
                              {"optionId": "always", "kind": "allow_always",
                               "name": "Always allow"},
                              {"optionId": "reject", "kind": "reject_once",
                               "name": "Reject"}]}})
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
        text = "".join(b.get("text", "") for b in params.get("prompt") or [])
        if CHECK_ECHO:
            _log({"echo_seen": _echo_seen(text), "text": text})
        if DIE:
            os._exit(3)
        # The real CLI echoes the prompt back and re-announces its commands
        # on every prompt: both must reach no row.
        _text("user_message_chunk", text)
        _update("available_commands_update", availableCommands=_commands[0])
        mid = "msg_%06d" % next(_msg_ids)
        _text("agent_message_chunk", TEXT, mid)
        _text("agent_message_chunk", " world", mid)
        if WITH_TOOL:
            rows, done = _tool_rows(os.getcwd())
            _update("tool_call", **rows[0])
            for row in rows[1:]:
                _update("tool_call_update", **row)
            _update("tool_call_update", **done)
        if PERMS:
            _ask(PERMS)
        if BLOCK_UNTIL:
            while not os.path.exists(BLOCK_UNTIL) and not _eof.is_set() \
                    and (IGNORE_CANCEL or not _cancel.is_set()):
                time.sleep(0.05)
        if _cancel.is_set():
            if PERM_AFTER_CANCEL:
                _ask(1)
            _reply(rid, {"stopReason": "cancelled", "_meta": {}})
            return
        if WITH_ERROR:
            _error(rid, -32000, "Rate limit exceeded: too many requests")
            return
        _text("agent_message_chunk", "!", mid)
        # OpenCode's token report: one `usage_update {used, size, cost}` at
        # the reply's end, AND a `usage` block on the prompt's reply.
        _update("usage_update", used=1234, size=200000,
                cost={"amount": 0.0012, "currency": "USD"})
        _reply(rid, {"stopReason": "end_turn",
                     "usage": {"inputTokens": 1234, "outputTokens": 7, "totalTokens": 1241},
                     "_meta": {}})
        if COMMANDS_AFTER_REPLY:
            _commands[0] = COMMANDS + [{"name": "after"}]
            _update("available_commands_update", availableCommands=_commands[0])
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
