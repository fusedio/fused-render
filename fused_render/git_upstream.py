"""A throttled "has origin moved?" check for the folder an app was just
opened in, plus (below) the opt-in update/rebase mutations the activity
card's repo rows offer (SPEC §33 / §36).

WHEN THIS RUNS. `note_app_opened` is called from GET /render's D301 block
(server/routers/render.py) — the one existing definition of "this app is
being opened", right beside `record_app_open`, inside the same
`_preview != "1" and not _referred_by_preview(referer)` guard. It is
deliberately NOT triggered from /api/fs/list: that is the hook
`fused_render.index.freshness.note_folder_opened` uses, gated on the file
index's own `indexing_enabled()` pref (server/routers/index.py), and it fires
once per directory LISTING rather than once per app open. Borrowing that hook
would put git notifications behind an unrelated switch and make the
throttle — not the trigger — carry all the load of a page that lists a
folder every second. A background fetch only ever needs to happen once per
repo per app open, and GET /render already says that exactly once.

WHY A NEW MODULE, MIRRORING RATHER THAN IMPORTING `templates/git/ops.py`.
`ops.py` is reached only as `fused.runPython("./ops.py")` from inside the
git companion's iframe (template.html) — the React activity card has no
route to it, and it is exec'd standalone with no `fused_render` import
allowed (SPEC PY-15), so a server-side caller cannot import it either. This
is the same shape `server/routers/git_show.py` and `server/routers/
git_repos.py` already use for the same reason (git_show.py:144-155): the
non-interactive git environment, the repo-root resolution, and the mount
refusal below are DUPLICATED from `ops.py`/`log.py` on purpose, each noting
its twin. Keep them in step.

THE THROTTLE. Keyed on the repo ROOT, not the app folder, so several apps
inside one repo share one entry — opening app after app in a monorepo must
not fetch once per app. One check per repo per CHECK_TTL_S, and one
background slot for the whole process (its own semaphore, distinct from
`server/routers/index.py`'s `_freshness_slot`, so a git fetch and an index
freshness check never contend for the same slot). The check always returns
immediately; the real work — if any — runs off the request thread.

SILENCE ON FAILURE IS DELIBERATE. An unreachable remote, a repo with no
`origin`, a repo git itself refuses to talk to — all of these record
nothing and raise nothing. A background check that nagged about a
misconfigured remote would be worse than one that says nothing; the git
companion is where a fetch error is visible.

MOUNT-BACKED REPOS ARE REFUSED OUTRIGHT, before any subprocess — the same
rule `ops.py`'s `_refuse_mounts` (GT-4 / MD-11) enforces for the same
reason: a background fetch across an rclone-NFS mount is exactly the wedge
that refusal exists to prevent.
"""
import logging
import os
import subprocess
import sys
import threading
import time

from fused_render.shell import mounts as shell_mounts

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ git plumbing
#
# Mirrors templates/git/ops.py's `_git_bin`/`_ENV`/`_popen_kwargs`/`_run`
# (not imported — see the module docstring). The env is the fetch/pull subset
# of ops.py's: non-interactive, no GUI credential prompt, no LFS smudge for a
# ref we are only counting commits on. `GIT_OPTIONAL_LOCKS=0` is deliberately
# ABSENT here too, for ops.py's own reason: a fetch takes the lock it needs
# regardless, and the flag would only misstate what this does.

_GIT_BIN = None


def _git_bin():
    global _GIT_BIN
    if _GIT_BIN is None:
        import shutil
        _GIT_BIN = shutil.which("git") or "git"
    return _GIT_BIN


_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "Never",
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GIT_EDITOR": "false",
    "GIT_SEQUENCE_EDITOR": "false",
}

# A network call (fetch) gets more headroom than a purely local one (rev-list,
# symbolic-ref, status) — both use this one bound for simplicity; a check that
# hangs past it is exactly the "unreachable remote" case this stays silent
# about.
TIMEOUT_S = 20.0

# How long a repo's last check is trusted before another app open in it is
# worth a fresh fetch. A few minutes: short enough that reopening an app
# notices a just-pushed change soon, long enough that opening several apps in
# one repo (or the same app repeatedly) costs one fetch, not one per open.
CHECK_TTL_S = 300.0


def _popen_kwargs():
    return {
        "env": {**os.environ, **_ENV},
        "stdin": subprocess.DEVNULL,
        # Absolute argv[0] (via _git_bin), close_fds=False and no cwd= together
        # are what keep CPython on the posix_spawn path instead of fork() —
        # see the note in templates/git/ops.py above its own _popen_kwargs. A
        # fork with libproj resident in this process dies with SIGSEGV before
        # exec, silently, which is worse here than a normal git failure: it
        # would look exactly like "unreachable remote" and never log.
        "close_fds": False,
        "creationflags": (subprocess.CREATE_NO_WINDOW
                          if sys.platform == "win32" else 0),
    }


def _run(root, *args, timeout=TIMEOUT_S):
    """One bounded git call; `(returncode, stdout, stderr)`, or None when git
    could not even be run (missing, timed out, OS-level spawn failure)."""
    try:
        proc = subprocess.run(
            [_git_bin(), "-C", root, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, **_popen_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.returncode, proc.stdout, proc.stderr


def _out(result):
    return result[1].decode("utf-8", "replace").strip() if result else ""


def _ok(result):
    return result is not None and result[0] == 0


# ------------------------------------------------------------------------ locate


def repo_root(path):
    """The work-tree root containing `path`, or None — not inside a repo, git
    unavailable, or mount-backed (refused before any subprocess: the pattern
    `ops.py:_refuse_mounts` enforces, for the reason the module docstring
    gives)."""
    if not path:
        return None
    if shell_mounts.is_mount_backed(path):
        return None
    cwd = path if os.path.isdir(path) else os.path.dirname(path)
    if not cwd or not os.path.isdir(cwd):
        return None
    result = _run(cwd, "rev-parse", "--show-toplevel")
    if not _ok(result):
        return None
    top = _out(result)
    return os.path.realpath(top) if top else None


def _default_branch(root):
    """The remote's default branch name, off the `refs/remotes/origin/HEAD`
    symref a clone records — `deeplink.py::_default_branch`'s own logic
    (deeplink.py:274-282), including its `remote set-head --auto` re-ask
    fallback for a clone (or a hand-init'd repo) missing that symref. None
    when there is no `origin` remote, or git cannot resolve it — either way,
    nothing to compare HEAD against."""
    result = _run(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not _ok(result):
        fixed = _run(root, "remote", "set-head", "origin", "--auto")
        if not _ok(fixed):
            return None
        result = _run(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if not _ok(result):
            return None
    short = _out(result)
    return short.split("/", 1)[1] if "/" in short else None


def _current_branch(root):
    result = _run(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return _out(result) or None if _ok(result) else None


def _fetch_one_ref(root, ref):
    """Fetch exactly one ref off `origin` — never `--all`, never every branch
    a repo happens to carry, for a check that runs on every app open."""
    return _ok(_run(root, "fetch", "--", "origin", ref, timeout=TIMEOUT_S))


def _behind_count(root, default_branch):
    """How many commits `origin/<default_branch>` has that HEAD does not —
    the `--left-right --count` reference is `templates/git/log.py:770-777`;
    here only one side is wanted, so a plain two-dot `--count` suffices."""
    result = _run(root, "rev-list", "--count", f"HEAD..origin/{default_branch}")
    if not _ok(result):
        return None
    out = _out(result)
    return int(out) if out.isdigit() else None


def check_repo(root):
    """One upstream check for `root`: fetch the default branch, count how far
    behind it HEAD is. Returns a result dict, or None when the remote
    couldn't be reached / resolved — silence is deliberate (module
    docstring)."""
    default_branch = _default_branch(root)
    if not default_branch:
        return None
    if not _fetch_one_ref(root, default_branch):
        return None
    behind = _behind_count(root, default_branch)
    if behind is None:
        return None
    branch = _current_branch(root)
    return {
        "root": root,
        "branch": branch,
        "default_branch": default_branch,
        "on_default": branch is not None and branch == default_branch,
        "behind": behind,
        "checked_at": time.time(),
    }


# --------------------------------------------------------------- the mutations
#
# The two actions the activity card's repo rows offer: Update (an --ff-only
# pull, primary, on the default branch) and Rebase (secondary, everywhere
# else — offered exactly where Update would refuse to fast-forward, and onto
# exactly one target: the remote's tracked default branch, never a
# user-chosen ref). Both mirror templates/git/ops.py's own `_pull`
# (ops.py:1096-1121, the explicit remote-and-refspec argument) and
# `_require_remote` (ops.py:827-836, the no-remote refusal) — mirrored, not
# imported, for the reason the module docstring gives. `_rebase` there is
# this function's twin; keep the two in step.


def _brief(result):
    """The lines of git's stderr worth quoting back into a refusal — same
    "pick the diagnosis, not the first N lines" idea as ops.py's `_brief`."""
    if not result:
        return ""
    text = result[2].decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    diagnostic = [line for line in lines
                  if line.lower().startswith(("fatal:", "error:", "warning:"))]
    return " ".join((diagnostic or lines)[:3])


def _refuse(reason, message):
    return {"ok": False, "reason": reason, "message": message}


def _is_clean(root):
    result = _run(root, "status", "--porcelain")
    if not _ok(result):
        return False  # unknown status — never mutate a repo we cannot read
    return not _out(result)


def _mutation_preflight(root):
    """Every check both mutations need before touching anything: the repo
    still exists, isn't mount-backed (GT-4 / MD-11 — the same wedge
    `ops.py:_refuse_mounts` exists to prevent), has a clean working tree, an
    attached branch, and a resolvable `origin` with a default branch. Returns
    `(branch, default_branch, refusal)` — exactly one of the first two or the
    third is None."""
    if not os.path.isdir(root):
        return None, None, _refuse("missing", f"{root} no longer exists.")
    if shell_mounts.is_mount_backed(root):
        return None, None, _refuse(
            "mount", "Git operations are not available on remote mounts.")
    if not _is_clean(root):
        return None, None, _refuse(
            "dirty", "This repository has uncommitted changes — commit, "
            "stash, or discard them first.")
    branch = _current_branch(root)
    if not branch:
        return None, None, _refuse(
            "detached", "HEAD is detached, so there is nothing to update. "
            "Check out a branch first.")
    if not _ok(_run(root, "remote", "get-url", "origin")):
        return None, None, _refuse(
            "no-remote", "This repository has no origin remote to talk to.")
    default_branch = _default_branch(root)
    if not default_branch:
        return None, None, _refuse(
            "no-remote",
            "origin has no default branch this app could resolve.")
    return branch, default_branch, None


def _record(result):
    if result is not None:
        with _state_lock:
            _state[result["root"]] = result


def update_repo(root):
    """--ff-only pull of `origin/<default>` — the card's primary action, on
    the default branch. Refuses on a dirty tree, a detached HEAD, a missing
    or unresolvable remote, or a mount-backed repo; a non-fast-forward pull
    (should not happen for the default branch under normal use, but the tree
    may have moved between the check and the click) is reported in git's own
    words, exactly like ops.py's `_pull`."""
    branch, default_branch, refusal = _mutation_preflight(root)
    if refusal is not None:
        return refusal
    result = _run(root, "pull", "--ff-only", "--", "origin", default_branch,
                  timeout=TIMEOUT_S)
    if not _ok(result):
        return _refuse("git-failed", _brief(result) or "git pull failed.")
    _record(check_repo(root))
    return {"ok": True, "op": "update", "root": root,
            "message": f"Updated to origin/{default_branch}."}


def rebase_repo(root):
    """Rebase the current branch onto `origin/<default>` — the card's
    secondary action, offered exactly where `update_repo` would refuse to
    fast-forward (off the default branch). Same preflight as `update_repo`.

    A conflict is left in place, mid-rebase, rather than aborted: the git
    companion's conflict reader and `resolve` op already handle a rebase in
    progress generically (the same reasoning `templates/git/ops.py`'s
    `_rebase` — this function's twin — documents in full), so the way back
    in is the git panel (or Fix with Claude, scoped to this repo), never a
    silent discard of the rebase the button just started."""
    branch, default_branch, refusal = _mutation_preflight(root)
    if refusal is not None:
        return refusal
    fetch = _run(root, "fetch", "--", "origin", default_branch, timeout=TIMEOUT_S)
    if not _ok(fetch):
        return _refuse("git-failed", _brief(fetch) or "git fetch failed.")
    result = _run(root, "rebase", "--", f"origin/{default_branch}",
                  timeout=TIMEOUT_S)
    if not _ok(result):
        return _refuse(
            "git-failed",
            _brief(result) or "git rebase failed — resolve the conflict in "
            "the git panel.")
    _record(check_repo(root))
    return {"ok": True, "op": "rebase", "root": root,
            "message": f"Rebased onto origin/{default_branch}."}


# ------------------------------------------------------------------- the throttle

_checked_lock = threading.Lock()
_checked: dict = {}  # repo root -> last-checked epoch seconds

# One fetch at a time for the whole process, in its own slot — distinct from
# server/routers/index.py's _freshness_slot (the module docstring's "never
# block each other").
_check_slot = threading.Lock()

_state_lock = threading.Lock()
_state: dict = {}  # repo root -> last known check_repo() result


def _due(root, now):
    with _checked_lock:
        last = _checked.get(root)
        if last is not None and (now - last) < CHECK_TTL_S:
            return False
        _checked[root] = now
        return True


def _run_check(root):
    """The check itself, off the request thread. Never raises: a render must
    not fail, or slow down, because a git housekeeping check did."""
    try:
        _record(check_repo(root))
    except Exception:  # noqa: BLE001 — best-effort housekeeping
        logger.exception("git-upstream check failed for %s", root)
    finally:
        _check_slot.release()


def note_app_opened(path, *, _runner=None):
    """An app rooted under `path` was just opened. Throttled per repo root,
    off the request path, best-effort — see the module docstring for the
    trigger, the throttle, and the silence-on-failure rules.

    `_runner` is a test seam: given a callable, it receives the zero-arg
    check function instead of a background thread being started for it, so a
    test can run the check synchronously and inspect `known_repos()`
    immediately. Production callers never pass it.

    Returns whether a check was started — for tests; real callers ignore it,
    matching `index.note_folder_opened`'s own return convention.
    """
    root = repo_root(path)
    if root is None:
        return False
    if not _due(root, time.time()):
        return False
    if not _check_slot.acquire(blocking=False):
        return False
    runner = _runner or (lambda fn: threading.Thread(
        target=fn, daemon=True, name="git-upstream-check").start())
    try:
        runner(lambda: _run_check(root))
    except RuntimeError:  # interpreter shutting down
        _check_slot.release()
        return False
    return True


def known_repos():
    """Every repo with a recorded, non-zero behind count — what
    GET /api/git-upstream reports. A repo that is up to date (or was never
    successfully checked) produces no row."""
    with _state_lock:
        return [dict(v) for v in _state.values() if v.get("behind", 0) > 0]
