"""runPython target for claude/template.html: chat with the Claude Code
CLI about the target — a FOLDER (an app folder, or any other) or a file. This is
the only chat backend: it began as a fork of the plain chat template's agent
(the split view was the fork), kept every improvement that fork gained, and
absorbed the folder chat when D235 deleted the plain template and this one took
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

Sessions are per-file. Claude runs with cwd = the target file's directory and
an appended system prompt that scopes it (softly) to the file.

The session LIST is the transcripts sitting in this cwd's ~/.claude/projects
dir — every chat about the same folder, whether started here or in a terminal.
Still not the global history; still scoped to this one cwd. See `_sessions`.

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
          "permissions": [{"id", "tool", "input", "decision", "scope",
                           "answers"}, ...],
          "app_state": [{"id", "reason", "created_at"}, ...]  (unanswered only),
          "mode": <the mode this run is RUNNING in, not the picker's>}
  main(action="decide", run_id=..., request_id=..., decision="allow"|"deny",
       scope="once"|"session", answers=<json string, AskUserQuestion only>,
       custom=<json string, the "Other" text per question, AskUserQuestion only>,
       note=<free text, ExitPlanMode deny only>)
                                      -> {"decided": ..., "decision": ...}
  main(action="app_state", run_id=..., request_id=..., state=<json string>)
                                      -> {"answered": ...}
  main(action="sessions", file=...)   -> {"sessions": [...]}
      every session about this target, newest first: the transcripts in this
      cwd's project dir (see _sessions)
  main(action="history", file=..., session_id=...) -> {"turns": [...]}
  main(action="snapshots", file=..., enrich=..., deltas=...)
      -> file_history.timeline(...) — Claude Code's checkpoints for this FILE
         (enrich="1" reads transcripts for the creation boundary; deltas="0"
          declines the per-version difflib, which is ~99% of the read)
  main(action="snapshot_plan", file=..., version_id=...)
      -> what going back to that snapshot would do (diff, counts),
         or `ok: False` + `error` saying why it cannot
  main(action="snapshot_revert", file=..., version_id=..., confirm_unique=...)
      -> {"ok": True, "action": "restore"|"delete",
          "timeline": {...}}  # version_id MUST come from a snapshot_plan call
  main(action="cancel", run_id=...)   -> {"cancelled": ...}
"""
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "agent.py")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))
from appenv import canvases_root as _canvases_root
from appenv import fused_cli_dir as _fused_cli_dir
from appenv import origin as _origin
from appenv import skill_plugin_dir as _skill_plugin_dir
from appenv import workbench_plugin_dir as _workbench_plugin_dir
from appenv import workspace_dir as _workspace_dir
from private_dir import private_dir as _private_dir_under
from private_dir import require_private as _require_private
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
# never idles long enough for the TTL to fire. Was 12h / 200: a restored turn
# re-reads its shots through /api/fs/raw, and a picture gone from a week-old
# conversation reads as a bug, so both were raised (2026-08-27) to 30 days /
# 1000 files — still bounded, no longer visible in ordinary use.
SHOTS_TTL = 30 * 24 * 3600
SHOTS_KEEP = 1000

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
# user-facing copy of a message (the run's `meta.json`, hence the commit
# subject and a re-attach match — plus the restored
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
#   plan        the CLI's own plan mode: claude is expected to research and not
#               modify anything until it calls ExitPlanMode with a plan for the
#               user to approve (the plan card). That "not modify anything" is
#               CLI-ENFORCED, not ours, and UNVERIFIED here against a live
#               headless run (queued for the end-to-end task) — the prompt tool
#               stays wired exactly as in every other mode, so an ordinary card
#               can still surface for some other tool while planning and stays
#               fully answerable if one does; only the ExitPlanMode card itself
#               is the intended way out (see `permChoices`' liveMode guard)
#   prompt      the CLI default — a card for anything not already allowed
#   acceptEdits file edits go through; Bash/web/everything else still cards
#   auto        the CLI's own classifier auto-approves what it judges safe,
#               and escalates the rest to a card (it is a broader opt-in, NOT
#               a blanket one — bypassPermissions is deliberately not offered)
PERMISSION_MODES = {"plan": "plan", "prompt": None,
                    "acceptEdits": "acceptEdits", "auto": "auto"}
DEFAULT_PERMISSION_MODE = "prompt"

# Modes a card may switch the RUNNING session to, via a `setMode` permission
# update (the sibling of the `addRules` one "allow all" sends). Only the two
# that loosen toward Claude judging for itself: "prompt" is not here because
# tightening mid-turn is what the picker is for, and `bypassPermissions` is not
# here for the same reason it is absent from the picker — the goal is having
# Claude evaluate the request, not having nobody evaluate it.
SWITCHABLE_MODES = frozenset({"acceptEdits", "auto"})

# The one tool whose card is not an approval: `AskUserQuestion` is the model
# asking the USER something, so what goes back is an answer. `decide` carries it
# as `answers` (a record keyed by the exact question text, value = the chosen
# option's label, or the chosen labels joined with ", " for a multi-select) and
# permission_server turns that into the `updatedInput` the CLI honours — see its
# module docstring for the wire and how it was pinned.
#
# Deliberately NOT in WHOLE_TOOL_GRANTABLE and never carrying a `setMode`: a
# question is one exchange, so "allow all of these in this reply" would mean
# answering the next question without asking, and a mode switch riding on it
# would loosen approvals for every later tool on the back of a click that said
# nothing about permissions.
ANSWERABLE_TOOL = "AskUserQuestion"

# The other tool whose card is not an ordinary approval: `ExitPlanMode` is the
# model asking to stop planning and start doing, and what is parked with it is a
# PLAN (`input.plan`, markdown) rather than a call to vet. The verdict is still an
# ordinary one — spiked against CLI 2.1.226: a plain `{"decision": "allow"}` is
# enough, because the CLI leaves plan mode itself when it sees one (it emits
# `system/status permissionMode:"default"` and the tool_result reads "User has
# approved your plan…"). What is special is only the DENY: "keep planning" has to
# tell the model to revise rather than to give up, and that sentence is composed
# here (see `_keep_planning`) rather than by whatever called `decide`.
#
# Deliberately NOT in WHOLE_TOOL_GRANTABLE: there is one plan, so "allow all
# ExitPlanMode in this reply" is either a grant for nothing or a pre-approval of
# the NEXT plan, unseen. A `setMode` MAY ride along on the allow, unlike a
# question card's — it is the mode the session lands in once planning is over,
# which is a statement about permissions — and it goes through the same
# SWITCHABLE_MODES gate as every other card's.
PLAN_TOOL = "ExitPlanMode"

# What "keep planning" tells the model. Page-independent on purpose: the deny
# message is the only thing the model reads off this card, and the page's half of
# it is a NOTE appended below, never the instruction itself.
KEEP_PLANNING = "Revise the plan — the user wants changes."
# How much of that note is carried. The user typed it, so it is not sanitised —
# it is BOUNDED: a pasted file must not become the deny message, and the control
# characters that are not whitespace have no business in a JSON string the CLI
# hands the model. The page mirrors this number as `PLAN_NOTE_LIMIT` (a
# `maxLength` on the textarea, so a user typing honestly never even reaches the
# cut) — a test holds the two together (D146).
NOTE_LIMIT = 2000
# The cut is never SILENT (D241's precedent: a size cap that just drops bytes
# without saying so reads as data loss, not a limit) — a note over the cap gets
# this appended, so both the user's own card and the sentence the model reads
# say plainly that something was left out, rather than quietly shortening it.
NOTE_TRUNCATED = f"\n[note truncated at {NOTE_LIMIT} chars]"


def _keep_planning(note: str) -> str:
    """The deny message for a plan sent back for revision, plus the user's note.

    Newlines and tabs survive (a note is allowed to be two lines); anything below
    them is dropped, and the whole thing is capped — visibly, with `NOTE_TRUNCATED`
    appended when the cap actually bit. Never markup and never tool input — this
    string only ever becomes the `message` of a deny."""
    text = "".join(ch for ch in str(note or "")
                   if ch in "\n\t" or ch >= " ")
    text = text.strip()
    cut = len(text) > NOTE_LIMIT
    text = text[:NOTE_LIMIT].strip()
    if not text:
        return KEEP_PLANNING
    return (KEEP_PLANNING + " The user's note: " + text
            + (NOTE_TRUNCATED if cut else ""))


def _multi_answer_ok(value: str, labels: list) -> bool:
    """Is `value` the ", "-join of a non-empty run of `labels`, in option order?

    Matched by CONSTRUCTION rather than by splitting on ", ", because a label may
    itself contain ", " and splitting would either accept a label the request
    never offered or reject one it did. Walked as the set of offsets into `value`
    reachable after consuming some prefix of the options, so a question with many
    options costs O(options x len(value)) rather than enumerating subsets.
    """
    reach = {0}
    for label in labels:
        nxt = set(reach)
        for off in reach:
            if not value.startswith(label, off):
                continue
            end = off + len(label)
            if end == len(value):
                return True          # consumed the whole answer, ending on a label
            if value.startswith(", ", end):
                nxt.add(end + 2)
        reach = nxt
    return False


def _answers_from(questions, answers, custom=None):
    """The answer record that may be latched, or None — which means deny.

    MUST stay identical to `_answers_from` in permission_server.py: this copy
    validates the click before it is written down, that one validates before the
    CLI is told about it, and a test runs both over one table (D146). Two copies
    because that server is spawned standalone by the CLI and imports nothing of
    ours.

    Every value has to be a label the PARKED REQUEST itself offered for that
    exact question, because the alternative failure is the model acting on a
    choice the user never made. An omitted question is allowed (the CLI reads it
    as unanswered, which is true); an invented one is not.

    `custom` is the one way a value that the model did not author gets through:
    the card's "Other" box (D407), keyed by the same question text, carrying what
    the USER typed. It is folded in as one extra option for that question and
    nothing more — the answer still has to match the option list exactly, the
    typed string still has to come last in a multi-select join, and free text for
    a question nobody asked is refused like any other invented answer. The
    distinction that survives is authorship: a label the model offered, or a
    sentence the user wrote — never a string neither of them ever produced.
    """
    if not isinstance(questions, list) or not questions:
        return None
    if not isinstance(answers, dict) or not answers:
        return None
    if custom is None:
        custom = {}
    if not isinstance(custom, dict):
        return None
    asked = {}
    for question in questions:
        if not isinstance(question, dict):
            return None
        text = question.get("question")
        options = question.get("options")
        if not isinstance(text, str) or not text or not isinstance(options, list):
            return None
        labels = [opt["label"] for opt in options
                  if isinstance(opt, dict) and isinstance(opt.get("label"), str)
                  and opt["label"]]
        # No usable option, or two questions an answer keyed by that text could
        # equally belong to: nothing here can be answered unambiguously.
        if not labels or text in asked:
            return None
        asked[text] = (labels, bool(question.get("multiSelect")))
    # The typed answers, each folded in as one more option for its own question.
    # Validated first and as a whole, on the same all-or-nothing rule as the
    # labels: free text for a question that was never asked is the same
    # fabrication as an invented label, and an empty box is not an answer.
    for text, typed in custom.items():
        if not isinstance(text, str) or text not in asked:
            return None
        if not isinstance(typed, str) or not typed:
            return None
        labels, multi = asked[text]
        # Typing out an option's own wording is not a second option: appending it
        # would let "Alpha, Alpha" match a multi-select join.
        if typed not in labels:
            # LAST, because the multi-select join is matched in option order and
            # the card puts the typed answer after everything ticked.
            asked[text] = (labels + [typed], multi)
    out = {}
    for text, value in answers.items():
        if not isinstance(text, str) or text not in asked:
            return None
        if not isinstance(value, str) or not value:
            return None
        labels, multi = asked[text]
        if not (_multi_answer_ok(value, labels) if multi else value in labels):
            return None
        out[text] = value
    return out


def _as_answers(answers: str):
    """The `answers` param as an object. It arrives as a JSON STRING like every
    other param (the URL/param binder is str-shaped), exactly as `app_state`
    sends its snapshot. Anything unparseable is handed on as-is so
    `_answers_from` rejects it — this function never decides anything."""
    if isinstance(answers, dict):
        return answers          # a direct caller (tests, the apps API)
    try:
        return json.loads(answers) if answers else None
    except (TypeError, ValueError):
        return None


# What the model is told when an answer arrives that the parked question cannot
# account for. Mirrors permission_server's BAD_ANSWER — this side writes it into
# the decision file, that side is where it reaches the CLI.
BAD_ANSWER = ("The answer could not be matched to the question that was asked, "
              "so nothing was recorded. Ask again if you still need it.")


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
    # claude_spawn.py recognizes this failure by the "claude CLI not found"
    # substring — keep the two in step if the wording changes.
    raise FileNotFoundError(
        "claude CLI not found — install Claude Code, put `claude` on the PATH "
        "of the environment that launched fused-render, or set "
        "FUSED_RENDER_CLAUDE_BIN to its full path. Also looked in: "
        + ", ".join(candidates)
    )


def _in_canvases_root(target: str) -> bool:
    """Whether `target` is inside the canvas-clones root.

    abspath + realpath + normcase + commonpath, and each of the four earns its
    place — every way this can be wrong ends in the gate silently withholding the
    workbench skills from a real canvas clone:

    * abspath, because callers do not all normalize first (`_terminal_command`
      does not) and a relative target would otherwise resolve against whatever
      cwd the server process happens to have;
    * realpath, because on macOS the root and the target routinely disagree about
      `/tmp` vs `/private/tmp` until both are resolved;
    * normcase, because Windows paths differing only in case (or in drive-letter
      case) are the SAME path — `apps.py`'s containment check normcases for the
      same reason;
    * commonpath rather than a string prefix, because `<root>-evil` starts with
      the root's characters and is a different directory entirely.

    A path that cannot be resolved at all is treated as OUTSIDE."""
    if not target:
        return False
    try:
        root = os.path.normcase(os.path.realpath(os.path.abspath(_canvases_root())))
        path = os.path.normcase(os.path.realpath(os.path.abspath(target)))
        return os.path.commonpath([root, path]) == root
    except (OSError, ValueError):
        # ValueError: commonpath refuses to mix an absolute and a relative path,
        # or paths on different Windows drives — both mean "not inside".
        return False


def _plugin_argv(target: str | None = None) -> list:
    """One `--plugin-dir <root>` per plugin root fused-render has to hand this
    session for THIS target, or `[]`.

    This is how a session we launch gets the fused-render skills with certainty
    instead of hoping the user-level sync landed somewhere the CLI reads (D216).
    The paths (and the decision to pass each at all — see appenv) arrive through
    the env contract, so `_start` neither imports the app nor shells out to
    interrogate the CLI. A `--plugin-dir` load is session-scoped and additive:
    the user's own skills, plugins, CLAUDE.md and settings are all untouched,
    and a user who installed the published plugin themselves just sees the same
    skills listed twice.

    TWO roots, because they are two separate plugins, and they are NOT handed out
    on the same terms:

    * fused-render's own skills (assembled by skill_plugin.py, shipped in this
      wheel) go to every session — the `fused` bridge contract is what every
      target shape needs.
    * the `workbench` plugin's canvas/UDF skills (fetched at runtime into a
      directory the app owns — see appenv.workbench_plugin_dir) go ONLY to a
      session whose target is inside the canvases root. A canvas clone's
      CLAUDE.md names them (the canvas.toml format reference above all), and
      nothing else does: handing them to a file, app-folder or plain-folder chat
      would load canvas/UDF guidance into a session with no canvas anywhere near
      it, which is noise at best and a wrong-tool suggestion at worst.

    The flag is repeatable, so the roots compose rather than needing a merged
    tree; either can be absent independently, and `target=None` (no target
    resolved yet) gets the ungated root only."""
    roots = [_skill_plugin_dir()]
    if target and _in_canvases_root(target):
        roots.append(_workbench_plugin_dir())
    return [arg for root in roots if root for arg in ("--plugin-dir", root)]


def _fused_cli_note() -> str:
    """The prompt paragraph disclosing the `fused` CLI, or "" when the server
    exported no wrapper (D334) — same rule as every other disclosure here: a
    tool the model is never told about is a tool it never calls, and a prompt
    promising a command the machine does not have is worse than silence.

    Appended to EVERY target's prompt (file, app folder, ordinary folder)
    rather than woven into each shape: the CLI is a fact about the machine,
    not about the target. Four things it must say, each guarding a real
    failure: run it as a BARE command (the `Bash(fused:*)` pre-allowance is a
    prefix rule, so `cd x && fused ...` still raises a card — correct, but
    surprising if unsaid, and the bare form is also the ONLY spelling that
    reaches the CLI this app ships, since the wrapper is what is on PATH);
    never reach for some other fused (a `pip install fused`, a `python -m
    fused`, another venv's copy — those miss the pieces the canvas sync needs
    and bypass the push protection); never run its login flows (they open a
    browser and a headless session hangs on them); and DO push inside a canvas
    clone with the standard command.

    That last one used to say the opposite — "let the sync push, rather than
    running `canvas push` yourself" — which was right when a hand-push meant an
    unguarded raw CLI call racing the watcher. It is now wrong twice: the
    auto-push is HELD while a session is live in the clone (so there is nothing
    to race, and a session that never pushes leaves its work unpublished until
    it ends), and `canvas push` inside a clone is intercepted into the guarded
    server-side push. Telling a session not to push now means telling it to
    finish blind."""
    if not _fused_cli_dir():
        return ""
    return (
        " The `fused` CLI is on PATH: use it when the user asks to push, "
        "pull or otherwise work with Fused (canvases, UDFs — e.g. `fused "
        "workbench canvas push <dir> --canvas <name>`; see `fused --help`). "
        "Run it as a plain `fused ...` command — that exact form is "
        "pre-approved, while compound commands (`cd x && fused ...`) ask the "
        "user first — and never invoke fused any other way: no `pip install "
        "fused`, no `python -m fused`, no copy from another path or "
        "environment, since only the bare command reaches the CLI this app "
        "ships. It uses the user's existing Fused sign-in; NEVER run "
        "`fused workbench login` or `fused cloud login` (they wait on a "
        "browser round-trip that cannot complete here) — on an auth error, "
        "ask the user to sign in from fused-render's Canvases page or a "
        "terminal instead. Inside a canvas folder under ~/.fused-render/"
        "canvases, fused-render holds its own auto-push while you work and "
        "routes `fused workbench canvas push .` through its sync manager, "
        "which merges concurrent workbench edits first: publish a coherent "
        "change set with that command and read the errors it prints back. "
        "See that folder's CLAUDE.md for the details."
    )


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
    parent. Everything keyed on the cwd (the ~/.claude/projects munge) goes
    through this one rule so files and folders can't drift apart."""
    return file if os.path.isdir(file) else os.path.dirname(file)


def _custom_env(origin: str, file: str) -> bool | None:
    """Does *file*'s own reader need a declared project environment, per
    `/api/env/custom-env`? None when the app couldn't be asked — a network
    hiccup on this prompt-building nicety must never fail the spawn, so any
    error (timeout, connection refused, a malformed response) is swallowed
    exactly like every other read in this module that decorates a screen
    rather than gating it (see artifacts.py's own "NOTHING HERE RAISES").
    `None` and `True` both mean "say nothing" downstream — the only value
    that unlocks the interpreter fact is a confirmed `False`.
    """
    try:
        url = origin + "/api/env/custom-env?" + urllib.parse.urlencode({"file": file})
        req = urllib.request.Request(url, headers={"X-Fused": "1"})
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("custom_env", True))
    except Exception:  # noqa: BLE001 — see docstring
        return None


def _system_prompt(file: str) -> str:
    """The FILE target's prompt: what to work on, plus the same app-state
    disclosure the directory prompt makes (D235).

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
    origin = _origin()
    # This session's OWN interpreter is a fact worth stating only when it is
    # KNOWN to be the one that read `file` — never a caveated guess. The claude
    # template's own folder never declares a project (SPEC PY-17), so this
    # process always runs on the app's own bundled interpreter; whether that
    # matches `file`'s actual reader depends on `file` itself, which
    # /api/env/custom-env resolves properly (a `.py` in a declared project, or
    # a data file whose template ships its own pyproject.toml — D276's
    # map/vector/pdf_studio and any future one — answers `custom_env: true`,
    # and this says nothing rather than assert a fact that might be wrong).
    # `origin` is None only when there is no server to ask (e.g. a bare test).
    custom_env = _custom_env(origin, file) if origin else None
    env_note = (
        f" For Python-based inspection of {name}, invoke the exact executable "
        f"`{sys.executable}`. Do not substitute `python` or `python3` from "
        "PATH; they may refer to a different environment."
    ) if custom_env is False else ""
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
        f"at send time, and goes stale as soon as you edit anything.{env_note}"
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


def _has_pane(file: str) -> bool:
    """Does this target get a LEFT PANE at all? THE FALLBACK ANSWER ONLY.

    Everything the pane implies hangs off one answer: the `app_state` tool's
    presence in the run's MCP roster, its pre-allowance on the spawn line, and
    whether the system prompt describes a page beside the chat. Only one target
    kind says no — an ordinary folder (D239), which gets a full-width chat.

    THE PAGE IS AUTHORITATIVE, NOT THIS FUNCTION, and `_start` prefers the page's
    answer whenever it is given one (`has_pane`). The question is "is there a page
    beside this chat", and only the page can answer it: `paneURL()` runs ONCE, in
    the boot IIFE, and `enterNoPane()` removes `#left` permanently — there is no
    re-resolution on the page side and there cannot be, because a pane cannot
    appear mid-session. Asking disk per turn therefore drifted, in both
    directions: scaffold an app into an ordinary folder and turn 2 offered a tool
    the page has no pane to answer with (the model calls it, `answerAppState`
    burns its null polls and replies APP_STATE_UNREADABLE — the one thing the page
    asserts can never be the answer to it); delete the entry page and the tool
    dropped while a live pane was still on screen.

    So this is what answers for a caller with NO page: the apps API, which spawns
    from inside the server process on a folder it has already resolved an entry
    for (routers/apps.py). Same predicate as everything else that branches on kind
    (`_is_app_dir` → `app_entry.entry_html`), so that fallback agrees with the
    pane a page would have built.
    """
    return not os.path.isdir(file) or _is_app_dir(file)


def _split_system_prompt(file: str, pane: bool) -> str:
    """The DIRECTORY target's prompt. Two shapes, because there are now two kinds
    of folder: this template is the ONLY chat template, offered on every
    directory, not just app folders (the plain chat mode it absorbed was the
    directory chat).

    `pane` IS THE ANSWER, PASSED IN — never re-derived here. For a directory the
    two are the same question ("does app_entry resolve an entry page?"), and
    asking it a second time reopened the window the single resolution in `_start`
    exists to close: an index.html appearing between the two calls (a concurrent
    scaffolding session, the user's editor, an in-flight `git checkout`), or a
    transient EMFILE/EIO hitting `_is_app_dir`'s blanket `except Exception: return
    False`, spawned a run WITHOUT the app-state directory — so `permission_server`
    omitted the tool — while this prompt announced it. Worse than either shape
    alone: an announced tool that is not in the roster is a promise the run cannot
    keep.

    The APP-FOLDER shape carries the app-state disclosure, for the reason D235
    gave: a tool the model is never told about is a tool it never calls, and the
    tool's own description is not enough on its own — nothing in an ordinary
    session suggests that the page beside the chat can be read back. The ordinary
    folder does NOT, because since D239 it has no pane and therefore no tool
    (`_has_pane`). What the two shapes must never share is the description of the
    pane, exactly as the file branch above does not share the folder branch's.

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
      to treat a folder of PDFs as a codebase. It says NOTHING about a pane and
      does not mention `app_state`, because as of D239 there is no pane: this
      target's chat is full width and the tool is not in the run's roster at all
      (`_has_pane`). The paragraph that used to be here described fused-render's
      own file browser beside the chat and warned that `app_state` "reports the
      BROWSER, not the folder"; it went with the pane it described. A prompt that
      tells the model what the user can see beside the conversation, when there
      is nothing beside the conversation, is a false claim about the screen — and
      announcing a tool the roster does not carry is worse than not announcing
      one, since an un-announced tool is merely unused.
    """
    tool = "mcp__%s__%s" % (PERMISSION_SERVER, APP_STATE_TOOL)
    if pane:
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
        "Use the ordinary file tools to find out what is in here."
    )


def _munge(path: str) -> str:
    """A cwd's project-dir name under ~/.claude/projects: every
    non-alphanumeric char becomes '-' (claude-code's own rule, verified
    against real project dirs — '/', '.', '_' all map to '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def _terminal_command(file: str, session_id: str = "") -> dict:
    """The shell command that continues (or starts) this target's session in a
    real terminal, for the page's "in terminal" menu item to put on the
    clipboard.

    Same session, same ground: the cwd is `_workdir` (the key everything else
    here uses) and the fused-render skills ride along via the same
    `--plugin-dir` the spawned runs get, so a session moved to the terminal
    keeps the skills it was using. Deliberately NOT carried over: the headless
    plumbing (-p, stream-json, the permission bridge, --append-system-prompt)
    — that machinery exists because a browser page cannot be a terminal, and
    an interactive `claude` brings its own. With a session id the transcript
    is migrated first (the same copy-on-resume a browser resume does), so
    `--resume` finds it from the cwd the command cd's into.

    The binary is spelled `claude` when PATH resolves it — a command the user
    reads and reuses should say what they would type — and falls back to the
    located absolute path only when it doesn't.

    The `fused` wrapper dir is PREPENDED to PATH for the handed-over command
    whenever the server exported one (`fused_cli_dir`, the same condition that
    gates the `Bash(fused:*)` pre-allowance and the prompt's CLI note). The
    sessions we spawn inherit that dir on PATH from the server process; a
    terminal the user opens themselves does not. Without this, `fused` in the
    continued session is not a wrong version — for a shipping user it is
    `command not found`, because fused-render bakes its own pre-release fused
    into the app's interpreter and they never installed one. Prepended rather
    than appended so the app's CLI also wins over any fused a developer does
    have, since only that one carries the manifest shims the canvas sync needs.
    Spelled as an ordinary PATH assignment, which is what a user would type."""
    if not file:
        return {"error": "missing target file (no _file param?)"}
    workdir = _workdir(file)
    if shutil.which("claude"):
        binary = "claude"
    else:
        try:
            binary = _claude_bin()
        except FileNotFoundError:
            # Not installed anywhere we know. Still hand over the command the
            # user WOULD run — pasted, it produces the shell's own "command
            # not found", which names the actual problem.
            binary = "claude"
    argv = [binary, *_plugin_argv(file)]
    if session_id:
        if _bad_id(session_id):
            return {"error": "malformed session id"}
        argv += ["--resume", session_id]
    cli_dir = _fused_cli_dir()
    if os.name == "nt":
        # cmd.exe quoting: bare when safe, double-quoted otherwise. shlex is
        # POSIX-only and its output misleads on Windows.
        def quote(s):
            return '"' + s + '"' if (" " in s or not s) else s
        parts = ["cd /d {}".format(quote(workdir))]
        if cli_dir:
            # `set` scopes to the shell the user pasted into, which is exactly
            # the lifetime we want: the session they just continued.
            parts.append('set "PATH={};%PATH%"'.format(cli_dir))
        parts.append(" ".join(quote(a) for a in argv))
        command = " && ".join(parts)
    else:
        run = shlex.join(argv)
        if cli_dir:
            # A one-command env prefix, so nothing outlives the session.
            run = "PATH={}:$PATH {}".format(shlex.quote(cli_dir), run)
        command = "cd {} && {}".format(shlex.quote(workdir), run)
    return {"command": command, "cwd": workdir}


# ------------------------------------------------------------- tool approvals

def _perm_dir(run_dir: str) -> str:
    return os.path.join(run_dir, "perm")


def _state_dir(run_dir: str) -> str:
    """Where `app_state` requests park — a SIBLING of `perm/`, never inside it.

    The page renders every request file in the perm dir as an approval card,
    and a snapshot read is not something to click: sharing the directory would
    put a card with no decision in it on screen once per edit."""
    return os.path.join(run_dir, "appstate")


def _private_dir(path: str) -> None:
    """shared/private_dir.py's `private_dir`, anchored at our own root (the
    parent of `RUNS`, read per call): anything from it downwards is vouched
    for before being built on; above it is the temp root, which belongs to
    the system."""
    _private_dir_under(path, os.path.dirname(RUNS))


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


def _attach_dirs(raw: str) -> list:
    """The directories THIS message's attachments live in, as the page reported
    them — one `Read(//dir/**)` rule each on the spawn line (`extra_read_dirs`).

    Why the page decides and not this module: the attachment list is the page's
    own state (`shotAttached`), it is per MESSAGE, and by the time `start` runs
    the paths are already inside the composed message where nothing can tell an
    attachment path from a path the user happened to type. So it is sent
    explicitly, as a JSON array of strings, on the same call as the message.

    VALIDATED HERE ANYWAY, because a grant is a grant: the parameter crosses the
    bridge as a plain string and this is the last place before it becomes a
    permission rule. Only absolute paths to directories that exist survive,
    deduplicated, and a filesystem ROOT is refused outright: `Read(///**)` is the
    whole disk, which is exactly the blanket rule this feature's own test asserts
    is never emitted.

    There is NO COUNT LIMIT (D617). A `_ATTACH_DIRS_MAX` of 4 used to sit here to
    keep the spawn line bounded, mirroring the page's own attachment cap; both
    are gone. A drop of thirty rows out of one folder was always ONE rule (they
    dedupe), and the pathological case — thirty rows out of thirty folders — is a
    long argv string, not an unsafe one, on a local tool the user drove by hand.
    Refusing the grant only cost them a permission card per attachment.

    Anything unparseable is an empty list, never an error. A refused grant costs
    the user one permission card; a refused SEND costs them their message.
    """
    if not raw:
        return []
    try:
        vals = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(vals, list):
        return []
    out = []
    for v in vals:
        if not isinstance(v, str) or not v.strip():
            continue
        path = v.strip()
        if not os.path.isabs(path) or not os.path.isdir(path):
            continue
        wired = _wire_path(os.path.normpath(path))
        # A root ("/", "C:/") has no parent to scope to — the rule would be the
        # whole disk. Also drops the shots dir if the page ever names it, since
        # its rule is unconditional.
        # Asked of the OS, not of the spelling: `os.path.dirname` of a root IS
        # the root on every platform ("/" and "C:\\" alike), where a string
        # strip of "C:/" left "C" and let the whole drive through on Windows.
        normed = os.path.normpath(path)
        if os.path.dirname(normed) == normed:
            continue
        if wired == _wire_path(SHOTS):
            continue
        if wired not in out:
            out.append(wired)
    return out


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


# The longest edge a transcoded picture is given, and the byte budget it has to
# land under. Both numbers are the PAGE's, deliberately: SHOT_VIEW_EDGE (1600) is
# what a capture of the whole pane is capped at and what the page's own downscale
# targets, and SHOT_ATTACH_MAX_BYTES (4 MB) is the downscale trigger it measures a picture
# against — a conversion that came back bigger or sharper than the app's own
# screenshots would be a second, quieter rule for the same thing.
SHOT_PNG_EDGE = 1600
SHOT_PNG_MAX_BYTES = 4 * 1024 * 1024
# What a PNG that missed the budget is re-tried as. Quality before resolution,
# same order as the page's encoder ladder: 1600px of a photo at q60 still answers
# every question the agent has of it, where 800px of it may not.
SHOT_JPEG_QUALITY = (90, 80, 70, 60)
# `sips` is a one-shot converter on a file the user just handed us; 20s is long
# enough for a 50 MP HEIC and short enough that a wedged binary does not hold the
# composer.
SHOT_SIPS_TIMEOUT = 20


def _in_shots(target: str) -> bool:
    """Whether `target` is a path INSIDE the shots directory.

    The same four-step containment `_in_canvases_root` documents (abspath,
    realpath, normcase, commonpath) and for the same four reasons — most sharply
    realpath here, because SHOTS lives under the macOS temp dir where `/var` and
    `/private/var` name one directory, so a string prefix would reject every real
    path the page sends. A path that cannot be resolved is treated as OUTSIDE.

    This is the whole authorisation for the transcode: the action reads bytes off
    disk and writes a sibling next to them, and the only place either is allowed
    to happen is the directory the page already uploads into and the pruner
    already owns."""
    if not target:
        return False
    try:
        root = os.path.normcase(os.path.realpath(os.path.abspath(SHOTS)))
        path = os.path.normcase(os.path.realpath(os.path.abspath(target)))
        return path != root and os.path.commonpath([root, path]) == root
    except (OSError, ValueError):
        return False


def _sips_to_png(path: str) -> str | None:
    """A macOS-only second opinion on bytes Pillow refused: `sips` through
    ImageIO, which decodes what the OS itself can — HEIC/HEIF above all, and the
    raw camera formats too.

    It exists because HEIC is the DEFAULT camera format on every iPhone and the
    one format both halves of this feature are blind to: no browser engine here
    decodes it, and Pillow needs `pillow-heif`, which is a compiled wheel we do
    not ship. Shelling out to a binary that is present on every macOS install
    costs nothing at rest and turns "this attachment is useless" into a PNG.

    Returns the temp file's path, or None for anything at all going wrong — a
    missing binary, a non-zero exit, a timeout, a format ImageIO does not know.
    The caller reports the ORIGINAL Pillow failure in that case, which is the
    honest one: sips is the fallback, not the diagnosis."""
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/sips"):
        return None
    try:
        fd, tmp = tempfile.mkstemp(prefix="conv-", suffix=".png", dir=SHOTS)
        os.close(fd)
    except OSError:
        return None
    try:
        proc = subprocess.run(
            ["/usr/bin/sips", "-s", "format", "png", path, "--out", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=SHOT_SIPS_TIMEOUT, check=False)
        if proc.returncode == 0 and os.path.getsize(tmp) > 0:
            return tmp
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return None


def _image_to_png(path: str) -> dict:
    """Transcode one picture in the shots directory into a PNG (or JPEG) beside
    it, and hand the page the copy's path and size.

    WHY THIS EXISTS AT ALL. D613 made an image this browser cannot decode travel
    as bytes with a note instead of a broken thumbnail, on the grounds that "the
    AGENT may well know a format this browser does not". For the two formats
    users actually drop — TIFF off a scanner or a GIS export, HEIC off an iPhone
    — that turned out to be false: the Read tool cannot open either, so the
    attachment was a path to bytes nobody in the conversation could look at. The
    page could see it was undecodable; it just had nowhere to send it. This is
    that somewhere, and it is on the python side because that is where Pillow and
    the OS's own decoders are.

    IT WRITES A SIBLING AND DELETES NOTHING. The original stays: it is what the
    user actually attached, the page may still want its byte count, and the
    pruner (30d/1000 files) already owns everything in this directory — a second
    deletion policy for a second file in the same directory would be the only
    thing here that could lose a user's picture early.

    PNG FIRST, JPEG ONLY IF PNG MISSES THE BUDGET. A screenshot, a scan and a
    diagram are the common TIFFs and all three are exactly what PNG is good at
    (and what JPEG's ringing ruins); a 12 MP photo is the common HEIC and is
    where a lossless 1600px re-encode blows past 4 MB. So the format follows the
    content instead of the extension, decided by measuring rather than guessing.

    NEVER RAISES. Every failure is `{"error": ...}` — the page's whole answer to
    one is to keep the bytes-plus-glyph attachment it already had, so an
    exception crossing the bridge would cost the user the attachment to report a
    problem with the attachment."""
    try:
        if not path or not os.path.isabs(path):
            return {"error": "not an absolute path"}
        if not _in_shots(path):
            # The page only ever asks about a file it just uploaded here. Anything
            # else is a bug or a caller that should not be reading the disk
            # through this action, and both get the same one sentence.
            return {"error": "not inside the attachments directory"}
        if not os.path.isfile(path):
            return {"error": "no such file"}
        try:
            from PIL import Image
        except ImportError:
            return {"error": "pillow is not installed"}

        tmp = None
        try:
            try:
                img = Image.open(path)
                img.load()
            except Exception as first:
                # HEIC without pillow-heif lands here, which is the common case
                # rather than the exotic one — hence the OS decoder below.
                tmp = _sips_to_png(path)
                if tmp is None:
                    return {"error": "could not decode: %s" % first}
                img = Image.open(tmp)
                img.load()
            # A multi-frame TIFF (a fax, a scanned stack) or an animated GIF has
            # one frame the user means by "the picture", and it is the first.
            try:
                if getattr(img, "n_frames", 1) > 1:
                    img.seek(0)
            except Exception:
                pass
            source_w, source_h = img.size
            if not source_w or not source_h:
                return {"error": "the picture has no pixels"}
            # Alpha is kept where it exists (a diagram with a transparent
            # background reads wrong flattened onto black) and dropped where it
            # does not — CMYK, 16-bit grey and palette all have to leave their
            # own mode either way, since PNG will not take some of them and the
            # agent's reader would not thank us for the rest.
            if img.mode in ("RGBA", "LA") or (
                    img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
            elif img.mode != "RGB":
                img = img.convert("RGB")
            if max(source_w, source_h) > SHOT_PNG_EDGE:
                img.thumbnail((SHOT_PNG_EDGE, SHOT_PNG_EDGE), Image.LANCZOS)
            width, height = img.size

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            data, ext = buf.getvalue(), ".png"
            if len(data) > SHOT_PNG_MAX_BYTES:
                flat = img
                if flat.mode != "RGB":
                    # JPEG has no alpha. White rather than black because these
                    # are documents and screenshots far more often than they are
                    # neon on dark.
                    bg = Image.new("RGB", flat.size, (255, 255, 255))
                    bg.paste(flat, mask=flat.split()[-1])
                    flat = bg
                for q in SHOT_JPEG_QUALITY:
                    jbuf = io.BytesIO()
                    flat.save(jbuf, format="JPEG", quality=q, optimize=True,
                              progressive=True)
                    data, ext = jbuf.getvalue(), ".jpg"
                    if len(data) <= SHOT_PNG_MAX_BYTES:
                        break
                # Past the ladder it goes out anyway: an oversize picture the
                # agent CAN read beats a perfectly sized one it cannot, and the
                # page reports the size it got.
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        base = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(SHOTS, base + ext)
        # 0600 on the create itself, like `_open_private`: the directory is
        # already 0700, and a converted picture of someone's screen or camera
        # roll should never be briefly wider than the original was.
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except BaseException:
            try:
                os.unlink(out)
            except OSError:
                pass
            raise
        return {"path": _wire_path(out), "width": width, "height": height,
                "bytes": len(data), "source_w": source_w, "source_h": source_h}
    except Exception as e:
        return {"error": "could not convert: %s" % e}


def _write_mcp_config(run_dir: str, pane: bool = True) -> str:
    """The one-server MCP config that makes the chat window the permission
    prompt AND — when the target has a left pane — the app's own eyes
    (`app_state`), written into the run dir. Returns its path (for --mcp-config).
    Each channel gets its own directory in argv — see _state_dir for why they are
    not one.

    `pane=False` omits the app-state directory from argv entirely, which is what
    takes the tool out of the server's roster (permission_server keys both its
    `tools/list` and its dispatch on having that directory). One switch for the
    channel and the tool, so they cannot disagree: a target with no pane (an
    ordinary folder, D239) has no page to answer a snapshot request, and a tool
    that can only time out is worse than a tool that is not there.

    The server path comes off HERE, not a fresh `__file__` read: under the
    optional fused engine (D69) this module is `exec`'d into a namespace that
    has no `__file__` at all, so reaching for it directly is a NameError for
    anyone with the `fused` extra installed. HERE is resolved once at import,
    behind the shim at the top of this file that covers both engines."""
    path = os.path.join(run_dir, "mcp.json")
    server = os.path.join(HERE, "permission_server.py")
    args = [server, _perm_dir(run_dir)]
    if pane:
        args.append(_state_dir(run_dir))
    with _private_open(path) as fh:
        json.dump({"mcpServers": {PERMISSION_SERVER: {
            # sys.executable, matching how the app spawns every other helper
            # (executor.py): in the packaged .app that is the bundled python.
            "command": sys.executable,
            "args": args,
            "env": {
                "FUSED_RENDER_PERMISSION_TIMEOUT": str(PERMISSION_WAIT),
                # UTF-8 stdio for the server, whatever the machine's locale is.
                # The CLI's MCP client is Node: it writes raw UTF-8 JSON with
                # non-ASCII unescaped, while Python decodes a pipe at the LOCALE
                # encoding — the ANSI code page on Windows, where a curly quote
                # in a `Write` payload used to kill the server before it parked
                # the request (no card, dead permission bridge, a turn that
                # simply stopped; see permission_server._utf8_stdio). It has to
                # be named HERE to reach the child at all: the MCP client passes
                # an allowlist of env vars plus exactly this dict, so an ambient
                # PYTHONUTF8 would not survive the spawn.
                "PYTHONUTF8": "1",
            },
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
        answers = res.get("answers")
        out.append({
            "id": req["id"],
            "tool": str(req.get("tool") or ""),
            "input": req.get("input") if isinstance(req.get("input"), dict) else {},
            "created_at": req.get("created_at") or 0,
            "decision": str(res.get("decision") or ""),
            "scope": str(res.get("scope") or ""),
            "mode": str(res.get("mode") or ""),
            # Only a question card has these, and it is the same reason the whole
            # list is returned: a frame that re-attaches has to be able to
            # rebuild a card it never saw, including what was chosen on it.
            "answers": answers if isinstance(answers, dict) else {},
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


def _request_asks(req_path: str) -> tuple:
    """(tool, input) for a parked request; ("", {}) if it can't be read.

    An unreadable request therefore lands outside WHOLE_TOOL_GRANTABLE (so it
    cannot talk its way into a session-wide grant) and outside ANSWERABLE_TOOL
    (so no answer can be validated against questions nobody can see)."""
    try:
        with open(req_path, encoding="utf-8") as fh:
            req = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return "", {}
    if not isinstance(req, dict):
        return "", {}
    body = req.get("input")
    return str(req.get("tool") or ""), body if isinstance(body, dict) else {}


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
            mode: str = "", answers: str = "", note: str = "",
            custom: str = "") -> dict:
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
    tool, asked = _request_asks(req_path)
    if _alive(run_dir):
        # Narrow, never widen: a session grant is only honoured for the tools
        # the card offers it for. Asking for one on a Bash request — which the
        # UI never does — downgrades to allow-once instead of installing a
        # session-wide Bash rule, and the caller is told which scope it got.
        if scope == "session" and tool not in WHOLE_TOOL_GRANTABLE:
            scope = "once"
        payload = {"decision": verdict,
                   "scope": "session" if scope == "session" else "once"}
        # "…and stop asking": switch the running session's mode as well. Only
        # ever alongside an allow (a deny that also loosened the mode would be
        # incoherent), and only to a mode on the short switchable list — an
        # unrecognised one is dropped, never passed through to the CLI.
        # A question card is excluded by tool, not by trusting it not to ask:
        # answering a question says nothing about how much to auto-approve.
        if verdict == "allow" and mode in SWITCHABLE_MODES and tool != ANSWERABLE_TOOL:
            payload["mode"] = mode
        if verdict == "deny" and tool == PLAN_TOOL:
            # "Keep planning": the one deny that carries a message of its own, so
            # the model revises the plan instead of reading a refusal and giving
            # up. The SENTENCE is ours (`_keep_planning`) and only the user's
            # optional note comes from the caller — and only for this tool, so a
            # `note` on anything else cannot rewrite another card's deny.
            payload["message"] = _keep_planning(note)
        if verdict == "allow" and tool == ANSWERABLE_TOOL:
            # The answer IS the payload here, so a click that carries no valid
            # one is recorded as a deny rather than as an allow the model would
            # read as "the user did not answer the questions". Validated against
            # the parked request's own questions, never against what was sent.
            typed = _as_answers(custom)
            picked = _answers_from(asked.get("questions"), _as_answers(answers),
                                   typed)
            if picked is None:
                payload = {"decision": "deny", "scope": "once",
                           "message": BAD_ANSWER}
            else:
                payload["answers"] = picked
                # Latched BESIDE the answer, not folded into it: the answer is
                # one string per question either way, and permission_server
                # re-validates from scratch — so it needs to be told which part
                # of that string the user typed rather than inferring it back
                # out of a join it must not try to split (D407).
                if typed:
                    payload["custom"] = typed
        # Anywhere else `answers` is simply not a field: dropping it here is what
        # keeps the page from adding keys to a tool input it does not own.
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
    landed = res.get("answers")
    return {"decided": request_id,
            "decision": str(res["decision"]),
            "scope": str(res.get("scope") or ""),
            "mode": str(res.get("mode") or ""),
            # What the card shows as chosen — the answer that WON the latch, so
            # the losing half of a double-click renders the other one's choice.
            "answers": landed if isinstance(landed, dict) else {}}


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
    each landed decision that MOVED it, in the order they were answered.

    Two kinds of decision move it, and the second one is not ours:

    * an `allow` carrying a validated `setMode` — the escalation button, and the
      optional landing mode on a plan approval;
    * an `allow` on `PLAN_TOOL`, with or without a `setMode`. The CLI leaves
      plan mode ITSELF the moment it sees one (D248's spike: it emits
      `system/status permissionMode:"default"` and the tool_result reads "User
      has approved your plan…"), so a derived mode that stayed `"plan"` was
      describing a session that had already left it — and `permChoices`' plan
      guard went on suppressing the mode-switch affordance on every later card
      of the run, for a plan mode nobody was in. Where it lands mirrors the
      picker write-back at template.html's `buildPlanCard.send` exactly: the
      granted `setMode` when the approval carried one, `DEFAULT_PERMISSION_MODE`
      ("prompt", the CLI's own default) otherwise. A "keep planning" `deny`
      moves nothing — the session is still planning, which is the point of it.
    """
    mode = meta.get("mode")
    if mode not in PERMISSION_MODES:
        mode = DEFAULT_PERMISSION_MODE
    moves = []
    for perm in permissions:
        if perm.get("decision") != "allow":
            continue
        if perm.get("mode") in SWITCHABLE_MODES:
            moves.append((perm, perm["mode"]))
        elif perm.get("tool") == PLAN_TOOL:
            moves.append((perm, DEFAULT_PERMISSION_MODE))
    # by created_at, not by id: ids lead with HH%M%S, which misorders a run
    # spanning midnight.
    for _perm, landed in sorted(moves, key=lambda m: m[0].get("created_at") or 0):
        mode = landed
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
    the closing tag, so text that merely mentions the tag survives intact.

    POSITION-INDEPENDENT, which is what separates it from `_strip_machinery`
    below: this one removes the block from wherever it sits (meta.json, the
    restored transcript), because those callers know the block is theirs. The
    other one only ever peels a LEADING block, because it runs over records
    nobody here wrote and a tag further in may be something a human typed."""
    return _APP_STATE_BLOCK.sub("", text or "").strip()


# ------------------------------------------------------- pane file on a record
#
# A MIRROR of `fused_render/tasks_store.py`'s `pane_file`, for the same reason
# `_strip_machinery` below is one: a template may not import fused_render (SPEC
# PY-15 / D166), and both sides have to get the same answer out of the same
# `~/.claude/projects` records. **Change one, change the other** — the parity
# test in tests/test_claude_sessions_merged.py pins them to identical output.
#
# What it reads and why it is the only durable record: the transcript's own
# `cwd` is always a FOLDER (`_workdir` resolves a file target to its parent
# before Claude Code ever sees it), so the pane's own url in the leading
# app-state block is the only place the FILE a chat was opened on survives.
_APP_STATE_LEAD = re.compile(
    r"<%s>(.*?)</%s>" % (APP_STATE_TAG, APP_STATE_TAG), re.DOTALL)


def _pane_file(text: str) -> str:
    """The file the LEADING app-state block says the pane was on, or "".

    Anchored, like every machinery matcher here: only a leading block is
    machinery, and the tag further in may be something a human typed. The
    state is prose followed by one JSON object, so the object is cut from
    first `{` to last `}` and parsed properly rather than regexed — a title
    containing `"url":` must not win.

    Three answers, in order of honesty. `entry` is the state's own name for the
    document the pane is about (template.html `appEntry`) and wins outright.
    The url is the fallback for older blocks, and there `_file` must beat
    `path`, because a templated preview's url is
    `/render?path=<template>&_file=<file>`: `path` names OUR template, which
    exists on disk and would sail through any isfile check as the target of
    somebody's chat about their own parquet file.

    A chat with no block ever — a folder chat, a terminal session — has no pane,
    and "" is the right answer for it rather than a failure.
    """
    match = _APP_STATE_LEAD.match((text or "").lstrip())
    if not match:
        return ""
    blob = match.group(1)
    start, end = blob.find("{"), blob.rfind("}")
    if start == -1 or end <= start:
        return ""
    try:
        state = json.loads(blob[start:end + 1])
    except ValueError:
        return ""
    if not isinstance(state, dict):
        return ""
    entry = state.get("entry")
    if isinstance(entry, str) and entry:
        return entry
    url = state.get("url")
    if not isinstance(url, str):
        return ""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    for key in ("_file", "path"):
        values = query.get(key)
        if values and values[0]:
            return values[0]
    return ""


# ------------------------------------------------ machinery on a user record
#
# A MIRROR of `fused_render/tasks_store.py`'s `strip_machinery` — same tag
# lists, same discipline, same answers — because a template may not import
# fused_render (SPEC PY-15 / D166) and this file has to read the same
# `~/.claude/projects` records the server's Tasks list reads. Two copies is the
# established shape for a rule that straddles that line (D253's model gate and
# reader, D301's `app_entry`); what keeps them honest is a test that pins the
# two to identical output over a corpus of real records, plus one that pins the
# lists themselves — see tests/test_claude_sessions_merged.py.
#
# **Change one, change the other.** tasks_store carries the corpus counts that
# justify the DROP/STRIP split; the short version is that DROP tags are Claude
# Code writing a `type: user` record on the user's behalf (never any prose after
# them), while STRIP tags are blocks THIS PAGE prepends to what the user typed
# (always prose after them). Putting a tag in the wrong list either surfaces
# machinery as a session's name or deletes the human's words.
_MACHINERY_DROP = (
    "task-notification",
    "command-message", "command-name", "command-args",
    "local-command-stdout", "local-command-stderr",
    "bash-input", "bash-stdout", "bash-stderr",
    "user-prompt-submit-hook", "system-reminder",
)

# Spelled out rather than built from `APP_STATE_TAG`: the parity test compares
# this tuple to the server's literal one, and a wire tag renamed on one side of
# that boundary has to fail loudly rather than drift quietly. (`pane-shot` has no
# constant on this side at all — only template.html, which writes the block,
# names it.)
_MACHINERY_STRIP = ("live-app-state", "pane-shot", "annotations")

_MACHINERY_TAGS = _MACHINERY_DROP + _MACHINERY_STRIP
_LEADING_MACHINERY = re.compile(
    r"<(%s)>.*?</\1>\s*" % "|".join(_MACHINERY_TAGS), re.DOTALL)
_LEADING_MACHINERY_OPEN = re.compile(r"<(%s)>" % "|".join(_MACHINERY_TAGS))
# The DROP half alone, for `_history`: those records are Claude Code writing on
# the user's behalf and are never a turn, where a STRIP tag is a block this page
# prepended to words the user really did type and must not take the turn with it.
_LEADING_DROP_OPEN = re.compile(r"<(%s)>" % "|".join(_MACHINERY_DROP))

# A synthetic `user` record the HARNESS writes when a background shell the turn
# started finishes (or is stopped): it wakes the run, and everything the agent
# says afterwards is the answer to it. Nobody typed it, so it may never render as
# a user bubble — it used to, as a screenful of raw XML — but it may not be
# silently dropped either, because it is the only explanation on screen for a
# reply that arrives with no message above it (D415).
_TASK_NOTIFICATION_OPEN = "<task-notification>"
_TASK_FIELD = re.compile(r"<(summary|status)>(.*?)</\1>", re.DOTALL)


def _task_notification(text: str) -> dict | None:
    """`{"summary", "status"}` for a task-notification record, else None.

    Matched on the OPENING tag only, and only at the head: the block is the
    whole record in practice, and a message that merely mentions the tag
    somewhere in its prose is a human writing about this feature, not the
    harness. A record whose `<summary>` is missing or empty still answers — with
    the status alone as its words — because the chip's job is to account for the
    turn underneath it, and "a background task finished" says that much."""
    if not isinstance(text, str) or not text.lstrip().startswith(_TASK_NOTIFICATION_OPEN):
        return None
    found = {m.group(1): m.group(2).strip() for m in _TASK_FIELD.finditer(text)}
    status = found.get("status", "")
    summary = found.get("summary", "")
    if not summary:
        summary = ("Background task %s" % status) if status \
            else "A background task woke the agent"
    return {"summary": summary, "status": status}


# `formatAnnotations`' block as it is written TODAY: an `<annotations>` tag
# holding one markdown stanza per pin. The tag is in `_MACHINERY_STRIP` above, so
# `_LEADING_MACHINERY` peels it like the other two blocks and there is no strip
# code of its own — but `_ann_notes` still has to read the user's words back out,
# and prose has no `content` key to ask for. A MIRROR of `tasks_store.py`'s
# `_ann_notes_md`, pinned to it over the shared corpus (D166: a template may not
# import fused_render, so the rule is written twice and tested once).
#
# The shape rules are the ones `formatAnnotations` writes and nothing else:
# stanzas separated by a blank line (the writer collapses any blank line INSIDE a
# note, so that boundary is unambiguous even though the composer takes a newline
# on Shift+Enter), the first paragraph the block's preamble (the only one not
# opening with `**A** — `), and inside a stanza the first line is the heading,
# the no-badge caveat and the no-words placeholder are OURS — matched exactly
# rather than as "any wholly-italic line", since a note that is one emphasised
# word is still a note — and what is left is what the user said.
_ANN_TAG = "annotations"
_ANN_BLOCK = re.compile(r"<%s>(.*?)</%s>" % (_ANN_TAG, _ANN_TAG), re.DOTALL)
_ANN_STANZA_HEAD = re.compile(r"^\*\*(.+?)\*\* — ")
_ANN_NO_WORDS = "_(no words for this spot)_"
_ANN_OFFSCREEN = re.compile(r"^_no badge on the overview: .+_$", re.DOTALL)

# The same block as it was written BEFORE that tag existed: a prose preamble and
# a fenced json payload, recognised at position ZERO because it has no tag —
# exactly the fragility the tag ended. Sessions on disk carry it forever, so this
# reader never goes away.
_ANN_PREAMBLE = "The user annotated "
_ANN_FENCE_OPEN = "\n```json\n"
_ANN_FENCE_CLOSE = "\n```"


def _ann_notes_md(block: str) -> str:
    """The user's words from one `<annotations>` block's stanzas, joined.

    Flattened with spaces, unlike the page's own reader (which rejoins with
    newlines): this builds a row TITLE, and a title is one line."""
    notes = []
    for para in re.split(r"\n\s*\n", block.strip()):
        lines = [ln.strip() for ln in para.strip().splitlines() if ln.strip()]
        if not lines or not _ANN_STANZA_HEAD.match(lines[0]):
            continue            # the preamble paragraph, or something we did not write
        said = [ln for ln in lines[1:]
                if ln != _ANN_NO_WORDS and not _ANN_OFFSCREEN.match(ln)]
        if said:
            notes.append(" ".join(said))
    return " · ".join(notes)


def _strip_ann_block(text: str) -> str:
    if not text.startswith(_ANN_PREAMBLE):
        return text
    open_at = text.find(_ANN_FENCE_OPEN)
    if open_at == -1:
        return text
    close_at = text.find(_ANN_FENCE_CLOSE, open_at + len(_ANN_FENCE_OPEN))
    if close_at == -1:
        return text
    return text[close_at + len(_ANN_FENCE_CLOSE):].lstrip("\n")


def _ann_notes(text: str) -> str:
    """The words the user typed INSIDE their pins, for a send that carried no
    free text at all — or "" when there are none.

    NOT part of `_strip_machinery`, deliberately. That function answers "what
    did the human TYPE in the composer", it is duplicated in
    `tasks_store.strip_machinery`, and the two are pinned character-identical
    over a corpus — so widening it to reach into an annotation payload would
    change every one of its readers at once. This is a SECOND source, consulted
    only where a nameless row is worse than an approximate one.

    Annotations carry a `content` field — the note the user wrote on the pin —
    which the block strip drops with the rest of the payload. A send that is
    ONLY annotations is therefore words the user typed, sitting in the record,
    that no reader would show: the chat vanished from "Recent chats" entirely
    (a `_cli_preview` of "" drops the session, not just its name) and its
    snapshot runbox could only call it "chat" plus a short id. Both from the
    same "".

    Joined in `t` order across pins, because that is the order the walkthrough
    was given in and the caller truncates to 80 chars anyway. A wordless send —
    a pin with no note, a bare screenshot — still yields "": there is nothing
    to name it with, which is the one case the empty answer was always for.
    """
    out = (text or "").strip()
    # TODAY'S shape first, and searched rather than anchored: the tag made this
    # block position-independent, so it is found wherever the send put it — no
    # peeling loop needed to expose it, unlike the untagged form below.
    found = _ANN_BLOCK.search(out)
    if found:
        return _ann_notes_md(found.group(1))
    while True:
        match = _LEADING_MACHINERY.match(out)
        if not match:
            break
        out = out[match.end():].strip()
    if not out.startswith(_ANN_PREAMBLE):
        return ""
    open_at = out.find(_ANN_FENCE_OPEN)
    if open_at == -1:
        return ""
    close_at = out.find(_ANN_FENCE_CLOSE, open_at + len(_ANN_FENCE_OPEN))
    if close_at == -1:
        return ""
    try:
        pins = json.loads(out[open_at + len(_ANN_FENCE_OPEN):close_at])
    except ValueError:
        return ""
    if not isinstance(pins, list):
        return ""
    notes = []
    for pin in pins:
        if not isinstance(pin, dict):
            continue
        note = pin.get("content")
        if isinstance(note, str) and note.strip():
            notes.append(note.strip())
    return " · ".join(notes)


def _strip_machinery(text: str) -> str:
    """What a human actually typed in one transcript record — every
    machine-written PREFIX peeled off — or "" if they typed no words at all.

    Loops because one send carries the blocks in combination (`composeOutgoing`
    fixes the order: state, pictures, notes, words) and peeling one exposes the
    next. A leading opener still standing at the end has no close in the string
    (a record caught mid-flush, or a head read cut inside a block), and
    everything from a machinery opener on is machinery whatever follows it."""
    out = (text or "").strip()
    while True:
        before = out
        match = _LEADING_MACHINERY.match(out)
        if match:
            out = out[match.end():].strip()
        out = _strip_ann_block(out).strip()
        if out == before:
            break
    return "" if _LEADING_MACHINERY_OPEN.match(out) else out


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


def _claude_argv(run_dir: str, pane: bool, cli_mode: str | None,
                 session_id: str, model: str, effort: str,
                 extra_read_dirs: list | None, file: str) -> list:
    """The CLAUDE CLI's own argv — everything from the binary down to the
    last `--effort` flag. Pulled out of `_start` so the session host, which
    spawns this SAME process (just kept open on a stdin pipe instead of a
    one-shot file), builds an identical command rather than a hand-rolled
    second copy that silently drifts from what `_start` used to build inline.

    Always `-p --input-format stream-json` on stdin, unconditionally: a
    session host is the only thing that ever calls this now, and it always
    holds the pipe open for the life of the process — there is no longer an
    argv-embedded-message form to choose between (the retired
    `message_via_stdin=False` branch)."""
    cmd = [_claude_bin(), "-p", "--input-format", "stream-json",
           "--output-format", "stream-json",
           "--verbose", "--include-partial-messages",
           # Every accepted line on stdin is echoed back on stdout as its own
           # row — the host's inbox drain has no other way to confirm a
           # follow-up sent mid-turn actually reached the CLI's own queue
           # rather than landing on a pipe nobody was reading from anymore.
           "--replay-user-messages",
           "--mcp-config", _write_mcp_config(run_dir, pane),
           "--permission-prompt-tool",
           f"mcp__{PERMISSION_SERVER}__{PERMISSION_TOOL}",
           # Naming a permission-prompt tool also un-gates AskUserQuestion and
           # ExitPlanMode, which the CLI otherwise disables in headless mode.
           # Both are now RENDERED — a question card (ANSWERABLE_TOOL) and a plan
           # card (PLAN_TOOL), each of which can say the thing the model is
           # waiting for — so nothing is disallowed and the flag is gone
           # altogether rather than passed with an empty value, which the CLI
           # would read as a tool whose name is the empty string.
           # Up to two pre-allowances, and they are the only ones — everything
           # else still raises a card. Both are the same thing in different
           # clothes: looking at the app the user is looking at.
           #
           #   the app_state tool — an MCP tool otherwise raises a card, so every
           #     app-state read would put a prompt on screen with no decision in
           #     it, for a read of the user's own screen by the agent they are
           #     already talking to. Omitted for a target with no pane (D239):
           #     the tool is not in that run's roster at all, and pre-allowing a
           #     name nothing can call is a rule about nothing.
           #   Read of the SHOTS dir — an annotation carries the path of a PNG
           #     crop of the element the user pointed at. The user attached it
           #     deliberately; carding it would make them approve their own
           #     screenshot. Scoped to that one directory, which holds nothing
           #     else and is not the user's project. Kept unconditionally: it is
           #     a directory rule, not a claim that this target can annotate.
           #
           # Narrow by construction: one fully-qualified tool name and one
           # directory, and the prompt bridge stays wired for everything else.
           #
           #   Bash(fused:*) — the third pre-allowance, and the only Bash one
           #     (D334). Present exactly when the server exported a `fused`
           #     wrapper (appenv.fused_cli_dir), never as a bare guess about
           #     PATH: the point is "push directly", and carding every push
           #     would put a prompt on screen for the one command this app
           #     itself runs on the user's behalf elsewhere (canvases.py). A
           #     prefix rule, so only a command that IS `fused ...` matches —
           #     compounds (`cd x && fused ...`) still card.
           "--allowed-tools",
           #   extra_read_dirs — the caller's own attachment dirs, same rule
           #     shape as SHOTS and for the same reason: the scheduler names
           #     its task-shots dir here, whose images the user attached
           #     deliberately in the New task form, and a headless run has
           #     nobody at the screen to answer a card. Now granted for the
           #     life of the SESSION, not the turn — --allowed-tools is fixed
           #     at spawn, so a later attachment from a NEW directory forces a
           #     respawn rather than silently arriving ungranted (_send).
           ",".join(([f"mcp__{PERMISSION_SERVER}__{APP_STATE_TOOL}"] if pane
                     else []) + [_read_rule(SHOTS)]
                    + [_read_rule(d) for d in (extra_read_dirs or [])]
                    + (["Bash(fused:*)"] if _fused_cli_dir() else []))]
    cmd += _plugin_argv(file)
    # BOTH targets get an --append-system-prompt here, and they get different
    # ones. A FILE target gets the scoping prompt. A DIRECTORY target that is an
    # APP FOLDER still does NOT get a scoping prompt — the session should be plain
    # Claude Code in that project, with the user's own system prompt, CLAUDE.md,
    # skills and tools, and cwd (_workdir) as the only scoping — but it does get a
    # narrow prompt of its own. An ordinary folder DOES get folder-scoping, which
    # is what the deleted plain chat mode gave it; _split_system_prompt picks.
    #
    # The app_state disclosure rides the two shapes that HAVE a pane, because an
    # un-announced tool does not get called (D235) — and only those two, because
    # since D239 an ordinary folder has no pane and is not offered the tool. What
    # the two must NOT share is the DESCRIPTION of that pane: an app folder frames
    # the user's own app, a file frames fused-render's preview of their file. Each
    # prompt says which, so the model never mistakes our UI for the user's code.
    # The fused CLI note rides every shape (file, app folder, ordinary
    # folder) because it is a fact about the machine, not the target — and
    # only when the wrapper actually exists (see _fused_cli_note).
    cmd += ["--append-system-prompt",
            (_split_system_prompt(file, pane) if os.path.isdir(file)
             else _system_prompt(file)) + _fused_cli_note()]
    if cli_mode:
        cmd += ["--permission-mode", cli_mode]
    if session_id:
        cmd += ["--resume", session_id]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    return cmd


def _spawn_env() -> dict:
    """`os.environ`, adjusted the same way for every `claude` spawn — the
    session host's own CLI Popen and (nothing else now, but kept as its own
    function so the two never drift again the way _start's inline copy could
    have).

    The session must not inherit an ambient FUSED_ENV from the server's own
    process: the `fused` wrapper (fusedcli._wrapper_text) only DEFAULTS
    FUSED_ENV when unset, so a value already present here — say the server
    itself was launched from a shell that exports FUSED_ENV for unrelated
    reasons — would look exactly like a deliberate `FUSED_ENV=x fused ...`
    from the model and skip the workbench default, silently diverging from
    canvases.py's own runs (`_cli_env` always forces FUSED_ENV=WORKBENCH_ENV,
    ambient or not). Popping it here is what makes "unset" in the wrapper
    mean what the model actually typed on that command line.

    File-history checkpoints are OFF by default in a non-interactive session,
    and every run here is non-interactive (`-p`). Without this the snapshots
    panel (SPEC §34) can only ever show versions written by a TERMINAL claude
    in that folder, and reports "no recorded versions" for every file this
    chat itself edited — the panel's own reason to exist. D394.

    An ENV VAR, not a setting: the CLI's `fileHistoryEnabled` takes a separate
    branch when `isInteractive()` is false, and that branch reads only these
    two variables — the `fileCheckpointingEnabled` config that governs the
    interactive case is not consulted, so `--settings` cannot reach it. Named
    for the SDK and absent from the public settings docs, so it may move; what
    to re-check if snapshots go quiet again is that branch.

    setdefault, because a user who exported it themselves means it: the CLI
    coerces the value properly (`1/true/yes/on`, everything else false), so a
    deliberate `=0` is an opt-out rather than a truthy string. Their
    CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING still wins inside the CLI either
    way — it is ANDed into the same branch — so this cannot override it."""
    env = os.environ.copy()
    env.pop("FUSED_ENV", None)
    env.setdefault("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "1")
    return env


def _inbox_dir(run_dir: str) -> str:
    return os.path.join(run_dir, "inbox")


def _write_inbox_row(run_dir: str, row: dict) -> None:
    """Queue one raw stream-json row into `run_dir/inbox/` for the session
    host to drain into the CLI's stdin verbatim (`session_host._drain_inbox`
    copies bytes, never parses them, so any row shape the CLI's
    `--input-format stream-json` accepts can go through here). Filenames
    sort in write order (a zero-padded nanosecond timestamp plus a few
    random hex digits — entropy against two entries landing in the same
    nanosecond, not a real id), which is the only order that matters: the
    host drains oldest-first, and this is the one place every inbox writer
    (`_write_inbox_entry`'s user turns, `_write_control_request`'s control
    requests) writes from."""
    inbox = _inbox_dir(run_dir)
    if not os.path.isdir(inbox):
        # `_private_dir`'s leaf create is exclusive (a run-id collision must
        # not silently adopt someone else's directory), which is right for
        # the FIRST write (inside `_start`) and wrong for every later one
        # (`_send`, `_cancel`) — the inbox is written to repeatedly for the
        # life of the session, not created once and never touched again.
        _private_dir(inbox)
    name = "%020d-%s.json" % (time.time_ns(), os.urandom(3).hex())
    final_path = os.path.join(inbox, name)
    # `session_host._drain_inbox` runs every `_DRAIN_INTERVAL_SECONDS` against
    # THIS SAME directory, listing whatever `*.json` names are present and
    # shipping their bytes straight to the CLI's stdin. `_private_open` at
    # `final_path` directly would let a drain tick land between the create
    # (truncated, 0 bytes) and the `json.dump` finishing — the entry the
    # drain sees is then empty or half-written, and it is gone (moved to
    # `done/`) before this function ever gets to finish writing it: the
    # user's message is lost, permanently, with nothing left to retry. A
    # `.tmp` name the drain's `endswith(".json")` filter never matches is
    # invisible to it until the whole write is done, so the final
    # `os.replace` (atomic on both platforms) is the only moment the entry
    # can be observed at all — always whole.
    tmp_path = os.path.join(inbox, name + ".tmp")
    with _private_open(tmp_path) as f:
        json.dump(row, f)
        f.write("\n")
    os.replace(tmp_path, final_path)


def _write_inbox_entry(run_dir: str, message: str) -> None:
    """Queue one user-turn line for the session host to drain into the CLI's
    stdin — the one place both `_start` (the turn's first message) and
    `_send` (every follow-up) write from."""
    _write_inbox_row(run_dir, {"type": "user", "message": {
        "role": "user",
        "content": [{"type": "text", "text": message}]}})


def _write_control_request(run_dir: str, subtype: str, **fields) -> str:
    """Queue a CLI control request — `interrupt`, `set_model`,
    `set_permission_mode` — verified live against 2.1.251 to ride the same
    stdin pipe a user turn does, just a different row shape: `{"type":
    "control_request", "request_id": ..., "request": {"subtype": ...}}`.
    Answered on stdout as a `control_response` row carrying the same
    `request_id` back (see `_await_control_response`), so this returns the id
    a caller that needs the answer has to watch for. Fire-and-forget callers
    (a model or permission-mode change — see `_send`) can ignore it; nothing
    here waits, because only `_cancel`'s `interrupt` has a caller that needs
    the reply before it can answer ITS OWN caller (the `still_queued` list)."""
    request_id = "%020d-%s" % (time.time_ns(), os.urandom(4).hex())
    request = dict(fields)
    request["subtype"] = subtype
    _write_inbox_row(run_dir, {"type": "control_request",
                               "request_id": request_id, "request": request})
    return request_id


def _await_control_response(run_dir: str, request_id: str,
                            timeout: float = 5.0,
                            start_offset: int = 0) -> dict | None:
    """Poll `out.jsonl` for the `control_response` row answering
    `request_id`, or None if it never arrives (the host never got to drain
    the request, the CLI died first, ...) or answered with anything other
    than `subtype: "success"`.

    A poll, not a blocking read, for the same reason `_poll` itself is one:
    `out.jsonl` is being written by a SEPARATE process (the CLI, through the
    session host), so there is no pipe here to block on directly — only a
    file to keep re-checking. The response is ordinarily seconds away (a
    drain tick plus however long the CLI takes to notice its own stdin), so
    a short sleep between checks costs nothing a caller would feel.

    Seeks to `start_offset` on every pass rather than reading from byte 0:
    this sits in the Stop button's synchronous path (`_cancel`'s
    `interrupt_first` branch), and a control response lands within the
    first second or two essentially always — up to 100 passes (5s / 50ms) of
    re-reading and re-`json.loads`ing the WHOLE transcript from scratch used
    to be gigabytes of I/O on the multi-hour, tens-of-MB sessions this
    feature exists to enable, all of it while the user waits on Stop. The
    caller captures `start_offset` (the file's size) BEFORE queuing the
    request — every row that can possibly answer it is written after that
    point, since the CLI cannot respond to a request it has not received
    yet — so nothing before it is ever worth reading, on any pass."""
    out_path = os.path.join(run_dir, "out.jsonl")
    deadline = time.time() + timeout
    while True:
        try:
            with open(out_path, "rb") as fh:
                fh.seek(start_offset)
                for raw_line in fh:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("type") != "control_response":
                        continue
                    resp = row.get("response") or {}
                    if resp.get("request_id") != request_id:
                        continue
                    if resp.get("subtype") != "success":
                        return None
                    inner = resp.get("response")
                    return inner if isinstance(inner, dict) else {}
        except OSError:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(0.05)


_SESSION_HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "session_host.py")


def _start(file: str, message: str, session_id: str, model: str,
           effort: str, permission_mode: str = "",
           message_via_stdin: bool = False,
           has_pane: bool | None = None,
           extra_read_dirs: list | None = None) -> dict:
    file = os.path.abspath(file)
    # A directory is a valid target too: this template's app-folder role opens
    # whole project folders (cwd/prompt handled by _workdir/_system_prompt).
    if not os.path.exists(file):
        return {"error": f"target not found: {file}"}

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    run_dir = os.path.join(RUNS, run_id)
    _private_dir(run_dir)
    _private_dir(_perm_dir(run_dir))
    # Whether this target has a page beside the chat at all: ONE value, read by
    # all three things that depend on it — the app-state channel's directory, the
    # tool's pre-allowance, and the prompt (`_split_system_prompt` takes it rather
    # than asking again; a second resolution is a second answer).
    #
    # THE PAGE'S ANSWER WINS. It decides at boot and cannot change (`paneURL` runs
    # once, `enterNoPane` is permanent), so it is the only thing that knows what is
    # actually on screen — and a roster that disagrees with the screen hands the
    # model a tool nothing can answer. Re-resolving from disk per turn is what made
    # a mid-session kind flip do that; see `_has_pane`. `None` means the caller has
    # no page (the apps API), and only then does disk decide.
    pane = _has_pane(file) if has_pane is None else has_pane
    # No pane, no channel, and no empty directory pretending there could be one:
    # the directory's absence is what removes the tool from the roster
    # (_write_mcp_config).
    if pane:
        _private_dir(_state_dir(run_dir))

    # An unknown mode falls back to the strictest of the three rather than
    # erroring: a mangled param must not quietly buy more auto-approval than
    # the user picked.
    mode = permission_mode if permission_mode in PERMISSION_MODES \
        else DEFAULT_PERMISSION_MODE
    cli_mode = PERMISSION_MODES[mode]

    # poll() records the session id with the run once claude reports it;
    # it needs the file + first message, so keep them with the run.
    # `mode` is the mode this process was SPAWNED with, and it is recorded
    # because nothing else can reconstruct it: the picker's URL param is what
    # the *next* turn will use, so reading that back mid-turn describes a
    # session that does not exist yet. See `_live_mode`.
    # `message` here is the USER-FACING one: the page prepends a live-app-state
    # block for the model, and everything fed from meta.json is a copy of what
    # the user said — the commit subject, and the message a
    # re-attaching page compares against the bubble on screen (which shows the
    # typed text only, so an unstripped copy silently stopped matching). Stripped
    # here, once, rather than at each of those three readers.
    with _private_open(os.path.join(run_dir, "meta.json")) as f:
        json.dump({"file": file, "message": _strip_app_state(message),
                   "resumed_from": session_id, "mode": mode}, f)

    # `err.log` exists from the very first instant, empty — `_poll`'s
    # abnormal-exit fallback reads its tail, and a host that dies before it
    # ever gets around to opening the file itself (a crash between spawn and
    # the CLI's own Popen) must still have something there to report from.
    _private_open(os.path.join(run_dir, "err.log")).close()

    # The turn's own first message is just the first inbox entry — `_send`
    # (a later follow-up) writes the exact same shape into the exact same
    # directory, and the host does not know or care which one started the
    # session versus which one rode in on the CLI's own queue mid-turn.
    _write_inbox_entry(run_dir, message)

    # The session host owns the CLI's stdin pipe for the life of the session
    # — see session_host.py's own module docstring for the fork-safety and
    # process-group reasoning this mirrors from claude_spawn.SESSION_HELPER.
    # `message_via_stdin` is accepted and ignored: every caller's message now
    # rides the inbox instead, so there is no longer an argv-message form for
    # it to choose between.
    del message_via_stdin
    req = {"agent": os.path.abspath(__file__), "run_dir": run_dir,
           "file": file, "cwd": _workdir(file), "pane": pane,
           "cli_mode": cli_mode, "session_id": session_id, "model": model,
           "effort": effort, "extra_read_dirs": list(extra_read_dirs or [])}
    proc = subprocess.Popen(
        [sys.executable, _SESSION_HOST],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=_spawn_env(),
        **_DETACH)
    try:
        proc.stdin.write(json.dumps(req).encode("utf-8"))
    finally:
        proc.stdin.close()
    # Transient: overwritten by the host with the CLI's OWN pid the moment it
    # spawns it (see session_host.py) — `_cancel`'s killpg needs that one, not
    # the host's, to reach the CLI's whole process group. Written here, to the
    # HOST's pid, only so `_alive` (and therefore `_poll`'s `done`) answers
    # True from the instant `_start` returns rather than during the gap while
    # a second interpreter is still starting up.
    #
    # O_EXCL, not `_private_open`'s truncate: the host (already running,
    # racing this write with no ordering guaranteed between the two
    # processes) does its own overwrite unconditionally, so if IT wins this
    # write must be a no-op rather than clobbering the CLI's real pid back to
    # the host's — `_cancel`'s killpg would then reach only the host's
    # process group and orphan the CLI it was supposed to kill. If this
    # write wins instead, the host's later unconditional overwrite still
    # lands on top of it, same as before.
    try:
        fd = os.open(os.path.join(run_dir, "pid"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass  # the host already wrote the CLI's own pid here — leave it
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
    so no turn's work is ever left outside history. Hard-scoped, mirroring
    app_git._repo_scope (D166 — keep the two in step): an app heading its OWN
    `.git` (unmigrated, or migration-skipped) commits there; otherwise an app
    directly under a `<workspace>/local` that IS a repo commits into that
    shared repo, PATHSPEC-SCOPED to its folder — a bare `add -A` is whole-tree
    since git 2.0 and would sweep a concurrent session's work on a sibling app
    into this commit. A target outside an app dir, or one resolving to no repo
    we own, commits nothing — this template also chats about files in
    arbitrary folders, and silently committing into a user's real repository
    is the one wrong move.

    Best-effort throughout: no git, index.lock contention, nothing staged —
    all mean "no commit", never a poll error. Identity rides per-invocation
    (`-c user.*`) so a machine with no git config still commits, and `git -C`
    replaces cwd= to keep Popen on the posix_spawn path (see apps.py)."""
    app_dir = _app_dir_for(file)
    if not app_dir:
        return
    if os.path.isdir(os.path.join(app_dir, ".git")):
        repo_dir, spec = app_dir, "."
    else:
        local = os.path.join(_workspace_dir(), "local")
        if not (os.path.dirname(app_dir) == local
                and os.path.isdir(os.path.join(local, ".git"))):
            return
        # :(literal) — folder names may carry pathspec magic (*, ?, […],
        # a leading :); mirrors app_git._pathspec.
        repo_dir, spec = local, ":(literal)" + os.path.basename(app_dir)
    subject = " ".join((message or "").split())
    subject = "Claude: " + (subject[:60] + "…" if len(subject) > 60 else subject) \
        if subject else "Claude turn"

    def git(*args):
        # ABSOLUTE argv[0]: close_fds=False alone does NOT reach posix_spawn —
        # CPython forks unless os.path.dirname(executable) is truthy, and a fork
        # with libproj resident dies with SIGSEGV before exec (rc -11, silently).
        import shutil
        return subprocess.run(
            [shutil.which("git") or "git", "-C", repo_dir, "-c", "user.name=Fused",
             "-c", "user.email=apps@fused.io", *args],
            capture_output=True, text=True, timeout=30, close_fds=False,
            encoding="utf-8", errors="replace")

    try:
        # The two sidecar patterns are a LEGACY defense: nothing writes those
        # files any more — the sidecar they belonged to is deleted outright
        # (D359), and it had already moved out of the app dir before that
        # (D83-reversal, D205) — but a repo from either era may still have one
        # sitting in its tree, and this sweep's add -A would commit it into app
        # history. `.fused/` is the LIVE one: the app's own state folder (D548)
        # is written continuously by the running app, so a turn's add -A would
        # otherwise sweep a whole cache into the commit. Mirror
        # app_git._ensure_excludes: append missing patterns to the repo-local
        # .git/info/exclude (never the user's .gitignore). Keep the pattern
        # list in step with app_git._GITIGNORE.
        exclude = os.path.join(repo_dir, ".git", "info", "exclude")
        if os.path.isdir(os.path.dirname(exclude)):
            try:
                with open(exclude, encoding="utf-8") as fh:
                    have = {ln.strip() for ln in fh}
            except OSError:
                have = set()
            missing = [p for p in ("*.html.json", ".claude-split.json",
                                   ".fused/")
                       if p not in have]
            if missing:
                with open(exclude, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(missing) + "\n")
        if git("add", "-A", "--", spec).returncode != 0:
            return
        if git("diff", "--cached", "--quiet", "--", spec).returncode == 0:
            return  # nothing to commit (turn changed no files under this app)
        git("commit", "-q", "-m", subject, "--", spec)
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


def _turn_state(run_dir: str) -> tuple:
    """(turn_open, tasks_pending), read off `out.jsonl` alone — no pid touched.

    `turn_open` lifts the exact D415 rule `_poll` already applies (see its
    `idle` there): a trailing `result` row means the turn that produced it is
    over, and ANYTHING after it — a hook firing, a fresh `system/init`, more
    text — reopens it, because that is the CLI waking itself for another turn
    on the same held-open stdin. A run with no rows yet (freshly spawned,
    `out.jsonl` not created or still empty) is open, same as `_poll` reads it:
    only `FileNotFoundError` degrades to an empty read, matching its own try.

    `tasks_pending` is the newest `background_tasks_changed` row's `tasks`
    array, non-empty. That is the CLI's own authoritative list — `_poll` also
    folds in the incremental `task_started`/`task_notification`/`task_updated`
    rows for its live activity line, but a caller here only needs the same
    yes/no a full `_poll` would eventually converge on, not the running
    description text.

    Both answers ignore the pid entirely, on purpose: a session host can hold
    a `claude` process alive long past its last turn, so "the process exists"
    and "a turn is open" stopped being the same fact the day the host shipped.
    A caller that still wants the raw process check has `_alive`.
    """
    try:
        with open(os.path.join(run_dir, "out.jsonl"), encoding="utf-8",
                  errors="replace") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []

    idle = False
    tasks = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # half-written last line; next read gets it
        if row.get("parent_tool_use_id"):
            continue  # a subagent's own row, not the main turn's (see _poll)
        t = row.get("type")
        idle = t == "result"
        if t == "system" and row.get("subtype") == "background_tasks_changed":
            arr = row.get("tasks")
            if isinstance(arr, list):
                tasks = {str(x.get("task_id")): True
                         for x in arr if isinstance(x, dict) and x.get("task_id")}
    return not idle, bool(tasks)


def _session_from_out(run_dir: str) -> str:
    """The session id the CLI announced in its first system row, or "".

    A fallback for the `session` file, which only exists once a poll has run
    (see _poll): the id is sitting in the head of out.jsonl the moment claude
    starts, and a lookup that needs it before any poll happened (see _live_run)
    can read it there. Head-bounded — the announcement is the first row the CLI
    writes, so anything past a handful of lines is a run whose head we cannot
    parse, not one still warming up."""
    try:
        with open(os.path.join(run_dir, "out.jsonl"), encoding="utf-8",
                  errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # half-written head; a later caller gets it
                sid = row.get("session_id")
                if sid:
                    return str(sid)
    except OSError:
        pass
    return ""


# How far back a live-run lookup bothers to look. Run dirs are named
# "<YYYYmmdd-HHMMSS>-<hex>", so a reverse sort is newest-first and a run that is
# still going is by construction among the newest few — a turn does not outlive
# 60 later ones. The cap is what keeps this O(1)-ish on a machine that has been
# chatting for weeks, since nothing prunes RUNS.
_LIVE_SCAN_LIMIT = 60


def _registry_running(workdir: str) -> set:
    """Session ids in `workdir` that a `claude` process on this machine holds
    RIGHT NOW — per Claude Code's own registry, `~/.claude/sessions/<pid>.json`,
    one file per running process (sessionId, cwd, status), deleted on exit.

    `_live_sessions` above knows only the runs THIS app spawned. A session
    resumed in a terminal, or started there, is invisible to it and read as
    idle on the Recent chats list while it is plainly generating. The registry
    is the same source the Tasks page reads (fused_render/tasks_watch.py), so
    the two lists agree on who is running.

    `busy`/`shell` is running; `idle`/`waiting` is not; a row with NO status is
    a headless `claude -p` that is alive, which is running for as long as the
    file exists. A dead pid (a crash left the file behind) counts for nothing.
    Best-effort throughout: an unreadable registry is an empty answer.

    A headless row whose pid is OURS — one of this app's own run dirs — is
    skipped rather than counted by file existence: since the session host
    shipped, that pid can sit alive well past its last turn, and `_turn_state`
    (off that run's own `out.jsonl`) already answers precisely whether a turn
    is actually open. Only a FOREIGN headless session (started at a terminal,
    invisible to RUNS) still needs this coarser heuristic."""
    want = os.path.abspath(workdir)
    own_pids = set()
    try:
        for name in os.listdir(RUNS):
            try:
                with open(os.path.join(RUNS, name, "pid"), encoding="utf-8") as fh:
                    own_pids.add(fh.read().strip())
            except OSError:
                continue
    except OSError:
        pass
    try:
        names = os.listdir(os.path.join(CLAUDE_DIR, "sessions"))
    except OSError:
        return set()
    out = set()
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(CLAUDE_DIR, "sessions", name), encoding="utf-8") as fh:
                row = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        sid = row.get("sessionId")
        cwd = row.get("cwd")
        if not isinstance(sid, str) or not isinstance(cwd, str):
            continue
        if os.path.abspath(cwd) != want:
            continue
        status = row.get("status")
        if isinstance(status, str) and status and status not in ("busy", "shell"):
            continue
        if not status and str(row.get("pid") or "") in own_pids:
            continue
        if not _pid_alive(str(row.get("pid") or "")):
            continue
        out.add(sid)
    return out


def _live_run(file: str, session_id: str = "", limit: int | None = _LIVE_SCAN_LIMIT) -> dict:
    """The id of a run for `file` that is STILL GOING, or "" if there is none.

    The page can only re-attach to a run whose id it has, and until this existed
    the id lived in exactly one place: the `run` param on a single history entry.
    Navigating away from that entry (Back, then re-opening the chat from the
    session list) lost it for good — the detached claude process kept writing
    into RUNS/<id>/out.jsonl with nothing watching, and the chat rendered its
    half-written transcript as if the turn had never started. Asking the server
    "is anything still running for this chat?" is the missing half: `resumeRun`
    was always able to adopt a run this frame did not start.

    Matched on the TARGET first, and on the session only when the caller names
    one. Two ids can identify the same chat — the session the run resumed
    (`resumed_from` in meta.json) and the session the CLI minted for it (written
    to the `session` file by the first poll that sees one, because
    `--fork-session` can hand back a NEW id) — so either matching is a match,
    and a run with no `session` file yet falls back to the id in out.jsonl's
    head (_session_from_out), because "no poll ever ran" is precisely the state
    a Back-mid-start leaves behind.

    `limit` is how many run dirs (newest first) the scan reads; `limit=None`
    reads all of them. The default cap is right for the ORIGINAL caller — a page
    re-attaching to its own run, where a run buried under 60 newer ones belongs
    to a frame long gone — and wrong for a caller that needs a RELIABLE answer
    about a folder rather than a cheap one. canvases.py's workbench lock is that
    caller: it asks "is a session editing this clone?" to decide whether to make
    the user's other editor read-only, and a live run that fell out of the
    window would read as "nobody is editing", silently leaving the lock off.
    Nothing prunes RUNS, so on a machine that has been chatting for weeks that
    miss is the normal case, not an exotic one. Unbounded costs one meta.json
    read per run dir, so the lock caller caches the answer across its poll
    interval rather than paying it on every tick.
    """
    file = os.path.abspath(file)
    try:
        names = sorted(os.listdir(RUNS), reverse=True)
    except OSError:
        return {"run_id": ""}
    if limit is not None:
        names = names[:limit]
    for name in names:
        run_dir = os.path.join(RUNS, name)
        try:
            with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if os.path.abspath(meta.get("file", "")) != file:
            continue
        if session_id:
            own = ""
            try:
                with open(os.path.join(run_dir, "session"), encoding="utf-8") as fh:
                    own = fh.read().strip()
            except OSError:
                pass
            if not own:
                # The `session` file is written by the FIRST POLL that sees the
                # id (see _poll) — so a run nobody ever polled has none. That is
                # not an exotic state: it is exactly what leaving mid-start
                # leaves behind (Akshil, 2026-08-19 — the reopened chat "does
                # not show me the streaming thing"): the page left before its
                # first poll, a NEW chat has no `resumed_from` either, and this
                # lookup answered "" for a run that was alive the whole time.
                # The CLI announces the id in its first system row, so read it
                # from the head of out.jsonl ourselves — a few lines, never the
                # transcript.
                own = _session_from_out(run_dir)
            if session_id not in (meta.get("resumed_from", ""), own):
                continue
        # Liveness LAST: the pid check alone used to be enough, because the
        # process exited the moment its one turn did. A session host now
        # holds `claude` open well past that, so "the process exists" is
        # necessary but no longer sufficient — the transcript also has to
        # say a turn is actually open (_turn_state) before this counts as
        # "still streaming".
        if _alive(run_dir):
            turn_open, _tasks_pending = _turn_state(run_dir)
            if turn_open:
                return {"run_id": name}
    return {"run_id": ""}


def _live_sessions(file: str, limit: int | None = _LIVE_SCAN_LIMIT) -> set:
    """Every session id under this target's FOLDER that has a run still going.

    `_live_run` above answers the same question for ONE session and matches on
    the exact target; a session list spans the whole folder — chats opened on
    the folder's other files are rows in it — so this one matches on the
    workdir instead, and answers for all of them in a single scan. Asking
    `_live_run` per row would re-read every run dir once per row.

    Both spellings of a run's session are collected, for the reason `_live_run`
    spells out: a run knows the session it RESUMED (`resumed_from`) and the one
    the CLI minted for it (the `session` file, or the head of out.jsonl before
    the first poll has written one), and either can be the id a row carries.

    Liveness is checked LAST and only for runs this folder owns — a pid check
    (_alive) plus a turn-open check (_turn_state), since a session host can
    hold the pid alive well past its last turn.
    """
    workdir = os.path.abspath(_workdir(file))
    try:
        names = sorted(os.listdir(RUNS), reverse=True)
    except OSError:
        return set()
    if limit is not None:
        names = names[:limit]
    live = set()
    for name in names:
        run_dir = os.path.join(RUNS, name)
        try:
            with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        target = meta.get("file", "")
        if not target or os.path.abspath(_workdir(target)) != workdir:
            continue
        # Alive AND turn-open — see _live_run's matching comment.
        if not _alive(run_dir):
            continue
        turn_open, _tasks_pending = _turn_state(run_dir)
        if not turn_open:
            continue
        own = ""
        try:
            with open(os.path.join(run_dir, "session"), encoding="utf-8") as fh:
                own = fh.read().strip()
        except OSError:
            pass
        if not own:
            own = _session_from_out(run_dir)
        for sid in (meta.get("resumed_from", ""), own):
            if sid:
                live.add(sid)
    return live


def _host_alive(run_dir: str) -> bool:
    """Whether `run_dir` has a LIVE session host, read off `host.json` alone.

    Not `_alive`: that reads `run_dir/pid`, which is the CLI's own pid once
    the host has overwritten it (see session_host.py), and the CLI can be
    freshly dead — a crash, the tail end of an idle-reap — with `host.json`
    still on disk for the instant before the host notices and removes it.
    `host.json` is written once, right after the host spawns the CLI, and
    removed in the host's own `finally` the moment it reaps (or dies) — so
    its PRESENCE plus an alive PID inside it is the host's own two-part
    answer to "is there still a queue to write a follow-up into"."""
    try:
        with open(os.path.join(run_dir, "host.json"), encoding="utf-8") as fh:
            host = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(host, dict):
        return False
    return _pid_alive(str(host.get("pid") or ""))


def _live_host(file: str, session_id: str = "",
               limit: int | None = _LIVE_SCAN_LIMIT) -> dict:
    """The id of a run for `file` whose session HOST is still up, or "".

    `_live_run` above answers "is a TURN open right now" — the question a page
    re-attaching to a stream needs. This answers a different one: "is there a
    session I can hand a follow-up to", which is true for the whole life of a
    chat, turn or no turn — a host sits idle between turns on purpose (that is
    the entire point of it), so gating this on `_turn_state`'s `turn_open`
    would make `action=send` miss every follow-up typed after a reply lands
    and before the idle-reap timer ends the session, which is most of them.

    Matching follows `_live_run`'s own rules exactly (same `meta.json` file
    match, same `resumed_from`/`session`-file/`_session_from_out` session
    match) — only the liveness check at the end differs.
    """
    file = os.path.abspath(file)
    try:
        names = sorted(os.listdir(RUNS), reverse=True)
    except OSError:
        return {"run_id": ""}
    if limit is not None:
        names = names[:limit]
    for name in names:
        run_dir = os.path.join(RUNS, name)
        try:
            with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if os.path.abspath(meta.get("file", "")) != file:
            continue
        if session_id:
            own = ""
            try:
                with open(os.path.join(run_dir, "session"), encoding="utf-8") as fh:
                    own = fh.read().strip()
            except OSError:
                pass
            if not own:
                own = _session_from_out(run_dir)
            if session_id not in (meta.get("resumed_from", ""), own):
                continue
        if _host_alive(run_dir):
            return {"run_id": name}
    return {"run_id": ""}


def _send(run_id: str, message: str, read_dirs: str = "", model: str = "",
         effort: str = "", permission_mode: str = "") -> dict:
    """Hand a follow-up to a LIVE host's own inbox, instead of starting a new
    process for it.

    This is what makes two messages typed while a turn runs land as ONE
    continuous reply instead of the page's own queue faking that with a
    second CLI process (that queue is retired — see the plan's Task 6): the
    message is written into `run_dir/inbox/` (`_write_inbox_entry`, the same
    function `_start` uses for a turn's opening message) and the session
    host already polling that directory picks it up on its next drain tick,
    whether the CLI is mid-turn (absorbed into the running turn) or idle
    (starts a fresh one on the same held-open stdin pipe).

    `read_dirs` is the SAME per-message attachment-directory string `_start`
    takes, but `--allowed-tools` is fixed at spawn time — a live session
    cannot be handed a new `Read` rule mid-session. So a message naming a
    directory the host was not already granted cannot be honored by sending:
    silently dropping the directory would card the very file the user just
    attached, so instead the whole session is ended (the same tree-kill
    `action=cancel` uses) and `{"respawn": True}` tells the caller to call
    `_start` fresh instead, which grants the new directory on the new spawn
    line. `effort` (also fixed at spawn — there is no CLI control request for
    it, unlike `model`) forces the exact same respawn when it differs from
    what the host was started with. Either way, a caller that gets
    `{"respawn": True}` must resend `message` itself through `_start` — this
    function never does that on its own, since only the caller knows the
    rest of `_start`'s arguments.

    `model` and `permission_mode`, by contrast, the CLI accepts CHANGED
    mid-session (`set_model`/`set_permission_mode` control requests, both
    verified live against 2.1.251) — a value that differs from what
    `host.json` recorded at spawn gets queued ahead of `message`, in the
    same inbox, so the CLI applies it before the turn `message` opens sees
    it. Neither is waited on the way `_cancel`'s `interrupt` is: nothing here
    needs the CLI's answer to answer ITS OWN caller, only the eventual
    `system/init`-style row the CLI writes on its own, which the page already
    reads for other reasons.
    """
    run_dir = os.path.join(RUNS, run_id)
    if _bad_id(run_id) or not os.path.isdir(run_dir):
        return {"error": "no such run"}
    try:
        with open(os.path.join(run_dir, "host.json"), encoding="utf-8") as fh:
            host = json.load(fh)
    except (OSError, ValueError):
        return {"error": "no live session"}
    if not isinstance(host, dict) or not _pid_alive(str(host.get("pid") or "")):
        return {"error": "no live session"}
    wanted = set(_attach_dirs(read_dirs))
    granted = set(host.get("read_dirs") or [])
    if not wanted <= granted or (effort and effort != host.get("effort", "")):
        # interrupt_first=False: unlike the stop button, this call means the
        # session can no longer serve this caller as it stands — leaving the
        # host up (an `interrupt` would) is not an option here, so it skips
        # straight past that to ending the whole process tree.
        _cancel(run_id, interrupt_first=False)
        return {"respawn": True}
    # `host.json`'s "model"/"mode" are compared against on every `_send`
    # call for the life of the session — left stale after a change actually
    # lands, EVERY later turn would see the same mismatch and re-queue the
    # same `set_model`/`set_permission_mode` control request the CLI already
    # applied, forever. Recorded here so only a REAL change ever queues one.
    host_changed = False
    if model and model != host.get("model", ""):
        _write_control_request(run_dir, "set_model", model=model)
        host["model"] = model
        host_changed = True
    if permission_mode:
        # `host.json`'s "mode" is the CLI-wire form (_start's `cli_mode`,
        # e.g. "acceptEdits"), the same shape `--permission-mode` takes and
        # `set_permission_mode` answers with — map the incoming page value
        # through the exact table `_start` uses so the comparison (and the
        # control request itself, if one is needed) speaks the CLI's spelling
        # and not the page's.
        wire_mode = permission_mode if permission_mode in PERMISSION_MODES \
            else DEFAULT_PERMISSION_MODE
        cli_mode = PERMISSION_MODES[wire_mode]
        if cli_mode != host.get("mode", ""):
            _write_control_request(run_dir, "set_permission_mode",
                                   mode=cli_mode)
            host["mode"] = cli_mode
            host_changed = True
    if host_changed:
        # Not racing the host itself: it writes host.json once, at spawn,
        # and only ever removes it (in its own `finally`) after that — it
        # never rewrites it mid-session, so this is the only writer for the
        # life of the file.
        with _private_open(os.path.join(run_dir, "host.json")) as f:
            json.dump(host, f)
    # Marks this message as sent-but-not-yet-echoed, so `_poll` (see its
    # `pending_echo` handling) will not believe a stale trailing `result` in
    # `out.jsonl` means the turn is done: at this exact instant the host may
    # not have drained the inbox yet, let alone gotten the CLI to write the
    # follow-up's own `--replay-user-messages` echo back — and until that
    # echo lands, `out.jsonl` still ends exactly where the PREVIOUS turn left
    # it. The byte offset (not just a flag) is what lets `_poll` tell THIS
    # message's echo apart from an older turn's own opening echo that may
    # still be sitting inside its current read window. `_poll` clears this
    # file itself, the moment it actually sees the echo (or gives up because
    # the process died).
    try:
        pending_offset = os.path.getsize(os.path.join(run_dir, "out.jsonl"))
    except OSError:
        pending_offset = 0
    with open(os.path.join(run_dir, "pending_echo"), "w", encoding="utf-8") as f:
        f.write(str(pending_offset))
    _write_inbox_entry(run_dir, message)
    return {"sent": True}


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


# The download page's troubleshooting anchor, used with a suffix per error
# (-login, -notfound, -limit) so the page opens the matching panel rather
# than always showing the login fix.
GUIDE_URL = "https://render.fused.io/#troubleshooting"


def _account_error(error: str) -> str:
    """Login and plan-limit failures rewritten to say what to do about them.

    The raw CLI text ("Invalid API key · Please run /login") names a fix that
    only works INSIDE an interactive claude session, while the user is looking
    at fused-render — so it reads as a bug in this app with no way out. Say
    where to run the fix and link the guide; the original rides along in
    parentheses because it is the part a bug report can be matched on.

    Substring matching over the error text is deliberate: these strings come
    from the CLI's own `result` row or stderr, never from model output, so a
    false positive would need the CLI itself to phrase an unrelated failure in
    login words."""
    if not error:
        return error
    low = error.lower()
    if ("invalid api key" in low or "/login" in low or "oauth token" in low
            or "not logged in" in low or "authentication_error" in low):
        return ("Claude Code isn't logged in. Open a terminal, run `claude`, "
                "type /login and finish the sign-in, then start a new chat "
                "here. Help: %s-login (%s)" % (GUIDE_URL, error))
    if "usage limit reached" in low or "session limit" in low:
        return ("Your Claude plan's usage limit was reached. Wait for it to "
                "reset, or upgrade the plan, then try again. Help: %s-limit (%s)"
                % (GUIDE_URL, error))
    return error


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


#: Display cap on one tool's output inside a segment. NOT a permission surface:
#: an approval card renders the tool's input untruncated and is the thing a
#: decision is made on (D161), so trimming here only ever costs the user a
#: re-read of something that already happened.
SEGMENT_OUTPUT_CAP = 4000
#: A base64 image bigger than this is dropped rather than shipped. The whole
#: segment list is re-sent on EVERY poll (400 ms), so one 8 MB screenshot would
#: be re-read, re-encoded and re-parsed a hundred-odd times a minute for the rest
#: of the turn — and the page has nowhere useful to put it either.
SEGMENT_IMAGE_CAP = 2 * 1024 * 1024


def _cap_output(text: str) -> str:
    """`text` trimmed to the display cap, saying how much it dropped.

    The tail matters more than the cap: silently truncated output reads as a
    tool that returned exactly that much, which is a lie a user cannot detect.
    """
    if len(text) <= SEGMENT_OUTPUT_CAP:
        return text
    return text[:SEGMENT_OUTPUT_CAP] + "… (+%d chars)" % (
        len(text) - SEGMENT_OUTPUT_CAP)


def _tool_result_payload(block: dict) -> tuple:
    """(output, images) for one `tool_result` block.

    `content` is a plain STRING for most tools and a list of typed blocks for
    the ones that return images — BOTH shapes are on the wire, so both are read
    here rather than at each call site. A block list that carries no text at all
    yields "" and not None: None is reserved for "no result has arrived yet",
    which is a different fact about the tool.

    The oversize note is appended AFTER the cap, deliberately: it is the only
    trace left of an image that was dropped, so it must not be the thing the
    cap eats.
    """
    content = block.get("content")
    if isinstance(content, str):
        return _cap_output(content), []
    parts, images, notes = [], [], []
    for sub in content if isinstance(content, list) else []:
        if not isinstance(sub, dict):
            continue
        if sub.get("type") == "text":
            if isinstance(sub.get("text"), str):
                parts.append(sub["text"])
        elif sub.get("type") == "image":
            source = sub.get("source") or {}
            data = source.get("data")
            # base64 only: a URL-sourced image is not something this page can
            # render from the payload, and inventing a fetch for it would put a
            # model-authored URL on the network.
            if source.get("type") != "base64" or not isinstance(data, str):
                continue
            if len(data) > SEGMENT_IMAGE_CAP:
                notes.append("[image dropped: %d bytes of base64 is over the "
                             "%d byte cap]" % (len(data), SEGMENT_IMAGE_CAP))
                continue
            images.append({"media_type": str(source.get("media_type")
                                             or "image/png"), "data": data})
    out = _cap_output("\n".join(parts))
    if notes:
        out = "\n".join(([out] if out else []) + notes)
    return out, images


def _is_text_delta(row) -> bool:
    """Whether `row` is one streamed chunk of assistant prose."""
    if not isinstance(row, dict) or row.get("type") != "stream_event":
        return False
    ev = row.get("event") or {}
    if ev.get("type") != "content_block_delta":
        return False
    return (ev.get("delta") or {}).get("type") == "text_delta"


def _thinking_delta_text(row) -> str:
    """The reasoning text of one streamed thinking chunk; "" for any other row
    AND for a chunk that carries no text.

    The empty case is the interesting one, and it is not an error: the wire key
    is `thinking` (as assumed), but WHETHER it holds anything is model-dependent
    on the shipping CLI. Measured over real `out.jsonl` files from live runs
    (CLI 2.1.226): `claude-haiku-4-5` streams the real trace, while
    `claude-sonnet-5` streams `{"type": "thinking_delta", "thinking": "",
    "estimated_tokens": 50}` and finalizes a `{"type": "thinking", "thinking":
    "", "signature": "…"}` block — the reasoning is REDACTED, and only its token
    estimate survives. Both surfaces agree within a run (all-empty or all-real,
    never one of each), so there is no recovering the text where it is redacted:
    the only honest rendering is no thinking block at all, which is what
    `_segments_from_rows` does with a segment this leaves empty. Reading the key
    through one function keeps the shape in one place for both the per-row growth
    and the "did any of them carry text?" gate."""
    if not isinstance(row, dict) or row.get("type") != "stream_event":
        return ""
    ev = row.get("event") or {}
    if ev.get("type") != "content_block_delta":
        return ""
    delta = ev.get("delta") or {}
    if delta.get("type") != "thinking_delta":
        return ""
    return str(delta.get("thinking") or "")


def _segments_from_rows(rows: list) -> list:
    """The ordered transcript of a reply: text, thinking and tool segments.

    ONE reader with TWO callers — `_poll` over the live `out.jsonl` and
    `_history` over the persisted session transcript — because they render the
    same conversation, and a second implementation would differ only by
    drifting. The row shapes are near-identical (the API message nests under
    `message` in both); what differs is that only `out.jsonl` carries
    `stream_event` rows. So text arrives as deltas there and as finalized blocks
    in the transcript, and both are read — but never both at once: an
    `assistant` row repeats verbatim the text its deltas already delivered, so
    the finalized blocks are read ONLY when this row set carries no text delta
    at all (the persisted transcript, or a CLI too old for
    `--include-partial-messages`). Decided over the whole list rather than
    per-message on purpose: it makes the choice independent of where the
    `assistant` row sits relative to its own `message_stop`, which is the
    ordering a duplicate would otherwise hinge on.

    THINKING follows the same deltas-or-finalized-blocks rule as text, on its
    own gate: the finalized `thinking` block is read only when no
    `thinking_delta` carried any text. That covers two real cases the text gate
    does not — the persisted transcript (no `stream_event` rows at all, so a
    restored turn's reasoning has nowhere else to come from) and a run whose
    prose streamed while its reasoning did not. A thinking segment that ends up
    with no text is DROPPED rather than returned empty: some models redact the
    trace entirely (see `_thinking_delta_text`), and a "Thought for a moment"
    disclosure that unfolds to nothing is worse than no disclosure.

    Tool calls are read ONLY from finalized `assistant` rows. The streamed
    `content_block_start` for the same call arrives with `input: {}` and its
    arguments only as `input_json_delta` fragments (same reason as
    `_skill_calls`), so the finalized row is both complete and what keeps one
    call from being reported twice.

    Ordering is file order, and a `tool_result` is joined to its `tool_use` by
    `tool_use_id` rather than by position — parallel tools answer out of call
    order routinely, and a result can even be flushed before the message that
    asked for it, hence `orphans`.

    Two segments of the same kind in a row MERGE (the tail grows in place)
    rather than accumulating one segment per delta: the page renders a text
    segment as markdown, and markdown split across arbitrary delta boundaries
    is not the same document.

    **For anything rendering this: segments are the authoritative transcript.**
    Render them whenever the list is non-empty. `text` on the poll payload (and
    on a history turn) is the flat LEGACY field: it is byte-identical to what it
    was before segments existed, and the text segments join back into it exactly
    — but only on a run that carried stream deltas. Where there are none (a CLI
    without `--include-partial-messages`) `text` falls back to the `result` row,
    which is the LAST assistant message only, so it can be a strict subset of
    what the segments say. Rendering `text` when segments exist therefore shows
    less than the turn contained; the reverse never happens. `text` stays the
    right thing to show for the error paths, which produce no segments at all.
    """
    segments = []
    by_tool_id = {}     # tool_use id -> its segment, for the result to find
    stripped = set()    # tool_use ids of calls deliberately not shown
    orphans = {}        # results that arrived before their tool_use row
    streamed = any(_is_text_delta(row) for row in rows)
    # The same "deltas or finalized blocks, never both" choice as `streamed`,
    # decided separately because it is a different question: a run can stream its
    # prose and still carry no usable thinking delta (redacted, or a transcript
    # with no `stream_event` rows at all — which is EVERY row set `_history`
    # reads, and is why a restored turn never showed a thinking block before).
    thinking_streamed = any(_thinking_delta_text(row) for row in rows)
    any_text = False    # mirrors _poll's `bool(text_parts)`
    pending_sep = False
    plumbing = "mcp__%s__%s" % (PERMISSION_SERVER, APP_STATE_TOOL)

    def tail(kind):
        return segments[-1] if segments and segments[-1]["kind"] == kind else None

    def grow(kind, chunk, separator=""):
        """Append `chunk` to the trailing `kind` segment, opening one if the
        tail is something else.

        Parts in a LIST, joined once at the end — never `+=` on a str. This
        whole function re-runs from scratch on every 400 ms poll, so growing a
        string in place re-copies the accumulated segment per delta: quadratic
        in the deltas of one turn, and measurably so (~46 ms a tick at 20k
        deltas, ~0.7 s at 80k, against a flat few ms for parts+join). `_poll`'s
        own `text_parts` exists for exactly this reason, and the finalize step
        below is what keeps the list an implementation detail — the returned
        segment carries a plain `text` string.

        `separator` goes INSIDE the segment it precedes — even when that segment
        is brand new — so that joining the text segments reproduces `_poll`'s
        `text` byte for byte on a streamed run. The two accumulations are two
        copies of one rule, so a test asserts they agree rather than a comment
        saying they should (D146).
        """
        seg = tail(kind)
        if seg is None:
            seg = {"kind": kind, "text": []}
            segments.append(seg)
        if separator:
            seg["text"].append(separator)
        seg["text"].append(chunk)

    def settle(seg, payload):
        seg["status"], seg["output"], seg["images"] = payload

    for row in rows:
        if not isinstance(row, dict):
            continue
        # Synthetic rows and subagent rows are not this conversation — the same
        # guard `_history` has always applied to turns.
        if row.get("isMeta") or row.get("isSidechain"):
            continue
        t = row.get("type")
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if t == "stream_event":
            ev = row.get("event") or {}
            et = ev.get("type")
            if et == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta":
                    grow("text", str(delta.get("text", "")),
                         "\n\n" if pending_sep else "")
                    any_text, pending_sep = True, False
                elif delta.get("type") == "thinking_delta":
                    # Only a chunk that actually carries text opens a segment:
                    # a redacted trace is all-empty chunks (see
                    # `_thinking_delta_text`), and growing on those built a
                    # thinking segment whose body was "" — which the page
                    # rendered as a "Thought for a moment" disclosure that
                    # unfolded to nothing at all.
                    chunk = _thinking_delta_text(row)
                    if chunk:
                        grow("thinking", chunk)
            elif et == "message_stop":
                # A tool-using turn is several assistant messages; without a
                # break their texts concatenate mid-word ("orange.After").
                pending_sep = any_text
        elif t == "assistant" and isinstance(content, list):
            # Thinking BEFORE text, because that is the order a real message
            # carries them (thinking, then text, then tool_use) and segments are
            # an ordered record. Read only when no thinking delta carried text:
            # the finalized block repeats verbatim what the deltas delivered, so
            # reading both prints the reasoning twice — and where there were no
            # deltas at all (the persisted transcript) this is its only source.
            if not thinking_streamed:
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "thinking":
                        continue
                    chunk = str(block.get("thinking") or "")
                    if chunk.strip():
                        grow("thinking", chunk)
            # Text blocks next, joined the way `_history` joins them, so a
            # restored turn's `text` and its segments say the same thing. Safe
            # against block order because a real message is text-then-tools.
            if not streamed:
                whole = "\n".join(b.get("text", "") for b in content
                                  if isinstance(b, dict) and b.get("type") == "text")
                if whole.strip():
                    grow("text", whole,
                         "\n\n" if any_text and tail("text") is not None else "")
                    any_text = True
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "")
                tool_id = str(block.get("id") or "")
                if name == plumbing:
                    # This template's own bridge asking the page what it is
                    # showing. Nobody requested it and its answer is our JSON,
                    # so it is not part of the conversation. ONLY this exact
                    # name: every other MCP tool is a real call.
                    if tool_id:
                        stripped.add(tool_id)
                    continue
                if tool_id and tool_id in by_tool_id:
                    continue  # the same finalized message written twice
                tool_input = block.get("input")
                seg = {"kind": "tool", "id": tool_id, "name": name,
                       "input": tool_input if isinstance(tool_input, dict) else {},
                       "status": "running", "output": None, "images": []}
                segments.append(seg)
                if tool_id:
                    by_tool_id[tool_id] = seg
                    if tool_id in orphans:
                        settle(seg, orphans.pop(tool_id))
        elif t == "system" and row.get("subtype") == "task_notification":
            # The harness waking the run because a background shell it started
            # has finished or been stopped (D415). It is not the model speaking
            # and it is not a tool call, so it is neither text nor a chip — it
            # is the REASON the turn that follows exists, and without it a reply
            # appears out of nowhere under a message the user never sent. One
            # line, from the CLI's own `summary`; the page draws it as a system
            # chip (buildNoticeView).
            #
            # This is the LIVE shape, the row `out.jsonl` carries. The persisted
            # transcript records the same event as a synthetic `user` row of
            # `<task-notification>` XML — the branch below — and both land here
            # so a restored conversation and a streaming one show the same chip.
            note = str(row.get("summary") or "").strip()
            if note:
                segments.append({"kind": "notice", "text": [note],
                                 "status": str(row.get("status") or "")})
        elif t == "user" and isinstance(content, str):
            note = _task_notification(content)
            if note:
                segments.append({"kind": "notice", "text": [note["summary"]],
                                 "status": note["status"]})
        elif t == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id") or "")
                if tool_id in stripped:
                    continue
                output, images = _tool_result_payload(block)
                payload = ("error" if block.get("is_error") else "ok",
                           output, images)
                seg = by_tool_id.get(tool_id)
                if seg is not None:
                    settle(seg, payload)
                elif tool_id:
                    orphans[tool_id] = payload
    # Finalize: the parts lists collapse to the plain `text` string the schema
    # promises. Tool segments have no `text` at all and are left alone.
    out = []
    for seg in segments:
        if seg["kind"] != "tool":
            seg["text"] = "".join(seg["text"])
        # A thinking segment with nothing in it is not a disclosure, it is an
        # empty box. The growth guards above already refuse an empty chunk, so
        # this only catches a trace that was pure whitespace — but it is the
        # invariant the page depends on ("a thinking segment HAS a body"), so it
        # is enforced here rather than assumed. Text segments are NOT filtered:
        # an empty one is how the page knows the reply's tail is still coming.
        if seg["kind"] == "thinking" and not seg["text"].strip():
            continue
        out.append(seg)
    return out


def _tool_detail(name, inp) -> str:
    """The one short thing worth saying about a tool call on the status line.

    Bash carries its own `description` (the model writes one per call); the
    file tools name a file; Task/Agent describe the job. Anything else is just
    its name. Kept to one line and ~80 chars — this rides inside "(47s · …)".
    """
    if not isinstance(inp, dict):
        return ""
    name = str(name or "")
    if name == "Bash":
        d = inp.get("description") or ""
        if not d:
            d = str(inp.get("command") or "").strip().splitlines()[:1]
            d = d[0] if d else ""
    elif name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        d = os.path.basename(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    elif name in ("Task", "Agent"):
        d = inp.get("description") or inp.get("subagent_type") or ""
    elif name in ("Grep", "Glob"):
        d = inp.get("pattern") or ""
    elif name == "Skill":
        d = inp.get("skill") or ""
    elif name in ("WebFetch", "WebSearch"):
        d = inp.get("url") or inp.get("query") or ""
    else:
        d = ""
    d = " ".join(str(d).split())
    return d if len(d) <= 80 else d[:77] + "…"


def _read_poll_cursor(run_dir: str, size: int) -> int:
    """The byte offset in `out.jsonl` that `_poll` last proved safe to resume
    from — see the advance logic at the bottom of `_read_current_turn` for
    what "safe" means. Anything that doesn't resolve to a real offset inside
    the CURRENT file — missing (first poll ever, or an old run predating this
    file), garbage (hand-edited, truncated write), or past `size` (the file
    got shorter somehow, or this cursor is stale from before something reset
    the run dir) — falls back to 0, a full parse from the start. A bad cursor
    must never make `_poll` skip real rows; the worst a wrong fallback costs
    is one slow poll, so the check errs toward re-reading."""
    try:
        with open(os.path.join(run_dir, "cursor"), encoding="utf-8") as fh:
            value = int(fh.read().strip())
    except (OSError, ValueError):
        return 0
    if value < 0 or value > size:
        return 0
    return value


def _write_poll_cursor(run_dir: str, value: int) -> None:
    try:
        with _private_open(os.path.join(run_dir, "cursor")) as fh:
            fh.write(str(value))
    except OSError:
        pass  # costs the next poll a full re-parse; never worth raising over


def _read_bg_tasks(run_dir: str) -> dict:
    """The `task_id -> description` map `_poll` last computed, persisted
    across cursor advances — see `_write_bg_tasks`."""
    try:
        with open(os.path.join(run_dir, "bg_tasks.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_bg_tasks(run_dir: str, bg_tasks: dict) -> None:
    """`background_tasks_changed` is the CLI's own AUTHORITATIVE full list,
    sent only when it changes — a page reading `activity.tasks`/
    `tasks_pending` needs the last one it saw for as long as it stays true,
    not just for the one poll whose narrow, cursor-bounded window (see
    `_read_current_turn`) happened to contain the row. Once a genuine new
    turn advances the cursor past that row, a poll scanning only what
    changed since the last one has no way to see it again — so `_poll`
    persists its own answer here every call and seeds the next call's scan
    from it, the same way `cursor` itself survives between calls."""
    try:
        with _private_open(os.path.join(run_dir, "bg_tasks.json")) as fh:
            json.dump(bg_tasks, fh)
    except OSError:
        pass  # costs the next poll a stale (not wrong-forever) answer


def _starts_new_turn(row: dict) -> bool:
    """Whether `row` is the CLI's own echo (`--replay-user-messages`) of a
    message `_write_inbox_entry` put on the wire — the one row shape that
    provably begins a fresh, user-authored turn.

    A D415 wake looks like a `user` row too on the PERSISTED transcript (the
    synthetic `<task-notification>` XML), but its `content` is a bare
    string; a real echoed turn's `content` is always the list
    `_write_inbox_entry` builds. The live `system/task_notification` shape
    fails the `type == "user"` check outright. Both wake shapes correctly
    read as "not a new turn" here.

    A `tool_result` row is `type: "user"` with a list `content` too — one
    lands after every tool call, far more often than a genuine new turn — so
    the list's own block types have to be checked: `_write_inbox_entry`
    always writes `text` blocks, never `tool_result` ones."""
    if row.get("type") != "user":
        return False
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(item, dict) and item.get("type") == "text"
               for item in content)


def _read_current_turn(run_dir: str) -> tuple:
    """(rows, cursor) — the parsed rows of `out.jsonl` from the last proven-safe
    offset onward, and the offset `_poll` should persist for next time.

    A run used to be one turn, so re-parsing the whole file every ~400ms was
    cheap. Tasks 2-4 made a run a whole SESSION: one `claude` process, many
    turns, one `out.jsonl` that only grows. Re-scanning it in full on every
    poll turned an O(1)-per-tick cost into an O(session length) one: for a
    page (or `claude_spawn.record_session_when_ready`) that has been watching
    a run continuously, ticking every ~400ms, this bounds each read to just
    what changed since the LAST tick — which, steady-state, is one turn's
    worth of rows or less. A page attaching to an already-multi-turn run for
    the first time still pays for a full scan, same as before this existed —
    a one-time cost, not a per-tick one.

    That steady-state narrowing also happens to fix a latent correctness bug,
    not just a perf one: `_poll`'s per-turn state (`text_parts`, `saw_result`,
    …) never reset at a `result` boundary, so a continuously-polled multi-turn
    session used to concatenate every turn's streamed text into one blob
    forever, and could mask a turn that crashed mid-stream because an EARLIER
    turn's `result` row already made `saw_result` true for the whole file.
    Once the cursor has advanced past a turn, later polls simply never see
    its rows again, so that accumulation stops happening — except on the one
    cold-attach read above, whose rows are a real, once-only reflection of
    everything that already happened, not an ongoing leak.

    The cursor only ever advances to the start of the newest row that is
    provably a fresh, user-authored turn — `_starts_new_turn`'s check
    (`_start`/`_send` echoed back by `--replay-user-messages`) AND a `result`
    row closing the previous turn somewhere between `cursor` and it. The
    second half matters because `_send` exists precisely to fold a follow-up
    into a turn still in flight: the CLI echoes that follow-up back in the
    exact same shape, but with no `result` in between, since the turn it is
    joining has not closed. Treating that echo as a fresh turn boundary would
    skip past text the current turn already streamed, so the NEXT poll's
    `rows`/`segments` would shrink instead of only ever growing — the bug this
    half of the rule exists to prevent, mirroring the one below it. A `result`
    alone, even one with more bytes after it, is NOT enough either: a D415
    wake (`system/task_notification`, or its persisted `<task-notification>`
    form) is exactly a `result` followed by more bytes that belong to the
    SAME displayed turn, not a new one — `_segments_from_rows` renders that
    combination as one notice-joined reply, and a page rendering the whole
    `segments` list fresh every poll (see `renderSegments` in template.html)
    needs the ORIGINAL text still in that list on every later poll, not just
    the wake's own continuation. Advancing past a `result` a wake continues
    would make the next poll return only the wake, silently erasing the reply
    already on screen. Everything before the newest genuine turn's own start
    IS provably dead weight for every future poll (this one included, once
    written back), because a real new turn only ever begins once the one
    before it — wakes, absorbed follow-ups and all — is completely finished.

    Reads via binary seek-then-decode, not a text-mode file's `.seek()`, on
    the same reasoning `_scan_transcript` already leans on: the offset being
    seeked to is one *this process* computed and persisted as a byte count,
    and only a binary seek is guaranteed to land exactly there — a text-mode
    file object's `.seek()` argument is an opaque cookie on some platforms,
    not necessarily a raw byte offset, once `errors="replace"` decoding is in
    the picture."""
    path = os.path.join(run_dir, "out.jsonl")
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0

    cursor = _read_poll_cursor(run_dir, size)
    try:
        with open(path, "rb") as fh:
            fh.seek(cursor)
            blob = fh.read()
    except OSError:
        return [], cursor

    # `blob.split(b"\n")` on "a\nb\n" gives [b"a", b"b", b""] — every element
    # but the last was followed by a real newline byte; the last is either
    # that trailing empty artifact (blob ends exactly on a newline) or a
    # half-written final line (the writer's `\n` hasn't landed yet). Only the
    # former group can ever move the cursor — the latter needs to be re-read
    # from scratch next poll however it turns out, no matter what it parses to
    # now.
    parts = blob.split(b"\n")
    complete, tail = parts[:-1], parts[-1]

    # This call still returns every row from `cursor` to EOF, wake
    # continuations included — a `result` followed by a `<task-notification>`
    # wake is not a turn boundary from the page's point of view (see the
    # `done`/`idle` comment on `_poll` itself: nothing genuinely ends until
    # the process goes quiet), and `_segments_from_rows` is what turns that
    # combined stream into a "notice" divider, not a second, isolated turn.
    # `advance_to` only moves the offset the NEXT call starts from; it never
    # trims what THIS call hands back. That keeps a page that has been
    # watching a run continuously exactly as fast as one that just attached
    # to it (both see, and pay for, only what has happened since either of
    # them last looked) while a page that attaches to an already-multi-turn
    # run for the very first time still gets its full history in that one
    # read, same as the pre-cursor behavior — a one-time cost, not a
    # per-poll one.
    rows = []
    line_start = cursor
    advance_to = None
    # A `_starts_new_turn` row only proves a genuine new turn when a `result`
    # closed the one before it SINCE this scan started (i.e. since `cursor`,
    # or since the last row this same scan already advanced past). `_send`
    # exists precisely so a follow-up can be absorbed into a turn still in
    # flight — the CLI echoes that follow-up back in exactly the same shape
    # a fresh turn's opening message has, but with no `result` in between.
    # Advancing on it anyway would jump the cursor past text the current
    # turn already streamed, so the NEXT poll's `rows` (and therefore
    # `segments`) would shrink — the docstring's "a real new turn only
    # begins once the one before it is completely finished" premise holds
    # for `_start`-opened turns, not for one folded in mid-turn by `_send`.
    seen_result = False
    for raw_line in complete:
        pos = line_start + len(raw_line) + 1
        try:
            row = json.loads(raw_line.decode("utf-8", "replace"))
        except ValueError:
            line_start = pos
            continue  # a stray blank/garbage line; not this poll's problem
        rows.append(row)
        if row.get("type") == "result":
            seen_result = True
        elif seen_result and _starts_new_turn(row):
            advance_to = line_start  # the newest genuine turn's own start
            seen_result = False      # this turn needs its own result too
        line_start = pos

    if tail:
        try:
            rows.append(json.loads(tail.decode("utf-8", "replace")))
        except ValueError:
            pass  # half-written last line; next poll gets it, same as before

    if advance_to is not None:
        _write_poll_cursor(run_dir, advance_to)
    return rows, cursor


def _poll(run_id: str, file: str = "") -> dict:
    run_dir = os.path.join(RUNS, run_id)
    if _bad_id(run_id) or not os.path.isdir(run_dir):
        return {"text": "", "done": True, "session_id": "", "error": "unknown run_id",
                "permissions": [], "app_state": [], "skills": [], "retry": None,
                "retry_total": 0, "retry_status": 0, "segments": []}

    # A page may only attach to a run about ITS OWN target. Run ids are global
    # (RUNS is one flat dir), and the `run` url param survives some hops the
    # target does not — the listing pane retargeting `_file` on a selection
    # change is the reported one — so an id alone must not be enough: without
    # this check a stale param re-attached a live run's whole conversation
    # under whichever folder the pane was pointed at next. Refused ONLY on a
    # provable mismatch: `file` is optional (claude_spawn's bookkeeping loop
    # polls with no page and no target), and a meta.json without `file` — or
    # unreadable entirely — proves nothing and keeps the historical behavior.
    # The wire shape matches "unknown run_id" so the page's existing stale-param
    # recovery (clear the param, no error banner) covers this case too.
    if file:
        try:
            with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
                run_file = json.load(fh).get("file", "")
        except (OSError, ValueError):
            run_file = ""
        if run_file and os.path.abspath(run_file) != os.path.abspath(file):
            return {"text": "", "done": True, "session_id": "",
                    "error": "run is for another target",
                    "permissions": [], "app_state": [], "skills": [], "retry": None,
                    "retry_total": 0, "retry_status": 0, "segments": []}

    text_parts = []
    result_text = None
    new_session = ""
    # `done` IS PER TURN, AND A `result` ONLY ENDS ONE WHILE NOTHING FOLLOWS IT
    # (D415). One claude process can run several turns: a turn that started a
    # background shell is woken by the harness when the command finishes — a
    # `<task-notification>` prompt this page never sent — and everything the
    # agent then says is written to this same `out.jsonl`, after the `result`
    # that closed the first turn. `done` used to latch True on that first
    # `result` and stay there for the life of the run, so the woken turn was
    # reported as a finished one: no working line, no streaming, an answer that
    # only appeared on the next reload (Akshil, 2026-08-21 — the chat "showed
    # nothing" until a few "continue"s later).
    #
    # So the answer is "the last thing in this file is a finished turn", which
    # goes back to False the moment the wake writes its first row, and the page's
    # standing live-run watch attaches to it like any other run it did not start.
    # The alternative — done only at process exit — was rejected for what it does
    # to the COMPOSER: the process outlives the turn for as long as the
    # background command runs (an hour is not unusual), and the chat would sit
    # busy, queueing everything the user typed, over a run that is not saying
    # anything.
    #
    # Liveness is sampled BEFORE the read, and that order is the point: a process
    # that dies between these two lines is read as alive for one more poll
    # (400ms, and the tail is on disk by then), where the reverse order could
    # call a run done over a file whose last rows had not been flushed yet.
    alive = _alive(run_dir)
    saw_result = False   # at least one turn of this run has finished
    idle = False         # ...and nothing has been said since
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
    # WHAT THE RUN IS DOING RIGHT NOW, beyond the verb. Every one of these is a
    # thing the CLI already writes to out.jsonl and the page used to ignore, so
    # a long quiet stretch — a minute of extended thinking, a Bash `sleep 30`,
    # a 40 KB Write streaming its input, a slow Stop hook — sat under a frozen
    # "Thinking… (47s)" that was indistinguishable from a hang (Akshil,
    # 2026-08-28). See `_activity` for the wire shape.
    # Tools in flight, BY ID and in start order. One assistant message can
    # carry several tool_use blocks (parallel calls); their results come back
    # as separate `user` rows, so "a result arrived" is not "the tool phase is
    # over" — only the LAST open call's result is (Bugbot, PR #908). The line
    # shows the most recently started call that has not returned.
    tools_open = {}        # tool_use id -> {"id", "name", "detail"}
    tool_input_bytes = 0   # input_json_delta bytes streamed for the newest block
    tool_inputs = {}       # tool_use id -> finalized input (from `assistant` rows)
    thinking_tokens = 0    # CLI's running estimate for the message in flight
    hooks_open = {}        # hook_id -> hook_name, started and not yet responded
    # Seeded from what the last poll persisted (see `_write_bg_tasks`), not
    # {} — `background_tasks_changed` is only sent on CHANGE, and once the
    # cursor advances past the row that announced a still-running task, this
    # poll's own window may say nothing about it at all.
    bg_tasks = _read_bg_tasks(run_dir)
    agent_rows = 0         # rows a subagent wrote (parent_tool_use_id set)
    after_tool = False     # a tool_result landed and nothing has streamed since
    # Every row this poll managed to parse, handed to `_segments_from_rows` once
    # the loop is done. Collected rather than parsed a second time: `rows`
    # (below) already did the one `json.loads` pass this poll needs, over just
    # the bytes since the last closed turn — see `_read_current_turn` — so this
    # list only holds a second reference to objects that pass already built.
    parsed = []

    rows, _cursor = _read_current_turn(run_dir)

    for row in rows:
        parsed.append(row)
        t = row.get("type")
        # Anything at all after a `result` is the run waking up for another turn
        # (the harness's hooks fire first, then `init`, then the reply), so the
        # quiet-verb window closes on the first row of any kind — see `idle`.
        if t != "result":
            idle = False
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
        # A subagent's rows come through the same stream tagged with the id of
        # the Task/Agent call that spawned them (SDK contract; not yet seen in a
        # local run, so counted rather than rendered). They must not move the
        # main line's phase — the parent is still "running Agent".
        if row.get("parent_tool_use_id"):
            agent_rows += 1
            continue
        if t in ("stream_event", "assistant"):
            after_tool = False
        if t == "system":
            new_session = row.get("session_id", new_session)
            sub = row.get("subtype")
            if sub == "api_retry":
                info = _retry_info(row)
                if info is not None:
                    retry = info
                    retry_total += 1
                    retry_status = info["status"]
            elif sub == "status":
                # `{"status": "requesting"}` is the CLI saying the request is
                # out and no token is back yet — the one gap the stream itself
                # cannot describe, and the most common "what is it doing".
                if row.get("status") == "requesting":
                    phase = "requesting"
                    after_tool = False
            elif sub == "thinking_tokens":
                est = row.get("estimated_tokens")
                if isinstance(est, (int, float)):
                    thinking_tokens = max(thinking_tokens, int(est))
            elif sub == "hook_started" and row.get("hook_id"):
                hooks_open[row["hook_id"]] = str(row.get("hook_name") or "hook")
            elif sub == "hook_response":
                hooks_open.pop(row.get("hook_id"), None)
            elif sub == "task_started" and row.get("task_id"):
                bg_tasks[row["task_id"]] = str(row.get("description") or "")
            elif sub == "task_notification":
                bg_tasks.pop(row.get("task_id"), None)
            elif sub == "task_updated":
                if (row.get("patch") or {}).get("status") in (
                        "completed", "killed", "failed", "stopped"):
                    bg_tasks.pop(row.get("task_id"), None)
            elif sub == "background_tasks_changed":
                # Authoritative list when the CLI sends one.
                tasks = row.get("tasks")
                if isinstance(tasks, list):
                    bg_tasks = {
                        str(x.get("task_id")): str(x.get("description") or "")
                        for x in tasks if isinstance(x, dict) and x.get("task_id")}
        elif t == "assistant":
            skills += _skill_calls(row)
            for blk in (row.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use" \
                        and blk.get("id"):
                    tool_inputs[blk["id"]] = blk.get("input") or {}
                    if blk["id"] in tools_open:
                        tools_open[blk["id"]]["detail"] = _tool_detail(
                            blk.get("name"), tool_inputs[blk["id"]])
        elif t == "user":
            # The tool's result went back in. From here until `status:
            # requesting` (or the next delta) the run is packing the result
            # into the next request — brief, but "Working" with no tool open
            # was the lie this used to tell.
            content = (row.get("message") or {}).get("content")
            results = [b for b in (content if isinstance(content, list) else [])
                       if isinstance(b, dict) and b.get("type") == "tool_result"]
            for b in results:
                # Close the call this result answers. An id we never saw
                # opened (older CLI, or a result whose start row was lost)
                # falls back to closing the oldest open call, so a missing id
                # can never leave a finished tool on the line forever.
                tid = b.get("tool_use_id")
                if tid in tools_open:
                    del tools_open[tid]
                elif tools_open:
                    del tools_open[next(iter(tools_open))]
            if results and not tools_open:
                tool_input_bytes = 0
                after_tool = True
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
                elif delta.get("type") == "input_json_delta":
                    tool_input_bytes += len(delta.get("partial_json") or "")
            elif et == "message_start":
                thinking_tokens = 0
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
                cb = ev.get("content_block") or {}
                block = cb.get("type")
                if block == "tool_use":
                    phase = "tooling"
                    tid = cb.get("id") or ""
                    tools_open[tid] = {
                        "id": tid, "name": str(cb.get("name") or "tool"),
                        "detail": _tool_detail(cb.get("name"), tool_inputs.get(tid, {}))}
                    tool_input_bytes = 0
        elif t == "result":
            saw_result = True
            idle = True
            new_session = row.get("session_id", new_session)
            result_text = row.get("result")
            if row.get("is_error"):
                error = str(result_text or "claude exited with an error")
                # Only if the failure arrived DURING a retry. A retry earlier in
                # the turn that then succeeded says nothing about why this ended.
                gave_up = was_retrying

    _write_bg_tasks(run_dir, bg_tasks)

    # Last word on the verb: a run sitting in a retry is not thinking, and
    # saying so is the whole point — "Thinking…" with a frozen token count is
    # indistinguishable from a hang, which is what an overload used to look like.
    if retry is not None:
        phase = "retrying"
    elif after_tool and phase == "tooling" and not tools_open:
        # Result in, nothing back yet, no `status` row (older CLI): the honest
        # word is still "sending", not "Working" over a tool that has finished.
        phase = "requesting"

    # A `_send` into this run is sitting between "queued" and "echoed" — see
    # `_send`'s own comment on why that window exists. `idle` was computed off
    # whatever `out.jsonl` happened to end with, which at this instant can
    # still be the PREVIOUS turn's own trailing `result`: believing that would
    # report the send as already finished and hand the page the previous
    # turn's reply as the answer to the new message (the bug this guards).
    # Liveness is left alone — a process that died before ever echoing the
    # follow-up must still end the poll, not hang it waiting for an echo that
    # is never coming.
    #
    # `pending_echo` holds the byte offset `out.jsonl` was at the moment
    # `_send` queued the message — NOT just "does this poll's rows contain any
    # `_starts_new_turn` row", which `rows` (from `_read_current_turn`) can
    # already do on its own: the cursor has not necessarily advanced past an
    # OLDER turn's own opening echo yet (see `_read_current_turn`'s
    # `seen_result` rule), so that row would still be in this poll's window
    # and would falsely look like the new message's own echo. Scanned
    # directly off the file, seeking straight to the recorded offset, rather
    # than through the cursor machinery — this is a one-time, bounded read
    # (only the bytes written since the send) whether or not the cursor
    # happens to reach that far this poll.
    pending_echo_path = os.path.join(run_dir, "pending_echo")
    if os.path.exists(pending_echo_path):
        try:
            with open(pending_echo_path, encoding="utf-8") as fh:
                pending_offset = int(fh.read().strip())
        except (OSError, ValueError):
            pending_offset = 0
        saw_echo_since_send = False
        try:
            with open(os.path.join(run_dir, "out.jsonl"), "rb") as fh:
                fh.seek(pending_offset)
                tail = fh.read()
        except OSError:
            tail = b""
        for raw_line in tail.split(b"\n"):
            if not raw_line:
                continue
            try:
                row = json.loads(raw_line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if _starts_new_turn(row):
                saw_echo_since_send = True
                break
        if saw_echo_since_send:
            try:
                os.remove(pending_echo_path)
            except OSError:
                pass
        else:
            idle = False

    # Finished: a `result` with nothing after it (the turn ended and no wake has
    # started another), or a process that is simply gone (D415).
    done = idle or not alive

    if not saw_result and done:
        # Dead without a `result` row = abnormal exit (crash, OOM, cancel),
        # even if some text streamed first. Report it as an error regardless
        # of partial text, so the UI doesn't render a truncated reply as a
        # clean success and the session-record guard below skips it.
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
    # Overload first, and it WINS: a run that died mid-retry is an API-health
    # story even when the underlying 429 text mentions a usage limit, and
    # letting _account_error re-match inside the overload message's
    # parenthesized original would bury the retries already spent.
    rewritten = _overload_error(error, gave_up or retry)
    error = rewritten if rewritten != error else _account_error(error)

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
    # session record below). This is a FALLBACK: the app's CLAUDE.md tells
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

    # First poll that sees the session id records it in the run dir (marker
    # file keeps the write one-shot across the remaining polls).
    marker = os.path.join(run_dir, "recorded")
    if new_session and not error and not os.path.exists(marker) and "file" in meta:
        try:
            # The id the CLI minted for this run, next to the id it resumed.
            # `--fork-session` makes those two different — so a page that
            # later asks "is anything running for this chat?" (see _live_run)
            # would be holding an id meta.json has never heard of.
            with _private_open(os.path.join(run_dir, "session")) as fh:
                fh.write(new_session)
            open(marker, "w", encoding="utf-8").close()
        except OSError:
            pass  # session bookkeeping must never break the chat itself

    # The streamed deltas are the full turn; the `result` row holds only the
    # LAST assistant message, so swapping to it after a tool-using turn threw
    # away every earlier message (the mid-sentence-freeze bug). Keep the
    # accumulated stream; fall back to `result` only when nothing streamed
    # (older CLI without --include-partial-messages).
    text = "".join(text_parts)
    # `saw_result`, not `done`: the fallback is about the row that carries the
    # text, and a run whose process is still up between turns (D415) has that
    # row already — waiting for the exit would blank a delta-less turn's reply
    # for as long as the run stays awake.
    if not text and saw_result and result_text and not error:
        text = result_text
    # `segments` is the authoritative record of the turn; `text` is the flat
    # legacy field, kept byte-identical to what it has always been for the
    # callers that only want prose (and for the error paths, which have no
    # segments to render).
    #
    # They agree EXACTLY — the text segments join back into this string — only
    # while stream deltas are present, which is every run of a current CLI.
    # On a delta-less run the fallback two blocks up makes `text` the `result`
    # row, i.e. the LAST assistant message, while `segments` carry all of them:
    # so `text` can be a strict SUBSET of the transcript, never a superset, and
    # never a different turn. That is deliberate and pinned by a test — the
    # alternative was widening `text` on the fallback path, and its byte
    # identity is a harder constraint than this asymmetry is a cost.
    return {"text": text, "done": done, "session_id": new_session, "error": error,
            "tokens": tokens_done + tokens_current, "phase": phase,
            "message": meta.get("message", ""), "permissions": permissions,
            "app_state": app_state, "mode": _live_mode(meta, permissions),
            "skills": skills, "retry": retry, "retry_total": retry_total,
            "retry_status": retry_status,
            # WAS THIS RUN'S END ASKED FOR — read off the run rather than
            # remembered by whoever asked. The page's own stop button sets a
            # variable and can swallow the resulting error itself, but a stop
            # from anywhere else (the tasks queue card's ✕, which goes through
            # schedule.py) left this page reporting the kill as a crash. See
            # `_cancel`, which writes the marker.
            "cancelled": os.path.exists(os.path.join(run_dir, "cancelled")),
            # Same array `activity.tasks` renders from, collapsed to the
            # yes/no a session host's reap loop wants: background work still
            # running is what holds a host open past an otherwise-idle turn
            # (see _turn_state, which computes this same fact off out.jsonl
            # alone for a caller with no reason to parse the whole file).
            "tasks_pending": bool(bg_tasks),
            "activity": {
                "tool": next(reversed(tools_open.values())) if tools_open else None,
                "tools_open": len(tools_open),
                "tool_input_bytes": tool_input_bytes if tools_open else 0,
                "thinking_tokens": thinking_tokens if phase == "thinking" else 0,
                "hook": next(reversed(hooks_open.values())) if hooks_open else "",
                "tasks": [{"id": k, "description": v} for k, v in bg_tasks.items()],
                "agent_rows": agent_rows,
            },
            "segments": _segments_from_rows(parsed)}


# ------------------------------------------------------- sessions & history

_MODEL_SHORT = ("fable", "opus", "sonnet", "haiku")
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# The PINNED entries the picker offers beside the aliases — full `--model` ids
# rather than a family name (template.html's MODELS). They have to be matched
# BEFORE the short names below, and matched exactly, because every one of them
# contains its own family name: "claude-fable-5-1-20260401" collapsed to "fable"
# under the old loop, so a chat actually running the pinned model came back from
# detection as the floating alias and the pill preselected the wrong option.
#
# Each pattern is anchored on the version and refuses a longer number, so a
# future "fable-5-10" is NOT read as 5.1 — the whole point of a pinned entry is
# that it names one model, and a prefix match would quietly stop being true.
# What is not pinned still falls through to the family name, which is the right
# answer for it: the alias is what the user would have picked.
_MODEL_PINNED = ((re.compile(r"fable-5[.-]1(?!\d)"), "claude-fable-5-1"),)


def _short_model(raw: str) -> str:
    """Collapse any spelling of a model — full id ('claude-fable-5'), alias
    ('opusplan'), or already-short name — to one of the selector's values.

    That is a short family name ('fable') for anything the picker offers as an
    alias, and the full pinned id ('claude-fable-5-1') for the ones it offers by
    version. The return value is a value the <select> actually holds: the page
    validates detection against its own MODELS list and drops anything else, so
    a spelling that does not round-trip here is a preselect that silently never
    happens."""
    raw = (raw or "").lower()
    for pattern, value in _MODEL_PINNED:
        if pattern.search(raw):
            return value
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


# How many OUTSIDE sessions the list carries, and how far into one of them the
# title read goes. Both are ceilings on work the home view pays for on every
# paint, over a folder whose project dir may hold hundreds of transcripts.
#
# 131072 is ~3.5x the deepest first-user-row this machine's 154 real transcripts
# have (37 KB — Claude Code writes its SessionStart hook output, which can be a
# whole skill file, ahead of the first thing the user said). A head read is the
# only affordable shape here: transcripts run to multiple MB, and everything
# this needs is in the opening rows.
_CLI_SESSION_LIMIT = 30
_CLI_HEAD_BYTES = 131072


def _cli_preview(path: str, workdir: str) -> tuple[str, str]:
    """`(preview, pane)` for one transcript: the first thing a HUMAN said in it
    truncated to 80 chars, and the FILE that chat was opened on. Either is ""
    when the transcript has none — a preview of "" means a transcript this list
    has no business showing at all.

    The pane rides this read rather than getting one of its own: the first send
    from a pane carries both the app-state block and the words, so the record
    that answers the preview is the record that answers the pane, and a second
    pass would double the opens to learn nothing new. It stops with the preview
    for the same reason — scanning on past it would trade a bounded head read
    for a full one on every pane-less chat.

    Read from the file's HEAD only, and only far enough to find that message:
    the alternative is parsing whole multi-MB transcripts to label a row.

    Two things earn a "": a transcript nobody ever spoke in (a session that
    opened and closed is not a past chat — there is nothing to name it with and
    nothing to resume into), and one whose own `cwd` is not this folder. The
    second is the munge guard: `_munge` maps every non-alphanumeric char to "-",
    so `/a/b-c` and `/a-b/c` land in the SAME project dir, and the directory
    name cannot be decoded back (server/routers/claude_sessions.py carries the
    same caveat and takes the same way out — believe the transcript, not the
    dirname).

    Skipped rows: `isMeta` (the local-command caveat Claude Code writes for the
    user), `isSidechain` (a subagent's prompt, which the user never typed), and
    any row that is machinery all the way down once `_strip_machinery` has had
    it — a slash command's envelope, a subagent reporting back, a wordless
    screenshot send.

    That last test used to be `startswith("<")`, and it was too blunt by exactly
    one case: the case THIS PAGE causes. `composeOutgoing` prepends the app-state
    block and the pane shots to what the user typed, so the only message in a
    session can open with "<" and still be the user's own words — the row went
    nameless while the words sat right there after the block. The annotation
    preamble it never caught at all, having no tag to open with, so those rows
    were titled "The user annotated 1 element in the left previe…".
    """
    try:
        with open(path, "rb") as fh:
            blob = fh.read(_CLI_HEAD_BYTES)
    except OSError:
        return "", ""
    lines = blob.decode("utf-8", "replace").splitlines()
    # A head read cuts the last line mid-way. Drop it rather than let it look
    # like a corrupt transcript — we are the ones who truncated it.
    if len(blob) == _CLI_HEAD_BYTES and lines:
        lines.pop()
    cwd_seen = False
    pane = ""
    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        # Checked before the row is used for anything else, so a colliding
        # transcript is rejected on the first row that can prove it (normally
        # line 0, and always at or before the first user row — user rows carry
        # `cwd` themselves).
        if not cwd_seen:
            cwd = row.get("cwd")
            if isinstance(cwd, str) and cwd:
                if os.path.abspath(cwd) != workdir:
                    return "", ""
                cwd_seen = True
        if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
            continue
        content = (row.get("message") or {}).get("content")
        if isinstance(content, list):
            # Block form: prose only. A message that is nothing but a tool
            # result or an image has no words to title a row with.
            content = " ".join(b.get("text", "") for b in content
                               if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(content, str):
            continue
        # RAW, and before the stripper: the block the pane is named in is the
        # very thing `_strip_machinery` exists to remove. Read once and kept,
        # so a first send that turns out to be wordless still leaves the pane
        # behind for the record two lines down to be titled with.
        if not pane:
            pane = _pane_file(content)
        # The pins are the fallback, not the first choice: a send that carried
        # both free text and annotations is named by the text (see `_ann_notes`
        # for why that reading is not folded into the stripper).
        content = _strip_machinery(content) or _ann_notes(content)
        if not content:
            continue
        return (content[:80], pane) if cwd_seen else ("", "")
    return "", ""


def _cli_sessions(file: str) -> list:
    """Claude sessions about this target — every transcript in this cwd's
    project dir when the target is a FOLDER, and only the ones opened on this
    very file when it is a FILE.

    They need no import, no copy and no new resume path, which is the whole
    reason this is a dozen lines: a session's home is its cwd's project dir
    (`_munge(_workdir(file))`), the template keys on exactly the same dir, so
    these transcripts are already sitting where `_history` reads and where
    `--resume` looks from.

    The folder collapse in `_workdir` is not a bug and is not undone here:
    Claude Code keys its store by cwd and a file has no cwd, so resume, history
    and spawn must all keep using the folder. What was wrong was only the LIST
    — three files in one folder shared one pile of chats, and selecting file 1
    offered you a chat that was entirely about file 3. The pane the chat was
    opened on (`_pane_file`, off the leading app-state block) is what tells
    them apart, and it is the same reading the server's Tasks list uses to
    decide which file "open this task" lands on.

    A transcript with no pane at all — a terminal session, a chat started on
    the folder itself — belongs to the FOLDER, and is offered there. It is not
    offered on a file, because it is not about one; showing it under every file
    in the folder is the pile we are dismantling.
    """
    workdir = os.path.abspath(_workdir(file))
    # "" for a folder target: the filter below is what a file target adds, and
    # a folder is the case where every transcript in the dir already qualifies.
    want = "" if os.path.isdir(file) else os.path.abspath(file)
    # One scan for the whole list — see `_live_sessions` for why this is not
    # `_live_run` asked once per row.
    live = _live_sessions(file) | _registry_running(_workdir(file))
    proj = os.path.join(PROJECTS, _munge(workdir))
    try:
        names = os.listdir(proj)
    except OSError:
        return []      # no store, or no sessions ever in this folder
    found = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        sid = name[:-len(".jsonl")]
        # `_bad_id` because the id becomes a path again on resume (and a URL
        # param on the way there); a filename we cannot round-trip is not
        # offered at all.
        if _bad_id(sid):
            continue
        try:
            found.append((os.path.getmtime(os.path.join(proj, name)), sid))
        except OSError:
            continue
    found.sort(reverse=True)
    out = []
    for mtime, sid in found:
        if len(out) >= _CLI_SESSION_LIMIT:
            break
        preview, pane = _cli_preview(os.path.join(proj, sid + ".jsonl"), workdir)
        if not preview:
            continue
        # Compared as an abspath, not as text: the block records the pane's own
        # url, and the target arrives from the caller — the two can spell the
        # same file differently and still be it.
        if want and (not pane or os.path.abspath(pane) != want):
            continue
        # mtime is the only timestamp a transcript offers for free — it is the
        # last activity, so it lands on `last_used` and `created_at` borrows it.
        out.append({"id": sid, "preview": preview,
                    "created_at": mtime, "last_used": mtime,
                    "cwd": workdir, "pane": pane, "running": sid in live})
    return out


def _sessions(file: str) -> dict:
    """Every Claude session about this target, newest activity first.

    ONE list, from the cwd's project dir, because the user has one memory: a
    chat they had about this thing is a chat they had about this thing, and it
    being in a terminal an hour ago rather than in this page does not make it a
    different thing to go back to.

    "This thing" is the target, though, not always its folder — see
    `_cli_sessions` for why a FILE target is offered only the chats that were
    opened on that file.
    """
    file = os.path.abspath(file)
    sessions = _cli_sessions(file)
    sessions.sort(key=lambda s: s.get("last_used") or s.get("created_at") or 0,
                  reverse=True)
    return {"sessions": sessions}


def _snapshots(file: str, enrich: bool, deltas: bool) -> dict:
    """Claude Code's file-history checkpoints for `file` (SPEC §34).

    A pass-through to `shared/file_history.timeline`, which is the ONE reader
    for this store and already returns its own empty states as data ("no store
    on this machine", "no versions for this file") — that is the whole reason it
    can be adopted here unchanged, and the reason a file Claude has never
    touched renders a sentence rather than the red traceback overlay. This
    module adds nothing but the offer; the store stays strictly READ-ONLY, as it
    must, because it is Claude Code's data and the very edit history the feature
    exists to protect.

    Deliberately NOT the `history` action: that one on this module replays a
    chat SESSION TRANSCRIPT. Two meanings on one action name is the sort of
    collision that is only ever found in production.

    **Files only.** A directory has no checkpoint chain — the store keys on one
    absolute file path (`sha256(abspath)[:16]@vN`) — so a folder target is a
    refusal here as well as being hidden in the page. The gate is the UX, the
    module is the guarantee (MD-11): a hand-written call cannot reach a state
    the panel does not offer.

    TWO cost knobs, both defaulting to the expensive-and-complete answer so a
    hand-written call gets the whole truth, and both declined by the page:

      * `enrich` reads session transcripts (5 MB+) and is what makes the
        creation boundary visible. Honoured here and nowhere else, exactly as
        the annotate panel had it.
      * `deltas` runs `difflib` once per version for the exact added/removed
        pair. It is the entire cost of a timeline — measured at 290 ms of a
        292 ms read on a 453 KB file with 12 checkpoints, against 0.2 ms to
        enumerate the store — and the page declines it because those two numbers
        are row decoration: the diff a user actually reads comes from
        `snapshot_plan`, per version, on the click that opens the row. Nothing
        structural moves either way (`file_history.timeline`), so the list is the
        same list with softer counts.

    ImportError alone is caught, and it means one thing: this folder was copied
    without its `shared/` sibling. A blanket `except Exception` would report a
    SyntaxError inside `file_history.py` as "helper is not available", which
    sends the reader to entirely the wrong place.
    """
    bad = _snap_target(file)
    if bad:
        return {"error": bad}
    try:
        import file_history
    except ImportError:
        return {"error": "file history helper (../shared/file_history.py) "
                         "is not available"}
    try:
        return file_history.timeline(file, enrich=enrich, deltas=deltas)
    except Exception as exc:  # noqa: BLE001 — a state to render, never an overlay
        return {"error": f"{type(exc).__name__}: {exc}"}


# ------------------------------------------------- going back to a snapshot

def _snap_target(file: str) -> str:
    """Empty when this panel may touch `file`, else the sentence saying why not.

    One gate for all three actions, cheapest and most dangerous first, so a
    hand-written call cannot reach a target the panel does not offer (MD-11):

      * no target at all;
      * a MOUNT-BACKED path. This runs BEFORE any stat, deliberately: the bytes
        under the mounts dir come from a remote over FUSE and an ordinary kernel
        stat on a wedged mount hangs the worker — the very reason
        `condition.py` refuses to offer this template there at all. `appenv`
        unreachable means we cannot tell, which reads as refuse (CT-12), and it
        can only happen for a copy of this folder taken without its `shared/`
        sibling;
      * a DIRECTORY. The store keys on one absolute FILE path
        (`sha256(abspath)[:16]@vN`), so a folder has no checkpoint chain to
        show, plan against, or write back.
    """
    if not file:
        return "missing target file (no _file param?)"
    try:
        from appenv import is_mount_backed
    except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
        return ("cannot tell whether this path is on a remote mount, so "
                "file history is not offered here")
    if is_mount_backed(file):
        return ("this file is on a remote mount, where file history is not "
                "offered")
    if os.path.isdir(file):
        return "file history is per-file; a folder has no checkpoints"
    return ""


def _snapshot_plan(file: str, version_id: str) -> dict:
    """What going back to `version_id` would do — what the expanded row shows.

    `version_id` is REQUIRED here, unlike annotate's equivalent. This panel is a
    list of rows and every plan comes from clicking one, so there is no "the
    last change" to resolve and nothing for this action to guess. The plan
    carries the diff itself (see `file_history._diff`), because the counts
    beside it answer how MUCH changes and never WHAT — and on the one
    destructive action here the second is the question being confirmed.
    """
    bad = _snap_target(file)
    if bad:
        return {"error": bad}
    if not isinstance(version_id, str) or not version_id:
        return {"error": "snapshot_plan needs the version_id of the row that "
                         "was clicked — it never picks a snapshot itself"}
    try:
        import file_history
    except ImportError:
        return {"error": "file history helper (../shared/file_history.py) "
                         "is not available"}
    try:
        return file_history.revert_plan(file, version_id)
    except Exception as exc:  # noqa: BLE001 — a state to render, never an overlay
        return {"error": f"{type(exc).__name__}: {exc}"}


def _snapshot_revert(file: str, version_id: str, confirm_unique: bool) -> dict:
    """Put a snapshot back on disk — applying a plan the caller has already seen.

    Two refusals, both structural rather than cosmetic:

      * the plan's `id` must be echoed back. A destructive write off its own
        freshly-computed choice has no confirmation token at all, and the echo
        doubles as a freshness check — a plan built against one disk state and
        applied against another is exactly how a user confirms one diff and gets
        a different one.
      * when the plan reports `unique_current` — the bytes on disk are in no
        checkpoint, so the write destroys the only copy — `confirm_unique` must
        be true. Deliberately NOT demanded for an ordinary step back, where
        nothing unrecorded is lost: a token the caller always passes is a token
        nobody reads.
    """
    bad = _snap_target(file)
    if bad:
        return {"error": bad}
    if not isinstance(version_id, str) or not version_id:
        return {"error": "snapshot_revert needs the version_id from a "
                         "snapshot_plan call — it never picks a snapshot itself"}
    try:
        import file_history
    except ImportError:
        return {"error": "file history helper (../shared/file_history.py) "
                         "is not available"}
    try:
        plan = file_history.revert_plan(file, version_id)
        if not plan.get("ok"):
            return plan
        if plan.get("writable") is False:
            return {"error": "This file cannot be restored: "
                             + (plan.get("writable_reason")
                                or "it is not writable")}
        if plan.get("unique_current") and not confirm_unique:
            return {"error": "what is on disk now is in no snapshot, so going "
                             "back would destroy the only copy — confirm once "
                             "the user has been shown that",
                    "plan": plan}
        res = file_history.apply_revert(file, plan["id"])
    except Exception as exc:  # noqa: BLE001 — a state to render, never an overlay
        return {"error": f"{type(exc).__name__}: {exc}"}
    # The POST-write timeline, in the same response: without it the row list
    # goes on showing the pre-revert position for a whole round trip —
    # precisely the window in which the user is staring at it to find out
    # whether it worked. Enriched, because an unenriched timeline cannot see the
    # did-not-exist boundary and would report the chain a step short.
    #
    # Best-effort, and the key is simply ABSENT when it fails: the write already
    # landed and is already reported, so a failure to re-enumerate the store must
    # not turn a successful revert into an error. The page falls back to its own
    # `snapshots` call. Named on stderr all the same — with no trace at all, a
    # timeline that has started failing every time is indistinguishable from one
    # that never fails.
    try:
        res["timeline"] = file_history.timeline(file, enrich=True)
    except Exception as exc:  # noqa: BLE001
        print("claude: post-revert timeline failed, the page will re-read it "
              "itself — %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    return res


def _stopped_last(file: str, session_id: str) -> bool:
    """Was the most recent run of this conversation STOPPED by the user?

    A killed run writes no `result` row, so a stopped turn is indistinguishable
    in the transcript from one that crashed or one that simply streamed less
    than usual — the transcript records what claude said, never why it stopped
    saying it. The evidence lives in the run dir instead (`_cancel`'s marker),
    which is why this reads runs rather than records.

    NEWEST RUN ONLY, and that is the whole rule. The question a reopened chat
    asks is about the turn at the bottom of it, so a run that was stopped
    yesterday and followed by one that completed says nothing about what is on
    screen now — answering True there would put "Stopped" under a finished
    reply. So the newest run for this conversation decides, and it decides for
    itself: no marker, no note.

    Matched the way `_live_run` matches — the target, then either the session
    the run RESUMED or the one the CLI minted for it, since `--fork-session`
    makes those differ — and bounded by the same scan limit, because a run
    buried under sixty newer ones is not the bottom of anybody's chat.

    False for everything unreadable. A missing runs dir, a meta.json that will
    not parse, a marker we cannot stat: the note is an explanation, and an
    explanation nobody can substantiate is better left unsaid than guessed.
    """
    if not session_id:
        return False
    file = os.path.abspath(file)
    try:
        names = sorted(os.listdir(RUNS), reverse=True)[:_LIVE_SCAN_LIMIT]
    except OSError:
        return False
    for name in names:
        run_dir = os.path.join(RUNS, name)
        try:
            with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        if os.path.abspath(meta.get("file", "")) != file:
            continue
        own = ""
        try:
            with open(os.path.join(run_dir, "session"), encoding="utf-8") as fh:
                own = fh.read().strip()
        except OSError:
            pass
        if not own:
            own = _session_from_out(run_dir)
        if own != session_id and str(meta.get("resumed_from") or "") != session_id:
            continue
        # The newest run of this conversation — whatever it says, it is the
        # answer, so this returns rather than carrying on down the list.
        return os.path.exists(os.path.join(run_dir, "cancelled"))
    return False


def _transcript_stat(path: str) -> dict:
    """`{path, mtime, size}` for a session transcript — the page's watermark.

    Its two readers are `_history` (which stats BEFORE it reads, see there) and
    the page, which hands `path` to `/api/claude-sessions/liveness` on every lap
    of its live watch and compares the pair. A missing file answers zeroes rather
    than raising: a chat can be open on a session whose transcript does not exist
    yet, and "0, 0" is the honest watermark for one — the first row written moves
    it."""
    try:
        st = os.stat(path)
        return {"path": path, "mtime": st.st_mtime, "size": st.st_size}
    except OSError:
        return {"path": path, "mtime": 0.0, "size": 0}


def _history(file: str, session_id: str) -> dict:
    """Rebuild the conversation from the Claude Code session transcript.

    Resolved ONLY at the target file's own project dir — with copied files
    the same session id exists in several project dirs with divergent
    content, and a glob would render some other copy's conversation while
    resume continues this one's. Migrates first (same as `start`) so a moved
    file's saved session shows its turns immediately, without waiting for the
    user to send a message.

    Assistant turns carry `segments` as well as `text` — the same ordered
    text/tool record `_poll` returns, through the same `_segments_from_rows`, so
    a restored conversation shows the tool calls it made instead of only the
    prose around them. User turns keep just `text`: there is nothing structured
    about a typed message, and the app-state block is stripped from it BEFORE
    anything else reads it (below), which is also why segments cannot become a
    second route back for the block the user never saw.

    User turns DO carry `uuid`, the transcript record's own id. It is the one
    field a restored turn can be addressed by from outside this page: the Tasks
    list reads the same uuid off the same record (`_prompt`, server/routers/
    tasks.py) and links a message as `?msg=<uuid>`, so the chat can scroll to the
    turn a person clicked instead of to the top of the conversation. "" on a
    record that has none — the template treats the key as optional throughout."""
    if _bad_id(session_id):
        return {"turns": [], "transcript": _transcript_stat("")}
    file = os.path.abspath(file)
    path = os.path.join(PROJECTS, _munge(_workdir(file)),
                        session_id + ".jsonl")
    # SAMPLED BEFORE THE READ, and the order is the whole guarantee (D415). This
    # is the watermark the page follows the conversation by — it re-renders when
    # the file moves past it — and a stat taken AFTER the read would describe
    # rows this payload may not contain, which is a turn silently swallowed. Taken
    # first, a write that lands mid-read shows up as a watermark the very next lap
    # disagrees with: one redundant re-render, never a missed one.
    #
    # The PATH rides back for `/api/claude-sessions/liveness`: resolving an id to
    # a transcript belongs here (the line above is the only place that knows the
    # folder this chat is open on), and the endpoint refuses to do it.
    stat = _transcript_stat(path)
    if not os.path.isfile(path):
        return {"turns": [], "transcript": stat}

    turns = []
    stretch = []  # rows of the assistant reply being read, for its segments

    def close_stretch():
        """Attach the stretch's segments to the assistant turn they belong to.

        Deferred to the END of the stretch because that is the first moment the
        turn is certainly there: the text turn is opened by whichever assistant
        row first carries prose, and a reply that only called tools opens no
        turn at all until here — dropping its segments would lose the only
        record that the work happened. Merged, never assigned, for the same
        reason consecutive assistant rows merge their text: a user row that was
        filtered out (a slash command, an app-state-only message) does not end
        the reply, so a later stretch can land on the same turn.
        """
        if not stretch:
            return
        segments = _segments_from_rows(stretch)
        del stretch[:]
        if not segments:
            return
        if turns and turns[-1]["role"] == "assistant":
            turns[-1]["segments"] = turns[-1].get("segments", []) + segments
        else:
            turns.append({"role": "assistant", "text": "", "segments": segments})

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
            # Claude Code's own synthetic `user` records are not turns: nobody
            # typed them and the reader has no use for their XML. Two of them
            # were named literally here; the rest — `<task-notification>` the
            # loudest, a whole notification block rendered as a message bubble —
            # were not, so they arrived on screen verbatim (D415). One list now,
            # the same `_MACHINERY_DROP` the session names are filtered by.
            # Everything filtered here falls to `stretch`, where
            # `_segments_from_rows` turns a task-notification into its chip and
            # ignores the others.
            if text.strip() and not _LEADING_DROP_OPEN.match(text.lstrip()):
                close_stretch()  # before the user turn, or the segments land on it
                turns.append({"role": "user", "text": text,
                              "uuid": str(row.get("uuid") or "")})
            else:
                # Everything else on a `user` row belongs to the assistant's
                # reply: tool_result blocks are what its tool segments are
                # waiting for, and the synthetic rows are not a turn either way.
                stretch.append(row)
        elif role == "assistant" and isinstance(content, list):
            stretch.append(row)
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                # consecutive assistant rows are one streamed turn; keep merged
                # (blank line between rows, matching _poll's stream separator)
                if turns and turns[-1]["role"] == "assistant":
                    turns[-1]["text"] += "\n\n" + text
                else:
                    turns.append({"role": "assistant", "text": text})
    close_stretch()
    # ...and whether the last of those turns was ENDED BY THE USER. The
    # transcript cannot say — a killed run just stops writing — so it is read
    # off the run dir (`_stopped_last`) and reported on the turn it belongs to,
    # which is the one the reader is looking at the bottom of. The page draws it
    # as the same ⏹ note a live stop leaves behind, so a stop looks identical
    # whether you watched it happen or came back to it later.
    if turns and _stopped_last(file, session_id):
        turns[-1]["stopped"] = True
    # `transcript` is the watermark the page's live watch compares against
    # (origin/main, D406) — the stat taken BEFORE this read, so a row appended
    # while we were parsing shows up as a change rather than being missed.
    return {"turns": turns, "transcript": stat}


def _cancel(run_id: str, interrupt_first: bool = True) -> dict:
    """End a run — the STOP button's own action, and also `_send`'s way of
    ending a session it cannot hand a mid-session change to (a `read_dirs`
    that outgrew what was granted at spawn, or a changed `effort`).

    `interrupt_first` is what tells those two callers apart. The stop button
    wants the gentler `interrupt` control request tried first, keeping a live
    host (and any background task it is holding open) alive — see the block
    below. `_send`'s respawn path wants the opposite: it is calling BECAUSE
    the session can no longer serve this caller as it stands, so it passes
    `interrupt_first=False` to skip straight to ending the whole process
    tree — trying `interrupt` there would just leave the very host `_send`
    needs gone.
    """
    run_dir = os.path.join(RUNS, run_id)
    # Same guard as _poll: run_id is joined into a path and drives a kill,
    # so reject anything that could resolve outside the runs dir.
    if _bad_id(run_id) or not os.path.isdir(run_dir):
        return {"cancelled": run_id}
    # WHO ASKED, written down before anything is killed.
    #
    # Killing claude leaves the run dead with no `result` row, which `_poll`
    # reports as an error BY DESIGN — a truncated reply must never read as a
    # clean success. The page that PRESSED stop knows to swallow that error and
    # say "Stopped" instead, but it knows it from a variable of its own
    # (`stoppedRun`), so every other reader saw a crash: a stop from the tasks
    # queue card left the chat showing "claude exited before completing the
    # reply", and a chat reopened afterwards showed a half-finished reply with
    # nothing at all to say the user had ended it (Akshil, 2026-08-21).
    #
    # This marker is the RUN's own record that its end was asked for, so `_poll`
    # and `_history` can both say so however the stop arrived. Written first,
    # because everything below can fail — an unreadable pid, a kill that does
    # not land — and the intent is true regardless of how the kill went.
    try:
        with _private_open(os.path.join(run_dir, "cancelled")) as fh:
            fh.write(str(time.time()))
    except OSError:
        pass  # bookkeeping must never stand between the user and a kill
    # Answer before killing: the kill takes the whole tree (the MCP server
    # included) on both platforms, but if it fails, a parked approval would
    # otherwise sit there holding the subprocess open for the full timeout.
    _deny_pending(run_dir, "cancelled")
    if interrupt_first and _host_alive(run_dir):
        # Interrupt the TURN, not the whole session. A live host survives an
        # `interrupt` control request (verified live against 2.1.251) exactly
        # the way it survives a trailing `result` — the CLI stays up on the
        # same held-open stdin, ready for the next follow-up, and the pid
        # file (the CLI's own) never changes. This is the whole reason a stop
        # button no longer has to mean "end the session": a background task
        # started earlier in the SAME session outlives a stop pressed on a
        # later turn, and the next message continues this session instead of
        # resuming a dead one.
        # Captured BEFORE the request is queued — see `_await_control_response`
        # for why that ordering is what makes the seek safe.
        try:
            out_offset = os.path.getsize(os.path.join(run_dir, "out.jsonl"))
        except OSError:
            out_offset = 0
        request_id = _write_control_request(run_dir, "interrupt")
        response = _await_control_response(
            run_dir, request_id, start_offset=out_offset)
        if response is not None:
            # The interrupt LANDED — the whole point of trying it first is
            # that the session survives, so the marker written above (which
            # exists to tell every later reader "this run's end was asked
            # for") must not outlive the turn it was asked for. Left in
            # place, it would taint every later poll of the same run_id —
            # spanning a whole multi-turn session now, not one turn — as
            # cancelled forever: a clean turn 3 would read as "the turn
            # finished before the stop landed", and a real error on turn 3
            # would be swallowed into a silent "Stopped." instead of shown.
            try:
                os.remove(os.path.join(run_dir, "cancelled"))
            except OSError:
                pass
            return {"cancelled": run_id,
                    "still_queued": list(response.get("still_queued") or [])}
        # No answer inside the timeout — the host may be stuck, or died
        # between the liveness check above and now. Falls through to the
        # tree-kill below exactly as if no host had ever been found: ending
        # the whole session is the right fallback for "asked and got
        # nothing back", not a hang.
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
         state: str = "", has_pane: str = "", enrich: str = "",
         deltas: str = "", version_id: str = "", confirm_unique: str = "",
         answers: str = "", note: str = "", custom: str = "",
         read_dirs: str = "", path: str = "") -> dict:
    if action == "start":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        if not message:
            return {"error": "(empty message)"}
        # `has_pane` arrives as a STRING like every other param (the URL/param
        # binder is str-shaped). Empty means "the caller did not say" — the apps
        # API, which has no page — and only then does `_start` ask disk. "0" is a
        # real no, so it must not be read as absence.
        return _start(file, message, session_id, model, effort, permission_mode,
                      has_pane=None if has_pane == "" else has_pane != "0",
                      extra_read_dirs=_attach_dirs(read_dirs))
    if action == "poll":
        # `file` rides along so the poll can refuse a run that is not about
        # this page's target (see _poll) — optional, because not every caller
        # has a page (claude_spawn's record loop).
        return _poll(run_id, file)
    if action == "decide":
        # `answers` arrives as a JSON string for the same reason `state` does
        # below — params cross into python string-shaped — and is only read for
        # an AskUserQuestion request (see _decide). `note` is the plan card's
        # equivalent: free text the user typed next to "keep planning", read only
        # for an ExitPlanMode deny and only ever as part of its message. `custom`
        # is the question card's sibling of both: a JSON record, keyed by the
        # same question text as `answers`, of what the user typed into "Other".
        return _decide(run_id, request_id, decision, scope, mode, answers, note,
                       custom)
    if action == "app_state":
        # `state` arrives as a JSON string, not a nested object: params reach
        # main() through the URL/param binder (str-shaped), and the snapshot is
        # the page's own structure — nothing here reads inside it.
        return _answer_app_state(run_id, request_id, state)
    if action == "sessions":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _sessions(file)
    if action == "live_run":
        # "Is a run for this chat still going?" — the lookup a page needs when it
        # arrives without a `run` param but the turn it started is still
        # streaming somewhere. `session_id` is optional: without one this
        # answers for the target as a whole.
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _live_run(file, session_id)
    if action == "defaults":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _defaults(file)
    if action == "history":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _history(file, session_id)
    if action == "snapshots":
        # `enrich` arrives as a STRING like every other param (the binder is
        # str-shaped), so "" and "0" both mean don't — the boot call sends
        # nothing and pays for no transcript reads.
        #
        # `deltas` is read the other way round: ABSENT means yes, because the
        # complete answer is the one a caller who did not think about it should
        # get, and only "0"/"false" decline. The page sends "0" and pays for no
        # difflib; the two knobs read in opposite directions because their honest
        # defaults are opposite.
        return _snapshots(file, enrich not in ("", "0", "false"),
                          deltas not in ("0", "false"))
    if action == "snapshot_plan":
        return _snapshot_plan(file, version_id)
    if action == "snapshot_revert":
        # `confirm_unique` arrives as a STRING like every other param, and only
        # a positive one counts: this is the token that stands between a click
        # and destroying the only copy of what is on disk.
        return _snapshot_revert(file, version_id,
                                confirm_unique not in ("", "0", "false"))
    if action == "shots_dir":
        # Asked for by the page BEFORE it composes a message, because that is
        # when it has crops to upload — see SHOTS for why this is not a run dir.
        return _shots_dir()
    if action == "image_to_png":
        # A picture the page uploaded but cannot DECODE (tiff, heic). Asked for
        # right after the upload, so the chip can show a thumbnail and — the real
        # point — so the agent's `Read` gets a format it can open. `path` is the
        # file the page just wrote, and `_image_to_png` re-checks that it is
        # inside SHOTS before touching it.
        return _image_to_png(path)
    if action == "terminal_command":
        return _terminal_command(file, session_id)
    if action == "cancel":
        return _cancel(run_id)
    if action == "live_host":
        # "Is there a session I can hand a follow-up to?" — asked BEFORE
        # every send (see template.html's sendMessage): a host answering
        # here is what turns a follow-up into an inbox write instead of a
        # fresh `_start`. Unlike `live_run`, this says yes for a session
        # sitting idle between turns too — see `_live_host`'s own comment.
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _live_host(file, session_id)
    if action == "send":
        # `read_dirs` is the SAME per-message attachment string `action=start`
        # takes (see `_attach_dirs`), just re-checked against what the host
        # was already granted at spawn time. `model`/`effort`/
        # `permission_mode` are the picker's current values, sent on every
        # turn exactly as `action=start` already receives them — `_send`
        # itself decides which of the three (if any) actually changed, and
        # whether that means a control request or a forced respawn.
        return _send(run_id, message, read_dirs, model, effort,
                    permission_mode)
    return {"error": f"unknown action: {action}"}
