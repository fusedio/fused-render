#!/usr/bin/env python3
"""A stand-in for `codex app-server`, driven by tests/test_codex_host.py.

The `fake_copilot_acp.py` twin for Codex's wire (docs/CODEX_BACKEND.md §3):
newline-delimited JSON-RPC 2.0 over stdio, shaped after the frames captured
from Codex CLI 0.163.0 on 2026-10-21. Speaks just enough of it for the host
to run a session: `initialize` (then expects the `initialized`
notification), `thread/start` (mints a uuid7-shaped thread id), `thread/resume`
(its reply's `thread.turns[]` carries `--replay N` turns, oldest first, each
`userMessage` + `reasoning` + `commandExecution` + `agentMessage`, with a
`thread/tokenUsage/updated` AFTER the reply as the real server sends it),
`turn/start` (answered at once with `{turn: {id, status: inProgress}}`, the
turn then streamed as notifications on the main thread) and `turn/interrupt`
(answered `{}`, then `turn/completed status: interrupted`).

A turn: `turn/started`, our own `userMessage` item, a `thread/tokenUsage/
updated` (with `modelContextWindow`), optionally a `reasoning` item streamed
as `item/reasoning/summaryTextDelta` (`--think`), an `agentMessage` streamed
as `item/agentMessage/delta`s ("Hello", " world"), then the tool items asked
for, then — after `--block-until PATH` exists, if given — "!" and
`turn/completed status: completed`. With tool items the first message is
completed before them ("Hello world") and the "!" is a second one; without,
the one message is "Hello world!". An interrupted turn leaves its open
message uncompleted, as the real server does (verified).

Tool items (each `item/started` → maybe an approval → `item/completed`):
  --tool            one `commandExecution` (`ls -la`); asks
                    `item/commandExecution/requestApproval` unless the turn's
                    `approvalPolicy` is `never` or the command was accepted
                    `acceptForSession` earlier in this server's life
  --perms N         N such commands, every approval raised at once
  --edit PATH       one `fileChange` of PATH (cwd-relative; `update` when it
                    exists, else `add`), with the path absolute as Codex sends
                    it; asks `item/fileChange/requestApproval` only under a
                    `readOnly` sandbox (verified) and a policy other than
                    `never` — `workspaceWrite` applies it unasked, which is
                    what fused's acceptEdits is
  --mcp             one `mcpToolCall` (fused_approvals/app_state), no approval
  --web             one `webSearch`, no approval
An approval answered `accept`/`acceptForSession` completes the item
`completed`; `decline` completes it `declined`; `cancel` completes it
`declined` AND interrupts the turn (the v2 enum's documented meaning). Any
other answer — a v1 word like `approved` — reads as a decline, as the real
server reads it (verified the hard way). Approval request ids count from 0,
like the real server's.

Failure roads:
  --error           `error` notification + `turn/completed status: failed`
                    carrying `turn.error.message`
  --error-notice    the same, but the failed turn's own `error` is null: the
                    words are only on the `error` notification
  --refuse-turn     `turn/start` answered with a JSON-RPC error (no turn)
  --die             exit 3 mid-turn, after the `turn/start` reply
  --signed-out      `thread/start` refused -32000 "Authentication required"
                    (the brief's assumed shape — NOT what 0.163.0 does)
  --signed-out-turn what 0.163.0 does signed out (measured 2026-10-21):
                    `thread/start` succeeds, and the turn fails on the 401 —
                    `error` + `turn/completed failed` (assumed wording)
  --ignore-interrupt `turn/interrupt` answered `{}`, the turn never stops
  --perm-after-interrupt an interrupted turn asks once more while winding down
  --ignore-eof      keep running after stdin closes
  --exit-at-start   exit 2 with a usage error before reading anything

Bookkeeping, as fake_copilot_acp.py: every inbound message is appended to
`--log PATH` as `{"t": monotonic, "msg": ...}`, every outbound one as `{"t",
"sent": ...}`; a `turn/start` arriving while a turn is still running is
logged `{"overlap": true}` (invariant 4); a request other than `initialize`
before the `initialized` notification is logged `{"early": method}` and
REFUSED — stricter than 0.163.0, which was measured answering one anyway, so
a host that forgot `initialized` fails loudly here (invariant 13).
`--check-echo OUT` logs `{"echo_seen": bool}` per turn: whether OUT already
held that prompt's user echo when `turn/start` arrived (invariant 3).
`--dump-env PATH` writes this process's environment there at start. `--text`
replaces "Hello"; `--surrogate` appends half an emoji to it.
"""
import json
import os
import queue
import sys
import threading
import time

ARGS = sys.argv[1:]
if "--exit-at-start" in ARGS:
    # A CLI that refuses its argv (a bad `-c` override): a usage error on
    # stderr and exit 2, before reading a byte.
    sys.stderr.write("error: invalid -c override\n")
    sys.exit(2)


def _flag(name, default=None):
    if name in ARGS:
        i = ARGS.index(name)
        return ARGS[i + 1] if i + 1 < len(ARGS) else default
    return default


LOG = _flag("--log")
BLOCK_UNTIL = _flag("--block-until")
REPLAY = int(_flag("--replay", "1"))
WITH_TOOL = "--tool" in ARGS
PERMS = int(_flag("--perms", "0"))
EDIT = _flag("--edit")
WITH_MCP = "--mcp" in ARGS
WITH_WEB = "--web" in ARGS
WITH_ERROR = "--error" in ARGS
ERROR_NOTICE = "--error-notice" in ARGS
REFUSE_TURN = "--refuse-turn" in ARGS
WITH_THOUGHT = "--think" in ARGS
DIE = "--die" in ARGS
SIGNED_OUT = "--signed-out" in ARGS
SIGNED_OUT_TURN = "--signed-out-turn" in ARGS
IGNORE_INTERRUPT = "--ignore-interrupt" in ARGS
PERM_AFTER_INTERRUPT = "--perm-after-interrupt" in ARGS
IGNORE_EOF = "--ignore-eof" in ARGS
CHECK_ECHO = _flag("--check-echo")
DUMP_ENV = _flag("--dump-env")
TEXT = _flag("--text", "Hello")
if "--surrogate" in ARGS:
    TEXT += "\ud83d"   # half an emoji: what a delta boundary can leave
DEFAULT_MODEL = "gpt-6-luna"
WINDOW = 258400

THREAD_ID = ""
_initialized = threading.Event()
_turns = queue.Queue()
_answers = {}                 # our request id -> the host's reply
_answered = threading.Condition()
_interrupt = threading.Event()
_eof = threading.Event()
_in_flight = [0]
_current_turn = [""]
_turn_count = [0]
_flight_lock = threading.Lock()
_out_lock = threading.Lock()
_log_lock = threading.Lock()
_req_ids = iter(range(0, 10**6))   # the real server's first request id is 0
_session_ok = set()           # commands/paths accepted for the session


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
    _send({"id": rid, "result": result})


def _error(rid, code, message):
    _send({"id": rid, "error": {"code": code, "message": message}})


def _notify(method, **params):
    _send({"method": method, "params": params,
           "emittedAtMs": int(time.time() * 1000)})


def _uuid7() -> str:
    """A uuid7-shaped id, as Codex mints thread and turn ids."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = int.from_bytes(os.urandom(10), "big")
    return "%08x-%04x-7%03x-%04x-%012x" % (
        ms >> 16, ms & 0xFFFF, (rnd >> 68) & 0xFFF,
        0x8000 | ((rnd >> 48) & 0x3FFF), rnd & ((1 << 48) - 1))


def _thread(turns=()):
    return {"id": THREAD_ID, "sessionId": THREAD_ID, "cwd": os.getcwd(),
            "model": DEFAULT_MODEL, "modelProvider": "openai",
            "status": {"type": "idle"}, "cliVersion": "0.163.0",
            "source": "vscode", "name": None, "turns": list(turns)}


def _usage(window=True):
    last = {"totalTokens": 1241, "inputTokens": 1234, "cachedInputTokens": 100,
            "cacheWriteInputTokens": 0, "outputTokens": 7,
            "reasoningOutputTokens": 0}
    _notify("thread/tokenUsage/updated", threadId=THREAD_ID,
            turnId=_current_turn[0],
            tokenUsage={"total": dict(last), "last": last,
                        "modelContextWindow": WINDOW if window else None})


def _replay_turns():
    turns = []
    for i in range(REPLAY):
        turns.append({
            "id": _uuid7(), "status": "completed", "error": None,
            "itemsView": "full",
            "items": [
                {"type": "userMessage", "id": _uuid7(), "clientId": None,
                 "content": [{"type": "text", "text": "replayed q%d" % (i + 1),
                              "text_elements": []}]},
                {"type": "reasoning", "id": "rs-%d" % i, "summary": ["hmm"],
                 "content": []},
                {"type": "commandExecution", "id": "exec-old-%d" % i,
                 "command": "/bin/bash -lc ls", "cwd": os.getcwd(),
                 "commandActions": [{"type": "unknown", "command": "ls"}],
                 "status": "completed", "aggregatedOutput": "x", "exitCode": 0},
                {"type": "agentMessage", "id": "msg-old-%d" % i,
                 "text": "replayed a%d" % (i + 1), "phase": "final_answer"},
            ]})
    return turns


def _serve(msg):
    """One inbound message, on the reader thread."""
    global THREAD_ID
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if method is None:
        with _answered:
            _answers[rid] = msg
            _answered.notify_all()
        return
    if method == "initialized":
        _initialized.set()
        return
    if method == "initialize":
        _reply(rid, {"userAgent": "fused-render/0.163.0 (fake)",
                     "codexHome": os.environ.get("CODEX_HOME", ""),
                     "platformFamily": "unix", "platformOs": "linux"})
        _notify("configWarning", summary="a warning nobody needs", details=None)
        return
    if rid is not None and not _initialized.is_set():
        _log({"early": method})
        _error(rid, -32002, "Not initialized")
        return
    if method == "thread/start":
        if SIGNED_OUT:
            _error(rid, -32000, "Authentication required")
            return
        THREAD_ID = _uuid7()
        _notify("mcpServer/startupStatus/updated", threadId=THREAD_ID,
                name="codex_apps", status="starting")
        result = {"thread": _thread(), "cwd": os.getcwd(),
                  "model": params.get("model") or DEFAULT_MODEL,
                  "approvalPolicy": params.get("approvalPolicy"),
                  "sandbox": params.get("sandbox")}
        result["thread"]["model"] = result["model"]
        _reply(rid, result)
        _notify("thread/started", thread=_thread())
    elif method == "thread/resume":
        THREAD_ID = str(params.get("threadId") or "")
        _reply(rid, {"thread": _thread(_replay_turns()), "model": DEFAULT_MODEL,
                     "cwd": os.getcwd(), "approvalPolicy": params.get("approvalPolicy"),
                     "sandbox": params.get("sandbox")})
        # Verified order: the resumed thread's usage lands AFTER the reply,
        # outside any turn.
        _usage()
    elif method == "turn/start":
        with _flight_lock:
            if _in_flight[0]:
                _log({"overlap": True})
            _in_flight[0] += 1
        if REFUSE_TURN:
            with _flight_lock:
                _in_flight[0] -= 1
            _error(rid, -32600, "model not supported: %s" % params.get("model"))
            return
        turn_id = _uuid7()
        _current_turn[0] = turn_id
        _interrupt.clear()
        _reply(rid, {"turn": {"id": turn_id, "items": [], "itemsView": "notLoaded",
                              "status": "inProgress", "error": None}})
        _turns.put((turn_id, params))
    elif method == "turn/interrupt":
        _reply(rid, {})
        if not IGNORE_INTERRUPT and params.get("turnId") == _current_turn[0]:
            _interrupt.set()
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
        _turns.put(None)


def _ask(requests):
    """Send every `(method, params)` approval request at once, wait for all
    the answers (or EOF), and return each one's decision string."""
    ids = []
    for method, params in requests:
        pid = next(_req_ids)
        ids.append(pid)
        _send({"id": pid, "method": method, "params": params})
    with _answered:
        while not all(i in _answers for i in ids) and not _eof.is_set():
            _answered.wait(0.05)
    out = []
    for i in ids:
        res = (_answers.get(i) or {}).get("result")
        out.append(res.get("decision") if isinstance(res, dict) else None)
        _notify("serverRequest/resolved", threadId=THREAD_ID, requestId=i)
    return out


def _item(method, turn_id, item):
    _notify(method, item=item, threadId=THREAD_ID, turnId=turn_id)


def _settle(decision):
    """The item status an approval answer leads to, per the v2 enum."""
    if decision in ("accept", "acceptForSession"):
        return "completed"
    if decision == "cancel":
        _interrupt.set()
    return "declined"


def _commands(turn_id, policy, n):
    items = []
    for i in range(n):
        items.append({"type": "commandExecution", "id": "exec-%d-%s" % (i, turn_id[-6:]),
                      "command": "/bin/bash -lc 'ls -la'", "cwd": os.getcwd(),
                      "processId": None, "source": "agent", "status": "inProgress",
                      "commandActions": [{"type": "listFiles", "command": "ls -la"}],
                      "aggregatedOutput": None, "exitCode": None, "durationMs": None})
    for it in items:
        _item("item/started", turn_id, it)
    asks = [it for it in items
            if policy != "never" and it["command"] not in _session_ok]
    decisions = _ask([("item/commandExecution/requestApproval", {
        "threadId": THREAD_ID, "turnId": turn_id, "itemId": it["id"],
        "command": it["command"], "cwd": it["cwd"],
        "commandActions": it["commandActions"],
        "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"]})
        for it in asks]) if asks else []
    verdict = dict(zip([it["id"] for it in asks], decisions))
    for it in items:
        decision = verdict.get(it["id"], "accept")
        if decision == "acceptForSession":
            _session_ok.add(it["command"])
        status = _settle(decision)
        done = dict(it, status=status, exitCode=0 if status == "completed" else None,
                    aggregatedOutput="a.txt\nb.txt" if status == "completed" else None)
        _item("item/completed", turn_id, done)


def _edit(turn_id, policy, sandbox):
    path = os.path.abspath(EDIT)
    exists = os.path.exists(path)
    change = {"path": path, "kind": {"type": "update", "move_path": None} if exists
              else {"type": "add"},
              "diff": "@@ -1,3 +1,3 @@\n one\n-two\n+2\n three\n" if exists else "hi\n"}
    item = {"type": "fileChange", "id": "patch-" + turn_id[-6:],
            "changes": [change], "status": "inProgress"}
    _item("item/started", turn_id, item)
    decision = "accept"
    if policy != "never" and sandbox == "readOnly" and path not in _session_ok:
        decision = _ask([("item/fileChange/requestApproval", {
            "threadId": THREAD_ID, "turnId": turn_id, "itemId": item["id"],
            "startedAtMs": int(time.time() * 1000), "reason": None,
            "grantRoot": None})])[0]
    if decision == "acceptForSession":
        _session_ok.add(path)
    _item("item/completed", turn_id, dict(item, status=_settle(decision)))


def _echo_seen(text):
    try:
        with open(CHECK_ECHO, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return False
    return any(r.get("type") == "user"
               and r.get("message", {}).get("content") == [{"type": "text", "text": text}]
               for r in rows)


def _complete(turn_id, status, error=None):
    _notify("thread/status/changed", threadId=THREAD_ID, status={"type": "idle"})
    _notify("turn/completed", threadId=THREAD_ID, turn={
        "id": turn_id, "items": [], "itemsView": "summary", "status": status,
        "error": error, "durationMs": 5})


def _turn(turn_id, params):
    """One turn, on the main thread."""
    text = "".join(i.get("text", "") for i in params.get("input") or [])
    policy = params.get("approvalPolicy")
    sandbox = (params.get("sandboxPolicy") or {}).get("type")
    _turn_count[0] += 1
    try:
        if CHECK_ECHO:
            _log({"echo_seen": _echo_seen(text), "text": text})
        _notify("turn/started", threadId=THREAD_ID,
                turn={"id": turn_id, "items": [], "status": "inProgress"})
        user = {"type": "userMessage", "id": _uuid7(), "clientId": None,
                "content": [{"type": "text", "text": text, "text_elements": []}]}
        _item("item/started", turn_id, user)
        _item("item/completed", turn_id, user)
        if _turn_count[0] == 1:
            _notify("thread/name/updated", threadId=THREAD_ID,
                    threadName="A fake thread")
        if DIE:
            sys.stdout.flush()
            os._exit(3)
        if SIGNED_OUT_TURN:
            msg = ("unexpected status 401 Unauthorized: Missing bearer or basic "
                   "authentication in header")
            _notify("error", threadId=THREAD_ID, turnId=turn_id, willRetry=False,
                    error={"message": msg, "codexErrorInfo": "unauthorized"})
            _complete(turn_id, "failed", {"message": msg})
            return
        _usage()
        if WITH_THOUGHT:
            rs = {"type": "reasoning", "id": "rs-" + turn_id[-6:], "summary": [],
                  "content": []}
            _item("item/started", turn_id, rs)
            _notify("item/reasoning/summaryTextDelta", threadId=THREAD_ID,
                    turnId=turn_id, itemId=rs["id"], delta="Let me think",
                    summaryIndex=0)
            # The raw text of the same thought: a host must not tell it twice.
            _notify("item/reasoning/textDelta", threadId=THREAD_ID,
                    turnId=turn_id, itemId=rs["id"], delta="RAW", contentIndex=0)
            _item("item/completed", turn_id, dict(rs, summary=["Let me think"]))
        msg = {"type": "agentMessage", "id": "msg-1-" + turn_id[-6:], "text": "",
               "phase": "commentary"}
        _item("item/started", turn_id, msg)
        for piece in (TEXT, " world"):
            _notify("item/agentMessage/delta", threadId=THREAD_ID, turnId=turn_id,
                    itemId=msg["id"], delta=piece)
        said = TEXT + " world"
        if WITH_TOOL or PERMS or EDIT or WITH_MCP or WITH_WEB:
            _item("item/completed", turn_id, dict(msg, text=said))
            if WITH_TOOL or PERMS:
                _commands(turn_id, policy, max(PERMS, 1))
            if EDIT and not _interrupt.is_set():
                _edit(turn_id, policy, sandbox)
            if WITH_MCP:
                call = {"type": "mcpToolCall", "id": "mcp-" + turn_id[-6:],
                        "server": "fused_approvals", "tool": "app_state",
                        "arguments": {"reason": "look"}, "status": "inProgress",
                        "result": None, "error": None}
                _item("item/started", turn_id, call)
                _item("item/completed", turn_id, dict(
                    call, status="completed",
                    result={"content": [{"type": "text", "text": "state: ok"}]}))
            if WITH_WEB:
                ws = {"type": "webSearch", "id": "ws-" + turn_id[-6:],
                      "query": "fused render"}
                _item("item/started", turn_id, ws)
                _item("item/completed", turn_id, dict(
                    ws, results=[{"title": "Fused", "url": "https://fused.io"}]))
            msg = {"type": "agentMessage", "id": "msg-2-" + turn_id[-6:], "text": "",
                   "phase": "final_answer"}
            _item("item/started", turn_id, msg)
            said = ""
        if BLOCK_UNTIL:
            while not os.path.exists(BLOCK_UNTIL) and not _eof.is_set() \
                    and not _interrupt.is_set():
                time.sleep(0.05)
        if _interrupt.is_set():
            if PERM_AFTER_INTERRUPT:
                _commands(turn_id, "untrusted", 1)
            # The open message is never completed (verified).
            _complete(turn_id, "interrupted")
            return
        if WITH_ERROR or ERROR_NOTICE:
            words = "You've hit your usage limit." if WITH_ERROR \
                else "stream disconnected before completion"
            _notify("error", threadId=THREAD_ID, turnId=turn_id, willRetry=True,
                    error={"message": "Reconnecting... 1/5"})
            _notify("error", threadId=THREAD_ID, turnId=turn_id, willRetry=False,
                    error={"message": words})
            _complete(turn_id, "failed", {"message": words} if WITH_ERROR else None)
            return
        _notify("item/agentMessage/delta", threadId=THREAD_ID, turnId=turn_id,
                itemId=msg["id"], delta="!")
        _item("item/completed", turn_id, dict(msg, text=said + "!"))
        _usage()
        _complete(turn_id, "completed")
    finally:
        with _flight_lock:
            _in_flight[0] -= 1


def main():
    if DUMP_ENV:
        with open(DUMP_ENV, "w", encoding="utf-8") as fh:
            json.dump(dict(os.environ), fh)
    threading.Thread(target=_reader, daemon=True).start()
    while True:
        item = _turns.get()
        if item is None:
            return 0
        _turn(*item)


if __name__ == "__main__":
    sys.exit(main())
