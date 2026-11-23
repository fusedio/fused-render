"""A session host for CURSOR CLI — the Cursor twin of gemini_host.py.

Cursor's CLI speaks the Agent Client Protocol too (`cursor-agent acp`, a
hidden subcommand: JSON-RPC 2.0, newline-delimited, over stdio), streams a
turn as `session/update` notifications, asks the CLIENT for every permission
its own policy does not settle, and keeps its chats in its own blob stores
under `~/.cursor/chats`, which fused never reads. So this process is BOTH
halves at once — the ACP client, and the writer of every file the rest of the
app reads for a Claude session — exactly as `gemini_host.py` is, and that
file (and `copilot_host.py` behind it) is the one to read first: this one is
a copy of it with Cursor's own facts in (docs/CURSOR_BACKEND.md, read from
the CLI's bundle, release 2026.11.19-f005e9d):

  * the session id is Cursor's own (`session/new` mints a uuid4), learned
    from the reply; a resume is `session/load`, which replays the history
    as `user_message_chunk` / `agent_message_chunk` updates BEFORE it
    answers, backfilled into the transcript the same way (D2, D3);
  * Cursor's ACP modes (`agent`, `plan`, `ask`) are NOT approval modes. The
    host pins `agent` after the handshake, and the fused approval modes are
    its OWN policy for answering `session/request_permission` (D5):
    `default` forwards every card, `acceptEdits` allows edit/delete cards
    itself, `auto` allows every card itself — so a `set_permission_mode`
    control request is live and never reaches the CLI;
  * a card's "Allow always" (`allow-always`) is NEVER answered: Cursor
    persists it into the user's GLOBAL allowlist (`~/.cursor/cli-config.json`).
    A session-scoped allow is the card's `allow_once`, and the host itself
    allows the same call again for the rest of this process (D4);
  * the model is LIVE (`session/set_model {modelId}`, D7). The host declares
    `_meta.parameterizedModelPicker` in `clientCapabilities`, so model ids are
    Cursor's base model names rather than its exploded variant strings;
  * ACP's `session/new` takes no instructions and Cursor reads no prompt
    file fused can name, so the appended prompt rides the FIRST
    `session/prompt` of every host process as a leading text block wrapped
    in `<fused_render_context>` — stripped again from a replayed prompt
    before the backfill compares texts (D8);
  * a `tool_call` carries `rawInput` (`{command}`, `{path}`, …) that is
    PARTIAL while the model is still streaming the call's arguments, and is
    refreshed by `tool_call_update`s; the host merges them by `toolCallId`
    and raises the tool row only once the call starts (`in_progress`),
    ends, or asks for permission (D14);
  * the CLI calls back into the client with extension requests
    (`cursor/update_todos`, `cursor/task`, …): refused `-32601`, which it
    tolerates; a question card (`askQuestion`, options ending in
    `__ask_question_skip__`) is answered `skip` at once, since fused has no
    question card for it.

Everything else — the row contract, the readers' three rules, the thread
model, the permission round trip through `perm/`, the inbox, the reap — is
copilot_host.py's, verbatim, and its header is the place that argues each.
Loaded and run BY PATH, exactly like the other hosts (`sys.executable
cursor_host.py`, the JSON request on stdin). Stdlib only, never imports
`fused_render`.

SPARE MODE. The CLI's first `session/new` in a process takes ~4.5 s (a later
one ~1.2 s), and only a completed `session/new` warms it. A first request
with `"spare": true` therefore spawns the CLI in `cwd` (its project rules and
skills load from the PROCESS cwd, so a spare serves only a chat on that same
cwd), runs `initialize` plus a throwaway `session/new`, writes
`<spare_dir>/ready`, and blocks on stdin for ONE more line: the real request,
after which it is a normal host that skips `initialize`. stdin EOF with no
request (the server went away, or retired the spare) kills the CLI and exits.
`agent._prewarm_cursor` / `_claim_cursor_spare` are the other half.
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
# reported dead. Cursor's first start fetches the account's models and team
# settings; a session that never answers at all is the case this bounds.
_HANDSHAKE_SECONDS = float(
    os.environ.get("FUSED_CURSOR_HOST_HANDSHAKE_SECONDS", "120"))
# The reap is close stdin → wait → kill: the ACP server exits on stdin EOF,
# so the kill is only for one that hangs. And how long the mode/model set
# after the handshake may hold the first prompt back.
_EXIT_WAIT_SECONDS = 5.0
_CONFIG_WAIT_SECONDS = 10.0
# How long a cancelled prompt may take to answer before the host stops
# waiting: it settles the turn itself and kills the CLI (`_reap_cancel`).
_CANCEL_GRACE_SECONDS = float(
    os.environ.get("FUSED_CURSOR_HOST_CANCEL_GRACE_SECONDS", "15"))
# The fused approval modes this host answers cards under (D5), by the
# CLI-wire mode the request carries (`cli_mode`). A COPY of
# `agent._CURSOR_MODES`' keys, pinned equal by a test.
_CURSOR_MODES = ("default", "acceptEdits", "auto")
# Cursor's own ACP mode the host pins after the handshake: full agent, as
# opposed to `plan` (read-only planning) and `ask` (no edits, no commands).
_CURSOR_AGENT_MODE = "agent"
# The ACP kinds `acceptEdits` allows without a card (D5): Cursor files a
# write as `edit` and a file deletion as `delete`.
_EDIT_KINDS = frozenset({"edit", "delete"})
# The option id Cursor's question card carries for "skip" (D12): the one
# card this host answers itself, whatever the mode.
_ASK_SKIP_OPTION = "__ask_question_skip__"
# D8: the wrapper the appended prompt rides in on a process's first prompt,
# and the pattern that strips it off a replayed prompt.
_CONTEXT_OPEN = "<fused_render_context>"
_CONTEXT_CLOSE = "</fused_render_context>"
_CONTEXT_RE = re.compile(r"^\s*" + re.escape(_CONTEXT_OPEN) + r".*?"
                         + re.escape(_CONTEXT_CLOSE) + r"\s*", re.S)
# Where `agent._cursor_argv` writes the appended prompt, under the run dir.
#: `agent._CURSOR_INSTRUCTIONS` — not under `cursor/`, which is the run
#: dir's poll-offset file.
_INSTRUCTIONS_FILE = os.path.join("cursor_agent", "instructions.md")
# The permission wait mirrors permission_server.py: same env knob, same poll,
# same write window — the card on the other side is the same card.
_PERM_WAIT = float(os.environ.get("FUSED_RENDER_PERMISSION_TIMEOUT", "3600"))
_PERM_POLL = 0.15
_DECISION_WRITE_WINDOW = 2.0
_UNSUPPORTED = "unsupported by cursor"
# Claude Code's own stop marker, as the tasks readers spell it
# (tasks_store._INTERRUPT_MARKS). Written to the transcript on a cancelled
# prompt so `_absorb` says "Interrupted by you" and `transcript_turn_open`
# reads the turn as closed.
_INTERRUPT_MARK = "[Request interrupted by user]"
#: A refused call's tool result. Cursor ends a rejected call with a bare
#: `tool_call_update {status: "completed"}` — no content, not `failed`
#: (observed) — so the host says what happened, in permission_server's words.
_DENIED_TEXT = "The user denied this request."
# ACP tool kinds → the Claude tool names the page already renders (D14).
_KIND_TO_TOOL = {"execute": "Bash", "read": "Read", "fetch": "WebFetch",
                 "search": "Grep"}
# What `rawInput` field each kind is shown by: a call is not raised before
# it starts, but a card or a result for one still needs its input.
_INPUT_KEYS = {"execute": ("command", "cmd"),
               "edit": ("file_path", "path", "filePath", "file"),
               "delete": ("file_path", "path", "filePath", "file"),
               "read": ("file_path", "path", "filePath", "file"),
               "fetch": ("url",), "search": ("pattern", "query", "searchTerm")}
_MCP_SERVER = "fused_render"
#: How Cursor titles an MCP call's permission card: `<server>: <tool>`.
_MCP_TITLE = re.compile(r"^(?P<server>[A-Za-z0-9_.-]+): (?P<tool>[A-Za-z0-9_.-]+)$")
#: fused's own pane-server tools (permission_server.py).
_FUSED_TOOLS = frozenset({"app_state", "canvas_screenshot", "terminal_list",
                          "terminal_read", "terminal_send", "s3_search", "s3_read",
                          "expert_list", "expert_context", "expert_ask"})
# `session/load` answers only AFTER its replay has streamed (Cursor awaits
# `replayConversationHistory` before replying), so the quiet wait is short;
# kept, bounded, for an update that trails the reply.
_REPLAY_QUIET_S = float(os.environ.get("FUSED_CURSOR_HOST_REPLAY_QUIET_SECONDS", "0.2"))
_REPLAY_SETTLE_S = float(os.environ.get("FUSED_CURSOR_HOST_REPLAY_SETTLE_SECONDS", "5"))
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
    `cursor_dir()` that outlives every run and is shared by every turn."""
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


def _initialize_params() -> dict:
    """The `initialize` request's params — one copy, shared by `handshake`
    and the spare's warm-up so they cannot drift. `clientCapabilities` offers
    no `fs`/`terminal` (the CLI's own tools do the work) and asks for the
    PARAMETERIZED model picker, so model ids are base names (D7); no
    `subagents`, so a subagent's turns stay inside the CLI."""
    return {"protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"_meta": {"parameterizedModelPicker": True}},
            "clientInfo": {"name": "fused-render", "version": "1"}}


def _kill_cli(cli) -> None:
    """`close()`'s reap for a CLI no `_Host` owns (a spare that never got a
    request): SIGTERM, wait, SIGKILL for one that hangs."""
    try:
        cli.stdin.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        cli.terminate()
    except OSError:
        pass
    try:
        cli.wait(timeout=_EXIT_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            cli.kill()
            cli.wait(timeout=_EXIT_WAIT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


class _StderrPump:
    """A spare's CLI stderr, copied to a file that can be switched: the
    spare's own dir until a chat adopts it, then that run's `err.log` (which
    `_poll` shows the tail of as the run's error text)."""

    def __init__(self, stream, path: str):
        self._stream = stream
        self._path = path
        self._fh = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def retarget(self, path: str) -> None:
        with self._lock:
            self._path = path
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            try:
                chunk = self._stream.read1(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self._lock:
                try:
                    if self._fh is None:
                        self._fh = _append_private(self._path)
                    self._fh.write(chunk)
                    self._fh.flush()
                except OSError:
                    self._fh = None


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
        # A stdout line that is not JSON: the CLI's own prose (a warning).
        # Logged to stderr, never parsed.
        self.on_text = lambda line: None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._started = False

    def start(self) -> None:
        """Begin reading — AFTER the host has installed its callbacks, or the
        first `available_commands_update` lands on the no-op defaults. A
        second call is a no-op: a spare starts its peer for the warm-up, and
        the `_Host` that adopts it installs callbacks on the running reader."""
        if self._started:
            return
        self._started = True
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


def _normalize_diff(d: dict) -> dict:
    """One ACP `diff` entry with Cursor's NEW-FILE shape repaired. For a file
    the call created, 2026.11.19-f005e9d sends the unified diff's own header
    lines, each missing its first character, as the texts:
    `oldText: "-- /dev/null"`, `newText: "++ b/<path>\n<content>"`
    (observed live). That is an empty old text and the content after the
    header line; any other entry is returned as it came."""
    old = str(d.get("oldText") or "")
    new = str(d.get("newText") or "")
    if old.strip() == "-- /dev/null" and new.startswith("++ "):
        _, _, body = new.partition("\n")
        return dict(d, oldText="", newText=body)
    return d


def _diffs_of(content) -> list:
    """The call's ACP `diff` content entries, normalized."""
    return [_normalize_diff(c) for c in (content if isinstance(content, list) else [])
            if isinstance(c, dict) and c.get("type") == "diff"]


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


def _unquote_title(title: str) -> str:
    """Cursor titles a shell call's card with the command in backticks
    (`` `ls -la` ``, inner backticks escaped): the bare command line."""
    t = title.strip()
    if len(t) >= 2 and t.startswith("`") and t.endswith("`"):
        return t[1:-1].replace("\\`", "`")
    return t


def _input_complete(kind: str, raw) -> bool:
    """Whether `rawInput` carries the field `kind` is shown by — a kind with
    no such field (`other`, `think`, "") is complete as it is."""
    keys = _INPUT_KEYS.get(str(kind or ""))
    if not keys:
        return True
    raw = raw if isinstance(raw, dict) else {}
    return any(isinstance(raw.get(k), str) and raw.get(k) for k in keys)


def _merge_input(old, new) -> dict:
    """`rawInput` merged by toolCallId: the newer row's fields over the
    older's, nothing dropped."""
    out = dict(old) if isinstance(old, dict) else {}
    if isinstance(new, dict):
        out.update({k: v for k, v in new.items() if v is not None})
    return out


def _map_tool(kind: str, title: str, raw, locations=None, cwd: str = "",
              content=None) -> tuple:
    """`(name, input)` for one ACP tool call, per D14: the Claude tool name the
    chat already has a card and a chip for, with the fields that card reads,
    plus `_acp: {kind, title}` (and `raw`, the call's own input, only for a
    kind with no Claude twin). Cursor sends `rawInput` (`{command}` for a
    shell call, `{path}` for a read, edit or delete, `{pattern, path}` for a
    grep, `{url}` for a fetch, `{providerIdentifier, toolName, args}` for an
    MCP call), with the title and `locations` as the fallback. fused's own
    MCP server becomes `mcp__fused_render__<tool>` (the name agent.py
    pre-allows and `_segments_from_rows` strips), another server's
    `<server>-<tool>`, Copilot's spelling."""
    raw = raw if isinstance(raw, dict) else ({} if raw is None else {"value": raw})
    kind = str(kind or "")
    title = str(title or "")
    acp = {"kind": kind, "title": title}
    name = _KIND_TO_TOOL.get(kind)
    if kind == "execute":
        command = str(raw.get("command") or raw.get("cmd") or _unquote_title(title))
        inp = {"command": command, "description": str(raw.get("description") or command)}
    elif kind == "edit":
        diffs = _diffs_of(content)
        path = _first_path(raw, locations) or next(
            (str(d["path"]) for d in diffs if isinstance(d.get("path"), str) and d["path"]), "")
        full = path if os.path.isabs(path) else os.path.join(cwd or "", path)
        # The diff decides when there is one: Cursor's edit carries no
        # content until it has RUN (observed), when the file exists either
        # way, so an empty old text is what says "created".
        created = (all(not d.get("oldText") for d in diffs) if diffs
                   else bool(path) and not os.path.exists(full))
        if created:
            name = "Write"
            inp = {"file_path": path,
                   "content": str(raw.get("content") or raw.get("newText")
                                  or "".join(str(d.get("newText") or "") for d in diffs))}
        else:
            name = "Edit"
            # The diff the card shows is what the user approves: Cursor sends
            # it as an ACP `content` diff entry on the permission card
            # (`{type:"diff", path, oldText, newText}`).
            inp = {"file_path": path,
                   "diff": str(raw.get("diff") or _unified_diff(diffs))}
    elif kind == "delete":
        name = "Delete"
        inp = {"file_path": _first_path(raw, locations)}
    elif kind == "read":
        inp = {"file_path": _first_path(raw, locations)}
    elif kind == "fetch":
        inp = {"url": str(raw.get("url") or title)}
    elif kind == "search":
        inp = {"pattern": str(raw.get("pattern") or raw.get("query")
                              or raw.get("searchTerm") or title),
               "path": str(raw.get("path") or _first_path(raw, locations) or "")}
    else:
        tool = raw.get("toolName")
        server = raw.get("providerIdentifier")
        m = _MCP_TITLE.match(title)
        if not (isinstance(tool, str) and tool) and m:
            tool, server = m.group("tool"), m.group("server")
        args = raw.get("args")
        if isinstance(tool, str) and tool:
            server = str(server or "")
            if server == _MCP_SERVER or (not server and tool in _FUSED_TOOLS) \
                    or (server.endswith(_MCP_SERVER) and tool in _FUSED_TOOLS):
                name = "mcp__%s__%s" % (_MCP_SERVER, tool)
            elif server:
                name = "%s-%s" % (server, tool)
            else:
                name = tool
            inp = dict(args) if isinstance(args, dict) else {}
        else:
            inp = dict(raw)
            name = title or kind or "tool"
        acp["raw"] = raw
    inp["_acp"] = acp
    return name, inp


def _raise_at(call: dict, status) -> bool:
    """Whether a held call's row goes up at this status (D14): when it
    starts, or ends. An EDIT waits for its end: Cursor announces it with an
    empty `rawInput`, names the path later and sends the diff only with
    `completed` (observed), so a row raised at `in_progress` would show an
    empty Write. A card raises any call at once (`_park_permission`)."""
    if status in ("completed", "failed"):
        return True
    return status == "in_progress" and call.get("kind") != "edit"


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
            parts.append(_unified_diff([_normalize_diff(item)]).rstrip("\n"))
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
    """D4: allow → `allow_once`, whatever its scope; everything else (deny,
    expired, cancelled, timeout) → `reject_once`. NEVER `allow_always`:
    Cursor writes that answer into the user's GLOBAL allowlist
    (`~/.cursor/cli-config.json`, `persistAllowlistEntry`), a grant far wider
    than "for this session"; the host keeps the session's own allows itself
    (`_Host.session_allows`). An option list with no usable option at all is
    answered `cancelled`, which the agent reads as a refusal."""
    if decision.get("decision") == "allow":
        kinds = ("allow_once",)
    else:
        kinds = ("reject_once", "reject_always")
    oid = _pick_option(options, kinds)
    if not oid:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": oid}}


def _is_rejection(options, outcome: dict) -> bool:
    """Whether an answer to a card refuses the call: `cancelled`, or the
    option picked is a `reject_*` one."""
    out = outcome.get("outcome") if isinstance(outcome, dict) else None
    if not isinstance(out, dict):
        return False
    if out.get("outcome") == "cancelled":
        return True
    picked = out.get("optionId")
    for opt in options if isinstance(options, list) else []:
        if isinstance(opt, dict) and opt.get("optionId") == picked:
            return str(opt.get("kind") or "").startswith("reject")
    return False


def _is_question(options) -> bool:
    """Whether a permission request is Cursor's question card (`askQuestion`:
    one `allow_once` option per answer plus `__ask_question_skip__`)."""
    return isinstance(options, list) and any(
        isinstance(o, dict) and o.get("optionId") == _ASK_SKIP_OPTION for o in options)


def _allow_key(call: dict) -> str:
    """What a session-scoped allow covers (D4): the same kind of call with
    the same title — the same shell command line, the same file edit, the
    same MCP tool. "" when the call names nothing to match on."""
    kind = str(call.get("kind") or "")
    title = str(call.get("title") or "")
    if not title:
        return ""
    return kind + "\0" + title


# ------------------------------------------------------------------- the host

class _Host:
    def __init__(self, agent, req: dict, cli, peer: _Peer,
                 initialized: bool = False, throwaway_sid: str = ""):
        self.agent = agent
        # A spare already ran `initialize`; `handshake` skips only that.
        self.initialized = initialized
        # The spare's warm-up session: updates addressed to it are never ours.
        self.throwaway_sid = throwaway_sid
        self.req = req
        self.cli = cli
        self.peer = peer
        self.run_dir = req["run_dir"]
        self.cwd = req["cwd"]
        self.model = str(req.get("model") or "")
        # The session's `models.availableModels` ({modelId, name}), from the
        # `session/new` / `session/load` reply: what `_wire_model` resolves
        # the catalog's `auto` against.
        self.available = []
        # THE SESSION — the key of every file this host writes. A resume
        # names it; a fresh chat has none until `session/new` answers, because
        # Cursor MINTS it (a uuid4 in `session/new`, `crypto.randomUUID()`),
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
        self.last_update_at = 0.0     # monotonic time of the last session/update
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
        self.tools = {}               # toolCallId -> (name, input) once raised
        # D14: everything known of each call this turn, merged by id
        # (`_merge_call`), and the ids announced but not raised yet.
        self.calls = {}
        self.held = set()
        # D4: the calls a session-scoped allow covers (`_allow_key`), for the
        # life of this process — answered `allow_once` without a card.
        self.session_allows = set()
        # The toolCallIds whose card was answered with a reject (any road:
        # Deny, a timeout, a cancel), so their bare `completed` reads denied.
        self.rejected = set()
        # The CLI-wire mode this session runs in (`cli_mode`): the host's own
        # card-answering policy (D5); `set_permission_mode` moves it.
        self.mode = str(req.get("cli_mode") or "default")
        if self.mode not in _CURSOR_MODES:
            self.mode = "default"
        # D8: the appended prompt, sent once with this process's first prompt.
        self.instructions = self._read_instructions()
        self.context_sent = False
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
                "summary": "Handled by Cursor · model %s" % self.model,
                "backend": BACKEND, "model": self.model}

    def write_first_turn_notice(self) -> None:
        """D18: the transcript's own record that Cursor answers here — its
        first row. The LIVE copy waits for the first echo (`send_prompt`)."""
        self._emit_tx(dict(self._notice_row(), isMeta=False, isSidechain=False))
        self.notice_pending = True

    # ------------------------------------------------------------- the turn

    def send_prompt(self, text: str, msg_uuid: str = "") -> None:
        """Echo first, then `session/prompt` (invariant 3). The state and the
        echo go under the lock, so the reader thread cannot see a reply before
        the turn reads open; the pipe write goes OUTSIDE it — a prompt larger
        than the pipe buffer, written while the reader is blocked on this
        same lock with Cursor's stdout full, would deadlock both.

        The process's FIRST prompt carries the appended prompt ahead of the
        user's words, as its own text block (D8); the echo and the
        transcript keep the user's words only.

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
            blocks = [{"type": "text", "text": text}]
            if self.instructions and not self.context_sent:
                blocks.insert(0, {"type": "text", "text": "%s\n%s\n%s\n\n" % (
                    _CONTEXT_OPEN, self.instructions, _CONTEXT_CLOSE)})
                self.context_sent = True
        self.peer.request("session/prompt", {
            "sessionId": self.sid, "prompt": blocks},
            on_done=self._on_prompt_reply)

    def _read_instructions(self) -> str:
        """`<run_dir>/cursor_agent/instructions.md`, written by `agent._cursor_argv`
        (D8), or "" when the spawn wrote none."""
        try:
            with open(os.path.join(self.run_dir, _INSTRUCTIONS_FILE),
                      encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

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

    def _merge_call(self, update: dict) -> dict:
        """D14: fold one `tool_call` / `tool_call_update` (or a permission
        card's `toolCall`) into what is known of its `toolCallId`: `rawInput`
        merged key by key, `kind`, `title`, `locations` and `content`
        replaced when the row carries them. Caller holds the lock."""
        tid = str(update.get("toolCallId") or "")
        call = self.calls.get(tid)
        if call is None:
            call = {"kind": "", "title": "", "rawInput": {}, "locations": None,
                    "content": None}
            self.calls[tid] = call
        for key in ("kind", "title"):
            v = update.get(key)
            if isinstance(v, str) and v:
                call[key] = v
        for key in ("locations", "content"):
            v = update.get(key)
            if isinstance(v, list) and v:
                call[key] = v
        if isinstance(update.get("rawInput"), dict):
            call["rawInput"] = _merge_input(call["rawInput"], update["rawInput"])
        return call

    def _raise_tool(self, tid: str, call: dict) -> None:
        """The tool row (`content_block_start` + `assistant` tool_use), from
        the MERGED call — once per id. Caller holds the lock."""
        self.held.discard(tid)
        if tid in self.tools:
            return
        self._close_message()
        name, inp = _map_tool(call["kind"], call["title"], call["rawInput"],
                              call["locations"], self.cwd, call["content"])
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

    def _on_tool_call(self, update: dict) -> None:
        """A call announced. HELD, not raised: Cursor announces a call while
        the model is still streaming its arguments (`partialToolCall`), so
        the first `rawInput` may be a truncated command; the row goes up
        when the call starts, ends, or asks (D14)."""
        if self.replaying or not self.turn_open:
            return
        tid = str(update.get("toolCallId") or "")
        call = self._merge_call(update)
        if tid in self.tools:
            return
        if _raise_at(call, update.get("status")):
            self._raise_tool(tid, call)
        else:
            self.held.add(tid)

    def _on_tool_call_update(self, update: dict) -> None:
        if self.replaying or not self.turn_open:
            return
        tid = str(update.get("toolCallId") or "")
        status = update.get("status")
        call = self._merge_call(update)
        if tid not in self.tools and _raise_at(call, status):
            self._raise_tool(tid, call)
        if status not in ("completed", "failed"):
            return
        if tid not in self.tools:
            return
        text = _tool_output_text(update)
        failed = status == "failed"
        if tid in self.rejected:
            self.rejected.discard(tid)
            if not text.strip():
                text, failed = _DENIED_TEXT, True
        block = {"type": "tool_result", "tool_use_id": tid,
                 "content": text, "is_error": failed}
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
                self.calls = {}
                self.held = set()
                self.rejected = set()
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
            # D13: an API/credits failure is an assistant row the readers
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
        # An update for another session — a spare's throwaway one, whose
        # trailing updates may land after adoption — is not this chat's.
        # Only when the id is known: `session/new`'s own updates can arrive
        # before its reply names the session.
        upd_sid = params.get("sessionId")
        if isinstance(upd_sid, str) and upd_sid and (
                upd_sid == self.throwaway_sid or (self.sid and upd_sid != self.sid)):
            return
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
        """A non-JSON stdout line: the CLI's own prose. Logged to stderr,
        where `err.log` keeps it — never one with a URL (a login link may
        carry a token, and the diagnostics bundle ships that file's tail)."""
        if "://" in line:
            sys.stderr.write("cursor: <a line with a URL, not logged>\n")
            return
        sys.stderr.write("cursor: %s\n" % line.strip()[:500])

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

    @staticmethod
    def _strip_context(text: str) -> str:
        """A replayed prompt without the D8 context block a host prepended to
        it — the transcript holds the user's words only."""
        return _CONTEXT_RE.sub("", text, count=1)

    def backfill_replay(self) -> None:
        """D3: turns taken in a terminal (`cursor-agent --resume`) are not in the
        fused transcript. The `session/load` replay carries every turn; each
        replayed prompt is looked for, in order, among the transcript's own
        prompts, and the ones not found are appended. Matched by TEXT, not
        by count: a fused prompt Cursor never recorded (one refused with a
        JSON-RPC error) would otherwise shift every later terminal turn out
        of the diff."""
        if self.replay_user is not None:
            self.replay_turns.append((self.replay_user, "".join(self.replay_agent)))
        self.replay_user, self.replay_agent = None, []
        self.replay_turns = [(self._strip_context(u), a) for u, a in self.replay_turns]
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
            if entry.get("tid") and _is_rejection(entry.get("options"), outcome):
                self.rejected.add(entry["tid"])
        self.peer.respond(rid, outcome)
        return True

    def _serve_permission(self, rid, params: dict) -> None:
        """One `session/request_permission`, on its own thread. REGISTERED
        FIRST and the whole body guarded: whatever goes wrong in here (a
        malformed call, a disk error), the RPC is still answered — once,
        `cancelled` — and Cursor is never left blocked on it (invariant 6).

        Three kinds of card never reach the page: Cursor's question card
        (answered `skip`, D12), a call the session already allowed
        (`session_allows`, D4), and one the mode allows by itself (D5:
        `auto` every card, `acceptEdits` an edit or delete)."""
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
            if _is_question(options):
                # D12: no question card here. "Skip" lets the agent go on
                # without an answer, rather than a misleading Allow/Deny.
                self._answer_perm(rid, {"outcome": {
                    "outcome": "selected", "optionId": _ASK_SKIP_OPTION}})
                return
            call = params.get("toolCall")
            call = call if isinstance(call, dict) else {}
            with self.lock:
                merged = self._merge_call(call) if call.get("toolCallId") else call
                entry["tid"] = str(call.get("toolCallId") or "")
                key = _allow_key(merged)
                kind = str(merged.get("kind") or "")
                auto = (self.mode == "auto"
                        or (self.mode == "acceptEdits" and kind in _EDIT_KINDS)
                        or (key and key in self.session_allows))
            if auto:
                self._answer_perm(rid, _permission_outcome(options, {"decision": "allow"}))
                return
            self._park_permission(rid, params, entry)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("cursor_host: permission request failed: %r\n" % (exc,))
            if self._answer_perm(rid, {"outcome": {"outcome": "cancelled"}}) \
                    and entry["perm_id"]:
                _post_card(self.run_dir, "card_cleared", entry["perm_id"])

    def _park_permission(self, rid, params: dict, entry: dict) -> None:
        call = params.get("toolCall")
        call = call if isinstance(call, dict) else {}
        tid = str(call.get("toolCallId") or "")
        with self.lock:
            # The card's call merged into everything the `tool_call` rows
            # said (`_serve_permission` already folded it in): Cursor's
            # card titles a shell call `` `cmd` `` and carries no `rawInput`,
            # while the announced call has `{command}` — and the card has the
            # edit's diff the announcement lacked. A call announced but
            # still held goes up now, ahead of its card (D14).
            merged = self.calls.get(tid) if tid else None
            if merged is None:
                merged = {"kind": call.get("kind") or "", "title": call.get("title") or "",
                          "rawInput": call.get("rawInput") or {},
                          "locations": call.get("locations"), "content": call.get("content")}
            elif tid not in self.tools and self.turn_open and not self.replaying:
                self._raise_tool(tid, merged)
            key = _allow_key(merged)
        name, inp = _map_tool(merged.get("kind"), merged.get("title"),
                              merged.get("rawInput"), merged.get("locations"),
                              self.cwd, merged.get("content"))
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
                # Announce BEFORE blocking, as permission_server does.
                _post_card(self.run_dir, "card_raised", perm_id)
        if not parked:
            self._answer_perm(rid, _permission_outcome(options, {"decision": "deny"}))
            return
        decision = self._await_decision(entry)
        if decision is None:
            return  # answered elsewhere (a cancel), card already cleared
        allowed = decision.get("decision") == "allow"
        mode = decision.get("mode") if allowed else None
        if allowed:
            with self.lock:
                if mode in _CURSOR_MODES:
                    # "…and stop asking" / "…and accept edits" (D5): the
                    # host's own policy from here on, recorded before the
                    # answer so the next card already reads it.
                    self.mode = mode
                elif decision.get("scope") == "session" and key:
                    # "Always approve" (D4): this process allows the same
                    # call again by itself — never Cursor's `allow-always`.
                    self.session_allows.add(key)
        if self._answer_perm(rid, _permission_outcome(options, decision)):
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
            # D5: the approval mode is this host's own card policy, so the
            # switch is in effect the moment it is recorded — nothing goes to
            # the CLI (its ACP modes are agent/plan/ask, pinned to agent).
            mode = str(request.get("mode") or "")
            with self.lock:
                if mode not in _CURSOR_MODES:
                    self._control_response(request_id, error="unknown mode %r" % mode)
                    return
                self.mode = mode
                self._control_response(request_id, {})
        elif subtype == "set_model":
            # D7: the model is a live ACP request. Answered after Cursor's
            # own reply, and the host's `model` (every row names it) moves
            # only once the CLI took it.
            model = str(request.get("model") or "")
            if not model:
                with self.lock:
                    self._control_response(request_id, error="no model named")
                return

            with self.lock:
                model = self._wire_model(model)

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

    def _wait_rpc(self, rpc: _Rpc, seconds: float = 0.0) -> bool:
        """Wait for a handshake reply (`initialize`, `session/new`,
        `session/load`), bounded. Answers whether the rpc is DONE (its
        `error` says how). Cursor has no in-band browser flow to cut the
        wait short: signed out, `session/new` fails at once with
        "Authentication required" (D13)."""
        return rpc.done.wait(seconds or _HANDSHAKE_SECONDS)

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

    def _pin_agent_mode(self) -> _Rpc:
        """`session/set_mode agent` (D5): full agent, never Cursor's `plan`
        or `ask` — a resumed chat may come back from a terminal left in
        either."""
        return self.peer.request("session/set_mode", {
            "sessionId": self.sid, "modeId": _CURSOR_AGENT_MODE})

    def handshake(self) -> bool:
        """`initialize` → `session/new` | `session/load`. False (with the
        failure already written as a result row) when Cursor never came up.
        A spare's host skips `initialize` only (`self.initialized`): it ran
        in the warm-up, with the same params (`_initialize_params`)."""
        if not self.initialized:
            rpc = self.peer.request("initialize", _initialize_params())
            if not self._wait_rpc(rpc) or rpc.error is not None:
                return self._handshake_failed("initialize", rpc)
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
            # The model first, so the backfilled rows name the session's
            # model as every later row does.
            self._apply_model(rpc.result)
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
            self._apply_model(rpc.result)
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
        self._apply_mode()
        return True

    def _apply_model(self, result) -> None:
        """D7 at spawn: the model the request named, as `session/set_model`
        when the session is not on it already — waited for (bounded), so the
        first prompt runs on it. A spawn that named none, or one the CLI
        refused (an id the account cannot run), adopts the reply's
        `models.currentModelId`, so the init row, the D18 notice and the
        settings record name the model that actually answers."""
        models = result.get("models") if isinstance(result, dict) else None
        cur = models.get("currentModelId") if isinstance(models, dict) else None
        cur = cur if isinstance(cur, str) else ""
        avail = models.get("availableModels") if isinstance(models, dict) else None
        with self.lock:
            self.available = [m for m in (avail if isinstance(avail, list) else [])
                              if isinstance(m, dict) and isinstance(m.get("modelId"), str)]
            if self.model:
                self.model = self._wire_model(self.model)
        if self.model and self.model != cur and self.sid:
            rpc = self.peer.request("session/set_model", {
                "sessionId": self.sid, "modelId": self.model})
            if rpc.done.wait(_CONFIG_WAIT_SECONDS) and rpc.error is None:
                return
            sys.stderr.write("cursor_host: session/set_model %s refused: %r\n"
                             % (self.model, rpc.error))
            if cur:
                with self.lock:
                    self.model = cur
            return
        if not self.model and cur:
            with self.lock:
                self.model = cur

    def _wire_model(self, model: str) -> str:
        """The id `session/set_model` takes for `model`. The catalog's seed
        `auto` is NOT one (the CLI answers `-32602 Invalid model value:
        auto`, observed): Cursor's Auto is a listed row (`auto-smart`, named
        "Auto", on 2026.11.19-f005e9d), so `auto` resolves to the first row
        whose id is or starts with `auto`, or whose name is "Auto". Every
        other id, and `auto` on a list with no such row, goes as it is.
        Without this a refused `auto` adopted `currentModelId` — which is
        whatever model was last picked anywhere, because `set_model` is
        persisted to the CLI's config."""
        if model != "auto" or any(m["modelId"] == model for m in self.available):
            return model
        for m in self.available:
            mid = m["modelId"]
            if mid.startswith("auto") or str(m.get("name") or "").strip().lower() == "auto":
                return mid
        return model

    def _await_replay_quiet(self) -> None:
        """D3: Cursor streams the whole replay BEFORE it answers
        `session/load`, but an update may still trail the reply on the pipe.
        Wait until no `session/update` has arrived for `_REPLAY_QUIET_S`,
        bounded by `_REPLAY_SETTLE_S`; only then is the backfill complete."""
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

    def _apply_mode(self) -> None:
        """D5 at spawn: Cursor's own `agent` mode, set EXPLICITLY (a resumed
        session may come back from a terminal left in `plan` or `ask`) and
        waited for, bounded, before the first prompt. The fused approval
        mode needs nothing from the CLI: it is how this host answers cards.
        A refusal is logged and the session carries on."""
        rpc = self._pin_agent_mode()
        if not rpc.done.wait(_CONFIG_WAIT_SECONDS) or rpc.error is not None:
            sys.stderr.write("cursor_host: %s: %r\n" % (rpc.method, rpc.error))

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
        text = "cursor %s failed: %s" % (step, err.get("message") or err)
        data = err.get("data")
        detail = data.get("message") if isinstance(data, dict) else data
        if isinstance(detail, str) and detail and detail != err.get("message"):
            # Cursor puts the actionable sentence here ("…Please run 'agent
            # login' first…"), the bare category in `message` (observed).
            text += " — " + detail
        if err.get("message") in ("cursor exited", "cursor is not running"):
            # The CLI refused to start (a bad flag, a broken install): its
            # exit code and its own stderr are the whole story.
            try:
                self.cli.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            text = "cursor exited during %s (code %s)" % (step, self.cli.poll())
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
                         "killing cursor-agent\n" % _CANCEL_GRACE_SECONDS)
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
        # Reap. Cursor's ACP server does NOT exit on stdin EOF (observed on
        # 2026.11.19-f005e9d: still running 30 s after it), but exits at once
        # on SIGTERM — so SIGTERM now rather than wait out the grace period
        # and SIGKILL, which would leave Node no chance to end its MCP
        # children. The launcher `exec`s Node, so this pid IS the server.
        try:
            self.cli.terminate()
        except OSError:
            pass
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


def _write_pid_and_host_json(agent, req: dict, cli) -> None:
    run_dir = req["run_dir"]
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


def _spare_warm_up(agent, req: dict):
    """Spare mode, steps 1-3: spawn the CLI in `req["cwd"]`, run `initialize`
    and a throwaway `session/new`, write `<spare_dir>/ready`. Returns
    `(cli, peer, pump, throwaway_sid)`, or None with the CLI already killed
    when anything failed (`ready` is then never written)."""
    spare_dir = req["spare_dir"]
    env = agent._spawn_env("", backend=BACKEND)
    try:
        cli = subprocess.Popen(
            list(req["argv"]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=req["cwd"], env=env, **agent._DETACH)
    except Exception as exc:  # noqa: BLE001
        with _append_private(os.path.join(spare_dir, "err.log")) as f:
            f.write(str(exc).encode("utf-8", "replace"))
        return None
    pump = _StderrPump(cli.stderr, os.path.join(spare_dir, "err.log"))
    pump.start()
    peer = _Peer(cli)
    # Nothing else is wired yet: the CLI's updates are dropped, and a request
    # it makes of the client is refused (a spare offers no capability).
    peer.on_request = lambda rid, method, params: peer.respond_error(
        rid, -32601, "Method not found: " + method)
    peer.start()
    ok = False
    sid = ""
    try:
        rpc = peer.request("initialize", _initialize_params())
        if rpc.done.wait(_HANDSHAKE_SECONDS) and rpc.error is None:
            rpc = peer.request("session/new", {"cwd": req["cwd"], "mcpServers": []})
            if rpc.done.wait(_HANDSHAKE_SECONDS) and rpc.error is None:
                got = rpc.result.get("sessionId") if isinstance(rpc.result, dict) else None
                if isinstance(got, str) and got:
                    sid, ok = got, True
        if ok:
            with agent._private_open(os.path.join(spare_dir, "pid")) as f:
                f.write(str(cli.pid))
            agent._private_open(os.path.join(spare_dir, "ready")).close()
    finally:
        if not ok:
            _kill_cli(cli)
    return (cli, peer, pump, sid) if ok else None


def _run_spare(req: dict) -> None:
    """Spare mode (see the module docstring): warm up, wait for the real
    request on stdin, then continue as a normal host. EOF with no request,
    or the CLI dying while it waits, ends the process."""
    agent = _load_agent(req["agent"])
    warm = _spare_warm_up(agent, req)
    if warm is None:
        return
    cli, peer, pump, throwaway_sid = warm
    stop_watch = threading.Event()

    def _watch() -> None:
        while not stop_watch.wait(0.5):
            if cli.poll() is not None:
                _kill_cli(cli)
                os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()
    adopted = False
    try:
        line = sys.stdin.buffer.readline()
        if line.strip():
            real = json.loads(line.decode("utf-8"))
            adopted = True
    finally:
        stop_watch.set()
        if not adopted:
            _kill_cli(cli)
    if not adopted:
        return
    run_dir = real["run_dir"]
    if os.path.exists(os.path.join(run_dir, "cancelled")):
        sys.stderr.write("cursor_host: run cancelled before the spare was adopted\n")
        _kill_cli(cli)
        return
    # From here the CLI's stderr and this process's own belong to the run.
    pump.retarget(os.path.join(run_dir, "err.log"))
    try:
        fd = os.open(os.path.join(run_dir, "host.err.log"),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.dup2(fd, 2)
        finally:
            os.close(fd)
    except OSError:
        pass
    _write_pid_and_host_json(agent, real, cli)
    _Host(agent, real, cli, peer, initialized=True,
          throwaway_sid=throwaway_sid).run()


def main() -> None:
    """The first stdin LINE is the request (a normal one arrives with no
    newline and stdin closed, which `readline` returns whole); with
    `"spare": true` it starts spare mode instead (`_run_spare`)."""
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
    req = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
    if req.get("spare"):
        _run_spare(req)
        return
    run_dir = req["run_dir"]
    # A Stop that landed before setsid() — session_host's D1310 window.
    if os.path.exists(os.path.join(run_dir, "cancelled")):
        sys.stderr.write("cursor_host: run cancelled before the CLI spawned\n")
        return
    agent = _load_agent(req["agent"])
    argv = list(req["argv"])
    # Cursor's env, not Claude's: the `CLAUDE_CODE_*` switches stripped,
    # `CURSOR_API_KEY` as found, and `NO_OPEN_BROWSER=1` (§3). No chat id:
    # there is no shell-prefix mirror (D9).
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

    _write_pid_and_host_json(agent, req, cli)
    _Host(agent, req, cli, _Peer(cli)).run()


if __name__ == "__main__":
    main()
