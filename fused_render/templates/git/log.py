"""Reader backing `git/template.html` — git history and changes scoped to the
open path (SPEC §33 / GT-5..GT-10).

Everything here is `git` shelled out to and parsed. Nothing reimplements git's
knowledge of anything: not what a repository is, not what "dirty" means, not how
a rename is detected, not how long ago a commit was. The module's whole job is to
ask the right bounded question and turn the answer into JSON.

Four operations, one per thing the view can ask for:

  overview  — the header (repo, branch, detached, dirty, scope), the uncommitted
              changes under the scope, and the FIRST page of the scoped log; one
              call, so opening the view is one round trip.
  log       — a later page of the same log ("load more").
  commit    — one commit's metadata plus its diff, restricted to the scope.
  worktree  — the working tree vs HEAD for one uncommitted entry.

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


def _argv(root, args):
    return ["git", "--no-pager", *_CONFIG, "-C", root, *args]


def _popen_kwargs():
    return {
        "env": {**os.environ, **_ENV},
        "stdin": subprocess.DEVNULL,
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
    `(entries, truncated, dirty)`, where `truncated` now covers BOTH caps.
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
        if not _in_scope(path, rel, is_dir):
            continue
        x, y = code[0], code[1]
        untracked = code == "??"
        entries.append({
            "status": code,
            "x": x,
            "y": y,
            "path": path,
            "orig": orig,
            "staged": not untracked and x not in (" ", "?"),
            "unstaged": not untracked and y not in (" ", "?"),
            "untracked": untracked,
            "label": _STATUS_LABELS.get("?" if untracked else (x if x != " " else y), ""),
        })
    truncated = byte_capped or dangling or len(entries) > MAX_CHANGES
    # A byte cap means the repo-wide dirty verdict is `True` regardless of what
    # survived parsing — git had more to say than we were willing to read.
    return entries[:MAX_CHANGES], truncated, dirty or byte_capped


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
    if op not in ("overview", "log", "commit", "worktree"):
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
    """The half of the containment check that needs the filesystem (GT-6).

    `_check_op`'s string test is necessary and not sufficient: it proves the
    entry NAMES nothing outside the repository, but every consumer of that name
    FOLLOWS symlinks. `os.path.isfile` follows them, and so does
    `git diff --no-index -- /dev/null <rel>`, which renders the target's bytes —
    so an untracked `link -> /etc/shadow` sitting inside the repo passes a string
    test made of nothing but repo-relative segments and then puts that file in
    the diff pane. The check plainly means "inside the repository", so it is made
    to mean that:

    * the REALPATH must still be under the root — which also covers a symlinked
      parent directory, where the final component is not a link at all;
    * and a symlinked entry is refused outright even when its target stays
      inside, because `--no-index` would show the target's content under the
      link's name, which is a different file than the row claims.

    Only the working-tree op needs this. A TRACKED symlink goes through
    `git diff HEAD -- <rel>`, where git handles it as a symlink (it diffs the
    link's target path text, it does not read through it) — the `--no-index`
    branch is the one that reads bytes off whatever the name resolves to.
    """
    real_root = os.path.realpath(root)
    real = os.path.realpath(full)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise _Refused("outside-repo",
                       f"{rel} resolves outside the repository.")
    if os.path.islink(full):
        raise _Refused("symlink",
                       f"{rel} is a symbolic link. Open its target directly — "
                       "this view will not read through a link.")


def _worktree(root, entry, has_commits):
    rel = (entry or "").replace("\\", "/").strip("/")
    full = os.path.join(root, *rel.split("/"))
    _contain(root, rel, full)
    tracked = _git(root, "ls-files", "--error-unmatch", "-z", *_pathspec(rel),
                   allow=(0, 1, 128)).strip(b"\0")
    untracked = not tracked
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
        "target": rel,
        "untracked": untracked,
        "diff": diff,
        "empty": not diff,
        "truncated": truncated,
        "shown_lines": shown,
        "max_bytes": MAX_DIFF_BYTES,
        "max_lines": MAX_DIFF_LINES,
    }


def main(
    file: str,
    op: str = "overview",
    limit: int = 30,
    page: int = 0,
    sha: str = "",
    entry: str = "",
) -> dict:
    try:
        _check_op(op, sha, entry)
        root, rel, is_dir = _locate(file)
        if op == "worktree":
            _, _, _, has_commits = _head(root)
            return _worktree(root, entry, has_commits)
        if op == "commit":
            return _commit(root, rel, sha)
        if op == "log":
            commits, has_more, capped, limit, page = _log(root, rel, limit, page)
            return {"ok": True, "commits": commits, "has_more": has_more,
                    "capped": capped, "max_commits": MAX_LOG_LIMIT,
                    "limit": limit, "page": page}

        branch, detached, head, has_commits = _head(root)
        changes, changes_truncated, dirty = _status(root, rel, is_dir)
        commits, has_more, capped, limit, page = (
            _log(root, rel, limit, page) if has_commits
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
            },
            "changes": changes,
            "changes_truncated": changes_truncated,
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
