#!/usr/bin/env python3
"""A stand-in for `cursor-agent acp`, driven by tests/test_cursor_host.py —
the Cursor twin of fake_gemini_acp.py, with Cursor CLI's own shapes
(https://cursor.com/docs/cli/acp; `initialize` verified against 2026.11.13
signed out).

Speaks just enough ACP for the host to run a session against it: answers
`initialize` (Cursor's `agentCapabilities` and `authMethods`, NO
`agentInfo`), `authenticate`, `session/new` (a uuid4 `sessionId` and
nothing else: the reply's other fields are not verified, so the host must
not need them), `session/load` (replaying one user + one agent chunk per
`--replay N`, with the noise a real replay carries); refuses
`session/set_model` and `session/set_config_option` (the model is a spawn
flag, and the host must never send either) and answers `session/set_mode`
(Cursor's own agent/plan/ask — the host never sends it, a test asserts
that); and on `session/prompt` streams two `agent_message_chunk`s,
optionally a thought, a `tool_call` + `tool_call_update`, extension
traffic (`--ext-notifications`, `--ask-question`, `--create-plan`,
`--unknown-request`), and `--perms N` `session/request_permission`s it
waits on, then replies `end_turn` (no usage anywhere) — or `cancelled` when
a `session/cancel` arrived meanwhile, or a JSON-RPC error with `--error`.
`--block-until PATH` holds the prompt open until the file exists; `--die`
exits mid-prompt without replying.

`--signed-out` is a signed-out Cursor (verified live): `authenticate` is
never answered and `session/new` fails with `-32000` "Authentication
required". `--auth-error` answers `authenticate` itself with that error, and
`--auth-hang` leaves `authenticate` unanswered while `session/new` works.

Everything but a prompt is answered on the READER thread, so a cancel sent
mid-turn is seen at once; prompts run one at a time on the main thread. A
`session/prompt` that arrives while another is still running is logged
`{"overlap": true}` and — as the real CLI does — cancels the first: the
host must never send one (invariant 4).

Every inbound message is appended to `--log PATH` as `{"t": monotonic,
"msg": ...}`, every outbound one as `{"t", "sent": ...}`. `--check-echo OUT`
makes each prompt log `{"echo_seen": bool}` (invariant 3). `--dump-env PATH`
writes this process's environment there at start.

The host passes the request's `argv` through verbatim, so the real flags it
would carry (`--approve-mcps`, `--trust`, `--model`, `acp`) arrive here too
and are ignored. The session id is the fake's own: `session/new` mints a
uuid4 and the replies and updates name it; `session/load` adopts the id it
is asked to load. A permission's options carry Cursor's literal ids
(`allow-once`/`allow-always`/`reject-once`); `--odd-ids` renames them
(`o-once`/`o-always`/`o-no`) so a host that picked by id would fail, and
`--no-options` sends none at all.
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
PERM_EDIT = _flag("--perm-edit")   # the card is an edit of this (relative) path
PERMS = int(_flag("--perms", "1" if ("--perm" in ARGS or PERM_EDIT) else "0"))
WITH_ERROR = "--error" in ARGS
WITH_THOUGHT = "--think" in ARGS
DIE = "--die" in ARGS
SIGNED_OUT = "--signed-out" in ARGS
AUTH_ERROR = "--auth-error" in ARGS
ASK_QUESTION = "--ask-question" in ARGS
CREATE_PLAN = "--create-plan" in ARGS
EXT_NOTIFICATIONS = "--ext-notifications" in ARGS
UNKNOWN_REQUEST = "--unknown-request" in ARGS
ODD_IDS = "--odd-ids" in ARGS
NO_OPTIONS = "--no-options" in ARGS
RAW_INPUT = "--raw-input" in ARGS       # the permission's toolCall carries rawInput
LATE_COMMANDS = "--late-commands" in ARGS
COMMANDS_AFTER_REPLY = "--commands-after-reply" in ARGS
IGNORE_EOF = "--ignore-eof" in ARGS
IGNORE_CANCEL = "--ignore-cancel" in ARGS        # a prompt that never stops
PERM_AFTER_CANCEL = "--perm-after-cancel" in ARGS
REPLAY_GAP = "--replay-gap" in ARGS
#: `session/load` replies first and the history streams after it.
REPLAY_AFTER_REPLY = "--replay-after-reply" in ARGS
#: `authenticate` never answered, but `session/new` works (a slow login check).
AUTH_HANG = "--auth-hang" in ARGS
CHECK_ECHO = _flag("--check-echo")
DUMP_ENV = _flag("--dump-env")
TEXT = _flag("--text", "Hello")
if "--surrogate" in ARGS:
    TEXT += "\ud83d"   # half an emoji: what a chunk boundary can leave
TOOL_KIND = _flag("--tool-kind", "execute")
TOOL_TITLE = _flag("--tool-title", "ls -la")
COMMANDS = [{"name": "help"}, {"name": "model"}]
AUTH_METHODS = [{"id": "cursor_login", "name": "Cursor Login",
                 "description": "Authenticate using existing Cursor login credentials."}]

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


def _error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _send({"jsonrpc": "2.0", "id": rid, "error": err})


def _update(update_kind, **fields):
    body = {"sessionUpdate": update_kind}
    body.update(fields)
    _send({"jsonrpc": "2.0", "method": "session/update",
           "params": {"sessionId": SESSION_ID, "update": body}})


def _text(kind, text):
    _update(kind, content={"type": "text", "text": text})


def _session_reply():
    return {"sessionId": SESSION_ID}


def _load(rid):
    """The replay a real `session/load` streams — prompts, answers, and the
    rest a turn leaves (thoughts, a tool). Before its reply by default;
    AFTER it, spread over a few beats, under `--replay-after-reply`."""
    def stream(pause):
        if REPLAY_GAP:
            _text("user_message_chunk", "gap q0")
            _update("tool_call", toolCallId="gap", title="ls", kind="execute",
                    status="completed", content=[], locations=[])
        for i in range(REPLAY):
            _text("user_message_chunk", "replayed q%d" % (i + 1))
            time.sleep(pause)
            _text("agent_thought_chunk", "thinking about q%d" % (i + 1))
            _update("tool_call", toolCallId="old-%d" % i, title="ls",
                    kind="execute", status="completed", content=[], locations=[])
            _update("tool_call_update", toolCallId="old-%d" % i, status="completed",
                    content=[{"type": "content",
                              "content": {"type": "text", "text": "x"}}])
            time.sleep(pause)
            _text("agent_message_chunk", "replayed a%d" % (i + 1))
    if REPLAY_AFTER_REPLY:
        _reply(rid, _session_reply())
        time.sleep(0.05)
        stream(0.08)
    else:
        stream(0)
        _reply(rid, _session_reply())


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
        _reply(rid, {"protocolVersion": 1,
                     "agentCapabilities": {
                         "loadSession": True,
                         "mcpCapabilities": {"http": True, "sse": True},
                         "promptCapabilities": {"audio": False,
                                                "embeddedContext": False,
                                                "image": True},
                         "sessionCapabilities": {"list": {}}},
                     "authMethods": AUTH_METHODS})
    elif method == "authenticate":
        if SIGNED_OUT or AUTH_HANG:
            return   # signed out, the real CLI never answers
        if AUTH_ERROR:
            _error(rid, -32000, "Authentication required")
            return
        _reply(rid, {})
    elif method == "session/new":
        if SIGNED_OUT:
            _error(rid, -32000, "Authentication required",
                   {"message": "Authentication required. Please run 'agent "
                               "login' first, then call authenticate() with "
                               "methodId 'cursor_login'."})
            return
        SESSION_ID = str(uuid.uuid4())
        if not LATE_COMMANDS:
            _update("available_commands_update", availableCommands=COMMANDS)
        _update("session_info_update", title="A fake session")
        _reply(rid, _session_reply())
        if LATE_COMMANDS:
            time.sleep(0.2)
            _update("available_commands_update", availableCommands=COMMANDS)
    elif method == "session/load":
        SESSION_ID = str(params.get("sessionId") or "")
        _load(rid)
    elif method == "session/set_mode":
        _reply(rid, {})
    elif method in ("session/set_model", "session/set_config_option"):
        _error(rid, -32601, "Method not found: " + method)
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


def _tool_call_fields():
    """A `tool_call`. An execute call also carries `rawInput` here only under
    `--raw-input`; an `mcp` call is title-only (`<tool> (<server> MCP
    Server)`), an `mcp-raw` call is kind `other` with a `rawInput` and a
    `<server>-<tool>` title (Cursor's real MCP shape is unverified, so the
    host takes both)."""
    if TOOL_KIND == "edit":
        return {"title": TOOL_TITLE if TOOL_TITLE != "ls -la" else "Writing to new_file.txt",
                "kind": "edit", "locations": [{"path": "new_file.txt"}],
                "content": [{"type": "diff", "path": "new_file.txt",
                             "oldText": None, "newText": "hi"}]}
    if TOOL_KIND == "mcp":
        return {"title": "app_state (fused_approvals MCP Server)", "kind": "other",
                "locations": [], "content": []}
    if TOOL_KIND == "mcp-raw":
        return {"title": TOOL_TITLE if TOOL_TITLE != "ls -la" else "fused_approvals-app_state",
                "kind": "other", "rawInput": {"selector": "main"},
                "locations": [], "content": []}
    fields = {"title": TOOL_TITLE, "kind": TOOL_KIND, "locations": [], "content": []}
    if RAW_INPUT and TOOL_KIND == "execute":
        fields["rawInput"] = {"command": TOOL_TITLE}
    return fields


def _options():
    if NO_OPTIONS:
        return []
    ids = ("o-always", "o-once", "o-no") if ODD_IDS \
        else ("allow-always", "allow-once", "reject-once")
    return [{"optionId": ids[0], "name": "Allow for this session", "kind": "allow_always"},
            {"optionId": ids[1], "name": "Allow", "kind": "allow_once"},
            {"optionId": ids[2], "name": "Reject", "kind": "reject_once"}]


def _ask(n):
    """Raise `n` permission requests at once and wait for every answer —
    or a cancel, which a real agent answers by giving up on the call."""
    ids = []
    for i in range(n):
        pid = next(_ids)
        ids.append(pid)
        call = {"toolCallId": "call-perm" + ("-%d" % i if i else ""),
                "title": "rm -rf build", "kind": "execute",
                "status": "pending", "locations": [], "content": []}
        if RAW_INPUT:
            call["rawInput"] = {"command": "rm -rf build"}
        if PERM_EDIT:
            call = {"toolCallId": "call-perm", "title": "Writing to " + PERM_EDIT,
                    "kind": "edit", "status": "pending",
                    "locations": [{"path": PERM_EDIT}],
                    "content": [{"type": "diff", "path": PERM_EDIT,
                                 "oldText": "one\ntwo\nthree\n",
                                 "newText": "one\n2\nthree\n"}]}
        _send({"jsonrpc": "2.0", "id": pid, "method": "session/request_permission",
               "params": {"sessionId": SESSION_ID, "toolCall": call,
                          "options": _options()}})
    _wait_for_answers(ids)
    return ids


def _wait_for_answers(ids):
    with _answered:
        while not all(i in _answers for i in ids) and not _eof.is_set():
            _answered.wait(0.05)


def _extensions():
    """Cursor's own traffic on top of ACP: blocking requests the host must
    answer, notifications it must ignore."""
    if EXT_NOTIFICATIONS:
        _send({"jsonrpc": "2.0", "method": "cursor/update_todos",
               "params": {"toolCallId": "t1", "todos": [{"id": "1", "content": "x"}]}})
        _send({"jsonrpc": "2.0", "method": "cursor/task",
               "params": {"toolCallId": "t2", "description": "sub"}})
        _send({"jsonrpc": "2.0", "method": "cursor/generate_image",
               "params": {"toolCallId": "t3", "description": "pic"}})
        _update("some_future_update", content={"type": "text", "text": "ignored"})
    pending = []
    if ASK_QUESTION:
        qid = next(_ids)
        pending.append(qid)
        _send({"jsonrpc": "2.0", "id": qid, "method": "cursor/ask_question",
               "params": {"toolCallId": "q1", "title": "Pick one",
                          "questions": [{"id": "a", "prompt": "Which?",
                                         "options": [{"id": "x", "label": "X"}]}]}})
    if CREATE_PLAN:
        pid = next(_ids)
        pending.append(pid)
        _send({"jsonrpc": "2.0", "id": pid, "method": "cursor/create_plan",
               "params": {"toolCallId": "p1", "name": "plan", "plan": "do it",
                          "todos": []}})
    if UNKNOWN_REQUEST:
        uid = next(_ids)
        pending.append(uid)
        _send({"jsonrpc": "2.0", "id": uid, "method": "cursor/future_request",
               "params": {}})
    if pending:
        _wait_for_answers(pending)


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
        if WITH_THOUGHT:
            _text("agent_thought_chunk", "Let me think")
        _text("agent_message_chunk", TEXT)
        _text("agent_message_chunk", " world")
        if WITH_TOOL:
            _update("tool_call", toolCallId="call-1", status="in_progress",
                    **_tool_call_fields())
            _update("tool_call_update", toolCallId="call-1", status="completed",
                    content=[{"type": "content",
                              "content": {"type": "text", "text": "a.txt\nb.txt"}}])
        _extensions()
        if PERMS:
            _ask(PERMS)
        if BLOCK_UNTIL:
            while not os.path.exists(BLOCK_UNTIL) and not _eof.is_set() \
                    and (IGNORE_CANCEL or not _cancel.is_set()):
                time.sleep(0.05)
        if _cancel.is_set():
            if PERM_AFTER_CANCEL:
                _ask(1)
            _reply(rid, {"stopReason": "cancelled"})
            return
        if WITH_ERROR:
            _error(rid, -32000, "Usage limit reached: out of usage")
            return
        _text("agent_message_chunk", "!")
        _reply(rid, {"stopReason": "end_turn"})
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
