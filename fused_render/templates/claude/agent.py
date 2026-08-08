"""runPython target for claude/template.html: chat with the Claude Code
CLI about the target — a FOLDER (an app folder, or any other) or a file. This is
the only chat backend: it began as a fork of the plain chat template's agent
(the split view was the fork), kept every improvement that fork gained, and
absorbed the folder chat when D230 deleted the plain template and this one took
over its name.

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
recorded in a sidecar under home_dir()/sidecar/<mapped path>.json
(D83-reversal, D205 — see shared/appenv.py's sidecar_path), never beside
the target, and the template lists ONLY the sessions in that sidecar,
never the user's global session history. Claude runs with cwd = the
target file's directory and an appended system prompt that scopes it
(softly) to the file.

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
          "app_state": [{"id", "reason", "created_at"}, ...]  (unanswered only),
          "mode": <the mode this run is RUNNING in, not the picker's>}
  main(action="decide", run_id=..., request_id=..., decision="allow"|"deny",
       scope="once"|"session")        -> {"decided": ..., "decision": ...}
  main(action="app_state", run_id=..., request_id=..., state=<json string>)
                                      -> {"answered": ...}
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
from appenv import skill_plugin_dir as _skill_plugin_dir
from appenv import workspace_dir as _workspace_dir
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

# Where annotation screenshots land: a SIBLING of `runs`, not a child of a run
# dir. The ordering is what forces it — annotations are captured and uploaded as
# part of composing the outgoing message, and the run dir does not exist until
# `_start` runs, which is strictly after. A per-run directory would mean either
# writing the crops somewhere else first and moving them, or splitting `_start`
# in two; a stable directory the page can ask for at any time is neither.
#
# Under our own 0700 root (never the user's project — a screenshot is not their
# file), so the same privacy argument as the run dir covers it: another local
# account cannot read the pixels of the app on this user's screen.
#
# It holds the app-state DOM outlines too, which the page writes here rather than
# into the message (D217). Same kind of artifact under the same argument — a
# private, short-lived record of what was on the user's screen, handed to the
# agent and junk once the turn is over — and sharing this directory means one
# 0700 enforcement, one pruner and one `Read(...)` rule rather than two of each.
SHOTS = os.path.join(os.path.dirname(RUNS), "shots")

# How long a crop is kept, and how many are kept at all. Both are cleanup, not
# a quota: the page names the file it writes and the ONLY reader is the agent
# reading a path out of one turn's message, so a crop stops mattering when its
# conversation does. The TTL is generously longer than a session anyone would
# keep scrolling back through, and the count is the backstop for a machine that
# never idles long enough for the TTL to fire.
SHOTS_TTL = 12 * 3600
SHOTS_KEEP = 200

# Claude Code's own data dir, and it must be the SAME one the CLI itself uses —
# reading the wrong dir loses history and resume. CLAUDE_CONFIG_DIR wins where
# it is set, which now means only where the user set it: the supervisor no
# longer overrides it, because that dir also holds the login credentials on
# Linux and Windows (see supervisor/paths.py child_environment).
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
# The same server's second tool, which the MODEL calls: "what is the app in the
# left pane doing right now". Pre-allowed on the spawn line (see _start) —
# carding a read of the page the user is already looking at would be a prompt
# with no decision in it, once per edit.
APP_STATE_TOOL = "app_state"
# The delimiters the PAGE wraps its send-time snapshot in. Stripped from every
# user-facing copy of a message (the run's `meta.json`, hence the sidecar
# preview, the commit subject and a re-attach match — plus the restored
# transcript in `_history`), because the user typed the message, not the block.
# Duplicated in template.html, which writes it; a test asserts the two agree
# (D146: a duplicated rule needs a test, not a comment).
APP_STATE_TAG = "live-app-state"


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


def _plugin_argv() -> list:
    """`["--plugin-dir", <root>]` when fused-render has a skill plugin to hand
    this session, else `[]`.

    This is how a session we launch gets the fused-render skills with certainty
    instead of hoping the user-level sync landed somewhere the CLI reads (D216).
    The path (and the decision to pass it at all — see appenv) arrives through
    the env contract, so `_start` neither imports the app nor shells out to
    interrogate the CLI. A `--plugin-dir` load is session-scoped and additive:
    the user's own skills, plugins, CLAUDE.md and settings are all untouched,
    and a user who installed the published plugin themselves just sees the same
    skills listed twice."""
    root = _skill_plugin_dir()
    return ["--plugin-dir", root] if root else []


def _bad_id(value: str) -> bool:
    """Whether an id from the page is unsafe to join into a filesystem path.

    run ids and session ids both arrive as URL params and both get joined onto
    a directory we own, so neither may carry a path separator or a leading dot
    — and on Windows `\\` escapes exactly like `/`, while a drive prefix
    ("d:x") makes os.path.join drop our directory entirely."""
    return not value or value.startswith(".") or any(c in value for c in "/\\:")


def _workdir(file: str) -> str:
    """Claude's cwd (and the session-store key) for a target. A directory
    target — this template's app-folder role opens whole project folders — IS
    the working directory; a file target keeps the historical rule: its
    parent. Everything keyed on the cwd (the ~/.claude/projects munge, the
    sidecar `cwd` field) goes through this one rule so files and folders
    can't drift apart."""
    return file if os.path.isdir(file) else os.path.dirname(file)


def _system_prompt(file: str) -> str:
    """The FILE target's prompt: what to work on, plus the same app-state
    disclosure the directory prompt makes (D230).

    The file branch needs that second half for the same reason the directory
    branch does — a tool the model is never told about is a tool it never calls —
    but it needs a DIFFERENT description of what the pane is. A folder target
    frames the user's own app; a file target frames fused-render's preview OF
    their file (`code` for a `.py`, `duckdb` for a `.parquet`, the page itself
    for an `.html`). Saying "your app" there would invite edits to our template,
    so this says whose page it is and what it is good for: the annotations and
    crops the user takes on it point at THEIR file's content, and the console
    errors belong to the viewer unless the file being viewed is itself the page.
    """
    name = os.path.basename(file)
    tool = "mcp__%s__%s" % (PERMISSION_SERVER, APP_STATE_TOOL)
    return (
        f"You are embedded in a local file viewer, opened on {file}. "
        f"The user is looking at {name} right now; treat that file as the "
        "subject of this conversation — answer questions about it and make "
        "requested edits to it. Keep your work scoped to this file (and "
        "assets it directly references) unless the user explicitly asks for "
        "something broader. This is guidance, not a hard rule: follow "
        "explicit user instructions even when they go beyond the file. "
        f"Beside this chat the user sees {name} rendered in fused-render's "
        "own preview for that file type — their content, our viewer, so never "
        "edit the viewer. "
        f"`{tool}` reads that pane back: its DOM outline, URL params and "
        "console errors. Call it when the user points at something they can "
        "see, or after a change whose effect should show up there (the pane "
        "reloads itself when the file changes). Anything the user annotates or "
        f"screenshots in that pane is a part of {name}, not of the viewer. A "
        f"<{APP_STATE_TAG}> block on their message is the same reading taken "
        "at send time, and goes stale as soon as you edit anything."
    )


def _is_app_dir(file: str) -> bool:
    """Does this folder resolve to an app entry page? Same rule, same code, as
    the left pane's `app.py` and the `app` template (../shared/app_entry.py) —
    so the prompt's claim about what the pane is showing can never disagree with
    what it is actually showing. One listdir, on a directory the user just
    opened, in the agent process; the no-I/O discipline belongs to `condition.py`
    (which runs per stat), not here.
    """
    try:
        from app_entry import entry_html
        return entry_html(file) is not None
    except Exception:  # noqa: BLE001 — cannot tell -> the honest, weaker claim
        return False


def _split_system_prompt(file: str) -> str:
    """The DIRECTORY target's prompt. Two shapes, because there are now two kinds
    of folder: this template is the ONLY chat template, offered on every
    directory, not just app folders (the plain chat mode it absorbed was the
    directory chat).

    Both shapes carry the app-state disclosure, and for the same reason: a tool
    the model is never told about is a tool it never calls, and the tool's own
    description is not enough on its own — nothing in an ordinary session
    suggests that the page beside the chat can be read back. What they must NOT
    share is the DESCRIPTION of the pane, exactly as the file branch above does
    not share the folder branch's.

    * An APP FOLDER (`app_entry` resolves an entry page) keeps today's wording.
      Naming fused-render belongs HERE rather than being left to the starter
      `CLAUDE.md`: that file is the user's, in their folder, and a session opened
      on a project whose CLAUDE.md was edited away — or that predates it —
      otherwise has nothing telling it the HTML in front of it is an app with a
      Python bridge behind it. Same reliability argument as the skill plugin
      (D216): the thing the model must know cannot depend on a file we do not
      own. Still deliberately short of the file-scoping prompt above, which the
      directory branch of `_start` exists to avoid (see the comment there) — this
      says what the project IS, not what to work on.
    * An ORDINARY FOLDER gets the folder-scoping instruction, ported verbatim
      from the deleted plain chat template's own directory prompt rather than
      reinvented, so the folder chat reads the same as it always did. Saying
      "this is a fused-render project: its HTML is an app fused-render serves"
      here would be a plain lie about `~/Downloads`, and a lie that costs
      something: it invites the agent to look for a bridge that is not there and
      to treat a folder of PDFs as a codebase. The pane is described for what it
      is — fused-render's file browser, our UI, never a thing to edit — for the
      same reason the file branch says "our viewer": an app_state reading of it
      must not be mistaken for a reading of the user's own work.
    """
    tool = "mcp__%s__%s" % (PERMISSION_SERVER, APP_STATE_TOOL)
    if _is_app_dir(file):
        return (
            "This is a fused-render project: its HTML is an app fused-render serves, "
            "calling local Python through fused-render's bridge rather than a server "
            "you write. The `fused-render-authoring` skill documents that bridge — "
            "use it rather than inferring the API. "
            "The user sees the app rendered live beside this chat. "
            f"`{tool}` reports what that page is doing now: console errors, URL "
            "params, a DOM outline. Call it after any change that affects the page "
            "(it reloads itself — this is how you see whether the change worked), "
            "and whenever the user reports something visibly wrong. A "
            f"<{APP_STATE_TAG}> block on their message is the same reading taken at "
            "send time; it carries the outline either inline or as a `dom_path` to "
            "read, and goes stale as soon as you edit anything."
        )
    name = os.path.basename(file.rstrip("/")) or file
    return (
        f"You are embedded in a local file explorer, opened on the "
        f"folder {file}. The user is looking at {name} right now; treat "
        "that folder as the subject of this conversation — answer "
        "questions about its contents and make requested changes inside "
        "it. Keep your work scoped to this folder unless the user "
        "explicitly asks for something broader. This is guidance, not a "
        "hard rule: follow explicit user instructions even when they go "
        "beyond the folder. "
        f"Beside this chat the user sees fused-render's own file browser for "
        f"{name} — our UI listing their folder, so never try to edit it; it is "
        "not a page in their project and it has no source they own. They can "
        "walk into a file there while talking to you. "
        f"`{tool}` reads that pane back: its DOM outline, URL params and "
        "console errors. It reports the BROWSER, not the folder — use the "
        "ordinary file tools to find out what is in here. A "
        f"<{APP_STATE_TAG}> block on their message is the same reading taken at "
        "send time."
    )


# ------------------------------------------------------------- sidecar store

def _sidecar_path(file: str) -> str:
    # home_dir()/sidecar/<mapped path>.json (D83-reversal, D205), never a
    # sibling of the target. A folder target used to keep its session index
    # INSIDE the folder under a reserved dotfile name — `<folder>.json` as a
    # sibling would have collided with an ordinary user file (a `todo` project
    # beside a real `todo.json`) — but that collision risk doesn't exist once
    # the sidecar lives in its own tree under home_dir(), so a directory target
    # maps through the exact same function as a file target.
    from appenv import sidecar_path
    return sidecar_path(file)


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
    os.makedirs(os.path.dirname(path), exist_ok=True)
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

    No mount-read-only check anymore (D83-reversal, D205): the sidecar lives
    under home_dir()/sidecar/ now, never on `file`'s own mount, so a
    read-only remote source no longer has any bearing on whether its sidecar
    can be written.
    """
    data = _load_sidecar(file)
    now = time.time()
    cwd = _workdir(file)
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
    CURRENT cwd's project dir, so when the target file has moved (and its
    sidecar's mapped location along with it, purely by recomputation — the
    sidecar itself never physically moves), copy the transcript from the
    sidecar's recorded `cwd` into the new directory's project dir. No-op
    when it is already there; never
    overwrites an existing destination (the destination is where new turns
    append — it is always the newer copy). Best-effort: any failure just
    means claude reports the session as not found."""
    if _bad_id(session_id):
        return
    new_cwd = _workdir(file)
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


def _state_dir(run_dir: str) -> str:
    """Where `app_state` requests park — a SIBLING of `perm/`, never inside it.

    The page renders every request file in the perm dir as an approval card,
    and a snapshot read is not something to click: sharing the directory would
    put a card with no decision in it on screen once per edit."""
    return os.path.join(run_dir, "appstate")


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


# ---------------------------------------------------- annotation screenshots

def _wire_path(path: str) -> str:
    """The ONE spelling of a shots path: forward slashes, on every platform.

    Two places name this directory and they have to name it identically — the
    `Read(//…/**)` rule on the spawn line, and the crop paths the page puts in
    the annotation JSON — because the CLI matches a rule as TEXT, not as a
    resolved path (see `_read_rule`). On POSIX they agreed by accident. On
    Windows `SHOTS` comes off `os.path.join`, so the rule (which has always
    normalised) said `C:/Users/a/shots` while the page, joining with the
    separator it read off the directory, produced `C:\\Users\\a\\shots\\x.png`:
    the rule matched nothing, every crop raised a card, and the whole
    pre-approval was defeated.

    Forward slashes is the form that wins rather than backslashes because the
    crop is WRITTEN through `/api/fs/upload`, whose only requirement on the path
    is `os.path.isabs` — and Windows' `ntpath` accepts either separator, as does
    `open`. So one spelling satisfies both the rule and the write, and the page
    can join with a plain `/` instead of guessing a platform.
    """
    return path.replace("\\", "/")


def _read_rule(path: str) -> str:
    """A `Read(...)` permission rule scoped to everything under `path`.

    The DOUBLE slash is load-bearing and is the whole reason this is a function
    with a comment rather than an f-string at the call site: the CLI reads a
    rule path as relative unless it starts with `//`, so `Read(/tmp/x/**)`
    silently matches nothing and every crop raises a card. Verified against
    claude 2.1.221 — `Read(//<abs>/**)` allows a read under it and a sibling
    directory is still refused.

    The rule has to name the path in the SAME form the agent will be handed
    (this is the dir string the page puts in the message), because the CLI
    matches the text, not the resolved inode: a rule spelled with macOS'
    `/private/var/...` does not match a read of `/var/...` even though they are
    one directory. `tempfile.gettempdir()` is where both come from, so they
    agree by construction — and `_wire_path` is the single normalisation both
    this rule and the path handed to the page go through, so the separator
    cannot differ between them either."""
    return "Read(//%s/**)" % _wire_path(path).lstrip("/")


def _prune_shots() -> None:
    """Drop crops nobody will read again. Best-effort throughout: this is
    housekeeping on a temp directory, and no failure here is worth refusing the
    user a screenshot over."""
    try:
        names = os.listdir(SHOTS)
    except OSError:
        return
    now = time.time()
    aged = []
    for name in names:
        path = os.path.join(SHOTS, name)
        try:
            mtime = os.lstat(path).st_mtime
        except OSError:
            continue
        aged.append((mtime, path))
    stale = [p for m, p in aged if now - m > SHOTS_TTL]
    # Oldest first, so what survives the count cap is the recent conversation.
    aged.sort()
    excess = [p for _m, p in aged[:max(0, len(aged) - SHOTS_KEEP)]]
    for path in set(stale) | set(excess):
        try:
            os.unlink(path)
        except OSError:
            pass


def _shots_dir() -> dict:
    """Ensure the screenshot directory exists and hand its path to the page.

    Unlike a run dir this one is SHARED and long-lived, so an existing directory
    is adopted rather than refused — but only after `_require_private` vouches
    for it, which is the same check `_private_dir` runs on the parents it did not
    create. A directory another account planted here would otherwise let them
    read every crop, and the crops are pictures of the user's screen.

    Two failure shapes, deliberately different:

      a REFUSAL (`_require_private` raising) propagates. Somebody else's
        directory is here, and that is worth failing loudly over — an attacker
        who plants it can deny the user screenshots, which is not a disclosure.
      an ordinary OSError becomes an error DICT. A full disk or a file in the way
        means no screenshots, and the page degrades to sending the annotations
        without them. It must never mean no message.
    """
    if os.path.isdir(SHOTS):
        _require_private(SHOTS)
        # `_require_private` refuses a directory others can WRITE to, which is the
        # right test for a parent we did not create. It is not enough for this
        # leaf: a crop is a picture of the user's screen, so others must not be
        # able to READ it either. Tightened rather than refused because we own it
        # (that is what _require_private just established) and it holds nothing
        # but our own crops — the argument that stops us chmod'ing the temp root
        # does not apply to our own directory.
        #
        # Skipped entirely where there are no mode bits to reason about, on the
        # same grounds `_require_private` skips its uid check there: Windows has
        # no uid model and its temp dir is already per-user, and `os.chmod` can
        # only move the read-only flag, so enforcing 0700 there would refuse
        # every Windows user their screenshots for a permission model that does
        # not exist.
        if hasattr(os, "geteuid"):
            try:
                mode = stat.S_IMODE(os.lstat(SHOTS).st_mode)
                if mode & ~0o700:
                    os.chmod(SHOTS, 0o700)
                    # Re-read rather than trust the call: an ACL, or a filesystem
                    # that does not carry unix modes, can accept a chmod and keep
                    # the bits exactly where they were.
                    mode = stat.S_IMODE(os.lstat(SHOTS).st_mode)
            except OSError as e:
                return {"error": "could not secure the screenshot directory: %s" % e}
            if mode & ~0o700:
                # REFUSE, rather than write crops into a directory we have just
                # proved others can read. This is the asymmetry stated two
                # paragraphs up, applied to the case where the fix fails: denial
                # costs the user their screenshots, adopting costs them pictures
                # of their screen.
                return {"error": "the screenshot directory is readable by others "
                                 "(mode %04o) and could not be tightened" % mode}
    else:
        try:
            _private_dir(SHOTS)
        except FileExistsError:
            # Another page asked at the same moment. Theirs is fine if it is
            # ours; _require_private is what decides that (and raises if not).
            _require_private(SHOTS)
        except OSError as e:
            return {"error": "could not prepare the screenshot directory: %s" % e}
    _prune_shots()
    # `_wire_path`, not the raw join: this string is what the page joins crop
    # names onto, and it has to be spelled the way the Read rule spells it.
    return {"dir": _wire_path(SHOTS)}


def _write_mcp_config(run_dir: str) -> str:
    """The one-server MCP config that makes the chat window the permission
    prompt AND the app's own eyes (`app_state`), written into the run dir.
    Returns its path (for --mcp-config). Each channel gets its own directory in
    argv — see _state_dir for why they are not one.

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
            "args": [server, _perm_dir(run_dir), _state_dir(run_dir)],
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
    waiting on a window that is not coming back.

    Both kinds: an `app_state` read blocks the subprocess exactly like an
    approval does, so releasing only the approvals leaves a cancelled run
    parked for the app-state timeout with nobody left to answer it."""
    for perm in _permissions(run_dir):
        if not perm["decision"]:
            _write_decision(_perm_dir(run_dir), perm["id"],
                            {"decision": "deny", "reason": reason})
    _expire_app_state(run_dir, reason)


# --------------------------------------------------------- live app state
# The split view's second channel: the agent asks the page what the app in the
# left pane is doing (console errors, params, a DOM outline) through the same
# request-file round trip approvals use. Requests land in `appstate/`, the page
# answers them from its poll loop, and there is no card — this is a read of the
# user's own screen for the agent they are already talking to.

_APP_STATE_BLOCK = re.compile(
    r"<%s>.*?</%s>\s*" % (APP_STATE_TAG, APP_STATE_TAG), re.DOTALL)


def _strip_app_state(text: str) -> str:
    """`text` without any pushed app-state block. Non-greedy and anchored on
    the closing tag, so text that merely mentions the tag survives intact."""
    return _APP_STATE_BLOCK.sub("", text or "").strip()


def _app_state_requests(run_dir: str) -> list:
    """The app-state requests still waiting for an answer.

    UNLIKE `_permissions`, only the unanswered ones: there is no card to
    rebuild, so a re-attaching page has nothing to learn from an answered
    request — and replaying it would invite a second answer to a request whose
    latch is already closed."""
    state_dir = _state_dir(run_dir)
    try:
        names = sorted(n for n in os.listdir(state_dir) if n.endswith(".req.json"))
    except OSError:
        return []
    out = []
    for name in names:
        try:
            with open(os.path.join(state_dir, name), encoding="utf-8") as fh:
                req = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue  # half-written; the next poll gets it
        if not isinstance(req, dict) or _bad_id(str(req.get("id") or "")):
            continue
        if _read_decision(state_dir, req["id"]):
            continue
        out.append({"id": req["id"],
                    "reason": str(req.get("reason") or ""),
                    "created_at": req.get("created_at") or 0})
    return out


def _answer_app_state(run_id: str, request_id: str, state: str) -> dict:
    """Hand the page's snapshot to the waiting tool call.

    Same first-writer-wins latch as a decision (`_write_decision` writes the
    `.res.json` either way), because the same two races apply: the server's own
    timeout may have landed first, and a re-attaching page may answer twice.

    Every error carries `retry`, saying whether trying again could ever help —
    the page keys on that flag rather than on the wording. A write that did not
    reach disk is worth another poll (the window is alive and willing, the tool
    call is still blocked); an unknown run or request never will be, and a page
    retrying one every 400 ms until the run ends is strictly worse than a page
    that lets the tool's own timeout settle it.
    """
    run_dir = os.path.join(RUNS, run_id)
    if _bad_id(run_id) or not os.path.isdir(run_dir):
        return {"error": "unknown run_id", "retry": False}
    if _bad_id(request_id):
        return {"error": "unknown app-state request", "retry": False}
    state_dir = _state_dir(run_dir)
    if not os.path.isfile(os.path.join(state_dir, request_id + ".req.json")):
        return {"error": "unknown app-state request", "retry": False}
    try:
        snapshot = json.loads(state) if state else None
    except (TypeError, ValueError):
        snapshot = None
    if isinstance(snapshot, dict):
        payload = {"state": snapshot}
    else:
        # An empty snapshot would read as a page with nothing wrong with it,
        # which is the one wrong answer here: say the read failed instead.
        payload = {"error": "the window could not read the app's state"}
    # Never claim an answer the tool cannot read. `_write_decision` reports False
    # for a write that raised and left nothing behind (a full disk being the
    # ordinary cause), and discarding that told the page "answered" while the
    # tool call stayed blocked for its whole timeout — the same bug `_decide`
    # avoids by reading its verdict back instead of trusting the write.
    if not _write_decision(state_dir, request_id, payload):
        return {"error": "could not record the window's answer", "retry": True}
    return {"answered": request_id}


def _expire_app_state(run_dir: str, reason: str) -> None:
    """Release every unanswered app-state request. Called when the run is
    cancelled and when a poll first sees it finished: the page's poll loop stops
    with the run, so from that moment nothing will ever answer one."""
    for req in _app_state_requests(run_dir):
        _write_decision(_state_dir(run_dir), req["id"],
                        {"error": "the reply ended before the window answered "
                                  "(%s)" % reason})


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
           effort: str, permission_mode: str = "",
           message_via_stdin: bool = False) -> dict:
    file = os.path.abspath(file)
    # A directory is a valid target too: this template's app-folder role opens
    # whole project folders (cwd/prompt handled by _workdir/_system_prompt).
    if not os.path.exists(file):
        return {"error": f"target not found: {file}"}
    if session_id:
        _migrate_session(file, session_id)

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    run_dir = os.path.join(RUNS, run_id)
    _private_dir(run_dir)
    _private_dir(_perm_dir(run_dir))
    _private_dir(_state_dir(run_dir))

    # An unknown mode falls back to the strictest of the three rather than
    # erroring: a mangled param must not quietly buy more auto-approval than
    # the user picked.
    mode = permission_mode if permission_mode in PERMISSION_MODES \
        else DEFAULT_PERMISSION_MODE
    cli_mode = PERMISSION_MODES[mode]

    # `message_via_stdin` keeps the user's text out of argv entirely: the
    # message is written to a file in the run dir as one stream-json user
    # line, and the detached process reads it as its stdin (EOF after the one
    # message, so -p still exits after the turn). The apps API uses this — it
    # runs inside the server process, where argv is visible to every local
    # user via `ps`, unlike the template path where the message came from the
    # page's own runPython call.
    if message_via_stdin:
        with _private_open(os.path.join(run_dir, "stdin.jsonl")) as f:
            json.dump({"type": "user", "message": {
                "role": "user",
                "content": [{"type": "text", "text": message}]}}, f)
            f.write("\n")
        message_argv = ["-p", "--input-format", "stream-json"]
    else:
        message_argv = ["-p", message]

    cmd = [_claude_bin(), *message_argv,
           "--output-format", "stream-json",
           "--verbose", "--include-partial-messages",
           "--mcp-config", _write_mcp_config(run_dir),
           "--permission-prompt-tool",
           f"mcp__{PERMISSION_SERVER}__{PERMISSION_TOOL}",
           # Naming a permission-prompt tool also un-gates AskUserQuestion and
           # ExitPlanMode, which the CLI otherwise disables in headless mode.
           # This chat renders neither a question picker nor a plan dialog, so
           # keep them off: the change is about tool approvals and nothing else.
           "--disallowed-tools", "AskUserQuestion,ExitPlanMode",
           # Two pre-allowances, and they are the only ones — everything else
           # still raises a card. Both are the same thing in different clothes:
           # looking at the app the user is looking at.
           #
           #   the app_state tool — an MCP tool otherwise raises a card, so every
           #     app-state read would put a prompt on screen with no decision in
           #     it, for a read of the user's own screen by the agent they are
           #     already talking to.
           #   Read of the SHOTS dir — an annotation carries the path of a PNG
           #     crop of the element the user pointed at. The user attached it
           #     deliberately; carding it would make them approve their own
           #     screenshot. Scoped to that one directory, which holds nothing
           #     else and is not the user's project.
           #
           # Narrow by construction: one fully-qualified tool name and one
           # directory, and the prompt bridge stays wired for everything else.
           "--allowed-tools",
           f"mcp__{PERMISSION_SERVER}__{APP_STATE_TOOL}," + _read_rule(SHOTS)]
    cmd += _plugin_argv()
    # BOTH targets get an --append-system-prompt here, and they get different
    # ones. A FILE target gets the scoping prompt. A DIRECTORY target that is an
    # APP FOLDER still does NOT get a scoping prompt — the session should be plain
    # Claude Code in that project, with the user's own system prompt, CLAUDE.md,
    # skills and tools, and cwd (_workdir) as the only scoping — but it does get a
    # narrow prompt of its own. An ordinary folder DOES get folder-scoping, which
    # is what the deleted plain chat mode gave it; _split_system_prompt picks.
    #
    # What all shapes share is the app_state disclosure, because an un-announced
    # tool does not get called and every target now has a pane worth reading
    # back (D230). What they must NOT share is the DESCRIPTION of that pane: an
    # app folder frames the user's own app, an ordinary folder frames our file
    # browser, a file frames fused-render's preview of their file. Each prompt
    # says which, so the model never mistakes our UI for the user's code.
    cmd += ["--append-system-prompt",
            _split_system_prompt(file) if os.path.isdir(file)
            else _system_prompt(file)]
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
    # `message` here is the USER-FACING one: the page prepends a live-app-state
    # block for the model, and everything fed from meta.json is a copy of what
    # the user said — the sidecar preview, the commit subject, and the message a
    # re-attaching page compares against the bubble on screen (which shows the
    # typed text only, so an unstripped copy silently stopped matching). Stripped
    # here, once, rather than at each of those three readers.
    with _private_open(os.path.join(run_dir, "meta.json")) as f:
        json.dump({"file": file, "message": _strip_app_state(message),
                   "resumed_from": session_id, "mode": mode}, f)

    stdin_path = os.path.join(run_dir, "stdin.jsonl")
    stdin_fh = open(stdin_path, "rb") if message_via_stdin else None
    try:
        with _private_open(os.path.join(run_dir, "out.jsonl")) as out, \
             _private_open(os.path.join(run_dir, "err.log")) as err:
            proc = subprocess.Popen(cmd, stdout=out, stderr=err,
                                    cwd=_workdir(file),
                                    stdin=stdin_fh or subprocess.DEVNULL,
                                    **_DETACH)
    finally:
        if stdin_fh is not None:
            stdin_fh.close()
    with _private_open(os.path.join(run_dir, "pid")) as f:
        f.write(str(proc.pid))
    return {"run_id": run_id}


def _app_dir_for(path: str) -> str:
    """The app folder containing `path`, or "" when it is not inside one.
    An app dir is exactly <workspace>/<tag>/<name> under the Fused workspace
    (appenv.workspace_dir). Mirrors fused_render/app_git.py:app_dir_for —
    keep the two in step (templates must not import fused_render, D166)."""
    root = _workspace_dir()
    ap = os.path.abspath(path)
    if not ap.startswith(root + os.sep):
        return ""
    parts = os.path.relpath(ap, root).split(os.sep)
    if len(parts) < 2 or parts[0].startswith(".") or parts[1].startswith("."):
        return ""
    return os.path.join(root, parts[0], parts[1])


def _commit_turn(file: str, message: str) -> None:
    """FALLBACK sweep: commit whatever a finished turn left UNcommitted in
    the target's APP repo.

    App folders are version-controlled from creation (fused_render/app_git.py)
    and the app's CLAUDE.md instructs claude to commit its own work in small
    chunks as it goes — when it did, the tree is clean and this is a no-op.
    This sweep only catches the turns where that instruction was not honoured,
    so no turn's work is ever left outside history. Hard-scoped: a
    target outside an app dir, or an app dir without a `.git`, commits nothing
    — this template also chats about files in arbitrary folders, and silently
    committing into a user's real repository is the one wrong move.

    Best-effort throughout: no git, index.lock contention, nothing staged —
    all mean "no commit", never a poll error. Identity rides per-invocation
    (`-c user.*`) so a machine with no git config still commits, and `git -C`
    replaces cwd= to keep Popen on the posix_spawn path (see apps.py)."""
    app_dir = _app_dir_for(file)
    if not app_dir or not os.path.isdir(os.path.join(app_dir, ".git")):
        return
    subject = " ".join((message or "").split())
    subject = "Claude: " + (subject[:60] + "…" if len(subject) > 60 else subject) \
        if subject else "Claude turn"

    def git(*args):
        return subprocess.run(
            ["git", "-C", app_dir, "-c", "user.name=Fused",
             "-c", "user.email=apps@fused.io", *args],
            capture_output=True, text=True, timeout=30, close_fds=False)

    try:
        # Legacy defense (D83-reversal, D205): sidecars now live under
        # home_dir()/sidecar/, never inside the app dir, so a fresh repo never
        # grows one of these files — but a repo from before the relocation may
        # still have an old co-located sidecar sitting in its tree, and this
        # sweep's add -A would commit it into app history. Mirror
        # app_git._ensure_excludes: append missing patterns to the repo-local
        # .git/info/exclude (never the user's .gitignore). Keep the pattern
        # list in step with app_git._GITIGNORE.
        exclude = os.path.join(app_dir, ".git", "info", "exclude")
        if os.path.isdir(os.path.dirname(exclude)):
            try:
                with open(exclude, encoding="utf-8") as fh:
                    have = {ln.strip() for ln in fh}
            except OSError:
                have = set()
            missing = [p for p in ("*.html.json", ".claude-split.json")
                       if p not in have]
            if missing:
                with open(exclude, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(missing) + "\n")
        if git("add", "-A").returncode != 0:
            return
        if git("diff", "--cached", "--quiet").returncode == 0:
            return  # nothing to commit (turn changed no files)
        git("commit", "-q", "-m", subject)
    except Exception:
        pass


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


def _retry_info(row: dict):
    """One `api_retry` row as the page's view of it, or None if unreadable.

    The CLI retries an overloaded or rate-limited request on its own and reports
    every attempt. `status` travels because 529 and 429 are different news for
    the user — the API is swamped vs. we are being throttled — and so does
    `max_retries`, because that budget is the CLI's and not ours to assume.
    """
    try:
        return {"attempt": int(row["attempt"]),
                "max_retries": int(row.get("max_retries") or 0),
                "delay_ms": int(row.get("retry_delay_ms") or 0),
                "status": int(row.get("error_status") or 0),
                "error": str(row.get("error") or "")}
    except (KeyError, TypeError, ValueError):
        return None


def _overload_error(error: str, info) -> str:
    """`error` rewritten to say what actually happened, for a run that died with a
    retry still in flight.

    The raw text is "API Error: 529 Overloaded" — accurate, but it reads as a bug
    in this app, says nothing about the attempts already spent on the user's
    behalf, and gives no hint that waiting a moment IS the remedy. The original is
    kept in parentheses because it is the part a bug report can be matched on.

    `info` is the retry that was live when the end arrived, NOT the run's retry
    tally. Keying off the tally was a bug: it survives a mid-turn retry that
    SUCCEEDED, so a later unrelated failure — a crashed tool, a bad edit, an auth
    error — was dressed up as an API overload and the real cause was buried. The
    tally still rides in the payload for the page; it just cannot decide this.
    """
    if info is None or not error:
        return error
    status = info.get("status") or 0
    spent = info.get("attempt") or 0
    what = ("the API was overloaded" if status == 529
            else "we were rate limited" if status == 429
            else "the API call kept failing")
    return ("Could not reach the API: %s, and %d retr%s did not clear it. "
            "Trying again in a moment usually works. (%s)"
            % (what, spent, "y" if spent == 1 else "ies", error))


def _skill_calls(row: dict) -> list:
    """The Skill invocations in one FINALIZED `assistant` row.

    This row rather than the streamed `content_block_start` for the same call:
    that one arrives with `input: {}` and the skill name only turns up as
    `input_json_delta` fragments that would have to be reassembled, while this
    one is already whole. Both are in the file, so reading only this one is also
    what keeps a call from being reported twice.

    A call whose name we cannot read is dropped rather than reported blank — an
    empty note row in the log would say less than no row at all.
    """
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") != "Skill":
            continue
        skill = (block.get("input") or {}).get("skill")
        if isinstance(skill, str) and skill and block.get("id"):
            out.append({"id": str(block["id"]), "skill": skill})
    return out


def _poll(run_id: str) -> dict:
    run_dir = os.path.join(RUNS, run_id)
    if _bad_id(run_id) or not os.path.isdir(run_dir):
        return {"text": "", "done": True, "session_id": "", "error": "unknown run_id",
                "permissions": [], "app_state": [], "skills": [], "retry": None,
                "retry_total": 0, "retry_status": 0}

    text_parts = []
    result_text = None
    new_session = ""
    done = False
    error = ""
    tokens_done = 0      # output tokens of finished messages this turn
    tokens_current = 0   # cumulative usage of the in-flight message
    phase = "thinking"
    pending_sep = False  # a message ended; separate it from the next one's text
    skills = []          # Skill invocations, in call order (see _skill_calls)
    retry = None         # the api_retry the request is sitting in RIGHT NOW
    retry_total = 0      # how many retries this run has seen at all
    retry_status = 0     # HTTP status of the last one (529 overloaded, 429 …)
    gave_up = None       # the retry still in flight when the run ended badly

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
        # Any of these means the request the retries were for went THROUGH.
        # Rows are in file order, so anything the model produced after an
        # `api_retry` ends it: the live retry state has to be transient, or the
        # page would go on saying "retrying" for the rest of the turn — a lie
        # for far longer than it was ever true. The TALLY below is deliberately
        # not cleared; "this turn was retried four times" is what makes a final
        # failure explainable.
        if t in ("stream_event", "assistant", "result"):
            # Kept for one row: a `result` clears the retry like anything else,
            # so without this the terminal row would erase the very evidence that
            # the run died mid-retry (see `gave_up` below).
            was_retrying, retry = retry, None
        else:
            was_retrying = None
        if t == "system":
            new_session = row.get("session_id", new_session)
            if row.get("subtype") == "api_retry":
                info = _retry_info(row)
                if info is not None:
                    retry = info
                    retry_total += 1
                    retry_status = info["status"]
        elif t == "assistant":
            skills += _skill_calls(row)
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
                # Only if the failure arrived DURING a retry. A retry earlier in
                # the turn that then succeeded says nothing about why this ended.
                gave_up = was_retrying

    # Last word on the verb: a run sitting in a retry is not thinking, and
    # saying so is the whole point — "Thinking…" with a frozen token count is
    # indistinguishable from a hang, which is what an overload used to look like.
    if retry is not None:
        phase = "retrying"

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

    # Both error paths above converge here: if the end arrived with a retry in
    # flight then THAT is the story and the raw text does not tell it. `retry`
    # covers the abnormal exit — a process killed mid-backoff never writes the
    # `result` row that would have moved it into `gave_up`.
    error = _overload_error(error, gave_up or retry)

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

    # App-state reads, same shape but a different audience: nobody is asked
    # anything, so they never set `phase`. Released once the run is over for the
    # same reason a leftover card is expired — the page's poll loop stops with
    # the run, so from here on no answer can arrive and the blocked subprocess
    # (if it somehow outlives us) would wait out the full app-state timeout.
    if done:
        _expire_app_state(run_dir, "the run finished")
        app_state = []
    else:
        app_state = _app_state_requests(run_dir)

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

    # First poll that sees the run finished CLEANLY sweeps anything left
    # uncommitted into the app's repo (one-shot via a marker, like the
    # sidecar record below). This is a FALLBACK: the app's CLAUDE.md tells
    # claude to commit as it works and end every turn with a clean tree, so
    # when it honoured that this add -A finds nothing and no commit happens.
    # Errored turns are skipped — a crash mid-edit is not a state worth
    # enshrining; the next clean turn's sweep picks the survivors up. The
    # marker is claimed BEFORE the commit so a racing concurrent poll can't
    # double-commit.
    commit_marker = os.path.join(run_dir, "committed")
    if done and not error and "file" in meta \
            and not os.path.exists(commit_marker):
        try:
            fd = os.open(commit_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            _commit_turn(meta["file"], meta.get("message", ""))
        except OSError:
            pass  # another poll claimed it, or the run dir is going away

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
            "app_state": app_state, "mode": _live_mode(meta, permissions),
            "skills": skills, "retry": retry, "retry_total": retry_total,
            "retry_status": retry_status}


# ------------------------------------------------------- sessions & history

_MODEL_SHORT = ("fable", "opus", "sonnet", "haiku")
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _short_model(raw: str) -> str:
    """Collapse any spelling of a model — full id ('claude-fable-5'), alias
    ('opusplan'), or already-short name — to the selector's short names."""
    raw = (raw or "").lower()
    for name in _MODEL_SHORT:
        if name in raw:
            return name
    return ""


def _scan_transcript(path: str) -> tuple:
    """(model, effort) of the newest main-loop rows in one session transcript.

    Reads only the file's tail: the last rows are the last-used config, and a
    long session's early history can't change the answer. Sidechain rows are
    skipped — subagents pick their own model, and the user never chose it."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 262144:
                f.seek(size - 262144)
            blob = f.read().decode("utf-8", "replace")
    except OSError:
        return "", ""
    model = effort = ""
    for line in reversed(blob.splitlines()):
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        if not model:
            msg = row.get("message")
            if isinstance(msg, dict):
                model = _short_model(str(msg.get("model", "")))
        if not effort:
            e = str(row.get("effort", "")).lower()
            if e in _EFFORT_LEVELS:
                effort = e
        if model and effort:
            break
    return model, effort


def _defaults(file: str) -> dict:
    """The model/effort the user ACTUALLY last used with Claude Code for this
    project — through this template or the CLI directly — so the selectors can
    preselect a real config instead of a hardcoded guess.

    Priority: newest session transcripts in this project's store (true
    last-used, shared by CLI and template runs since both key sessions on the
    same cwd munge), then settings files (project .claude/settings.local.json,
    project .claude/settings.json, ~/.claude/settings.json — the `model` and
    `effortLevel` keys). Empty fields mean nothing was detected; the page keeps
    its own fallback."""
    workdir = _workdir(os.path.abspath(file))
    model = effort = source = ""
    proj = os.path.join(PROJECTS, _munge(workdir))
    try:
        names = [n for n in os.listdir(proj) if n.endswith(".jsonl")]
        paths = sorted((os.path.join(proj, n) for n in names),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        paths = []
    # Newest few only: the newest transcript IS the answer when it has both
    # fields, and one more file covers a fresh session that hasn't spoken yet.
    for path in paths[:5]:
        m, e = _scan_transcript(path)
        model = model or m
        effort = effort or e
        if model or effort:
            source = "session"
        if model and effort:
            break
    if not (model and effort):
        for p in (os.path.join(workdir, ".claude", "settings.local.json"),
                  os.path.join(workdir, ".claude", "settings.json"),
                  os.path.join(CLAUDE_DIR, "settings.json")):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if not model:
                m = _short_model(str(data.get("model", "")))
                if m:
                    model, source = m, source or "settings"
            if not effort:
                e = str(data.get("effortLevel", "")).lower()
                if e in _EFFORT_LEVELS:
                    effort, source = e, source or "settings"
            if model and effort:
                break
    return {"model": model, "effort": effort, "source": source}


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
    path = os.path.join(PROJECTS, _munge(_workdir(file)),
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
            # The transcript holds what claude was SENT, so a pushed app-state
            # block comes back on every restore. The user never typed it and
            # never saw it — showing them a screenful of JSON they don't
            # recognise is the whole reason it is stripped here.
            text = _strip_app_state(text)
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
         scope: str = "once", permission_mode: str = "", mode: str = "",
         state: str = "") -> dict:
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
    if action == "app_state":
        # `state` arrives as a JSON string, not a nested object: params reach
        # main() through the URL/param binder (str-shaped), and the snapshot is
        # the page's own structure — nothing here reads inside it.
        return _answer_app_state(run_id, request_id, state)
    if action == "sessions":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _sessions(file)
    if action == "defaults":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _defaults(file)
    if action == "history":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _history(file, session_id)
    if action == "shots_dir":
        # Asked for by the page BEFORE it composes a message, because that is
        # when it has crops to upload — see SHOTS for why this is not a run dir.
        return _shots_dir()
    if action == "cancel":
        return _cancel(run_id)
    return {"error": f"unknown action: {action}"}
