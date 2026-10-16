"""The contribution loop's git engine (docs/CONTRIB_LOOP.md §6): what a copy's
state is, and the moves that keep a GitHub-link copy on the author's latest
with Bob's changes on top — never losing them, never pushing them anywhere
he did not choose.

THE MODEL (§6.1). A GitHub-link copy stays on its local default branch.
Bob's changes are the commits on it the follow tip does not have. The
engine brings the author's latest in by REBASING them onto it (in a scratch
worktree — replay.py), so "changes" is a plain `rev-list` count and nothing
else in the app ever sees an off-default branch. Two exceptions, both about
never losing what a commit carries: a range holding a MERGE commit (Claude or
Bob ran `git merge` in the folder) is brought in with a merge instead — a
rebase would flatten it and drop its hand resolution — and every clean
rebase is held to the three-way merge of the same two sides
(`replay._replay_unsound`) before it may land.

WHERE IT RUNS. `git_upstream._sync_locked` — both auto-sync triggers (an app
open, an app-made commit) pass through it — asks `owns(root)` first and hands
an owned root to `sync`, slot held. `sync` gives the slot up for the fetch
and the replay (`replay.slot_released`) and takes it back only for the land
(backup + final check + `reset --keep`): minutes of rebase never block every
other repo's sync. Every other entry point here is a user action (the
routers): `bring_in_latest`, `discard`, `push_to_main`,
`return_to_following`, each taking the slot only around its writes, with
every GitHub call made OUTSIDE it (§0.9). One integration per copy at a time
(`_copy_lock`): a second trigger while one runs is a no-op.

OWNERSHIP OF A ROOT (`owns`). Only with its phase on: a `github` record when
`flags.changes()`, an `own` record with a GitHub repo when `flags.inbox()`
(A3's "no sync error, whatever state her copy was in"). `owns` reads the
store and nothing else: it runs under the slot, and classifying may ask
GitHub who the user is, so a folder with no record (or an ownership still
unchecked) is classified on a background thread and today's sync runs this
once. Only workspace app folders are classified that way — a developer's own
checkout outside the workspace keeps today's auto-sync exactly.

QUIET ON SUCCESS, CLEAR WHEN A DECISION IS NEEDED (handoff principle 5).
`sync` never records a git_upstream sync failure for an owned root (and
clears any standing one). An update that cannot land yet — a Claude turn
still running in the folder, the slot busy, the author unreachable — DEFERS
and is retried on the next trigger. Uncommitted edits that would block it
are committed first as "Your edits" (they are Bob's changes either way;
`replay.commit_edits_locked`), so an update never waits on them forever. An
overlap with the author's change is recorded as `needs_merge` (the page's
one button, Merge with Claude) with Bob's tree untouched.

WHAT "BASE" MEANS. `refs/fused/base` is the follow tip Bob's changes sit on
(advanced by every land); `upstream` for a replay is that base when HEAD
contains it, else origin's tracking ref when HEAD contains that (a fresh
install), else the merge-base. With an author force-push the merge-base
would reach back into the author's OWN rewritten history and replay it as if
it were Bob's; the base never does.

AN OWN COPY NEVER RE-PUBLISHES WHAT ITS AUTHOR REMOVED (review r01). When
the author's main on GitHub moved while this machine has unpushed commits,
only the commits this machine NEVER pushed — `<synced>..HEAD`, `synced`
being the last origin tip it integrated with or pushed (`refs/fused/base`,
else origin's tracking ref before the fetch) — are replayed onto the new
tip and pushed. A commit the author deliberately rewrote away (a leaked key)
was pushed, so it is in `synced`, so it is never replayed. When `synced` is
unknown, or HEAD does not contain it, nothing is replayed or pushed: the
overlap is recorded with a plain sentence and the edits stay where they are.

A PROPOSAL SURVIVES AN AUTO-INTEGRATE. Rebasing Bob's commits gives them new
ids, so `proposal.proposed_head` would fall out of HEAD's history and every
"N edits not in your proposal" count would become "all of them". The replay
is split at `proposed_head` (`replay.replay(mark=…)`) and the engine writes
where it landed back into the proposal (and `refs/fused/proposed`, which
pins the object). A Merge with Claude job reports the same (`merge_landed`'s
`mark_tip`); where neither could (a merge strategy), A4's return drops the
proposed commits by patch-id instead (`git cherry` against the pin).
"""
from __future__ import annotations

import logging
import os
import threading
import time

from fused_render import git_upstream as gu
from fused_render.contrib import classify, flags, follow, replay, store

logger = logging.getLogger(__name__)

#: An unacknowledged event older than this is no longer news on the page.
EVENT_TTL_S = 24 * 3600

_SWEEP_EVERY_S = 3600.0
_last_sweep = [0.0]

#: How long a user action waits for an integration already running on the
#: same copy before saying "busy".
_USER_LOCK_WAIT_S = 30.0

CLAUDE_BUSY_MESSAGE = "Claude is still working on this app — try again when it finishes."
REWRITTEN_MESSAGE = ("The version on GitHub changed in a way fused can't combine on its own. "
                     "Your edits are safe here.")


# --------------------------------------------------------------------- helpers


def _owner(rec) -> str | None:
    repo = rec.get("repo") if isinstance(rec, dict) else None
    return repo.split("/", 1)[0] if isinstance(repo, str) and "/" in repo else None


def _in_workspace(root) -> bool:
    """`root` is a workspace app folder, `<workspace>/<tag>/<name>`, by
    realpath (git_upstream's roots are realpath'd; the workspace setting may
    be a symlinked spelling)."""
    try:
        from fused_render.shell.seed import fused_dir

        rel = os.path.relpath(os.path.realpath(root), os.path.realpath(fused_dir()))
    except (ValueError, OSError):
        return False
    parts = rel.split(os.sep)
    return len(parts) == 2 and not any(p.startswith(".") for p in parts)


def _merge_job(root):
    """The app's current Merge with Claude job, if any (`merge_job.current`:
    no git, no network)."""
    try:
        from fused_render.contrib import merge_job
    except ImportError:
        return None
    fn = getattr(merge_job, "current", None)
    if fn is None:
        return None
    try:
        job = fn(root)
    except Exception:  # noqa: BLE001
        logger.exception("contrib: merge_job.current failed for %s", root)
        return None
    return job if isinstance(job, dict) else None


def _job_open(root) -> bool:
    job = _merge_job(root)
    return bool(job) and job.get("status") in ("running", "ready", "landing")


def _claude_busy(root) -> bool:
    """Whether a Claude session is mid-turn in this folder right now — the
    transcript rule `session_liveness` owns (the Inbox badge's), over the
    transcripts whose cwd is the folder or under it. Its commits do not take
    the slot, so the engine never auto-lands or discards while one runs
    (review r02); unreadable reads as not running."""
    try:
        from fused_render import claude_session_move as csm
        from fused_render import session_liveness as sl

        now = time.time()
        for bucket in csm._candidate_buckets(csm.PROJECTS_DIR, root):
            try:
                names = os.listdir(bucket)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(bucket, name)
                try:
                    if now - os.path.getmtime(path) > sl.STALE_TAIL_SEC:
                        continue
                except OSError:
                    continue
                cwd = csm.transcript_cwd(path)
                if not cwd or csm._under(os.path.realpath(cwd), root) is None:
                    continue
                if sl.transcript_running(path, now)[0]:
                    return True
    except Exception:  # noqa: BLE001
        logger.debug("contrib: claude liveness check failed for %s", root, exc_info=True)
    return False


def _base(root, head, new_tip, branch=None) -> str | None:
    """The commit Bob's changes sit on (module docstring "WHAT BASE MEANS")."""
    mb = gu._out(gu._run(root, "merge-base", head, new_tip)) or None
    base = replay.rev(root, replay.BASE_REF)
    if not (base and replay.is_ancestor(root, base, head)) and branch:
        base = replay.rev(root, f"refs/remotes/origin/{branch}")
    if base and replay.is_ancestor(root, base, head):
        if mb and replay.is_ancestor(root, base, mb):
            return mb
        return base
    return mb


def _latest(root, commits) -> str | None:
    """The one of `commits` that contains all the others, else None."""
    commits = [c for c in dict.fromkeys(commits) if c]
    for c in commits:
        if all(replay.is_ancestor(root, o, c) for o in commits):
            return c
    return None


def _changes(root, head, tip, base) -> int:
    """Bob's commits: in HEAD, not in the follow tip, not in the base."""
    args = ["rev-list", "--count", head]
    if tip:
        args.append("^" + tip)
    if base and replay.is_ancestor(root, base, head):
        args.append("^" + base)
    r = gu._run(root, *args)
    try:
        return int(gu._out(r)) if gu._ok(r) else 0
    except ValueError:
        return 0


def _dirty_paths(root) -> list[str]:
    """Paths with uncommitted edits — tracked or untracked-not-ignored,
    fused's machine state excluded: the ONE definition proposals.py's "Your
    edits" uses too (`replay.edits`)."""
    return [p for _code, p in replay.edits(root)]


def _changed_between(root, a, b) -> set:
    r = gu._run(root, "diff", "--name-only", "-z", a, b, "--")
    if not gu._ok(r):
        return set()
    return {p for p in r[1].decode("utf-8", "replace").split("\0") if p}


def _state(root, branch, head=None, tip=None) -> dict:
    """A `check_repo`-shaped dict against the follow tip, so git_upstream
    can refresh its cache and App Doctor reads the same picture."""
    head = head or replay.rev(root, "HEAD")
    ahead = behind = 0
    if head and tip:
        ahead = replay.count(root, f"{tip}..{head}") or 0
        behind = replay.count(root, f"{head}..{tip}") or 0
    return {"root": root, "branch": branch, "default_branch": branch,
            "on_default": True, "ahead": ahead, "behind": behind,
            "checked_at": time.time()}


def _out(status, why=None, **extra) -> dict:
    """An integration's outcome: `status` is `synced` | `noop` | `skipped`
    | `offline` | `needs-merge` | `deferred`, plus `why`, `pulled`,
    `pushed`, `tip`, `branch`, `had_changes`."""
    return {"status": status, "why": why, "pulled": 0, "pushed": False, **extra}


def _set_needs_merge(root, *, onto, upstream, head, purpose, files, strategy="rebase",
                     mark=None, message=None, rewritten=False):
    def fn(r):
        old = r.get("needs_merge") if isinstance(r.get("needs_merge"), dict) else {}
        r["needs_merge"] = {
            "onto": onto, "upstream": upstream, "head": head, "purpose": purpose,
            "strategy": strategy, "mark": mark, "message": message,
            "rewritten": bool(rewritten), "files": list(files or [])[:50],
            "since": old.get("since") or time.time(),
        }
    store.update(root, fn)


def _clear(root, *keys):
    def fn(r):
        for k in keys:
            if r.get(k) is not None:
                r[k] = None
    store.update(root, fn)


def _note_sync_error(root, reason):
    logger.warning("contrib: sync of %s failed (%s)", root, reason)

    def fn(r):
        r["sync_error"] = {"reason": reason, "at": time.time()}
    store.update(root, fn)


def _carry_proposal_locked(root, mark_tip):
    """A replay moved the proposed commits: point the proposal (and its pin
    ref) at where they landed. Slot held (the ref write)."""
    if not mark_tip:
        return

    def fn(r):
        if isinstance(r.get("proposal"), dict):
            r["proposal"]["proposed_head"] = mark_tip
    if store.update(root, fn) is not None:
        gu._run(root, "update-ref", replay.PROPOSED_REF, mark_tip)


def _proposed_mark(root, rec, head, base):
    p = rec.get("proposal") if isinstance(rec.get("proposal"), dict) else None
    sha = p.get("proposed_head") if p else None
    if not sha or p.get("returned_at"):
        return None
    if replay.is_ancestor(root, sha, head) and not (base and replay.is_ancestor(root, sha, base)):
        return sha
    return None


def _protect_soon(root):
    """Re-protect a committed `.fused/` when the slot is free right now (the
    land re-protects anyway)."""
    if not flags.sources():
        return
    with replay.slot(block=False) as held:
        if held:
            replay.protect_locked(root)


def _land(root, branch, old, new, why, *, mark_tip=None) -> bool:
    """The only write of an integration: backup + final check + move, under
    the slot. Defers (False) when the slot is busy, a Claude turn is running
    in the folder, or `land_locked` refuses (the branch moved since the plan
    was made — a fresh plan is the next trigger's)."""
    with replay.slot() as held:
        if not held or _claude_busy(root):
            return False
        if not replay.land_locked(root, branch=branch, old_tip=old, new_tip=new, why=why,
                                  app_dir=root):
            return False
        _carry_proposal_locked(root, mark_tip)
        return True


_locks_guard = threading.Lock()
_copy_locks: dict = {}


def _copy_lock(root) -> threading.Lock:
    with _locks_guard:
        return _copy_locks.setdefault(store.key(root), threading.Lock())


# ----------------------------------------------------------------------- owns


def owns(root) -> bool:
    """Whether the engine, not git_upstream's plain auto-sync, syncs `root`
    (module docstring "OWNERSHIP OF A ROOT"). Store reads only."""
    snap = flags.snapshot()
    if not (snap["changes"] or snap["inbox"]):
        return False
    rec = store.get(root)
    if rec is None:
        if _in_workspace(root):
            classify.ensure_in_background(root)
        return False
    if rec.get("kind") == "github" and not rec.get("owner_checked"):
        classify.ensure_in_background(root)
        if rec.get("classified_by") == "migration":
            # A copy that existed before the loop, whose owner could not be
            # asked yet: today's sync until the one-time check runs (it may
            # be the author's own repo, pushing as ever). A fresh install is
            # active at once: the engine never pushes a GitHub-link copy
            # anywhere, so the worst an unchecked author sees is their own
            # edits kept local until they sign in (CopyStatus.owner_checked).
            return False
    if rec.get("kind") == "github":
        return snap["changes"] and follow.for_record(rec) is not None
    if rec.get("kind") == "own":
        return snap["inbox"] and follow.for_record(rec) is not None
    return False


def follows_author(root) -> bool:
    """Whether `root` is a GitHub-link copy the engine keeps on the author's
    latest — `owns` for a `github` record, as a pure read (no background
    classification kicked): git_upstream asks this on every state write and
    every Update-card poll, so that the old Update/Switch card (an `--ff-only`
    pull or a checkout that would fail against Bob's commits, or move his
    branch behind the engine's back) never shows for such a copy."""
    try:
        if not flags.changes():
            return False
        rec = store.get(root)
        if not rec or rec.get("kind") != "github":
            return False
        if not rec.get("owner_checked") and rec.get("classified_by") == "migration":
            return False
        return follow.for_record(rec) is not None
    except Exception:  # noqa: BLE001 — unknown reads as today's behaviour
        return False


def settle_when_off(root) -> bool:
    """Called by git_upstream's intercept for every root the engine does NOT
    own, slot held: when the loop is off and this folder's `.fused/` paths
    still carry the skip-worktree bits it set, clear them once and drop the
    record's `protected` marker — so switching the loop off really returns
    the repo to today's behaviour. A store read and nothing else otherwise.
    Answers whether it un-protected."""
    if flags.sources():
        return False
    rec = store.get(root)
    if not rec or not rec.get("protected"):
        return False
    with replay.slot_held():
        replay.unprotect_locked(root)
    store.update(root, lambda r: r.pop("protected", None))
    return True


# ----------------------------------------------------------------------- sync


def sync(root, *, action, push) -> dict:
    """One sync of an owned root (§6.4), called by git_upstream's intercept
    with the slot held. Same return shape as `_sync_locked`; never a
    `failed` status (module docstring)."""
    with replay.slot_held():
        try:
            rec = store.get(root)
            if rec is None:
                return gu._skipped("unavailable")
            now = time.time()
            if now - _last_sweep[0] > _SWEEP_EVERY_S:
                _last_sweep[0] = now
                replay.sweep()
                store.sweep_orphans(now)
            lock = _copy_lock(root)
            if not lock.acquire(blocking=False):
                return gu._skipped("integrating")
            try:
                with replay.slot_released():
                    res = _integrate(root, rec, push=push, user=False)
            finally:
                lock.release()
            return _as_sync_result(root, res)
        finally:
            gu._clear_failures(root)


def _as_sync_result(root, res) -> dict:
    """An integration outcome in git_upstream's `_sync_locked` shape."""
    st = res["status"]
    if st == "offline":
        return {"ok": True, "status": "offline", "pulled": res.get("pulled", 0)}
    out = {"ok": True, "pulled": res.get("pulled", 0), "pushed": res.get("pushed", False)}
    if st in ("synced", "noop"):
        out["status"] = "synced"
    else:
        out["status"] = "skipped"
        out["why"] = res.get("why") or st
    if res.get("branch"):
        out["state"] = _state(root, res["branch"], tip=res.get("tip"))
    return out


def _integrate(root, rec, *, push, user) -> dict:
    if rec.get("kind") == "github":
        return _integrate_github(root, rec, user=user)
    return _integrate_own(root, rec, push=push, user=user)


def _preflight(root, branch):
    """None, or an outcome saying why nothing may move."""
    if not os.path.isdir(root):
        return _out("skipped", "unavailable")
    if gu._current_branch(root) != branch:
        return _out("skipped", "not-default")
    if gu._operation_in_flight(root) is not None:
        return _out("skipped", "in-progress")
    return None


def _integrate_github(root, rec, *, user) -> dict:
    """Fetch the author's latest and bring it in (module docstring). Runs
    without the slot; `_land` takes it for the move."""
    fol = follow.for_record(rec)
    if fol is None:
        return _out("skipped", "no-remote")
    refusal = _preflight(root, fol.branch)
    if refusal:
        return refusal
    tip, reason = follow.fetch_detail(root, fol)
    if tip is None:
        if reason == "redirected":
            _note_sync_error(root, "redirected")
            return _out("skipped", "redirected")
        # Offline, the repo gone or private to this account: silent, like
        # every unreachable remote in auto-sync; retried next trigger.
        return _out("offline")
    if (rec.get("sync_error") or {}).get("reason") == "redirected":
        _clear(root, "sync_error")
    _protect_soon(root)
    if not user and not gu.auto_sync_enabled():
        # Fetched for the page's "updates waiting" count (CopyStatus.behind);
        # the move is the user's (`bring_in_latest`).
        return _out("skipped", "auto-sync-off", branch=fol.branch, tip=tip)
    return _take_github(root, store.get(root) or rec, fol, tip)


def _take_github(root, rec, fol, tip) -> dict:
    """Bring the fetched follow tip `tip` into a GitHub-link copy."""
    branch, who = fol.branch, _owner(rec)
    done = {"branch": branch, "tip": tip}
    if _claude_busy(root):
        return _out("deferred", "claude", **done)
    head = replay.rev(root, "HEAD")
    if not head:
        return _out("skipped", "unreadable")

    # A4 first: a merged proposal the follow tip now contains is a RETURN,
    # not an overlap — integrating Bob's original commits onto the author's
    # squash of them would conflict for no reason. With the loop off, the
    # same reason says to leave the copy alone rather than conflict.
    if _proposal_merged_in(root, rec, tip):
        if flags.loop():
            return _return(root, rec, fol, head, tip)
        return _out("deferred", "merged-proposal", **done)

    if _job_open(root):
        return _out("skipped", "merge-job", **done)

    if replay.is_ancestor(root, tip, head):
        # Nothing of the author's is missing: following, or changes on top.
        with replay.slot(block=False) as held:
            if held:
                replay.advance_base_locked(root, head)
        if rec.get("needs_merge"):
            _clear(root, "needs_merge")
        return _out("noop", **done)

    behind = replay.count(root, f"{head}..{tip}") or 0
    base = _base(root, head, tip, branch)
    if not base:
        return _out("skipped", "unrelated", **done)
    mark_tip = None
    if set(_dirty_paths(root)) & _changed_between(root, head, tip):
        # Uncommitted edits to files the update changes (or untracked files
        # where it adds one): `reset --keep` would refuse every time, so the
        # update would wait forever. They are Bob's changes — commit them
        # ("Your edits", the Propose pathspec) and bring the update in on
        # top like any other change of his.
        head = _commit_edits(root, head)
        if not head:
            return _out("deferred", "edits", **done)
    if replay.is_ancestor(root, head, tip) or base == head:
        # No changes of Bob's: the author's tip as it is. `base == head` is
        # an author force-push under a copy with nothing of Bob's on top —
        # a move off history the author dropped (backed up), not a replay.
        new, event, why, had = tip, "updated", "update", False
    else:
        nm = rec.get("needs_merge") if isinstance(rec.get("needs_merge"), dict) else None
        if nm and nm.get("onto") == tip and nm.get("head") == head:
            return _out("needs-merge", "needs-merge", **done)
        strategy = "merge" if replay.has_merges(root, base, head) else "rebase"
        mark = None
        if strategy == "merge":
            res = replay.merge_into(root, head=head, onto=tip,
                                    message=f"Bring in {who or 'the author'}'s latest")
        else:
            mark = _proposed_mark(root, rec, head, base)
            res = replay.replay(root, onto=tip, upstream=base, branch=head, mark=mark)
        if res["conflict"] or (strategy == "merge" and not res.get("supported", True)):
            _set_needs_merge(root, onto=tip, upstream=base, head=head, purpose="needs-merge",
                             files=res["files"], strategy=strategy, mark=mark)
            return _out("needs-merge", "needs-merge", **done)
        if not res["ok"]:
            return _out("deferred", "replay", **done)
        new, event, why, had = res["tip"], "updated-kept", "update-kept", True
        mark_tip = res.get("mark_tip")
    if not _land(root, branch, head, new, why, mark_tip=mark_tip):
        return _out("deferred", "land", **done)
    gu._record_pull(root, behind)
    store.add_event(root, event, who=who)
    _clear(root, "needs_merge")
    return _out("synced", pulled=behind, had_changes=had, **done)


def _commit_edits(root, head) -> str | None:
    """Commit the uncommitted edits standing in an update's way as "Your
    edits" (module docstring); the new HEAD, or None (defer)."""
    with replay.slot() as held:
        if not held or _claude_busy(root) or replay.rev(root, "HEAD") != head:
            return None
        return replay.commit_edits_locked(root)


def _integrate_own(root, rec, *, push, user) -> dict:
    """An own copy with a GitHub repo (§6.4): today's fast-forward and push,
    except that a divergence (unpushed commits here while origin moved —
    e.g. the author accepted a proposal on GitHub) is replayed onto origin's
    tip and pushed instead of standing as a failure — replaying ONLY the
    commits this machine never pushed (module docstring, review r01)."""
    fol = follow.for_record(rec)
    if fol is None or not gu._ok(gu._run(root, "remote", "get-url", "origin")):
        return _out("skipped", "no-remote")
    branch = fol.branch
    refusal = _preflight(root, branch)
    if refusal:
        return refusal
    old_tracking = replay.rev(root, fol.ref)  # BEFORE the fetch moves it
    base_ref = replay.rev(root, replay.BASE_REF)
    origin_tip, reason = follow.fetch_detail(root, fol)
    if origin_tip is None:
        if reason not in (None, "offline"):
            _note_sync_error(root, reason)
            return _out("skipped", reason)
        return _out("offline")
    _protect_soon(root)
    done = {"branch": branch, "tip": origin_tip}
    if not user and not gu.auto_sync_enabled():
        return _out("skipped", "auto-sync-off", **done)
    if _claude_busy(root):
        return _out("deferred", "claude", **done)
    head = replay.rev(root, "HEAD")
    if not head:
        return _out("skipped", "unreadable")
    if _job_open(root):
        return _out("skipped", "merge-job", **done)

    pulled = 0
    if not replay.is_ancestor(root, origin_tip, head):
        behind = replay.count(root, f"{head}..{origin_tip}") or 0
        if replay.is_ancestor(root, head, origin_tip):
            new = origin_tip
        else:
            nm = rec.get("needs_merge") if isinstance(rec.get("needs_merge"), dict) else None
            if nm and nm.get("onto") == origin_tip and nm.get("head") == head:
                return _out("needs-merge", "needs-merge", **done)
            synced = _latest(root, [c for c in (base_ref, old_tracking)
                                    if c and replay.is_ancestor(root, c, head)])
            if not synced:
                _set_needs_merge(root, onto=origin_tip, upstream=None, head=head,
                                 purpose="own-sync", files=[], message=REWRITTEN_MESSAGE,
                                 rewritten=True)
                return _out("needs-merge", "rewritten", **done)
            if synced == head:
                # Everything here was published; GitHub moved on without it
                # (a history rewrite): follow it — the old tip is backed up.
                new = origin_tip
            else:
                rewritten = not replay.is_ancestor(root, synced, origin_tip)
                if replay.has_merges(root, synced, head):
                    if rewritten:
                        _set_needs_merge(root, onto=origin_tip, upstream=synced, head=head,
                                         purpose="own-sync", files=[], strategy="merge",
                                         message=REWRITTEN_MESSAGE, rewritten=True)
                        return _out("needs-merge", "rewritten", **done)
                    strategy = "merge"
                    res = replay.merge_into(root, head=head, onto=origin_tip,
                                            message="Bring in the latest from GitHub")
                else:
                    strategy = "rebase"
                    res = replay.replay(root, onto=origin_tip, upstream=synced, branch=head)
                if res["conflict"] or (strategy == "merge" and not res.get("supported", True)):
                    _set_needs_merge(root, onto=origin_tip, upstream=synced, head=head,
                                     purpose="own-sync", files=res["files"], strategy=strategy)
                    return _out("needs-merge", "needs-merge", **done)
                if not res["ok"]:
                    return _out("deferred", "replay", **done)
                new = res["tip"]
        if not _land(root, branch, head, new, "own-sync"):
            return _out("deferred", "land", **done)
        with replay.slot() as held:
            if held:
                gu._run(root, "update-ref", replay.BASE_REF, origin_tip)
        pulled = behind
        gu._record_pull(root, pulled)
        _clear(root, "needs_merge")
        head = replay.rev(root, "HEAD")

    pushed = False
    if push and head and not replay.is_ancestor(root, head, origin_tip):
        if follow.github_redirected(root):
            _note_sync_error(root, "redirected")
            return _out("skipped", "redirected", pulled=pulled, **done)
        r = gu._run(root, "push", "--", "origin", branch)
        if not gu._ok(r):
            why = gu._classify(r, push=True)
            if why == "offline":
                return _out("offline", pulled=pulled)
            if why == "rejected":
                # origin moved again since the fetch: the next trigger
                # integrates and pushes.
                return _out("deferred", "rejected", pulled=pulled, **done)
            _note_sync_error(root, why)
            return _out("skipped", why, pulled=pulled, **done)
        pushed = True
        with replay.slot() as held:
            if held:
                gu._run(root, "update-ref", replay.BASE_REF, head)
        done["tip"] = head
    _clear(root, "sync_error")
    return _out("synced", pulled=pulled, pushed=pushed, **done)


# ------------------------------------------------------------ A4: the return


def _proposal_merged_in(root, rec, tip) -> bool:
    p = rec.get("proposal") if isinstance(rec.get("proposal"), dict) else None
    if not p or p.get("returned_at"):
        return False
    cache = p.get("cache") if isinstance(p.get("cache"), dict) else {}
    sha = cache.get("merge_commit_sha")
    return cache.get("state") == "merged" and bool(sha) and replay.is_ancestor(root, sha, tip)


def _unproposed_commits(root, pin, head, base) -> list[str]:
    """Bob's commits in `base..head` with no patch-equivalent in what was
    proposed (`pin`), oldest first — `git cherry`'s `+` lines."""
    r = gu._run(root, "cherry", pin, head, base)
    if not gu._ok(r):
        return []
    return [ln[2:].strip() for ln in gu._out(r).splitlines() if ln.startswith("+ ")]


def _return(root, rec, fol, head, tip) -> dict:
    """Bob's proposal is in the author's latest: move to that tip keeping
    only the commits he never proposed. `proposed_head..HEAD` when HEAD still
    contains the proposed commits; else (a Merge with Claude job or a loop
    switched off rewrote them) the commits since the base whose patch the
    proposal does not hold. Runs without the slot (`_land` takes it)."""
    branch, who = fol.branch, _owner(rec)
    done = {"branch": branch, "tip": tip}
    p = rec.get("proposal") or {}
    proposed = replay.rev(root, p.get("proposed_head") or "")
    strategy, upstream, res, new = "rebase", None, None, None
    if proposed and replay.is_ancestor(root, proposed, head):
        upstream = proposed
        if proposed == head:
            new = tip
        elif replay.has_merges(root, proposed, head):
            strategy = "merge"
            res = replay.merge_into(root, head=head, onto=tip,
                                    message=f"Bring in {who or 'the author'}'s latest")
        else:
            res = replay.replay(root, onto=tip, upstream=proposed, branch=head)
    else:
        upstream = _base(root, head, tip, branch)
        pin = replay.rev(root, replay.PROPOSED_REF) or proposed
        if not upstream:
            return _out("deferred", "unrelated", **done)
        if pin and not replay.has_merges(root, upstream, head):
            picks = _unproposed_commits(root, pin, head, upstream)
            if not picks:
                new = tip
            else:
                res = replay.pick(root, onto=tip, commits=picks)
                if res["conflict"] or not res["supported"]:
                    strategy = "merge"  # a merge job cannot replay a picked list
                    if not res["supported"]:
                        res = replay.merge_into(root, head=head, onto=tip,
                                                message=f"Bring in {who or 'the author'}'s latest")
        else:
            strategy = "merge"
            res = replay.merge_into(root, head=head, onto=tip,
                                    message=f"Bring in {who or 'the author'}'s latest")
    if new is None:
        if res["conflict"] or not res.get("supported", True):
            _set_needs_merge(root, onto=tip, upstream=upstream, head=head, purpose="return",
                             files=res["files"], strategy=strategy)
            return _out("needs-merge", "needs-merge", onto=tip, upstream=upstream,
                        strategy=strategy, **done)
        if not res["ok"]:
            return _out("deferred", "replay", **done)
        new = res["tip"]
    if not _land(root, branch, head, new, "merged-back"):
        return _out("deferred", "land", **done)

    def fn(r):
        r["needs_merge"] = None
        if isinstance(r.get("proposal"), dict):
            r["proposal"]["returned_at"] = time.time()
    store.update(root, fn)
    store.add_event(root, "merged-back", who=who)
    return _out("synced", "return", **done)


def return_to_following(root) -> dict:
    """A4 (§6.4), user/refresh-triggered: fetch, then move to the author's
    latest keeping the unproposed edits. A clean result lands (event
    `merged-back`); an overlap starts a Merge with Claude job (purpose
    `return`)."""
    rec = store.get(root)
    if not rec or rec.get("kind") != "github" or not isinstance(rec.get("proposal"), dict):
        return {"ok": False, "reason": "no-proposal", "message": "There is no proposal to finish."}
    fol = follow.for_record(rec)
    if fol is None:
        return {"ok": False, "reason": "no-source", "message": "This copy has no source to return to."}
    who = _owner(rec)
    lock = _copy_lock(root)
    if not lock.acquire(timeout=_USER_LOCK_WAIT_S):
        return _busy()
    try:
        refusal = _preflight(root, fol.branch)
        if refusal:
            return _in_progress()
        if _claude_busy(root):
            return {"ok": False, "reason": "claude", "message": CLAUDE_BUSY_MESSAGE}
        tip, reason = follow.fetch_detail(root, fol)
        if reason == "redirected":
            return {"ok": False, "reason": "redirected", "message": follow.REDIRECTED_MESSAGE}
        tip = tip or follow.tip(root, fol)
        rec = store.get(root) or rec
        head = replay.rev(root, "HEAD")
        if not tip or not head or not _proposal_merged_in(root, rec, tip):
            return {"ok": False, "reason": "not-yet",
                    "message": f"{who}'s latest version doesn't include your change yet."}
        res = _return(root, rec, fol, head, tip)
    finally:
        lock.release()
    if res["status"] == "synced":
        return {"ok": True, "message": f"You're on {who}'s latest version."}
    if res["status"] == "needs-merge":
        try:
            from fused_render.contrib import merge_job

            job = merge_job.start(root, purpose="return", onto=res["onto"],
                                  upstream=res["upstream"], strategy=res.get("strategy"))
        except Exception:  # noqa: BLE001 — the needs_merge record stands; the page offers the button
            logger.exception("contrib: could not start the return merge for %s", root)
            job = None
        return {"ok": True, "job": job, "message": f"{who} changed the same parts you did."}
    return _deferred_message(res, who)


# ------------------------------------------------------- Merge with Claude


def merge_plan(root) -> dict | None:
    """What a Merge with Claude job for `root` should do, from the recorded
    overlap: `{"purpose", "onto", "upstream", "strategy", "mark"}`, or None
    when there is none, its commits are gone, or it is the rewritten-history
    case no merge may resolve (the edits would re-publish what the author
    removed). `upstream` is exactly what the engine's own replay used; with
    `strategy` "merge" the job merges `onto` into HEAD instead of replaying.
    No network."""
    rec = store.get(root)
    nm = rec.get("needs_merge") if rec and isinstance(rec.get("needs_merge"), dict) else None
    if not nm or nm.get("rewritten"):
        return None
    onto, upstream = replay.rev(root, nm.get("onto") or ""), replay.rev(root, nm.get("upstream") or "")
    if not (onto and upstream):
        return None
    purpose = nm.get("purpose") if nm.get("purpose") in ("needs-merge", "own-sync", "return") \
        else "needs-merge"
    strategy = nm.get("strategy") if nm.get("strategy") in ("rebase", "merge") else "rebase"
    mark = replay.rev(root, nm.get("mark") or "") if strategy == "rebase" else None
    return {"purpose": purpose, "onto": onto, "upstream": upstream, "strategy": strategy,
            "mark": mark}


def merge_landed(root, *, purpose, old_tip, new_tip, mark_tip=None) -> None:
    """A Merge with Claude job's result just landed (`merge_job.accept`
    called `replay.land`): the record bookkeeping for its purpose — and
    `mark_tip`, where the job's replay put the proposed commit. Never
    raises."""
    try:
        rec = store.get(root) or {}
        who = _owner(rec)
        nm = rec.get("needs_merge") if isinstance(rec.get("needs_merge"), dict) else {}
        now = time.time()

        def fn(r):
            r["needs_merge"] = None
            if purpose == "return" and isinstance(r.get("proposal"), dict):
                r["proposal"]["returned_at"] = now
            if purpose == "original":
                r["original_update"] = None
        store.update(root, fn)
        with replay.slot() as held:
            if held:
                if mark_tip and purpose == "needs-merge":
                    _carry_proposal_locked(root, mark_tip)
                onto = nm.get("onto")
                if purpose == "own-sync" and onto and replay.is_ancestor(root, onto, new_tip):
                    # What this copy now sits on, for the next own-sync's
                    # "never pushed" range (module docstring, r01).
                    gu._run(root, "update-ref", replay.BASE_REF, onto)
        kind = {"needs-merge": "merge-accepted", "return": "merged-back",
                "original": "original-brought-in"}.get(purpose)
        if kind:
            store.add_event(root, kind, who=who)
        if purpose in ("own-sync", "original"):
            # The combined tip is the author's own: auto-sync pushes it.
            gu.schedule_sync(root, "Auto-push after merge", push=True)
    except Exception:  # noqa: BLE001
        logger.exception("contrib: merge bookkeeping failed for %s", root)


# ------------------------------------------------------------ user actions


def _busy():
    return {"ok": False, "reason": "busy", "message": "Busy updating this app; try again in a moment."}


def _in_progress():
    return {"ok": False, "reason": "in-progress",
            "message": "This app is in the middle of something else; try again later."}


def _deferred_message(res, who) -> dict:
    why = res.get("why")
    if why == "claude":
        return {"ok": False, "reason": "claude", "message": CLAUDE_BUSY_MESSAGE}
    if why == "redirected":
        return {"ok": False, "reason": "redirected", "message": follow.REDIRECTED_MESSAGE}
    if res["status"] == "offline":
        return {"ok": False, "reason": "offline", "message": "Can't reach GitHub right now."}
    if why == "merged-proposal":
        return {"ok": False, "reason": "merged-proposal",
                "message": f"{who} took your proposal; fused will finish updating once that's "
                           "switched on."}
    if why in ("not-default", "in-progress"):
        return _in_progress()
    return {"ok": False, "reason": why or "deferred",
            "message": "Couldn't finish updating this app right now; fused will try again."}


def bring_in_latest(root) -> dict:
    """The author's updates, brought in on request — what a copy uses with
    auto-sync off, when nothing does it in the background (CopyStatus
    `behind` > 0 and `auto_sync` False). The same integrate as `sync`,
    whatever the auto-sync setting: a fast-forward, or a replay that keeps
    Bob's changes on top; an overlap is recorded for Merge with Claude."""
    rec = store.get(root)
    snap = flags.snapshot()
    if not rec or not ((rec.get("kind") == "github" and snap["changes"])
                       or (rec.get("kind") == "own" and snap["inbox"] and rec.get("repo"))):
        return {"ok": False, "reason": "disabled", "message": "There is nothing to update here."}
    who = _owner(rec) or "the author"
    lock = _copy_lock(root)
    if not lock.acquire(timeout=_USER_LOCK_WAIT_S):
        return _busy()
    try:
        res = _integrate(root, rec, push=False, user=True)
    finally:
        lock.release()
    if res["status"] == "synced":
        tail = ", your changes kept." if res.get("had_changes") else "."
        return {"ok": True, "message": f"Updated to {who}'s latest{tail}"}
    if res["status"] == "noop":
        return {"ok": True, "message": f"You already have {who}'s latest."}
    if res["status"] == "needs-merge":
        nm = (store.get(root) or {}).get("needs_merge") or {}
        return {"ok": True, "needs_merge": True,
                "message": nm.get("message") or f"{who} changed the same parts you did."}
    return _deferred_message(res, who)


def discard(root, *, withdraw: bool) -> dict:
    """Discard my changes (§6.4): back up HEAD and every uncommitted edit
    (untracked files included), then move to the author's latest. `withdraw`
    also withdraws an open proposal (default no — the handoff's default).
    Refused while a Claude turn is running in the folder."""
    rec = store.get(root)
    if not rec or rec.get("kind") != "github":
        return {"ok": False, "reason": "not-a-copy",
                "message": "Only a copy of someone else's app can go back to their version."}
    fol = follow.for_record(rec)
    if fol is None:
        return {"ok": False, "reason": "no-source", "message": "This copy has no source to go back to."}
    who = _owner(rec)
    if _claude_busy(root):
        return {"ok": False, "reason": "claude", "message": CLAUDE_BUSY_MESSAGE}
    job = _merge_job(root)
    if job and job.get("id") and job.get("status") in ("running", "ready"):
        try:
            from fused_render.contrib import merge_job

            merge_job.discard(job["id"])
        except Exception:  # noqa: BLE001
            logger.exception("contrib: could not discard the merge job for %s", root)
    lock = _copy_lock(root)
    if not lock.acquire(timeout=_USER_LOCK_WAIT_S):
        return _busy()
    try:
        if _preflight(root, fol.branch):
            return _in_progress()
        tip, reason = follow.fetch_detail(root, fol)
        if tip is None:
            # Never the clone's origin when its config re-points GitHub.
            tip = follow.tip(root, fol) if reason == "redirected" else follow.tip_or_origin(root, fol)
        if not tip:
            return {"ok": False, "reason": "offline",
                    "message": f"Can't reach {who}'s version right now; try again when you're online."}
        with replay.slot() as held:
            if not held:
                return _busy()
            if _claude_busy(root):
                return {"ok": False, "reason": "claude", "message": CLAUDE_BUSY_MESSAGE}
            if not replay.reset_hard_locked(root, branch=fol.branch, target=tip, why="discard",
                                            app_dir=root):
                return {"ok": False, "reason": "failed",
                        "message": "Couldn't go back to the original version; nothing was changed."}
        _clear(root, "needs_merge")
        store.add_event(root, "discarded", who=who)
    finally:
        lock.release()
    out = {"ok": True,
           "message": "Your changes were discarded. fused kept a copy on this computer, just in case."}
    if withdraw and isinstance(rec.get("proposal"), dict):
        try:
            from fused_render.contrib import proposals

            proposals.withdraw(root)
        except Exception:  # noqa: BLE001 — the local discard already happened
            logger.exception("contrib: withdraw after discard failed for %s", root)
            out["message"] += " Your proposal couldn't be withdrawn; try again from the proposal."
    return out


def push_to_main(root) -> dict:
    """A collaborator's explicit "push straight to main" (§6.4, D20):
    integrate the author's latest first, send HEAD to the followed branch on
    the author's repo, and record that tip as followed — so "your changes"
    goes to zero."""
    rec = store.get(root)
    if not rec or rec.get("kind") != "github" or not rec.get("repo"):
        return {"ok": False, "reason": "not-a-copy", "message": "This isn't a copy of someone else's app."}
    fol = follow.for_record(rec)
    who = _owner(rec)
    if fol is None:
        gu._clear_failures(root)
        return {"ok": False, "reason": "no-source",
                "message": "This copy isn't linked to its GitHub source."}
    gh = classify._gh()
    if gh is None:
        return {"ok": False, "reason": "signed-out", "message": "Sign in to GitHub first."}
    try:
        info = gh.repo(rec["repo"])
        can = bool(((info or {}).get("permissions") or {}).get("push"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": getattr(exc, "reason", "failed"),
                "message": getattr(exc, "message", None) or "Couldn't reach GitHub right now."}
    if not can:
        return {"ok": False, "reason": "forbidden",
                "message": f"You can't add changes directly to {who}'s app. Propose them instead."}
    lock = _copy_lock(root)
    if not lock.acquire(timeout=_USER_LOCK_WAIT_S):
        return _busy()
    try:
        res = _integrate_github(root, store.get(root) or rec, user=True)
        if res["status"] == "needs-merge":
            return {"ok": False, "reason": "needs-merge",
                    "message": f"{who} changed the same parts you did. Merge first."}
        if res["status"] not in ("synced", "noop"):
            return _deferred_message(res, who)
        tip, head = follow.tip(root, fol), replay.rev(root, "HEAD")
        if not tip or not head or not replay.is_ancestor(root, tip, head):
            return {"ok": False, "reason": "deferred",
                    "message": f"{who}'s latest isn't in your copy yet; try again in a moment."}
        if head == tip:
            return {"ok": True, "message": "Nothing to send."}
        if follow.github_redirected(root):
            return {"ok": False, "reason": "redirected", "message": follow.REDIRECTED_MESSAGE}
        try:
            gh.push(root, head, rec["repo"], fol.branch)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": getattr(exc, "reason", "failed"),
                    "message": getattr(exc, "message", None) or "Couldn't send your changes."}
        with replay.slot() as held:
            if held:
                gu._run(root, "update-ref", replay.FOLLOW_REF, head)
                replay.advance_base_locked(root, head)
    finally:
        lock.release()
    return {"ok": True, "message": f"Your changes are now in {who}'s app."}


# --------------------------------------------------------------------- status

_status_lock = threading.Lock()
_status_cache: dict = {}  # root -> (key tuple, (changes, behind, unproposed))
_STATUS_CACHE_MAX = 256


def _refs(root) -> dict:
    """HEAD plus every ref the status reads, in two git calls."""
    out = {"HEAD": replay.rev(root, "HEAD")}
    # Exact refs only: never `refs/fused/` (every backup ref would be listed
    # on each card render).
    r = gu._run(root, "for-each-ref", "--format=%(refname) %(objectname)",
                replay.FOLLOW_REF, replay.BASE_REF, replay.PROPOSED_REF,
                "refs/fused/original", "refs/remotes/origin/")
    if gu._ok(r):
        for line in gu._out(r).splitlines():
            name, _, sha = line.partition(" ")
            out[name] = sha
    return out


def _unproposed(root, head, tip, proposed) -> int:
    if not proposed or not replay.rev(root, proposed):
        return 0
    if replay.is_ancestor(root, proposed, head):
        args = ["rev-list", "--count", head, "^" + proposed] + (["^" + tip] if tip else [])
        r = gu._run(root, *args)
        try:
            return int(gu._out(r)) if gu._ok(r) else 0
        except ValueError:
            return 0
    # The proposed commits were rewritten (a merge job): count Bob's commits
    # with no patch-equivalent in what was proposed.
    r = gu._run(root, "cherry", proposed, head, *([tip] if tip else []))
    return sum(1 for line in gu._out(r).splitlines() if line.startswith("+")) if gu._ok(r) else 0


def _event(rec):
    """The newest unseen, still-fresh event of a kind the page renders
    (`store.RENDERED_EVENTS`) — a newer one it never draws must not hide it
    (those are stored seen already)."""
    cutoff = time.time() - EVENT_TTL_S
    for e in reversed(rec.get("events") or []):
        if (isinstance(e, dict) and not e.get("seen") and e.get("kind") in store.RENDERED_EVENTS
                and (e.get("at") or 0) >= cutoff):
            return {"kind": e.get("kind"), "at": e.get("at"), "who": e.get("who")}
    return None


def _job_view(root):
    job = _merge_job(root)
    if not job:
        return None
    # Passed through as merge_job.current answers it — "landing" (accept in
    # progress) is its own status; the page treats it as running.
    return {k: job.get(k) for k in ("id", "status", "summary", "files", "purpose",
                                    "error", "started_at")}


#: `sync_error` codes in words for the page — a code like "git-failed" must
#: never reach a person (§0.1); the code itself rides in `sync_error_code`.
_SYNC_ERROR_WORDS = {
    "auth": "Your latest changes couldn't be sent to GitHub — sign in to GitHub again.",
    "redirected": follow.REDIRECTED_MESSAGE,
}
_SYNC_ERROR_DEFAULT = "Your latest changes couldn't be sent to GitHub yet; fused will keep trying."


def status(root) -> dict:
    """CopyStatus (§6.4) — no network, a few local git calls, cached on the
    refs it depends on. A folder with no record yet is classified in the
    background and answers `pending: True` with an own-shaped status."""
    rec = store.get(root)
    base_view = {
        "kind": "own", "state": "own", "repo": None, "author": None, "remix_of": None,
        "changes": 0, "dirty": False, "unproposed": 0, "needs_merge": False,
        "needs_merge_message": None, "merge_job": None, "event": None,
        "original_update": False, "can_push": None, "behind": 0, "sync_error": None,
        "sync_error_code": None, "pending": False, "owner_checked": True,
        "auto_sync": gu.auto_sync_enabled(),
    }
    if rec is None:
        if os.path.isdir(root):
            classify.ensure_in_background(root)
        return {**base_view, "pending": True}
    remix_of = rec.get("remix_of") if isinstance(rec.get("remix_of"), dict) else None
    nm = rec.get("needs_merge") if isinstance(rec.get("needs_merge"), dict) else None
    view = {
        **base_view,
        "kind": rec.get("kind") or "own",
        "repo": rec.get("repo"),
        "remix_of": remix_of.get("repo") if remix_of else None,
        "needs_merge": bool(nm),
        "needs_merge_message": (nm or {}).get("message"),
        "merge_job": _job_view(root),
        "event": _event(rec),
        "original_update": bool(rec.get("original_update")),
        "can_push": rec.get("can_push"),
        "owner_checked": bool(rec.get("owner_checked", True)),
    }
    code = rec.get("sync_error", {}).get("reason") if isinstance(rec.get("sync_error"), dict) \
        else None
    if code:
        view["sync_error"] = _SYNC_ERROR_WORDS.get(code, _SYNC_ERROR_DEFAULT)
        view["sync_error_code"] = code
    if view["kind"] != "github":
        view["state"] = "remix" if remix_of else "own"
        return view
    view["author"] = _owner(rec)
    fol = follow.for_record(rec)
    if fol is None:
        view["state"] = "following"  # nothing to compare against
        return view
    refs = _refs(root)
    head = refs.get("HEAD")
    tip = refs.get(fol.ref) or refs.get(f"refs/remotes/origin/{fol.branch}")
    base = refs.get(replay.BASE_REF)
    proposal = rec.get("proposal") if isinstance(rec.get("proposal"), dict) else None
    proposed = proposal.get("proposed_head") if proposal else None
    if head:
        key = (head, tip, base, proposed)
        with _status_lock:
            hit = _status_cache.get(root)
        if hit and hit[0] == key:
            changes, behind, unproposed = hit[1]
        else:
            changes = _changes(root, head, tip, base)
            behind = (replay.count(root, f"{head}..{tip}") or 0) if tip else 0
            unproposed = _unproposed(root, head, tip, proposed) if proposal else 0
            with _status_lock:
                if len(_status_cache) >= _STATUS_CACHE_MAX:
                    _status_cache.clear()
                _status_cache[root] = (key, (changes, behind, unproposed))
        view.update(changes=changes, behind=behind, unproposed=unproposed)
    view["dirty"] = bool(_dirty_paths(root))
    cache = (proposal or {}).get("cache") if isinstance((proposal or {}).get("cache"), dict) else {}
    pstate = cache.get("state") if proposal else None
    settled_seen = bool((proposal or {}).get("settled_seen"))
    if view["needs_merge"]:
        state = "needs-merge"
    elif pstate == "merged" and not settled_seen:
        state = "merged"
    elif pstate == "closed" and not settled_seen:
        state = "declined"
    elif pstate == "open":
        state = "proposed"
    elif view["changes"] or view["dirty"]:
        state = "changes"
    else:
        state = "following"
    view["state"] = state
    return view
