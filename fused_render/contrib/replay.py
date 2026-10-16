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
`refs/fused/backup/<unix_ms>-<why>` (and the uncommitted edits, untracked
files included, at `…-wip` — `wip_commit_locked`). A pure fast-forward pins
nothing: its old tip stays in the new tip's history, so a backup would be
noise in every follower's repo for every author push. The record names the
last ten; the refs themselves are NEVER deleted by the engine, with one
exception (`prune_backups_locked`): a plain update's backup (`why` not
`discard`/`race`, not `-wip`) older than 30 days ages out. A Discard's and a
race's backups — the only copy of what they hold — and every `-wip` stay.

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
def slot_released():
    """Inside `slot_held` at its top level (git_upstream's caller holds the
    raw lock on THIS thread): give the slot up for a long stretch — a fetch,
    a replay — and take it back before the caller's own release. Every write
    inside then takes the slot itself (`slot()`), briefly: the review's rule
    that the slot covers only backup + final check + land, never minutes of
    rebase. The re-acquire is BOUNDED (`_reacquire_timeout`, the same
    budget as git_upstream's `_mutation_slot`): on a timeout the thread no
    longer holds the slot, so it tells git_upstream (`_mark_slot_lost`) and
    the caller's `finally` skips its release rather than freeing someone
    else's lock. Nothing is lost by it: every tree write inside took the
    slot itself and finished before this point, and `slot_lost()` lets the
    caller report the stretch as deferred. Anywhere else (nested, or not
    held) this is a no-op."""
    if _depth() != 1:
        yield
        return
    _held.depth = 0
    _held.lost = False
    gu._check_slot.release()
    gu._drain_pending()
    try:
        yield
    finally:
        if gu._check_slot.acquire(timeout=_reacquire_timeout()):
            _held.depth = 1
        else:
            logger.warning("contrib: could not take the sync slot back in time")
            gu._mark_slot_lost()
            _held.lost = True


def _reacquire_timeout() -> float:
    return gu.TIMEOUT_S + 5.0


def slot_lost() -> bool:
    """Whether this thread's last `slot_released` could not take the slot
    back (its caller then no longer holds it)."""
    return getattr(_held, "lost", False)


@contextlib.contextmanager
def slot_held():
    """Mark that the CALLER already holds the slot (git_upstream's intercept
    — `_sync_locked`'s callers acquired it), so `slot()` inside nests."""
    _held.depth = _depth() + 1
    try:
        yield
    finally:
        _held.depth = max(0, _depth() - 1)  # 0 already when slot_released lost it


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


#: Backups whose `why` keeps them forever (`prune_backups_locked`).
KEEP_FOREVER = frozenset({"discard", "race"})
#: A plain update's backup older than this may be pruned.
BACKUP_AGE_OUT_S = 30 * 24 * 3600


def _new_backup_ref(root, why="update") -> str:
    """`refs/fused/backup/<unix_ms>-<why>` (the why in the NAME, so the age-out
    sweep can tell a Discard's backup from an update's without the record)."""
    ms = int(time.time() * 1000)
    tag = "".join(c if c.isalnum() or c == "-" else "-" for c in (why or "update"))
    name = f"{BACKUP_PREFIX}{ms}-{tag}"
    n = 1
    while rev(root, name):
        name = f"{BACKUP_PREFIX}{ms}-{tag}-{n}"
        n += 1
    return name


def prune_backups_locked(root, now=None) -> int:
    """The one way a backup ref goes: a plain update's backup (not
    `discard`/`race`, not a `-wip`, nor one whose why the name does not
    tell) older than BACKUP_AGE_OUT_S. Caller holds the slot. Answers how
    many went."""
    now = time.time() if now is None else now
    r = git(root, "for-each-ref", "--format=%(refname)", BACKUP_PREFIX)
    gone = 0
    for ref in (out(r).split() if ok(r) else []):
        tail = ref[len(BACKUP_PREFIX):]
        ms, _, why = tail.partition("-")
        if not ms.isdigit() or not why or why.endswith("wip"):
            continue
        base_why = why.rsplit("-", 1)[0] if why.rsplit("-", 1)[-1].isdigit() else why
        if base_why in KEEP_FOREVER:
            continue
        if now - int(ms) / 1000.0 > BACKUP_AGE_OUT_S and ok(git(root, "update-ref", "-d", ref)):
            gone += 1
    return gone


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

    # gu._popen_kwargs() spelled out: test_git_posix_spawn reads these kwargs
    # statically, and close_fds=False keeps CPython on posix_spawn.
    try:
        proc = subprocess.run([gu._git_bin(), "-C", root, *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              env={**os.environ, **gu._ENV, **env}, timeout=timeout,
                              close_fds=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.returncode, proc.stdout, proc.stderr


def clobber_paths(root, head, target) -> list[str]:
    """Paths `target` adds over `head` that exist in the working tree as
    UNTRACKED files — ignored ones included (a `.env`, a data file): a
    `reset --keep`/`--hard` to `target` overwrites an ignored file without
    asking, so these must be in the backup before the move. `.fused/` is
    left to `fused_aside`."""
    if not head or not target:
        return []
    r = git(root, "diff", "--name-only", "-z", "--diff-filter=A", head, target, "--",
            ".", ":(exclude).fused")
    if not ok(r):
        return []
    return [p for p in r[1].decode("utf-8", "replace").split("\0")
            if p and os.path.lexists(os.path.join(root, p))]


def wip_commit_locked(root, head, *, also=()) -> str | None:
    """A commit (parent `head`) holding every uncommitted edit — tracked
    changes, staged or not, AND untracked non-ignored files — plus the paths
    in `also` force-added whatever the ignore rules say (`clobber_paths`);
    None when there is nothing to hold. Built through a TEMPORARY index (a
    copy of the real one, so staged state and skip-worktree bits carry
    over): the real index, the working tree and the shared stash stack are
    never touched, unlike `git stash -u`, which would also remove the files
    it saves."""
    if not edits(root) and not also:
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
        if also and not ok(_run_env(root, env, "add", "-f", "--", *also,
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


def backup_locked(root, tip, *, why="update", also=()) -> list[str]:
    """Pin `tip` (and, when there are uncommitted edits or `also` paths, a
    `wip_commit_locked` commit of them) under `refs/fused/backup/`. Answers
    the refs written — empty when even the first could not be written, and
    the caller must then not move the branch. NOT yet named on the record:
    `name_backups` does that once the move really happened, so a deferred
    move leaves no dead names behind."""
    refs = []
    ref = _new_backup_ref(root, why)
    if not ok(git(root, "update-ref", ref, tip)):
        return []
    refs.append(ref)
    wip = wip_commit_locked(root, tip, also=also)
    if wip and ok(git(root, "update-ref", f"{ref}-wip", wip)):
        refs.append(f"{ref}-wip")
    return refs


def name_backups(root, refs, why, app_dir=None) -> None:
    """Name the refs of a move that happened on the record (newest last,
    BACKUPS_KEEP of them). A name that falls off the list only stops being
    named — the ref stays (module docstring; `prune_backups_locked` is the
    only deleter)."""
    from fused_render.contrib import store

    for r in refs or ():
        store.add_backup(app_dir or root, r, why)


def _unbackup_locked(root, refs) -> None:
    """Take back the refs of a move that did not happen."""
    for r in refs:
        git(root, "update-ref", "-d", r)


def _backup_refs_named(rec) -> set:
    return {b.get("ref") for b in (rec or {}).get("backups") or []
            if isinstance(b, dict) and isinstance(b.get("ref"), str)}


def commit_edits_locked(root, message="Your edits") -> str | None:
    """Commit every uncommitted edit (`edits()`, the Propose pathspec) as one
    commit — the engine's answer to edits that would otherwise block an
    update forever: they are Bob's changes either way, and committed they
    ride the replay like the rest. Caller holds the slot. Answers the new
    HEAD, or None (nothing to commit / git refused)."""
    if not edits(root):
        return None
    if not ok(git(root, "add", "-A", "--", *EDITS_PATHSPEC, timeout=REPLAY_TIMEOUT_S)):
        return None
    r = git(root, *_REPLAY_CONFIG, *identity_args(root), "commit", "--no-verify", "-q",
            "-m", message)
    if not ok(r):
        logger.warning("contrib: committing edits in %s failed: %s", root, gu._text(r))
        return None
    return rev(root, "HEAD")


# ------------------------------------------------------------- tree merges
#
# `git merge-tree --write-tree` (git >= 2.38; `--merge-base` >= 2.40) merges
# in memory: no worktree, no index. It is the yardstick a finished rebase is
# held to (`expected_tree`) and the whole of the MERGE strategy
# (`merge_into`) and of `pick`. A git too old for it answers
# `supported: False` and the engine takes the conservative road
# (needs-merge, or defer).


def merge_tree(root, ours, theirs, *, base=None) -> dict:
    """`{"supported", "clean", "tree", "files"}` for a three-way merge of
    `ours` and `theirs` (on `base`, else their merge-base)."""
    # `-z`: NUL-separated, so a path is never C-quoted (core.quotePath turns
    # `café.html` into `"caf\303\251.html"` otherwise). Output: the tree,
    # then one conflicted path per field (repeated per stage), then an empty
    # field closing the section.
    args = ["merge-tree", "--write-tree", "--name-only", "--no-messages", "-z"]
    if base:
        args.append(f"--merge-base={base}")
    r = git(root, *args, ours, theirs, timeout=REPLAY_TIMEOUT_S)
    if r is None or r[0] not in (0, 1):
        return {"supported": False, "clean": False, "tree": None, "files": []}
    fields = r[1].decode("utf-8", "replace").split("\0")
    tree = fields[0].strip() if fields else None
    files = []
    if r[0] == 1:
        for f in fields[1:]:
            if not f:
                break
            files.append(f)
    return {"supported": bool(tree), "clean": r[0] == 0, "tree": tree,
            "files": list(dict.fromkeys(files))}


def tree_of(root, commit) -> str | None:
    if not commit:
        return None
    r = git(root, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}")
    return out(r) or None if ok(r) else None


def has_merges(root, upstream, head) -> bool:
    """Whether `upstream..head` holds a merge commit — a rebase would
    flatten it and drop whatever the merge itself carried. Unknown reads as
    yes (the merge strategy never loses one)."""
    r = git(root, "rev-list", "--merges", "--count", f"{upstream}..{head}")
    try:
        return int(out(r)) > 0 if ok(r) else True
    except ValueError:
        return True


def merge_into(root, *, head, onto, message) -> dict:
    """The MERGE strategy: a merge commit with parents `head` and `onto`,
    built in memory. `{"ok", "tip", "conflict", "files", "supported"}`."""
    mt = merge_tree(root, head, onto)
    res = {"ok": False, "tip": None, "conflict": False, "files": mt["files"],
           "supported": mt["supported"], "mark_tip": None}
    if not mt["supported"]:
        return res
    if not mt["clean"]:
        res["conflict"] = True
        return res
    c = out(git(root, *identity_args(root), "commit-tree", mt["tree"], "-p", head,
                "-p", onto, "-m", message))
    if c:
        res.update(ok=True, tip=c)
    return res


def pick(root, *, onto, commits) -> dict:
    """Re-apply exactly `commits` (oldest first) onto `onto`, in memory — a
    rebase of a hand-picked list (A4's return drops the commits the author
    already took, by patch-id). Each keeps its author and message; one that
    becomes empty is dropped. `{"ok", "tip", "conflict", "files",
    "supported"}`."""
    cur = rev(root, onto)
    res = {"ok": False, "tip": None, "conflict": False, "files": [], "supported": True,
           "mark_tip": None}
    if not cur:
        return res
    for c in commits:
        parent = rev(root, f"{c}^")
        mt = merge_tree(root, cur, c, base=parent)
        if not mt["supported"]:
            res["supported"] = False
            return res
        if not mt["clean"]:
            res.update(conflict=True, files=mt["files"])
            return res
        if mt["tree"] == tree_of(root, cur):
            continue
        meta = out(git(root, "log", "-1", "--date=raw", "--format=%an%x00%ae%x00%ad", c))
        parts = meta.split("\0")
        if len(parts) != 3:
            return res
        msg = git(root, "log", "-1", "--format=%B", c)
        message = msg[1].decode("utf-8", "replace") if ok(msg) else "Your edit"
        env = {"GIT_AUTHOR_NAME": parts[0], "GIT_AUTHOR_EMAIL": parts[1],
               "GIT_AUTHOR_DATE": parts[2]}
        made = _run_env(root, env, *identity_args(root), "commit-tree", mt["tree"],
                        "-p", cur, "-m", message)
        cur = out(made) if ok(made) else None
        if not cur:
            return res
    res.update(ok=True, tip=cur)
    return res


def expected_tree(root, *, upstream, onto, head) -> dict:
    """What replaying `upstream..head` onto `onto` MUST produce: the
    three-way merge of `onto` and `head` on `upstream` (`merge_tree`)."""
    return merge_tree(root, onto, head, base=upstream)


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
        if job_id is None and _replay_unsound(root, upstream_sha, onto_sha, target, tip, result):
            return result
        listing = git(path, "rev-list", "--reverse", f"{onto_sha}..{tip}")
        result.update(ok=True, tip=tip, mark_tip=mark_tip,
                      commits=out(listing).split() if ok(listing) else [])
        return result
    finally:
        if not keep:
            cleanup(path)


def _replay_unsound(root, upstream, onto, target, tip, result) -> bool:
    """A clean rebase is held to `expected_tree` before anyone may land it:
    a rebase silently flattens a merge commit in the range (and whatever
    hand resolution it carried), so a tip whose tree is not the three-way
    merge of the same two sides is NOT "your changes kept" — it is a
    decision for the page (`conflict` + `mismatch`). A git too old for
    `merge-tree --merge-base` can only trust a range with no merges."""
    exp = expected_tree(root, upstream=upstream, onto=onto, head=target)
    if exp["supported"]:
        bad = not exp["clean"] or exp["tree"] != tree_of(root, tip)
    else:
        bad = has_merges(root, upstream, target)
    if bad:
        logger.info("contrib replay in %s: result differs from the three-way merge", root)
        result.update(conflict=True, mismatch=True, files=exp["files"])
    return bad


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
    """`land`'s body; the caller holds the slot.

    THE RACE (review r02). Nothing that commits into an app folder — a Claude
    turn's own `git commit`, app_git's sweep — holds the slot, so a commit
    can land between the "branch still at old_tip" check below and the
    `reset --keep`. reset then moves from THAT commit, reverting its files,
    and only the reflog would remember it. So after the reset, `ORIG_HEAD`
    (the tip reset actually moved from) is compared with `old_tip`: a
    mismatch pins it under `refs/fused/backup/` and resets straight back to
    it — the exact state the racing writer left — and the land defers."""
    if gu._current_branch(root) != branch or rev(root, f"refs/heads/{branch}") != old_tip:
        return False
    if gu._operation_in_flight(root) is not None:
        return False
    new_tip = rev(root, new_tip)
    if not new_tip:
        return False
    refs = []
    if new_tip != old_tip:
        clobber = clobber_paths(root, old_tip, new_tip)
        if clobber or not is_ancestor(root, old_tip, new_tip):
            refs = backup_locked(root, old_tip, why=why, also=clobber)
            if not refs or (clobber and not any(r.endswith("-wip") for r in refs)):
                _unbackup_locked(root, refs)
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
            _unbackup_locked(root, refs)
            logger.info("contrib land deferred in %s: %s", root, gu._text(moved))
            return False
        if not _undo_race_locked(root, old_tip, new_tip, refs, app_dir):
            return False
    name_backups(root, refs, why, app_dir)
    advance_base_locked(root, new_tip)
    _protect_after_move(root)
    return True


def _undo_race_locked(root, old_tip, new_tip, refs, app_dir, *, hard=False) -> bool:
    """True when the reset to `new_tip` really moved from `old_tip`. Else a
    commit raced in (land_locked's docstring): pin it, put the branch back
    on it, take back the planned backups, answer False (defer)."""
    moved_from = rev(root, "ORIG_HEAD")
    if moved_from == old_tip:
        return True
    logger.warning("contrib: a commit landed in %s during an update (%s); putting it back",
                   root, moved_from)
    if moved_from:
        pin = _new_backup_ref(root, "race")
        if ok(git(root, "update-ref", pin, moved_from)):
            name_backups(root, [pin], "race", app_dir)
        with fused_aside(root, moved_from):
            back = git(root, "reset", "--keep", moved_from, timeout=REPLAY_TIMEOUT_S)
        if not ok(back):
            logger.error("contrib: could not put %s back on %s (pinned at %s): %s",
                         root, moved_from, pin, gu._text(back))
            name_backups(root, refs, "race", app_dir)  # keep everything
            return False
    if hard:
        # A hard reset already dropped the uncommitted edits: their only
        # copy is the planned backup's -wip, which therefore stays.
        name_backups(root, refs, "race", app_dir)
    else:
        _unbackup_locked(root, refs)
    return False


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
    clobber = clobber_paths(root, head, target)
    refs = backup_locked(root, head, why=why, also=clobber)
    if not refs or ((dirty(root) or clobber) and not any(r.endswith("-wip") for r in refs)):
        _unbackup_locked(root, refs)
        return False
    _protect_after_move(root)  # see land_locked
    with fused_aside(root, target):
        moved = git(root, "reset", "--hard", target, timeout=REPLAY_TIMEOUT_S)
    if not ok(moved):
        logger.warning("contrib: reset --hard failed in %s: %s", root, gu._text(moved))
        name_backups(root, refs, why, app_dir)  # a failed hard reset may be partial
        return False
    if not _undo_race_locked(root, head, target, refs, app_dir, hard=True):
        return False
    name_backups(root, refs, why, app_dir)
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
