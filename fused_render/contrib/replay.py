"""The tree primitives every contrib mutation is built from
(docs/CONTRIB_LOOP.md §0.2, §0.3, §6.2): replay in a scratch worktree, land
the result with a backup, clean up — plus the mutation slot and the
committed-`.fused/` guard they share.

THE REAL APP FOLDER NEVER HAS A REBASE IN FLIGHT (§0.3). `replay` runs
`git rebase` inside a scratch `git worktree` under `<home>/contrib/work/<id>/`
— outside the workspace, so `app_git.commit` and the Claude-turn sweep never
commit there, and a conflict stops THAT tree, never Bob's. Only a finished
tip leaves it, through `land`, which moves Bob's branch with
`git reset --keep`: it refuses (rather than clobbers) when an uncommitted
edit overlaps what the move would change, and a refusal is "defer, try next
time" (§0.2), never an error.

NOTHING OF BOB'S IS LOST SILENTLY (§0.2). Before `land` moves a branch off a
tip the new one does not contain, the old tip is pinned at
`refs/fused/backup/<unix_ms>` (and a dirty tree's `git stash create` commit
at `…-wip`). A pure fast-forward pins nothing: its old tip stays in the new
tip's history, so a backup would be noise in every follower's repo for every
author push. The refs are never deleted by the engine; the record names the
last ten.

COMMITTED `.fused/` (§4). A repo that tracks its own `.fused/` would show
fused's writes there (the install stamp, `meta.json`) as edits, so tracked
`.fused/*` paths get git's skip-worktree bit (`protect_locked`). That hides
them from `status` — but it does NOT let a tree move through: verified on
git 2.39–2.50, `merge --ff-only`, `reset --keep` AND `reset --hard` all
refuse with "Entry '.fused/…' not uptodate" the moment the author's update
changes a file whose local copy fused has rewritten. So every tree move runs
inside `fused_aside`: the worktree copies of tracked `.fused/` paths are
renamed into `<gitdir>/fused-aside/` (same filesystem, no copy), the move
runs, and they are renamed back over whatever git checked out. Machine state
wins locally; the index carries the author's version, hidden by the bit. A
crash between the two renames is recovered by the next `fused_aside` or
`protect_locked`, which restore any leftover first.

THE SLOT (§0.9). Every write to a real app repo holds
`git_upstream._check_slot`, the same process-wide lock auto-sync's fetches
use, so two git writers never meet in one repo. It is a plain `Lock`: a
thread that already holds it and acquires again would wait out the bounded
timeout and then refuse. `slot()` makes the nesting EXPLICIT instead of
guessed: the one entry point that is called with the slot already held
(`engine.sync`, from git_upstream's intercept) wraps its body in
`slot_held()`, and every `slot()` inside that on the same thread is then a
no-op that answers True. Every other entry point (a router-triggered
discard, a merge job's accept) acquires it for real. Network calls to GitHub
never run under it (§0.9); a `git fetch` does, because it writes refs.

`replay` itself does not need the slot: it never touches the real
branch, index or worktree — only the shared object store (safe under
concurrent writers) and the scratch worktree's own admin dir. That is what
lets a Merge with Claude job keep its worktree for minutes without blocking
every other sync on the machine.

Subprocess discipline is git_upstream's (`_run`: absolute git, `-C`,
`close_fds=False`, non-interactive env, bounded timeout).
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import threading
import time
import uuid

from fused_render import git_upstream as gu

logger = logging.getLogger(__name__)

FOLLOW_REF = "refs/fused/follow"
#: The follow tip HEAD currently sits on — what Bob's changes are "on top
#: of". Advanced by `land` whenever the landed tip contains the follow tip;
#: the engine replays `<base>..HEAD`, so an author's force-push never turns
#: their own old commits into Bob's changes.
BASE_REF = "refs/fused/base"
BACKUP_PREFIX = "refs/fused/backup/"
PROPOSED_REF = "refs/fused/proposed"

#: A rebase may lazily fetch blobs (installs are `--filter=blob:none`
#: clones) and replays several commits: more headroom than one fetch.
REPLAY_TIMEOUT_S = 180.0

# Config for every rebase/commit the engine runs: no hooks (the user's global
# hooks are not ours to fire for a background replay), no signing prompt, no
# autostash (the scratch tree is clean by construction), zdiff3 markers (what
# a Merge with Claude job reads), no rerere replaying a stale resolution.
_REPLAY_CONFIG = (
    "-c", "core.hooksPath=" + os.devnull,
    "-c", "commit.gpgsign=false",
    "-c", "rebase.autoStash=false",
    "-c", "rebase.updateRefs=false",
    "-c", "rebase.autoSquash=false",
    "-c", "merge.conflictStyle=zdiff3",
    "-c", "rerere.enabled=false",
    "-c", "gc.auto=0",
)

_FUSED_IDENTITY = ("-c", "user.name=Fused", "-c", "user.email=apps@fused.io")


# ------------------------------------------------------------------- git bits


def git(root, *args, timeout=gu.TIMEOUT_S):
    """`git_upstream._run` — one bounded call, `(rc, out, err)` or None."""
    return gu._run(root, *args, timeout=timeout)


def ok(result) -> bool:
    return gu._ok(result)


def out(result) -> str:
    return gu._out(result)


def rev(root, spec) -> str | None:
    """The commit `spec` names, or None."""
    r = git(root, "rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}")
    sha = out(r) if ok(r) else ""
    return sha or None


def is_ancestor(root, a, b) -> bool:
    """Whether commit `a` is in `b`'s history (a == b counts)."""
    if not a or not b:
        return False
    return ok(git(root, "merge-base", "--is-ancestor", a, b))


def count(root, rng) -> int | None:
    r = git(root, "rev-list", "--count", rng)
    try:
        return int(out(r)) if ok(r) else None
    except ValueError:
        return None


def identity_args(root) -> tuple:
    """Committer identity for a replay: the repo's own when it has one (a
    rebase rewrites the committer, and Bob's machine usually knows Bob),
    else the `Fused` identity app_git commits with — a fresh machine with no
    git identity must still be able to replay."""
    if out(git(root, "config", "--get", "user.email")):
        return ()
    return _FUSED_IDENTITY


def work_dir() -> str:
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), "contrib", "work")


# ------------------------------------------------------------------- the slot

_held = threading.local()


def _depth() -> int:
    return getattr(_held, "depth", 0)


@contextlib.contextmanager
def slot(*, block: bool = True):
    """Hold the process-wide mutation slot; yields whether it is held. A
    no-op yielding True when this thread already holds it (`slot_held`, or
    an enclosing `slot`). `block=False` never waits — for idempotent
    housekeeping that the next sync redoes anyway."""
    if _depth() > 0:
        _held.depth += 1
        try:
            yield True
        finally:
            _held.depth -= 1
        return
    if block:
        acquired = gu._check_slot.acquire(timeout=gu.TIMEOUT_S + 5.0)
    else:
        acquired = gu._check_slot.acquire(blocking=False)
    if acquired:
        _held.depth = 1
    try:
        yield acquired
    finally:
        if acquired:
            _held.depth = 0
            gu._check_slot.release()
            gu._drain_pending()


@contextlib.contextmanager
def slot_held():
    """Mark that the CALLER already holds the slot (git_upstream's intercept
    — `_sync_locked`'s callers acquired it), so `slot()` inside nests."""
    _held.depth = _depth() + 1
    try:
        yield
    finally:
        _held.depth -= 1


# ------------------------------------------------------- the .fused/ guard


def _tracked_fused(root) -> list[tuple[str, str]]:
    """`[(tag, path)]` for every tracked path under `.fused/` (`ls-files -v`
    tags: `S` = skip-worktree already set)."""
    r = git(root, "ls-files", "-v", "-z", "--", ".fused")
    if not ok(r):
        return []
    entries = []
    for item in r[1].decode("utf-8", "replace").split("\0"):
        if len(item) > 2 and item[1] == " ":
            entries.append((item[0], item[2:]))
    return entries


def _aside_dir(root) -> str | None:
    gitdir = gu._real_gitdir(root)
    return os.path.join(gitdir, "fused-aside") if gitdir else None


def _restore_aside(root, aside) -> None:
    """Rename every file left in `aside` back over its path in `root`, then
    drop `aside`. A file that cannot go back stays put (logged) — never
    deleted."""
    if not aside or not os.path.isdir(aside):
        return
    failed = False
    for dirpath, _dirs, files in os.walk(aside):
        for name in files:
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, aside)
            dest = os.path.join(root, rel)
            try:
                if os.path.isdir(dest) and not os.path.islink(dest):
                    raise OSError(f"{dest} is a directory")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(src, dest)
            except OSError:
                failed = True
                logger.error("contrib: could not restore %s from %s", dest, src, exc_info=True)
    if not failed:
        shutil.rmtree(aside, ignore_errors=True)


def _target_fused(root, target) -> list[str]:
    """The `.fused/` paths commit `target` tracks."""
    if not target:
        return []
    r = git(root, "ls-tree", "-r", "-z", "--name-only", target, "--", ".fused")
    if not ok(r):
        return []
    return [p for p in r[1].decode("utf-8", "replace").split("\0") if p]


@contextlib.contextmanager
def fused_aside(root, target=None):
    """Run a tree move with the worktree copies of `.fused/` paths that the
    index OR the move's `target` tracks renamed out of git's way (module
    docstring). The target's paths matter too: a file the author newly
    committed under `.fused/` is, locally, an IGNORED file fused wrote — and
    git overwrites ignored files on checkout without asking. Restores on
    every exit."""
    aside = _aside_dir(root)
    _restore_aside(root, aside)  # a crash's leftovers first
    moved = 0
    if aside:
        paths = dict.fromkeys([p for _tag, p in _tracked_fused(root)]
                              + _target_fused(root, target))
        for rel in paths:
            src = os.path.join(root, rel)
            if not os.path.lexists(src) or (os.path.isdir(src) and not os.path.islink(src)):
                continue
            dest = os.path.join(aside, rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(src, dest)
                moved += 1
            except OSError:
                logger.warning("contrib: could not set %s aside", src, exc_info=True)
    try:
        yield moved
    finally:
        if moved:
            _restore_aside(root, aside)


def protect_locked(root) -> int:
    """Set skip-worktree on every tracked `.fused/` path that lacks it; the
    caller holds the slot (it writes the index). Answers how many were newly
    protected. Restores a crashed `fused_aside` first."""
    _restore_aside(root, _aside_dir(root))
    todo = [p for tag, p in _tracked_fused(root) if tag != "S"]
    if not todo:
        return 0
    r = git(root, "update-index", "--skip-worktree", "--", *todo)
    if not ok(r):
        logger.warning("contrib: skip-worktree failed in %s: %s", root, gu._text(r))
        return 0
    # The record remembers that the bits are ours, so switching the loop off
    # can take them back (`unprotect_locked`).
    from fused_render.contrib import store

    store.update(root, lambda rec: rec.__setitem__("protected", True))
    return len(todo)


def unprotect_locked(root) -> int:
    """Clear skip-worktree on every tracked `.fused/` path — the loop was
    switched off, and with it off `git status` must report exactly what it
    did before (§0.6). The caller holds the slot. Answers how many."""
    _restore_aside(root, _aside_dir(root))
    todo = [p for tag, p in _tracked_fused(root) if tag == "S"]
    if todo:
        r = git(root, "update-index", "--no-skip-worktree", "--", *todo)
        if not ok(r):
            logger.warning("contrib: clearing skip-worktree failed in %s: %s", root, gu._text(r))
            return 0
    return len(todo)


def _protect_after_move(root) -> None:
    try:
        from fused_render.contrib import flags

        if flags.sources():
            protect_locked(root)
    except Exception:  # noqa: BLE001 — housekeeping; the next sync redoes it
        logger.exception("contrib: protect after move failed for %s", root)


# --------------------------------------------------------------------- backups


def _new_backup_ref(root) -> str:
    ms = int(time.time() * 1000)
    name = f"{BACKUP_PREFIX}{ms}"
    n = 1
    while rev(root, name):
        name = f"{BACKUP_PREFIX}{ms}-{n}"
        n += 1
    return name


# --------------------------------------------------------------- "your edits"
#
# ONE definition of Bob's uncommitted edits, shared by the engine (status's
# `dirty`, the update precheck, the Discard backup) and proposals.py ("Your
# edits", the commit a Propose sends): edits to tracked files AND untracked
# files git does not ignore (a file Bob made by hand is part of what runs,
# D16), minus fused's machine state and app_git._GITIGNORE's generated set —
# excluded here even when the app's own .gitignore forgets them (keep in
# step with that list).

EDITS_EXCLUDE = (
    ":(exclude).fused", ":(exclude).venv", ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.html.json", ":(exclude,glob)**/.claude-split.json",
    ":(exclude,glob)**/.fused-render-write-probe.*", ":(exclude,glob)**/.DS_Store",
)
#: The pathspec for `git status`/`git add -A`: the whole tree, minus the above.
EDITS_PATHSPEC = (".",) + EDITS_EXCLUDE


def edits(root) -> list[tuple[str, str]]:
    """`[(code, path)]` — porcelain v1 `XY` codes, `??` for untracked — for
    every uncommitted edit (section comment). Empty when there are none or
    git cannot say."""
    r = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            "--", *EDITS_PATHSPEC)
    if not ok(r):
        return []
    found = []
    items = iter(r[1].decode("utf-8", "replace").split("\0"))
    for item in items:
        if len(item) > 3:
            found.append((item[:2], item[3:]))
            if item[0] in "RC":
                next(items, None)  # a rename's source path follows, bare
    return found


def dirty(root) -> bool:
    return bool(edits(root))


def _run_env(root, env, *args, timeout=gu.TIMEOUT_S):
    """`git -C root …` with extra environment (a temporary index)."""
    import subprocess

    kwargs = gu._popen_kwargs()
    kwargs["env"] = {**kwargs["env"], **env}
    try:
        proc = subprocess.run([gu._git_bin(), "-C", root, *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, **kwargs)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.returncode, proc.stdout, proc.stderr


def wip_commit_locked(root, head) -> str | None:
    """A commit (parent `head`) holding every uncommitted edit — tracked
    changes, staged or not, AND untracked non-ignored files — or None when
    there are none. Built through a TEMPORARY index (a copy of the real one,
    so staged state and skip-worktree bits carry over): the real index, the
    working tree and the shared stash stack are never touched, unlike
    `git stash -u`, which would also remove the files it saves."""
    if not edits(root):
        return None
    gitdir = gu._real_gitdir(root)
    if not gitdir:
        return None
    tmp = os.path.join(gitdir, f"fused-backup-index-{uuid.uuid4().hex}")
    try:
        real_index = os.path.join(gitdir, "index")
        env = {"GIT_INDEX_FILE": tmp}
        if os.path.isfile(real_index):
            shutil.copyfile(real_index, tmp)
        elif not ok(_run_env(root, env, "read-tree", head)):
            return None
        if not ok(_run_env(root, env, "add", "-A", "--", *EDITS_PATHSPEC,
                           timeout=REPLAY_TIMEOUT_S)):
            return None
        tree = out(_run_env(root, env, "write-tree"))
        if not tree or tree == out(git(root, "rev-parse", f"{head}^{{tree}}")):
            return None
        commit = out(git(root, *identity_args(root), "commit-tree", tree, "-p", head,
                         "-m", "Backup of uncommitted edits"))
        return commit or None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def backup_locked(root, tip, *, why, app_dir=None) -> list[str]:
    """Pin `tip` (and, when there are uncommitted edits, a `wip_commit_locked`
    commit of them — untracked files included) under `refs/fused/backup/`.
    Answers the refs written (empty when even the first could not be
    written — the caller must then not move the branch). Names them on the
    record."""
    from fused_render.contrib import store

    refs = []
    ref = _new_backup_ref(root)
    if not ok(git(root, "update-ref", ref, tip)):
        return []
    refs.append(ref)
    wip = wip_commit_locked(root, tip)
    if wip and ok(git(root, "update-ref", f"{ref}-wip", wip)):
        refs.append(f"{ref}-wip")
    before = _backup_refs_named(store.get(app_dir or root))
    rec = None
    for r in refs:
        rec = store.add_backup(app_dir or root, r, why) or rec
    if rec is not None:
        # The record keeps the newest BACKUPS_KEEP; a ref whose entry just
        # fell off goes with it, so a copy's repo does not grow a ref per
        # update forever. Refs the record never named are left alone.
        for old in before - _backup_refs_named(rec):
            if old.startswith(BACKUP_PREFIX):
                git(root, "update-ref", "-d", old)
    return refs


def _unbackup_locked(root, refs, app_dir=None) -> None:
    """Take back backups whose move then did not happen: the refs, and their
    entries on the record."""
    from fused_render.contrib import store

    for r in refs:
        git(root, "update-ref", "-d", r)
    gone = set(refs)
    store.update(app_dir or root, lambda rec: rec.__setitem__(
        "backups", [b for b in rec.get("backups") or []
                    if not (isinstance(b, dict) and b.get("ref") in gone)]))


def _backup_refs_named(rec) -> set:
    return {b.get("ref") for b in (rec or {}).get("backups") or []
            if isinstance(b, dict) and isinstance(b.get("ref"), str)}


# ---------------------------------------------------------------------- replay


def _wt_gitdir(worktree) -> str | None:
    r = git(worktree, "rev-parse", "--absolute-git-dir")
    return out(r) if ok(r) else None


def _rebase_in_flight(worktree) -> bool:
    d = _wt_gitdir(worktree)
    return bool(d) and any(os.path.isdir(os.path.join(d, s))
                           for s in ("rebase-merge", "rebase-apply"))


def _unmerged(worktree) -> list[str]:
    r = git(worktree, "diff", "--name-only", "--diff-filter=U", "-z")
    if not ok(r):
        return []
    return [p for p in r[1].decode("utf-8", "replace").split("\0") if p]


def _rebase(worktree, root, onto, upstream, target):
    return git(worktree, *_REPLAY_CONFIG, *identity_args(root), "rebase",
               "--no-autosquash", "--empty=drop", "--onto", onto, upstream, target,
               timeout=REPLAY_TIMEOUT_S)


def replay(root, *, onto, upstream, branch, job_id=None, mark=None) -> dict:
    """Rebase the commits `upstream..<branch>` onto `onto` inside a scratch
    worktree (§6.2). `branch` is a branch name or a commit.

    Answers `{"ok", "tip", "conflict", "worktree", "files", "commits",
    "mark_tip"}`: `tip` is the replayed tip (on ok); `commits` the replayed
    commits oldest first; `worktree` is kept (rebase stopped in it) ONLY when
    a conflict happened and `job_id` was given — the merge job resolves and
    continues there and calls `cleanup` itself. Every other outcome cleans
    the worktree up before returning.

    `mark` (a commit inside `upstream..<branch>`, engine-only) splits the
    replay in two — `upstream..mark`, then `mark..<branch>` on top — so the
    caller learns exactly where `mark` landed (`mark_tip`): how the engine
    carries a proposal's `proposed_head` across an auto-integrate. Ignored
    with `job_id` (a job resolving a stopped first half would never replay
    the second) or when it is not between the two."""
    result = {"ok": False, "tip": None, "conflict": False, "worktree": None,
              "files": [], "commits": [], "mark_tip": None}
    target = rev(root, branch)
    onto_sha, upstream_sha = rev(root, onto), rev(root, upstream)
    if not (target and onto_sha and upstream_sha):
        return result
    # `mark` at either end needs no split: it lands on `onto` (nothing of
    # it was replayed) or on the replayed tip.
    mark = rev(root, mark) if (mark and job_id is None) else None
    mark_end = None
    if mark in (upstream_sha, target):
        mark_end, mark = mark, None
    elif mark and not (is_ancestor(root, upstream_sha, mark) and is_ancestor(root, mark, target)):
        mark = None

    path = os.path.join(work_dir(), job_id or uuid.uuid4().hex)
    if os.path.exists(path):
        cleanup(path)
    try:
        os.makedirs(work_dir(), exist_ok=True)
    except OSError:
        return result
    git(root, "worktree", "prune")
    added = git(root, "worktree", "add", "--detach", path, target, timeout=REPLAY_TIMEOUT_S)
    if not ok(added):
        logger.warning("contrib replay: worktree add failed in %s: %s", root, gu._text(added))
        cleanup(path)
        return result

    keep = False
    try:
        stages = [(onto_sha, upstream_sha, mark), (None, mark, target)] if mark \
            else [(onto_sha, upstream_sha, target)]
        mark_tip = None
        for stage_onto, stage_up, stage_target in stages:
            stage_onto = stage_onto or mark_tip
            r = _rebase(path, root, stage_onto, stage_up, stage_target)
            if not ok(r):
                files = _unmerged(path)
                if _rebase_in_flight(path) and files:
                    result.update(conflict=True, files=files)
                    if job_id is not None:
                        keep = True
                        result["worktree"] = path
                        return result
                else:
                    logger.warning("contrib replay: rebase failed in %s: %s", root, gu._text(r))
                git(path, "rebase", "--abort")
                return result
            if stage_target == mark:
                mark_tip = rev(path, "HEAD")
        tip = rev(path, "HEAD")
        if not tip or _rebase_in_flight(path):
            return result
        if mark_end is not None:
            mark_tip = onto_sha if mark_end == upstream_sha else tip
        listing = git(path, "rev-list", "--reverse", f"{onto_sha}..{tip}")
        result.update(ok=True, tip=tip, mark_tip=mark_tip,
                      commits=out(listing).split() if ok(listing) else [])
        return result
    finally:
        if not keep:
            cleanup(path)


def cleanup(worktree) -> None:
    """Remove a scratch worktree (rebase in flight or not) and prune its
    registration. Never raises."""
    if not worktree:
        return
    try:
        common = None
        if os.path.exists(os.path.join(worktree, ".git")):
            r = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
            common = out(r) if ok(r) else None
        if common:
            git(common, "worktree", "remove", "--force", "--force", worktree)
        shutil.rmtree(worktree, ignore_errors=True)
        if common:
            git(common, "worktree", "prune")
    except Exception:  # noqa: BLE001 — cleanup never raises
        logger.exception("contrib: cleanup of %s failed", worktree)


def sweep(ttl_s: float = 48 * 3600) -> int:
    """Drop scratch worktrees older than `ttl_s` (a crash's leftovers; a
    merge job keeps its own for 24 h at most). Their repos' registrations
    are pruned by the next `replay` there. Answers how many went."""
    gone = 0
    try:
        names = os.listdir(work_dir())
    except OSError:
        return 0
    cutoff = time.time() - ttl_s
    for name in names:
        p = os.path.join(work_dir(), name)
        try:
            if os.path.isdir(p) and not os.path.islink(p) and os.path.getmtime(p) < cutoff:
                cleanup(p)
                gone += 1
        except OSError:
            continue
    return gone


# ------------------------------------------------------------------------ land


def land(root, *, branch, old_tip, new_tip, why, app_dir=None) -> bool:
    """Move `branch` (checked out in `root`) from `old_tip` to `new_tip`
    (§6.2): backup, then `git reset --keep` under the slot. False — and
    nothing moved — when the slot is busy, HEAD is not on `branch`, the
    branch moved meanwhile, an operation is in flight, or an uncommitted
    edit overlaps the move. Advances `refs/fused/base` when `new_tip`
    contains the follow tip, and re-protects `.fused/` afterwards."""
    with slot() as held:
        if not held:
            return False
        return land_locked(root, branch=branch, old_tip=old_tip, new_tip=new_tip,
                           why=why, app_dir=app_dir)


def land_locked(root, *, branch, old_tip, new_tip, why, app_dir=None) -> bool:
    if gu._current_branch(root) != branch or rev(root, f"refs/heads/{branch}") != old_tip:
        return False
    if gu._operation_in_flight(root) is not None:
        return False
    new_tip = rev(root, new_tip)
    if not new_tip:
        return False
    if new_tip != old_tip:
        refs = []
        if not is_ancestor(root, old_tip, new_tip):
            refs = backup_locked(root, old_tip, why=why, app_dir=app_dir)
            if not refs:
                return False
        # The S bit is what makes a set-aside `.fused/` file invisible to
        # `reset --keep`; without it the file reads as a local deletion and
        # the move refuses. Callers outside `engine.sync` (a merge job's
        # accept, the return) have not protected yet.
        _protect_after_move(root)
        with fused_aside(root, new_tip):
            moved = git(root, "reset", "--keep", new_tip, timeout=REPLAY_TIMEOUT_S)
        if not ok(moved):
            # Nothing moved: the backup pins a tip that is still the branch.
            _unbackup_locked(root, refs, app_dir)
            logger.info("contrib land deferred in %s: %s", root, gu._text(moved))
            return False
    advance_base_locked(root, new_tip)
    _protect_after_move(root)
    return True


def advance_base_locked(root, tip) -> None:
    """Point `refs/fused/base` at the follow tip when `tip` contains it."""
    follow = rev(root, FOLLOW_REF)
    if follow and is_ancestor(root, follow, tip) and rev(root, BASE_REF) != follow:
        git(root, "update-ref", BASE_REF, follow)


def reset_hard_locked(root, *, branch, target, why, app_dir=None) -> bool:
    """Discard's move (§6.4): back up HEAD and every uncommitted edit
    (untracked files included), then `reset --hard target` and remove the
    untracked files the backup holds — "your edits" are exactly what
    `edits()` names, so a Discard leaves nothing of Bob's behind and nothing
    unsaved. Ignored files (`.venv`, caches) and `.fused/` are never touched.
    Refuses — nothing moved — when there are edits the backup failed to
    capture. The one place a real folder is hard-reset."""
    head = rev(root, "HEAD")
    if gu._current_branch(root) != branch or not head:
        return False
    if gu._operation_in_flight(root) is not None:
        return False
    untracked = {p for code, p in edits(root) if code == "??"}
    refs = backup_locked(root, head, why=why, app_dir=app_dir)
    if not refs or (dirty(root) and not any(r.endswith("-wip") for r in refs)):
        _unbackup_locked(root, refs, app_dir)
        return False
    _protect_after_move(root)  # see land_locked
    with fused_aside(root, target):
        moved = git(root, "reset", "--hard", target, timeout=REPLAY_TIMEOUT_S)
    if not ok(moved):
        logger.warning("contrib: reset --hard failed in %s: %s", root, gu._text(moved))
        return False
    # Only paths STILL untracked: one the target now tracks is the author's.
    still = {p for code, p in edits(root) if code == "??"}
    for rel in sorted(untracked & still):
        _remove_untracked(root, rel)
    advance_base_locked(root, target)
    _protect_after_move(root)
    return True


def _remove_untracked(root, rel) -> None:
    """Delete one untracked file (backed up already), then any directories it
    leaves empty, up to `root`."""
    path = os.path.join(root, rel)
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        parent = os.path.dirname(path)
        while parent.startswith(root.rstrip(os.sep) + os.sep) and not os.listdir(parent):
            os.rmdir(parent)
            parent = os.path.dirname(parent)
    except OSError:
        logger.warning("contrib: could not remove %s after discard", path, exc_info=True)
