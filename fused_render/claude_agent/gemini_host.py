"""A session host for GOOGLE GEMINI CLI — the Gemini twin of copilot_host.py.

Gemini CLI speaks the Agent Client Protocol too (`gemini --acp`: JSON-RPC,
newline-delimited, over stdio), streams a turn as `session/update`
notifications, asks the CLIENT for every permission, and persists nothing
fused can read in place (its chat files under `~/.gemini/tmp` are its own
resume format). So this process is BOTH halves at once — the ACP client, and
the writer of every file the rest of the app reads for a Claude session —
exactly as `copilot_host.py` is, and that file is the one to read first:
this one is a copy of it with Gemini's own facts in (docs/GEMINI_BACKEND.md):

  * the session id is Gemini's own (`session/new` mints a uuid4), learned
    from the reply as Copilot's is; a resume is `session/load`, whose replay
    is backfilled into the transcript the same way (D2, D3);
  * the approval mode is `session/set_mode` with Gemini's ids — `default`,
    `autoEdit`, `yolo` (`_GEMINI_MODES`, D5) — set after the handshake and
    on every `set_permission_mode` control request; there is no `allow_all`
    config option;
  * the model is LIVE: a `set_model` control request becomes
    `session/set_model {modelId}` (D7), so `_send` never respawns for it;
  * a `tool_call` carries no `rawInput` — only `title`, `kind`, `locations`
    and `content` — so the mapping (D14) reads those, and an MCP tool's
    title is `<tool> (<server> MCP Server)`;
  * the pane MCP server (`app_state`, `terminal_*`) rides `session/new`'s
    own `mcpServers` list, built from the run dir's `mcp.json` (D8);
  * a Google login whose token cannot be refreshed makes the CLI start its
    OAuth flow INSIDE `session/new`; with `NO_BROWSER=1` on the spawn env
    that flow prints "Please visit the following URL to authorize" on stdout
    and waits on stdin — the host reads that line as "signed out" and fails
    the handshake at once instead of waiting the handshake bound out (D13).

Everything else — the row contract, the readers' three rules, the thread
model, the permission round trip through `perm/`, the inbox, the reap — is
copilot_host.py's, verbatim, and its header is the place that argues each.
Loaded and run BY PATH, exactly like the other hosts (`sys.executable
gemini_host.py`, the JSON request on stdin). Stdlib only, never imports
`fused_render`.
"""
import difflib
import importlib.util
import re
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

BACKEND = "gemini"
PROTOCOL_VERSION = 1

# Same two knobs as session_host.py, by the same names, so a test (or an
# operator) shrinks both hosts with one setting.
_IDLE_REAP_SECONDS = float(
    os.environ.get("FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS", "30"))
_DRAIN_INTERVAL_SECONDS = float(
    os.environ.get("FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS", "0.2"))
# How long `initialize` + `session/new|load` may take before the run is
# reported dead. Gemini's first start (a token refresh, MCP and extension
# discovery) is slow; a session that never answers at all is the case this
# bounds.
_HANDSHAKE_SECONDS = float(
    os.environ.get("FUSED_GEMINI_HOST_HANDSHAKE_SECONDS", "120"))
# The reap is close stdin → wait → kill: the ACP server exits on stdin EOF
# and tears its own MCP children down, so the kill is only for one that
# hangs. And how long the mode set after the handshake may hold the first
# prompt back — a reply that never comes must not wedge the session.
_EXIT_WAIT_SECONDS = 5.0
_CONFIG_WAIT_SECONDS = 5.0
# How long a cancelled prompt may take to answer before the host stops
# waiting: it settles the turn itself and kills the CLI (`_reap_cancel`).
_CANCEL_GRACE_SECONDS = float(
    os.environ.get("FUSED_GEMINI_HOST_CANCEL_GRACE_SECONDS", "15"))
# Gemini's own approval-mode ids (`session/set_mode`), by the CLI-wire mode
# the request carries (`cli_mode`: `_start`'s PERMISSION_MODES value). A COPY
# of `agent._GEMINI_MODES`, pinned equal by a test. `prompt` is `default`.
_GEMINI_MODES = {"default": "default", "acceptEdits": "autoEdit", "auto": "yolo"}
# What the CLI's OAuth flow prints on STDOUT (not stderr) when it needs a
# browser and `NO_BROWSER` says no: the one line that means "signed out" in
# the middle of a `session/new` (verified in 0.65.0's source).
_OAUTH_PROMPT_MARK = "Please visit the following URL to authorize"
_SIGNED_OUT_TEXT = ("gemini is not signed in: the CLI asked for a browser "
                    "sign-in during session/new")
# The permission wait mirrors permission_server.py: same env knob, same poll,
# same write window — the card on the other side is the same card.
_PERM_WAIT = float(os.environ.get("FUSED_RENDER_PERMISSION_TIMEOUT", "3600"))
_PERM_POLL = 0.15
_DECISION_WRITE_WINDOW = 2.0
_UNSUPPORTED = "unsupported by gemini"
# Claude Code's own stop marker, as the tasks readers spell it
# (tasks_store._INTERRUPT_MARKS). Written to the transcript on a cancelled
# prompt so `_absorb` says "Interrupted by you" and `transcript_turn_open`
# reads the turn as closed — the contract's §4 table has no transcript row
# for that case, and without one a stopped turn reads open for
# STALE_TAIL_SEC.
_INTERRUPT_MARK = "[Request interrupted by user]"
# ACP tool kinds → the Claude tool names the page already renders (D14).
_KIND_TO_TOOL = {"execute": "Bash", "read": "Read", "fetch": "WebFetch",
                 "search": "Grep"}
# How Gemini titles an MCP tool call: `<tool> (<server> MCP Server)`.
_MCP_TITLE = re.compile(r"^(?P<tool>[A-Za-z0-9_.-]+) \((?P<server>[A-Za-z0-9_.-]+) MCP Server\)$")
_MCP_SERVER = "fused_approvals"
_EDIT_TOOLS = frozenset({"Edit", "Write"})
_FD_CLOSE_CAP = 65536

_EVENT_PATH = "/api/tasks/queue/event"
_EVENT_TIMEOUT = 5.0
_EVENT_RETRY_SECONDS = 1.0


# ----------------------------------------------------------------- plumbing

def _now_iso() -> str:
    """ISO 8601 UTC with a `Z`, the way Claude Code stamps every record —
    `tasks_store.epoch` and `agent._row_ts` parse exactly this."""
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + \
        ".%03dZ" % int((t % 1) * 1000)


def _load_agent(path: str):
    """agent.py by file path — mirrors `session_host._load_agent`."""
    spec = importlib.util.spec_from_file_location("claude_agent_gemini_host", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _append_private(path: str):
    """Binary append, created 0600 if new (session_host's own helper)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "ab")


def _private_dirs(path: str) -> None:
    """`makedirs` with 0700 on every level this call creates. Not
    `agent._private_dir`: that one is anchored at the runs root and makes an
    EXCLUSIVE leaf, and neither fits a transcript bucket under
    `gemini_dir()` that outlives every run and is shared by every turn."""
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


# ------------------------------------------------------------- the ACP wire

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
    """The CLI's stdio as JSON-RPC: one writer lock, one reader thread.

    Replies are routed to their `_Rpc` by id; notifications and inbound
    requests are handed to the callbacks the host installs. On EOF every
    pending request is failed (so an open prompt ends in a `result`, invariant
    2) and `dead` is set, which is the main loop's cue to leave."""

    def __init__(self, cli):
        self.cli = cli
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending = {}
        self._pending_lock = threading.Lock()
        self.dead = threading.Event()
        self.on_notification = lambda method, params: None
        self.on_request = lambda rid, method, params: None
        # A stdout line that is not JSON: the CLI's own prose (the OAuth
        # flow's "Please visit…", a warning). Copilot's host drops these;
        # here the host reads one of them (`_OAUTH_PROMPT_MARK`).
        self.on_text = lambda line: None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)

    def start(self) -> None:
        """Begin reading — AFTER the host has installed its callbacks, or the
        first `available_commands_update` lands on the no-op defaults."""
        self._reader.start()

    def _send(self, msg: dict) -> bool:
        # ASCII-escaped, like every line this host writes: a lone surrogate
        # (half an emoji, cut by a chunk boundary or a paste) is valid JSON
        # as `\ud83d` and raises as raw UTF-8.
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
            rpc.settle(error={"code": -32000, "message": "gemini is not running"})
        return rpc

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

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
                    try:
                        self.on_text(line)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self._dispatch(msg)
                except Exception as exc:  # noqa: BLE001 — one bad message must not end the session
                    sys.stderr.write("gemini_host: dispatch failed: %r\n" % (exc,))
        finally:
            with self._pending_lock:
                pending, self._pending = list(self._pending.values()), {}
            for rpc in pending:
                rpc.settle(error={"code": -32000, "message": "gemini exited"})
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

def _first_path(raw: dict, locations) -> str:
    for key in ("file_path", "path", "filePath", "file"):
        v = raw.get(key)
        if isinstance(v, str) and v:
            return v
    if isinstance(locations, list):
        for loc in locations:
            if isinstance(loc, dict) and isinstance(loc.get("path"), str) and loc["path"]:
                return loc["path"]
    return ""


def _unified_diff(diffs: list) -> str:
    """ACP `diff` content entries as one unified diff, HEADER-FREE (no
    `---`/`+++` lines — the chat's diff view draws hunks, and the path is
    already the card's `file_path`). "" when there are none."""
    out = []
    for d in diffs:
        old = str(d.get("oldText") or "")
        new = str(d.get("newText") or "")
        lines = difflib.unified_diff(old.splitlines(keepends=True),
                                     new.splitlines(keepends=True), n=3)
        for line in lines:
            if line.startswith(("--- ", "+++ ")):
                continue
            out.append(line if line.endswith("\n") else line + "\n")
    return "".join(out)


def _map_tool(kind: str, title: str, raw, locations=None, cwd: str = "",
              content=None) -> tuple:
    """`(name, input)` for one ACP tool call, per D14: the Claude tool name the
    chat already has a card and a chip for, with the fields that card reads,
    plus `_acp: {kind, title}` (and `raw`, the call's own input, only for a
    kind with no Claude twin — a mapped one already carries every field it
    has). Gemini sends no `rawInput`, so `title` (the tool's own description
    of the call — a shell command's command line) and `locations` carry
    what the card shows. An MCP tool is titled `<tool> (<server> MCP
    Server)`; fused's own server becomes `mcp__fused_approvals__<tool>` (the
    name agent.py pre-allows and `_segments_from_rows` strips), another
    server's `<server>-<tool>`, Copilot's spelling."""
    raw = raw if isinstance(raw, dict) else ({} if raw is None else {"value": raw})
    kind = str(kind or "")
    title = str(title or "")
    acp = {"kind": kind, "title": title}
    name = _KIND_TO_TOOL.get(kind)
    if kind == "execute":
        inp = {"command": str(raw.get("command") or raw.get("cmd") or title),
               "description": str(raw.get("description") or title)}
    elif kind == "edit":
        diffs = [c for c in (content if isinstance(content, list) else [])
                 if isinstance(c, dict) and c.get("type") == "diff"]
        path = _first_path(raw, locations) or next(
            (str(d["path"]) for d in diffs if isinstance(d.get("path"), str) and d["path"]), "")
        full = path if os.path.isabs(path) else os.path.join(cwd or "", path)
        if path and not os.path.exists(full):
            name = "Write"
            inp = {"file_path": path,
                   "content": str(raw.get("content") or raw.get("newText")
                                  or raw.get("new_text")
                                  or "".join(str(d.get("newText") or "") for d in diffs))}
        else:
            name = "Edit"
            # The diff the card shows is what the user approves: from the
            # call's input when it has one, else from the ACP `content`
            # diff entries (`{type:"diff", path, oldText, newText}` — how
            # Copilot usually sends an edit), never an empty one.
            inp = {"file_path": path,
                   "diff": str(raw.get("diff") or raw.get("newText")
                               or raw.get("new_text") or _unified_diff(diffs))}
    elif kind == "read":
        inp = {"file_path": _first_path(raw, locations)}
    elif kind == "fetch":
        inp = {"url": str(raw.get("url") or title)}
    elif kind == "search":
        inp = {"pattern": str(raw.get("pattern") or raw.get("query") or title),
               "path": str(raw.get("path") or _first_path(raw, locations) or "")}
    else:
        inp = dict(raw)
        acp["raw"] = raw
        m = _MCP_TITLE.match(title)
        if m and m.group("server") == _MCP_SERVER:
            name = "mcp__%s__%s" % (_MCP_SERVER, m.group("tool"))
        elif m:
            name = "%s-%s" % (m.group("server"), m.group("tool"))
        else:
            name = title or kind or "tool"
    inp["_acp"] = acp
    return name, inp


def _tool_output_text(update: dict) -> str:
    """One string out of a `tool_call_update`'s `content[]` / `rawOutput`."""
    parts = []
    for item in update.get("content") or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "content":
            block = item.get("content")
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        elif t == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif t == "diff":
            parts.append("--- %s\n-%s\n+%s" % (item.get("path", ""),
                                               item.get("oldText", ""),
                                               item.get("newText", "")))
    if not parts:
        raw = update.get("rawOutput")
        if isinstance(raw, str):
            parts.append(raw)
        elif raw is not None:
            parts.append(json.dumps(raw))
    return "\n".join(parts)


def _pick_option(options, kinds) -> str:
    """The `optionId` of the first option whose `kind` is in `kinds`, in the
    order given, or "". By KIND, never by id: the ids are the agent's to
    choose and only the kind is protocol."""
    if not isinstance(options, list):
        return ""
    for kind in kinds:
        for opt in options:
            if isinstance(opt, dict) and opt.get("kind") == kind \
                    and isinstance(opt.get("optionId"), str):
                return opt["optionId"]
    return ""


def _permission_outcome(options, decision: dict) -> dict:
    """D4: allow + scope session → allow_always, allow → allow_once,
    everything else (deny, expired, cancelled, timeout) → reject_once. An
    option list that lacks the wanted kind falls to its neighbour, and one
    with no usable option at all is answered `cancelled`, which the agent
    reads as a refusal."""
    if decision.get("decision") == "allow":
        kinds = ("allow_always", "allow_once") if decision.get("scope") == "session" \
            else ("allow_once", "allow_always")
    else:
        kinds = ("reject_once", "reject_always")
    oid = _pick_option(options, kinds)
    if not oid:
        return {"outcome": {"outcome": "cancelled"}}
    if kinds[0] == "allow_once" and not _pick_option(options, ("allow_once",)):
        # The only allow on offer is WIDER than the one the user gave. The
        # narrowest offered is still the answer (refusing a tool the user
        # allowed would be worse), but never silently.
        sys.stderr.write("gemini_host: no allow_once offered; answering "
                         "allow_always for a once-only allow\n")
    return {"outcome": {"outcome": "selected", "optionId": oid}}


# ------------------------------------------------------------------- the host

class _Host:
    def __init__(self, agent, req: dict, cli, peer: _Peer):
        self.agent = agent
        self.req = req
        self.cli = cli
        self.peer = peer
        self.run_dir = req["run_dir"]
        self.cwd = req["cwd"]
        self.model = str(req.get("model") or "")
        # THE SESSION — the key of every file this host writes. A resume
        # names it; a fresh chat has none until `session/new` answers, because
        # Gemini MINTS it (a uuid4 in `session/new`, verified in the source),
        # so it is set in `handshake` and nothing keyed on it is written
        # before. `new_session_id` is not read: that is Claude's fused-minted
        # id.
        self.sid = str(req.get("session_id") or "")
        self.resume = bool(self.sid)
        self.lock = threading.RLock()
        self._out = _append_private(os.path.join(self.run_dir, "out.jsonl"))
        self.transcript_path = self._transcript_path()
        # Opened on the first record, not here: a session that never comes
        # up (signed out, a dead binary) must leave no empty transcript for
        # the tasks scan to list.
        self._tx = None
        self.parent_uuid = None
        self.registry_path = os.path.join(agent_roots.sessions_dir(BACKEND),
                                          "%d.json" % cli.pid)
        _private_dirs(os.path.dirname(self.registry_path))
        self.perm_dir = os.path.join(self.run_dir, "perm")
        # Turn state (§5 of the contract).
        self.turn_open = False
        self.turn_started = 0.0
        self.turn_started_at = 0.0
        self.turn_text = None         # text of the last closed text message
        self.turn_messages = 0        # assistant messages this turn (num_turns)
        self.tail_closed = True       # transcript's last row is a text assistant row
        self.msg_open = False         # a message_start has been emitted
        self.text_parts = []
        self.thinking_parts = []
        self.last_usage = {}          # newest usage_update {used, size}
        self.context_window = None
        self.replaying = False
        self.replay_turns = []
        self.replay_user = None
        self.replay_agent = []
        self.replay_gap = False       # a non-prompt update since the last prompt chunk
        # The Stop road (§5): set when `session/cancel` goes out, cleared when
        # the turn ends. `cancel_deadline` is when a cancelled prompt that
        # never answered is settled by the host itself (`_reap_cancel`).
        self.cancelling = False
        self.cancel_deadline = None
        self.pending_perm = {}        # rpc id -> {"perm_id", "done": Event, "options"}
        self.slash_commands = []
        self.commands_dirty = False   # the list moved after the init row
        self.results = 0              # result rows written so far
        self.result_written = False   # this prompt's result is on disk
        self.started = False          # handshake done, init row written
        self.closing = False          # close() began: no new cards
        self.closed = False           # files closed; late notifications dropped
        self.pending_title = ""
        self.notice_pending = False   # D18 notice owed to out.jsonl's first turn
        self.tools = {}               # toolCallId -> (name, input)
        # The CLI-wire mode this session runs in (`cli_mode`), re-sent as
        # Gemini's own id on every switch (D5); `set_permission_mode` moves it.
        self.mode = str(req.get("cli_mode") or "default")
        self.oauth_prompted = False   # the CLI asked for a browser sign-in
        self._ids = itertools.count(1)
        peer.on_notification = self._on_notification
        peer.on_request = self._on_request
        peer.on_text = self._on_text
        peer.start()

    # ------------------------------------------------------------ writers

    def _transcript_path(self) -> str:
        """`<projects>/<munge(cwd)>/<sid>.jsonl`, "" while the id is unknown.
        A resume continues the transcript it already has, in whatever bucket
        that is (a relocated folder's session keeps its old bucket until the
        move rewrites it): the tasks readers know a session by ONE file, and a
        second, fresh one under the new cwd would split its history."""
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

    def _usage(self) -> dict:
        used = self.last_usage.get("used")
        return {"input_tokens": int(used) if isinstance(used, (int, float)) else 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "output_tokens": 0}

    def _write_registry(self, status: str) -> None:
        try:
            _write_atomic(self.registry_path, {
                "sessionId": self.sid, "cwd": self.cwd, "status": status,
                "updatedAt": _now_iso(), "pid": self.cli.pid})
        except OSError:
            pass

    def write_init(self) -> None:
        """The FIRST row of out.jsonl (invariant 1), after the handshake."""
        row = {"type": "system", "subtype": "init", "session_id": self.sid,
               "cwd": self.cwd, "model": self.model, "backend": BACKEND,
               "permissionMode": self.mode,
               "slash_commands": list(self.slash_commands), "skills": [],
               "tools": []}
        if self.context_window:
            row["context_window"] = self.context_window
        self._emit_out(row)
        self.commands_dirty = False

    def _notice_row(self) -> dict:
        return {"type": "system", "subtype": "task_notification",
                "summary": "Handled by Google Gemini · model %s" % self.model,
                "backend": BACKEND, "model": self.model}

    def write_first_turn_notice(self) -> None:
        """D18: the transcript's own record that Gemini answers here — its
        first row. The LIVE copy waits for the first echo (`send_prompt`)."""
        self._emit_tx(dict(self._notice_row(), isMeta=False, isSidechain=False))
        self.notice_pending = True

    # ------------------------------------------------------------- the turn

    def send_prompt(self, text: str, msg_uuid: str = "") -> None:
        """Echo first, then `session/prompt` (invariant 3). The state and the
        echo go under the lock, so the reader thread cannot see a reply before
        the turn reads open; the pipe write goes OUTSIDE it — a prompt larger
        than the pipe buffer, written while the reader is blocked on this
        same lock with Gemini's stdout full, would deadlock both.

        `msg_uuid` is the stamp `agent._write_inbox_entry` put on the inbox
        row, carried onto the echo the way Claude Code's `--replay-user-messages`
        carries it, so `agent._drained_unechoed` settles the entry by identity
        rather than by its words (PR #1778)."""
        with self.lock:
            self.turn_open = True
            self.turn_started = time.monotonic()
            self.turn_started_at = time.time()   # wall clock, against file mtimes
            self.turn_text = None
            self.turn_messages = 0
            self.tail_closed = False
            self.result_written = False
            if self.commands_dirty:
                # Ahead of the echo, where Claude Code writes its own init.
                self.write_init()
            self._emit_out({"type": "user",
                            **({"uuid": msg_uuid} if msg_uuid else {}),
                            "message": {"role": "user", "content": [
                                {"type": "text", "text": text}]}})
            if self.notice_pending:
                # D18 LIVE: the same row into out.jsonl, so the first poll
                # draws it (`_segments_from_rows` makes a `system/
                # task_notification` a notice chip). Right AFTER the echo,
                # never before: the echo is what opens the turn and lifts
                # `pending_echo`; inside the turn this row moves nothing —
                # `idle` is already False, it carries no `task_id` for the
                # task registry, and the `result` still closes the turn.
                self._emit_out(self._notice_row())
                self.notice_pending = False
            self._emit_tx({"type": "user", "message": {
                "role": "user", "content": [{"type": "text", "text": text}]},
                "entrypoint": "sdk-cli", "isMeta": False, "isSidechain": False})
            self._write_registry("busy")
        self.peer.request("session/prompt", {
            "sessionId": self.sid,
            "prompt": [{"type": "text", "text": text}]},
            on_done=self._on_prompt_reply)

    def _open_message(self) -> None:
        if self.msg_open:
            return
        self.msg_open = True
        self._emit_out({"type": "stream_event", "event": {
            "type": "message_start",
            "message": {"role": "assistant", "model": self.model,
                        "content": [], "usage": self._usage()}}})

    def _close_message(self, output_tokens: int = 0) -> None:
        """End the streamed message: `message_delta`, `message_stop`, then the
        finalized `assistant` row the transcript keeps (and `_poll` reads
        the usage off). `output_tokens` is known only for the turn's LAST
        message — the prompt reply's usage, folded in by `_finish_turn`;
        ACP reports none per message."""
        if not self.msg_open:
            return
        self.msg_open = False
        text = "".join(self.text_parts)
        thinking = "".join(self.thinking_parts)
        self.text_parts, self.thinking_parts = [], []
        self._emit_out({"type": "stream_event", "event": {
            "type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": output_tokens}}})
        self._emit_out({"type": "stream_event", "event": {"type": "message_stop"}})
        content = []
        if thinking:
            content.append({"type": "thinking", "thinking": thinking})
        if text:
            content.append({"type": "text", "text": text})
        if not content:
            return
        self.turn_messages += 1
        usage = self._usage()
        usage["output_tokens"] = output_tokens
        message = {"role": "assistant", "model": self.model,
                   "content": content, "usage": usage}
        self._emit_out({"type": "assistant", "message": message})
        self._emit_tx({"type": "assistant", "message": message,
                       "isMeta": False, "isSidechain": False})
        if text:
            self.turn_text = text
        # No `tool_use` in this row, so `transcript_turn_open` reads it as a
        # reply that finished — the turn's transcript may end here.
        self.tail_closed = True

    def _on_chunk(self, kind: str, text: str) -> None:
        if self.replaying:
            if kind == "text":
                self.replay_agent.append(text)
            return
        if not self.turn_open:
            return  # a chunk outside a prompt has no turn to belong to
        self._open_message()
        if kind == "text":
            self.text_parts.append(text)
            delta = {"type": "text_delta", "text": text}
        else:
            self.thinking_parts.append(text)
            delta = {"type": "thinking_delta", "thinking": text}
        self._emit_out({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0, "delta": delta}})

    def _on_tool_call(self, update: dict) -> None:
        if self.replaying or not self.turn_open:
            return
        self._close_message()
        tid = str(update.get("toolCallId") or "")
        name, inp = _map_tool(update.get("kind"), update.get("title"),
                              update.get("rawInput"), update.get("locations"),
                              self.cwd, update.get("content"))
        self.tools[tid] = (name, inp)
        block = {"type": "tool_use", "id": tid, "name": name, "input": inp}
        self._emit_out({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0, "content_block": block}})
        self.turn_messages += 1
        message = {"role": "assistant", "model": self.model,
                   "content": [block], "usage": self._usage()}
        self._emit_out({"type": "assistant", "message": message})
        self._emit_tx({"type": "assistant", "message": message,
                       "isMeta": False, "isSidechain": False})
        self.tail_closed = False

    def _on_tool_call_update(self, update: dict) -> None:
        if self.replaying or not self.turn_open:
            return
        status = update.get("status")
        if status not in ("completed", "failed"):
            return
        tid = str(update.get("toolCallId") or "")
        block = {"type": "tool_result", "tool_use_id": tid,
                 "content": _tool_output_text(update),
                 "is_error": status == "failed"}
        message = {"role": "user", "content": [block]}
        self._emit_out({"type": "user", "message": message})
        self._emit_tx({"type": "user", "message": message,
                       "entrypoint": "sdk-cli", "isMeta": False,
                       "isSidechain": False})
        self.tail_closed = False

    def _on_prompt_reply(self, rpc: _Rpc) -> None:
        """EVERY road a prompt can end on lands here, once: a reply of any
        stopReason, a JSON-RPC error, the transport dying. One `result` row
        per prompt (invariant 2)."""
        with self.lock:
            if not self.turn_open:
                return
            try:
                self._finish_turn(rpc)
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
                                    "result": "gemini_host: %r" % (exc,),
                                    "num_turns": self.turn_messages,
                                    "usage": self._usage(), "backend": BACKEND})
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self.turn_open = False
                self.msg_open = False
                self.text_parts, self.thinking_parts = [], []
                # Tool ids are per turn: a later card naming one must not be
                # dressed in a finished call's input.
                self.tools = {}
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
        """Whether `run_dir/cancelled` was dropped DURING this turn. The
        marker outlives the Stop that wrote it (nothing removes it), so one
        older than this prompt is an earlier turn's, and a crash now is a
        crash — not a Stop the user never pressed."""
        try:
            return os.path.getmtime(os.path.join(self.run_dir, "cancelled")) \
                >= self.turn_started_at
        except OSError:
            return False

    def _finish_turn(self, rpc: _Rpc) -> None:
        reply = rpc.result if rpc.error is None and isinstance(rpc.result, dict) else {}
        usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}

        def n(key):
            v = usage.get(key)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

        # The reply's output count rides the turn's last message (L16), the
        # only per-turn number ACP gives — a poll mid-turn still reads 0.
        self._close_message(output_tokens=n("outputTokens"))
        duration_ms = int((time.monotonic() - self.turn_started) * 1000)
        result = {"type": "result", "session_id": self.sid, "backend": BACKEND,
                  "num_turns": self.turn_messages, "duration_ms": duration_ms,
                  "usage": self._usage()}
        died = rpc.error is not None and \
            rpc.error.get("message") in ("gemini exited", "gemini is not running")
        if died and self._stopped_this_turn():
            # The transport died BECAUSE the user stopped it (`_cancel`'s
            # tree-kill drops `cancelled` first; a respawning `_send` does
            # too): a stop, not a failure. The transcript says so the way
            # an answered cancel does; out.jsonl still gets its result.
            self._write_interrupt_mark()
            result.update({"subtype": "error_during_execution", "is_error": True,
                           "result": "", "stop_reason": "cancelled"})
        elif rpc.error is not None:
            # D13: an API/credits failure is an assistant row the readers
            # already know (`isApiErrorMessage`), then an error result.
            text = str(rpc.error.get("message") or "gemini refused the prompt")
            data = rpc.error.get("data")
            if data:
                text += " — " + (data if isinstance(data, str)
                                 else json.dumps(data))
            if died:
                try:
                    self.cli.wait(timeout=1)   # EOF can beat the exit status
                except subprocess.TimeoutExpired:
                    pass
                tail = _err_tail(self.run_dir)
                text = "gemini exited (code %s)%s" % (
                    self.cli.poll(), (": " + tail) if tail else "")
            message = {"role": "assistant", "model": "<synthetic>",
                       "content": [{"type": "text", "text": text}],
                       "usage": self._usage()}
            self._emit_out({"type": "assistant", "isApiErrorMessage": True,
                            "message": message})
            self._emit_tx({"type": "assistant", "isApiErrorMessage": True,
                           "message": message, "isMeta": False,
                           "isSidechain": False})
            self.tail_closed = True
            result.update({"subtype": "error_during_execution", "is_error": True,
                           "result": text, "stop_reason": "error"})
        else:
            stop = str(reply.get("stopReason") or "end_turn")
            if usage:
                result["usage"] = {
                    "input_tokens": n("inputTokens"),
                    "output_tokens": n("outputTokens"),
                    "cache_read_input_tokens": n("cachedReadTokens"),
                    "cache_creation_input_tokens": n("cachedWriteTokens")}
            if stop == "cancelled":
                self._write_interrupt_mark()
            elif not self.tail_closed:
                # A reply that ended on a tool call: close the transcript's
                # turn with an assistant row carrying no `tool_use`, which is
                # what `transcript_turn_open` reads as "reply finished".
                self._emit_tx({"type": "assistant", "message": {
                    "role": "assistant", "model": self.model,
                    "content": [{"type": "text", "text": self.turn_text or ""}],
                    "usage": self._usage()},
                    "isMeta": False, "isSidechain": False})
                self.tail_closed = True
            ok = stop == "end_turn"
            # `result` is the REPLY only on success. On an error result
            # `_poll` shows `result` as the error and runs the account/limit
            # matchers over it — over a stopped reply's prose, they would
            # fire on whatever the model happened to be saying.
            result.update({"subtype": "success" if ok else "error_during_execution",
                           "is_error": not ok,
                           "result": (self.turn_text or "") if ok else "",
                           "stop_reason": stop})
        self._emit_tx({"type": "system", "subtype": "turn_duration",
                       "durationMs": duration_ms, "isMeta": False,
                       "isSidechain": False})
        self._emit_out(result)
        self.result_written = True
        self.results += 1

    # ------------------------------------------------------- notifications

    def _on_notification(self, method: str, params: dict) -> None:
        if method != "session/update":
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = str(update.get("sessionUpdate") or "")
        with self.lock:
            if self.replaying and kind != "user_message_chunk":
                self.replay_gap = True
            if kind == "agent_message_chunk":
                self._on_chunk("text", self._chunk_text(update))
            elif kind == "agent_thought_chunk":
                self._on_chunk("thinking", self._chunk_text(update))
            elif kind == "user_message_chunk":
                if self.replaying:
                    self._replay_user(self._chunk_text(update))
            elif kind == "tool_call":
                self._on_tool_call(update)
            elif kind == "tool_call_update":
                self._on_tool_call_update(update)
            elif kind == "usage_update":
                self._on_usage(update)
            elif kind == "available_commands_update":
                cmds = update.get("availableCommands")
                if isinstance(cmds, list):
                    names = [str(c.get("name")) for c in cmds
                             if isinstance(c, dict) and c.get("name")]
                    if names != self.slash_commands:
                        self.slash_commands = names
                        self._refresh_commands()
            elif kind == "session_info_update":
                title = update.get("title")
                if isinstance(title, str) and title.strip():
                    if self.started:
                        self._emit_tx({"type": "ai-title", "aiTitle": title.strip()})
                    else:
                        # Arrives during `session/new`, before the handshake
                        # has written the D18 notice: held so that notice
                        # stays the transcript's first row.
                        self.pending_title = title.strip()

    def _on_text(self, line: str) -> None:
        """A non-JSON stdout line. One matters: the OAuth flow's prompt,
        which means `session/new` is waiting on a sign-in nobody here can
        give (D13). Noted for `handshake`; everything else goes to stderr,
        where `err.log` keeps the CLI's own words."""
        if _OAUTH_PROMPT_MARK in line:
            with self.lock:
                self.oauth_prompted = True
        sys.stderr.write("gemini: %s\n" % line.strip()[:500])

    def _refresh_commands(self) -> None:
        """A command list that lands AFTER the init row (ACP sends it when it
        likes, often just after the `session/new` reply) rides a fresh init
        row. `init` is turn preamble, so one is safe while a turn is open or
        before any turn has ended; after a `result` it would reopen the turn
        for `_turn_state`, so then it waits for the next echo."""
        if not self.started:
            return  # the handshake's own init row will carry it
        self.commands_dirty = True
        if self.turn_open or not self.results:
            self.write_init()

    @staticmethod
    def _chunk_text(update: dict) -> str:
        content = update.get("content")
        if isinstance(content, dict):
            return str(content.get("text") or "")
        if isinstance(content, list):
            return "".join(str(c.get("text") or "") for c in content
                           if isinstance(c, dict))
        return str(content or "")

    def _on_usage(self, update: dict) -> None:
        for key in ("used", "size"):
            if isinstance(update.get(key), (int, float)):
                self.last_usage[key] = update[key]
        size = self.last_usage.get("size")
        if size and size != self.context_window:
            self.context_window = int(size)
            # Only INSIDE a turn: any row after a `result` reopens the turn
            # for `_turn_state` (`status` is not a bookkeeping subtype).
            if self.turn_open:
                self._emit_out({"type": "system", "subtype": "status",
                                "context_window": self.context_window})

    def _replay_user(self, text: str) -> None:
        """A replayed prompt chunk. Chunks of ONE prompt arrive back to back;
        anything else between two (`replay_gap`: a thought, a tool, a reply
        with no text) means the first prompt's turn is over, so the two never
        merge into one."""
        if self.replay_user is not None and (self.replay_agent or self.replay_gap):
            self.replay_turns.append((self.replay_user, "".join(self.replay_agent)))
            self.replay_user, self.replay_agent = None, []
        self.replay_gap = False
        self.replay_user = (self.replay_user or "") + text

    def backfill_replay(self) -> None:
        """D3: turns taken in a terminal (`gemini --resume`) are not in the
        fused transcript. The `session/load` replay carries every turn; each
        replayed prompt is looked for, in order, among the transcript's own
        prompts, and the ones not found are appended. Matched by TEXT, not
        by count: a fused prompt Gemini never recorded (one refused with a
        JSON-RPC error) would otherwise shift every later terminal turn out
        of the diff."""
        if self.replay_user is not None:
            self.replay_turns.append((self.replay_user, "".join(self.replay_agent)))
        self.replay_user, self.replay_agent = None, []
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
        for user_text, agent_text in self.replay_turns:
            try:
                at = have.index(user_text.strip(), at) + 1
            except ValueError:
                missing.append((user_text, agent_text))
        for user_text, agent_text in missing:
            self._emit_tx({"type": "user", "message": {
                "role": "user", "content": [{"type": "text", "text": user_text}]},
                "entrypoint": "sdk-cli", "isMeta": False, "isSidechain": False})
            self._emit_tx({"type": "assistant", "message": {
                "role": "assistant", "model": self.model,
                "content": [{"type": "text", "text": agent_text}],
                "usage": self._usage()},
                "isMeta": False, "isSidechain": False})
        self.replay_turns = []

    # ---------------------------------------------------------- permissions

    def _on_request(self, rid, method: str, params: dict) -> None:
        if method == "session/request_permission":
            threading.Thread(target=self._serve_permission, args=(rid, params),
                             daemon=True).start()
            return
        # `fs/*`, `terminal/*` and anything else the client is asked for: not
        # offered (clientCapabilities was empty), so refused by the book.
        self.peer.respond_error(rid, -32601, "method not supported: %s" % method)

    def _perm_id(self) -> str:
        """permission_server's id format (`HHMMSS-NNN-6hex`): ours, minted,
        path-safe."""
        return "%s-%03d-%s" % (time.strftime("%H%M%S"), next(self._ids),
                               os.urandom(3).hex())

    def _answer_perm(self, rid, outcome: dict) -> bool:
        """Answer the RPC — once. Both the card's own wait and a cancel can
        reach for the same request; the first to pop it wins (invariant 6)."""
        with self.lock:
            entry = self.pending_perm.pop(rid, None)
            if entry is None:
                return False
            entry["done"].set()
        self.peer.respond(rid, outcome)
        return True

    def _serve_permission(self, rid, params: dict) -> None:
        """One `session/request_permission`, on its own thread. REGISTERED
        FIRST and the whole body guarded: whatever goes wrong in here (a
        malformed call, a disk error), the RPC is still answered — once,
        `cancelled` — and Gemini is never left blocked on it (invariant 6)."""
        options = params.get("options")
        entry = {"perm_id": "", "done": threading.Event(), "options": options}
        with self.lock:
            self.pending_perm[rid] = entry
            stopping = self.closing or (self.cancelling and self.turn_open)
        try:
            if stopping:
                # Raised while the turn is being cancelled (L6), or while the
                # host is closing: no card for a tool the user has already
                # said stop to, nor one nobody will be left to clear.
                self._answer_perm(rid, _permission_outcome(options, {"decision": "deny"}))
                return
            self._park_permission(rid, params, entry)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("gemini_host: permission request failed: %r\n" % (exc,))
            if self._answer_perm(rid, {"outcome": {"outcome": "cancelled"}}) \
                    and entry["perm_id"]:
                _post_card(self.run_dir, "card_cleared", entry["perm_id"])

    def _park_permission(self, rid, params: dict, entry: dict) -> None:
        call = params.get("toolCall")
        call = call if isinstance(call, dict) else {}
        tid = str(call.get("toolCallId") or "")
        known = self.tools.get(tid)
        if known is not None and not call.get("rawInput") and not call.get("content"):
            name, inp = known
        else:
            # The card's own call wins; what it leaves out (often the kind
            # and title on a permission's `toolCall`) comes from the
            # `tool_call` that announced it.
            seen = known[1].get("_acp", {}) if known is not None else {}
            name, inp = _map_tool(call.get("kind") or seen.get("kind"),
                                  call.get("title") or seen.get("title"),
                                  call.get("rawInput"), call.get("locations"),
                                  self.cwd, call.get("content"))
        options = entry["options"]
        perm_id = self._perm_id()
        _private_dirs(self.perm_dir)
        # The card goes up UNDER THE LOCK, and only if nothing answered this
        # request first. `reject_pending` answers (setting `done` under the
        # same lock) and only THEN reads `perm_id`: so either the card parked
        # first and the cancel finds its id — deny on disk, `card_cleared` —
        # or the cancel came first and no card is ever raised. Never a card
        # with no deny and no `card_cleared`.
        with self.lock:
            if entry["done"].is_set():
                return  # answered while this was being built (a cancel, close)
            try:
                _write_atomic(os.path.join(self.perm_dir, perm_id + ".req.json"), {
                    "id": perm_id, "tool": name, "input": inp,
                    "tool_use_id": tid, "created_at": time.time()})
                parked = True
            except OSError as exc:
                sys.stderr.write("gemini_host: cannot park permission: %r\n" % (exc,))
                parked = False
            if parked:
                entry["perm_id"] = perm_id
                # Announce BEFORE blocking, as permission_server does. Each
                # post is its own thread, so the queue may hear a racing
                # cancel's `card_cleared` first; what this lock guarantees is
                # that a card raised here always GETS one.
                _post_card(self.run_dir, "card_raised", perm_id)
        if not parked:
            self._answer_perm(rid, _permission_outcome(options, {"decision": "deny"}))
            return
        decision = self._await_decision(entry)
        if decision is None:
            return  # answered elsewhere (a cancel), card already cleared
        mode = decision.get("mode") if decision.get("decision") == "allow" else None
        if mode == "acceptEdits":
            # D5, narrowed (Copilot's rule, kept): "allow, and accept edits"
            # WIDENS EDITS ONLY. On an edit card it is that card's
            # allow_always — Gemini stops asking for edits — and never a
            # session-wide mode; on any other card (a shell command) it is a
            # plain allow_once, whatever scope the card sent.
            decision = dict(decision, scope="session" if name in _EDIT_TOOLS else "once")
        if self._answer_perm(rid, _permission_outcome(options, decision)):
            # Only the answer that won announces the card gone: a cancel
            # that beat it here has posted its own `card_cleared`.
            _post_card(self.run_dir, "card_cleared", perm_id)
            if mode == "auto":
                # "…and stop asking" (D5): after the RPC reply, so the tool
                # that asked runs first and everything after it is allowed
                # — Gemini's `yolo` mode, by `session/set_mode`.
                with self.lock:
                    self.mode = "auto"
                self._set_mode("auto")

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
        """Every outstanding permission RPC → `reject_once`, its card closed
        with a deny on disk (first writer wins, like `_deny_pending`), the
        queue told. For `session/cancel` and the final teardown."""
        with self.lock:
            items = list(self.pending_perm.items())
        for rid, entry in items:
            if not self._answer_perm(rid, _permission_outcome(
                    entry["options"], {"decision": "deny"})):
                continue  # answered already (the page, a racing cancel)
            # perm_id read only NOW: `_answer_perm` set `done` under the lock,
            # and `_park_permission` checks `done` under that lock before it
            # parks — so from here no card can appear, and the id is final:
            # the card that is on disk, or "" for one that never will be. Read
            # at the snapshot instead, a card parked in between was orphaned
            # with no deny and no `card_cleared`.
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
        """session_host's `_drain_inbox`, PARSED: a user row becomes a
        `session/prompt` (or is HELD, left in `inbox/`, while a turn is open —
        D6: a second prompt would cancel the first); a control request is
        acted on at once whatever the turn is doing. Re-listed from disk on
        every pass, never cached: `_cancel_run` deletes undrained user
        entries, and a held message the user just stopped must not be sent.
        Returns how many user entries are still held."""
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
                if cancel:
                    # Set BEFORE the cancel goes out, so a card Gemini
                    # raises while it winds down is refused at once.
                    self.cancelling = True
                    self.cancel_deadline = time.monotonic() + _CANCEL_GRACE_SECONDS
                # Answered NOW, as Claude answers it: `_await_control_response`
                # tree-kills a session that is silent for 5 s — which a
                # Gemini still unwinding a tool would be. The turn's `result`
                # follows on the cancelled reply, or from `_reap_cancel` if
                # that reply never comes. Nothing reaches the CLI's own queue
                # (held follow-ups stay in inbox/, which `_cancel_run`
                # empties), so there is nothing `still_queued`.
                # Written BEFORE the cancel goes out and under the lock
                # `_on_prompt_reply` writes the result under: answered after
                # the notify, a fast cancelled reply on the reader thread
                # could land its `result` first (seen on a loaded CI runner),
                # and the answer must precede the turn's close, as Claude's
                # does.
                self._control_response(request_id, {"still_queued": []})
            if cancel:
                self.peer.notify("session/cancel", {"sessionId": self.sid})
                self.reject_pending("cancelled")
        elif subtype == "set_permission_mode":
            mode = str(request.get("mode") or "")
            if mode not in _GEMINI_MODES:
                with self.lock:
                    self._control_response(request_id, error="unknown mode %r" % mode)
                return

            def answered(rpc, request_id=request_id, mode=mode):
                # After Gemini's own answer (L14): the switch is either in
                # effect or reported as refused, never assumed.
                with self.lock:
                    if rpc.error is None:
                        self.mode = mode
                        self._control_response(request_id, {})
                    else:
                        self._control_response(request_id, error=str(
                            rpc.error.get("message") or "set_mode failed"))

            self._set_mode(mode, on_done=answered)
        elif subtype == "set_model":
            # D7: the model is a live ACP request. Answered after Gemini's
            # own reply, and the host's `model` (every row names it) moves
            # only once the CLI took it.
            model = str(request.get("model") or "")
            if not model:
                with self.lock:
                    self._control_response(request_id, error="no model named")
                return

            def took(rpc, request_id=request_id, model=model):
                with self.lock:
                    if rpc.error is None:
                        self.model = model
                        self._control_response(request_id, {})
                    else:
                        self._control_response(request_id, error=str(
                            rpc.error.get("message") or "set_model failed"))

            self.peer.request("session/set_model", {
                "sessionId": self.sid, "modelId": model}, on_done=took)
        else:
            with self.lock:
                self._control_response(request_id, error=_UNSUPPORTED)

    # ------------------------------------------------------------ lifecycle

    def _wait_session(self, rpc: _Rpc) -> bool:
        """Wait for a `session/new` | `session/load` reply, bounded — and
        cut short by the OAuth prompt (D13): a CLI that started its browser
        flow inside the request will not answer until a person signs in,
        which nobody here will, so the request is failed as "signed out"
        now rather than when `_HANDSHAKE_SECONDS` runs out. Answers whether
        the rpc is DONE (its `error` says how)."""
        deadline = time.monotonic() + _HANDSHAKE_SECONDS
        while not rpc.done.wait(min(0.2, max(0.0, deadline - time.monotonic()))):
            with self.lock:
                prompted = self.oauth_prompted
            if prompted:
                rpc.error = {"code": -32000, "message": _SIGNED_OUT_TEXT}
                return True
            if time.monotonic() >= deadline:
                return False
        return True

    def _mcp_servers(self) -> list:
        """ACP's `mcpServers` for `session/new` / `session/load`: the pane
        server `agent._write_mcp_config` wrote to the run dir's `mcp.json`
        (`--no-approve`, D8), as `[{name, command, args, env:[{name,value}]}]`
        — or `[]` when the spawn wrote none (no pane, no terminal origin)."""
        servers = _read_json(os.path.join(self.run_dir, "mcp.json")).get("mcpServers")
        out = []
        if not isinstance(servers, dict):
            return out
        for name, spec in servers.items():
            if not isinstance(spec, dict) or not spec.get("command"):
                continue
            env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
            out.append({"name": str(name), "command": str(spec["command"]),
                        "args": [str(a) for a in (spec.get("args") or [])],
                        "env": [{"name": str(k), "value": str(v)} for k, v in env.items()]})
        return out

    def _set_mode(self, mode: str, on_done=None) -> _Rpc:
        """`session/set_mode` with Gemini's id for the CLI-wire `mode` (D5)."""
        return self.peer.request("session/set_mode", {
            "sessionId": self.sid, "modeId": _GEMINI_MODES.get(mode, "default")},
            on_done=on_done)

    def handshake(self) -> bool:
        """`initialize` → `session/new` | `session/load`. False (with the
        failure already written as a result row) when Gemini never came up."""
        rpc = self.peer.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {},
            "clientInfo": {"name": "fused-render", "version": "1"}})
        if not rpc.done.wait(_HANDSHAKE_SECONDS) or rpc.error is not None:
            return self._handshake_failed("initialize", rpc)
        servers = self._mcp_servers()
        if self.resume:
            with self.lock:
                self.replaying = True
            rpc = self.peer.request("session/load", {
                "sessionId": self.sid, "cwd": self.cwd, "mcpServers": servers})
            ok = self._wait_session(rpc)
            with self.lock:
                self.replaying = False
                if ok and rpc.error is None:
                    self.backfill_replay()
            if not ok or rpc.error is not None:
                return self._handshake_failed("session/load", rpc)
            self._adopt_model(rpc.result)
        else:
            rpc = self.peer.request("session/new", {
                "cwd": self.cwd, "mcpServers": servers})
            if not self._wait_session(rpc) or rpc.error is not None:
                return self._handshake_failed("session/new", rpc)
            self._adopt_model(rpc.result)
            got = rpc.result.get("sessionId") if isinstance(rpc.result, dict) else None
            if not isinstance(got, str) or self.agent._bad_id(got):
                # The id names files on disk: one a path could be built out
                # of is refused, by agent.py's own rule for session ids.
                rpc.error = {"code": -32603,
                             "message": "no usable sessionId in the reply: %r" % (got,)}
                return self._handshake_failed("session/new", rpc)
            with self.lock:
                self.sid = got
                self.transcript_path = self._transcript_path()
            # The session-settings record (model, effort, backend — D2)
            # BEFORE anything names the id: no init row, transcript or
            # registry row exists yet, so no reader can find this session
            # without its record. Best-effort: `_poll`'s own write is the
            # backstop, so a failure is logged and the handshake goes on.
            try:
                self.agent._record_settings(self.sid, self.model,
                                            str(self.req.get("effort") or ""),
                                            BACKEND)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write("gemini_host: cannot record settings: %r\n" % (exc,))
            self._write_host_json()
        with self.lock:
            self.write_init()
            if not self.resume:
                self.write_first_turn_notice()
            self.started = True
            if self.pending_title:
                self._emit_tx({"type": "ai-title", "aiTitle": self.pending_title})
                self.pending_title = ""
            self._write_registry("idle")
        self._apply_mode()
        return True

    def _adopt_model(self, result) -> None:
        """A spawn that named no model (`-m` omitted: the CLI's default)
        learns which one the session runs from the reply's
        `models.currentModelId`, so the init row, the D18 notice and the
        settings record name a real id rather than ""."""
        if self.model:
            return
        models = result.get("models") if isinstance(result, dict) else None
        cur = models.get("currentModelId") if isinstance(models, dict) else None
        if isinstance(cur, str) and cur:
            with self.lock:
                self.model = cur

    def _apply_mode(self) -> None:
        """D5 at spawn: the mode the request carries, as `session/set_mode`
        — argv said it too (`_gemini_argv`'s `--approval-mode`), but a
        resumed session may come back from a terminal left in another mode,
        so it is set EXPLICITLY. Waited for (bounded) before the first
        prompt goes out, so that prompt runs under the mode the user picked;
        a refusal is logged and the session carries on."""
        rpc = self._set_mode(self.mode)
        if not rpc.done.wait(_CONFIG_WAIT_SECONDS) or rpc.error is not None:
            sys.stderr.write("gemini_host: %s: %r\n" % (rpc.method, rpc.error))

    def _write_host_json(self) -> None:
        """host.json's `session_id`, once Gemini has named a fresh session.
        Read-modify-write, atomic: `main` wrote the rest before the
        handshake, and a `_send` may already have re-recorded model/mode."""
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
        text = "gemini %s failed: %s" % (step, err.get("message") or err)
        if err.get("message") in ("gemini exited", "gemini is not running"):
            # The CLI refused to start (a bad flag, a broken install): its
            # exit code and its own stderr are the whole story.
            try:
                self.cli.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            text = "gemini exited during %s (code %s)" % (step, self.cli.poll())
        tail = _err_tail(self.run_dir)
        if tail:
            text += "\n" + tail
        with self.lock:
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
        """A run that never came up retires its user entries to `inbox/done/`:
        nothing will ever send them, and an undrained entry is what the page
        draws as a queued bubble on a dead run. The opening prompt is not
        lost — `_start` keeps it as `meta.message`."""
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
        """Drain, watch, reap — session_host's `_reap_loop` for this host.

        The reap closes session_host's own race the same way: a `_send` can
        land in the inbox between the loop's last drain and its exit, already
        told `{"sent": True}`. So `host.json` goes FIRST (a later `_send`
        then fails cleanly with "no live session"), and one more pass drains
        what is there — which for Gemini means running that turn to its
        `result` before stdin closes, since EOF ends the ACP server."""
        try:
            if not self.handshake():
                return
            self._serve(reaping=False)
            self._remove_host_json()
            self._serve(reaping=True)
        except Exception as exc:  # noqa: BLE001 — favour reaping over hanging
            try:
                with _append_private(os.path.join(self.run_dir, "err.log")) as f:
                    f.write(("\ngemini_host: %r\n" % (exc,)).encode("utf-8"))
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
        """A cancelled prompt Gemini never answered (`_CANCEL_GRACE_SECONDS`
        after `session/cancel`): the turn is settled HERE as the cancel it
        was — so the page's Stop ends in a `result` whatever the CLI does —
        and the CLI is killed, since a session that ignores a cancel cannot
        be trusted with the next prompt. Its reply, if one ever comes, finds
        the turn closed and is dropped (`_on_prompt_reply`)."""
        with self.lock:
            due = self.turn_open and self.cancel_deadline is not None \
                and time.monotonic() >= self.cancel_deadline
        if not due:
            return
        sys.stderr.write("gemini_host: cancelled prompt unanswered after %gs; "
                         "killing gemini\n" % _CANCEL_GRACE_SECONDS)
        rpc = _Rpc("session/prompt")
        rpc.result = {"stopReason": "cancelled"}
        self._on_prompt_reply(rpc)
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
        # refused without a card (`_serve_permission`), and every one already
        # registered is in the snapshot `reject_pending` takes next — which
        # writes each raised card's deny and posts its `card_cleared`.
        with self.lock:
            self.closing = True
        self.reject_pending("cancelled")
        with self.lock:
            if self.turn_open:
                # Invariant 2 from the other side: the transport died (or the
                # loop threw) with a prompt still open and no reply ever
                # coming. The reader normally settles it on EOF; this is the
                # backstop for the loop ending first.
                rpc = _Rpc("session/prompt")
                rpc.error = {"code": -32000, "message": "gemini exited"}
                try:
                    self._finish_turn(rpc)
                except Exception:  # noqa: BLE001
                    pass
                self.turn_open = False
        try:
            self.cli.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        self._remove_host_json()
        try:
            os.remove(self.registry_path)
        except OSError:
            pass
        # Reap: stdin is closed above, and the ACP server exits on that EOF
        # (taking its MCP children with it). Wait, then kill one that hangs.
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
        sys.stderr.write("gemini_host: run cancelled before the CLI spawned\n")
        return
    agent = _load_agent(req["agent"])
    argv = list(req["argv"])
    # Gemini's env, not Claude's: the `CLAUDE_CODE_*` switches stripped,
    # `GEMINI_CLI_HOME` and the Google credential variables as found, and
    # `NO_BROWSER=1` (§3). No chat id: there is no shell-prefix mirror (D9).
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
            # A resume's id; "" for a fresh chat until `session/new` names
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
