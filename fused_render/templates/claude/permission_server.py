"""Minimal stdio MCP server whose one tool is "ask the browser".

Claude Code runs headless in this template (`claude -p`), so the CLI has no
terminal to put a permission prompt on: anything the session's rules don't
already allow was denied with "tool requires user interaction; no prompt
available in headless mode", invisibly, and the user just saw Claude give up.
`--permission-prompt-tool` names an MCP tool the CLI calls *instead* of
prompting, and this file is that tool. Each request is written to a file in the
run's `perm/` directory and the call blocks until `agent.py` drops a decision
next to it — that decision being the user's click in the chat UI.

Spawned by `claude`, never by the app: stdlib only, no `fused_render` import,
no assumption about cwd. The perm directory arrives as argv[1] (a tmp path, not
a secret).

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

# How long a request may sit unanswered before it denies itself. The chat frame
# can die (mode switch, reload) while a card is on screen; without a ceiling the
# claude subprocess would wait for a click that is never coming. Generous
# because the honest answer to "how long until a human clicks" is "a while" —
# the run dir's `timeout` in mcp.json is set above this so THIS deny wins and
# the user gets a sentence instead of an MCP timeout error.
WAIT_TIMEOUT = float(os.environ.get("FUSED_RENDER_PERMISSION_TIMEOUT", "3600"))
POLL_INTERVAL = 0.15
# How long a decision file that exists but has not parsed is treated as a write
# in flight rather than as no answer. Mirrors agent.py's DECISION_WRITE_WINDOW.
DECISION_WRITE_WINDOW = 2.0
# Modes a card may switch the running session to. Mirrors SWITCHABLE_MODES in
# agent.py; a test asserts the two agree. Never `bypassPermissions`.
SWITCHABLE_MODES = frozenset({"acceptEdits", "auto"})

PERM_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else ""

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


def _await_decision(req_id: str) -> dict:
    """Block until agent.py writes the decision file, or we give up."""
    res_path = os.path.join(PERM_DIR, req_id + ".res.json")
    timeout = {"decision": "deny", "reason": "timeout"}
    deadline = time.monotonic() + WAIT_TIMEOUT
    while time.monotonic() < deadline:
        decision = _read_decision(res_path)
        if decision:
            return decision
        time.sleep(POLL_INTERVAL)

    # Nobody answered. Record the deny rather than just returning it, so the
    # request stops reading as "still waiting for you" on disk. Same
    # first-writer-wins rule agent.py uses: if the create loses, a click landed
    # in this very instant and that click is the answer — waited out rather
    # than guessed, because its JSON may still be in flight.
    try:
        fd = os.open(res_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _await_written(res_path, DECISION_WRITE_WINDOW) or timeout
    except OSError:
        return timeout
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(timeout, fh)
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
    return timeout


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
    result = _permission_result(tool_name, tool_input, _await_decision(req_id))
    # A single text block holding JSON — anything else and the CLI raises
    # "Permission prompt tool returned an invalid result".
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


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
        return {"tools": [TOOL_SCHEMA]}
    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            raise LookupError("unknown tool: %s" % params.get("name"))
        args = params.get("arguments")
        return _handle_approve(args if isinstance(args, dict) else {})
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
    try:
        # 0700 for the same reason the files are 0600 — agent.py normally
        # created this already, but the mode must not depend on who got here
        # first (and `mode` reaches the leaf only, which is all we make).
        os.makedirs(PERM_DIR, mode=0o700, exist_ok=True)
    except OSError as exc:
        _log("permission_server.py: cannot use %s (%s)" % (PERM_DIR, exc))
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
