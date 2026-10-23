"""The tree primitives every contrib mutation is built from
(docs/CONTRIB_LOOP.md §0.2, §0.3, §16): merge in memory (or in a scratch
worktree), land a result with a backup, clean up — plus the mutation slot
and the committed-`.fused/` guard they share.

v3 (§17): a GitHub-link copy's work lives on `fused/mine` (`HOME_BRANCH`)
and is MERGED, never rebased; its moves are `land_ff_locked` (bring in,
Merge with Claude's accept) and `settle_locked` (a merged proposal), each one
git command with git's own guard. `land_locked`, `undo_race_locked`,
`reset_hard_locked`, `pick` and `merge_into` stay for own copies (ownsync)
and Discard. The v1 rebase machinery is gone; the four v1 ref names stay
below only so the first v3 sync can delete them (`V1_REFS`).

THE REAL APP FOLDER NEVER HAS A MERGE IN FLIGHT (§0.3). A three-way merge
runs in memory (`git merge-tree --write-tree`, git >= 2.38; `--merge-base`
>= 2.40) or, on an older git, in a scratch `git worktree` under
`<home>/contrib/work/<id>/` (`_merge_tree_scratch`: same answer, same shape)
— outside the workspace, so `app_git.commit` and the Claude-turn sweep never
commit there. Only a finished commit leaves it, through `land`, which moves
the branch with `git reset --keep`: it refuses (rather than clobbers) when an
uncommitted edit overlaps what the move would change, and a refusal is
"defer, try next time" (§0.2), never an error.

NOTHING OF BOB'S IS LOST SILENTLY (§0.2). Before `land` moves a branch off a
tip the new one does not contain — or over a local file the new tip adds —
the old tip is pinned at `refs/fused/backup/<unix_ms>-<why>` (and the
uncommitted edits, untracked files included, at `…-wip` —
`wip_commit_locked`). A fast-forward or a merge commit whose first parent is
HEAD drops nothing, but an automatic one still pins the old tip
(`undoable=True`) so its Activity line can offer Undo. `land` answers
the ref it wrote so the engine can name it on the Activity line that offers
Undo. The refs are NEVER deleted while an activity line references them;
`prune_backups_locked` drops only unreferenced ones older than 30 days.

COMMITTED `.fused/` (§4). A repo that tracks its own `.fused/` would show
fused's writes there (the install stamp, `meta.json`) as edits, so tracked
`.fused/*` paths get git's skip-worktree bit (`protect_locked`) — and keep it
(§16.1: never un-protected). That hides them from `status` — but it does NOT
let a tree move through: verified on git 2.39–2.50, `merge --ff-only`,
`reset --keep` AND `reset --hard` all refuse with "Entry '.fused/…' not
uptodate" the moment the author's update changes a file whose local copy
fused has rewritten. So every tree move runs inside `fused_aside`: the
worktree copies of tracked `.fused/` paths are renamed into
`<gitdir>/fused-aside/` (same filesystem, no copy), the move runs, and they
are renamed back over whatever git checked out. Machine state wins locally;
the index carries the author's version, hidden by the bit. A crash between
the two renames is recovered by the next `fused_aside` or `protect_locked`.

THE SLOT (§0.9). Every write to a real app repo holds
`git_upstream._check_slot`, the same process-wide lock auto-sync's fetches
use, so two git writers never meet in one repo. It is a plain `Lock`;
`slot()` makes nesting EXPLICIT: the one entry point called with the slot
already held (`engine.sync`, from git_upstream's intercept) wraps its body in
`slot_held()`, and every `slot()` inside that on the same thread is a no-op
that answers True. `slot_released` gives it up for a long stretch (a Claude
refresh, a merge) and takes it back, bounded. Merges and picks need no slot:
they write only objects (safe under concurrent writers) and a scratch
worktree's own admin dir.

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

# v1 private refs (§16.0 P3): deleted on the first v2 sync of a repo and
# never written again. Named here only for that cleanup.
FOLLOW_REF = "refs/fused/follow"
BASE_REF = "refs/fused/base"
PROPOSED_REF = "refs/fused/proposed"
ORIGIN_SEEN_REF = "refs/fused/origin-seen"
V1_REFS = (FOLLOW_REF, BASE_REF, PROPOSED_REF, ORIGIN_SEEN_REF)

BACKUP_PREFIX = "refs/fused/backup/"

#: A merge may lazily fetch blobs (installs are `--filter=blob:none`
#: clones): more headroom than one fetch.
REPLAY_TIMEOUT_S = 180.0

# Config for every merge/commit fused runs: no hooks (the user's global hooks
# are not ours to fire for a background merge), no signing prompt, zdiff3
# markers (what a Merge with Claude job reads), no rerere replaying a stale
# resolution, no gc mid-flight.
COMMIT_CONFIG = (
    "-c", "core.hooksPath=" + os.devnull,
    "-c", "commit.gpgsign=false",
    "-c", "merge.conflictStyle=zdiff3",
    "-c", "rerere.enabled=false",
    "-c", "gc.auto=0",
)

_FUSED_IDENTITY = ("-c", "user.name=Fused", "-c", "user.email=apps@fused.io")

#: Tests flip this to exercise the old-git path on a new git.
_FORCE_SCRATCH_MERGE = False


# ------------------------------------------------------------------- git bits


#: Before every git call fused makes on a contrib repo (§16.12): the repo's
#: own `.git/config` is app-writable, and `core.fsmonitor` / `core.hooksPath`
#: there would run its commands from fused's own reads and checkouts.
SAFE_CONFIG = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=" + os.devnull)


def git(root, *args, timeout=gu.TIMEOUT_S):
    """`git_upstream._run` with SAFE_CONFIG — one bounded call,
    `(rc, out, err)` or None."""
    return gu._run(root, *SAFE_CONFIG, *args, timeout=timeout)


def ok(result) -> bool:
    return gu._ok(result)


def out(result) -> str:
    return gu._out(result)


def rev(root, spec) -> str | None:
    """The commit `spec` names, or None."""
    if not spec:
        return None
    r = git(root, "rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}")
    sha = out(r) if ok(r) else ""
    return sha or None


def is_ancestor(root, a, b) -> bool:
    """Whether commit `a` is in `b`'s history (a == b counts)."""
    if not a or not b:
        return False
    return ok(git(root, "merge-base", "--is-ancestor", a, b))


def count(root, *spec) -> int | None:
    r = git(root, "rev-list", "--count", *spec)
    try:
        return int(out(r)) if ok(r) else None
    except ValueError:
        return None


def tree_of(root, commit) -> str | None:
    if not commit:
        return None
    r = git(root, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}")
    return (out(r) or None) if ok(r) else None


def names(result) -> list[str]:
    """The NUL-separated paths of a `-z` listing."""
    if not ok(result):
        return []
    return [p for p in result[1].decode("utf-8", "replace").split("\0") if p]


def identity_args(root, identity=None) -> tuple:
    """Committer (and author) identity for every commit fused makes in a
    copy: `identity` when the caller names one (`{"name", "email"}` — an own
    copy's author, from GitHub), else the repo-LOCAL `user.name`/`user.email`
    classification sets for a `github` copy (Bob's GitHub name + noreply,
    §16.1), else the `Fused` identity app_git commits with. Never the
    machine's global identity — a work laptop's `bob@corp.example` must not
    ride into a proposal."""
    if isinstance(identity, dict) and identity.get("name") and identity.get("email"):
        return ("-c", f"user.name={identity['name']}", "-c", f"user.email={identity['email']}")
    email = out(git(root, "config", "--local", "--get", "user.email"))
    name = out(git(root, "config", "--local", "--get", "user.name"))
    if email and name:
        return ("-c", f"user.name={name}", "-c", f"user.email={email}")
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
    raw lock on THIS thread): give the slot up for a long stretch — a
    GitHub refresh, a merge — and take it back before the caller's own
    release. Every write inside takes the slot itself (`slot()`), briefly.
    The re-acquire is BOUNDED (the budget of git_upstream's
    `_mutation_slot`): on a timeout the thread no longer holds the slot, so
    it tells git_upstream (`_mark_slot_lost`) and the caller's `finally`
    skips its release rather than freeing someone else's lock; `slot_lost()`
    lets the caller report the stretch as deferred. Anywhere else (nested,
    or not held) this is a no-op."""
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
        if gu._check_slot.acquire(timeout=gu.TIMEOUT_S + 5.0):
            _held.depth = 1
        else:
            logger.warning("contrib: could not take the sync slot back in time")
            gu._mark_slot_lost()
            _held.lost = True


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
    entries = []
    for item in names(r):
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
    return names(git(root, "ls-tree", "-r", "-z", "--name-only", target, "--", ".fused"))


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
    protected. Restores a crashed `fused_aside` first. Never undone (§16.1)."""
    _restore_aside(root, _aside_dir(root))
    todo = [p for tag, p in _tracked_fused(root) if tag != "S"]
    if not todo:
        return 0
    r = git(root, "update-index", "--skip-worktree", "--", *todo)
    if not ok(r):
        logger.warning("contrib: skip-worktree failed in %s: %s", root, gu._text(r))
        return 0
    return len(todo)


def protect_after_move(root) -> None:
    try:
        protect_locked(root)
    except Exception:  # noqa: BLE001 — housekeeping; the next sync redoes it
        logger.exception("contrib: protect after move failed for %s", root)


# --------------------------------------------------------------- "your edits"
#
# ONE definition of Bob's uncommitted edits, shared by the engine (status's
# `dirty`, the update precheck, the Discard backup) and proposals.py: edits
# to tracked files AND untracked files git does not ignore, minus fused's
# machine state and app_git._GITIGNORE's generated set — excluded here even
# when the app's own .gitignore forgets them (keep in step with that list).

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


def tracked_edits(root) -> list[str]:
    """Paths of uncommitted edits to TRACKED files (staged or not, a staged
    new file included) — never an untracked file (§16.3 status, row 6)."""
    return [p for code, p in edits(root) if code != "??"]


def dirty(root) -> bool:
    return bool(edits(root))


def _run_env(root, env, *args, timeout=gu.TIMEOUT_S):
    """`git -C root …` with extra environment (a temporary index)."""
    import subprocess

    # gu._popen_kwargs() spelled out: test_git_posix_spawn reads these kwargs
    # statically, and close_fds=False keeps CPython on posix_spawn.
    try:
        proc = subprocess.run([gu._git_bin(), "-C", root, *SAFE_CONFIG, *args],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              env={**os.environ, **gu._ENV, **env}, timeout=timeout,
                              close_fds=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.returncode, proc.stdout, proc.stderr


def clobber_paths(root, head, target) -> list[str]:
    """Paths `target` adds over `head` that exist in the working tree but
    are not tracked — ignored ones included (a `.env`, a data file): a
    `reset --keep`/`--hard` to `target` overwrites an ignored file without
    asking. Row 5 of the engine's table; `.fused/` is left to `fused_aside`."""
    if not head or not target:
        return []
    r = git(root, "diff", "--name-only", "-z", "--diff-filter=A", head, target, "--",
            ".", ":(exclude).fused")
    staged = set(names(git(root, "diff", "--cached", "--name-only", "-z")))
    return [p for p in names(r)
            if p not in staged and os.path.lexists(os.path.join(root, p))]


def commit_edits_locked(root, paths, message="Your edits", *, identity=None) -> str | None:
    """Commit the uncommitted edits to `paths` — the ones standing in an
    update's way (the engine's row 6), NEVER the rest of the tree: an
    untracked `creds.json` only Bob made, or anything else he staged, stays
    out (`commit --only`, literal pathspecs). Bob's identity
    (`identity_args(root, identity)`). Caller holds the slot. Answers the new HEAD, or None
    (nothing to commit / git refused)."""
    wanted = set(paths or ())
    paths = [p for _code, p in edits(root) if p in wanted]
    if not paths:
        return None
    if not ok(git(root, "--literal-pathspecs", "add", "-A", "--", *paths,
                  timeout=REPLAY_TIMEOUT_S)):
        return None
    r = git(root, "--literal-pathspecs", *COMMIT_CONFIG, *identity_args(root, identity),
            "commit",
            "--only", "--no-verify", "-q", "-m", message, "--", *paths)
    if not ok(r):
        logger.warning("contrib: committing edits in %s failed: %s", root, gu._text(r))
        return None
    return rev(root, "HEAD")


# --------------------------------------------------------------------- backups


def _new_backup_ref(root, why="update") -> str:
    """`refs/fused/backup/<unix_ms>-<why>` (the why in the NAME)."""
    ms = int(time.time() * 1000)
    tag = "".join(c if c.isalnum() or c == "-" else "-" for c in (why or "update"))
    name = f"{BACKUP_PREFIX}{ms}-{tag}"
    n = 1
    while rev(root, name):
        name = f"{BACKUP_PREFIX}{ms}-{tag}-{n}"
        n += 1
    return name


def wip_commit_locked(root, head, *, also=()) -> str | None:
    """A commit (parent `head`) holding every uncommitted edit — tracked
    changes, staged or not, AND untracked non-ignored files — plus the paths
    in `also` force-added whatever the ignore rules say (`clobber_paths`);
    None when there is nothing to hold. Built through a TEMPORARY index (a
    copy of the real one, so staged state and skip-worktree bits carry
    over): the real index, the working tree and the shared stash stack are
    never touched, unlike `git stash -u`."""
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
        if not tree or tree == tree_of(root, head):
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
    `wip_commit_locked` commit of them at `<ref>-wip`) under
    `refs/fused/backup/`. Answers the refs written — empty when even the
    first could not be written, and the caller must then not move."""
    refs = []
    ref = _new_backup_ref(root, why)
    if not ok(git(root, "update-ref", ref, tip)):
        return []
    refs.append(ref)
    wip = wip_commit_locked(root, tip, also=also)
    if wip and ok(git(root, "update-ref", f"{ref}-wip", wip)):
        refs.append(f"{ref}-wip")
    return refs


def unbackup_locked(root, refs) -> None:
    """Take back the refs of a move that did not happen."""
    for r in refs:
        git(root, "update-ref", "-d", r)


#: Backups whose `why` keeps them forever (the pin of a commit that raced an
#: update in: nothing names it, and it may be the only copy of that commit).
KEEP_FOREVER = frozenset({"race"})
#: An unreferenced backup older than this may be pruned (§16.3).
BACKUP_AGE_OUT_S = 30 * 24 * 3600


def prune_backups_locked(root, keep=(), now=None) -> int:
    """The one way a backup ref goes: not in `keep` (the refs an Activity
    line still references — a `-wip` rides with its tip), not a race pin,
    and older than BACKUP_AGE_OUT_S. Caller holds the slot. Answers how many
    went."""
    now = time.time() if now is None else now
    keep = set(keep or ())
    keep |= {k + "-wip" for k in keep}
    r = git(root, "for-each-ref", "--format=%(refname)", BACKUP_PREFIX)
    gone = 0
    for ref in (out(r).split() if ok(r) else []):
        if ref in keep:
            continue
        tail = ref[len(BACKUP_PREFIX):]
        ms, _, why = tail.partition("-")
        if not ms.isdigit():
            continue
        if why.endswith("-wip"):
            why = why[:-4]
        if why.rsplit("-", 1)[-1].isdigit():
            why = why.rsplit("-", 1)[0]
        if why in KEEP_FOREVER:
            continue
        if now - int(ms) / 1000.0 > BACKUP_AGE_OUT_S and ok(git(root, "update-ref", "-d", ref)):
            gone += 1
    return gone


# ------------------------------------------------------------- tree merges
#
# `git merge-tree --write-tree` merges in memory: no worktree, no index. A
# git too old for it (< 2.38, or < 2.40 for `--merge-base`) does the same
# merge in a scratch worktree (§16.0 P9) — same result, same answer shape.


def _merge_tree_mem(root, ours, theirs, base):
    """`merge-tree --write-tree`'s answer, or None when this git cannot."""
    # `-z`: NUL-separated, so a path is never C-quoted. Output: the tree,
    # then one conflicted path per field (repeated per stage), then an empty
    # field closing the section.
    args = ["merge-tree", "--write-tree", "--name-only", "--no-messages", "-z"]
    if base:
        args.append(f"--merge-base={base}")
    r = git(root, *args, ours, theirs, timeout=REPLAY_TIMEOUT_S)
    if r is None or r[0] not in (0, 1):
        return None
    fields = r[1].decode("utf-8", "replace").split("\0")
    tree = fields[0].strip() if fields else None
    if not tree:
        return None
    files = []
    if r[0] == 1:
        for f in fields[1:]:
            if not f:
                break
            files.append(f)
    return {"supported": True, "clean": r[0] == 0, "tree": tree,
            "files": list(dict.fromkeys(files))}


def add_worktree(root, at, name=None) -> str | None:
    """A scratch worktree of `root` checked out (detached) at `at`, under
    `work_dir()`; its path, or None. The caller `cleanup`s it."""
    path = os.path.join(work_dir(), name or uuid.uuid4().hex)
    if os.path.exists(path):
        cleanup(path)
    try:
        os.makedirs(work_dir(), exist_ok=True)
    except OSError:
        return None
    git(root, "worktree", "prune")
    added = git(root, "worktree", "add", "--detach", "-q", path, at, timeout=REPLAY_TIMEOUT_S)
    if not ok(added):
        logger.warning("contrib: worktree add failed in %s: %s", root, gu._text(added))
        cleanup(path)
        return None
    return path


def unmerged(worktree) -> list[str]:
    return list(dict.fromkeys(names(git(worktree, "diff", "--name-only", "--diff-filter=U",
                                        "-z"))))


def _merge_tree_scratch(root, ours, theirs, base):
    """The same three-way merge in a scratch worktree, for an old git:
    `merge-recursive <base> -- HEAD <theirs>` (a plain `merge --no-commit`
    when no base is named), every path added, `write-tree` — markers and
    all, like merge-tree's tree."""
    wt = add_worktree(root, ours)
    res = {"supported": False, "clean": False, "tree": None, "files": []}
    if not wt:
        return res
    try:
        if base:
            r = git(wt, *COMMIT_CONFIG, "merge-recursive", base, "--", "HEAD", theirs,
                    timeout=REPLAY_TIMEOUT_S)
        else:
            r = git(wt, *COMMIT_CONFIG, *identity_args(root), "merge", "--no-commit",
                    "--no-ff", "-q", theirs, timeout=REPLAY_TIMEOUT_S)
        if r is None:
            return res
        files = unmerged(wt)
        if not ok(r) and not files:
            logger.warning("contrib: scratch merge failed in %s: %s", root, gu._text(r))
            return res
        if not ok(git(wt, "add", "-A", timeout=REPLAY_TIMEOUT_S)):
            return res
        tree = out(git(wt, "write-tree"))
        if tree:
            res.update(supported=True, clean=not files, tree=tree, files=files)
        return res
    finally:
        cleanup(wt)


def merge_tree(root, ours, theirs, *, base=None) -> dict:
    """`{"supported", "clean", "tree", "files"}` for a three-way merge of
    `ours` and `theirs` (on `base`, else their merge-base). `supported`
    False only when neither way could answer."""
    if not _FORCE_SCRATCH_MERGE:
        mem = _merge_tree_mem(root, ours, theirs, base)
        if mem is not None:
            return mem
    return _merge_tree_scratch(root, ours, theirs, base)


def merge_into(root, *, head, onto, message, identity=None) -> dict:
    """A merge commit with parents `head` and `onto` (§16.3 row 11), built
    without touching the folder, as Bob (`identity_args`).
    `{"ok", "tip", "conflict", "files", "supported"}`."""
    mt = merge_tree(root, head, onto)
    res = {"ok": False, "tip": None, "conflict": False, "files": mt["files"],
           "supported": mt["supported"]}
    if not mt["supported"]:
        return res
    if not mt["clean"]:
        res["conflict"] = True
        return res
    c = out(git(root, *identity_args(root, identity), "commit-tree", mt["tree"], "-p", head,
                "-p", onto, "-m", message))
    if c:
        res.update(ok=True, tip=c)
    return res


def pick(root, *, onto, commits, identity=None) -> dict:
    """Re-apply exactly `commits` (oldest first) onto `onto` — A4's return
    (§16.3 row 7) keeps only the edits made after sending. Each is the
    three-way merge a cherry-pick does (its parent as the base), in memory
    or in scratch (`merge_tree`), never in the real folder; each keeps its
    author, date and message; one whose change is already there comes out
    empty and is dropped. `{"ok", "tip", "conflict", "files", "supported",
    "kept"}` — `kept` the number of commits that survived."""
    cur = rev(root, onto)
    res = {"ok": False, "tip": None, "conflict": False, "files": [], "supported": True,
           "kept": 0}
    if not cur:
        return res
    kept = 0
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
        made = _run_env(root, env, *identity_args(root, identity), "commit-tree", mt["tree"],
                        "-p", cur, "-m", message)
        cur = out(made) if ok(made) else None
        if not cur:
            return res
        kept += 1
    res.update(ok=True, tip=cur, kept=kept)
    return res


# ------------------------------------------------------------ scratch trees


def cleanup(worktree) -> None:
    """Remove a scratch worktree (a merge in flight or not) and prune its
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
    merge job keeps its own for 24 h at most). Answers how many went."""
    gone = 0
    try:
        listing = os.listdir(work_dir())
    except OSError:
        return 0
    cutoff = time.time() - ttl_s
    for name in listing:
        p = os.path.join(work_dir(), name)
        try:
            if os.path.isdir(p) and not os.path.islink(p) and os.path.getmtime(p) < cutoff:
                cleanup(p)
                gone += 1
        except OSError:
            continue
    return gone


# ------------------------------------------------------------------------ land


def land(root, *, branch, old_tip, new_tip, why) -> tuple[str | None, str | None]:
    """Move `branch` (checked out in `root`) from `old_tip` to `new_tip`:
    backup when the move can drop something, then `git reset --keep` under
    the slot. `(None, backup_ref|None)` when it moved; else `(why, None)`
    and nothing moved — `slot-busy` or `land_locked`'s reasons."""
    with slot() as held:
        if not held:
            return "slot-busy", None
        return land_locked(root, branch=branch, old_tip=old_tip, new_tip=new_tip, why=why)


def land_locked(root, *, branch, old_tip, new_tip, why,
                undoable=False) -> tuple[str | None, str | None]:
    """`land`'s body; the caller holds the slot. `(None, backup)` when the
    branch moved to `new_tip` (`backup` None for a move that keeps the old
    tip in history and overwrites no local file — unless `undoable`: an
    automatic move whose Activity line offers Undo pins the old tip even
    then), else `(reason, None)`:
    `moved` (the branch is not at `old_tip`, or a commit raced in),
    `in-progress` (an operation in flight) or `reset-refused` (the backup or
    `reset --keep` failed — an uncommitted edit overlapping the move).

    THE RACE (review r02). Nothing that commits into an app folder — a Claude
    turn's own `git commit`, app_git's sweep — holds the slot, so a commit
    can land between the "branch still at old_tip" check and the `reset
    --keep`. reset then moves from THAT commit, and only the reflog would
    remember it. So after the reset `ORIG_HEAD` is compared with `old_tip`:
    a mismatch pins it under `refs/fused/backup/` and resets straight back
    to it, and the land defers."""
    if gu._current_branch(root) != branch or rev(root, f"refs/heads/{branch}") != old_tip:
        return "moved", None
    if gu._operation_in_flight(root) is not None:
        return "in-progress", None
    new_tip = rev(root, new_tip)
    if not new_tip:
        return "moved", None
    refs = []
    if new_tip != old_tip:
        clobber = clobber_paths(root, old_tip, new_tip)
        if clobber or undoable or not is_ancestor(root, old_tip, new_tip):
            refs = backup_locked(root, old_tip, why=why, also=clobber)
            if not refs or (clobber and not any(r.endswith("-wip") for r in refs)):
                unbackup_locked(root, refs)
                return "reset-refused", None
        # The S bit is what makes a set-aside `.fused/` file invisible to
        # `reset --keep`; without it the file reads as a local deletion and
        # the move refuses.
        protect_after_move(root)
        with fused_aside(root, new_tip):
            moved = git(root, "reset", "--keep", new_tip, timeout=REPLAY_TIMEOUT_S)
        if not ok(moved):
            unbackup_locked(root, refs)
            logger.info("contrib land deferred in %s: %s", root, gu._text(moved))
            return "reset-refused", None
        if not undo_race_locked(root, old_tip, refs):
            return "moved", None
    protect_after_move(root)
    return None, (refs[0] if refs else None)


def undo_race_locked(root, old_tip, refs, *, hard=False) -> bool:
    """True when the reset really moved from `old_tip`. Else a commit raced
    in (land_locked's docstring): pin it, put the branch back on it, take
    back the planned backups (a hard reset keeps them: their `-wip` is the
    only copy of the edits it dropped), answer False (defer)."""
    moved_from = rev(root, "ORIG_HEAD")
    if moved_from == old_tip:
        return True
    logger.warning("contrib: a commit landed in %s during an update (%s); putting it back",
                   root, moved_from)
    if moved_from:
        pin = _new_backup_ref(root, "race")
        git(root, "update-ref", pin, moved_from)
        with fused_aside(root, moved_from):
            back = git(root, "reset", "--keep", moved_from, timeout=REPLAY_TIMEOUT_S)
        if not ok(back):
            logger.error("contrib: could not put %s back on %s (pinned at %s): %s",
                         root, moved_from, pin, gu._text(back))
            try:
                from fused_render.contrib import store

                store.add_activity(root, "race", "A change saved while fused was updating "
                                                 "this app couldn't be put back in place. "
                                                 "It is kept: Restore brings it back.",
                                   backup=pin)
            except Exception:  # noqa: BLE001 — the pin itself already keeps the change
                logger.exception("contrib: race activity line failed for %s", root)
            return False
    if not hard:
        unbackup_locked(root, refs)
    return False


def reset_hard_locked(root, *, branch, target, why) -> tuple[bool, str | None]:
    """Use-theirs / Discard / Restore's move: back up HEAD and every
    uncommitted edit (untracked files included), then `reset --hard target`
    and remove the untracked files the backup holds — "your edits" are
    exactly what `edits()` names, so nothing of Bob's is left behind and
    nothing unsaved. Ignored files (`.venv`, caches) and `.fused/` are never
    touched. Refuses — nothing moved — when there are edits the backup
    failed to capture. `(ok, backup_ref)`; the caller holds the slot."""
    head = rev(root, "HEAD")
    if gu._current_branch(root) != branch or not head:
        return False, None
    if gu._operation_in_flight(root) is not None:
        return False, None
    untracked = {p for code, p in edits(root) if code == "??"}
    clobber = clobber_paths(root, head, target)
    refs = backup_locked(root, head, why=why, also=clobber)
    if not refs or ((dirty(root) or clobber) and not any(r.endswith("-wip") for r in refs)):
        unbackup_locked(root, refs)
        return False, None
    protect_after_move(root)
    with fused_aside(root, target):
        moved = git(root, "reset", "--hard", target, timeout=REPLAY_TIMEOUT_S)
    if not ok(moved):
        logger.warning("contrib: reset --hard failed in %s: %s", root, gu._text(moved))
        return False, refs[0]  # a failed hard reset may be partial: the backup stays
    if not undo_race_locked(root, head, refs, hard=True):
        return False, refs[0]
    # Only paths STILL untracked: one the target now tracks is the author's.
    still = {p for code, p in edits(root) if code == "??"}
    for rel in sorted(untracked & still):
        _remove_untracked(root, rel)
    protect_after_move(root)
    return True, refs[0]


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
        logger.warning("contrib: could not remove %s", path, exc_info=True)


# Private spellings ownsync used before these went public.
_protect_after_move = protect_after_move
_unbackup_locked = unbackup_locked
_undo_race_locked = undo_race_locked


# ------------------------------------------------------- v3 moves (§17.3)
#
# A `github` copy's own branch is `fused/mine` (`HOME_BRANCH`, I4); its
# `refs/heads/<b>` is a fetch-only mirror of the author's branch (I3). Both
# moves below are ONE git command each with git's own guard: `merge
# --ff-only` refuses unless HEAD is in the new tip's history (a commit that
# raced in makes it refuse — nothing to undo), and `update-ref <ref> <new>
# <old>` refuses unless the ref is still at `old`.

def _guard_module():
    """`templates/shared/contrib_guard.py` (§17.8, stdlib-only, loaded by
    path: templates/shared is not a package) — the one copy of
    `HOME_BRANCH`; `classify` loads the same file."""
    import importlib.util
    import sys

    name = "fused_render_contrib_guard"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "shared", "contrib_guard.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[name] = mod
    return mod


#: The contributor's own branch on a `github` copy (§17.1 I4), from the
#: guard module (`classify.HOME_BRANCH` is the same value).
HOME_BRANCH = _guard_module().HOME_BRANCH
HOME_REF = "refs/heads/" + HOME_BRANCH


def land_ff_locked(root, new_tip) -> tuple[str | None, None]:
    """Move the checked-out `fused/mine` forward to `new_tip` with `git
    merge --ff-only` (inside `fused_aside`): a bring-in commit (first
    parent HEAD), the author's tip, a Merge with Claude result. Nothing is
    dropped, so no backup. `(None, None)` when it landed, else `(why,
    None)`: `moved` (HEAD is not `fused/mine`, or is no longer in
    `new_tip`'s history — a commit raced in), `in-progress` (an operation
    in flight), `reset-refused` (git refused: an uncommitted edit or a
    local file in the way, or an ignored local file the move would
    overwrite — never overwritten silently). The caller holds the slot."""
    if gu._current_branch(root) != HOME_BRANCH:
        return "moved", None
    if gu._operation_in_flight(root) is not None:
        return "in-progress", None
    new = rev(root, new_tip)
    head = rev(root, "HEAD")
    if not new or not head or not is_ancestor(root, head, new):
        return "moved", None
    if new == head:
        return None, None
    if clobber_paths(root, head, new):
        return "reset-refused", None
    # Without the S bit a set-aside `.fused/` file reads as a deletion.
    protect_after_move(root)
    with fused_aside(root, new):
        r = git(root, *COMMIT_CONFIG, "merge", "--ff-only", "-q", new, timeout=REPLAY_TIMEOUT_S)
    protect_after_move(root)
    if ok(r):
        return None, None
    logger.info("contrib ff deferred in %s: %s", root, gu._text(r))
    if rev(root, "HEAD") != head:
        return "moved", None
    return "reset-refused", None


def mirror_ref(root) -> str | None:
    """`refs/heads/<b>`, the mirror of the author's branch `fused/mine`
    follows (its upstream config, `branch.fused/mine.merge`), or None."""
    r = git(root, "config", "--get", f"branch.{HOME_BRANCH}.merge")
    ref = out(r) if ok(r) else ""
    return ref if ref.startswith("refs/heads/") and ref != HOME_REF else None


def settle_locked(root, *, why="settle", main=None, merged=None) -> tuple[bool, str | None]:
    """Settle (§17.3): `fused/mine` holds the author's version (same tree as
    `main`) by other commits — Bob's own, a bring-in merge, a squash on
    GitHub — so `fused/mine` becomes `main`. HEAD is pinned first
    (`backup_locked`: its tip, and any uncommitted edits at `-wip`; Undo).

    Same tree: a compare-and-set `update-ref fused/mine <main> <HEAD>`, then
    `reset --keep main`. No file differs between the two commits, so every
    unsaved edit is kept and git never refuses: no clean tree is needed.

    `merged` — a merge result the caller computed with the proposal's head
    as the merge base (the engine, after a squash/rebase accept) — equal to
    tree(main) while tree(HEAD) is not: Bob's change is already in `main`,
    the author edited it since. Then the move changes files: `reset --keep
    main` (git refuses rather than drop an unsaved edit on a file the move
    changes; an ignored local file it would overwrite rides in the backup or
    it does not move), and a commit that raced in is put back
    (`undo_race_locked`).

    Requires HEAD on `fused/mine`. `main` defaults to `mirror_ref`. `(True,
    backup)` when it settled; `(False, None)` and nothing moved otherwise.
    The caller holds the slot."""
    main = main or mirror_ref(root)
    if gu._current_branch(root) != HOME_BRANCH or gu._operation_in_flight(root) is not None:
        return False, None
    head = rev(root, "HEAD")
    main = rev(root, main)
    if not head or not main or head == main:
        return False, None
    want = tree_of(root, main)
    if tree_of(root, head) == want:
        refs = backup_locked(root, head, why=why)
        if not refs:
            return False, None
        if not ok(git(root, "update-ref", "-m", "fused: settle", HOME_REF, main, head)):
            unbackup_locked(root, refs)
            return False, None
        protect_after_move(root)
        with fused_aside(root, main):
            r = git(root, "reset", "-q", "--keep", main, timeout=REPLAY_TIMEOUT_S)
        if not ok(r):
            # The ref moved and the tree is the same: only the index's stat
            # cache is stale, which git refreshes on its next read.
            logger.info("contrib settle: reset after the ref move failed in %s: %s", root,
                        gu._text(r))
        protect_after_move(root)
        return True, refs[0]
    if not merged or merged != want:
        return False, None
    clobber = clobber_paths(root, head, main)
    refs = backup_locked(root, head, why=why, also=clobber)
    if not refs or (clobber and not any(r.endswith("-wip") for r in refs)):
        unbackup_locked(root, refs)
        return False, None
    protect_after_move(root)
    with fused_aside(root, main):
        r = git(root, "reset", "-q", "--keep", main, timeout=REPLAY_TIMEOUT_S)
    if not ok(r):
        unbackup_locked(root, refs)
        logger.info("contrib settle deferred in %s: %s", root, gu._text(r))
        return False, None
    if not undo_race_locked(root, head, refs, hard=True):
        return False, None
    protect_after_move(root)
    return True, refs[0]
