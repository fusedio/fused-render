#!/usr/bin/env python3
"""A stand-in for `gemini --acp`, driven by tests/test_gemini_host.py — the
Gemini twin of fake_copilot_acp.py, with Gemini CLI's own shapes
(docs/GEMINI_BACKEND.md, verified against 0.65.0 where §10 says so).

Speaks just enough ACP for the host to run a session against it: answers
`initialize` (with Gemini's `agentInfo` and `authMethods`), `session/new`
(a uuid4 `sessionId` plus the `modes` and `models` blocks the real reply
carries), `session/load` (replaying one user + one agent chunk per
`--replay N`, with the noise a real replay carries), `session/set_mode`
(Gemini's ids only: `default`, `autoEdit`, `yolo` — anything else is the
CLI's "Invalid or unavailable mode" error) and `session/set_model`; refuses
`session/set_config_option` (the host must never send Copilot's switch);
and on `session/prompt` streams two `agent_message_chunk`s, optionally a
thought, a `tool_call` + `tool_call_update` WITHOUT `rawInput` (Gemini sends
title, kind, locations and content only), and `--perms N`
`session/request_permission`s it waits on, then replies `end_turn` (no
`usage`: Gemini reports tokens as `usage_update` notifications) — or
`cancelled` when a `session/cancel` arrived meanwhile, or a JSON-RPC error
with `--error`. `--block-until PATH` holds the prompt open until the file
exists; `--die` exits mid-prompt without replying; `--signed-out` refuses
`session/new` the way a signed-out Gemini does (verified live: `-32000`
"Gemini API key is missing or not configured."); `--oauth-prompt` does what
a Google login with a dead token does under NO_BROWSER: prints the flow's
"Please visit the following URL…" lines on STDOUT and never answers.

Everything but a prompt is answered on the READER thread, so a mode switch
or a cancel sent mid-turn is seen at once; prompts run one at a time on the
main thread. A `session/prompt` that arrives while another is still running
is logged `{"overlap": true}` and — as the real CLI does — cancels the first:
the host must never send one (invariant 4).

Every inbound message is appended to `--log PATH` as `{"t": monotonic,
"msg": ...}`, every outbound one as `{"t", "sent": ...}`. `--check-echo OUT`
makes each prompt log `{"echo_seen": bool}` (invariant 3). `--dump-env PATH`
writes this process's environment there at start.

The host passes the request's `argv` through verbatim, so the real `gemini`
flags it would carry (`--acp`, `--skip-trust`, `-m`, `--approval-mode`,
`--include-directories`, ...) arrive here too and are ignored. The session
id is the fake's own: `session/new` mints a uuid4 and the replies and
updates name it; `session/load` adopts the id it is asked to load.
Permission option ids deliberately differ from their kinds
(`o-once`/`o-always`/`o-no`) so a host that picked by id would fail the test.
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
OAUTH_PROMPT = "--oauth-prompt" in ARGS
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
TOOL_TITLE = _flag("--tool-title", "ls -la (list files)")
MODEL = _flag("-m", "auto")   # what `models.currentModelId` answers
COMMANDS = [{"name": "help"}, {"name": "model"}]
MODES = [{"id": "default", "name": "Default", "description": "Prompts for approval"},
         {"id": "autoEdit", "name": "Auto Edit", "description": "Auto-approves edit tools"},
         {"id": "yolo", "name": "YOLO", "description": "Auto-approves all tools"}]
MODELS = [{"modelId": "auto", "name": "Auto", "description": "Gemini picks"},
          {"modelId": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
          {"modelId": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"}]

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
_mode = ["default"]
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


def _print(text):
    """The CLI's own prose on stdout (the OAuth flow's lines)."""
    with _out_lock:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


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


def _session_blocks():
    return {"modes": {"availableModes": MODES, "currentModeId": _mode[0]},
            "models": {"availableModels": MODELS, "currentModelId": _model[0]}}


def _load(rid):
    """The replay a real `session/load` streams before its reply — prompts,
    answers, and the rest a turn leaves (thoughts, a tool, usage)."""
    if REPLAY_GAP:
        _text("user_message_chunk", "gap q0")
        _update("tool_call", toolCallId="gap", title="ls (list)", kind="execute",
                status="completed", content=[], locations=[])
    for i in range(REPLAY):
        _text("user_message_chunk", "replayed q%d" % (i + 1))
        _text("agent_thought_chunk", "thinking about q%d" % (i + 1))
        _update("tool_call", toolCallId="old-%d" % i, title="ls (list)",
                kind="execute", status="completed", content=[], locations=[])
        _update("tool_call_update", toolCallId="old-%d" % i, status="completed",
                content=[{"type": "content",
                          "content": {"type": "text", "text": "x"}}])
        _text("agent_message_chunk", "replayed a%d" % (i + 1))
    _update("usage_update", used=50, size=1048576)
    _reply(rid, _session_blocks())


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
                     "authMethods": [{"id": "oauth-personal", "name": "Log in with Google"},
                                     {"id": "gemini-api-key", "name": "Gemini API key"}],
                     "agentInfo": {"name": "gemini-cli", "title": "Gemini CLI",
                                   "version": "0.65.0"},
                     "agentCapabilities": {"loadSession": True}})
    elif method == "session/new":
        if SIGNED_OUT:
            _error(rid, -32000, "Gemini API key is missing or not configured.")
            return
        if OAUTH_PROMPT:
            # The web flow under NO_BROWSER: a prompt on stdout, then a wait
            # on stdin for a code nobody will type — no reply, ever.
            _print("Please visit the following URL to authorize the application:")
            _print("")
            _print("https://accounts.google.com/o/oauth2/v2/auth?state=XYZ&code_challenge=abc")
            _print("")
            _print("Enter the authorization code: ")
            return
        SESSION_ID = str(uuid.uuid4())
        if not LATE_COMMANDS:
            _update("available_commands_update", availableCommands=COMMANDS)
        _update("session_info_update", title="A fake session")
        _reply(rid, {"sessionId": SESSION_ID, **_session_blocks()})
        if LATE_COMMANDS:
            time.sleep(0.2)
            _update("available_commands_update", availableCommands=COMMANDS)
    elif method == "session/load":
        SESSION_ID = str(params.get("sessionId") or "")
        _load(rid)
    elif method == "session/set_mode":
        mode_id = params.get("modeId")
        if mode_id not in [m["id"] for m in MODES]:
            _error(rid, -32602, "Invalid or unavailable mode: %s" % mode_id)
            return
        _mode[0] = mode_id
        _update("current_mode_update", currentModeId=mode_id)
        _reply(rid, {})
    elif method == "session/set_model":
        _model[0] = str(params.get("modelId") or "")
        _reply(rid, {})
    elif method == "session/set_config_option":
        _error(rid, -32601, "Method not found: session/set_config_option")
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
    """Gemini's `tool_call`: title, kind, locations, content — no rawInput."""
    if TOOL_KIND == "edit":
        return {"title": TOOL_TITLE if TOOL_TITLE != "ls -la (list files)" else "Writing to new_file.txt",
                "kind": "edit", "locations": [{"path": "new_file.txt"}],
                "content": [{"type": "diff", "path": "new_file.txt",
                             "oldText": None, "newText": "hi"}]}
    if TOOL_KIND == "mcp":
        return {"title": "app_state (fused_approvals MCP Server)", "kind": "other",
                "locations": [], "content": []}
    return {"title": TOOL_TITLE, "kind": TOOL_KIND, "locations": [], "content": []}


def _ask(n):
    """Raise `n` permission requests at once and wait for every answer —
    or a cancel, which a real agent answers by giving up on the call."""
    ids = []
    for i in range(n):
        pid = next(_ids)
        ids.append(pid)
        call = {"toolCallId": "call-perm" + ("-%d" % i if i else ""),
                "title": "rm -rf build (clean the build dir)", "kind": "execute",
                "status": "pending", "locations": [], "content": []}
        if PERM_EDIT:
            # How Gemini sends an edit: the change as an ACP `diff` content
            # entry, the path in `locations`, no rawInput anywhere.
            call = {"toolCallId": "call-perm", "title": "Writing to " + PERM_EDIT,
                    "kind": "edit", "status": "pending",
                    "locations": [{"path": PERM_EDIT}],
                    "content": [{"type": "diff", "path": PERM_EDIT,
                                 "oldText": "one\ntwo\nthree\n",
                                 "newText": "one\n2\nthree\n"}]}
        _send({"jsonrpc": "2.0", "id": pid, "method": "session/request_permission",
               "params": {"sessionId": SESSION_ID,
                          "toolCall": call,
                          "options": [
                              {"optionId": "o-always", "name": "Allow for this session",
                               "kind": "allow_always"},
                              {"optionId": "o-once", "name": "Allow",
                               "kind": "allow_once"},
                              {"optionId": "o-no", "name": "Reject",
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
            _error(rid, -32000, "RESOURCE_EXHAUSTED: quota exceeded")
            return
        _text("agent_message_chunk", "!")
        # Gemini's token report: one `usage_update` per model reply, at the
        # end — never a `usage` block on the prompt's reply.
        _update("usage_update", used=1234, size=1048576)
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
