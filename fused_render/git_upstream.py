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


def _ahead_behind_counts(root, default_branch):
    """`(ahead, behind)` — how many commits HEAD has that
    `origin/<default_branch>` does not, and vice versa. `--left-right
    --count` on the three-dot range prints "<ahead>\t<behind>" in one call
    — the reference is `templates/git/log.py:770-777`, whose own comment
    explains the three dots: two would give one combined total and lose the
    direction. Both sides are wanted here (not just `behind`, the original
    narrower call this replaced): `ahead` is what the "Fix with Claude"
    prompt (repo-updates-lib.ts's `repoFixPrompt`) needs to describe a
    rebase refusal honestly — the rebase path exists BECAUSE the branch has
    local commits behind couldn't tell it about."""
    result = _run(root, "rev-list", "--left-right", "--count",
                  f"HEAD...origin/{default_branch}")
    if not _ok(result):
        return None, None
    parts = _out(result).split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None, None
    return int(parts[0]), int(parts[1])


def check_repo(root):
    """One upstream check for `root`: fetch the default branch, count how far
    ahead/behind it HEAD is. Returns a result dict, or None when the remote
    couldn't be reached / resolved — silence is deliberate (module
    docstring)."""
    default_branch = _default_branch(root)
    if not default_branch:
        return None
    if not _fetch_one_ref(root, default_branch):
        return None
    ahead, behind = _ahead_behind_counts(root, default_branch)
    if behind is None:
        return None
    branch = _current_branch(root)
    return {
        "root": root,
        "branch": branch,
        "default_branch": default_branch,
        "on_default": branch is not None and branch == default_branch,
        "ahead": ahead or 0,
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


def _is_clean(root, *, include_untracked=True):
    """Whether `root`'s working tree is clean enough to mutate.

    `include_untracked` is NOT one bound tightened for its own sake: an
    `--ff-only` pull only ever touches TRACKED refs and the index, so a
    `.venv/`, build output, or a scratch file sitting untracked in the tree
    can never conflict with it — `update_repo` passes `include_untracked=
    False` so a repo that would otherwise never be able to update from this
    card (an ordinary repo with an ordinary gitignored build dir) isn't
    refused over files the pull will never touch. `rebase_repo` keeps the
    stricter, untracked-inclusive check (the default here): a rebase
    replays commits by checking out each one in turn, and an untracked file
    that happens to collide with a path one of THOSE commits touches is a
    real, if rarer, way to lose it — worth refusing up front rather than
    discovering mid-rebase.
    """
    args = ["status", "--porcelain"]
    if not include_untracked:
        args.append("--untracked-files=no")
    result = _run(root, *args)
    if not _ok(result):
        return False  # unknown status — never mutate a repo we cannot read
    return not _out(result)


def _real_gitdir(root):
    """The actual git directory for `root` — a plain subdirectory for an
    ordinary checkout, but a DIFFERENT, shared directory for a LINKED
    WORKTREE, where `root/.git` is a FILE (`gitdir: /path/to/real/gitdir`),
    not a directory. `os.path.isdir(root, ".git")` is blind to that shape —
    it answers False for every linked worktree, silently — which is exactly
    how a conflicted rebase inside one went undetected by
    `_operation_in_flight` below. `git rev-parse --absolute-git-dir` asks
    git itself, which already resolves this correctly for both shapes, so
    nothing here has to special-case `.git`-as-file by hand. None on any
    failure (git missing, `root` not a repo) — the same silent-on-uncertainty
    posture the rest of this preflight uses, never a raise."""
    result = _run(root, "rev-parse", "--absolute-git-dir")
    if not _ok(result):
        return None
    return _out(result) or None


def _operation_in_flight(root):
    """Which multi-step git operation `root` is already mid-way through, if
    any. Mirrors `templates/git/log.py`'s `_operation_in_flight` in every
    other respect (that module's own twin of this) — `rebase-merge`/
    `rebase-apply` are checked FIRST because a conflicted rebase step also
    writes `MERGE_HEAD`, and asking about single refs first would misreport
    the step's PARENT operation as a plain merge. Every name log.py's
    version reports is kept here, not just `rebase` (the only one this
    module's own mutations can leave behind): a merge/cherry-pick/revert
    started some OTHER way — a terminal, the git companion — must still be
    named accurately by this preflight rather than folding into the
    generic "dirty" refusal. UNLIKE log.py's twin, this one resolves the
    real gitdir via `_real_gitdir` rather than assuming `root/".git"` is a
    directory, so it (and only it, for now — log.py's copy has the same
    blind spot, pre-existing on main and out of scope here) still finds
    these markers inside a linked worktree."""
    gitdir = _real_gitdir(root)
    if gitdir is None:
        return None
    for sub in ("rebase-merge", "rebase-apply"):
        if os.path.isdir(os.path.join(gitdir, sub)):
            return "rebase"
    for marker, name in (("MERGE_HEAD", "merge"),
                         ("CHERRY_PICK_HEAD", "cherry-pick"),
                         ("REVERT_HEAD", "revert")):
        if os.path.exists(os.path.join(gitdir, marker)):
            return name
    return None


def _mutation_preflight(root, *, include_untracked=True):
    """Every check both mutations need before touching anything: the repo
    still exists, isn't mount-backed (GT-4 / MD-11 — the same wedge
    `ops.py:_refuse_mounts` exists to prevent), isn't already mid an
    operation `rebase_repo` (or a terminal) left in flight, has a clean
    working tree, an attached branch, and a resolvable `origin` with a
    default branch. Returns `(branch, default_branch, refusal)` — exactly
    one of the first two or the third is None."""
    if not os.path.isdir(root):
        return None, None, _refuse("missing", f"{root} no longer exists.")
    if shell_mounts.is_mount_backed(root):
        return None, None, _refuse(
            "mount", "Git operations are not available on remote mounts.")
    operation = _operation_in_flight(root)
    if operation is not None:
        # Checked BEFORE the dirty check on purpose: a mid-rebase tree
        # normally has unmerged paths of its own, which `_is_clean` would
        # otherwise report as ordinary "uncommitted changes" — the wrong
        # diagnosis (this isn't an edit to commit or discard) and the wrong
        # instruction (there is no tree state the dirty refusal's own
        # wording can be resolved into; the only way out is finishing or
        # aborting the operation already in progress).
        return None, None, _refuse(
            "in-progress",
            f"This repository is already in the middle of a {operation} — "
            "resolve it (the git panel's conflict view, or a terminal) "
            "before trying again.")
    if not _is_clean(root, include_untracked=include_untracked):
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


def _refresh_after_mutation(root):
    """Re-check `root` right after a successful `update`/`rebase`, so its
    row clears (or updates) without waiting out CHECK_TTL_S. Unlike the
    throttled background path (`_background_check`), a re-check that fails
    here must NOT leave the pre-mutation entry standing — a stale `behind >
    0` with an Update button, after the mutation that button ran already
    succeeded — so a failed re-check drops the entry outright rather than
    trusting a second git call the mutation's own success never depended
    on."""
    result = check_repo(root)
    if result is not None:
        _record(result)
        return
    with _state_lock:
        _state.pop(root, None)


def update_repo(root):
    """--ff-only pull of `origin/<default>` — the card's primary action, on
    the default branch. Refuses on a dirty tree, a detached HEAD, a missing
    or unresolvable remote, or a mount-backed repo; a non-fast-forward pull
    (should not happen for the default branch under normal use, but the tree
    may have moved between the check and the click) is reported in git's own
    words, exactly like ops.py's `_pull`."""
    branch, default_branch, refusal = _mutation_preflight(
        root, include_untracked=False)
    if refusal is not None:
        return refusal
    result = _run(root, "pull", "--ff-only", "--", "origin", default_branch,
                  timeout=TIMEOUT_S)
    if not _ok(result):
        return _refuse("git-failed", _brief(result) or "git pull failed.")
    _refresh_after_mutation(root)
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
    branch, default_branch, refusal = _mutation_preflight(
        root, include_untracked=True)
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
    _refresh_after_mutation(root)
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


def _background_check(path):
    """Everything a note_app_opened dispatch does, entirely off the request
    thread: resolve `path` to a repo root (a `git rev-parse` subprocess),
    decide whether that root is due, and run the check if so. Never raises:
    a render must not fail, or slow down, because a git housekeeping check
    did. Releases the process-wide check slot exactly once, on every exit
    path — `note_app_opened` acquires it before dispatching this, and this
    is the only place it is released.

    Splitting `repo_root` resolution OUT of `note_app_opened` and into here
    is what makes the module docstring's "the check always returns
    immediately; the real work runs off the request thread" true: resolving
    a path to a repo root is itself a git subprocess (plus a mount-guard
    check), and running it synchronously in `note_app_opened` — as an
    earlier version of this function did — meant EVERY non-preview
    `/render` of an app paid that spawn on the request thread, whether or
    not the app was even in a git repo."""
    try:
        root = repo_root(path)
        if root is not None and _due(root, time.time()):
            _record(check_repo(root))
    except Exception:  # noqa: BLE001 — best-effort housekeeping
        logger.exception("git-upstream check failed for %s", path)
    finally:
        _check_slot.release()


def note_app_opened(path, *, _runner=None):
    """An app rooted under `path` was just opened. Throttled per repo root,
    off the request path, best-effort — see the module docstring for the
    trigger, the throttle, and the silence-on-failure rules.

    Does NO synchronous git work of its own: the process-wide check slot is
    acquired here (a plain, non-blocking `Lock.acquire` — never a
    subprocess), and everything that touches git — resolving `path` to a
    repo root, deciding whether it is due, fetching if so — happens in
    `_background_check`, dispatched below. Acquiring the slot BEFORE that
    resolution, rather than after (an earlier version of this function did
    it the other way around), is also what fixes the throttle's own
    cross-repo bug: stamping `_checked[root]` only ever happens once the
    slot is already held, so a repo B opened while repo A's check is still
    running is never marked "just checked" by a dispatch that the busy slot
    is about to refuse — it gets a real check on the next open instead of
    silently waiting out the rest of CHECK_TTL_S for nothing.

    `_runner` is a test seam: given a callable, it receives the zero-arg
    background-check function instead of a background thread being started
    for it, so a test can run the check synchronously and inspect
    `known_repos()` immediately. Production callers never pass it.

    Returns whether a background attempt was DISPATCHED — for tests; real
    callers ignore it. Matches `index.note_folder_opened`'s own return
    convention exactly: that function also returns True as soon as its
    thread starts, before its own equivalent of `repo_root`/`_due`
    (`enclosing_root`/`_freshness_due`) has run at all, off-thread, inside
    the dispatched call. True here means "a check may run"; it does not
    mean `path` is confirmed to be in a repo, or that this root is due —
    both are resolved off the request thread, by design, and are not
    knowable synchronously any more.
    """
    if not _check_slot.acquire(blocking=False):
        return False
    runner = _runner or (lambda fn: threading.Thread(
        target=fn, daemon=True, name="git-upstream-check").start())
    try:
        runner(lambda: _background_check(path))
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


def is_known_repo(root):
    """Whether `root` is a repo THIS module's own background check has
    recorded state for — the allowlist `POST /api/git-upstream` (server/
    routers/git_upstream.py) checks a client-supplied `root` against before
    running `update_repo`/`rebase_repo`. Membership in `_state`, not
    `known_repos()`'s filtered (behind > 0) view: a repo the check just
    brought up to date (behind == 0) is still a repo THIS server checked,
    and a card race (poll says behind, click lands after a concurrent
    check already zeroed it) must not turn into a 403 for an otherwise
    legitimate root. What this refuses is a root the check has never even
    heard of — an arbitrary path handed in from an open page's POST body."""
    with _state_lock:
        return root in _state
