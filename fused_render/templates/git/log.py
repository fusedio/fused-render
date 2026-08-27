"""Reader backing `git/template.html` — git history and changes scoped to the
open path (SPEC §33 / GT-5..GT-10).

Everything here is `git` shelled out to and parsed. Nothing reimplements git's
knowledge of anything: not what a repository is, not what "dirty" means, not how
a rename is detected, not how long ago a commit was. The module's whole job is to
ask the right bounded question and turn the answer into JSON.

Five operations, one per thing the view can ask for:

  overview  — the header (repo, branch, detached, dirty, scope), the uncommitted
              changes under the scope, and the FIRST page of the scoped log; one
              call, so opening the view is one round trip.
  log       — a later page of the same log ("load more").
  commit    — one commit's metadata plus its diff, restricted to the scope.
  worktree  — the working tree vs HEAD for one uncommitted entry.
  pending   — the diff a commit made right now would RECORD, under its own
              (smaller) cap, for the AI commit-message writer (GT-18).

The rules every invocation obeys, because each one is a way this could go wrong:

* **`-C <repo root>` and `--no-pager` on every call.** The root is resolved once
  with `rev-parse --show-toplevel`; every later command is pinned to it, so a
  relative pathspec means exactly one thing and a `cd` cannot change it.
* **argv lists only, never a shell string, and `--` before every pathspec.** A
  path is data. After `--` it can never be read as a revision, and a pathspec is
  additionally wrapped in `:(literal)` so a filename containing `*`, `?`, `[` or
  a leading `:` is matched as itself rather than as a glob or as pathspec magic.
* **A revision that is not a hex object name never becomes an argument.** `sha`
  arrives from a URL param; it is validated against `_SHA_RE` before any argv is
  built, so an option-shaped value cannot reach git even in the position where
  options are still legal.
* **Machine formats only.** The log is `%x00`-delimited fields with one commit
  per line (every field in the format is single-line by construction, so the
  newline is an unambiguous record separator); status is `--porcelain=v1 -z`.
  Nothing parses human-formatted output — except `%ar`, which is a human STRING
  we pass through verbatim rather than reinventing "3 months ago" from a date.
* **A timeout on every call**, and diffs additionally capped in bytes AND lines
  while they stream, with a watchdog that kills the process — so a
  hundred-megabyte diff neither buffers into memory nor wedges the browser.
* **Non-interactive by environment**: no credential prompt, no askpass, no
  pager, no optional locks, no LFS smudge. Nothing here contacts a remote, but a
  repository can carry config that makes a local command ask a human something,
  and a question nobody can answer is a hang.
* **The user's git config is deliberately LEFT ALONE** — no
  `GIT_CONFIG_GLOBAL=/dev/null`. `safe.directory` lives there, and a repository
  the user has explicitly marked safe must keep working here. The specific knobs
  that could corrupt parsing are overridden per command with `-c` instead
  (`core.quotepath=false`, `color.ui=false`, `log.showSignature=false`,
  `diff.noprefix=false`, `diff.mnemonicPrefix=false`) plus the `--no-ext-diff`
  flag on every command that produces a patch.

Refusal is a PAYLOAD, never an exception: `{"ok": false, "reason", "message"}`.
The view renders a calm empty state from it — a non-repo, a missing path, a
mount-backed target or a missing git binary is an ordinary situation, not a
traceback overlay.

And the refusals are this module's OWN, not the gate's (MD-11 / GT-4): the
`condition.py` beside this file keeps the mode from being OFFERED on a
mount-backed or non-repo path, but a hand-written `?_mode=git` URL bypasses the
switcher entirely — so the mount check and the repo check are repeated here,
where they are a guarantee rather than a nicety.
"""
import os
import re
import subprocess
import sys
import threading

# Under the fused local execution backend a script is exec'd with its own
# directory first on sys.path but no __file__; rebuild it from there so the
# `../shared` hop below works in both hosts (the markdown/graph.py pattern).
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "log.py")

_HERE = os.path.dirname(os.path.abspath(__file__))
# `../shared/appenv.py` is how a template asks the app about its environment
# (SPEC PY-15): env vars only, stdlib only, no `fused_render` import. The import
# stays LAZY at its use site so an unreachable appenv is still the "cannot tell"
# case that must read as a refusal.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "shared"))


# ------------------------------------------------------------------- the bounds

# Wall clock for one git invocation. Generous next to any local plumbing command
# and still well inside the 30s `fused.runPython` ceiling, so a stalled call
# surfaces as this module's own empty state rather than the runtime's timeout.
TIMEOUT_S = 12.0

# A diff is shown, not stored: past a few thousand lines nobody reads it and the
# browser starts paying for the DOM. Both caps apply; whichever hits first wins,
# and the payload says which.
MAX_DIFF_BYTES = 400_000
MAX_DIFF_LINES = 3_000

# The `pending` diff has its OWN, much smaller budget, because its consumer is
# not a reader but a PROMPT (GT-18). A diff that is merely long for a human is
# ruinous for a model: it is billed per token, the summary quality falls off long
# before the cap is reached, and the whole point of the feature is a one-line
# subject. So the AI sees ~80 KB where the pane shows 400 KB, and the truncation
# is reported so the prompt can say the diff was cut rather than let the model
# describe a change it only half saw.
MAX_PROMPT_DIFF_BYTES = 80_000
MAX_PROMPT_DIFF_LINES = 1_500
MAX_PROMPT_FILES = 100

# The `conflicts` read has its own pair of budgets, and they are smaller again
# than the `pending` ones for a different reason: a conflicted file is sent WHOLE
# (the markers only mean something in their surroundings), so the bound is on how
# much whole-file text one prompt may carry, not on how much of a patch. Ten
# files is already an unusual merge; a repository-wide conflict of hundreds is a
# situation to hand back to the command line rather than to a model, and the
# payload says so instead of quietly sending the first few as if they were all.
MAX_CONFLICT_FILES = 10
MAX_CONFLICT_BYTES = 60_000

# The three lines git writes into a conflicted file. Recognised here so a binary
# or marker-less unmerged file can be reported as such, and mirrored in ops.py,
# which refuses to APPLY content that still contains them.
CONFLICT_MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")

# `git status` is unbounded in principle (a build tree can hold 100k untracked
# files). Bounded on the way in — bytes off the pipe — and again on the way out.
MAX_STATUS_BYTES = 4_000_000
MAX_CHANGES = 500

# One page of log, and the ceiling on how many commits the view will hold for a
# single path. The ceiling does two jobs that used to be conflated: it stops a
# hand-edited `limit=1e9` from being unbounded, AND it is a real product limit
# the UI has to be able to STATE ("showing the most recent N"), because the page
# grows its window rather than paging — see `_log`'s `capped`. It is generous
# enough that reaching it is a deliberate act: 500 rows is ~100 KB of payload and
# 17 clicks of "load more".
DEFAULT_LOG_LIMIT = 30
MAX_LOG_LIMIT = 500

# The SCM surface's own bounds (GT-12). A branch list, a stash list and the
# out-of-scope staged list are all "however many the user has", i.e. unbounded in
# principle — a long-lived monorepo checkout can carry thousands of local
# branches, and `git stash` has no ceiling of its own. Each is asked for with a
# count limit + 1, so "there were more" is observed rather than guessed.
MAX_BRANCHES = 300
MAX_STASHES = 100

# How many out-of-scope staged PATHS the overview names. The count is separate
# and stays a true total: the view's warning has to be able to say "17 staged
# changes outside this scope" honestly while listing only the first few, because
# the number is the part that decides whether you look.
MAX_STAGED_OUTSIDE = 20

# A hex object name, full or abbreviated. Anything else never becomes an argv
# entry — this is what keeps an option-shaped `sha` out of the command line.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

# Non-interactivity, as environment (see the module docstring for why the user's
# config is NOT disabled here).
_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "Never",
    "GIT_LFS_SKIP_SMUDGE": "1",
}

# Config overrides that keep the output parseable regardless of what the user's
# own config says. `-c` beats every config file, and only these knobs are
# touched — everything else (safe.directory, includeIf, mailmap) still applies.
_CONFIG = (
    "-c", "core.quotepath=false",       # raw bytes in paths, not \NNN escapes
    "-c", "color.ui=false",             # never ANSI, even with color.ui=always
    "-c", "log.showSignature=false",    # a GPG block would sit inside %s's line
    "-c", "diff.noprefix=false",        # a/ and b/ prefixes stay predictable
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.renames=true",
)

# Never hand the diff to the user's external diff helper — it would replace
# git's patch output with whatever that program prints (or, when `diff.external`
# is set to an empty string, kill the command outright). This is the FLAG and not
# a `-c diff.external=` override for exactly that reason: clearing the config
# value tells git to run the empty program, which is an error, whereas the flag
# tells it to run none.
_NO_EXT_DIFF = "--no-ext-diff"

# `%x00`-delimited fields, one commit per line. Every field is single-line by
# construction (%s is the subject's first line only), so the newline is an
# unambiguous record separator and no second delimiter is needed.
_LOG_FORMAT = "%H%x00%h%x00%an%x00%aI%x00%ar%x00%s"
_LOG_FIELDS = ("sha", "short", "author", "date", "relative", "subject")

# `for-each-ref`'s machine format for the branch list. Every field is single-line
# by construction, so — exactly as with the log format — the newline is an
# unambiguous record separator and `%x00` separates the fields.
#
# This is asked of `for-each-ref` and NEVER of `git branch`, whose output is a
# human format: column-aligned, colourable (`color.branch=always` survives even
# `color.ui=false` because it is a different key), and marking the current branch
# with a leading `* ` that a branch name could itself contain. `for-each-ref`
# exists precisely so scripts do not have to guess at any of that.
#
# The separator is written `%00` and not `%x00`: `for-each-ref`'s format language
# is NOT `git log`'s. It spells a literal byte as `%<hex>`, and leaves `%x00`
# alone as the four characters "%x00" — which parses as a field count that is
# never right, i.e. an empty branch list rather than an error.
_REF_FORMAT = "%(refname:short)%00%(upstream:short)%00%(upstream:track)%00%(HEAD)"

# `%(upstream:track)` is git's own human-ish "[ahead 2, behind 1]" / "[gone]" /
# "" summary. It is the ONE field here that is not a bare value, so the two
# numbers are pulled out with a regex rather than by splitting on punctuation —
# and a missing group means zero, which is what git omitting the word means.
_TRACK_AHEAD = re.compile(r"ahead (\d+)")
_TRACK_BEHIND = re.compile(r"behind (\d+)")

# `git stash list` is a reflog walk, so its subject (`%gs`) is git's own
# "WIP on <branch>: <sha> <subject>" or "On <branch>: <message>". The branch is
# worth surfacing (a stash applied onto the wrong branch is a bad afternoon), and
# it is the only part of that string with a stable shape, so it is the only part
# parsed — the remainder is passed through as the message rather than re-derived.
#
# `%H` first, and it is not decoration: a stash's positional index is the ONLY
# thing `stash@{n}` means, and the position shifts under a `stash push` from
# anywhere — this view, another tab, a terminal. So the entry carries its commit
# id, and a destructive op quotes it back (ops.py `_stash_at`) to prove it is
# dropping the entry the user actually looked at.
_STASH_FORMAT = "%H%x00%gs%x00%cr"
_STASH_SUBJECT = re.compile(r"^(?:WIP on|On) ([^:]+): (.*)$", re.DOTALL)

# A rename with only ONE side inside the open scope is a MOVE relative to that
# scope, and the direction is the whole point of showing it.
_MOVE_LABELS = {"in": "Moved into this scope", "out": "Moved out of this scope"}

_STATUS_LABELS = {
    "M": "Modified", "A": "Added", "D": "Deleted", "R": "Renamed",
    "C": "Copied", "U": "Unmerged", "T": "Type changed", "?": "Untracked",
    "!": "Ignored", " ": "",
}


class _Refused(Exception):
    """A situation the view renders as an empty state. Carries its own payload."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.payload = {"ok": False, "reason": reason, "message": message}


# ------------------------------------------------------------------ invocation



# ---------------------------------------------------------------- how git is run
#
# argv[0] is an ABSOLUTE path, and that is load-bearing rather than tidy. With
# libproj resident in the host process a plain fork() runs PROJ's pthread_atfork
# child handler into a SIGSEGV before exec, so the child dies with signal 11 and
# empty output and NO exception — every git answer becomes a silent negative.
# CPython avoids fork only when EVERY clause holds
# (`subprocess.py::_execute_child`): `os.path.dirname(executable)` truthy,
# `close_fds` false, `cwd is None`, no preexec_fn/pass_fds/start_new_session.
# `close_fds=False` alone is NOT enough, which is what the previous version of
# this comment got wrong: a bare "git" has dirname "" and forks regardless, and
# so does any call that passes `cwd=`. All three parts together, or none of them
# work. `-C <root>` is what replaces `cwd=`.
_GIT_BIN = None


def _git_bin():
    """An absolute path to git, resolved once. Bare name as a last resort so a
    PATH-less environment still raises the FileNotFoundError callers expect."""
    global _GIT_BIN
    if _GIT_BIN is None:
        import shutil
        _GIT_BIN = shutil.which("git") or "git"
    return _GIT_BIN


def _argv(root, args):
    return [_git_bin(), "--no-pager", *_CONFIG, "-C", root, *args]


def _popen_kwargs():
    return {
        "env": {**os.environ, **_ENV},
        "stdin": subprocess.DEVNULL,
        # See the note above _argv: required to reach posix_spawn, and only in
        # combination with the absolute argv[0] and no `cwd=`.
        "close_fds": False,
        "creationflags": (subprocess.CREATE_NO_WINDOW
                          if sys.platform == "win32" else 0),
    }


def _git(root, *args, allow=(0,)):
    """One bounded git call; returns its stdout bytes.

    `allow` is the set of exit codes that are ANSWERS rather than failures
    (`rev-parse --verify` exits 1 for "no such ref", `diff` exits 1 for
    "there were differences"). Anything else — including a missing binary — is a
    refusal, so the caller never has to branch on a raw CalledProcessError.
    """
    try:
        proc = subprocess.run(
            _argv(root, args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_S,
            **_popen_kwargs(),
        )
    except FileNotFoundError as exc:
        raise _Refused("no-git", "git is not installed, or not on this app's PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise _Refused("timeout", f"git took longer than {TIMEOUT_S:.0f}s to answer.") from exc
    except OSError as exc:
        raise _Refused("no-git", f"git could not be started: {exc}") from exc
    if proc.returncode not in allow:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise _Refused("git-failed", detail[0] if detail else
                       f"git exited {proc.returncode}.")
    return proc.stdout


def _git_stream(root, args, cap_bytes, allow=(0, 1)):
    """Stream a git command's stdout, stopping at `cap_bytes`.

    Streamed rather than captured because a byte cap applied to
    `subprocess.run`'s result is not a bound at all — the whole output is in
    memory by the time the slice happens, so the cap only trims what gets
    PARSED. Reading with a cap and killing the process is what makes it a real
    memory bound. A watchdog timer does the killing, so a git that stops writing
    mid-stream cannot park a blocking read forever (`timeout=` does not exist on
    a manual read loop).

    Returns `(raw_bytes, truncated)`. The caller owns record framing: a cap fires
    at an arbitrary byte, so whatever the last record was, it is a FRAGMENT and
    the caller must discard it (`_git_capped` drops the partial line, `_status`
    drops everything after the last NUL).
    """
    try:
        proc = subprocess.Popen(
            _argv(root, args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            **_popen_kwargs())
    except FileNotFoundError as exc:
        raise _Refused("no-git", "git is not installed, or not on this app's PATH.") from exc
    except OSError as exc:
        raise _Refused("no-git", f"git could not be started: {exc}") from exc

    killer = threading.Timer(TIMEOUT_S, proc.kill)
    killer.daemon = True
    killer.start()
    chunks, total, truncated = [], 0, False
    try:
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            if total + len(chunk) > cap_bytes:
                chunks.append(chunk[: cap_bytes - total])
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        killer.cancel()
        try:
            proc.stdout.close()
        except OSError:
            pass
        if truncated:
            proc.kill()
        try:
            code = proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = None

    # A cap that fired means we closed the pipe under git, so its exit status is
    # about the broken pipe and not about the output — only judge it when we read
    # the stream to its end.
    if not truncated and code is not None and code not in allow:
        raise _Refused("git-failed", f"git exited {code}.")
    return b"".join(chunks), truncated


def _git_capped(root, *args, cap_bytes, cap_lines, allow=(0, 1)):
    """A streamed git command as text, capped in bytes AND in lines.

    Returns `(text, truncated, shown_lines)`.
    """
    raw, truncated = _git_stream(root, args, cap_bytes, allow)
    text = raw.decode("utf-8", "replace")
    lines = text.split("\n")
    if truncated and lines:
        lines.pop()  # a byte cap almost certainly cut mid-line; drop the fragment
    if len(lines) > cap_lines:
        lines = lines[:cap_lines]
        truncated = True
    return "\n".join(lines), truncated, len(lines)


def _trim(diff, shown):
    """Strip a patch's framing blank lines and re-derive its line count.

    `_git_capped` counts what came off the pipe, which for a diff that touched
    nothing at all is a single empty line — so an "empty" patch would otherwise
    report one shown line and the UI's truncation arithmetic would be off by one.
    """
    diff = diff.strip("\n")
    if not diff:
        return "", 0
    return diff, min(shown, diff.count("\n") + 1)


# ----------------------------------------------------------------- the location


def _refuse_mounts(path):
    """Refuse a mount-backed target outright (GT-4 / MD-11).

    The detector is `shared/appenv.is_mount_backed`, the app's own rule answered
    from `FUSED_RENDER_*` rather than by importing fused_render — this module
    runs as a child process whose PYTHONPATH is stripped, so an import of the
    package would take its except branch on every run. An ImportError therefore
    means we cannot tell, and "cannot tell" reads as "refuse": running git across
    an rclone-NFS mount is the failure this exists to prevent.
    """
    try:
        from appenv import is_mount_backed
    except Exception as exc:  # noqa: BLE001 — cannot tell -> refuse
        raise _Refused("mount", f"Mount detection unavailable ({exc}); "
                                "refusing to run git here.") from exc
    if is_mount_backed(path):
        raise _Refused(
            "mount",
            "Git history is not available on remote mounts — git would have to "
            "walk the mounted tree. Opening the file itself still works.")


def _locate(file):
    """Resolve `(root, rel, is_dir)` for the open path, or refuse.

    `rel` is POSIX and relative to the work-tree root — "" for the root itself.
    Normalized once, here, because every pathspec below and the header's scope
    label are all built from it.
    """
    if not file:
        raise _Refused("missing", "No file or folder was given.")
    _refuse_mounts(file)
    path = os.path.abspath(file)
    if not os.path.exists(path):
        raise _Refused("missing", f"{path} does not exist.")
    is_dir = os.path.isdir(path)
    cwd = path if is_dir else os.path.dirname(path)

    # `-C cwd` cannot be used before the root is known, so this one call is the
    # bootstrap: it is the only invocation pinned to the target rather than the
    # root. `--show-toplevel` is empty for a bare repo and inside `.git`, which
    # is the same "no work tree to scope to" refusal as a non-repo.
    top = _git(cwd, "rev-parse", "--show-toplevel", allow=(0, 128))
    root = top.decode("utf-8", "replace").strip()
    if not root:
        raise _Refused("not-a-repo", f"{path} is not inside a git repository.")
    root = os.path.realpath(root)

    # realpath both sides: a repo reached through a symlink (or /var -> /private/var
    # on macOS) otherwise fails relpath's containment check for no real reason.
    real = os.path.realpath(path)
    rel = "" if real == root else os.path.relpath(real, root).replace(os.sep, "/")
    if rel.startswith("../"):
        raise _Refused("not-a-repo", f"{path} is not inside {root}.")
    return root, rel, is_dir


def _pathspec(rel):
    """The `--`-guarded pathspec list for a scope, or `[]` for the whole repo.

    `:(literal)` disables pathspec magic and globbing, so a filename holding
    `*`, `?`, `[` — or a leading `:`, which would otherwise BE magic — matches
    itself. Directory-prefix matching still applies, which is what scoping to a
    folder needs.
    """
    return ["--", ":(literal)" + rel] if rel else ["--"]


# ------------------------------------------------------------------ the answers


def _head(root):
    """`(branch, detached, short_sha, has_commits)`.

    `symbolic-ref` answers the branch even on an UNBORN one (a fresh `git init`
    has a named branch and no commits), which is why it is asked instead of
    reading `HEAD` through `rev-parse --abbrev-ref` — that returns the literal
    string "HEAD" for a detached head and cannot tell it apart from a ref named
    HEAD. Exit 1 from `symbolic-ref` IS the detached answer.
    """
    ref = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allow=(0, 1))
    branch = ref.decode("utf-8", "replace").strip() or None
    short = _git(root, "rev-parse", "--short", "--verify", "--quiet", "HEAD",
                 allow=(0, 1)).decode("utf-8", "replace").strip() or None
    return branch, branch is None, short, short is not None


def _status(root, rel, is_dir):
    """The uncommitted entries under the scope, plus repo-wide dirtiness.

    ONE unscoped `git status`, filtered to the scope in Python — not one scoped
    call plus another unscoped one. The header's clean/dirty light describes the
    REPOSITORY (that is what the word means) while the list describes the scope,
    and both facts come out of the same walk of the index this way.

    `--porcelain=v1 -z` is the stable machine format: `XY <path>NUL`, and for a
    rename or copy `XY <to>NUL<from>NUL` — the `-z` form reverses the arrow'd
    order, so the NEW path comes first.

    STREAMED under a byte cap, and the cap's fragment is discarded before
    parsing. Both halves matter and the first version of this got both wrong by
    slicing `subprocess.run`'s captured output: the slice bounded nothing (the
    whole output was already in memory) and it cut at an arbitrary byte, so the
    last entry showed a TRUNCATED PATH — a wrong path in the UI, and a row that
    fails when clicked. Worse, a cut landing inside a rename's `<to>` record
    shifted the `<from>` pairing by one for everything after it. So: cut back to
    the last NUL (only whole NUL-terminated fields survive), and drop a trailing
    rename whose `<from>` did not make it. Returns
    `(entries, truncated, dirty, staged_outside)`, where `truncated` covers BOTH
    caps.

    `staged_outside` is collected HERE rather than by a second `git status`,
    because this walk already sees every entry in the repository and throws the
    out-of-scope ones away — the information is in hand, and asking git for the
    same walk twice would double the one call GT-7 measured. It is what makes
    GT-14's honesty possible: `git commit` is index-based, so a commit triggered
    from a scoped view carries staged work the view never listed, and the only
    alternative to reporting it is to surprise the user with it.
    """
    raw, byte_capped = _git_stream(
        root, ("status", "--porcelain=v1", "-z",
               "--untracked-files=normal", "--ignored=no"),
        MAX_STATUS_BYTES, allow=(0,))
    if byte_capped:
        # Keep only complete NUL-terminated fields. A whole stream ends with NUL,
        # so this is a no-op on untruncated output and drops exactly the fragment.
        raw = raw[: raw.rfind(b"\0") + 1]
    fields = raw.split(b"\0")
    entries, dirty, dangling = [], False, False
    outside_count, outside_paths = 0, []
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if len(record) < 4:
            continue
        code = record[:2].decode("utf-8", "replace")
        path = record[3:].decode("utf-8", "replace")
        orig = None
        if code[0] in ("R", "C"):
            if i >= len(fields) - 1:
                # The paired `<from>` field is missing: the byte cap landed
                # between the two halves of one rename. Emitting the entry would
                # show a rename with no source; dropping it keeps every entry
                # that IS shown fully correct.
                dangling = True
                break
            orig = fields[i].decode("utf-8", "replace")
            i += 1
        dirty = True
        # BOTH sides of a rename/copy are scope-tested. Testing only the new path
        # dropped a file MOVED OUT of the open folder from the list entirely —
        # and "it left this folder" is exactly the kind of change this view is for.
        # The two directions are different facts, so which side matched is
        # reported rather than flattened into "renamed".
        in_new = _in_scope(path, rel, is_dir)
        in_old = orig is not None and _in_scope(orig, rel, is_dir)
        if not (in_new or in_old):
            # Out of scope — but if it is STAGED it is still going into the next
            # commit (GT-14), so it is counted before it is dropped. The test is
            # git's own index column `X`: " " means nothing staged, "?" is the
            # untracked marker, and an untracked file is not in the index at all,
            # so neither can be committed by accident. Only the total is
            # unbounded, so only the path LIST is capped.
            if code != "??" and code[0] not in (" ", "?"):
                outside_count += 1
                if len(outside_paths) < MAX_STAGED_OUTSIDE:
                    outside_paths.append(path)
            continue
        moved = None
        if orig is not None and not (in_new and in_old):
            moved = "in" if in_new else "out"
        x, y = code[0], code[1]
        untracked = code == "??"
        entries.append({
            "status": code,
            "x": x,
            "y": y,
            "path": path,
            "orig": orig,
            "moved": moved,
            # Whether this entry is UNMERGED, answered from git's own porcelain
            # rule rather than left for the view to re-derive: any code with a
            # `U` on either side, plus `AA` and `DD` (both-added / both-deleted
            # carry no U at all). A mirror of this in the view would be a second
            # copy of a rule with seven cases and no way to notice it drifting —
            # and getting it wrong means offering to resolve a file that is not
            # conflicted, or hiding the button on one that is.
            "conflicted": "U" in (x, y) or code in ("AA", "DD"),
            "staged": not untracked and x not in (" ", "?"),
            "unstaged": not untracked and y not in (" ", "?"),
            "untracked": untracked,
            "label": (_MOVE_LABELS[moved] if moved else
                      _STATUS_LABELS.get("?" if untracked else (x if x != " " else y), "")),
        })
    truncated = byte_capped or dangling or len(entries) > MAX_CHANGES
    # A byte cap means the repo-wide dirty verdict is `True` regardless of what
    # survived parsing — git had more to say than we were willing to read.
    return (entries[:MAX_CHANGES], truncated, dirty or byte_capped,
            {"count": outside_count, "paths": outside_paths})


def _in_scope(path, rel, is_dir):
    """Whether a status path falls under the open scope.

    Mirrors what `git status -- <pathspec>` would have answered, including the
    one case a naive prefix test gets wrong: with `--untracked-files=normal` git
    COLLAPSES a wholly-untracked directory to `dir/`, so an entry can be an
    ANCESTOR of the scope rather than a descendant of it, and the scope is still
    dirty because of it.
    """
    if not rel:
        return True
    if path == rel or path.rstrip("/") == rel:
        return True
    if is_dir and path.startswith(rel + "/"):
        return True
    return path.endswith("/") and (rel + "/").startswith(path)


def _remotes(root):
    """The configured remote NAMES, bounded.

    `git remote` with no arguments prints one bare name per line — it is the
    plumbing-ish form (no URLs, no `-v` columns), so there is nothing here to
    mis-parse. Bounded anyway: a name list is user data, and nothing in this
    module returns an unbounded list.
    """
    raw = _git(root, "remote").decode("utf-8", "replace")
    return [line.strip() for line in raw.split("\n") if line.strip()][:MAX_BRANCHES]


def _track(track):
    """`(ahead, behind)` out of git's `%(upstream:track)` string.

    "" (in sync, or no upstream), "[ahead 3]", "[behind 2]", "[ahead 3, behind 2]"
    and "[gone]" — the last meaning the upstream ref was deleted, which yields
    (0, 0) here and is visible to the caller as an upstream that no longer
    resolves. A missing word means zero, which is exactly what git omitting it
    says.
    """
    ahead = _TRACK_AHEAD.search(track or "")
    behind = _TRACK_BEHIND.search(track or "")
    return (int(ahead.group(1)) if ahead else 0,
            int(behind.group(1)) if behind else 0)


def _branches(root):
    """The LOCAL branches, each with its recorded upstream and divergence.

    Local only (`refs/heads/`): a remote-tracking ref is not something this view
    can check out without inventing a local branch for it, and listing hundreds
    of `origin/*` entries beside a handful of real branches is how the list stops
    being scannable.

    **Nothing here fetches.** `%(upstream:track)` is computed from the objects
    already in the repository, i.e. from what the last fetch recorded — so the
    numbers can be stale, and that is the correct trade: a read that silently
    contacted a network to keep a header honest would be a far bigger surprise
    than a stale count next to an explicit ⟳ button.

    Sorted newest-committed-first with the ref name as tie-break, because the
    count limit has to cut SOMEWHERE and the branches you touched recently are
    the ones you are looking for. (`for-each-ref` takes the keys in reverse
    precedence: the LAST `--sort` is the primary one.)
    """
    raw = _git(root, "for-each-ref", f"--format={_REF_FORMAT}",
               f"--count={MAX_BRANCHES + 1}",
               "--sort=refname", "--sort=-committerdate", "refs/heads/")
    records = [line for line in raw.decode("utf-8", "replace").split("\n") if line]
    truncated = len(records) > MAX_BRANCHES
    branches = []
    for line in records[:MAX_BRANCHES]:
        parts = line.split("\0")
        if len(parts) != 4:
            continue  # a record we cannot trust is dropped, never half-read
        name, upstream, track, head = parts
        ahead, behind = _track(track)
        branches.append({
            "name": name,
            # `%(HEAD)` is "*" for the checked-out branch and " " otherwise. In a
            # detached worktree no branch is marked, which is the honest answer.
            "current": head == "*",
            "upstream": upstream or None,
            "ahead": ahead,
            "behind": behind,
        })
    return branches, truncated


def _stashes(root):
    """The stash entries, newest first, bounded.

    `git stash list` is `git log` over the stash reflog, so it takes log options
    — `--max-count` bounds it, and `--format` gives it the same `%x00`-delimited
    machine shape as everything else here.

    The INDEX is positional rather than parsed out of `%gd`: `stash@{n}` is
    defined as the nth entry of this very list, so enumerating it is not an
    approximation of the truth, it IS the truth — and it cannot be thrown off by
    a reflog selector git formats differently than we expect.
    """
    raw = _git(root, "stash", "list", "--no-color", f"--format={_STASH_FORMAT}",
               f"--max-count={MAX_STASHES + 1}")
    records = [line for line in raw.decode("utf-8", "replace").split("\n") if line]
    truncated = len(records) > MAX_STASHES
    stashes = []
    for index, line in enumerate(records[:MAX_STASHES]):
        parts = line.split("\0")
        if len(parts) != 3:
            continue
        sha, subject, relative = parts
        matched = _STASH_SUBJECT.match(subject)
        stashes.append({
            "index": index,
            "ref": f"stash@{{{index}}}",
            # The stable identity of this entry, independent of its position.
            "sha": sha,
            "message": matched.group(2) if matched else subject,
            "relative": relative,
            "branch": matched.group(1) if matched else None,
        })
    return stashes, truncated


def _upstream_state(root, has_commits):
    """`(upstream, ahead, behind, remote)` for the CURRENT branch.

    `@{upstream}` is a literal constant in the argv, never a user string, so the
    branch name never has to be quoted or validated to ask this question — which
    is also why it is asked this way rather than by interpolating the branch into
    `<branch>@{upstream}`.

    Exit 128 is the ordinary "no upstream configured" answer (and, on an unborn
    HEAD, "no HEAD"), not a failure — hence `allow`. As in `_branches`, the
    numbers come from the recorded upstream: no fetch, ever.

    `remote` degrades in three steps, because the view needs SOMETHING to push
    to: the upstream's own remote if there is one; otherwise the sole configured
    remote; otherwise `origin` if it exists among several. With more than one
    remote and no `origin` and no upstream there is no defensible guess, so the
    answer is None and the view offers no push rather than picking for the user.
    """
    remotes = _remotes(root)
    upstream = None
    ahead = behind = 0
    if has_commits:
        raw = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                   "@{upstream}", allow=(0, 128))
        upstream = raw.decode("utf-8", "replace").strip() or None
    if upstream:
        # `--left-right --count` prints "<ahead>\t<behind>" for `HEAD...upstream`:
        # commits reachable from one side only, in each direction. Three dots,
        # not two — two would give one total and lose the direction.
        counts = _git(root, "rev-list", "--left-right", "--count",
                      "HEAD...@{upstream}", allow=(0, 128))
        parts = counts.decode("utf-8", "replace").split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            ahead, behind = int(parts[0]), int(parts[1])

    remote = None
    if upstream:
        # Longest match rather than `split("/", 1)`: a remote name may itself
        # contain a slash, so the first slash is not reliably the boundary.
        for candidate in sorted(remotes, key=len, reverse=True):
            if upstream.startswith(candidate + "/"):
                remote = candidate
                break
    if remote is None:
        remote = (remotes[0] if len(remotes) == 1
                  else ("origin" if "origin" in remotes else None))
    return upstream, ahead, behind, remote


def _log(root, rel, limit, page):
    """One window of the scoped log.

    Returns `(commits, has_more, capped, limit, page)`. The last two facts are
    separate on purpose, and collapsing them is a bug in each direction:

    * **`has_more`** — "git had more records than we are returning". Counted off
      the records GIT emitted, NOT the ones we kept: counting kept records let one
      dropped malformed record on a full page make `len(commits) == limit`, so the
      UI said "End of history for this path" while more commits existed.
    * **`capped`** — "we refused to widen the window any further", i.e. the
      requested limit was reduced to MAX_LOG_LIMIT. The page grows its window
      (`limit = PAGE_SIZE * pages`) instead of paging, so once the clamp bites,
      `has_more` stays honestly True forever while every further request returns
      the identical rows — an endless "Load more" that never advances. The clamp
      has to be VISIBLE for the UI to stop offering it, so it is a field rather
      than a silent `min()`.

    Both signals are therefore needed: `has_more and not capped` is the only state
    in which asking for more is worth a click, and `has_more and capped` is what
    the UI renders as "showing the most recent N for this path".
    """
    wanted = max(1, int(limit or DEFAULT_LOG_LIMIT))
    limit = min(wanted, MAX_LOG_LIMIT)
    capped = wanted > limit
    page = max(0, int(page or 0))
    # limit + 1 is the has_more probe: one extra row proves another page exists
    # without a second count-everything call.
    raw = _git(root, "log", "--no-color", f"--format={_LOG_FORMAT}",
               f"--max-count={limit + 1}", f"--skip={page * limit}",
               *_pathspec(rel))
    records = [line for line in raw.decode("utf-8", "replace").split("\n") if line]
    has_more = len(records) > limit
    commits = []
    for line in records:
        parts = line.split("\0")
        if len(parts) != len(_LOG_FIELDS):
            continue  # a record we cannot trust is dropped, never half-read
        commits.append(dict(zip(_LOG_FIELDS, parts)))
    return commits[:limit], has_more, capped, limit, page


def _commit(root, rel, sha):
    meta_raw = _git(root, "show", "--no-patch", _NO_EXT_DIFF, f"--format={_LOG_FORMAT}", sha,
                    allow=(0, 128, 129))
    fields = meta_raw.decode("utf-8", "replace").strip().split("\0")
    if len(fields) != len(_LOG_FIELDS):
        raise _Refused("no-such-commit", f"No commit {sha} in this repository.")
    # `--first-parent` gives a merge commit the diff against the branch it was
    # merged into; without it `git show` prints nothing at all for a merge.
    diff, truncated, shown = _git_capped(
        root, "show", "--no-color", _NO_EXT_DIFF, "--format=", "--first-parent",
        "--find-renames", sha, *_pathspec(rel),
        cap_bytes=MAX_DIFF_BYTES, cap_lines=MAX_DIFF_LINES)
    diff, shown = _trim(diff, shown)
    return {
        "ok": True,
        "commit": dict(zip(_LOG_FIELDS, fields)),
        "scope": rel,
        "diff": diff,
        "empty": not diff,
        "truncated": truncated,
        "shown_lines": shown,
        "max_bytes": MAX_DIFF_BYTES,
        "max_lines": MAX_DIFF_LINES,
    }


def _check_op(op, sha, entry):
    """Validate the operation's own arguments BEFORE anything forks git.

    Ordering, not just validation: `_locate` is itself a git call, so validating
    afterwards would mean an option-shaped `sha` had already caused a subprocess
    (with the target's cwd) to run. Cheap string checks come first, always.
    """
    if op not in ("overview", "log", "commit", "worktree", "branches", "stashes",
                  "pending", "conflicts"):
        raise _Refused("bad-op", f"Unknown operation: {op}")
    if op == "commit" and not _SHA_RE.match(sha or ""):
        raise _Refused("bad-sha", "That is not a commit id.")
    if op == "worktree":
        rel = (entry or "").replace("\\", "/").strip("/")
        if not rel:
            raise _Refused("missing", "No change was selected.")
        # First pass, on the STRING and before any I/O: `..` is not a pathspec
        # error we want to surface as a raw git failure, and an absolute path
        # would silently resolve outside the repository. This is NOT the whole
        # containment check — see `_contain` for the half that needs the root.
        if os.path.isabs(entry) or any(part == ".." for part in rel.split("/")):
            raise _Refused("outside-repo", "That path is outside the repository.")


def _contain(root, rel, full):
    """Containment for EVERY working-tree entry: the realpath must be under root.

    `_check_op`'s string test is necessary and not sufficient — it proves the
    entry NAMES nothing outside the repository, but the consumers of that name
    resolve it, so a link is how a repo-relative-looking name reaches outside.
    This also covers the case an `islink` test on the final component misses
    entirely: an ordinary file reached through a symlinked PARENT directory.

    Deliberately kept uniform across the tracked and untracked branches rather
    than pushed down into the one that reads bytes. One containment rule for the
    whole op is easier to reason about than a rule that varies by trackedness —
    and trackedness is something we learn from a git call, i.e. later than this.

    Known residual, stated rather than hidden: a TRACKED symlink whose target is
    outside the repo is refused here even though its branch (`git diff HEAD --`)
    would never read through it. That is the conservative side of the trade, and
    it costs a diff nobody can see rather than showing bytes from outside.
    """
    real_root = os.path.realpath(root)
    real = os.path.realpath(full)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise _Refused("outside-repo",
                       f"{rel} resolves outside the repository.")


def _refuse_untracked_link(rel, full):
    """Refuse a symlink on the UNTRACKED branches only.

    Placement is the whole point, and getting it wrong is what this function
    exists to fix: the check used to sit in `_contain`, which runs before the
    tracked/untracked split, so it refused every symlink row — including a
    TRACKED, modified symlink whose branch is perfectly safe. `git diff HEAD --
    <rel>` treats a symlink as a symlink: it diffs the link's target *path text*
    and never reads through it, so a tracked link must diff normally.

    An untracked link has no such branch. `git diff --no-index -- /dev/null
    <rel>` follows it and renders the TARGET's bytes under the link's name, which
    is a different file than the row claims — true whether the target is outside
    the repository (already refused by `_contain`) or inside it. And an untracked
    symlink to a DIRECTORY would slip into the `_untracked_dir` listing branch by
    way of `os.path.isdir` following it, so this guards both untracked branches
    rather than only the `--no-index` call.
    """
    if os.path.islink(full):
        raise _Refused("symlink",
                       f"{rel} is a symbolic link. Open its target directly — "
                       "this view will not read through a link.")


def _untracked_dir(root, rel):
    """The untracked files inside a collapsed `dir/` status entry.

    `--untracked-files=normal` reports a wholly-untracked directory as a single
    `dir/` row, and a directory has no diff: `git diff HEAD -- dir/` is empty, so
    the row used to open a blank pane wearing the commit-oriented copy ("nothing
    in this commit touched the path…") — wrong twice over, since it is not a
    commit and the path IS in scope. A directory's honest answer is what is
    inside it, so that is what this returns, and each entry is clickable through
    to its own whole-file diff.

    GIT does the enumeration, not this module: `ls-files --others
    --exclude-standard` is the canonical "what would `git add` pick up here",
    which means the answer already honours .gitignore and nested excludes instead
    of reimplementing them. It is read through `_git_stream` under the same byte
    cap as `git status`, and capped again by entry count, so a 100k-file untracked
    tree is bounded twice on the way in — the reason it is NOT `os.walk`, which
    would be an unbounded recursion inside a template (the discipline the gate
    documents and the reader keeps).
    """
    raw, byte_capped = _git_stream(
        root, ("ls-files", "--others", "--exclude-standard", "-z",
               *_pathspec(rel)),
        MAX_STATUS_BYTES, allow=(0,))
    if byte_capped:
        raw = raw[: raw.rfind(b"\0") + 1]   # whole NUL-terminated fields only
    names = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\0") if chunk]
    return names[:MAX_CHANGES], byte_capped or len(names) > MAX_CHANGES


def _worktree(root, entry, has_commits):
    rel = (entry or "").replace("\\", "/").strip("/")
    full = os.path.join(root, *rel.split("/"))
    _contain(root, rel, full)
    tracked = _git(root, "ls-files", "--error-unmatch", "-z", *_pathspec(rel),
                   allow=(0, 1, 128)).strip(b"\0")
    untracked = not tracked
    if untracked:
        _refuse_untracked_link(rel, full)
    # One stat decides directory-ness. The status row's trailing "/" would say the
    # same thing, but `_check_op` strips it (deliberately — it is a path, and a
    # trailing slash must not change containment), so the fact is re-derived here
    # rather than smuggled through the param.
    if untracked and os.path.isdir(full):
        files, truncated = _untracked_dir(root, rel)
        return {
            "ok": True,
            "kind": "untracked-dir",
            "target": rel,
            "untracked": True,
            "files": files,
            "truncated": truncated,
            "max_files": MAX_CHANGES,
        }
    if untracked and os.path.isfile(full):
        # An untracked file has nothing in the index to diff against, so the
        # whole file IS the change. `--no-index` is git's own way to say that,
        # and it exits 1 for "there were differences" — an answer, not a failure.
        #
        # The RELATIVE path is passed even though the file was just located
        # absolutely: `--no-index` echoes its arguments into the `a/…` / `b/…`
        # header verbatim, so an absolute path would print the reader's whole
        # filesystem layout above every untracked diff. `-C root` already put
        # git in the work tree, so the relative form resolves identically.
        diff, truncated, shown = _git_capped(
            root, "diff", "--no-color", _NO_EXT_DIFF, "--no-index", "--", os.devnull, rel,
            cap_bytes=MAX_DIFF_BYTES, cap_lines=MAX_DIFF_LINES)
    else:
        # vs HEAD, not vs the index: a staged-but-uncommitted change is part of
        # "what is different from the last commit", which is what the row means.
        # Before the first commit there is no HEAD, so the index is the baseline.
        base = ["HEAD"] if has_commits else []
        diff, truncated, shown = _git_capped(
            root, "diff", "--no-color", _NO_EXT_DIFF, "--find-renames", *base, *_pathspec(rel),
            cap_bytes=MAX_DIFF_BYTES, cap_lines=MAX_DIFF_LINES)
    diff, shown = _trim(diff, shown)
    return {
        "ok": True,
        "kind": "diff",
        "target": rel,
        "untracked": untracked,
        "diff": diff,
        "empty": not diff,
        "truncated": truncated,
        "shown_lines": shown,
        "max_bytes": MAX_DIFF_BYTES,
        "max_lines": MAX_DIFF_LINES,
    }


def _name_list(root, args):
    """A `-z` name list off a git command, bounded on the way in and out.

    Same two-sided bound as `_untracked_dir`: streamed under the status byte cap
    (a repo-wide `--name-only` is "however many files the user touched", i.e.
    unbounded in principle), the cap's fragment cut back to the last whole
    NUL-terminated field, and capped again by entry count.
    """
    raw, byte_capped = _git_stream(root, args, MAX_STATUS_BYTES, allow=(0, 1))
    if byte_capped:
        raw = raw[: raw.rfind(b"\0") + 1]
    names = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\0") if chunk]
    return names[:MAX_PROMPT_FILES], byte_capped or len(names) > MAX_PROMPT_FILES


def _pending(root, rel, branch):
    """What a commit made right now would record — as prompt material (GT-18).

    A READ, and it belongs here rather than in `ops.py`: it forks `git diff` and
    nothing else, changes no ref, no index and no file. The write module's
    confirmation-and-refusal machinery is for operations that can lose work, and
    putting a read behind it would say this one can.

    Two shapes, mirroring what the button means in each state — and the choice is
    made from what git says is staged, not from what the view happens to be
    listing:

    * **staged** (the normal case) — `git diff --cached`, deliberately
      **UNSCOPED**. `git commit` records the INDEX, all of it, wherever it lives
      (GT-14), so a message written from a diff scoped to the open folder would
      describe less than the commit is about to make. The scope is a reading lens
      on this view; it is not a lens on the commit.
    * **worktree** — nothing is staged at all, so there is no commit to describe
      yet and the honest fallback is what the panel is showing: the uncommitted
      changes under the open scope, untracked names included. They are named
      rather than diffed because an untracked file has no `git diff` at all
      (`_worktree` reaches for `--no-index` per file, which is a fan-out this one
      bounded call will not do) — a name list is enough for "this adds X".

    `empty` is the state the view needs to say "there is nothing to describe":
    a message cannot be written from no change, and it is a first-class answer
    rather than an error, exactly like every other awkward state here (GT-9).
    """
    files, files_truncated = _name_list(
        root, ("diff", "--cached", "--name-only", "-z", *_pathspec("")))
    if files:
        kind = "staged"
        diff, truncated, shown = _git_capped(
            root, "diff", "--cached", "--no-color", _NO_EXT_DIFF, "--find-renames",
            *_pathspec(""),
            cap_bytes=MAX_PROMPT_DIFF_BYTES, cap_lines=MAX_PROMPT_DIFF_LINES)
    else:
        kind = "worktree"
        files, files_truncated = _name_list(
            root, ("diff", "--name-only", "-z", *_pathspec(rel)))
        untracked, untracked_truncated = _untracked_dir(root, rel)
        files_truncated = files_truncated or untracked_truncated
        files = (files + untracked)[:MAX_PROMPT_FILES]
        diff, truncated, shown = _git_capped(
            root, "diff", "--no-color", _NO_EXT_DIFF, "--find-renames",
            *_pathspec(rel),
            cap_bytes=MAX_PROMPT_DIFF_BYTES, cap_lines=MAX_PROMPT_DIFF_LINES)
    diff, shown = _trim(diff, shown)
    return {
        "ok": True,
        "kind": kind,
        "scope": rel,
        "branch": branch,
        "diff": diff,
        "files": files,
        "files_truncated": files_truncated,
        "empty": not diff and not files,
        "truncated": truncated,
        "shown_lines": shown,
        "max_bytes": MAX_PROMPT_DIFF_BYTES,
        "max_lines": MAX_PROMPT_DIFF_LINES,
    }


def _operation_in_flight(root):
    """Which multi-step operation git is part-way through, and against what.

    Returns `(operation, theirs, summary)` — all three `None` when the tree is
    not mid-anything. Read from the ref/marker files in the git dir rather than
    parsed out of `git status`'s prose, because that prose is localised and
    reworded between releases while the marker names are plumbing.

    Only the operations that can leave an unmerged index are recognised, which is
    the same set that can produce the conflict this read exists to describe. A
    `None` operation with unmerged paths is a real state, not a bug: `git
    checkout --merge` and a conflicted `stash pop` both leave one with no
    operation to continue.
    """
    gitdir = _git(root, "rev-parse", "--absolute-git-dir",
                  allow=(0, 128)).decode("utf-8", "replace").strip()
    if not gitdir or not os.path.isdir(gitdir):
        return None, None, None

    def head_of(name):
        raw = _git(root, "rev-parse", "--short", name, allow=(0, 128, 1))
        sha = raw.decode("utf-8", "replace").strip()
        return sha or None

    def first_line(*parts):
        path = os.path.join(gitdir, *parts)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.readline(500).strip() or None
        except OSError:
            return None

    # A rebase is a directory, not a ref, and it is checked FIRST: a conflicted
    # rebase step also writes REBASE_HEAD, so asking about single refs first
    # would report the step's parent instead of the rebase.
    for sub in ("rebase-merge", "rebase-apply"):
        if os.path.isdir(os.path.join(gitdir, sub)):
            onto = first_line(sub, "head-name")
            if onto and onto.startswith("refs/heads/"):
                onto = onto[len("refs/heads/"):]
            return "rebase", onto, first_line(sub, "message")
    for marker, name in (("MERGE_HEAD", "merge"),
                         ("CHERRY_PICK_HEAD", "cherry-pick"),
                         ("REVERT_HEAD", "revert")):
        if os.path.exists(os.path.join(gitdir, marker)):
            return name, head_of(marker), first_line("MERGE_MSG")
    return None, None, None


def _conflict_body(root, rel, budget):
    """One unmerged file as text-with-markers, or a flag saying it is not text.

    The file is read from the WORKING TREE, which is where git wrote the markers —
    the index holds the three unmerged stages separately and none of them is the
    marked-up text a resolution has to replace.

    `budget` is the bytes left for this file across the whole payload, so a merge
    of one enormous file and a merge of many small ones share one ceiling.
    Truncated on a CHARACTER boundary after decoding (a byte slice can split a
    UTF-8 sequence) and reported, so the prompt can say the file was cut rather
    than let a model resolve a hunk whose closing marker it never saw.
    """
    full = os.path.join(root, *rel.split("/"))
    try:
        with open(full, "rb") as handle:
            raw = handle.read(budget + 1)
    except OSError:
        return "", False, True          # unreadable: named, not read
    truncated = len(raw) > budget
    if truncated:
        raw = raw[:budget]
    if b"\0" in raw:
        return "", True, False          # binary: named, never sent to a model
    text = raw.decode("utf-8", "replace")
    while truncated and text and len(text.encode("utf-8")) > budget:
        text = text[:-1]
    return text, False, truncated


def _conflicts(root, rel, branch):
    """The unmerged paths and their marker text — the model's context (GT-19).

    A READ, and it lives here for the same reason `_pending` does: it forks
    `git diff --diff-filter=U` and reads files, changes no ref, no index and
    nothing on disk. The write half is `ops.py`'s `resolve`, which is where the
    confirmation-and-refusal machinery belongs.

    Deliberately **UNSCOPED**, unlike almost every other read here. A conflict is
    a state of the whole repository and of the operation in flight: a merge is not
    half-finished for `pkg/` and finished elsewhere, so a view scoped to a folder
    that hid the conflicts outside it would describe a situation that does not
    exist. Each entry carries `in_scope` instead, which is what the view needs —
    it may SHOW every conflict and may only offer to write the ones `ops.py`
    would accept (GT-13).

    `empty` is the first-class "there is nothing to resolve" answer (GT-9), and it
    is about the CONFLICT, not about how many files were listed: a cap reached
    reports `files_truncated` with `empty` false, because "we showed you none of
    them" must never read as "there are none".
    """
    operation, theirs, summary = _operation_in_flight(root)
    names, name_capped = _name_list(
        root, ("diff", "--name-only", "--diff-filter=U", "-z", *_pathspec("")))
    shown = names[:MAX_CONFLICT_FILES]
    files = []
    budget = MAX_CONFLICT_BYTES
    for name in shown:
        content, binary, truncated = _conflict_body(root, name, max(budget, 0))
        budget -= len(content.encode("utf-8"))
        files.append({
            "path": name,
            "in_scope": (not rel) or name == rel or name.startswith(rel + "/"),
            "binary": binary,
            "content": content,
            "truncated": truncated,
        })
    return {
        "ok": True,
        "operation": operation,
        "theirs": theirs,
        "summary": summary,
        "branch": branch,
        "scope": rel,
        "files": files,
        "files_truncated": name_capped or len(names) > len(shown),
        "empty": not names,
        "max_files": MAX_CONFLICT_FILES,
        "max_bytes": MAX_CONFLICT_BYTES,
    }


def main(
    file: str,
    op: str = "overview",
    limit: int = 30,
    page: int = 0,
    sha: str = "",
    entry: str = "",
    history: bool = True,
) -> dict:
    """`history=False` drops the commit log from the `overview` payload.

    An escape hatch for a caller that wants the working-tree half and nothing
    else: with it false the `git log` fork does not happen at all. The `git`
    view does NOT use it — it draws a scoped Commits section from exactly these
    `commits`/`has_more`/`capped` fields, which is why they ride along on the
    overview rather than costing a second read.

    Defaulted TRUE rather than flipped, because `overview` is the reader's
    documented shape and those fields are what every caller (and this module's
    own test suite) reads. Opting out empties them rather than removing them, so
    the payload keeps ONE shape. Ignored by every other op — `op="log"` is how
    you ask for the log on purpose, and it is unaffected."""
    try:
        _check_op(op, sha, entry)
        root, rel, is_dir = _locate(file)
        if op == "worktree":
            _, _, _, has_commits = _head(root)
            return _worktree(root, entry, has_commits)
        if op == "commit":
            return _commit(root, rel, sha)
        if op == "branches":
            # Branch checkout is repo-wide by nature — a branch IS a repository
            # concept — so this op is deliberately NOT scoped to `rel`, unlike
            # every other read here (GT-13).
            branch, detached, _, _ = _head(root)
            branches, truncated = _branches(root)
            return {"ok": True, "current": branch, "detached": detached,
                    "branches": branches, "remotes": _remotes(root),
                    "truncated": truncated}
        if op == "pending":
            branch, detached, head, _ = _head(root)
            return _pending(root, rel,
                            branch or (("detached at " + head) if head else None))
        if op == "conflicts":
            branch, detached, head, _ = _head(root)
            return _conflicts(root, rel,
                              branch or (("detached at " + head) if head else None))
        if op == "stashes":
            stashes, truncated = _stashes(root)
            return {"ok": True, "stashes": stashes, "truncated": truncated}
        if op == "log":
            commits, has_more, capped, limit, page = _log(root, rel, limit, page)
            return {"ok": True, "commits": commits, "has_more": has_more,
                    "capped": capped, "max_commits": MAX_LOG_LIMIT,
                    "limit": limit, "page": page}

        branch, detached, head, has_commits = _head(root)
        changes, changes_truncated, dirty, staged_outside = _status(root, rel, is_dir)
        upstream, ahead, behind, remote = _upstream_state(root, has_commits)
        commits, has_more, capped, limit, page = (
            _log(root, rel, limit, page) if (has_commits and history)
            else ([], False, False,
                  min(max(1, int(limit or DEFAULT_LOG_LIMIT)), MAX_LOG_LIMIT), 0))
        return {
            "ok": True,
            "repo": {
                "root": root,
                "name": os.path.basename(root) or root,
                "branch": branch,
                "detached": detached,
                "head": head,
                "has_commits": has_commits,
                "dirty": dirty,
                "rel": rel,
                "is_dir": is_dir,
                # The remote picture, from what is RECORDED (see
                # `_upstream_state`): this read never fetches, so ⟳ stays an
                # explicit act rather than a side effect of opening the view.
                "upstream": upstream,
                "ahead": ahead,
                "behind": behind,
                "remote": remote,
            },
            "changes": changes,
            "changes_truncated": changes_truncated,
            # GT-14: what a commit from here would ALSO carry. The view turns
            # this into a warning; the reader's job is only to make it visible.
            "staged_outside": staged_outside,
            "commits": commits,
            "has_more": has_more,
            "capped": capped,
            "max_commits": MAX_LOG_LIMIT,
            "limit": limit,
            "page": page,
        }
    except _Refused as refused:
        return refused.payload


# The fused-render engine / app runner only invoke a @fused.udf-registered
# entrypoint; a bare main() returns null under them. Register main via the shim
# (the house pattern — canvas/las/usd readers) so it runs under the engine, while
# `main` stays a plain callable for the built-in executor and for tests.
try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass
