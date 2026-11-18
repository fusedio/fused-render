"""A session host for the Cursor agent CLI (`cursor-agent acp`).

`cursor-agent acp` speaks the Agent Client Protocol (JSON-RPC, newline-
delimited, over stdio), streams a turn as `session/update` notifications,
asks the CLIENT for permissions, and persists nothing fused can read in
place. So this process is BOTH halves at once — the ACP client, and the
writer of every file the rest of the app reads for a Claude session. The
row contract, the readers' three rules, the thread model, the permission
round trip through `perm/`, the inbox and the reap are the other ACP
hosts' (the ACP ones: see session_host.py). What is Cursor's own:

  * the handshake is `initialize`, then `authenticate` with the method the
    CLI advertised (`cursor_login`), then `session/new` | `session/load`.
    A signed-out CLI never answers `authenticate` and refuses `session/new`
    with -32000 "Authentication required": the `authenticate` wait is
    bounded by `_AUTH_SECONDS` and the run carries on, so the refusal is
    what the user is told, with the fix (`agent login`);
  * the session id is Cursor's own (`session/new` mints a uuid4); a resume
    is `session/load`, whose replay is backfilled into the transcript;
  * the approval mode is the HOST's: `default` raises a card for every
    request, `acceptEdits` answers an edit at once and cards the rest, `auto`
    answers everything without a card. Nothing is ever sent to the CLI to
    change it (the spawn's `--approve-mcps --trust` flags are the CLI's
    own, fixed), and a live `set_permission_mode` only moves the host's;
  * the model is a spawn flag (`--model`), never changed live; a spawn that
    names none runs on `auto`; there is no usage or cost on the wire;
  * `cursor/ask_question` and `cursor/create_plan` are requests the CLI
    BLOCKS on: ask-question is answered `skipped`, create-plan `rejected`.
    `cursor/*` notifications are ignored;
  * a tool call may or may not carry a `rawInput`, and an MCP tool's title
    comes in several spellings (`_mcp_identity`);
  * the pane MCP server (`app_state`, `terminal_*`) rides `session/new` and
    `session/load`'s `mcpServers`, built from the run dir's `mcp.json`.

Loaded and run BY PATH, exactly like the other hosts (`sys.executable
cursor_host.py`, the JSON request on stdin). Stdlib only, never imports
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
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.join(os.path.dirname(HERE), "templates", "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)
import agent_roots  # noqa: E402  (shared dir, loaded by path like agent.py does)

BACKEND = "cursor"
PROTOCOL_VERSION = 1

# Same two knobs as session_host.py, by the same names, so a test (or an
# operator) shrinks both hosts with one setting.
_IDLE_REAP_SECONDS = float(
    os.environ.get("FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS", "30"))
_DRAIN_INTERVAL_SECONDS = float(
    os.environ.get("FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS", "0.2"))
# How long `initialize` + `session/new|load` may take before the run is
# reported dead. Cursor's first start (workspace and MCP discovery) can be
# slow; a session that never answers at all is the case this bounds.
_HANDSHAKE_SECONDS = float(
    os.environ.get("FUSED_CURSOR_HOST_HANDSHAKE_SECONDS", "120"))
# `authenticate` never answers on a signed-out CLI (and `session/new` then
# says why), so its wait is far shorter than the handshake's.
_AUTH_SECONDS = float(os.environ.get("FUSED_CURSOR_HOST_AUTH_SECONDS", "10"))
# The reap is close stdin → wait → kill: the ACP server exits on stdin EOF
# and tears its own MCP children down, so the kill is only for one that
# hangs.
_EXIT_WAIT_SECONDS = 5.0
# How long a cancelled prompt may take to answer before the host stops
# waiting: it settles the turn itself and kills the CLI (`_reap_cancel`).
_CANCEL_GRACE_SECONDS = float(
    os.environ.get("FUSED_CURSOR_HOST_CANCEL_GRACE_SECONDS", "15"))
# The approval modes the host itself implements, by the CLI-wire mode the
# request carries (`cli_mode`): `prompt` is `default`.
_APPROVAL_MODES = frozenset({"default", "acceptEdits", "auto"})
_AUTH_REQUIRED = "authentication required"
_SIGNED_OUT_TEXT = ("cursor is not signed in: Authentication required. "
                    "Run `agent login` (or set CURSOR_API_KEY), then try again.")
_AUTH_METHOD = "cursor_login"
# The permission wait mirrors permission_server.py: same env knob, same poll,
# same write window — the card on the other side is the same card.
_PERM_WAIT = float(os.environ.get("FUSED_RENDER_PERMISSION_TIMEOUT", "3600"))
_PERM_POLL = 0.15
_DECISION_WRITE_WINDOW = 2.0
_UNSUPPORTED = "unsupported by cursor"
# Claude Code's own stop marker, as the tasks readers spell it
# (tasks_store._INTERRUPT_MARKS). Written to the transcript on a cancelled
# prompt so `_absorb` says "Interrupted by you" and `transcript_turn_open`
# reads the turn as closed — the row contract has no transcript row
# for that case, and without one a stopped turn reads open for
# STALE_TAIL_SEC.
_INTERRUPT_MARK = "[Request interrupted by user]"
# ACP tool kinds → the Claude tool names the page already renders.
_KIND_TO_TOOL = {"execute": "Bash", "read": "Read", "fetch": "WebFetch",
                 "search": "Grep"}
# How Cursor titles an MCP tool call when it sends no `rawInput`:
# `<tool> (<server> MCP Server)`.
_MCP_TITLE = re.compile(r"^(?P<tool>[A-Za-z0-9_.-]+) \((?P<server>[A-Za-z0-9_.-]+) MCP Server\)$")
# ...or Claude's own spelling, `mcp__<server>__<tool>`.
_MCP_DOUBLE = re.compile(r"^mcp__(?P<server>[A-Za-z0-9_.-]+?)__(?P<tool>[A-Za-z0-9_.-]+)$")
_MCP_SERVER = "fused_render"
_MCP_COLON = re.compile(r"^fused_render:\s*(?P<tool>[A-Za-z0-9_.-]+)$")
#: ...and an MCP call WITH arguments: `<tool>(key: value, …)`: pairs joined
#: by ", ", string values BARE (never quoted). A value may itself hold
#: commas, so a pair ends only where the next `, key: ` begins.
_MCP_CALL = re.compile(r"^(?P<tool>[A-Za-z0-9_.-]+)\((?P<args>.*)\)$", re.S)
_CALL_ARG = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*): (?P<val>.*?)(?=, [A-Za-z_][A-Za-z0-9_]*: |$)", re.S)
#: fused's own pane-server tools (permission_server.py): the names an
#: argument-bearing title is matched against, since it names no server.
_FUSED_TOOLS = frozenset({"app_state", "canvas_screenshot", "terminal_list",
                          "terminal_read", "terminal_send", "s3_search", "s3_read"})
#: `session/load` may answer BEFORE most of its replay has streamed, so the
#: replay is over only once the updates have been quiet for a beat —
#: bounded, in case it never is.
_REPLAY_QUIET_S = float(os.environ.get("FUSED_CURSOR_HOST_REPLAY_QUIET_SECONDS", "0.4"))
_REPLAY_SETTLE_S = float(os.environ.get("FUSED_CURSOR_HOST_REPLAY_SETTLE_SECONDS", "10"))
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
    spec = importlib.util.spec_from_file_location("claude_agent_cursor_host", path)
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
    `projects_dir()` that outlives every run and is shared by every turn."""
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
        origin = _EVENT_ORIGIN.current()
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
        origin = _EVENT_ORIGIN.current()
        if not origin:
            return
        body = {"kind": kind,
                "run_id": os.path.basename(os.path.normpath(run_dir)),
                "request_id": req_id}
        threading.Thread(target=_send_event, args=(origin + _EVENT_PATH, body),
                         daemon=True).start()
    except Exception:
        pass


def _load_event_origin():
    """`event_origin.py` beside this file, loaded by path (this directory is
    never a package on sys.path): where a failed post re-resolves the live
    server from this install's server.json."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "event_origin.py")
    spec = importlib.util.spec_from_file_location("fused_claude_event_origin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_EVENT_ORIGIN = _load_event_origin()


def _send_event(url: str, body: dict) -> None:
    _EVENT_ORIGIN.send(url, body, _EVENT_PATH, _EVENT_TIMEOUT,
                       _EVENT_RETRY_SECONDS)


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
            rpc.settle(error={"code": -32000, "message": "cursor is not running"})
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
                    # The CLI's own prose (a warning): err.log keeps it.
                    sys.stderr.write("cursor: %s\n" % line[:500])
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self._dispatch(msg)
                except Exception as exc:  # noqa: BLE001 — one bad message must not end the session
                    sys.stderr.write("cursor_host: dispatch failed: %r\n" % (exc,))
        finally:
            with self._pending_lock:
                pending, self._pending = list(self._pending.values()), {}
            for rpc in pending:
                rpc.settle(error={"code": -32000, "message": "cursor exited"})
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


def _mcp_identity(title: str):
    """`(server, tool, args_text)` for a title that names an MCP tool, or
    None. Cursor's spellings: `<tool> (<server> MCP Server)`,
    `mcp__<server>__<tool>`, and — for fused's own server only, whose tool
    names are known — `<server>-<tool>`, `<server>: <tool>` and the bare
    `<tool>`, any of them optionally followed by `(key: value, …)`."""
    m = _MCP_TITLE.match(title)
    if m:
        return m.group("server"), m.group("tool"), ""
    head, args = title, ""
    call = _MCP_CALL.match(title)
    if call:
        head, args = call.group("tool"), call.group("args")
    m = _MCP_DOUBLE.match(head)
    if m:
        return m.group("server"), m.group("tool"), args
    prefix = _MCP_SERVER + "-"
    if head.startswith(prefix) and len(head) > len(prefix):
        return _MCP_SERVER, head[len(prefix):], args
    m = _MCP_COLON.match(title)
    if m:
        return _MCP_SERVER, m.group("tool"), ""
    if head in _FUSED_TOOLS:
        return _MCP_SERVER, head, args
    return None


def _map_tool(kind: str, title: str, raw, locations=None, cwd: str = "",
              content=None) -> tuple:
    """`(name, input)` for one ACP tool call: the Claude tool name the chat
    already has a card and a chip for, with the fields that card reads, plus
    `_acp: {kind, title}` (and `raw`, the call's own input, only for a kind
    with no Claude twin — a mapped one already carries every field it has).
    A call may carry no `rawInput`, so `title` (the tool's own description of
    the call — a shell command's command line) and `locations` carry what the
    card shows. An MCP tool (`_mcp_identity`) from fused's own server becomes
    `mcp__fused_render__<tool>` (the name agent.py pre-allows and
    `_segments_from_rows` strips), another server's `<server>-<tool>`."""
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
            # the usual shape of an edit), never an empty one.
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
        ident = _mcp_identity(title)
        if ident and ident[0] == _MCP_SERVER:
            name = "mcp__%s__%s" % (_MCP_SERVER, ident[1])
            # `<tool>(key: value, …)`: the pairs are parsed best-effort into
            # the input the card shows, never over what `rawInput` said.
            for arg in _CALL_ARG.finditer(ident[2]):
                val = arg.group("val").strip()
                if val != "":
                    inp.setdefault(arg.group("key"), val)
        elif ident:
            name = "%s-%s" % (ident[0], ident[1])
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
    """allow + scope session → allow_always, allow → allow_once, everything
    else (deny, expired, cancelled, timeout) → reject_once. An option list
    that lacks the wanted kind falls to its neighbour; with no option list at
    all (or an empty one) the protocol's literal ids are answered, and one
    that offers options but none usable is answered `cancelled`, which the
    agent reads as a refusal."""
    if decision.get("decision") == "allow":
        kinds = ("allow_always", "allow_once") if decision.get("scope") == "session" \
            else ("allow_once", "allow_always")
        literal = "allow-always" if kinds[0] == "allow_always" else "allow-once"
    else:
        kinds = ("reject_once", "reject_always")
        literal = "reject-once"
    oid = _pick_option(options, kinds)
    if not oid:
        if isinstance(options, list) and options:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": literal}}
    if kinds[0] == "allow_once" and not _pick_option(options, ("allow_once",)):
        # The only allow on offer is WIDER than the one the user gave. The
        # narrowest offered is still the answer (refusing a tool the user
        # allowed would be worse), but never silently.
        sys.stderr.write("cursor_host: no allow_once offered; answering "
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
        self.model = str(req.get("model") or "auto")
        # THE SESSION — the key of every file this host writes. A resume
        # names it; a fresh chat has none until `session/new` answers, because
        # Cursor MINTS it (a uuid4 in `session/new`),
        # so it is set in `handshake` and nothing keyed on it is written
        # before. `new_session_id` is not read: that is Claude's fused-minted
        # id.
        self.sid = str(req.get("session_id") or "")
        self.resume = bool(self.sid)
        # The appended system prompt (`agent._cursor_context_dir`): Cursor has
        # no channel for it, so it goes ahead of a NEW chat's first prompt on
        # the wire. A resumed chat carries it in its own history.
        self.context = "" if self.resume else self._read_context()
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
        # Turn state.
        self.turn_open = False
        self.turn_started = 0.0
        self.turn_started_at = 0.0
        self.turn_text = None         # text of the last closed text message
        self.turn_messages = 0        # assistant messages this turn (num_turns)
        self.tail_closed = True       # transcript's last row is a text assistant row
        self.msg_open = False         # a message_start has been emitted
        self.text_parts = []
        self.thinking_parts = []
        self.replaying = False
        self.last_update_at = 0.0     # monotonic time of the last session/update
        self.replay_turns = []
        self.replay_user = None
        self.replay_agent = []
        self.replay_gap = False       # a non-prompt update since the last prompt chunk
        # The Stop road: set when `session/cancel` goes out, cleared when
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
        self.notice_pending = False   # notice owed to out.jsonl's first turn
        self.tools = {}               # toolCallId -> (name, input)
        # The CLI-wire mode this session runs in (`cli_mode`): the HOST's
        # approval mode, moved by `set_permission_mode` and by a card's
        # "…and stop asking".
        self.mode = str(req.get("cli_mode") or "default")
        if self.mode not in _APPROVAL_MODES:
            self.mode = "default"
        self._ids = itertools.count(1)
        peer.on_notification = self._on_notification
        peer.on_request = self._on_request
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

    @staticmethod
    def _usage() -> dict:
        """Cursor reports no usage: every count is 0."""
        return {"input_tokens": 0, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0}

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
        self._emit_out(row)
        self.commands_dirty = False

    def _notice_row(self) -> dict:
        return {"type": "system", "subtype": "task_notification",
                "summary": "Handled by Cursor · model %s" % self.model,
                "backend": BACKEND, "model": self.model}

    def write_first_turn_notice(self) -> None:
        """The transcript's own record that Cursor answers here — its
        first row. The LIVE copy waits for the first echo (`send_prompt`)."""
        self._emit_tx(dict(self._notice_row(), isMeta=False, isSidechain=False))
        self.notice_pending = True

    # ------------------------------------------------------------- the turn

    def _read_context(self) -> str:
        try:
            with open(os.path.join(self.run_dir, "cursor", "AGENTS.md"),
                      encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def send_prompt(self, text: str, msg_uuid: str = "") -> None:
        """Echo first, then `session/prompt` (invariant 3). The state and the
        echo go under the lock, so the reader thread cannot see a reply before
        the turn reads open; the pipe write goes OUTSIDE it — a prompt larger
        than the pipe buffer, written while the reader is blocked on this
        same lock with Cursor's stdout full, would deadlock both.

        `msg_uuid` is the stamp `agent._write_inbox_entry` put on the inbox
        row, carried onto the echo the way Claude Code's `--replay-user-messages`
        carries it, so `agent._drained_unechoed` settles the entry by identity
        rather than by its words."""
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
                # LIVE: the same row into out.jsonl, so the first poll
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
        wire = text
        if self.context:
            wire = self.context + "\n\n" + text
            self.context = ""
        self.peer.request("session/prompt", {
            "sessionId": self.sid,
            "prompt": [{"type": "text", "text": wire}]},
            on_done=self._on_prompt_reply)

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
        finalized `assistant` row the transcript keeps (and `_poll` reads
        the usage off). ACP reports no token counts."""
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
                                    "result": "cursor_host: %r" % (exc,),
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
        self._close_message()
        duration_ms = int((time.monotonic() - self.turn_started) * 1000)
        result = {"type": "result", "session_id": self.sid, "backend": BACKEND,
                  "num_turns": self.turn_messages, "duration_ms": duration_ms,
                  "usage": self._usage()}
        died = rpc.error is not None and \
            rpc.error.get("message") in ("cursor exited", "cursor is not running")
        if died and self._stopped_this_turn():
            # The transport died BECAUSE the user stopped it (`_cancel`'s
            # tree-kill drops `cancelled` first; a respawning `_send` does
            # too): a stop, not a failure. The transcript says so the way
            # an answered cancel does; out.jsonl still gets its result.
            self._write_interrupt_mark()
            result.update({"subtype": "error_during_execution", "is_error": True,
                           "result": "", "stop_reason": "cancelled"})
        elif rpc.error is not None:
            # An API/credits failure is an assistant row the readers
            # already know (`isApiErrorMessage`), then an error result.
            text = str(rpc.error.get("message") or "cursor refused the prompt")
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
                text = "cursor exited (code %s)%s" % (
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
        # `stop_reason` rides on the turn's closing record: the transcript is
        # what recovery reads when nothing watched this turn
        # (`session_liveness.session_last_turn`), and a refusal or max_tokens
        # after a streamed reply leaves a plain assistant row as the last word.
        self._emit_tx({"type": "system", "subtype": "turn_duration",
                       "durationMs": duration_ms, "isMeta": False,
                       "isSidechain": False,
                       "stop_reason": result.get("stop_reason") or ""})
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
            self.last_update_at = time.monotonic()
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
                        # has written the notice: held so that notice
                        # stays the transcript's first row.
                        self.pending_title = title.strip()

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
        """Turns taken in a terminal (`agent --resume`) are not in the
        fused transcript. The `session/load` replay carries every turn; each
        replayed prompt is looked for, in order, among the transcript's own
        prompts, and the ones not found are appended. Matched by TEXT, not
        by count: a fused prompt Cursor never recorded (one refused with a
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
        # Cursor's own blocking requests: it waits on an answer, so each is
        # answered at once — no question is put to a person mid-run, and plan
        # mode is not offered.
        if method == "cursor/ask_question":
            self.peer.respond(rid, {"outcome": {
                "outcome": "skipped", "reason": "no one is available to answer"}})
            return
        if method == "cursor/create_plan":
            self.peer.respond(rid, {"outcome": {
                "outcome": "rejected", "reason": "plan mode is not offered"}})
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
        `cancelled` — and Cursor is never left blocked on it (invariant 6)."""
        options = params.get("options")
        entry = {"perm_id": "", "done": threading.Event(), "options": options}
        with self.lock:
            self.pending_perm[rid] = entry
            stopping = self.closing or (self.cancelling and self.turn_open)
        try:
            if stopping:
                # Raised while the turn is being cancelled, or while the
                # host is closing: no card for a tool the user has already
                # said stop to, nor one nobody will be left to clear.
                self._answer_perm(rid, _permission_outcome(options, {"decision": "deny"}))
                return
            with self.lock:
                mode = self.mode
            if mode == "auto":
                self._answer_perm(rid, _permission_outcome(options, {"decision": "allow"}))
                return
            call = params.get("toolCall")
            call = call if isinstance(call, dict) else {}
            name, inp = self._resolve_tool(call)
            if mode == "acceptEdits" and name in _EDIT_TOOLS:
                self._answer_perm(rid, _permission_outcome(options, {"decision": "allow"}))
                return
            self._park_permission(rid, call, name, inp, entry)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("cursor_host: permission request failed: %r\n" % (exc,))
            if self._answer_perm(rid, {"outcome": {"outcome": "cancelled"}}) \
                    and entry["perm_id"]:
                _post_card(self.run_dir, "card_cleared", entry["perm_id"])

    def _resolve_tool(self, call: dict) -> tuple:
        """`(name, input)` for a permission request's `toolCall`: its own
        fields win, and what it leaves out (often the kind and title) comes
        from the `tool_call` that announced it."""
        tid = str(call.get("toolCallId") or "")
        known = self.tools.get(tid)
        if known is not None and not call.get("rawInput") and not call.get("content"):
            return known
        seen = known[1].get("_acp", {}) if known is not None else {}
        return _map_tool(call.get("kind") or seen.get("kind"),
                         call.get("title") or seen.get("title"),
                         call.get("rawInput"), call.get("locations"),
                         self.cwd, call.get("content"))

    def _park_permission(self, rid, call: dict, name: str, inp: dict,
                         entry: dict) -> None:
        tid = str(call.get("toolCallId") or "")
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
                sys.stderr.write("cursor_host: cannot park permission: %r\n" % (exc,))
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
        if mode in ("auto", "acceptEdits"):
            # "…and stop asking" / "…and accept edits": this card is a plain
            # allow_once, and the session moves to the host-side mode, so
            # what follows runs under it. Nothing is sent to the CLI.
            decision = dict(decision, scope="once")
        if self._answer_perm(rid, _permission_outcome(options, decision)):
            # Only the answer that won announces the card gone: a cancel
            # that beat it here has posted its own `card_cleared`.
            _post_card(self.run_dir, "card_cleared", perm_id)
            if mode in ("auto", "acceptEdits"):
                with self.lock:
                    self.mode = mode

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
        A second prompt would cancel the first); a control request is
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
                    # Set BEFORE the cancel goes out, so a card Cursor
                    # raises while it winds down is refused at once.
                    self.cancelling = True
                    self.cancel_deadline = time.monotonic() + _CANCEL_GRACE_SECONDS
                # Answered NOW, as Claude answers it: `_await_control_response`
                # tree-kills a session that is silent for 5 s — which a
                # Cursor still unwinding a tool would be. The turn's `result`
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
            # The approval mode is the host's own: it is in effect for the
            # next request, and nothing is sent to the CLI.
            mode = str(request.get("mode") or "")
            with self.lock:
                if mode in _APPROVAL_MODES:
                    self.mode = mode
                    self._control_response(request_id, {})
                else:
                    self._control_response(request_id, error="unknown mode %r" % mode)
        else:
            # `set_model` too: the model is a spawn flag.
            with self.lock:
                self._control_response(request_id, error=_UNSUPPORTED)

    # ------------------------------------------------------------ lifecycle

    @staticmethod
    def _wait_rpc(rpc: _Rpc, seconds: float = _HANDSHAKE_SECONDS) -> bool:
        """Wait for a handshake reply, bounded. Answers whether the rpc is
        DONE (its `error` says how)."""
        return rpc.done.wait(seconds)

    def _mcp_servers(self) -> list:
        """ACP's `mcpServers` for `session/new` / `session/load`: the pane
        server `agent._write_mcp_config` wrote to the run dir's `mcp.json`
        (`--no-approve`), as `[{name, command, args, env:[{name,value}]}]`
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

    def handshake(self) -> bool:
        """`initialize` → `authenticate` → `session/new` | `session/load`.
        False (with the failure already written as a result row) when Cursor
        never came up."""
        rpc = self.peer.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {},
            "clientInfo": {"name": "fused-render", "version": "1"}})
        if not self._wait_rpc(rpc) or rpc.error is not None:
            return self._handshake_failed("initialize", rpc)
        failed = self._authenticate(rpc.result)
        if failed is not None:
            return self._handshake_failed("authenticate", failed)
        servers = self._mcp_servers()
        if self.resume:
            with self.lock:
                self.replaying = True
            rpc = self.peer.request("session/load", {
                "sessionId": self.sid, "cwd": self.cwd, "mcpServers": servers})
            ok = self._wait_rpc(rpc)
            if not ok or rpc.error is not None:
                with self.lock:
                    self.replaying = False
                return self._handshake_failed("session/load", rpc)
            self._await_replay_quiet()
            with self.lock:
                self.replaying = False
                self.backfill_replay()
        else:
            rpc = self.peer.request("session/new", {
                "cwd": self.cwd, "mcpServers": servers})
            if not self._wait_rpc(rpc) or rpc.error is not None:
                return self._handshake_failed("session/new", rpc)
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
            # The session-settings record (model, effort, backend)
            # BEFORE anything names the id: no init row, transcript or
            # registry row exists yet, so no reader can find this session
            # without its record. Best-effort: `_poll`'s own write is the
            # backstop, so a failure is logged and the handshake goes on.
            try:
                self.agent._record_settings(self.sid, self.model,
                                            str(self.req.get("effort") or ""),
                                            BACKEND)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write("cursor_host: cannot record settings: %r\n" % (exc,))
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
        return True

    def _authenticate(self, init_result):
        """`authenticate` with the method `initialize` advertised. None to
        carry on — it answered, or it did not answer within `_AUTH_SECONDS`
        (a signed-out CLI never does; `session/new` then names the problem),
        or the CLI has no such request. The refused `_Rpc` when the handshake
        must end: signed out, or any other error."""
        methods = init_result.get("authMethods") if isinstance(init_result, dict) else None
        first = methods[0] if isinstance(methods, list) and methods else None
        method = first.get("id") if isinstance(first, dict) else None
        rpc = self.peer.request("authenticate", {
            "methodId": method if isinstance(method, str) and method else _AUTH_METHOD})
        if not self._wait_rpc(rpc, _AUTH_SECONDS):
            sys.stderr.write("cursor_host: authenticate unanswered after %gs; "
                             "continuing\n" % _AUTH_SECONDS)
            return None
        err = rpc.error
        if err is None or err.get("code") == -32601:
            return None
        return rpc

    def _await_replay_quiet(self) -> None:
        """The `session/load` reply may not be the end of the replay: the
        history can land after it. Wait until no `session/update` has arrived
        for `_REPLAY_QUIET_S`, bounded by `_REPLAY_SETTLE_S`; only then is
        the backfill complete and the first prompt safe to send."""
        deadline = time.monotonic() + _REPLAY_SETTLE_S
        with self.lock:
            self.last_update_at = max(self.last_update_at, time.monotonic())
        while True:
            now = time.monotonic()
            with self.lock:
                quiet_for = now - self.last_update_at
            if quiet_for >= _REPLAY_QUIET_S or now >= deadline:
                return
            time.sleep(min(0.05, max(0.005, _REPLAY_QUIET_S - quiet_for)))

    def _write_host_json(self) -> None:
        """host.json's `session_id`, once Cursor has named a fresh session.
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
        detail = "%s %s" % (err.get("message") or "", err.get("data") or "")
        text = "cursor %s failed: %s" % (step, err.get("message") or err)
        if err.get("message") in ("cursor exited", "cursor is not running"):
            # The CLI refused to start (a bad flag, a broken install): its
            # exit code and its own stderr are the whole story.
            try:
                self.cli.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            text = "cursor exited during %s (code %s)" % (step, self.cli.poll())
        elif _AUTH_REQUIRED in detail.lower():
            text = _SIGNED_OUT_TEXT
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
        what is there — which for Cursor means running that turn to its
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
                    f.write(("\ncursor_host: %r\n" % (exc,)).encode("utf-8"))
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
        """A cancelled prompt Cursor never answered (`_CANCEL_GRACE_SECONDS`
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
        sys.stderr.write("cursor_host: cancelled prompt unanswered after %gs; "
                         "killing cursor\n" % _CANCEL_GRACE_SECONDS)
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
                rpc.error = {"code": -32000, "message": "cursor exited"}
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
                # Long enough for the server.json fallback too (`send_budget`):
                # a relaunched server may still be booting on its new port.
                last.join(_EVENT_ORIGIN.send_budget(_EVENT_TIMEOUT,
                                                    _EVENT_RETRY_SECONDS))
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
        sys.stderr.write("cursor_host: run cancelled before the CLI spawned\n")
        return
    agent = _load_agent(req["agent"])
    argv = list(req["argv"])
    # Cursor's env, not Claude's: the `CLAUDE_CODE_*` switches stripped,
    # `CURSOR_API_KEY` and the CLI's own variables as found. No chat id:
    # there is no shell-prefix mirror.
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
