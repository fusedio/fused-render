"""Minimal stdio MCP server whose tools are "ask the browser" and "read the page".

Claude Code runs headless in this template (`claude -p`), so the CLI has no
terminal to put a permission prompt on: anything the session's rules don't
already allow was denied with "tool requires user interaction; no prompt
available in headless mode", invisibly, and the user just saw Claude give up.
`--permission-prompt-tool` names an MCP tool the CLI calls *instead* of
prompting, and this file is that tool. Each request is written to a file in the
run's `perm/` directory and the call blocks until `agent.py` drops a decision
next to it — that decision being the user's click in the chat UI.

The second tool is the same trick pointed the other way. This template is the
SPLIT view: the left pane renders the app the user is looking at, and the agent
edits it blind — after an edit it cannot see whether the page came back or
threw. `app_state` parks a request the same way, and the page answers it with a
snapshot of that iframe (console errors, params, a bounded DOM outline). It is
a read of the user's own screen for the agent they are already talking to, so
it raises no card; `agent.py` pre-allows it.

Spawned by `claude`, never by the app: stdlib only, no `fused_render` import,
no assumption about cwd. Up to two directories arrive as argv (tmp paths, not
secrets): argv[1] holds the permission round trip, argv[2] the app-state one.
Separate directories on purpose — the page renders every file in the perm dir
as an approval card, and a snapshot request is not something to click.

argv[2] is OPTIONAL, and its absence is the whole switch for the second tool: a
target with no left pane (an ordinary folder, D239) has no page to read back, so
`agent.py` spawns us with the perm dir alone and `app_state` then appears in
neither `tools/list` nor the dispatch. A tool the model can call but that can
never answer is worse than no tool — it gets called after every edit and times
out, once per turn.

Wire contract (Claude Code CLI, verified against 2.1.220):

    in   {"tool_name": str, "input": {...}, "tool_use_id": str}
    out  exactly one text block whose text is JSON —
         {"behavior": "allow", "updatedInput": {...}}
         {"behavior": "deny",  "message": str}          (message is required)

    "allow for the rest of this session" additionally returns
    `updatedPermissions: [{"type": "addRules", rules: [{"toolName": ...}],
    "behavior": "allow", "destination": "session"}]`, which hands the matching
    to the CLI's own rule engine rather than to a hand-rolled one here.

The JSON-RPC framing is newline-delimited JSON on stdin/stdout (MCP stdio).
stdout carries protocol only — diagnostics go to stderr.
"""
import json
import os
import sys
import threading
import time

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "fused_approvals"
TOOL_NAME = "approve"
APP_STATE_TOOL = "app_state"

# How long a request may sit unanswered before it denies itself. The chat frame
# can die (mode switch, reload) while a card is on screen; without a ceiling the
# claude subprocess would wait for a click that is never coming. Generous
# because the honest answer to "how long until a human clicks" is "a while" —
# the run dir's `timeout` in mcp.json is set above this so THIS deny wins and
# the user gets a sentence instead of an MCP timeout error.
WAIT_TIMEOUT = float(os.environ.get("FUSED_RENDER_PERMISSION_TIMEOUT", "3600"))
# The app-state wait is a different order of magnitude because there is no human
# in it: the page's poll loop answers within a tick (400 ms) for as long as the
# turn is running, so anything past a few seconds means the chat frame is gone
# (mode switch, reload) and no amount of waiting will produce an answer. Kept
# far below the permission wait, since the per-server MCP `timeout` in the run
# dir's mcp.json is sized for that one.
APP_STATE_TIMEOUT = float(os.environ.get("FUSED_RENDER_APP_STATE_TIMEOUT", "20"))
POLL_INTERVAL = 0.15
# How long a decision file that exists but has not parsed is treated as a write
# in flight rather than as no answer. Mirrors agent.py's DECISION_WRITE_WINDOW.
DECISION_WRITE_WINDOW = 2.0
# Modes a card may switch the running session to. Mirrors SWITCHABLE_MODES in
# agent.py; a test asserts the two agree. Never `bypassPermissions`.
SWITCHABLE_MODES = frozenset({"acceptEdits", "auto"})

PERM_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else ""
STATE_DIR = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else ""

_stdout_lock = threading.Lock()
_id_lock = threading.Lock()
_id_counter = 0


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _send(payload: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def _request_id() -> str:
    """Ours, not the CLI's `tool_use_id`: this id is joined into a path, and a
    filename we minted ourselves cannot escape the perm dir no matter what the
    caller sends. The tool_use_id still rides along inside the request body."""
    global _id_counter
    with _id_lock:
        _id_counter += 1
        n = _id_counter
    return "%s-%03d-%s" % (time.strftime("%H%M%S"), n, os.urandom(3).hex())


def _write_atomic(path: str, data: dict) -> None:
    """Write a request file, atomically and `rw-------`.

    0600 from the create itself rather than a chmod afterwards, so the file is
    never briefly readable: a request body is the whole tool payload — the
    command, the content being written, the web input — and the run tree sits
    under a temp root that is world-readable on a typical Linux box."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)  # a poll must never read a half-written request


def _read_decision(res_path: str) -> dict:
    try:
        with open(res_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}  # absent, or caught mid-write — the next tick re-reads it
    return data if isinstance(data, dict) else {}


def _await_written(res_path: str, window: float) -> dict:
    """Re-read a decision file that exists but has not parsed yet.

    agent.py creates it with O_EXCL and writes the JSON a moment later, so
    "unparseable" means a click is landing right now — not that nobody
    answered. Reading it as nobody-answered is how this server could hand
    claude a deny for a tool the user had just allowed."""
    deadline = time.monotonic() + window
    while True:
        decision = _read_decision(res_path)
        if decision or time.monotonic() >= deadline:
            return decision
        time.sleep(POLL_INTERVAL)


def _await_answer(res_path: str, timeout: float, fallback: dict) -> dict:
    """Block until the page writes its answer next to the request, or give up.

    ONE mechanism for both tools — an approval and an app-state read are the
    same round trip with different payloads, and a second copy of this is a
    second place for the latch rules below to be got wrong.

    `fallback` is what the caller gets when nobody answered, and it is also
    RECORDED, so the request stops reading as "still waiting for you" on disk.
    Same first-writer-wins rule agent.py uses: if the create loses, an answer
    landed in this very instant and that answer is the truth — waited out
    rather than guessed, because its JSON may still be in flight.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = _read_decision(res_path)
        if answer:
            return answer
        time.sleep(POLL_INTERVAL)

    try:
        fd = os.open(res_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _await_written(res_path, DECISION_WRITE_WINDOW) or fallback
    except OSError:
        return fallback
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(fallback, fh)
    except OSError:
        # Deliberately NOT the unlink agent.py's writer does on the same
        # failure. There the write is the only thing that happened and nobody
        # has been told anything, so freeing the claim lets a retry succeed.
        # Here the verdict has ALREADY gone back to claude in the return below
        # — releasing the latch would let a later Allow land on disk and the
        # card would read "✓ Allowed" for a tool that was refused. Keeping the
        # claim costs a card that ends up reading "could not record that
        # decision", which is the truth.
        pass
    return fallback


def _await_decision(req_id: str) -> dict:
    """Block until agent.py writes the decision file, or we give up."""
    return _await_answer(os.path.join(PERM_DIR, req_id + ".res.json"),
                         WAIT_TIMEOUT, {"decision": "deny", "reason": "timeout"})


def _permission_result(tool_name: str, tool_input: dict, decision: dict) -> dict:
    """Turn the browser's click into the CLI's permission-result shape."""
    verdict = decision.get("decision")
    if verdict == "allow":
        result = {"behavior": "allow", "updatedInput": tool_input,
                  "decisionClassification": "user_temporary"}
        updates = []
        if decision.get("scope") == "session":
            # Let the CLI's rule engine own the matching from here on. Rule is
            # the bare tool name (no ruleContent): the wire gives us no
            # permission *suggestions* to narrow it with, and inventing our own
            # pattern — Bash(rm -rf *) prefix-matching and friends — is exactly
            # the hand-rolled matcher this defers to Claude Code instead.
            updates.append({
                "type": "addRules",
                "rules": [{"toolName": tool_name}],
                "behavior": "allow",
                "destination": "session",
            })
        # "…and stop asking": the sibling update type, which re-points the
        # running session's permission mode. Re-validated here and not merely
        # trusted from the decision file, because this is the side that hands
        # the CLI its payload — an unlisted mode must never reach it.
        if decision.get("mode") in SWITCHABLE_MODES:
            updates.append({
                "type": "setMode",
                "mode": decision["mode"],
                "destination": "session",
            })
        if updates:
            result["updatedPermissions"] = updates
            result["decisionClassification"] = "user_permanent"
        return result

    if verdict == "expired":
        # agent.py latched this when it saw the run end with the request still
        # unanswered. Reaching us at all means the run outlived that call, so
        # say what happened rather than blaming the user.
        message = "The reply ended before this was answered."
    elif decision.get("reason") == "timeout":
        message = ("No answer from the fused-render chat window after "
                   "%d minutes — treating this as denied." % (WAIT_TIMEOUT // 60))
    elif decision.get("reason") == "cancelled":
        message = "The user cancelled this turn."
    else:
        message = decision.get("message") or "The user denied this request."
    return {"behavior": "deny", "message": message,
            "decisionClassification": "user_reject"}


def _text(payload: dict) -> dict:
    """A tool result the way both tools return one: a single text block whose
    text is JSON. Anything else and the CLI raises "Permission prompt tool
    returned an invalid result"; the app-state tool matches it because the
    model reads that text and JSON is the shape it parses best."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _handle_approve(args: dict) -> dict:
    tool_name = args.get("tool_name") or "(unknown tool)"
    tool_input = args.get("input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    req_id = _request_id()
    _write_atomic(os.path.join(PERM_DIR, req_id + ".req.json"), {
        "id": req_id,
        "tool": tool_name,
        "input": tool_input,
        "tool_use_id": args.get("tool_use_id") or "",
        "created_at": time.time(),
    })
    return _text(_permission_result(tool_name, tool_input,
                                    _await_decision(req_id)))


def _handle_app_state(args: dict) -> dict:
    """Ask the page for a snapshot of the app it is rendering.

    Always one text block holding JSON, whatever happened: either
    {"state": ...} as the page described it, or {"error": ...}. Never a
    JSON-RPC error — a model can act on a sentence saying the window did not
    answer, and cannot act on a tool that appears broken (it retries, or gives
    up on looking at all, which is the whole feature).
    """
    if not STATE_DIR:
        return _text({"error": "this session has no app-state channel"})
    reason = args.get("reason")
    req_id = _request_id()
    _write_atomic(os.path.join(STATE_DIR, req_id + ".req.json"), {
        "id": req_id,
        "reason": str(reason or ""),
        "created_at": time.time(),
    })
    answer = _await_answer(
        os.path.join(STATE_DIR, req_id + ".res.json"), APP_STATE_TIMEOUT,
        {"error": "the fused-render window did not answer within %d seconds — "
                  "it may have been closed or navigated away, so nothing is "
                  "known about the live app right now" % APP_STATE_TIMEOUT})
    if "state" in answer:
        return _text({"state": answer["state"]})
    return _text({"error": str(answer.get("error")
                              or "the window could not read the app")})


TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Ask the fused-render chat window whether a tool call may proceed. "
        "Called by Claude Code as the --permission-prompt-tool; not for model use."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}

APP_STATE_SCHEMA = {
    "name": APP_STATE_TOOL,
    "description": (
        "Read the live state of the app the user is looking at — console "
        "errors and warnings, the page's URL params, and an outline of its "
        "DOM — as it is rendering right now in the left pane. Call this after "
        "making an edit that affects the rendered page: the page live-reloads "
        "itself, so this is how you find out whether your change worked, and "
        "whether it threw. Cheap and read-only; it does not touch the app."
    ),
    "inputSchema": {
        "type": "object",
        # No required field: the agent must never be blocked from looking
        # because it did not phrase a reason.
        "properties": {
            "reason": {
                "type": "string",
                "description": "Optionally, what you are checking — shown to "
                               "the user in the chat as a one-line note.",
            },
        },
    },
}


def _dispatch(method: str, params: dict) -> dict:
    if method == "initialize":
        # Echo the client's protocol version when it names one: this server
        # uses nothing version-specific, and mirroring avoids a needless
        # mismatch with whichever CLI the user has installed.
        client_version = params.get("protocolVersion")
        return {
            "protocolVersion": (client_version if isinstance(client_version, str)
                                else PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }
    if method == "tools/list":
        # The roster follows the CHANNEL, not the target kind: no app-state
        # directory in argv means this session has no page to read back, so the
        # tool is not offered at all. One signal for both halves — a roster that
        # could vary independently of the channel would advertise a tool this
        # server cannot serve, and the model would spend a 20-second timeout per
        # turn discovering that. D239: an ordinary folder has no left pane, so
        # `agent.py` spawns us with the perm dir alone.
        tools = [TOOL_SCHEMA]
        if STATE_DIR:
            tools.append(APP_STATE_SCHEMA)
        return {"tools": tools}
    if method == "tools/call":
        name = params.get("name")
        known = (TOOL_NAME, APP_STATE_TOOL) if STATE_DIR else (TOOL_NAME,)
        if name not in known:
            raise LookupError("unknown tool: %s" % name)
        args = params.get("arguments")
        args = args if isinstance(args, dict) else {}
        return _handle_approve(args) if name == TOOL_NAME \
            else _handle_app_state(args)
    if method == "ping":
        return {}
    raise LookupError("unknown method: %s" % method)


def _serve_request(req_id, method: str, params: dict) -> None:
    try:
        _send({"jsonrpc": "2.0", "id": req_id, "result": _dispatch(method, params)})
    except LookupError as exc:
        _send({"jsonrpc": "2.0", "id": req_id,
               "error": {"code": -32601, "message": str(exc)}})
    except (OSError, TypeError, ValueError) as exc:
        _send({"jsonrpc": "2.0", "id": req_id,
               "error": {"code": -32603, "message": "%s: %s"
                         % (type(exc).__name__, exc)}})


def main() -> int:
    if not PERM_DIR:
        _log("permission_server.py: missing perm-dir argument")
        return 2
    for path in (PERM_DIR, STATE_DIR):
        if not path:
            # A session without an app-state dir still does approvals: the tool
            # says so per call (_handle_app_state) rather than the whole server
            # refusing to start, because approvals are the load-bearing half.
            _log("permission_server.py: no app-state directory given")
            continue
        try:
            # 0700 for the same reason the files are 0600 — agent.py normally
            # created these already, but the mode must not depend on who got
            # here first (and `mode` reaches the leaf only, which is all we make).
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError as exc:
            _log("permission_server.py: cannot use %s (%s)" % (path, exc))
            return 2

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        req_id = msg.get("id")
        if req_id is None:
            continue  # a notification (initialized, cancelled, …) — nothing to answer
        method = msg.get("method") or ""
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "tools/call":
            # Off the reader thread: a pending approval blocks for as long as
            # the user takes, and parallel tool calls must each get their own
            # card instead of queueing behind the first one.
            threading.Thread(target=_serve_request,
                             args=(req_id, method, params), daemon=True).start()
        else:
            _serve_request(req_id, method, params)
    return 0


if __name__ == "__main__":
    sys.exit(main())
