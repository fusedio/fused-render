"""A session host for OPENAI CODEX CLI — the Codex twin of copilot_host.py.

Codex, like Copilot, persists nothing fused can read in place and asks the
CLIENT for every approval, so this process is again BOTH halves at once: the
JSON-RPC client of `codex app-server` (Codex's own "app-server protocol v2",
newline-delimited JSON-RPC 2.0 over stdio, the wire its VS Code extension
speaks — docs/CODEX_BACKEND.md §3), and the writer of every file the rest of
the app reads for a Claude session:

  * `run_dir/out.jsonl` in the stream-json shape (`system/init`, the user
    echo, `stream_event` deltas, `assistant`/`user` tool rows, one `result`
    per turn) — docs/CODEX_BACKEND.md §4 is the row contract; it is
    COPILOT_BACKEND.md §4 with the left column changed, and the readers it
    was written against are `agent._poll`, `_turn_state`,
    `_read_current_turn`, `_starts_new_turn`, `_segments_from_rows`;
  * the durable transcript at `agent_roots.projects_dir("codex")/<munge(cwd)>/
    <sid>.jsonl` in Claude Code's own record shape, for `tasks_store._parse_head`,
    `routers/tasks._absorb` and `session_liveness.transcript_turn_open` (D3);
  * `run_dir/pid` (the CLI's), `host.json`, the live registry row at
    `agent_roots.sessions_dir("codex")/<pid>.json`, and the permission round
    trip through `run_dir/perm/<id>.req.json` / `.res.json` in exactly the
    shapes `permission_server.py` writes and `agent._permissions` / `_decide`
    read (D4) — Codex's `item/commandExecution/requestApproval` and
    `item/fileChange/requestApproval` are answered off the same card the chat
    window already draws.

A SEPARATE FILE FROM copilot_host.py ON PURPOSE (D1): Codex is not ACP. Every
method name differs, and so do the turn's shape (a `turn/start` request that
is answered AT ONCE with a turn id, then notifications until a
`turn/completed`, where ACP's `session/prompt` stays pending for the whole
turn), the cancel (a `turn/interrupt` REQUEST naming that turn id, where ACP
has a `session/cancel` notification), the approval answer (a v2 decision
enum, where ACP picks an option by kind) and the resume (`thread/resume`
answers with the whole history in its reply, where ACP replays it as
notifications). What is the same — the run-dir files, the row shapes, the
inbox, the card round trip, the reap, the event posts — is copied from
copilot_host.py deliberately and kept in the same order, so a fix to one is
easy to carry to the other; the two never import each other.

Where Codex is SIMPLER, this host takes the simpler road (§0): the system
prompt is a request field (`developerInstructions`, D8), and model, effort,
approval policy and sandbox ride EVERY `turn/start` (D5, D7) — so a model or
mode change is stored here and is simply the next turn's values, never a
respawn (invariant 15).

Loaded and run BY PATH, exactly like session_host.py and copilot_host.py
(`sys.executable codex_host.py`, the JSON request on stdin — never argv,
which `ps` shows to every local user). Same request keys as copilot_host's:
`run_dir, cwd, file, pane, model, effort, cli_mode, session_id,
extra_read_dirs, agent, argv` (the full `codex app-server ...` argv
`agent._codex_argv` built — this host runs it verbatim) and `backend:
"codex"`. Stdlib only, never imports `fused_render`; `agent.py` is loaded by
path for its spawn and prompt helpers and `agent_roots` through the shared
dir, the way agent.py itself seeds `_SHARED`.

THE READERS DECIDE THE ROW ORDER, and copilot_host.py's three rules hold here
verbatim (see its header for the long form):

  1. nothing notification-driven — a late `thread/tokenUsage/updated`, a
     title — lands in out.jsonl between a `result` and the next user echo;
  2. a turn starts at a `user` row whose content is all `text` blocks — the
     echo written BEFORE `turn/start` goes out (invariant 3) — and a tool's
     output rides a `tool_result` block, never prose;
  3. every turn's transcript ends on a text-only assistant row, or, when it
     was stopped, on Claude Code's own `[Request interrupted by user]` mark.

Threads: ONE reader thread owns the CLI's stdout and dispatches replies by
id, notifications by method, and server→client requests by method — an
approval request is served on its own thread because it blocks for as long as
the user takes. The main thread drains the inbox and runs the reap timer.
Every state change happens under one lock; every write to the CLI's stdin
under another. Fork-safety is session_host's argument verbatim: this is a
bare interpreter the server never forks into.
"""
import importlib.util
import itertools
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.join(os.path.dirname(HERE), "templates", "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)
import agent_roots  # noqa: E402  (shared dir, loaded by path like agent.py does)

BACKEND = "codex"
# What `initialize` tells the server who we are. The server echoes it into its
# `userAgent` and into each thread's `originator` (verified: the probe's
# `fused/0.163.0 (...)`), so it is a name a user may meet in `codex resume`'s
# picker — the app's own, never a version that could be mistaken for Codex's.
_CLIENT_INFO = {"name": "fused-render", "version": "1"}

# Same two knobs as session_host.py and copilot_host.py, by the same names, so
# a test (or an operator) shrinks every host with one setting.
_IDLE_REAP_SECONDS = float(
    os.environ.get("FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS", "30"))
_DRAIN_INTERVAL_SECONDS = float(
    os.environ.get("FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS", "0.2"))
# How long `initialize` + `thread/start|resume` may take before the run is
# reported dead (§5). A first start on a machine (MCP server startup, the
# sandbox helper, auth refresh) is slow; a server that never answers at all is
# the case this bounds.
_HANDSHAKE_SECONDS = float(
    os.environ.get("FUSED_CODEX_HOST_HANDSHAKE_SECONDS", "120"))
# The reap is close stdin → wait → kill: the app-server exits on stdin EOF
# (verified 2026-10-21) and tears its own MCP children down, so the kill is
# only for one that hangs.
_EXIT_WAIT_SECONDS = 5.0
# How long an interrupted turn may take to report `turn/completed` before the
# host stops waiting: it settles the turn itself and kills the CLI
# (`_reap_cancel`).
_CANCEL_GRACE_SECONDS = float(
    os.environ.get("FUSED_CODEX_HOST_CANCEL_GRACE_SECONDS", "15"))
# The permission wait mirrors permission_server.py: same env knob, same poll,
# same write window — the card on the other side is the same card.
_PERM_WAIT = float(os.environ.get("FUSED_RENDER_PERMISSION_TIMEOUT", "3600"))
_PERM_POLL = 0.15
_DECISION_WRITE_WINDOW = 2.0
_UNSUPPORTED = "unsupported by codex"
# Claude Code's own stop marker, as the tasks readers spell it
# (tasks_store._INTERRUPT_MARKS) — copilot_host's reasoning verbatim: without
# it a stopped turn's transcript reads open for STALE_TAIL_SEC.
_INTERRUPT_MARK = "[Request interrupted by user]"
_DIED = ("codex exited", "codex is not running")

# D5: fused's mode → (approvalPolicy, sandbox). `sandbox` is the string
# `thread/start`/`thread/resume` take (SandboxMode); `turn/start` takes the
# structured `sandboxPolicy` of the same name below (SandboxPolicy). Both
# spellings are the generated schema's. `danger-full-access` is never sent.
_MODE_POLICY = {"prompt": ("untrusted", "read-only"),
                "acceptEdits": ("untrusted", "workspace-write"),
                "auto": ("never", "workspace-write")}
_SANDBOX_POLICY = {"read-only": {"type": "readOnly"},
                   "workspace-write": {"type": "workspaceWrite"}}
# D4: the server→client requests this host answers with a card, and which
# card `kind` each is. Every other server request is refused -32601.
_APPROVAL_KINDS = {"item/commandExecution/requestApproval": "command",
                   "item/fileChange/requestApproval": "file"}
# Invariant 14: the ONLY words a permission answer may carry — the v2
# CommandExecutionApprovalDecision / FileChangeApprovalDecision strings. The
# v1 `applyPatchApproval` words (`approved`, `denied`, ...) read as a decline
# on the v2 request (verified the hard way: the item completed `declined`).
_V2_DECISIONS = frozenset({"accept", "acceptForSession", "decline", "cancel"})
# D14: the item types that become tool rows; everything else (plan,
# contextCompaction, imageView, ...) is ignored.
_TOOL_ITEMS = frozenset({"commandExecution", "fileChange", "mcpToolCall",
                         "webSearch"})
_EDIT_TOOLS = frozenset({"Edit", "Write"})
# Streamed command output is kept per item only as a fallback for an
# `item/completed` whose `aggregatedOutput` is null; a runaway command must
# not grow the host without bound.
_OUTPUT_CAP = 200_000
_FD_CLOSE_CAP = 65536

_EVENT_PATH = "/api/tasks/queue/event"
_EVENT_TIMEOUT = 5.0
_EVENT_RETRY_SECONDS = 1.0


# ----------------------------------------------------------------- plumbing
#
# copilot_host.py's helpers, verbatim but for the names in messages.

def _now_iso() -> str:
    """ISO 8601 UTC with a `Z`, the way Claude Code stamps every record —
    `tasks_store.epoch` and `agent._row_ts` parse exactly this."""
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + \
        ".%03dZ" % int((t % 1) * 1000)


def _load_agent(path: str):
    """agent.py by file path — mirrors `session_host._load_agent`."""
    spec = importlib.util.spec_from_file_location("claude_agent_codex_host", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _append_private(path: str):
    """Binary append, created 0600 if new (session_host's own helper)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "ab")


def _private_dirs(path: str) -> None:
    """`makedirs` with 0700 on every level this call creates — copilot_host's
    helper, for the same reason: a transcript bucket under `codex_dir()`
    outlives every run and is shared by every turn."""
    missing = []
    head = os.path.abspath(path)
    while head and not os.path.isdir(head):
        missing.append(head)
        head = os.path.dirname(head)
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass


def _write_atomic(path: str, data: dict) -> None:
    """permission_server's `_write_atomic`: 0600 from the create, then one
    `os.replace`, so a poll never reads half a request."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _err_tail(run_dir: str, limit: int = 2000) -> str:
    """The last bytes of `err.log` — what `_poll`'s abnormal-exit fallback
    shows, handed back on the `result` row here so a transport death reads
    the same way whichever reader gets there first."""
    try:
        with open(os.path.join(run_dir, "err.log"), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _enable_faulthandler() -> None:
    try:
        import faulthandler
        if sys.stderr is not None:
            faulthandler.enable(all_threads=True)
    except Exception:  # noqa: BLE001
        pass


def _close_inherited_fds() -> None:
    """session_host's D1310 cleanup: every fd above stdio the server leaked
    into this process must not ride on into the CLI."""
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (AttributeError, ValueError, OSError):
        limit = -1
    if limit is None or limit < 0 or limit > _FD_CLOSE_CAP:
        limit = _FD_CLOSE_CAP
    os.closerange(3, limit)


def _int(value) -> int:
    """A token count off the wire: an int (never a bool, which IS an int in
    Python and would read as 1 token), else 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _fused_mode(cli_mode) -> str:
    """fused's mode key for whatever spelling arrived — `_start`'s `cli_mode`
    (PERMISSION_MODES' value: `default` for Ask, or None), a
    `set_permission_mode` control request's `mode`, a card's `mode`. Ask is
    the default for anything else, `plan` included: the server refuses plan
    for Codex (§2 D5) before it gets here, and the narrowest policy is the
    right answer to a spelling this host does not know."""
    return cli_mode if cli_mode in ("acceptEdits", "auto") else "prompt"


# ------------------------------------------------------- telling the queue
#
# The same four posts session_host.py and permission_server.py make
# (`turn_ended`, `exited`, `card_raised`, `card_cleared`), with session_host's
# posture — fire and forget, own daemon thread, 5 s + one retry, silence when
# no origin is exported. See the long comment above `_post_event` there.

def _meta_session_id(run_dir: str) -> str:
    meta = _read_json(os.path.join(run_dir, "meta.json"))
    return str(meta.get("session_id") or meta.get("resumed_from") or "")


def _post_event(run_dir: str, kind: str, **extra):
    try:
        origin = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")
        if not origin:
            return None
        body = {"kind": kind,
                "run_id": os.path.basename(os.path.normpath(run_dir)),
                "session_id": _meta_session_id(run_dir)}
        body.update(extra)
        t = threading.Thread(target=_send_event, args=(origin + _EVENT_PATH, body),
                             daemon=True)
        t.start()
        return t
    except Exception:
        return None


def _post_card(run_dir: str, kind: str, req_id: str) -> None:
    """permission_server's `_post_card` body: `{kind, run_id, request_id}`."""
    try:
        origin = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")
        if not origin:
            return
        body = {"kind": kind,
                "run_id": os.path.basename(os.path.normpath(run_dir)),
                "request_id": req_id}
        threading.Thread(target=_send_event, args=(origin + _EVENT_PATH, body),
                         daemon=True).start()
    except Exception:
        pass


def _send_event(url: str, body: dict) -> None:
    data = json.dumps(body).encode("utf-8")
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json", "X-Fused": "1"},
                method="POST")
            with urllib.request.urlopen(req, timeout=_EVENT_TIMEOUT) as resp:
                resp.read()
            return
        except Exception:
            if attempt:
                return
        try:
            time.sleep(_EVENT_RETRY_SECONDS)
        except Exception:
            return


# ---------------------------------------------------------- the JSON-RPC wire

class _Rpc:
    """One outbound request: the slot its reply lands in. `error` is the
    JSON-RPC error object, or a transport failure's message."""

    def __init__(self, method: str):
        self.method = method
        self.done = threading.Event()
        self.result = None
        self.error = None
        self.on_done = None

    def settle(self, result=None, error=None) -> None:
        self.result, self.error = result, error
        self.done.set()
        cb = self.on_done
        if cb is not None:
            cb(self)


class _Peer:
    """The CLI's stdio as JSON-RPC: one writer lock, one reader thread —
    copilot_host's `_Peer`, plus `on_eof`.

    Replies are routed to their `_Rpc` by id; notifications and inbound
    requests are handed to the callbacks the host installs. On EOF every
    pending request is failed, `on_eof` runs and `dead` is set, which is the
    main loop's cue to leave. `on_eof` is what copilot_host never needed: an
    ACP `session/prompt` stays PENDING for its whole turn, so failing pending
    requests settles an open turn; Codex answers `turn/start` at once, and a
    turn the server dies in the middle of has no request left to fail — the
    host's EOF hook settles it instead (invariant 2).

    Server→client request ids are the server's own and may be 0 (verified:
    the first approval of a connection is `"id": 0`), so a request is told
    from a notification by the KEY's presence, never by the id's truth."""

    def __init__(self, cli):
        self.cli = cli
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending = {}
        self._pending_lock = threading.Lock()
        self.dead = threading.Event()
        self.on_notification = lambda method, params: None
        self.on_request = lambda rid, method, params: None
        self.on_eof = lambda: None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)

    def start(self) -> None:
        """Begin reading — AFTER the host has installed its callbacks, or the
        first notifications land on the no-op defaults."""
        self._reader.start()

    def _send(self, msg: dict) -> bool:
        # ASCII-escaped, like every line this host writes: a lone surrogate
        # (half an emoji, cut by a delta boundary or a paste) is valid JSON as
        # `\ud83d` and raises as raw UTF-8.
        data = (json.dumps(msg) + "\n").encode("ascii")
        with self._write_lock:
            try:
                self.cli.stdin.write(data)
                self.cli.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def request(self, method: str, params: dict, on_done=None) -> _Rpc:
        rpc = _Rpc(method)
        rpc.on_done = on_done
        rid = next(self._ids)
        with self._pending_lock:
            self._pending[rid] = rpc
        if self.dead.is_set() or not self._send(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            with self._pending_lock:
                self._pending.pop(rid, None)
            rpc.settle(error={"code": -32000, "message": "codex is not running"})
        return rpc

    def notify(self, method: str, params: dict | None = None) -> None:
        """A client notification. `params` left OUT when None: `initialized`
        is a bare `{"method": "initialized"}` in the generated schema
        (ClientNotification has no params) and in the verified probe."""
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def respond(self, rid, result) -> None:
        self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def respond_error(self, rid, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": rid,
                    "error": {"code": code, "message": message}})

    def _read_loop(self) -> None:
        try:
            for raw in self.cli.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self._dispatch(msg)
                except Exception as exc:  # noqa: BLE001 — one bad message must not end the session
                    sys.stderr.write("codex_host: dispatch failed: %r\n" % (exc,))
        finally:
            with self._pending_lock:
                pending, self._pending = list(self._pending.values()), {}
            for rpc in pending:
                rpc.settle(error={"code": -32000, "message": "codex exited"})
            try:
                self.on_eof()
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write("codex_host: eof handling failed: %r\n" % (exc,))
            self.dead.set()

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg:
            params = msg.get("params")
            params = params if isinstance(params, dict) else {}
            if "id" in msg:
                self.on_request(msg["id"], str(msg["method"]), params)
            else:
                self.on_notification(str(msg["method"]), params)
            return
        with self._pending_lock:
            rpc = self._pending.pop(msg.get("id"), None)
        if rpc is None:
            return
        if "error" in msg:
            err = msg["error"]
            rpc.settle(error=err if isinstance(err, dict)
                       else {"code": -32000, "message": str(err)})
        else:
            rpc.settle(result=msg.get("result"))


# ------------------------------------------------------------- tool mapping

def _rel_path(path: str, cwd: str) -> str:
    """D14: a change's path, made relative to the session's cwd when it lies
    under it — Codex reports absolute paths (verified), the chat's cards and
    chips read best relative, and a path outside the cwd stays absolute so it
    is never mistaken for one inside."""
    if not path or not cwd or not os.path.isabs(path):
        return path
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:
        return path
    return path if rel == os.pardir or rel.startswith(os.pardir + os.sep) else rel


def _map_item(item: dict, cwd: str = "") -> tuple:
    """`(name, input)` for one tool-shaped Codex item, per D14: the Claude
    tool name the chat already has a card and a chip for, with the fields
    that card reads, plus `_codex: {type, itemId}` (the `_acp` sidecar's twin
    — `summaries.ts` reads `_acp` only, so this one is for logs and tests).
    `("", {})` for an item type with no row (D14's "ignored" line)."""
    kind = str(item.get("type") or "")
    item_id = str(item.get("id") or "")
    if kind == "commandExecution":
        command = str(item.get("command") or "")
        # The card's one-line summary: Codex's own parse of what the shell
        # line does (`commandActions[].command` — the bare `echo hi >
        # probe.txt` inside the verified `/bin/bash -lc '...'` wrapper),
        # else the line itself.
        actions = [str(a.get("command")) for a in item.get("commandActions") or []
                   if isinstance(a, dict) and a.get("command")]
        name, inp = "Bash", {"command": command,
                             "description": "; ".join(actions) or command}
    elif kind == "fileChange":
        changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
        paths = [str(c.get("path") or "") for c in changes]
        first = _rel_path(paths[0], cwd) if paths else ""
        kinds = [str((c.get("kind") or {}).get("type") or "")
                 if isinstance(c.get("kind"), dict) else str(c.get("kind") or "")
                 for c in changes]
        if changes and all(k == "add" for k in kinds):
            # A new file: its `diff` IS the content (verified: an `add` of
            # notes.md carried `"diff": "ok\n"`).
            name = "Write"
            if len(changes) == 1:
                content = str(changes[0].get("diff") or "")
            else:
                content = "".join("--- %s\n%s" % (_rel_path(p, cwd), str(c.get("diff") or ""))
                                  for p, c in zip(paths, changes))
            inp = {"file_path": first, "content": content}
        else:
            # ONE block per item (several files in one patch are one
            # approval in Codex, so they are one card here), every diff
            # labelled when there is more than one so the card says which
            # hunk is whose.
            name = "Edit"
            if len(changes) == 1:
                diff = str(changes[0].get("diff") or "")
            else:
                diff = "".join("--- %s\n%s" % (_rel_path(p, cwd), str(c.get("diff") or ""))
                               for p, c in zip(paths, changes))
            inp = {"file_path": first, "diff": diff}
    elif kind == "mcpToolCall":
        server = str(item.get("server") or "")
        tool = str(item.get("tool") or "")
        args = item.get("arguments")
        # D8: fused's own pane server is `fused_approvals`, so its
        # `app_state` comes out as `mcp__fused_approvals__app_state` — the
        # name agent.py pre-allows and `_segments_from_rows` strips — by the
        # same rule every other MCP tool follows.
        name = "mcp__%s__%s" % (server, tool)
        inp = dict(args) if isinstance(args, dict) else \
            ({} if args is None else {"arguments": args})
    elif kind == "webSearch":
        query = item.get("query")
        if not query:
            action = item.get("action")
            if isinstance(action, dict):
                query = action.get("query") or " ".join(
                    str(q) for q in action.get("queries") or []) or action.get("url")
        name, inp = "WebSearch", {"query": str(query or "")}
    else:
        return "", {}
    inp["_codex"] = {"type": kind, "itemId": item_id}
    return name, inp


def _content_text(content) -> str:
    """MCP `content[]` (text blocks) as one string; anything else as JSON."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block is not None:
                parts.append(json.dumps(block))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content)


def _item_result(item: dict, streamed: str = "") -> tuple:
    """`(content_text, is_error)` for a finished tool item (D14's right-hand
    column). `streamed` is the item's `outputDelta`s, for a command whose
    completed item carries no `aggregatedOutput`."""
    kind = item.get("type")
    status = str(item.get("status") or "")
    if kind == "commandExecution":
        out = item.get("aggregatedOutput")
        text = out if isinstance(out, str) else streamed
        code = item.get("exitCode")
        failed = status in ("failed", "declined") or (
            isinstance(code, int) and not isinstance(code, bool) and code != 0)
        # A declined or failed command with no output still says what
        # happened — an empty error block reads as a tool that never ran.
        if not text and failed:
            text = status if status in ("failed", "declined") else "exit code %s" % code
        return text, failed
    if kind == "fileChange":
        return status, status != "completed"
    if kind == "mcpToolCall":
        err = item.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"]), True
        result = item.get("result")
        text = _content_text(result.get("content")) if isinstance(result, dict) \
            else _content_text(result)
        if not text and isinstance(result, dict) and result.get("structuredContent") is not None:
            text = json.dumps(result["structuredContent"])
        return text, status == "failed"
    if kind == "webSearch":
        lines = []
        for r in item.get("results") or []:
            if isinstance(r, dict):
                label = " — ".join(str(r[k]) for k in ("title", "url") if r.get(k))
                lines.append(label or json.dumps(r))
            elif r is not None:
                lines.append(str(r))
        return "\n".join(lines), False
    return "", False


def _decision(card: dict, kind: str) -> str:
    """D4: the page's verdict as a v2 decision. allow + scope session →
    `acceptForSession`, allow → `accept`, everything else (deny, expired,
    timeout) → `decline`. The card's `mode` widens or narrows it:

      * `acceptEdits` ("allow, and accept edits") is that card's
        `acceptForSession` on a FILE card — Codex stops asking for that file
        — and a plain `accept` on a command card, whatever scope it sent: it
        widens edits only, never shell (copilot_host's L3, D4 here);
      * `auto` ("allow, and stop asking") is `acceptForSession` on any card;
        the host's stored mode moves too (`_park_permission`)."""
    if card.get("decision") != "allow":
        return "decline"
    mode = card.get("mode")
    if mode == "acceptEdits":
        return "acceptForSession" if kind == "file" else "accept"
    if mode == "auto" or card.get("scope") == "session":
        return "acceptForSession"
    return "accept"


# ------------------------------------------------------------------- the host

class _Host:
    def __init__(self, agent, req: dict, cli, peer: _Peer):
        self.agent = agent
        self.req = req
        self.cli = cli
        self.peer = peer
        self.run_dir = req["run_dir"]
        self.cwd = req["cwd"]
        # The values the NEXT `turn/start` carries (D5, D7): the spawn's, then
        # whatever a `set_model` / `set_permission_mode` control request or an
        # "allow, and stop asking" card stored since. Never sent as argv.
        self.model = str(req.get("model") or "")
        self.effort = str(req.get("effort") or "")
        self.mode = _fused_mode(req.get("cli_mode"))
        # THE SESSION — Codex's thread id. A resume names it; a fresh chat has
        # none until `thread/start` answers (Codex MINTS it, D2), so it is set
        # in `handshake` and nothing keyed on it is written before.
        self.sid = str(req.get("session_id") or "")
        self.resume = bool(self.sid)
        self.lock = threading.RLock()
        self._out = _append_private(os.path.join(self.run_dir, "out.jsonl"))
        self.transcript_path = self._transcript_path()
        # Opened on the first record, not here: a session that never comes
        # up (a refused handshake, a dead binary) must leave no empty
        # transcript for the tasks scan to list.
        self._tx = None
        self.parent_uuid = None
        self.registry_path = os.path.join(agent_roots.sessions_dir(BACKEND),
                                          "%d.json" % cli.pid)
        _private_dirs(os.path.dirname(self.registry_path))
        self.perm_dir = os.path.join(self.run_dir, "perm")
        # Turn state (§5 of the contract).
        self.turn_open = False
        self.turn_id = ""             # from the turn/start reply; turn/interrupt needs it
        self.interrupt_wanted = False  # a Stop that came before that reply
        self.turn_started = 0.0
        self.turn_started_at = 0.0
        self.turn_text = None         # text of the last closed text message
        self.turn_messages = 0        # assistant messages this turn (num_turns)
        self.turn_error = ""          # the newest final `error` notification's text
        self.tail_closed = True       # transcript's last row is a text assistant row
        self.msg_open = False         # a message_start has been emitted
        self.text_parts = []
        self.thinking_parts = []
        self.thinking_items = {}      # reasoning itemId -> "summary" | "text" (D14: one stream each)
        self.thinking_part = {}       # reasoning itemId -> last summaryIndex seen
        self.last_usage = {}          # newest tokenUsage.last block
        self.context_window = None
        self.window_announced = None  # the window out.jsonl has last been told
        # The Stop road (§5): set when `turn/interrupt` goes out (or is
        # wanted), cleared when the turn ends. `cancel_deadline` is when an
        # interrupted turn that never completed is settled by the host itself
        # (`_reap_cancel`).
        self.cancelling = False
        self.cancel_deadline = None
        self.pending_perm = {}        # rpc id -> {"perm_id", "done": Event, "kind"}
        self.results = 0              # result rows written so far
        self.result_written = False   # this turn's result is on disk
        self.started = False          # handshake done, init row written
        self.closing = False          # close() began: no new cards
        self.closed = False           # files closed; late notifications dropped
        self.pending_title = ""
        self.notice_pending = False   # D18 notice owed to out.jsonl's first turn
        self.tools = {}               # itemId -> (name, input), this turn's
        self.tool_output = {}         # itemId -> streamed outputDelta text
        self.tools_done = set()       # itemIds whose tool_result is written
        self._ids = itertools.count(1)
        peer.on_notification = self._on_notification
        peer.on_request = self._on_request
        peer.on_eof = self._on_eof
        peer.start()

    # ------------------------------------------------------------ writers

    def _transcript_path(self) -> str:
        """`<projects>/<munge(cwd)>/<sid>.jsonl`, "" while the id is unknown.
        A resume continues the transcript it already has, in whatever bucket
        that is — copilot_host's M9 reasoning verbatim."""
        if not self.sid:
            return ""
        if self.resume and not self.agent._bad_id(self.sid):
            found = agent_roots.find_transcript(self.sid, self.cwd)
            if found and found[0] == BACKEND:
                return found[1]
        return os.path.join(agent_roots.projects_dir(BACKEND),
                            agent_roots.munge(self.cwd), self.sid + ".jsonl")

    def _emit_out(self, row: dict) -> None:
        if self.closed:
            return
        row.setdefault("session_id", self.sid)
        row["timestamp"] = _now_iso()
        # `ensure_ascii` on (the default): see `_Peer._send`. The readers
        # json.loads every line, so the escape is invisible to them.
        self._out.write((json.dumps(row) + "\n").encode("ascii"))
        self._out.flush()

    def _emit_tx(self, row: dict) -> None:
        """One transcript record, in Claude Code's shape: `uuid`/`parentUuid`
        chain, `sessionId`, `cwd`, `timestamp`."""
        if self.closed or not self.transcript_path:
            return  # no session id yet: nothing to key a transcript on
        rid = str(uuid.uuid4())
        row.setdefault("uuid", rid)
        row.setdefault("parentUuid", self.parent_uuid)
        row.setdefault("sessionId", self.sid)
        row.setdefault("cwd", self.cwd)
        row["timestamp"] = _now_iso()
        self.parent_uuid = row["uuid"]
        if self._tx is None:
            _private_dirs(os.path.dirname(self.transcript_path))
            self._tx = _append_private(self.transcript_path)
        self._tx.write((json.dumps(row) + "\n").encode("ascii"))
        self._tx.flush()

    def _usage(self, with_output: bool = False) -> dict:
        """The newest `tokenUsage.last` in Claude's usage shape (§4).

        Codex's `inputTokens` INCLUDES its `cachedInputTokens` (verified:
        13466 input of which 11520 cached, one request), while Claude's
        `input_tokens` EXCLUDES its `cache_read_input_tokens` and the context
        meter adds the two. So the cached part is moved out of
        `input_tokens` into `cache_read_input_tokens`, and the meter's sum is
        Codex's own `inputTokens` — the prompt that request actually sent —
        never that plus the cached part counted twice. `output_tokens` only
        on the turn's `result` (`with_output`): Codex reports usage per
        request, not per message, so a message row cannot know its own."""
        last = self.last_usage
        total_in = _int(last.get("inputTokens"))
        cached = min(_int(last.get("cachedInputTokens")), total_in)
        return {"input_tokens": total_in - cached,
                "cache_creation_input_tokens": _int(last.get("cacheWriteInputTokens")),
                "cache_read_input_tokens": cached,
                "output_tokens": _int(last.get("outputTokens")) if with_output else 0}

    def _write_registry(self, status: str) -> None:
        try:
            _write_atomic(self.registry_path, {
                "sessionId": self.sid, "cwd": self.cwd, "status": status,
                "updatedAt": _now_iso(), "pid": self.cli.pid})
        except OSError:
            pass

    def write_init(self) -> None:
        """The FIRST row of out.jsonl (invariant 1), after the handshake.
        Codex offers no slash commands, skills or tool roster to the client,
        so those lists are empty; the context window rides along once a
        `thread/tokenUsage/updated` has named it."""
        row = {"type": "system", "subtype": "init", "session_id": self.sid,
               "cwd": self.cwd, "model": self.model, "backend": BACKEND,
               "permissionMode": str(self.req.get("cli_mode") or ""),
               "slash_commands": [], "skills": [], "tools": []}
        if self.context_window:
            row["context_window"] = self.context_window
            self.window_announced = self.context_window
        self._emit_out(row)

    def _notice_row(self) -> dict:
        return {"type": "system", "subtype": "task_notification",
                "summary": "Handled by OpenAI Codex · model %s" % self.model,
                "backend": BACKEND, "model": self.model}

    def write_first_turn_notice(self) -> None:
        """D18: the transcript's own record that Codex answers here — its
        first row. The LIVE copy waits for the first echo (`send_prompt`)."""
        self._emit_tx(dict(self._notice_row(), isMeta=False, isSidechain=False))
        self.notice_pending = True

    def _policy(self) -> tuple:
        """(approvalPolicy, sandbox string) for the stored mode (D5)."""
        return _MODE_POLICY[self.mode]

    # ------------------------------------------------------------- the turn

    def send_prompt(self, text: str, msg_uuid: str = "") -> None:
        """Echo first, then `turn/start` (invariant 3). The state and the
        echo go under the lock, so the reader thread cannot see a reply
        before the turn reads open; the pipe write goes OUTSIDE it —
        copilot_host's deadlock argument verbatim.

        The request carries the host's CURRENT model, effort, approval policy
        and sandbox (D5, D7): what a `set_model` / `set_permission_mode`
        control request or an "allow, and stop asking" card stored since the
        last turn takes effect here, with no respawn (invariant 15). Read
        under the lock with the echo, so the values sent are the ones that
        held when the turn opened.

        `msg_uuid` is the stamp `agent._write_inbox_entry` put on the inbox
        row, carried onto the echo (PR #1778), as copilot_host does."""
        with self.lock:
            self.turn_open = True
            self.turn_id = ""
            self.interrupt_wanted = False
            self.turn_started = time.monotonic()
            self.turn_started_at = time.time()   # wall clock, against file mtimes
            self.turn_text = None
            self.turn_messages = 0
            self.turn_error = ""
            self.tail_closed = False
            self.result_written = False
            self._emit_out({"type": "user",
                            **({"uuid": msg_uuid} if msg_uuid else {}),
                            "message": {"role": "user", "content": [
                                {"type": "text", "text": text}]}})
            if self.notice_pending:
                # D18 LIVE, right AFTER the echo — copilot_host's reasoning.
                self._emit_out(self._notice_row())
                self.notice_pending = False
            self._announce_window()
            self._emit_tx({"type": "user", "message": {
                "role": "user", "content": [{"type": "text", "text": text}]},
                "entrypoint": "sdk-cli", "isMeta": False, "isSidechain": False})
            self._write_registry("busy")
            policy, sandbox = self._policy()
            params = {"threadId": self.sid,
                      "input": [{"type": "text", "text": text}],
                      "approvalPolicy": policy,
                      "sandboxPolicy": dict(_SANDBOX_POLICY[sandbox])}
            if self.model:
                params["model"] = self.model
            if self.effort:
                params["effort"] = self.effort
        self.peer.request("turn/start", params, on_done=self._on_turn_started)

    def _on_turn_started(self, rpc: _Rpc) -> None:
        """The `turn/start` reply: the turn's id (`turn.id`), which is all a
        `turn/interrupt` can name. A REFUSED `turn/start` (an unknown model,
        a bad effort) means no turn ever began on the server and no
        `turn/completed` will come — so it ends the turn here, as a failure.
        A transport death is the EOF hook's to settle; this only declines to
        race it."""
        if rpc.error is not None:
            if rpc.error.get("message") in _DIED:
                self._on_eof()
                return
            text = str(rpc.error.get("message") or "codex refused the turn")
            data = rpc.error.get("data")
            if data:
                text += " — " + (data if isinstance(data, str) else json.dumps(data))
            self._end_turn("failed", text)
            return
        turn = rpc.result.get("turn") if isinstance(rpc.result, dict) else None
        tid = str(turn.get("id") or "") if isinstance(turn, dict) else ""
        with self.lock:
            if not self.turn_open:
                return
            self.turn_id = tid
            interrupt = self.interrupt_wanted and bool(tid)
        if interrupt:
            # A Stop that landed before the turn had an id (D6): sent now.
            self.peer.request("turn/interrupt", {"threadId": self.sid, "turnId": tid})

    def _announce_window(self) -> None:
        """`{type:system, subtype:status, context_window}` when the window
        out.jsonl knows is not the newest — only ever INSIDE a turn (rule 1:
        `status` is not a bookkeeping subtype, so after a `result` it would
        reopen the turn). A window learned between turns (a resume's
        `thread/tokenUsage/updated` lands after its reply, verified) waits for
        the next echo."""
        if self.turn_open and self.context_window \
                and self.context_window != self.window_announced:
            self.window_announced = self.context_window
            self._emit_out({"type": "system", "subtype": "status",
                            "context_window": self.context_window})

    def _open_message(self) -> None:
        if self.msg_open:
            return
        self.msg_open = True
        self._emit_out({"type": "stream_event", "event": {
            "type": "message_start",
            "message": {"role": "assistant", "model": self.model,
                        "content": [], "usage": self._usage()}}})

    def _close_message(self) -> None:
        """End the streamed message: `message_delta`, `message_stop`, then the
        finalized `assistant` row the transcript keeps — thinking first, then
        text, as one Claude message (§4). Called on `item/completed
        agentMessage`, before a tool row, and when the turn ends with a
        message still open (an interrupt never completes the item it cut,
        verified)."""
        if not self.msg_open:
            return
        self.msg_open = False
        text = "".join(self.text_parts)
        thinking = "".join(self.thinking_parts)
        self.text_parts, self.thinking_parts = [], []
        self._emit_out({"type": "stream_event", "event": {
            "type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 0}}})
        self._emit_out({"type": "stream_event", "event": {"type": "message_stop"}})
        content = []
        if thinking:
            content.append({"type": "thinking", "thinking": thinking})
        if text:
            content.append({"type": "text", "text": text})
        if not content:
            return
        self.turn_messages += 1
        message = {"role": "assistant", "model": self.model,
                   "content": content, "usage": self._usage()}
        self._emit_out({"type": "assistant", "message": message})
        self._emit_tx({"type": "assistant", "message": message,
                       "isMeta": False, "isSidechain": False})
        if text:
            self.turn_text = text
        # No `tool_use` in this row, so `transcript_turn_open` reads it as a
        # reply that finished — the turn's transcript may end here.
        self.tail_closed = True

    def _delta(self, kind: str, text: str) -> None:
        """One streamed piece of the open message (opened if none is)."""
        if not self.turn_open or not text:
            return  # a delta outside a turn has no turn to belong to
        self._open_message()
        if kind == "text":
            self.text_parts.append(text)
            delta = {"type": "text_delta", "text": text}
        else:
            self.thinking_parts.append(text)
            delta = {"type": "thinking_delta", "thinking": text}
        self._emit_out({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0, "delta": delta}})

    def _on_thinking_delta(self, method: str, params: dict) -> None:
        """`item/reasoning/summaryTextDelta` and `item/reasoning/textDelta`.
        A model may stream BOTH for one reasoning item (the summary and the
        raw text of the same thought); the first stream seen for an item is
        the one shown and the other is dropped, so the thought is never told
        twice. A new summary part (`summaryIndex` moving on) is a paragraph
        break."""
        item_id = str(params.get("itemId") or "")
        stream = "summary" if method.endswith("summaryTextDelta") else "text"
        if self.thinking_items.setdefault(item_id, stream) != stream:
            return
        delta = str(params.get("delta") or "")
        if stream == "summary":
            index = params.get("summaryIndex")
            last = self.thinking_part.get(item_id)
            if last is not None and index != last and self.thinking_parts:
                delta = "\n\n" + delta
            self.thinking_part[item_id] = index
        self._delta("thinking", delta)

    def _on_item_started(self, item: dict) -> None:
        if not self.turn_open:
            return
        kind = item.get("type")
        if kind in _TOOL_ITEMS:
            self._tool_use(item)
        # agentMessage / reasoning: the message opens on its first delta
        # (§4), so an item that never says anything leaves no empty message.
        # userMessage is our own echo (written at send time); the rest of
        # D14's list is ignored.

    def _tool_use(self, item: dict) -> None:
        """D14: an item becomes `content_block_start` + an `assistant` row
        carrying one `tool_use` block, after closing any open message (the
        text before a tool is its own message, as Claude writes it)."""
        item_id = str(item.get("id") or "")
        if item_id in self.tools:
            return
        name, inp = _map_item(item, self.cwd)
        if not name:
            return
        self._close_message()
        self.tools[item_id] = (name, inp)
        block = {"type": "tool_use", "id": item_id, "name": name, "input": inp}
        self._emit_out({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0, "content_block": block}})
        self.turn_messages += 1
        message = {"role": "assistant", "model": self.model,
                   "content": [block], "usage": self._usage()}
        self._emit_out({"type": "assistant", "message": message})
        self._emit_tx({"type": "assistant", "message": message,
                       "isMeta": False, "isSidechain": False})
        self.tail_closed = False

    def _on_item_completed(self, item: dict) -> None:
        if not self.turn_open:
            return
        kind = item.get("type")
        item_id = str(item.get("id") or "")
        if kind == "agentMessage":
            # The item's own `text` is the message as Codex recorded it. What
            # streamed is normally exactly that; when it is a prefix (or
            # nothing streamed at all) the rest goes out as one more delta so
            # the stream and the final row agree; when it is something else
            # entirely, the final row carries Codex's text.
            final = item.get("text")
            if isinstance(final, str) and final:
                streamed = "".join(self.text_parts)
                if final.startswith(streamed):
                    self._delta("text", final[len(streamed):])
                else:
                    self.text_parts = [final]
            self._close_message()
        elif kind == "reasoning":
            # A reasoning item that streamed nothing (no delta for it) but
            # completed with a summary or content: that thought, as thinking.
            if item_id not in self.thinking_items:
                parts = [str(p) for p in (item.get("summary") or item.get("content") or [])
                         if p]
                if parts:
                    self.thinking_items[item_id] = "item"
                    self._delta("thinking", "\n\n".join(parts))
        elif kind in _TOOL_ITEMS:
            if item_id not in self.tools:
                # Completed without a start we saw: the call first, so the
                # result always has a `tool_use` to belong to.
                self._tool_use(item)
            if item_id not in self.tools or item_id in self.tools_done:
                return
            self.tools_done.add(item_id)
            text, is_error = _item_result(item, self.tool_output.pop(item_id, ""))
            block = {"type": "tool_result", "tool_use_id": item_id,
                     "content": text, "is_error": is_error}
            message = {"role": "user", "content": [block]}
            self._emit_out({"type": "user", "message": message})
            self._emit_tx({"type": "user", "message": message,
                           "entrypoint": "sdk-cli", "isMeta": False,
                           "isSidechain": False})
            self.tail_closed = False

    def _on_output_delta(self, params: dict) -> None:
        if not self.turn_open:
            return
        item_id = str(params.get("itemId") or "")
        have = self.tool_output.get(item_id, "")
        if len(have) < _OUTPUT_CAP:
            self.tool_output[item_id] = (have + str(params.get("delta") or ""))[:_OUTPUT_CAP]

    # ---------------------------------------------------------- turn's end

    def _on_turn_completed(self, params: dict) -> None:
        turn = params.get("turn")
        turn = turn if isinstance(turn, dict) else {}
        with self.lock:
            # Another turn's completion (one the host already settled — a
            # cancel it gave up waiting on) is not this turn's end. Before the
            # turn/start reply lands the id is unknown and any completion is
            # ours: there is only ever one turn in flight (invariant 4).
            if not self.turn_open or (self.turn_id and turn.get("id")
                                      and turn.get("id") != self.turn_id):
                return
        status = str(turn.get("status") or "completed")
        err = turn.get("error")
        text = ""
        if isinstance(err, dict) and err.get("message"):
            text = str(err["message"])
            if err.get("additionalDetails"):
                text += " — " + str(err["additionalDetails"])
        self._end_turn(status, text)

    def _on_error(self, params: dict) -> None:
        """An `error` notification (D13). The turn is NOT ended here: Codex
        follows a final error with `turn/completed status: failed`, and a
        `willRetry: true` one is a stream it is retrying, which may yet
        succeed. Its text is kept as the failure's words when the failed
        turn's own `error.message` is empty. Shape: the generated schema's
        `{error: {message, additionalDetails}, willRetry, threadId, turnId}`;
        the contract's assumed `{message}` is read too."""
        if params.get("willRetry") is True:
            return
        err = params.get("error")
        text = err.get("message") if isinstance(err, dict) else params.get("message")
        if isinstance(err, dict) and err.get("additionalDetails") and text:
            text = "%s — %s" % (text, err["additionalDetails"])
        if text and self.turn_open:
            self.turn_error = str(text)

    def _on_eof(self) -> None:
        """The server's stdout closed — it exited, crashed, or was killed. A
        turn still open will never see its `turn/completed`: settled here, as
        the transport death it is (or the Stop it was, `_stopped_this_turn`)."""
        self._end_turn("", "", died=True)

    def _end_turn(self, status: str, error: str = "", died: bool = False) -> None:
        """EVERY road a turn can end on lands here, once: `turn/completed` of
        any status, a refused `turn/start`, the transport dying, the host
        settling an unanswered interrupt. One `result` row per turn
        (invariant 2) — copilot_host's `_on_prompt_reply`, by status instead
        of by reply."""
        with self.lock:
            if not self.turn_open:
                return
            try:
                self._finish_turn(status, error, died)
            except Exception as exc:  # noqa: BLE001
                # A write failed mid-way: the turn still has to CLOSE with a
                # result row, or `_turn_state` reads it open forever — and
                # with exactly one, so not when the failure came after it.
                try:
                    if self.result_written:
                        raise exc
                    self._emit_out({"type": "result", "session_id": self.sid,
                                    "subtype": "error_during_execution",
                                    "is_error": True, "stop_reason": "error",
                                    "result": "codex_host: %r" % (exc,),
                                    "num_turns": self.turn_messages,
                                    "usage": self._usage(True), "backend": BACKEND})
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self.turn_open = False
                self.turn_id = ""
                self.interrupt_wanted = False
                self.msg_open = False
                self.text_parts, self.thinking_parts = [], []
                # Item ids are per turn: a later card naming one must not be
                # dressed in a finished call's input.
                self.tools, self.tool_output, self.tools_done = {}, {}, set()
                self.thinking_items, self.thinking_part = {}, {}
                self.cancelling = False
                self.cancel_deadline = None
                self._write_registry("idle")
                _post_event(self.run_dir, "turn_ended", at=time.time(),
                            pid=self.cli.pid, session_id=self.sid)

    def _write_interrupt_mark(self) -> None:
        """The stop button's own last word in the transcript, so the tasks
        readers say "Interrupted by you" and read the turn as closed."""
        self._emit_tx({"type": "user", "message": {
            "role": "user",
            "content": [{"type": "text", "text": _INTERRUPT_MARK}]},
            "entrypoint": "sdk-cli", "isMeta": False, "isSidechain": False})
        self.tail_closed = True

    def _stopped_this_turn(self) -> bool:
        """Whether `run_dir/cancelled` was dropped DURING this turn —
        copilot_host's L5: an older marker is an earlier Stop's, and a crash
        now is a crash."""
        try:
            return os.path.getmtime(os.path.join(self.run_dir, "cancelled")) \
                >= self.turn_started_at
        except OSError:
            return False

    def _api_error(self, text: str) -> None:
        """D13: a failure is an assistant row the readers already know
        (`isApiErrorMessage`, model `<synthetic>` so the context meter skips
        it), in both files."""
        message = {"role": "assistant", "model": "<synthetic>",
                   "content": [{"type": "text", "text": text}],
                   "usage": self._usage()}
        self._emit_out({"type": "assistant", "isApiErrorMessage": True,
                        "message": message})
        self._emit_tx({"type": "assistant", "isApiErrorMessage": True,
                       "message": message, "isMeta": False, "isSidechain": False})
        self.tail_closed = True

    def _finish_turn(self, status: str, error: str, died: bool) -> None:
        self._close_message()
        duration_ms = int((time.monotonic() - self.turn_started) * 1000)
        result = {"type": "result", "session_id": self.sid, "backend": BACKEND,
                  "num_turns": self.turn_messages, "duration_ms": duration_ms,
                  "usage": self._usage(True)}
        if died and self._stopped_this_turn():
            # The transport died BECAUSE the user stopped it (`_cancel`'s
            # tree-kill drops `cancelled` first): a stop, not a failure.
            self._write_interrupt_mark()
            result.update({"subtype": "error_during_execution", "is_error": True,
                           "result": "", "stop_reason": "cancelled"})
        elif died or status not in ("completed", "interrupted"):
            if died:
                try:
                    self.cli.wait(timeout=1)   # EOF can beat the exit status
                except subprocess.TimeoutExpired:
                    pass
                tail = _err_tail(self.run_dir)
                text = "codex exited (code %s)%s" % (
                    self.cli.poll(), (": " + tail) if tail else "")
            else:
                text = error or self.turn_error or "codex turn %s" % (status or "failed")
            self._api_error(text)
            # On an error result `_poll` shows `result` as the error and runs
            # the account/limit matchers over it — the error's own words,
            # never a stopped reply's prose (copilot_host's M1).
            result.update({"subtype": "error_during_execution", "is_error": True,
                           "result": text, "stop_reason": "error"})
        elif status == "interrupted":
            self._write_interrupt_mark()
            result.update({"subtype": "error_during_execution", "is_error": True,
                           "result": "", "stop_reason": "cancelled"})
        else:
            if not self.tail_closed:
                # A turn that ended on a tool call: close the transcript's
                # turn with an assistant row carrying no `tool_use`.
                self._emit_tx({"type": "assistant", "message": {
                    "role": "assistant", "model": self.model,
                    "content": [{"type": "text", "text": self.turn_text or ""}],
                    "usage": self._usage()},
                    "isMeta": False, "isSidechain": False})
                self.tail_closed = True
            result.update({"subtype": "success", "is_error": False,
                           "result": self.turn_text or "", "stop_reason": "end_turn"})
        self._emit_tx({"type": "system", "subtype": "turn_duration",
                       "durationMs": duration_ms, "isMeta": False,
                       "isSidechain": False})
        self._emit_out(result)
        self.result_written = True
        self.results += 1

    # ------------------------------------------------------- notifications

    def _on_notification(self, method: str, params: dict) -> None:
        tid = params.get("threadId")
        with self.lock:
            # Another thread's traffic on this connection (a sub-agent Codex
            # spawned) is not this session's turn.
            if isinstance(tid, str) and tid and self.sid and tid != self.sid:
                return
            if method == "item/agentMessage/delta":
                self._delta("text", str(params.get("delta") or ""))
            elif method in ("item/reasoning/summaryTextDelta",
                            "item/reasoning/textDelta"):
                self._on_thinking_delta(method, params)
            elif method == "item/started":
                item = params.get("item")
                if isinstance(item, dict):
                    self._on_item_started(item)
            elif method == "item/completed":
                item = params.get("item")
                if isinstance(item, dict):
                    self._on_item_completed(item)
            elif method == "item/commandExecution/outputDelta":
                self._on_output_delta(params)
            elif method == "thread/tokenUsage/updated":
                self._on_usage(params)
            elif method == "thread/name/updated":
                self._on_title(params)
            elif method == "error":
                self._on_error(params)
            elif method == "turn/completed":
                self._on_turn_completed(params)
            # Everything else (thread/started, thread/status/changed,
            # turn/started, account/*, serverRequest/resolved, configWarning,
            # deprecationNotice, mcpServer/*, remoteControl/*, ...) says
            # nothing the rows carry. Unknown ones are ignored (§3).

    def _on_usage(self, params: dict) -> None:
        usage = params.get("tokenUsage")
        if not isinstance(usage, dict):
            return
        last = usage.get("last")
        if isinstance(last, dict):
            self.last_usage = dict(last)
        window = usage.get("modelContextWindow")
        if isinstance(window, int) and not isinstance(window, bool) and window > 0:
            self.context_window = window
            self._announce_window()

    def _on_title(self, params: dict) -> None:
        """`thread/name/updated` → the transcript's `ai-title`. The generated
        schema names the field `threadName` (the contract assumed `name`;
        both are read)."""
        title = params.get("threadName")
        if not isinstance(title, str):
            title = params.get("name")
        if not isinstance(title, str) or not title.strip():
            return
        if self.started:
            self._emit_tx({"type": "ai-title", "aiTitle": title.strip()})
        else:
            # Before the handshake has written the D18 notice: held, so that
            # notice stays the transcript's first row.
            self.pending_title = title.strip()

    def backfill_turns(self, turns) -> None:
        """D3: turns taken in a terminal (`codex resume <sid>`) are not in
        the fused transcript. The `thread/resume` reply's `thread.turns[]`
        (oldest first, each turn's `items[]` holding its `userMessage` and
        `agentMessage`s — verified) carries every turn; each prompt is looked
        for, in order, among the transcript's own prompts, and the ones not
        found are appended — matched by TEXT, never by count, for
        copilot_host's reason (a fused prompt Codex never recorded must not
        shift every later terminal turn out of the diff). Nothing here goes
        near out.jsonl (invariant 5): the history arrives in a REPLY, not as
        notifications, so there is no replay stream to swallow at all."""
        replay = []
        for turn in turns if isinstance(turns, list) else []:
            if not isinstance(turn, dict):
                continue
            user, said = [], []
            for item in turn.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "userMessage":
                    user.append("".join(
                        str(c.get("text") or "") for c in item.get("content") or []
                        if isinstance(c, dict) and c.get("type") == "text"))
                elif item.get("type") == "agentMessage" and item.get("text"):
                    said.append(str(item["text"]))
            text = "\n\n".join(u for u in user if u)
            if text.strip():
                replay.append((text, "\n\n".join(said), str(turn.get("status") or "")))
        have = []
        try:
            with open(self.transcript_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or row.get("type") != "user":
                        continue
                    if row.get("isMeta") or row.get("isSidechain"):
                        continue
                    msg = row.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, list) and content and all(
                            isinstance(b, dict) and b.get("type") == "text"
                            for b in content):
                        text = "".join(str(b.get("text") or "") for b in content)
                        if text != _INTERRUPT_MARK:
                            have.append(text.strip())
        except OSError:
            pass
        missing, at = [], 0
        for user_text, agent_text, status in replay:
            try:
                at = have.index(user_text.strip(), at) + 1
            except ValueError:
                missing.append((user_text, agent_text, status))
        for user_text, agent_text, status in missing:
            self._emit_tx({"type": "user", "message": {
                "role": "user", "content": [{"type": "text", "text": user_text}]},
                "entrypoint": "sdk-cli", "isMeta": False, "isSidechain": False})
            if not agent_text and status == "interrupted":
                # A terminal turn the user stopped before Codex said anything:
                # closed the way a stopped fused turn is, not with "".
                self._write_interrupt_mark()
                continue
            self._emit_tx({"type": "assistant", "message": {
                "role": "assistant", "model": self.model,
                "content": [{"type": "text", "text": agent_text}],
                "usage": self._usage()},
                "isMeta": False, "isSidechain": False})

    # ---------------------------------------------------------- permissions

    def _on_request(self, rid, method: str, params: dict) -> None:
        kind = _APPROVAL_KINDS.get(method)
        if kind:
            threading.Thread(target=self._serve_permission,
                             args=(rid, kind, params), daemon=True).start()
            return
        # D4: `item/permissions/requestApproval`, `item/tool/requestUserInput`,
        # `mcpServer/elicitation/request`, `item/tool/call`,
        # `account/chatgptAuthTokens/refresh`, the v1 `applyPatchApproval` /
        # `execCommandApproval` (answering those would need the v1 words —
        # invariant 14) and anything newer: refused by the book, which Codex
        # treats as declined (assumed harmless, §10).
        self.peer.respond_error(rid, -32601, "method not supported: %s" % method)

    def _perm_id(self) -> str:
        """permission_server's id format (`HHMMSS-NNN-6hex`): ours, minted,
        path-safe."""
        return "%s-%03d-%s" % (time.strftime("%H%M%S"), next(self._ids),
                               os.urandom(3).hex())

    def _answer_perm(self, rid, decision: str) -> bool:
        """Answer the request — once, with a v2 decision only (invariants 6
        and 14). Both the card's own wait and a cancel can reach for the same
        request; the first to pop it wins."""
        if decision not in _V2_DECISIONS:
            # Unreachable by construction; a word that is not v2 would read
            # as a decline anyway (verified), so say decline outright.
            sys.stderr.write("codex_host: refusing to send decision %r\n" % (decision,))
            decision = "decline"
        with self.lock:
            entry = self.pending_perm.pop(rid, None)
            if entry is None:
                return False
            entry["done"].set()
        offered = entry.get("offered")
        if isinstance(offered, list) and offered and decision not in offered:
            # `availableDecisions` is the server's menu for ITS OWN UI — the
            # verified one for a shell command was ["accept",
            # {acceptWithExecpolicyAmendment}, "cancel"], with neither
            # `decline` nor `acceptForSession` on it — while the schema
            # accepts every v2 word for every request. The contract's word is
            # sent (a deny must not become a `cancel` that kills the turn),
            # but never silently.
            sys.stderr.write("codex_host: answering %r, not among the offered %r\n"
                             % (decision, offered))
        self.peer.respond(rid, {"decision": decision})
        return True

    def _serve_permission(self, rid, kind: str, params: dict) -> None:
        """One approval request, on its own thread. REGISTERED FIRST and the
        whole body guarded: whatever goes wrong in here, the request is still
        answered — once — and Codex is never left blocked on it (invariant
        6)."""
        entry = {"perm_id": "", "done": threading.Event(), "kind": kind,
                 "offered": params.get("availableDecisions")}
        with self.lock:
            self.pending_perm[rid] = entry
            stopping = self.closing or (self.cancelling and self.turn_open)
        try:
            if stopping:
                # Raised while the turn is being interrupted, or while the
                # host is closing: no card for a tool the user has already
                # said stop to. `cancel` — the v2 word that also interrupts
                # the turn, which is what a Stop wants (D4).
                self._answer_perm(rid, "cancel")
                return
            self._park_permission(rid, kind, params, entry)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("codex_host: permission request failed: %r\n" % (exc,))
            # `decline`, not `cancel`: nothing the user did asked to stop the
            # turn, and Codex carries on without the tool.
            if self._answer_perm(rid, "decline") and entry["perm_id"]:
                _post_card(self.run_dir, "card_cleared", entry["perm_id"])

    def _card_tool(self, kind: str, params: dict) -> tuple:
        """`(name, input, item_id)` for the card. The item the request names
        was announced by its own `item/started` (verified: the command's and
        the patch's both arrive first), and that mapping is the card — the
        file card's request carries no change at all, only `{itemId, reason,
        grantRoot}`. A request for an item this turn never announced is
        dressed from its own params."""
        item_id = str(params.get("itemId") or "")
        with self.lock:
            known = self.tools.get(item_id)
        if known is not None:
            name, inp = known
            inp = dict(inp)
        elif kind == "command":
            name, inp = _map_item({"type": "commandExecution", "id": item_id,
                                   "command": params.get("command"),
                                   "commandActions": params.get("commandActions")},
                                  self.cwd)
        else:
            name, inp = _map_item({"type": "fileChange", "id": item_id,
                                   "changes": []}, self.cwd)
            name = "Edit"
        reason = params.get("reason")
        if isinstance(reason, str) and reason:
            inp["reason"] = reason
        return name, inp, item_id

    def _park_permission(self, rid, kind: str, params: dict, entry: dict) -> None:
        name, inp, item_id = self._card_tool(kind, params)
        perm_id = self._perm_id()
        _private_dirs(self.perm_dir)
        # The card goes up UNDER THE LOCK, and only if nothing answered this
        # request first — copilot_host's M1 / PR #1695 reasoning verbatim:
        # either the card parked first and a cancel finds its id (deny on
        # disk, `card_cleared`), or the cancel came first and no card is ever
        # raised.
        with self.lock:
            if entry["done"].is_set():
                return  # answered while this was being built (a cancel, close)
            try:
                _write_atomic(os.path.join(self.perm_dir, perm_id + ".req.json"), {
                    "id": perm_id, "tool": name, "input": inp,
                    "tool_use_id": item_id, "created_at": time.time()})
                parked = True
            except OSError as exc:
                sys.stderr.write("codex_host: cannot park permission: %r\n" % (exc,))
                parked = False
            if parked:
                entry["perm_id"] = perm_id
                _post_card(self.run_dir, "card_raised", perm_id)
        if not parked:
            self._answer_perm(rid, "decline")
            return
        card = self._await_decision(entry)
        if card is None:
            return  # answered elsewhere (a cancel), card already cleared
        decision = _decision(card, kind)
        if card.get("decision") == "allow" and card.get("mode") == "auto":
            # "…and stop asking" (D4/D5): stored BEFORE the answer goes, so
            # a turn the answer lets finish can only be followed by one
            # running `never`. Codex has no mid-turn policy switch: the rest
            # of THIS turn may still ask (assumed acceptable, D4).
            with self.lock:
                self.mode = "auto"
        if self._answer_perm(rid, decision):
            # Only the answer that won announces the card gone: a cancel
            # that beat it here has posted its own `card_cleared`.
            _post_card(self.run_dir, "card_cleared", perm_id)

    def _await_decision(self, entry: dict):
        """permission_server's `_await_answer`, with one extra exit: the
        entry's `done` event, set when a cancel answered this card already.
        Same timeout, same fallback deny written O_EXCL (first writer wins),
        same write-window re-read for a decision landing right now."""
        res_path = os.path.join(self.perm_dir, entry["perm_id"] + ".res.json")
        deadline = time.monotonic() + _PERM_WAIT
        while time.monotonic() < deadline:
            if entry["done"].is_set():
                return None
            decision = _read_json(res_path)
            if decision:
                return decision
            time.sleep(_PERM_POLL)
        fallback = {"decision": "deny", "reason": "timeout"}
        try:
            fd = os.open(res_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            end = time.monotonic() + _DECISION_WRITE_WINDOW
            while time.monotonic() < end:
                decision = _read_json(res_path)
                if decision:
                    return decision
                time.sleep(_PERM_POLL)
            return fallback
        except OSError:
            return fallback
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(fallback, fh)
        except OSError:
            pass  # the latch stays claimed — permission_server's reasoning
        return fallback

    def reject_pending(self, reason: str) -> None:
        """Every outstanding approval → `cancel` (D4: the Stop and the
        teardown are the two roads here, and `cancel` is the v2 word for
        "no, and stop the turn"), its card closed with a deny on disk (first
        writer wins), the queue told. `perm_id` read only AFTER the answer —
        copilot_host's PR #1695 argument."""
        with self.lock:
            items = list(self.pending_perm.items())
        for rid, entry in items:
            if not self._answer_perm(rid, "cancel"):
                continue  # answered already (the page, a racing cancel)
            perm_id = entry["perm_id"]
            if not perm_id:
                continue
            res_path = os.path.join(self.perm_dir, perm_id + ".res.json")
            try:
                fd = os.open(res_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"decision": "deny", "reason": reason}, fh)
            except OSError:
                pass  # the page decided in this instant; the verdict sent stands
            _post_card(self.run_dir, "card_cleared", perm_id)

    # -------------------------------------------------------------- inbox

    def _control_response(self, request_id: str, response=None, error: str = "") -> None:
        body = {"request_id": request_id}
        if error:
            body.update({"subtype": "error", "error": error})
        else:
            body.update({"subtype": "success", "response": response or {}})
        self._emit_out({"type": "control_response", "response": body})

    def drain_inbox(self) -> int:
        """copilot_host's `drain_inbox` verbatim: a user row becomes a
        `turn/start` (or is HELD, left in `inbox/`, while a turn is open —
        D6), a control request is acted on at once whatever the turn is
        doing. Re-listed from disk on every pass, never cached. Returns how
        many user entries are still held."""
        inbox = os.path.join(self.run_dir, "inbox")
        done = os.path.join(inbox, "done")
        try:
            names = sorted(n for n in os.listdir(inbox) if n.endswith(".json"))
        except FileNotFoundError:
            return 0
        held = 0
        for name in names:
            src = os.path.join(inbox, name)
            try:
                with open(src, encoding="utf-8") as fh:
                    row = json.load(fh)
            except (OSError, ValueError):
                continue  # half-written or raced away; the next tick sees it
            if not isinstance(row, dict):
                row = {}
            if row.get("type") == "user":
                with self.lock:
                    hold = self.turn_open or held
                if hold:
                    held += 1
                    continue
                self.send_prompt(self._user_text(row), str(row.get("uuid") or ""))
            elif row.get("type") == "control_request":
                self._handle_control(row)
            if not os.path.isdir(done):
                _private_dirs(done)
            try:
                os.replace(src, os.path.join(done, name))
            except OSError:
                pass
        return held

    @staticmethod
    def _user_text(row: dict) -> str:
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(b.get("text") or "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
        return ""

    def _handle_control(self, row: dict) -> None:
        request_id = str(row.get("request_id") or "")
        request = row.get("request")
        request = request if isinstance(request, dict) else {}
        subtype = str(request.get("subtype") or "")
        if subtype == "interrupt":
            with self.lock:
                cancel = self.turn_open
                turn_id = self.turn_id
                if cancel:
                    # Set BEFORE the interrupt goes out, so a card Codex
                    # raises while it winds down is refused at once.
                    self.cancelling = True
                    self.cancel_deadline = time.monotonic() + _CANCEL_GRACE_SECONDS
                    # No id yet (the turn/start reply is still in flight):
                    # `_on_turn_started` sends it the moment one is known.
                    self.interrupt_wanted = not turn_id
                # Answered NOW, as Claude and copilot_host answer it — see
                # the long comment there — and BEFORE the interrupt goes out:
                # Codex completes an interrupted turn within milliseconds
                # (and a card answered `cancel` interrupts it too), so
                # answered after, the stop's own `result` could land first.
                # Readers take either order; out.jsonl gets one. Nothing
                # reaches Codex's own queue (held follow-ups stay in inbox/),
                # so there is nothing `still_queued`.
                self._control_response(request_id, {"still_queued": []})
            if cancel:
                if turn_id:
                    # D6: `turn/interrupt {threadId, turnId}` → `{}`, then
                    # `turn/completed status: interrupted` (verified). Its
                    # reply is not waited for: the completion is the answer
                    # that matters, and `_reap_cancel` covers its absence.
                    self.peer.request("turn/interrupt",
                                      {"threadId": self.sid, "turnId": turn_id})
                self.reject_pending("cancelled")
        elif subtype == "set_model":
            # D7 / invariant 15: stored, used by the next `turn/start`,
            # answered at once — no respawn, no RPC (Codex takes the model
            # per turn). An `effort` riding the same request is taken too.
            model = request.get("model")
            effort = request.get("effort")
            with self.lock:
                if isinstance(model, str) and model.strip():
                    self.model = model.strip()
                if isinstance(effort, str) and effort.strip():
                    self.effort = effort.strip()
                self._control_response(request_id, {})
        elif subtype == "set_permission_mode":
            # D5 / invariant 15: the same — the next `turn/start` carries the
            # new approvalPolicy + sandboxPolicy.
            with self.lock:
                self.mode = _fused_mode(request.get("mode"))
                self._control_response(request_id, {})
        else:
            with self.lock:
                self._control_response(request_id, error=_UNSUPPORTED)

    # ------------------------------------------------------------ lifecycle

    def _developer_instructions(self) -> str:
        """D8: the prompt every backend is appended for this target, as the
        thread's `developerInstructions`. agent.py's own builder, so Codex
        is never told something different from Claude about the same target.
        A failure costs the session its context note, never the session."""
        try:
            return str(self.agent._appended_prompt(
                str(self.req.get("file") or self.cwd), bool(self.req.get("pane")),
                BACKEND) or "")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("codex_host: no developer instructions: %r\n" % (exc,))
            return ""

    def handshake(self) -> bool:
        """`initialize` → `initialized` → `thread/start` | `thread/resume`.
        False (with the failure already written as a result row) when Codex
        never came up.

        `initialized` goes out after the `initialize` reply and BEFORE any
        other request (invariant 13) — the protocol's order. (0.163.0 was
        measured answering an `account/read` sent before it anyway, so the
        order is kept for the protocol, not because this server enforces
        it.)"""
        rpc = self.peer.request("initialize", {"clientInfo": dict(_CLIENT_INFO)})
        if not rpc.done.wait(_HANDSHAKE_SECONDS) or rpc.error is not None:
            return self._handshake_failed("initialize", rpc)
        self.peer.notify("initialized")
        policy, sandbox = self._policy()
        params = {"cwd": self.cwd, "approvalPolicy": policy, "sandbox": sandbox}
        instructions = self._developer_instructions()
        if instructions:
            params["developerInstructions"] = instructions
        if self.resume:
            params["threadId"] = self.sid
            step = "thread/resume"
        else:
            if self.model:
                params["model"] = self.model
            step = "thread/start"
        rpc = self.peer.request(step, params)
        if not rpc.done.wait(_HANDSHAKE_SECONDS) or rpc.error is not None:
            return self._handshake_failed(step, rpc)
        reply = rpc.result if isinstance(rpc.result, dict) else {}
        thread = reply.get("thread") if isinstance(reply.get("thread"), dict) else {}
        if not self.model:
            # An unnamed spawn (the scheduler, the apps API) lets Codex pick:
            # the init row, the D18 notice and every message then name the
            # model it picked, not "".
            got_model = reply.get("model") or thread.get("model")
            if isinstance(got_model, str):
                self.model = got_model
        if not self.resume:
            got = thread.get("id")
            if not isinstance(got, str) or self.agent._bad_id(got):
                # The id names files on disk: one a path could be built out
                # of is refused, by agent.py's own rule for session ids.
                rpc.error = {"code": -32603,
                             "message": "no usable thread id in the reply: %r" % (got,)}
                return self._handshake_failed(step, rpc)
            with self.lock:
                self.sid = got
                self.transcript_path = self._transcript_path()
            # The session-settings record (model, effort, backend — D2)
            # BEFORE anything names the id: copilot_host's order and reason.
            try:
                self.agent._record_settings(self.sid, self.model, self.effort, BACKEND)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write("codex_host: cannot record settings: %r\n" % (exc,))
            self._write_host_json()
        with self.lock:
            if self.resume:
                self.backfill_turns(thread.get("turns"))
            self.write_init()
            if not self.resume:
                self.write_first_turn_notice()
            self.started = True
            if self.pending_title:
                self._emit_tx({"type": "ai-title", "aiTitle": self.pending_title})
                self.pending_title = ""
            self._write_registry("idle")
        return True

    def _write_host_json(self) -> None:
        """host.json's `session_id`, once Codex has named a fresh thread.
        Read-modify-write, atomic — copilot_host's reasoning."""
        path = os.path.join(self.run_dir, "host.json")
        host = _read_json(path)
        if not host:
            return  # reaped, or never written: nothing to name
        host["session_id"] = self.sid
        try:
            _write_atomic(path, host)
        except OSError:
            pass

    def _handshake_failed(self, step: str, rpc: _Rpc) -> bool:
        err = rpc.error or {"message": "no answer within %ds" % _HANDSHAKE_SECONDS}
        text = "codex %s failed: %s" % (step, err.get("message") or err)
        if err.get("message") in _DIED:
            # The CLI refused to start (a bad `-c`, a broken install): its
            # exit code and its own stderr are the whole story.
            try:
                self.cli.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            text = "codex exited during %s (code %s)" % (step, self.cli.poll())
        tail = _err_tail(self.run_dir)
        if tail:
            text += "\n" + tail
        with self.lock:
            # A fresh thread that never answered has no id: the init row says
            # so (""), and no transcript or registry row is ever written.
            if not self.resume and not self.started:
                self.sid = ""
                self.transcript_path = ""
            self.write_init()
            message = {"role": "assistant", "model": "<synthetic>",
                       "content": [{"type": "text", "text": text}],
                       "usage": self._usage()}
            self._emit_out({"type": "assistant", "isApiErrorMessage": True,
                            "message": message})
            self._emit_out({"type": "result", "subtype": "error_during_execution",
                            "is_error": True, "result": text, "stop_reason": "error",
                            "num_turns": 0, "usage": self._usage(),
                            "backend": BACKEND})
        self._retire_inbox()
        return False

    def _retire_inbox(self) -> None:
        """A run that never came up retires its user entries to `inbox/done/`
        (copilot_host's M7): nothing will ever send them, and an undrained
        entry is what the page draws as a queued bubble on a dead run."""
        inbox = os.path.join(self.run_dir, "inbox")
        done = os.path.join(inbox, "done")
        try:
            names = sorted(n for n in os.listdir(inbox) if n.endswith(".json"))
        except OSError:
            return
        for name in names:
            src = os.path.join(inbox, name)
            if _read_json(src).get("type") != "user":
                continue
            try:
                _private_dirs(done)
                os.replace(src, os.path.join(done, name))
            except OSError:
                pass

    def run(self) -> None:
        """Drain, watch, reap — copilot_host's `run`, and its reap race
        closed the same way: `host.json` goes FIRST, then one more pass runs
        whatever landed to its `result` before stdin closes (EOF ends the
        app-server)."""
        try:
            if not self.handshake():
                return
            self._serve(reaping=False)
            self._remove_host_json()
            self._serve(reaping=True)
        except Exception as exc:  # noqa: BLE001 — favour reaping over hanging
            try:
                with _append_private(os.path.join(self.run_dir, "err.log")) as f:
                    f.write(("\ncodex_host: %r\n" % (exc,)).encode("utf-8"))
            except OSError:
                pass
        finally:
            self.close()

    def _serve(self, reaping: bool) -> None:
        """Until the session goes idle for `_IDLE_REAP_SECONDS` — or, when
        `reaping`, until it is idle at all — or the CLI is gone."""
        idle_since = None
        while self.cli.poll() is None and not self.peer.dead.is_set():
            self._reap_cancel()
            held = self.drain_inbox()
            with self.lock:
                busy = self.turn_open or held or bool(self.pending_perm)
            if busy:
                idle_since = None
            elif reaping:
                return
            elif idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= _IDLE_REAP_SECONDS:
                return
            time.sleep(_DRAIN_INTERVAL_SECONDS)

    def _reap_cancel(self) -> None:
        """An interrupted turn Codex never completed (`_CANCEL_GRACE_SECONDS`
        after the Stop): settled HERE as the interrupt it was, and the CLI
        killed, since a server that ignores an interrupt cannot be trusted
        with the next turn. A `turn/completed` that comes after finds the
        turn closed and is dropped."""
        with self.lock:
            due = self.turn_open and self.cancel_deadline is not None \
                and time.monotonic() >= self.cancel_deadline
        if not due:
            return
        sys.stderr.write("codex_host: interrupted turn unanswered after %gs; "
                         "killing codex\n" % _CANCEL_GRACE_SECONDS)
        self._end_turn("interrupted")
        try:
            self.cli.kill()
        except OSError:
            pass

    def _remove_host_json(self) -> None:
        try:
            os.remove(os.path.join(self.run_dir, "host.json"))
        except OSError:
            pass

    def close(self) -> None:
        # `closing` first, under the lock: a request arriving from here on is
        # refused without a card, and every one already registered is in the
        # snapshot `reject_pending` takes next.
        with self.lock:
            self.closing = True
        self.reject_pending("cancelled")
        with self.lock:
            if self.turn_open:
                # Invariant 2 from the other side: the loop ended (it threw,
                # or the CLI is gone and EOF has not been read yet) with a
                # turn still open. The EOF hook normally settles it; this is
                # the backstop for the loop getting here first.
                self._end_turn("", "", died=True)
        try:
            self.cli.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        self._remove_host_json()
        try:
            os.remove(self.registry_path)
        except OSError:
            pass
        # Reap: stdin is closed above, and the app-server exits on that EOF
        # (verified). Wait, then kill one that hangs.
        try:
            self.cli.wait(timeout=_EXIT_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                self.cli.kill()
                self.cli.wait(timeout=_EXIT_WAIT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
        exited_at = time.time()
        with self.lock:
            self.closed = True
        for fh in (self._out, self._tx):
            if fh is None:
                continue  # no transcript was ever written
            try:
                fh.close()
            except OSError:
                pass
        last = _post_event(self.run_dir, "exited", code=self.cli.poll(),
                           at=exited_at, pid=self.cli.pid,
                           session_id=self.sid or _meta_session_id(self.run_dir))
        if last is not None:
            try:
                last.join(_EVENT_TIMEOUT + 1.0)
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    _enable_faulthandler()
    if hasattr(os, "setsid"):
        try:
            os.setsid()
        except OSError:
            pass
    # A SIGTERM (a tree-kill's first signal) unwinds as SystemExit, so
    # `_Host.run`'s finally still runs `close()`: the open turn gets its
    # result, every card its answer, the registry row its removal.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    _close_inherited_fds()
    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    run_dir = req["run_dir"]
    # A Stop that landed before setsid() — session_host's D1310 window.
    if os.path.exists(os.path.join(run_dir, "cancelled")):
        sys.stderr.write("codex_host: run cancelled before the CLI spawned\n")
        return
    agent = _load_agent(req["agent"])
    argv = list(req["argv"])
    # Codex's env, not Claude's (§3): the `CLAUDE_CODE_*` switches stripped,
    # `CODEX_HOME` left exactly as found (it is where the credential lives),
    # the fused CLI first on PATH. No chat id: there is no shell-prefix
    # mirror for Codex (D9).
    env = agent._spawn_env("", backend=BACKEND)

    err_fh = _append_private(os.path.join(run_dir, "err.log"))
    try:
        cli = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=err_fh, cwd=req["cwd"], env=env, **agent._DETACH)
    except Exception as exc:  # noqa: BLE001
        err_fh.write(str(exc).encode("utf-8", "replace"))
        return
    finally:
        err_fh.close()

    with agent._private_open(os.path.join(run_dir, "pid")) as f:
        f.write(str(cli.pid))
    with agent._private_open(os.path.join(run_dir, "host.json")) as f:
        json.dump({
            "pid": os.getpid(),
            # A resume's id; "" for a fresh chat until `thread/start` names
            # it (`_Host._write_host_json`).
            "session_id": req.get("session_id") or "",
            "file": req.get("file", ""), "mode": req.get("cli_mode", ""),
            "model": req.get("model", ""), "effort": req.get("effort", ""),
            "read_dirs": req.get("extra_read_dirs", []),
            "backend": BACKEND,
        }, f)

    _Host(agent, req, cli, _Peer(cli)).run()


if __name__ == "__main__":
    main()
