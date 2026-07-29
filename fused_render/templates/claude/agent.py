"""runPython target for claude/template.html: chat with the Claude Code CLI
about the target file (POC).

The browser never owns the work: `start` detaches a claude subprocess whose
stream-json stdout goes to a log file in tmp; `poll` re-reads that file and
returns the accumulated assistant text so the page can render the reply as it
streams in. Stdlib only (plus ../shared/procutil).

Cross-platform: `claude` is looked up on PATH and then in the platform's known
install locations, because a Windows install commonly isn't on the PATH this
process inherited (_claude_bin); detaching, liveness and cancel each take the
win32 route where the POSIX one is absent or destructive (_DETACH, _alive,
_cancel).

Sessions are per-file. Every conversation started from this template is
recorded in a sidecar next to the target file — `<file>.json`, e.g.
`my-folder/sample.html` -> `my-folder/sample.html.json` — and the template
lists ONLY the sessions in that sidecar, never the user's global session
history. Claude runs with cwd = the target file's directory and an appended
system prompt that scopes it (softly) to the file.

Tool approvals are the browser's to give: claude is spawned with a
`--permission-prompt-tool` pointing at `permission_server.py` (a one-tool stdio
MCP server), which parks each request as a file under the run's `perm/` dir.
`poll` hands those to the page, `decide` writes the answer back, and the
blocked claude subprocess picks it up.

Actions:
  main(action="start", file=..., message=..., session_id="", model="", effort="")
      -> {"run_id": ...}
  main(action="poll", run_id=...)
      -> {"text": ..., "done": bool, "session_id": ..., "error": ..., "tokens": N,
          "phase": ..., "message": <the run's first message, for re-attach>,
          "permissions": [{"id", "tool", "input", "decision", "scope"}, ...],
          "mode": <the mode this run is RUNNING in, not the picker's>}
  main(action="decide", run_id=..., request_id=..., decision="allow"|"deny",
       scope="once"|"session")        -> {"decided": ..., "decision": ...}
  main(action="sessions", file=...)   -> {"sessions": [...]}   (sidecar only)
  main(action="history", file=..., session_id=...) -> {"turns": [...]}
  main(action="cancel", run_id=...)   -> {"cancelled": ...}
"""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "agent.py")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))
from procutil import pid_alive as _pid_alive

def _runs_root() -> str:
    """Where run dirs live: a per-user tree under the shared temp root.

    Per-user because one `fused_render_claude` shared by everybody cannot be
    both private and usable. At 0700 the first account to open a chat owns the
    namespace and every other local user is locked out — they cannot create a
    run at all. Loose enough for them to write means either world-writable
    (a hazard we would be creating ourselves) or readable, which is the
    disclosure the 0700 exists to prevent. Giving each uid its own root
    dissolves the conflict: nobody contends for anybody else's directory, and
    0700 on it is then simply correct.

    POSIX-only suffix: `geteuid` does not exist on Windows, whose temp dir is
    already per-user (%LOCALAPPDATA%\\Temp), so there is nothing to separate.
    """
    geteuid = getattr(os, "geteuid", None)
    suffix = "-%d" % geteuid() if geteuid is not None else ""
    return os.path.join(tempfile.gettempdir(),
                        "fused_render_claude" + suffix, "runs")


RUNS = _runs_root()

# Claude Code's own data dir. CLAUDE_CONFIG_DIR wins where it is set: the
# supervisor sets it for every packaged build (supervisor/paths.py
# child_environment), so the transcripts claude writes for OUR runs land there
# and not under ~/.claude — reading the wrong dir loses history and resume.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJECTS = os.path.join(CLAUDE_DIR, "projects")

# Where Claude Code installs `claude`, for when it isn't on our PATH. Windows is
# the case that needs this: a GUI-launched app inherits the PATH of its login
# session, so an install that appended to the *user* PATH afterwards stays
# invisible until the next sign-in — and the packaged app's PATH is the
# supervisor's, not a shell's. Ordered most-canonical first, `.exe` ahead of any
# `.cmd` shim: a shim's arguments are re-parsed by cmd.exe, and our argv carries
# arbitrary user text (-p) and the target path (--append-system-prompt).
_WINDOWS_CANDIDATES = (
    # native installer (irm https://claude.ai/install.ps1 | iex) — recommended
    r"%USERPROFILE%\.local\bin\claude.exe",
    # winget install Anthropic.ClaudeCode, via winget's own shim dir
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe",
    # npm install -g @anthropic-ai/claude-code, in npm's global prefix
    r"%APPDATA%\npm\claude.exe",
    r"%APPDATA%\npm\claude.cmd",
    # legacy local npm install, written by older Claude Code versions
    r"%USERPROFILE%\.claude\local\claude.exe",
)
_POSIX_CANDIDATES = ("~/.local/bin/claude", "/opt/homebrew/bin/claude",
                     "/usr/local/bin/claude")

# The MCP server + tool that `--permission-prompt-tool` names. The CLI addresses
# an MCP tool as mcp__<server>__<tool>, so neither half may contain "__".
PERMISSION_SERVER = "fused_approvals"
PERMISSION_TOOL = "approve"


_DEFAULT_WAIT = 3600
# (wait + 60) * 1000 has to stay inside the int32 millisecond ceiling the CLI
# clamps a per-server MCP timeout to (2147483647).
_MAX_WAIT = 2147423


def _permission_wait() -> int:
    """Seconds an unanswered request waits before denying itself.

    Read from the environment here, NOT just in permission_server: this side
    stamps the value into the generated mcp.json (both as the server's own env
    and as the CLI's per-call ceiling), so a hardcoded constant here silently
    overwrote whatever the user had set — the var read as configurable and was
    not. Nonsense values fall back rather than producing a run that gives up
    instantly or never."""
    raw = os.environ.get("FUSED_RENDER_PERMISSION_TIMEOUT")
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        # OverflowError is the one that is easy to miss: `int(float("inf"))`
        # raises it and it is NOT a ValueError, so `inf` (or 1e400, which
        # floats to inf) crashed this module at *import* — taking down every
        # action in the template, not just the one that reads the setting.
        return _DEFAULT_WAIT
    if seconds < 1:
        return _DEFAULT_WAIT
    # A bigger number is not a longer wait past this point: the CLI clamps a
    # per-server MCP timeout to int32 milliseconds, and _write_mcp_config sends
    # (wait + 60) * 1000, so anything above this is just out of range.
    return min(seconds, _MAX_WAIT)


PERMISSION_WAIT = _permission_wait()

# Tools for which "allow all of these for the rest of the reply" is offered.
# MUST stay identical to WHOLE_TOOL_GRANTABLE in template.html — the card is
# where the choice is made, this is where it is enforced, and a test asserts
# the two lists agree (D146: a duplicated rule needs a test, not a comment).
#
# Enforced here and not only in the page because the page is a view, and a
# view is the wrong place for the only copy of a security-relevant rule: any
# other caller of `decide` — a future surface, a hand-built request — would
# otherwise get a session-wide Bash grant the UI deliberately never offers.
WHOLE_TOOL_GRANTABLE = frozenset({
    "Edit", "Write", "Read", "Glob", "Grep", "NotebookEdit",
})

# How many approvals the user wants to be asked for, mapped onto the CLI's own
# --permission-mode. The prompt tool stays wired in ALL of them: the mode only
# decides how much is auto-approved before it is consulted, and whatever is
# left still has to be answerable or it goes back to being a silent refusal.
#
#   prompt      the CLI default — a card for anything not already allowed
#   acceptEdits file edits go through; Bash/web/everything else still cards
#   auto        the CLI's own classifier auto-approves what it judges safe,
#               and escalates the rest to a card (it is a broader opt-in, NOT
#               a blanket one — bypassPermissions is deliberately not offered)
PERMISSION_MODES = {"prompt": None, "acceptEdits": "acceptEdits", "auto": "auto"}
DEFAULT_PERMISSION_MODE = "prompt"

# Modes a card may switch the RUNNING session to, via a `setMode` permission
# update (the sibling of the `addRules` one "allow all" sends). Only the two
# that loosen toward Claude judging for itself: "prompt" is not here because
# tightening mid-turn is what the picker is for, and `bypassPermissions` is not
# here for the same reason it is absent from the picker — the goal is having
# Claude evaluate the request, not having nobody evaluate it.
SWITCHABLE_MODES = frozenset({"acceptEdits", "auto"})


def _claude_bin() -> str:
    """Path to the claude executable to run.

    FUSED_RENDER_CLAUDE_BIN (an explicit override, mirroring
    FUSED_RENDER_RCLONE_BIN) beats PATH, which beats the platform's known
    install locations. A stale override that isn't a file is ignored rather
    than allowed to shadow a real install."""
    override = os.environ.get("FUSED_RENDER_CLAUDE_BIN")
    if override and os.path.isfile(override):
        return override
    found = shutil.which("claude")
    if found:
        return found
    candidates = _WINDOWS_CANDIDATES if os.name == "nt" else _POSIX_CANDIDATES
    for candidate in candidates:
        resolved = os.path.expanduser(os.path.expandvars(candidate))
        if os.path.isfile(resolved):
            return resolved
    raise FileNotFoundError(
        "claude CLI not found — install Claude Code, put `claude` on the PATH "
        "of the environment that launched fused-render, or set "
        "FUSED_RENDER_CLAUDE_BIN to its full path. Also looked in: "
        + ", ".join(candidates)
    )


def _bad_id(value: str) -> bool:
    """Whether an id from the page is unsafe to join into a filesystem path.

    run ids and session ids both arrive as URL params and both get joined onto
    a directory we own, so neither may carry a path separator or a leading dot
    — and on Windows `\\` escapes exactly like `/`, while a drive prefix
    ("d:x") makes os.path.join drop our directory entirely."""
    return not value or value.startswith(".") or any(c in value for c in "/\\:")


def _system_prompt(file: str) -> str:
    name = os.path.basename(file)
    return (
        f"You are embedded in a local file viewer, opened on {file}. "
        f"The user is looking at {name} right now; treat that file as the "
        "subject of this conversation — answer questions about it and make "
        "requested edits to it. Keep your work scoped to this file (and "
        "assets it directly references) unless the user explicitly asks for "
        "something broader. This is guidance, not a hard rule: follow "
        "explicit user instructions even when they go beyond the file."
    )


# ------------------------------------------------------------- sidecar store

def _sidecar_path(file: str) -> str:
    return file + ".json"


def _mount_read_only(file: str) -> bool:
    """True when `file` sits under a read-only remote mount, where the sidecar
    write can never be accepted — with CacheMode=full the doomed upload lands
    in the VFS cache and 403-loops forever (the sidecar-write incident).

    Guarded lazy import: in the app this reads the mount store; a standalone
    copy of this template (no fused_render on the path) degrades to False, the
    pre-guard behavior. Deliberately not the stdlib-only rule the rest of this
    file follows (cf. templates/zarr_aoi/tile_server.py, which also reaches for
    a fused_render internal) — os.access(W_OK) can't see a remote's read-only
    -ness, so only the shell's flag can answer this."""
    try:
        from fused_render.shell.mounts import mount_read_only
        return mount_read_only(file)
    except Exception:
        return False


def _load_sidecar(file: str) -> dict:
    # Preserve every key we don't own (bookmarkHistory, lastSession, ...) so a
    # claude turn round-trips them instead of clobbering them off disk. Only the
    # claudeSessions key is normalised to a list. The remaining loss window is a
    # true read-modify-write interleave between the two writers (both read the
    # old file, both write) — acceptable under D3 (single local user, both
    # writes human-paced).
    try:
        with open(_sidecar_path(file), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("claudeSessions"), list):
        data["claudeSessions"] = []
    return data


def _save_sidecar(file: str, data: dict) -> None:
    path = _sidecar_path(file)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _record_session(file: str, session_id: str, message: str,
                    resumed_from: str) -> None:
    """Add/refresh a sidecar entry.

    Plain --resume keeps the session id, but --fork-session (and older
    claude versions) mint a new one — a resumed turn therefore replaces the
    old entry's id in place (keeping created_at/preview) so one conversation
    stays one row. `cwd` tracks where the transcript lives so a moved file
    can migrate it (see _migrate_session); refreshed every turn.

    No-op when `file` is inside a read-only remote mount: the sidecar write
    can't be accepted there (the sidecar-write incident). The chat and its
    transcript (~/.claude/projects) are unaffected — only this file's session
    list stays empty, so past conversations won't be listed/resumable from the
    template UI for a mounted file.
    """
    if _mount_read_only(file):
        return
    data = _load_sidecar(file)
    now = time.time()
    cwd = os.path.dirname(file)
    for entry in data["claudeSessions"]:
        if entry.get("id") in (session_id, resumed_from):
            entry["id"] = session_id
            entry["last_used"] = now
            entry["cwd"] = cwd
            return _save_sidecar(file, data)
    data["claudeSessions"].append({
        "id": session_id,
        "preview": message.strip()[:80],
        "created_at": now,
        "last_used": now,
        "cwd": cwd,
    })
    _save_sidecar(file, data)


# ---------------------------------------------------------- session transfer

def _munge(path: str) -> str:
    """A cwd's project-dir name under ~/.claude/projects: every
    non-alphanumeric char becomes '-' (claude-code's own rule, verified
    against real project dirs — '/', '.', '_' all map to '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def _migrate_session(file: str, session_id: str) -> None:
    """Copy-on-resume: claude's --resume only finds transcripts under the
    CURRENT cwd's project dir, so when the target file (plus sidecar) has
    been moved, copy the transcript from the sidecar's recorded `cwd` into
    the new directory's project dir. No-op when it is already there; never
    overwrites an existing destination (the destination is where new turns
    append — it is always the newer copy). Best-effort: any failure just
    means claude reports the session as not found."""
    if _bad_id(session_id):
        return
    new_cwd = os.path.dirname(file)
    dest_dir = os.path.join(PROJECTS, _munge(new_cwd))
    dest = os.path.join(dest_dir, session_id + ".jsonl")

    data = _load_sidecar(file)
    entry = next((e for e in data["claudeSessions"] if e.get("id") == session_id), None)

    if not os.path.exists(dest):
        old_cwd = (entry or {}).get("cwd", "")
        if not old_cwd or os.path.abspath(old_cwd) == os.path.abspath(new_cwd):
            return  # nowhere to copy from
        src = os.path.join(PROJECTS, _munge(old_cwd), session_id + ".jsonl")
        if not os.path.isfile(src):
            return  # transcript gone; claude will surface the error
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(src, dest)
        except OSError:
            return

    # keep the sidecar's cwd truthful so later resumes skip straight through
    if entry is not None and entry.get("cwd") != new_cwd:
        entry["cwd"] = new_cwd
        try:
            _save_sidecar(file, data)
        except OSError:
            pass


# ------------------------------------------------------------- tool approvals

def _perm_dir(run_dir: str) -> str:
    return os.path.join(run_dir, "perm")


def _within_our_tree(path: str) -> bool:
    """Whether `path` is our runs root or something under it — i.e. a
    directory we are responsible for, rather than the system's temp root."""
    root = os.path.dirname(RUNS)
    return os.path.abspath(path) == root or \
        os.path.abspath(path).startswith(root + os.sep)


def _require_private(path: str) -> None:
    """Refuse a directory in our tree that we did not make, or that anyone
    else can write to.

    The temp root is world-writable, and our path under it is *predictable* —
    `fused_render_claude-<uid>` names the victim. So another account can
    pre-create it, or `runs` inside it, before we ever run. Adopting that hands
    them the parent of every run dir, and the parent is enough: the sticky bit
    that stops one account renaming another's entry protects OUR entries in
    /tmp, but it is not inherited by a directory THEY created. They can rename
    the 0700 run dir aside the instant after `mkdir` returns and leave a
    world-readable one in its place, and the transcript, the user's message
    and every tool payload get written into it. The 0700 means nothing if the
    thing above it is theirs.

    `lstat`, not `stat`: a symlink is not a directory we own, however good its
    target looks. Raising is the right outcome — an attacker who plants the
    directory can deny us the chat, but a loud failure is not a disclosure.
    """
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(
            "%s is not a directory (a symlink or file is in the way)" % path)
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return  # Windows: no uid model here, and its temp dir is already per-user
    if st.st_uid != geteuid():
        raise PermissionError(
            "%s belongs to uid %d, not %d — refusing to keep this conversation "
            "under a directory another account controls" % (path, st.st_uid, geteuid()))
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError(
            "%s is writable by others (mode %04o) — refusing to keep this "
            "conversation there" % (path, stat.S_IMODE(st.st_mode)))


def _private_dir(path: str) -> None:
    """Create `path` and any missing parents as `rwx------`.

    The run tree lives under the shared temp root, and on a typical Linux box
    that means /tmp with a default 0755 for anything created in it — while a
    run dir holds the entire conversation: `out.jsonl` is the transcript,
    `meta.json` the user's message, and `perm/*.req.json` every tool payload
    there is (commands, edited file content, web inputs). None of it should be
    readable by another local account. macOS' per-user temp root happens to
    make the exposure moot there, which is exactly why it cannot be relied on.

    Levels are created one at a time because `os.makedirs` has applied `mode`
    to the leaf only since 3.7. Existing directories are deliberately NOT
    chmod'ed: the chain starts at a directory we do not own (tightening the
    temp root would be a far worse bug than the one being fixed), and the run
    dir underneath — always freshly created here, always 0700 — is the level
    that actually contains the data.

    Parents tolerate losing the race, the leaf does not. Our root and `runs`
    are shared by every run of ours, so two templates starting their first run
    at once both find them missing and both call mkdir — and the loser used to
    abort `_start`, so the user's message simply never sent. Whoever won is
    fine, PROVIDED it is ours (`_require_private`). The **leaf** stays an
    exclusive create: it is this run's private 0700 boundary, so finding one
    already there means a run-id collision or somebody else's directory, and
    quietly adopting it is the wrong answer.
    """
    path = os.path.abspath(path)
    missing = []
    head = os.path.dirname(path)
    while head and not os.path.isdir(head):
        head, tail = os.path.split(head)
        if not tail:
            break
        missing.append(tail)
    # `head` is the deepest thing that already exists. Anything from our own
    # root downwards has to be vouched for before we build on it; above that
    # is the temp root, which belongs to the system.
    if _within_our_tree(head):
        _require_private(head)
    for tail in reversed(missing):
        head = os.path.join(head, tail)
        try:
            os.mkdir(head, 0o700)
        except FileExistsError:
            # Somebody got here first. A concurrent run of ours is fine; a
            # directory another account planted is not.
            _require_private(head)
    os.mkdir(path, 0o700)


def _private_open(path: str):
    """`open(path, "w")` for a run-dir file, created `rw-------` whatever the
    umask is. Belt and braces next to the 0700 directory: the mode is set by
    the create itself, so the file is never briefly world-readable."""
    return os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "w", encoding="utf-8")


def _write_mcp_config(run_dir: str) -> str:
    """The one-server MCP config that makes the chat window the permission
    prompt, written into the run dir. Returns its path (for --mcp-config).

    The server path comes off HERE, not a fresh `__file__` read: under the
    optional fused engine (D69) this module is `exec`'d into a namespace that
    has no `__file__` at all, so reaching for it directly is a NameError for
    anyone with the `fused` extra installed. HERE is resolved once at import,
    behind the shim at the top of this file that covers both engines."""
    path = os.path.join(run_dir, "mcp.json")
    server = os.path.join(HERE, "permission_server.py")
    with _private_open(path) as fh:
        json.dump({"mcpServers": {PERMISSION_SERVER: {
            # sys.executable, matching how the app spawns every other helper
            # (executor.py): in the packaged .app that is the bundled python.
            "command": sys.executable,
            "args": [server, _perm_dir(run_dir)],
            "env": {"FUSED_RENDER_PERMISSION_TIMEOUT": str(PERMISSION_WAIT)},
            # Hard per-call ceiling for this server, and a permission card is a
            # tool call that lasts as long as the user takes to look at it. Set
            # above the server's own wait so an unanswered card returns OUR
            # "nobody answered" deny instead of the CLI's MCP-timeout error.
            "timeout": (PERMISSION_WAIT + 60) * 1000,
        }}}, fh)
    return path


def _permissions(run_dir: str) -> list:
    """Every permission request this run has raised, each with the user's
    decision if one has been made. The whole list, not just the unanswered
    ones: a frame that re-attaches mid-turn (mode switch, reload) has to be
    able to rebuild the cards it never saw."""
    perm_dir = _perm_dir(run_dir)
    try:
        names = sorted(n for n in os.listdir(perm_dir) if n.endswith(".req.json"))
    except OSError:
        return []
    out = []
    for name in names:
        try:
            with open(os.path.join(perm_dir, name), encoding="utf-8") as fh:
                req = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue  # half-written; the next poll gets it
        if not isinstance(req, dict) or _bad_id(str(req.get("id") or "")):
            continue
        res = _read_decision(perm_dir, req["id"])
        out.append({
            "id": req["id"],
            "tool": str(req.get("tool") or ""),
            "input": req.get("input") if isinstance(req.get("input"), dict) else {},
            "created_at": req.get("created_at") or 0,
            "decision": str(res.get("decision") or ""),
            "scope": str(res.get("scope") or ""),
            "mode": str(res.get("mode") or ""),
        })
    return out


# O_EXCL makes the decision file's EXISTENCE the latch, but its content lands a
# moment later — so for a few microseconds the file is there and unparseable.
# A reader that calls that "no decision" will happily substitute a verdict of
# its own for the one that actually won, which is how a card can say Allowed
# while claude was told Deny. Long enough to cover any real write; a file still
# unparseable after it is a writer that died, and every caller treats an
# unreadable decision as no answer, which denies.
DECISION_WRITE_WINDOW = 2.0


def _request_tool(req_path: str) -> str:
    """The tool a parked request is asking about, or "" if it can't be read —
    which lands outside WHOLE_TOOL_GRANTABLE, so an unreadable request cannot
    talk its way into a session-wide grant."""
    try:
        with open(req_path, encoding="utf-8") as fh:
            req = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return ""
    return str(req.get("tool") or "") if isinstance(req, dict) else ""


def _read_decision(perm_dir: str, request_id: str, wait: float = 0.0) -> dict:
    """The decision on disk, or `{}` when there is none.

    `wait` seconds are spent re-reading a file that EXISTS but does not parse:
    that is a write in flight, not an absent answer (see the latch below).
    Callers that are about to fall back to a verdict of their own must pass a
    wait; `poll` must not (it runs every 400 ms and simply reports the request
    as still pending, which the next tick corrects)."""
    path = os.path.join(perm_dir, request_id + ".res.json")
    deadline = time.monotonic() + wait
    while True:
        try:
            with open(path, encoding="utf-8") as fh:
                res = json.load(fh)
        except OSError:
            return {}  # absent — and nothing is being written either
        except json.JSONDecodeError:
            res = None  # exists, not complete yet
        if isinstance(res, dict) and res:
            return res
        if time.monotonic() >= deadline:
            return {}
        time.sleep(0.02)


def _write_decision(perm_dir: str, request_id: str, payload: dict) -> bool:
    """Record one decision, first writer wins; True once a decision is on disk
    (this one, or whichever got there first).

    O_EXCL rather than the atomic temp+replace used elsewhere in this file,
    because the race that matters here is a *second* answer to the same request
    — a double-click, or cancel landing on a card the user just allowed —
    overwriting a verdict the tool may already have acted on."""
    path = os.path.join(perm_dir, request_id + ".res.json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return True
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        # Don't leave the corpse. An empty file holds the latch forever — every
        # later O_EXCL loses to it — while never parsing, so the request could
        # no longer be answered by anyone. Releasing it lets the next writer in.
        try:
            os.unlink(path)
        except OSError:
            pass
        return False
    return True


def _decide(run_id: str, request_id: str, decision: str, scope: str,
            mode: str = "") -> dict:
    run_dir = os.path.join(RUNS, run_id)
    if _bad_id(run_id) or not os.path.isdir(run_dir):
        return {"error": "unknown run_id"}
    if _bad_id(request_id):
        return {"error": "unknown permission request"}
    perm_dir = _perm_dir(run_dir)
    req_path = os.path.join(perm_dir, request_id + ".req.json")
    if not os.path.isfile(req_path):
        return {"error": "unknown permission request"}
    # Anything that is not an explicit allow is a deny: a mangled param must
    # fail closed, never grant.
    verdict = "allow" if decision == "allow" else "deny"
    if _alive(run_dir):
        # Narrow, never widen: a session grant is only honoured for the tools
        # the card offers it for. Asking for one on a Bash request — which the
        # UI never does — downgrades to allow-once instead of installing a
        # session-wide Bash rule, and the caller is told which scope it got.
        if scope == "session" and _request_tool(req_path) not in WHOLE_TOOL_GRANTABLE:
            scope = "once"
        payload = {"decision": verdict,
                   "scope": "session" if scope == "session" else "once"}
        # "…and stop asking": switch the running session's mode as well. Only
        # ever alongside an allow (a deny that also loosened the mode would be
        # incoherent), and only to a mode on the short switchable list — an
        # unrecognised one is dropped, never passed through to the CLI.
        if verdict == "allow" and mode in SWITCHABLE_MODES:
            payload["mode"] = mode
    else:
        # The run is over, so nothing will ever read this answer. Record the
        # expiry rather than the click: an Allow that was in flight when the
        # run died used to latch on disk all the same, and the card then read
        # "✓ Allowed" for a tool claude never ran — a permission UI telling the
        # user their grant took effect when it provably did not.
        payload = {"decision": "expired"}
    _write_decision(perm_dir, request_id, payload)
    # Report what is on disk, never what was clicked — the losing half of a
    # double-click must not show a verdict the tool will never see — and read it
    # back rather than trusting our own write, so the answer is the same one
    # claude will read. No decision here means nobody's write survived: claude
    # is still blocked, so say so instead of rendering the card as answered.
    res = _read_decision(perm_dir, request_id, wait=DECISION_WRITE_WINDOW)
    if not res.get("decision"):
        return {"error": "could not record that decision"}
    return {"decided": request_id,
            "decision": str(res["decision"]),
            "scope": str(res.get("scope") or ""),
            "mode": str(res.get("mode") or "")}


def _live_mode(meta: dict, permissions: list) -> str:
    """The mode the RUNNING claude process is actually in.

    Not the same thing as the picker's `permission` param, and conflating the
    two hid the one control that can fix it (Bugbot, PR #308): the picker takes
    effect at the next spawn, so switching it to "Claude decides" mid-turn left
    the live session in the strict mode, still carding — while the card's
    "Allow, and let Claude decide from here" button, gated on that param,
    vanished from every card built afterwards.

    Derived rather than stored, so it survives a re-attach and cannot drift
    from the decisions claude actually received: the spawn mode, re-pointed by
    each allow whose `setMode` reached disk, in the order they were answered.
    """
    mode = meta.get("mode")
    if mode not in PERMISSION_MODES:
        mode = DEFAULT_PERMISSION_MODE
    switches = [p for p in permissions
                if p.get("decision") == "allow" and p.get("mode") in SWITCHABLE_MODES]
    # by created_at, not by id: ids lead with HH%M%S, which misorders a run
    # spanning midnight.
    for perm in sorted(switches, key=lambda p: p.get("created_at") or 0):
        mode = perm["mode"]
    return mode


def _deny_pending(run_dir: str, reason: str) -> None:
    """Release every unanswered request so the blocked claude subprocess stops
    waiting on a window that is not coming back."""
    for perm in _permissions(run_dir):
        if not perm["decision"]:
            _write_decision(_perm_dir(run_dir), perm["id"],
                            {"decision": "deny", "reason": reason})


# ----------------------------------------------------------------- start/poll

# Detach the run so it outlives this 30 s executor subprocess. start_new_session
# (setsid) is POSIX-only — Windows ignores it silently, where DETACHED_PROCESS +
# CREATE_NEW_PROCESS_GROUP is the equivalent (mirrors templates/docs, latex and
# usd). Only the taken branch of the conditional is evaluated, so the win32-only
# subprocess constants are never touched on POSIX.
_DETACH = (
    {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    if os.name == "nt" else {"start_new_session": True}
)


def _start(file: str, message: str, session_id: str, model: str,
           effort: str, permission_mode: str = "") -> dict:
    file = os.path.abspath(file)
    if not os.path.isfile(file):
        return {"error": f"target file not found: {file}"}
    if session_id:
        _migrate_session(file, session_id)

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    run_dir = os.path.join(RUNS, run_id)
    _private_dir(run_dir)
    _private_dir(_perm_dir(run_dir))

    # An unknown mode falls back to the strictest of the three rather than
    # erroring: a mangled param must not quietly buy more auto-approval than
    # the user picked.
    mode = permission_mode if permission_mode in PERMISSION_MODES \
        else DEFAULT_PERMISSION_MODE
    cli_mode = PERMISSION_MODES[mode]

    cmd = [_claude_bin(), "-p", message,
           "--output-format", "stream-json",
           "--verbose", "--include-partial-messages",
           "--append-system-prompt", _system_prompt(file),
           "--mcp-config", _write_mcp_config(run_dir),
           "--permission-prompt-tool",
           f"mcp__{PERMISSION_SERVER}__{PERMISSION_TOOL}",
           # Naming a permission-prompt tool also un-gates AskUserQuestion and
           # ExitPlanMode, which the CLI otherwise disables in headless mode.
           # This chat renders neither a question picker nor a plan dialog, so
           # keep them off: the change is about tool approvals and nothing else.
           "--disallowed-tools", "AskUserQuestion,ExitPlanMode"]
    if cli_mode:
        cmd += ["--permission-mode", cli_mode]
    if session_id:
        cmd += ["--resume", session_id]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]

    # poll() records the session into the sidecar once claude reports its id;
    # it needs the file + first message, so stash them with the run.
    # `mode` is the mode this process was SPAWNED with, and it is recorded
    # because nothing else can reconstruct it: the picker's URL param is what
    # the *next* turn will use, so reading that back mid-turn describes a
    # session that does not exist yet. See `_live_mode`.
    with _private_open(os.path.join(run_dir, "meta.json")) as f:
        json.dump({"file": file, "message": message,
                   "resumed_from": session_id, "mode": mode}, f)

    with _private_open(os.path.join(run_dir, "out.jsonl")) as out, \
         _private_open(os.path.join(run_dir, "err.log")) as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err,
                                cwd=os.path.dirname(file),
                                stdin=subprocess.DEVNULL,
                                **_DETACH)
    with _private_open(os.path.join(run_dir, "pid")) as f:
        f.write(str(proc.pid))
    return {"run_id": run_id}


def _alive(run_dir: str) -> bool:
    """Whether this run's claude process is still going.

    procutil.pid_alive, NOT the POSIX `os.kill(pid, 0)` idiom: on Windows
    signal 0 *is* CTRL_C_EVENT, so os.kill routes it to
    GenerateConsoleCtrlEvent — it sends a real Ctrl+C where the pid resolves to
    a process group sharing our console, and raises OSError where it doesn't,
    which the POSIX idiom reads as "gone". Either way a poll kills or condemns
    the run it is only supposed to be looking at."""
    try:
        with open(os.path.join(run_dir, "pid"), encoding="utf-8") as fh:
            return _pid_alive(fh.read().strip())
    except OSError:
        return False


def _poll(run_id: str) -> dict:
    run_dir = os.path.join(RUNS, run_id)
    if _bad_id(run_id) or not os.path.isdir(run_dir):
        return {"text": "", "done": True, "session_id": "", "error": "unknown run_id",
                "permissions": []}

    text_parts = []
    result_text = None
    new_session = ""
    done = False
    error = ""
    tokens_done = 0      # output tokens of finished messages this turn
    tokens_current = 0   # cumulative usage of the in-flight message
    phase = "thinking"
    pending_sep = False  # a message ended; separate it from the next one's text

    try:
        lines = open(os.path.join(run_dir, "out.jsonl"), encoding="utf-8",
                     errors="replace").read().splitlines()
    except FileNotFoundError:
        lines = []

    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # half-written last line; next poll gets it
        t = row.get("type")
        if t == "system":
            new_session = row.get("session_id", new_session)
        elif t == "stream_event":
            ev = row.get("event", {})
            et = ev.get("type")
            if et == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    if pending_sep:
                        text_parts.append("\n\n")
                        pending_sep = False
                    text_parts.append(delta.get("text", ""))
                    phase = "composing"
                elif delta.get("type") == "thinking_delta":
                    phase = "thinking"
            elif et == "message_delta":
                usage = ev.get("usage") or {}
                tokens_current = usage.get("output_tokens", tokens_current)
            elif et == "message_stop":
                tokens_done += tokens_current
                tokens_current = 0
                # A tool-using turn is several assistant messages; without a
                # break their texts concatenate mid-word ("orange.After").
                pending_sep = bool(text_parts)
            elif et == "content_block_start":
                block = (ev.get("content_block") or {}).get("type")
                if block == "tool_use":
                    phase = "tooling"
        elif t == "result":
            done = True
            new_session = row.get("session_id", new_session)
            result_text = row.get("result")
            if row.get("is_error"):
                error = str(result_text or "claude exited with an error")

    if not done and not _alive(run_dir):
        # Dead without a `result` row = abnormal exit (crash, OOM, cancel),
        # even if some text streamed first. Report it as an error regardless
        # of partial text, so the UI doesn't render a truncated reply as a
        # clean success and the sidecar-record guard below skips it.
        done = True
        try:
            tail = open(os.path.join(run_dir, "err.log"), encoding="utf-8",
                        errors="replace").read().strip()
        except FileNotFoundError:
            tail = ""
        error = tail or ("claude exited before completing the reply"
                         if text_parts else "claude exited unexpectedly")

    # Approvals, after `done` is final. A card the user never answered is only
    # still live while the run is: once it ends, whatever the request was
    # waiting for is gone (the server denied itself at its own timeout, or the
    # subprocess died holding it), so mark it expired rather than leaving the
    # page rendering buttons that lead nowhere.
    permissions = _permissions(run_dir)
    if done:
        for perm in permissions:
            if not perm["decision"]:
                # Latch it, don't just label it. A payload-only "expired" left
                # the file unwritten, so a click still in flight landed on disk
                # afterwards and flipped the card to "✓ Allowed" for a tool the
                # dead run will never run. Re-read rather than assume: a real
                # answer racing this write wins the O_EXCL and is the truth.
                _write_decision(_perm_dir(run_dir), perm["id"],
                                {"decision": "expired"})
                perm["decision"] = str(
                    _read_decision(_perm_dir(run_dir), perm["id"]).get("decision")
                    or "expired")
    elif any(not p["decision"] for p in permissions):
        # A parked approval outranks whatever the stream last said it was
        # doing: the run is not thinking, it is waiting on the user.
        phase = "awaiting"

    # The run's own first message rides back on every poll so a re-attaching
    # page (mode switch / reload killed the poll loop, subprocess kept going)
    # can restore the user turn it never saw.
    try:
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, json.JSONDecodeError):
        meta = {}

    # First poll that sees the session id writes it to the sidecar (marker
    # file keeps the write one-shot across the remaining polls).
    marker = os.path.join(run_dir, "recorded")
    if new_session and not error and not os.path.exists(marker) and "file" in meta:
        try:
            _record_session(meta["file"], new_session, meta.get("message", ""),
                            meta.get("resumed_from", ""))
            open(marker, "w", encoding="utf-8").close()
        except OSError:
            pass  # sidecar bookkeeping must never break the chat itself

    # The streamed deltas are the full turn; the `result` row holds only the
    # LAST assistant message, so swapping to it after a tool-using turn threw
    # away every earlier message (the mid-sentence-freeze bug). Keep the
    # accumulated stream; fall back to `result` only when nothing streamed
    # (older CLI without --include-partial-messages).
    text = "".join(text_parts)
    if not text and done and result_text and not error:
        text = result_text
    return {"text": text, "done": done, "session_id": new_session, "error": error,
            "tokens": tokens_done + tokens_current, "phase": phase,
            "message": meta.get("message", ""), "permissions": permissions,
            "mode": _live_mode(meta, permissions)}


# ------------------------------------------------------- sessions & history

def _sessions(file: str) -> dict:
    """Sessions recorded in THIS file's sidecar, newest activity first."""
    file = os.path.abspath(file)
    sessions = sorted(_load_sidecar(file)["claudeSessions"],
                      key=lambda s: s.get("last_used", 0), reverse=True)
    return {"sessions": sessions}


def _history(file: str, session_id: str) -> dict:
    """Rebuild the conversation from the Claude Code session transcript.

    Resolved ONLY at the target file's own project dir — with copied files
    the same session id exists in several project dirs with divergent
    content, and a glob would render some other copy's conversation while
    resume continues this one's. Migrates first (same as `start`) so a moved
    file's saved session shows its turns immediately, without waiting for the
    user to send a message."""
    if _bad_id(session_id):
        return {"turns": []}
    file = os.path.abspath(file)
    _migrate_session(file, session_id)
    path = os.path.join(PROJECTS, _munge(os.path.dirname(file)),
                        session_id + ".jsonl")
    if not os.path.isfile(path):
        return {"turns": []}

    turns = []
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("isMeta") or row.get("isSidechain"):
            continue
        msg = row.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, str):
                text = content
            else:
                text = "\n".join(b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text")
            if text.strip() and not text.startswith(("<local-command", "<command-name")):
                turns.append({"role": "user", "text": text})
        elif role == "assistant" and isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                # consecutive assistant rows are one streamed turn; keep merged
                # (blank line between rows, matching _poll's stream separator)
                if turns and turns[-1]["role"] == "assistant":
                    turns[-1]["text"] += "\n\n" + text
                else:
                    turns.append({"role": "assistant", "text": text})
    return {"turns": turns}


def _cancel(run_id: str) -> dict:
    run_dir = os.path.join(RUNS, run_id)
    # Same guard as _poll: run_id is joined into a path and drives a kill,
    # so reject anything that could resolve outside the runs dir.
    if _bad_id(run_id) or not os.path.isdir(run_dir):
        return {"cancelled": run_id}
    # Answer before killing: the kill takes the whole tree (the MCP server
    # included) on both platforms, but if it fails, a parked approval would
    # otherwise sit there holding the subprocess open for the full timeout.
    _deny_pending(run_dir, "cancelled")
    try:
        pid = int(open(os.path.join(run_dir, "pid"), encoding="utf-8").read())
    except (OSError, ValueError):
        return {"cancelled": run_id}
    if os.name == "nt":
        # os.killpg doesn't exist on Windows, and CTRL_BREAK only reaches a
        # shared console — a DETACHED_PROCESS run has none. taskkill /T walks
        # the tree instead, collecting claude's own children with it.
        #
        # CREATE_NO_WINDOW because taskkill is itself a console program and this
        # worker has no console to lend it (executor.py spawns us with that same
        # flag), so without it a cancel flashes exactly the console window
        # _DETACH just removed from the run. The server's global no-window policy
        # does NOT cover us: it patches Popen in cli.py's process, and the worker
        # is a bare `python _child.py`.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)  # start_new_session=True -> pid is pgid
        except OSError:
            pass
    return {"cancelled": run_id}


def main(action: str = "start", file: str = "", message: str = "",
         session_id: str = "", model: str = "", effort: str = "",
         run_id: str = "", request_id: str = "", decision: str = "",
         scope: str = "once", permission_mode: str = "", mode: str = "") -> dict:
    if action == "start":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        if not message:
            return {"error": "(empty message)"}
        return _start(file, message, session_id, model, effort, permission_mode)
    if action == "poll":
        return _poll(run_id)
    if action == "decide":
        return _decide(run_id, request_id, decision, scope, mode)
    if action == "sessions":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _sessions(file)
    if action == "history":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _history(file, session_id)
    if action == "cancel":
        return _cancel(run_id)
    return {"error": f"unknown action: {action}"}
