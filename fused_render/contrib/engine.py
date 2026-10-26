"""The contribution loop's git engine for a GitHub-link copy
(docs/CONTRIB_LOOP.md §17, the v3 model): what the copy's state is, and the
moves that keep Bob on the author's latest with his changes kept — never
losing them, never sending them anywhere he did not choose.

THE MODEL (§17.1). Bob's work lives on his own branch, `fused/mine`
(`classify.HOME_BRANCH`); HEAD is always there (I4). The author's branch
`refs/heads/<b>` is a fetch-only MIRROR of git's own
`refs/remotes/origin/<b>` (I3): every sync fetches by the RECORD's URL
(`follow.fetch`) and then moves the mirror as a ref update
(`follow.update_mirror`), never a worktree move — and never while it is
checked out. So "the author's latest" is `main` (the mirror), "Bob's
changes" are what `fused/mine` has that `main` does not, and every git tool
(the Git panel, ↑N, a terminal's `git status`) reads the same picture.

THREE FACTS, NO TABLE (§17.2). `ahead = rev-list --count --no-merges
main..HEAD` (bring-in merges never count), `dirty` (tracked edits), and the
proposal's state on GitHub (`proposal.cache.state`). Everything else —
behind, a conflict (`merge-tree`), a local file in the way — is derived at
status time from the refs, cached on `(HEAD, main, pr)`. Nothing about a
stuck state is recorded: the next status derives it again.

THE MOVES (§17.3), each ONE git command with git's own guard:
- ff: `merge --ff-only main` when `fused/mine` is in `main`'s history;
- bring in: `merge-tree` clean → `commit-tree <tree> -p HEAD -p main`
  ("Bring in <who>'s latest", Bob's identity) → `merge --ff-only` to it
  (`replay.land_ff_locked`: HEAD moved meanwhile = git refuses);
- settle: the author merged Bob's proposal (pr merged) and `fused/mine`
  holds exactly `main`'s tree by other commits (a squash, a merge commit
  that is a sibling of Bob's own bring-in) → `fused/mine` := `main`
  (`replay.settle_locked`: backup, compare-and-set, no file changes);
- discard / match: backup (incl. uncommitted edits), `reset --hard main`.
An uncommitted tracked edit on a file the author's side changes is first
committed as "Your edits" (v2 row 6, kept: D245's one exception); an
untracked or ignored local file at a path `main` adds is `blocked` (Move my
file aside). Bob's commits keep their ids forever; nothing is rebased.

AUTOMATIC IS FINE; MAGIC IS NOT (P6). Every automatic move writes one
Activity line — with Undo (a backup of the old HEAD) — and is also a
button. Undo of an automatic move HOLDS: `restore` sets the record's
`skip_tip` to the author's tip it undid, and no automatic move runs while
the mirror is still there (`kept-mine`); a button clears the hold, and a
newer tip from the author ends it by itself. The `merged` once-stamp: `proposal.landed_at` is set ONCE, in the
same store update as the line "<who> merged your change…".

THE ENGINE NEVER PUSHES (P2) except `push_to_main` (a collaborator's
explicit act, by URL). Tree moves wait while a Claude turn is open in the
folder (P8) and run on their own only with `git_upstream.auto_sync_enabled()`
(a button runs them anyway). With the loop switched off (`flags.sources()`)
the moves still run — "Your edits" included: it is a commit on Bob's own
branch (§17.7) — and a stuck state (overlap, a file in the way) is
git_upstream's failure row in plain words instead of a page state.

WHERE IT RUNS. git_upstream's intercept hands a `github` root to `sync`,
slot held (after `classify.settle_locked`: the v2 → v3 move, the guards).
`sync` gives the slot up for the run (`slot_released`); every write takes it
back briefly. GitHub reads (`proposals.refresh`) never run under it (§0.9).
One run per copy at a time (`_copy_lock`); every button takes the same lock.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time

from fused_render import git_upstream as gu
from fused_render.contrib import classify, flags, follow, replay, store

logger = logging.getLogger(__name__)

HOME_BRANCH = classify.HOME_BRANCH

#: A fetch error in words (§0.1) when no better sentence fits.
_SYNC_ERROR_DEFAULT = "fused couldn't check for updates to this app; it will keep trying."

_SWEEP_EVERY_S = 3600.0
_last_sweep = [0.0]
#: Each copy's unreferenced old backups are pruned at most once a day.
_PRUNE_EVERY_S = 24 * 3600.0
_last_prune: dict = {}

#: How long a user action waits for a run already going on the same copy.
_USER_LOCK_WAIT_S = 30.0

CLAUDE_BUSY_MESSAGE = "Claude is still working on this app — try again when it finishes."
BUSY_MESSAGE = "Busy updating this app; try again in a moment."
ELSEWHERE_MESSAGE = "This app is in the middle of something else; try again later."
OFFLINE_MESSAGE = "Can't reach GitHub right now."
OTHER_VERSION_MESSAGE = ("You're on a different version of this app. Go back to your usual "
                         "version first.")
#: §18.4: a delisted marketplace copy's state sentence (and a button's
#: refusal: nothing to update from).
DELISTED_MESSAGE = ("No longer in the Marketplace. Your copy keeps working on its last "
                    "reviewed version.")
BLOCKED_MESSAGE = "Fused blocked the version of this app you were running."

#: A proposal's head after Update's fetch (proposals.PR_HEAD_REF).
_PR_HEAD_REF = "refs/fused/pr-head"

# --------------------------------------------------------------------- helpers


def _who(rec) -> str:
    repo = rec.get("repo") if isinstance(rec, dict) else None
    return repo.split("/", 1)[0] if isinstance(repo, str) and "/" in repo else "the author"


def _out(status, why=None, **extra) -> dict:
    """A run's outcome: `status` is `synced` | `noop` | `skipped` |
    `offline` | `needs-merge` | `blocked` | `deferred` (and
    `failed` once `sync` recorded a flags-off failure row), plus `why`,
    `move` (`ff` | `merge` | `settle`), `pulled`, `files`, `tip`,
    `branch`."""
    return {"status": status, "why": why, "pulled": 0, "pushed": False, **extra}


#: The Merge with Claude purposes that run on a GitHub-link copy (§17.4).
_COPY_PURPOSES = ("update", "proposal")


def _merge_job(root):
    """The copy's own Merge with Claude job (`merge_job.current`, the
    purposes a GitHub-link copy runs), if any."""
    try:
        from fused_render.contrib import merge_job

        job = merge_job.current(root, purpose=_COPY_PURPOSES)
    except Exception:  # noqa: BLE001
        logger.exception("contrib: merge_job.current failed for %s", root)
        return None
    return job if isinstance(job, dict) else None


def _job_open(root) -> bool:
    job = _merge_job(root)
    return bool(job) and job.get("status") in ("running", "ready", "landing", "choice")


def _discard_job(root) -> None:
    job = _merge_job(root)
    if job and job.get("id") and job.get("status") in ("running", "ready", "choice"):
        try:
            from fused_render.contrib import merge_job

            merge_job.discard(job["id"])
        except Exception:  # noqa: BLE001
            logger.exception("contrib: could not discard the merge job for %s", root)


def _claude_busy(root) -> bool:
    """Whether a Claude turn is open in this folder right now (P8): the
    last-message rule `session_liveness.transcript_turn_open` (right for
    headless runs too), over the transcripts whose cwd is the folder or
    under it. Its commits do not take the slot, so no tree move runs while
    one is open. Unreadable reads as not open."""
    try:
        from fused_render import claude_session_move as csm
        from fused_render import session_liveness as sl

        now = time.time()
        for bucket in csm._candidate_buckets(csm.PROJECTS_DIR, root):
            try:
                listing = os.listdir(bucket)
            except OSError:
                continue
            for name in listing:
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
                if sl.transcript_turn_open(path, now, interrupt_closes=True):
                    return True
    except Exception:  # noqa: BLE001
        logger.debug("contrib: claude liveness check failed for %s", root, exc_info=True)
    return False


def claude_busy(root) -> bool:
    """`_claude_busy`, for proposals/ownsync: one busy rule (P8)."""
    return _claude_busy(root)


def _activity(root, kind, text, *, backup=None, aside=None):
    """One Activity line (§16.9); never raises."""
    try:
        return store.add_activity(root, kind, text, backup=backup, aside=aside)
    except Exception:  # noqa: BLE001
        logger.exception("contrib: activity line failed for %s", root)
        return None


def _merge_base(root, a, b) -> str | None:
    r = replay.git(root, "merge-base", a, b)
    return (gu._out(r) or None) if gu._ok(r) else None


def _changed(root, a, b) -> set:
    if not a or not b:
        return set()
    return set(replay.names(replay.git(root, "diff", "--name-only", "--no-renames", "-z", a, b,
                                    "--", *replay.EDITS_PATHSPEC)))


def _subjects(root, a, b) -> str:
    """The author's new work in words: up to three commit subjects of
    `a..b` (first parent), "…and N more". The author's own text."""
    rng = f"{a}..{b}" if a else b
    r = replay.git(root, "log", "--first-parent", "--no-merges", "--format=%s", rng)
    # The "Claude: " prefix is fused's own commit convention, not words.
    subs = [re.sub(r"^Claude: ", "", s.strip()) for s in gu._out(r).splitlines()
            if s.strip()] if gu._ok(r) else []
    if not subs:
        return ""
    shown = ", ".join(f"“{s[:80]}”" for s in subs[:3])
    return shown + (f" and {len(subs) - 3} more" if len(subs) > 3 else "")


def _files_words(files) -> str:
    names = [os.path.basename(f) for f in files][:3]
    more = len(files) - len(names)
    if not names:
        return "your files"
    text = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return text + (f" and {more} more" if more > 0 else "")


def _sync_error_words(reason, who) -> str:
    if reason == follow.NOT_ON_BRANCH:
        # §18.4: the approved commit is not on the author's branch (any
        # more): the mirror stays where it is.
        return _market().not_on_branch_words(who)
    return {
        "redirected": follow.REDIRECTED_MESSAGE,
        "auth": f"fused couldn't read {who}'s app on GitHub. Sign in to GitHub again.",
        "not-found": f"{who}'s app isn't on GitHub any more, or it's private now.",
        "forbidden": f"Your GitHub account can't read {who}'s app any more.",
    }.get(reason, _SYNC_ERROR_DEFAULT)


def _note_sync_error(root, reason):
    """A fetch that failed for a reason the person can act on (never
    `offline`: that stays silent and is retried): `sync_error` {reason,
    message, at}, the signal for the page's Try again / Sign in."""
    logger.warning("contrib: fetch for %s failed (%s)", root, reason)

    def fn(r):
        r["sync_error"] = {"reason": reason, "message": _sync_error_words(reason, _who(r)),
                           "at": time.time()}
    store.update(root, fn)


def _note_checked(root):
    """A successful fetch: `last_checked` (§16.9); any fetch error is over."""
    def fn(r):
        r["last_checked"] = time.time()
        r["sync_error"] = None
    store.update(root, fn)


def _note_head(root, head=None) -> None:
    try:
        store.note_head(root, head or replay.rev(root, "HEAD"))
    except Exception:  # noqa: BLE001 — bookkeeping for adoption only
        logger.exception("contrib: note_head failed for %s", root)


def _mirror(fol) -> str:
    """The author's branch as a local ref: the mirror (I3)."""
    return f"refs/heads/{fol.branch}"


def _main(root, fol) -> str | None:
    """The author's latest this copy knows: the mirror, else (never moved
    yet) git's own tracking ref — which is also the answer while the mirror
    is checked out (it is not moved then, §17.3). A marketplace copy
    (pinned, §18.4): the mirror, else the approved commit — never
    `origin/<b>`, the author's unreviewed tip (M1)."""
    if fol.pinned:
        if gu._current_branch(root) == fol.branch:
            return replay.rev(root, fol.commit)
        return replay.rev(root, _mirror(fol)) or replay.rev(root, fol.commit)
    if gu._current_branch(root) == fol.branch:
        return follow.tip(root, fol) or replay.rev(root, _mirror(fol))
    return replay.rev(root, _mirror(fol)) or follow.tip(root, fol)


def _on_home(root) -> bool:
    return gu._current_branch(root) == HOME_BRANCH


_locks_guard = threading.Lock()
_copy_locks: dict = {}


def _copy_lock(root) -> threading.Lock:
    with _locks_guard:
        return _copy_locks.setdefault(store.key(root), threading.Lock())


#: Set while this thread runs the engine for a copy: a `bring_in` from
#: inside (a refresh or hook the run called) refuses as busy instead of
#: deadlocking on `_copy_lock`.
_inside = threading.local()


def _record(root):
    rec = store.get(root)
    if not rec or not classify.is_github_like(rec.get("kind")):
        return None, None
    return rec, follow.for_record(rec)


def _is_market(rec) -> bool:
    return isinstance(rec, dict) and rec.get("kind") == "marketplace"


def _market():
    from fused_render.contrib import market

    return market


def _blocks():
    from fused_render.contrib import blocks

    return blocks


def _proposal(rec) -> dict | None:
    p = (rec or {}).get("proposal")
    return p if isinstance(p, dict) and p.get("number") else None


def _pr_state(p) -> str | None:
    cache = (p or {}).get("cache") if isinstance((p or {}).get("cache"), dict) else {}
    return cache.get("state") if p else None


def _landed_in(root, p, main) -> bool:
    """The proposal is merged on GitHub and what GitHub merged is in `main`
    (its merge commit — or, for a direct publish, the head it pushed)."""
    if _pr_state(p) != "merged" or not main:
        return False
    sha = ((p or {}).get("cache") or {}).get("merge_commit_sha")
    return bool(sha) and replay.is_ancestor(root, sha, main)


# ----------------------------------------------------------------------- sync


def sync(root, *, action, push) -> dict:
    """One background sync of a `github` root (§17.3), called by
    git_upstream's intercept with the slot held. Same return shape as
    `_sync_locked`; never a push (`push` is ignored: P2). A `failed` status
    only for a stuck state with the loop switched off (git_upstream's
    failure row, `_failure_row`); every other outcome clears a standing
    failure row."""
    with replay.slot_held():
        res = None
        try:
            rec = store.get(root)
            if rec is None or not classify.is_github_like(rec.get("kind")):
                return gu._skipped("unavailable")
            _housekeep(root, rec)
            # §18.4 B4: the block list is enforced on every open's sync,
            # BEFORE the fetch — a hard block rolls back (or marks) here; a
            # github copy only gets its warning. Never waits for the copy
            # lock (a button holding it re-enforces itself).
            try:
                _blocks().enforce(root, wait=False)
            except Exception:  # noqa: BLE001 — the gate still answers from the record
                logger.exception("contrib: block enforcement failed for %s", root)
            lock = _copy_lock(root)
            if not lock.acquire(blocking=False):
                return gu._skipped("integrating")
            try:
                with replay.slot_released():
                    res = _run(root, user=False, action=action)
            finally:
                lock.release()
            if replay.slot_lost() and res["status"] not in ("synced", "failed"):
                return gu._skipped("busy")
            if res["status"] in ("needs-merge", "blocked") and not flags.sources():
                res = _failure_row(root, rec, res, action=action)
            return _as_sync_result(root, res)
        finally:
            if not res or res["status"] not in ("failed", "deferred"):
                gu._clear_failures(root)


def _housekeep(root, rec) -> None:
    """Scratch worktrees a crash left (hourly), and this copy's old
    unreferenced backups (daily). Slot held."""
    now = time.time()
    if now - _last_sweep[0] > _SWEEP_EVERY_S:
        _last_sweep[0] = now
        replay.sweep()
        try:
            store.sweep_orphans(now)
        except Exception:  # noqa: BLE001
            logger.exception("contrib: orphan sweep failed")
    k = store.key(root)
    if now - _last_prune.get(k, 0.0) > _PRUNE_EVERY_S:
        _last_prune[k] = now
        replay.prune_backups_locked(root, store.referenced_backups(root), now)


#: A stuck state with the loop off → git_upstream's failure reason (§16.12).
_FAILURE_REASONS = {"blocked": "git-failed", "needs-merge": "diverged"}


def _stuck_words(who, status, files) -> str:
    names = _files_words(files)
    if status == "blocked":
        return (f"{who}'s update adds {names}, which you already have here. Rename "
                "yours to get the update.")
    return (f"Your copy and {who}'s latest both changed {names}, so fused can't bring "
            "the update in on its own.")


def _failure_row(root, rec, res, *, action) -> dict:
    """A stuck state while the loop's page (and its fix buttons) is off:
    git_upstream's failure row in plain words, tree untouched (§16.12,
    §17.7)."""
    reason = _FAILURE_REASONS[res["status"]]
    words = _stuck_words(_who(rec), res["status"], res.get("files") or [])
    failure = gu.record_sync_failure(
        root, reason, action=action, push=False, message=words,
        command=f"git pull --ff-only -- origin {res.get('branch') or 'main'}").get("failure")
    return {**res, "status": "failed", "why": reason, "failure": failure, "message": words}


def _as_sync_result(root, res) -> dict:
    st = res["status"]
    if st == "offline":
        return {"ok": True, "status": "offline", "pulled": 0}
    if st == "failed":
        out = {"ok": False, "status": "failed", "failure": res.get("failure"), "pulled": 0,
               "pushed": False}
    else:
        out = {"ok": True, "pulled": res.get("pulled", 0), "pushed": False}
        if st in ("synced", "noop"):
            out["status"] = "synced"
        else:
            out["status"] = "skipped"
            out["why"] = res.get("why") or st
    if res.get("branch") and res.get("tip") and _on_home(root):
        out["state"] = _state(root, res["branch"])
    return out


def _state(root, branch) -> dict:
    """A `check_repo`-shaped dict (§17.6), so git_upstream refreshes its
    cache and App Doctor reads the same picture: `on_default` is HEAD on
    `fused/mine`, `default_branch` the author's branch, ahead/behind
    `--left-right --count HEAD...main` (merges counted, for one release)."""
    ahead = behind = 0
    r = replay.git(root, "rev-list", "--left-right", "--count", f"HEAD...refs/heads/{branch}")
    parts = gu._out(r).split() if gu._ok(r) else []
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        ahead, behind = int(parts[0]), int(parts[1])
    cur = gu._current_branch(root)
    return {"root": root, "branch": cur, "default_branch": branch,
            "on_default": cur == HOME_BRANCH, "ahead": ahead, "behind": behind,
            "checked_at": time.time()}


def _run(root, *, user, action="Bring in") -> dict:
    """Fetch, mirror, refresh a proposal the author's latest may have
    merged, then the one move this copy needs — the one path for `sync`
    and every button that brings the author's latest in."""
    _inside.depth = getattr(_inside, "depth", 0) + 1
    try:
        return _run_inner(root, user=user, action=action)
    finally:
        _inside.depth -= 1


def _market_gate(root, rec) -> tuple:
    """A marketplace copy's row, before the fetch (§18.4): the catalog's
    approved commit into the record (`market.refresh_approved`, its own
    10-minute TTL), then whether it may move at all — `(rec, fol, why)`,
    `why` "delisted" (no fetch, no move: it keeps its last reviewed
    version) or "blocked" (a hard block stands: enforcement owns the copy)
    or None."""
    try:
        _market().refresh_approved(root)
    except Exception:  # noqa: BLE001 — M3: an unreadable catalog is no move
        logger.exception("contrib: catalog read failed for %s", root)
    rec, fol = _record(root)
    if rec is None:
        return None, None, None
    if _market().is_delisted(rec):
        return rec, fol, "delisted"
    if _blocks().standing(rec):
        return rec, fol, "blocked"
    return rec, fol, None


def _soft_paused(root, rec, main) -> bool:
    """§18.4 soft block: automatic moves pause while the copy runs a
    soft-blocked commit, or the release waiting for it is one."""
    b = rec.get("block") if isinstance(rec.get("block"), dict) else None
    if b and b.get("level") == "soft":
        return True
    try:
        return _blocks().contains_blocked(root, rec.get("slug"), main, None,
                                          levels=("soft",)) is not None
    except Exception:  # noqa: BLE001 — unknown reads as paused (fails closed)
        return True


def _run_inner(root, *, user, action) -> dict:
    rec, fol = _record(root)
    if rec is None:
        return _out("skipped", "not-a-copy")
    if _is_market(rec):
        rec, fol, why = _market_gate(root, rec)
        if rec is None:
            return _out("skipped", "not-a-copy")
        if why:
            return _out("skipped", why)
    if fol is None:
        return _out("skipped", "no-source")
    if not os.path.isdir(root):
        return _out("skipped", "unavailable")
    with replay.slot() as held:
        if not held:
            return _out("deferred", "slot-busy")
        if rec.get("v") != store.RECORD_V:
            rec = classify.settle_locked(root, rec.get("kind")) or rec
        old_main = replay.rev(root, _mirror(fol))
        # Fetch first whatever is checked out, so the page can still say how
        # far behind the copy is; only a move needs fused/mine. The fetch
        # moves the mirror (and names an author's rewind) on fused/mine;
        # `update_mirror` again covers another version checked out.
        tip, reason = follow.fetch(root, fol)
        if tip and _is_market(rec):
            # §18.4: the mirror holds the approved commit — vetted first
            # against the block list (a release built on a blocked commit is
            # not safe either), and only when it is on the author's branch.
            safe = _blocks().vet_approved(root)
            if safe is None:
                # Every release left contains a hard-blocked commit: the
                # mirror stays where it is (M1, not even briefly);
                # enforcement owns the copy.
                return _out("skipped", "blocked")
            if safe != fol.commit:
                rec, fol = _record(root)
                if fol is None:
                    return _out("skipped", "blocked")
            m = follow.update_mirror(root, fol)
            if replay.rev(root, _mirror(fol)) != replay.rev(root, fol.commit) \
                    and not gu._current_branch(root) == fol.branch:
                reason = m.get("reason") or follow.NOT_ON_BRANCH
                tip = None
            else:
                tip = _main(root, fol)
        elif tip:
            follow.update_mirror(root, fol)
        on_home = _on_home(root)
        in_flight = gu._operation_in_flight(root) is not None
    if tip is None:
        if reason == "offline":
            return _out("offline")
        _note_sync_error(root, reason)
        return _out("skipped", reason)
    _note_checked(root)
    done = {"branch": fol.branch, "tip": tip}
    if _is_market(rec) and not user and _soft_paused(root, rec, tip):
        return _out("skipped", "soft-blocked", **done)
    if not on_home:
        return _out("skipped", "not-default", **done)
    if in_flight:
        return _out("skipped", "in-progress", **done)
    if not user and not gu.auto_sync_enabled():
        # Fetched for the page's "updates waiting"; the move is the user's.
        return _out("skipped", "auto-sync-off", **done)
    hold = (store.get(root) or {}).get("skip_tip")
    if hold:
        main = _main(root, fol)
        if user or hold != main:
            _clear_hold(root)  # a button, or the author published again
        elif not replay.is_ancestor(root, main, replay.rev(root, "HEAD")):
            # Something to bring in, and Bob undid exactly that: wait.
            return _out("skipped", "kept-mine", **done)
    if old_main != tip:
        _refresh_proposal(root)
    for _round in range(3):  # another round only after committing "Your edits"
        res, again = _step(root, fol, tip, user=user, done=done)
        if not again:
            return res
    return _out("deferred", "edits", **done)


def _clear_hold(root) -> None:
    """End an undo hold (`skip_tip`)."""
    if (store.get(root) or {}).get("skip_tip"):
        store.update(root, lambda r: r.__setitem__("skip_tip", None))


#: Activity kinds of AUTOMATIC moves (ff / bring-in, settle): Undo of one
#: holds the author's tip it undid (`skip_tip`).
_HOLD_KINDS = frozenset({"update", "merged"})


def _refresh_proposal(root) -> None:
    """The author's latest moved: ask GitHub about an unsettled proposal
    first (`proposals.refresh(force=True)`, slot NOT held), so a merge is
    known BEFORE it is brought in and the settle can follow."""
    p = _proposal(store.get(root))
    if not p or p.get("landed_at") or p.get("direct") or _pr_state(p) not in ("open", "merged", None):
        return
    try:
        from fused_render.contrib import proposals

        proposals.refresh(root, force=True)
    except Exception:  # noqa: BLE001 — the cached state stands; a later refresh asks again
        logger.exception("contrib: proposal refresh failed for %s", root)


def _step(root, fol, tip, *, user, done) -> tuple[dict, bool]:
    """One look and at most one move (§17.3). `(outcome, again)` — again
    after committing "Your edits", so the caller looks once more."""
    rec = store.get(root) or {}
    who = _who(rec)
    head = replay.rev(root, "HEAD")
    main = _main(root, fol) or tip
    if not head:
        return _out("skipped", "unreadable", **done), False
    if replay.is_ancestor(root, main, head):
        return _after_in(root, fol, done, move=None, backup=None, pulled=0, text=None), False
    if _claude_busy(root):
        return _out("deferred", "claude", **done), False
    if _job_open(root):
        return _out("skipped", "merge-job", **done), False
    tracked = set(replay.tracked_edits(root))
    clobber = [p for p in replay.clobber_paths(root, head, main) if p not in tracked]
    if clobber:
        return _out("blocked", "clobber", files=clobber, **done), False
    # After a squash/rebase accept Bob's own commits are not in `main` by
    # ancestry: the proposal's head is the honest merge base (§17.3, round 3c).
    base = _merged_base(root, rec, head, main)
    blocking = sorted(tracked & _changed(root, base or _merge_base(root, head, main), main)) \
        if tracked else []
    if blocking:
        if not _commit_edits(root, head, blocking, who):
            return _out("deferred", "edits", **done), False
        return _out("deferred", "edits", **done), True
    pulled = replay.count(root, f"{head}..{main}") or 0
    subjects = _subjects(root, _merge_base(root, head, main), main)
    if replay.is_ancestor(root, head, main):
        new, move = main, "ff"
        text = f"Got {who}'s latest" + (f": {subjects}." if subjects else ".")
    else:
        mt = replay.merge_tree(root, head, main, base=base)
        if not mt["supported"]:
            return _out("deferred", "merge", **done), False
        if not mt["clean"]:
            return _out("needs-merge", "needs-merge", files=list(mt["files"]), **done), False
        p = _proposal(rec)
        if mt["tree"] == replay.tree_of(root, main) and (
                base or (p and not p.get("landed_at") and _landed_in(root, p, main))):
            # Everything of Bob's is in the author's version already (a
            # squash/rebase accept seen through the PR-head base, or a
            # merge-commit accept whose merge is a sibling of his own
            # bring-in): settle straight onto it rather than record one more
            # bookkeeping merge first — files move only where the author
            # edited since, and Undo pins Bob's own tip.
            with replay.slot() as held:
                if not held:
                    return _out("deferred", "slot-busy", **done), False
                if _claude_busy(root):
                    return _out("deferred", "claude", **done), False
                ok, backup = replay.settle_locked(root, why="settle", main=_mirror(fol),
                                                  merged=mt["tree"], head=head)
            if not ok:
                return _out("deferred", "reset-refused", **done), False
            gu._record_pull(root, pulled)
            text = f"Got {who}'s latest" + (f": {subjects}." if subjects else ".")
            return _after_in(root, fol, done, move="settle", backup=backup, pulled=pulled,
                             text=text), False
        new = gu._out(replay.git(root, *replay.identity_args(root), "commit-tree", mt["tree"],
                                 "-p", head, "-p", main, "-m", f"Bring in {who}'s latest"))
        if not new:
            return _out("deferred", "merge", **done), False
        move = "merge"
        text = (f"Brought in {who}'s {subjects}, your changes kept." if subjects
                else f"Brought in {who}'s latest, your changes kept.")
    why, backup = _land(root, head, new)
    if why:
        return _out("deferred", why, **done), False
    gu._record_pull(root, pulled)
    return _after_in(root, fol, done, move=move, backup=backup, pulled=pulled, text=text), False


def _merged_base(root, rec, head, main) -> str | None:
    """The proposal's head when Bob's proposal is merged, what GitHub merged
    is in `main` (`_landed_in`) and the head is in HEAD's history — the
    merge base a bring-in uses then (round 3c): with git's own base Bob's
    pre-squash commits look new, and the author's later edits of the same
    lines would conflict with his own, already-merged change. None
    otherwise (git's base); a merge-commit accept has the head in `main`
    already, where it is git's base anyway."""
    p = _proposal(rec)
    if _pr_state(p) != "merged" or not _landed_in(root, p, main):
        return None
    pr_head = _pr_head(root, p, main)
    if not pr_head or replay.is_ancestor(root, pr_head, main) \
            or not replay.is_ancestor(root, pr_head, head):
        return None
    return pr_head


def _land(root, head, new) -> tuple:
    """A bring-in's only write: under the slot, a Claude turn re-checked,
    the old HEAD pinned (every automatic move has Undo), then
    `replay.land_ff_locked` — git's own `merge --ff-only`, which refuses
    when a commit raced in. `(None, backup)` when it landed, else `(why,
    None)` and the pin is taken back."""
    with replay.slot() as held:
        if not held:
            return "slot-busy", None
        if _claude_busy(root):
            return "claude", None
        if replay.rev(root, "HEAD") != head or not _on_home(root):
            return "moved", None
        refs = replay.backup_locked(root, head, why="update")
        if not refs:
            return "reset-refused", None
        why, _ = replay.land_ff_locked(root, new)
        if why:
            replay.unbackup_locked(root, refs)
            return why, None
    return None, refs[0]


def _commit_edits(root, head, paths, who) -> bool:
    """Commit exactly the blocking paths as "Your edits" (module docstring,
    the D245 exception), on fused/mine."""
    with replay.slot() as held:
        if not held or _claude_busy(root) or replay.rev(root, "HEAD") != head \
                or not _on_home(root):
            return False
        made = replay.commit_edits_locked(root, paths)
    if not made:
        return False
    _activity(root, "edits-saved",
              f"Saved your edit to {_files_words(paths)} so {who}'s update could come in.")
    return True


def _merged_text(who, *, kept) -> str:
    return (f"{who} merged your change. You're on {who}'s latest version."
            + (" Edits you made since are kept." if kept else ""))


def _after_in(root, fol, done, *, move, backup, pulled, text) -> dict:
    """The author's latest is in `fused/mine` now (just brought in, or
    already). With Bob's proposal merged and not landed yet (§17.2 `merged`
    row): settle when `fused/mine` holds exactly the author's tree by other
    commits, then the once-stamp — `landed_at` and the line "<who> merged
    your change…" in ONE store update, Undo = the settle's backup (else the
    bring-in's). A settle that cannot run now (Claude
    working) leaves both for the next sync. Otherwise the move's own line."""
    _note_head(root)
    rec = store.get(root) or {}
    who = _who(rec)
    p = _proposal(rec)
    head = replay.rev(root, "HEAD")
    main = _main(root, fol)
    if _is_market(rec) and move:
        # §18.4: a marketplace copy's automatic move is a RELEASE — its
        # Activity line names it and says what's new (the review check's
        # words); the author's commit subjects are never the story here.
        text = _market().release_words(who, rec.get("approved"))
    if p and not p.get("landed_at") and _is_market(rec) and _landed_in(root, p, main):
        # B5: Bob's merged proposal reached the mirror only because a
        # RELEASE containing it did (`_landed_in`: what GitHub merged ⊆ the
        # approved commit) — until then nothing settles and the panel says
        # "It ships in <who>'s next release.".
        return _released(root, fol, done, rec, p, head=head, main=main, move=move,
                         backup=backup, pulled=pulled, text=text)
    if p and not p.get("landed_at") and _landed_in(root, p, main):
        settled = False
        undo = backup
        same = head != main and replay.tree_of(root, head) == replay.tree_of(root, main)
        if same:
            with replay.slot() as held:
                ok, sb = (replay.settle_locked(root, why="settle", main=_mirror(fol))
                          if held and not _claude_busy(root) else (False, None))
            if not ok:
                if move:
                    _activity(root, "update", text, backup=backup)
                    return _out("synced", move=move, pulled=pulled, **done)
                return _out("noop", **done)
            settled, undo = True, sb
        line = store.new_activity("merged", _merged_text(who, kept=not same and head != main),
                                  backup=undo)
        number = p.get("number")

        def stamp(r):
            q = r.get("proposal")
            if isinstance(q, dict) and q.get("number") == number and not q.get("landed_at"):
                q["landed_at"] = time.time()
                store.append_activity(r, line)
        store.update(root, stamp)
        if settled:
            _note_head(root)
        return _out("synced", move="settle" if settled else move, pulled=pulled, **done)
    if move:
        _activity(root, "update", text, backup=backup)
        return _out("synced", move=move, pulled=pulled, **done)
    return _out("noop", **done)


RELEASED_TEXT = "{who} released your change. You're on the reviewed version."


def _released(root, fol, done, rec, p, *, head, main, move, backup, pulled, text) -> dict:
    """`_after_in`'s `merged` row for a marketplace copy (§18.4 B5): the
    release containing Bob's merged change arrived — the same settle as a
    GitHub-link copy's, its line "<who> released your change…" (Undo = the
    settle's backup, else the move's), and `proposal.live` set in the same
    store update as `landed_at` (the page: "Your change is live for
    everyone.", crediting Bob when the review credits his login)."""
    who = _who(rec)
    settled, undo = False, backup
    same = head != main and replay.tree_of(root, head) == replay.tree_of(root, main)
    if same:
        with replay.slot() as held:
            ok, sb = (replay.settle_locked(root, why="settle", main=_mirror(fol))
                      if held and not _claude_busy(root) else (False, None))
        if not ok:
            if move:
                _activity(root, "update", text, backup=backup)
                return _out("synced", move=move, pulled=pulled, **done)
            return _out("noop", **done)
        settled, undo = True, sb
    line = store.new_activity("merged", RELEASED_TEXT.format(who=who), backup=undo)
    number = p.get("number")

    def stamp(r):
        q = r.get("proposal")
        if isinstance(q, dict) and q.get("number") == number and not q.get("landed_at"):
            q["landed_at"] = time.time()
            q["live"] = True
            store.append_activity(r, line)
    store.update(root, stamp)
    if settled:
        _note_head(root)
    return _out("synced", move="settle" if settled else move, pulled=pulled, **done)


# ------------------------------------------------------------ user actions


def _refuse(reason, message, **extra) -> dict:
    return {"ok": False, "reason": reason, "message": message, **extra}


def _deferred_message(res, who) -> dict:
    why = res.get("why")
    if res["status"] == "offline":
        return _refuse("offline", OFFLINE_MESSAGE)
    if why == "claude":
        return _refuse("claude", CLAUDE_BUSY_MESSAGE)
    if why in ("redirected", "auth", "not-found", "forbidden", "failed"):
        return _refuse(why, _sync_error_words(why, who))
    if why == follow.NOT_ON_BRANCH:
        return _refuse(why, _sync_error_words(why, who))
    if why == "delisted":
        return _refuse(why, DELISTED_MESSAGE)
    if why == "blocked":
        return _refuse(why, BLOCKED_MESSAGE)
    if why == "not-default":
        return _refuse("off-default", OTHER_VERSION_MESSAGE)
    if why in ("in-progress", "slot-busy", "merge-job"):
        return _refuse(why, ELSEWHERE_MESSAGE if why != "slot-busy" else BUSY_MESSAGE)
    if why == "reset-refused":
        return _refuse(why, "Unsaved edits here are in the way of the update; try again "
                            "in a moment.")
    return _refuse(why or "deferred", "Couldn't finish updating this app right now; fused "
                                      "will try again.")


def _user_run(root, fn):
    """A button: one at a time per copy (waits for a run already going)."""
    lock = _copy_lock(root)
    if not lock.acquire(timeout=_USER_LOCK_WAIT_S):
        return _refuse("busy", BUSY_MESSAGE)
    _inside.depth = getattr(_inside, "depth", 0) + 1
    try:
        return fn()
    finally:
        _inside.depth -= 1
        lock.release()


def bring_in(root) -> dict:
    """Bring in the author's latest now (§17.3, the same moves as a sync,
    with auto-sync off too). `{ok, message, reason?, needs_merge?,
    files?}`. A marketplace copy: the approved release (§18.4)."""
    rec, fol = _record(root)
    if rec is None or (fol is None and not _is_market(rec)):
        return _refuse("not-a-copy", "There is nothing to update here.")
    who = _who(rec)
    if getattr(_inside, "depth", 0):
        return _refuse("busy", BUSY_MESSAGE)
    res = _user_run(root, lambda: _run(root, user=True))
    if _is_market(rec) and res["status"] in ("synced", "noop"):
        return _market_answer(res, who)
    return _bring_in_answer(res, who)


def _market_answer(res, who) -> dict:
    """A marketplace copy's Bring in, in release words (§18.4)."""
    if res["status"] == "noop":
        return {"ok": True, "message": "You're on the reviewed version."}
    if res.get("move") == "settle":
        return {"ok": True, "message": RELEASED_TEXT.format(who=who)}
    return {"ok": True, "message": f"You're on {who}'s newest reviewed release."}


def _bring_in_answer(res, who) -> dict:
    st = res["status"]
    if st == "synced":
        msg = {"merge": f"Brought in {who}'s latest, your changes kept.",
               "settle": _merged_text(who, kept=False)}.get(res.get("move"),
                                                            f"You're on {who}'s latest.")
        return {"ok": True, "message": msg}
    if st == "noop":
        return {"ok": True, "message": f"You already have {who}'s latest."}
    if st == "needs-merge":
        if not flags.sources():
            return _refuse("diverged", _stuck_words(who, st, res.get("files") or []),
                           files=res.get("files") or [])
        return {"ok": True, "needs_merge": True, "files": res.get("files") or [],
                "message": f"{who} changed the same parts you did. Claude can combine "
                           f"them for you, or you can switch to {who}'s version."}
    if st == "blocked":
        files = res.get("files") or []
        if not flags.sources():
            return _refuse("git-failed", _stuck_words(who, st, files), files=files)
        return _refuse("blocked", f"{who}'s update adds {_files_words(files)}, which you "
                                  "already have here. Move yours aside to get the update.",
                       files=files)
    return _deferred_message(res, who)


def _hard_to_main(root, *, why, kind, text, message) -> dict:
    """Discard / Match: backup (incl. every uncommitted edit), then `reset
    --hard` the author's latest on fused/mine. Refused while a Claude turn
    is open."""
    rec, fol = _record(root)
    if rec is None or fol is None:
        return _refuse("not-a-copy", "Only a copy of someone else's app can go back to "
                                     "their version.")
    who = _who(rec)
    if _claude_busy(root):
        return _refuse("claude", CLAUDE_BUSY_MESSAGE)

    def go():
        with replay.slot() as held:
            if not held:
                return _refuse("busy", BUSY_MESSAGE)
            if not _on_home(root):
                return _refuse("off-default", OTHER_VERSION_MESSAGE)
            if gu._operation_in_flight(root):
                return _refuse("in-progress", ELSEWHERE_MESSAGE)
            # Only now, with the move sure to be attempted: a ready Merge
            # with Claude result is not thrown away by a refused click.
            _discard_job(root)
            _clear_hold(root)
            tip, _reason = follow.fetch(root, fol)
            if tip:
                follow.update_mirror(root, fol)
            main = _main(root, fol)
            if not main:
                return _refuse("offline", f"Can't reach {who}'s version right now; try again "
                                          "when you're online.")
            if _claude_busy(root):
                return _refuse("claude", CLAUDE_BUSY_MESSAGE)
            moved, backup = replay.reset_hard_locked(root, branch=HOME_BRANCH, target=main,
                                                     why=why)
        if not moved:
            return _refuse("failed", f"Couldn't switch to {who}'s version; nothing was "
                                     "changed.")
        _note_head(root)
        _activity(root, kind, text.format(who=who), backup=backup)
        return {"ok": True, "message": message.format(who=who)}

    return _user_run(root, go)


def match(root) -> dict:
    """Match <who>: the author's latest exactly, mine saved (Undo) — the
    `needs-merge` state's other way out (§17.3; replaces v2's Use theirs)."""
    return _hard_to_main(
        root, why="match", kind="match",
        text="Switched to {who}'s version. Your version is saved.",
        message="You're on {who}'s version. Your version is saved in Activity.")


def discard(root, *, withdraw: bool = False) -> dict:
    """Discard my changes: as Match; `withdraw` also withdraws an open
    proposal (default no)."""
    rec = store.get(root) or {}
    out = _hard_to_main(
        root, why="discard", kind="discard",
        text="Discarded your changes. You're on {who}'s version; a copy is saved.",
        message="Your changes were discarded. fused kept a copy on this computer, just "
                "in case.")
    if out.get("ok") and withdraw and isinstance(rec.get("proposal"), dict):
        try:
            from fused_render.contrib import proposals

            proposals.withdraw(root)
        except Exception:  # noqa: BLE001 — the local discard already happened
            logger.exception("contrib: withdraw after discard failed for %s", root)
            out["message"] += " Your proposal couldn't be withdrawn; try again from the proposal."
    return out


def keep_working(root) -> dict:
    """Keep working after a declined or withdrawn proposal (§17.3): the
    proposal is forgotten (`proposal = None`); nothing moves, your changes
    stay. Refused while the proposal is still open on GitHub."""
    rec, fol = _record(root)
    if rec is None or fol is None:
        return _refuse("not-a-copy", "There is nothing to change here.")
    p = _proposal(rec)
    if p and _pr_state(p) == "open":
        return _refuse("open", "Your proposal is still open. Withdraw it first.")

    def fn(r):
        r["proposal"] = None
        r["skip_tip"] = None  # a button ends an undo hold (§17.2)
    store.update(root, fn)
    return {"ok": True, "message": "Your changes are still here. Propose them again any time."}


def _aside_root(root) -> str:
    from fused_render.shell import storage

    tag = hashlib.sha1(store.key(root).encode("utf-8")).hexdigest()[:16]
    return os.path.join(storage.home_dir(), "contrib", "aside", tag)


def move_aside(root) -> dict:
    """The `blocked` fix: move the local files the author's latest adds out
    of the folder (to `<home>/contrib/aside/<key>/<ms>/…`, Restore puts
    them back), then Bring in."""
    rec, fol = _record(root)
    if rec is None or fol is None:
        return _refuse("not-a-copy", "There is nothing to update here.")
    who = _who(rec)
    if _claude_busy(root):
        return _refuse("claude", CLAUDE_BUSY_MESSAGE)

    def go():
        with replay.slot() as held:
            if not held:
                return _refuse("busy", BUSY_MESSAGE), None
            if not _on_home(root):
                return _refuse("off-default", OTHER_VERSION_MESSAGE), None
            if _claude_busy(root):
                return _refuse("claude", CLAUDE_BUSY_MESSAGE), None
            _clear_hold(root)
            main, head = _main(root, fol), replay.rev(root, "HEAD")
            tracked = set(replay.tracked_edits(root))
            files = [p for p in replay.clobber_paths(root, head, main) if p not in tracked]
            if not files:
                return None, None
            dest = os.path.join(_aside_root(root), str(int(time.time() * 1000)))
            moved = []
            for rel in files:
                try:
                    os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
                    os.replace(os.path.join(root, rel), os.path.join(dest, rel))
                    moved.append(rel)
                except OSError:
                    logger.warning("contrib: could not move %s aside", rel, exc_info=True)
            if moved:
                _activity(root, "aside", f"Moved your {_files_words(moved)} aside so {who}'s "
                                         "update could come in.",
                          aside={"dir": dest, "files": moved})
            if len(moved) != len(files):
                return _refuse("failed", "Couldn't move your file out of the way; nothing "
                                         "else was changed."), None
            return None, moved

    got = _user_run(root, go)
    if isinstance(got, dict):
        return got  # the copy was busy
    refusal, moved = got
    if refusal:
        return refusal
    out = bring_in(root)
    if moved and out.get("ok"):
        out["message"] = (f"Moved your {_files_words(moved)} aside — you can put "
                          f"{'it' if len(moved) == 1 else 'them'} back from Activity. "
                          + out["message"])
    return out


def go_back_main(root) -> dict:
    """The `other-branch` state's fix (§17.3 "go back"): another version of
    the app — a branch Claude made, a detached HEAD, the author's branch
    checked out by hand — is checked out, so nothing updates. Back to
    `fused/mine`: when what is checked out CONTAINS `fused/mine` (or there
    is none yet), `fused/mine` moves there — nothing left behind, commits
    made on the other version carried; else `fused/mine` is checked out and
    the other version is saved (Undo). The author's branch then becomes the
    mirror again (I3). Uncommitted tracked edits → a plain sentence,
    nothing moved."""
    rec, fol = _record(root)
    if rec is None or fol is None:
        return _refuse("not-a-copy", "There is nothing to change here.")
    if _on_home(root):
        return {"ok": True, "message": "You're already on your usual version."}
    if _claude_busy(root):
        return _refuse("claude", CLAUDE_BUSY_MESSAGE)

    def go():
        with replay.slot() as held:
            if not held:
                return _refuse("busy", BUSY_MESSAGE)
            if gu._operation_in_flight(root) is not None:
                return _refuse("in-progress", ELSEWHERE_MESSAGE)
            if _claude_busy(root):
                return _refuse("claude", CLAUDE_BUSY_MESSAGE)
            if replay.tracked_edits(root):
                return _refuse("dirty", "You have unsaved edits in this version. Save or undo "
                                        "them, then try again.")
            _clear_hold(root)
            head = replay.rev(root, "HEAD")
            home = replay.rev(root, classify.HOME_REF)
            backup = None
            if home is None or replay.is_ancestor(root, home, head):
                r = replay.git(root, "switch", "-q", "-C", HOME_BRANCH)
                text = "Went back to your usual version of this app, with your edits."
            else:
                # `switch` overwrites an ignored local file fused/mine tracks
                # without asking: it goes into the backup too.
                clobber = replay.clobber_paths(root, head, home)
                refs = replay.backup_locked(root, head, why="go-back", also=clobber)
                if not refs or (clobber and not any(x.endswith("-wip") for x in refs)):
                    replay.unbackup_locked(root, refs)
                    return _refuse("failed", "Couldn't save this version first; nothing was "
                                             "changed.")
                backup = refs[0]
                with replay.fused_aside(root, home):
                    r = replay.git(root, "switch", "-q", HOME_BRANCH)
                text = "Went back to your usual version of this app. The other version is saved."
            if not gu._ok(r) or not _on_home(root):
                logger.warning("contrib: go back failed in %s: %s", root, gu._text(r))
                if backup:
                    replay.unbackup_locked(root, [backup, backup + "-wip"])
                return _refuse("failed", "Couldn't go back to your usual version; a file here "
                                         "is in the way. Nothing was changed.")
            classify.ensure_home_locked(root, fol.branch)
            replay.protect_after_move(root)
            # The author's branch is not checked out any more: the mirror
            # again. Commits made on it are on fused/mine now (or in the
            # saved version): reset it straight to the author's tip — that is
            # not the author rewinding (no line, no pin).
            old, tip = replay.rev(root, _mirror(fol)), follow.tip(root, fol)
            if fol.pinned:
                # §18.4: a marketplace copy's mirror goes back to the
                # APPROVED commit, never the author's tip (M1).
                tip = replay.rev(root, fol.commit)
            if old and tip and old != tip and not replay.is_ancestor(root, old, tip) and (
                    replay.is_ancestor(root, old, replay.rev(root, "HEAD"))
                    or (backup and replay.is_ancestor(root, old, backup))):
                replay.git(root, "update-ref", _mirror(fol), tip, old)
            follow.update_mirror(root, fol)
        _note_head(root)
        _activity(root, "go-back", text, backup=backup)
        return {"ok": True, "message": text}

    return _user_run(root, go)


def _apply_wip_locked(root, base, wip) -> None:
    """Put a backup's uncommitted edits back as uncommitted edits: every
    path `base..wip` changes gets wip's content in the working tree (index
    left at HEAD); a path wip deleted is removed."""
    r = replay.git(root, "diff", "--name-status", "--no-renames", "-z", base, wip)
    items = replay.names(r)
    for status, rel in zip(items[0::2], items[1::2]):
        path = os.path.join(root, rel)
        if status.startswith("D"):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if gu._ok(replay.git(root, "checkout", wip, "--", rel)):
            replay.git(root, "reset", "-q", "--", rel)


def restore(root, activity_id) -> dict:
    """Undo / Restore an Activity line: back up what is here now (so this,
    too, can be undone), then move fused/mine to the line's backup (`reset
    --keep`, else `--hard` — everything current is in the new backup) with
    its uncommitted edits put back, and/or put its set-aside files back.
    Undo of an automatic move (`_HOLD_KINDS`) sets the undo hold
    (`skip_tip` = the author's tip now)."""
    rec, fol = _record(root)
    entry = store.activity_entry(root, activity_id) if rec is not None else None
    if entry and entry.get("kind") == store.SWITCH_KIND and not entry.get("restored_at"):
        # §18.4 D13: Undo of a switch is the opposite switch (landing on the
        # line's backup when it has one) — market's, which takes the locks.
        return _market().undo_switch(root, entry)
    if rec is None or fol is None:
        return _refuse("not-a-copy", "There is nothing to bring back here.")
    backup = (entry or {}).get("backup")
    if isinstance(backup, list):  # a tip and its -wip: the tip names both
        backup = next((b for b in backup if isinstance(b, str)
                       and not b.endswith("-wip")), None)
    aside = (entry or {}).get("aside") if isinstance((entry or {}).get("aside"), dict) else None
    if not entry or not (backup or aside) or entry.get("restored_at"):
        return _refuse("nothing", "There's nothing to bring back for that.")
    if _is_market(rec) and backup and _blocks().backup_blocked(root, rec, backup):
        # M1: Undo never lands a marketplace copy on a blocked commit.
        return _refuse("blocked", "That version was blocked by Fused, so it can't come back.")
    if _claude_busy(root):
        return _refuse("claude", CLAUDE_BUSY_MESSAGE)

    def go():
        with replay.slot() as held:
            if not held:
                return _refuse("busy", BUSY_MESSAGE)
            if _claude_busy(root):
                return _refuse("claude", CLAUDE_BUSY_MESSAGE)
            new_backup = None
            if backup:
                target = replay.rev(root, backup)
                if not target:
                    return _refuse("gone", "That saved version is no longer on this computer.")
                if not _on_home(root):
                    return _refuse("off-default", OTHER_VERSION_MESSAGE)
                if gu._operation_in_flight(root):
                    return _refuse("in-progress", ELSEWHERE_MESSAGE)
                head = replay.rev(root, "HEAD")
                # As a land: an ignored local file the restored version
                # tracks is overwritten by the move, so it rides in the backup.
                clobber = replay.clobber_paths(root, head, target)
                refs = replay.backup_locked(root, head, why="restore", also=clobber)
                if not refs or (clobber and not any(x.endswith("-wip") for x in refs)):
                    replay.unbackup_locked(root, refs)
                    return _refuse("failed", "Couldn't save what's here first; nothing was "
                                             "changed.")
                new_backup = refs[0]
                held_wip = any(x.endswith("-wip") for x in refs)
                with replay.fused_aside(root, target):
                    r = replay.git(root, "reset", "-q", "--keep", target)
                    # `--hard` only when everything it drops is in the backup
                    # (`reset_hard_locked`'s guard, review P2 #18).
                    if not gu._ok(r) and (held_wip or not (replay.dirty(root) or clobber)):
                        r = replay.git(root, "reset", "-q", "--hard", target)
                if not gu._ok(r):
                    if not held_wip:
                        replay.unbackup_locked(root, refs)
                        return _refuse("failed", "Couldn't bring that version back; unsaved "
                                                 "edits are in the way. Nothing was changed.")
                    return _refuse("failed", "Couldn't bring that version back; what you had is "
                                             "saved in Activity.")
                if not replay.undo_race_locked(root, head, refs, hard=True):
                    return _refuse("moved", "Something changed in this app meanwhile; what you "
                                            "had is saved in Activity. Try again.")
                wip = replay.rev(root, backup + "-wip")
                if wip:
                    _apply_wip_locked(root, target, wip)
                replay.protect_after_move(root)
            back = _unaside(root, aside) if aside else []
        # Still in the aside folder = not back yet (an earlier Restore may
        # have brought the others).
        box = (aside or {}).get("dir") or ""
        left = [p for p in (aside or {}).get("files") or []
                if os.path.lexists(os.path.join(box, p))]
        if not left:
            # A line whose files did not all come back keeps its Restore.
            store.mark_restored(root, activity_id)
        if backup and entry.get("kind") in _HOLD_KINDS:
            # Undo of an automatic move holds: the next automatic sync must
            # not bring the same tip straight back; Bring in still does.
            main = _main(root, fol)
            if main:
                store.update(root, lambda r: r.__setitem__("skip_tip", main))
        _note_head(root)
        t = time.localtime(entry.get("at") or time.time())
        when = f"{time.strftime('%b', t)} {t.tm_mday}, {time.strftime('%H:%M', t)}"
        if backup:
            text = f"Brought back your version from {when}."
        elif not back:
            return _refuse("in-the-way", f"Couldn't put your {_files_words(left)} back: a file "
                                         "with that name is already here. Rename it, then try "
                                         "again.")
        else:
            text = f"Put your {_files_words(back)} back."
            if left:
                text += (f" {_files_words(left)} is still set aside: a file with that name is "
                         "already here.")
        _activity(root, "restore", text, backup=new_backup)
        return {"ok": True, "message": text}

    return _user_run(root, go)


def _unaside(root, aside) -> list:
    """Move set-aside files back. A path the folder now TRACKS takes Bob's
    file as an uncommitted edit (the author's version is in history); an
    untracked file already there is never overwritten."""
    base = aside.get("dir") or ""
    back = []
    for rel in aside.get("files") or []:
        src, dest = os.path.join(base, rel), os.path.join(root, rel)
        if not os.path.lexists(src):
            continue
        tracked = gu._ok(replay.git(root, "ls-files", "--error-unmatch", "--", rel))
        if os.path.lexists(dest) and not tracked:
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(src, dest)
            back.append(rel)
        except OSError:
            logger.warning("contrib: could not put %s back", rel, exc_info=True)
    return back


def push_to_main(root) -> dict:
    """A collaborator's explicit "publish straight to <who>'s app"
    (§17.3): the author's latest must be in (a clean bring-in first), then
    HEAD is pushed to the author's branch BY URL without force; the mirror
    and git's tracking ref move to it (I3), and an open proposal is closed
    (`proposals.settle_direct`)."""
    rec, fol = _record(root)
    if rec is None or fol is None or not rec.get("repo"):
        return _refuse("not-a-copy", "This isn't a copy of someone else's app.")
    who = _who(rec)
    if _claude_busy(root):  # a send waits for Claude like every other (§16.5)
        return _refuse("claude", CLAUDE_BUSY_MESSAGE)
    gh = classify._gh()
    if gh is None:
        return _refuse("signed-out", "Sign in to GitHub first.")
    try:
        info = gh.repo(rec["repo"])
        can = bool(((info or {}).get("permissions") or {}).get("push"))
    except Exception as exc:  # noqa: BLE001
        return _refuse(getattr(exc, "reason", "failed"),
                       getattr(exc, "message", None) or OFFLINE_MESSAGE)
    if not can:
        return _refuse("forbidden", f"You can't add changes directly to {who}'s app. "
                                    "Propose them instead.")
    res = bring_in(root)
    if not res.get("ok") or res.get("needs_merge"):
        if res.get("needs_merge"):
            return _refuse("needs-merge", f"{who} changed the same parts you did. Combine them "
                                          "first.")
        return res
    _clear_hold(root)
    main, head = _main(root, fol), replay.rev(root, "HEAD")
    if not _on_home(root) or not main or not head or not replay.is_ancestor(root, main, head):
        return _refuse("deferred", f"{who}'s latest isn't in your copy yet; try again in a "
                                   "moment.")
    if head == main:
        return {"ok": True, "message": "Nothing to send."}
    if follow.github_redirected(root, fol.url):
        return _refuse("redirected", follow.REDIRECTED_MESSAGE)
    try:
        gh.push(root, head, rec["repo"], fol.branch)
    except Exception as exc:  # noqa: BLE001
        return _refuse(getattr(exc, "reason", "failed"),
                       getattr(exc, "message", None) or "Couldn't send your changes.")
    with replay.slot() as held:
        if held:
            old = follow.tip(root, fol)
            replay.git(root, "update-ref", fol.ref, head, *([old] if old else []))
            follow.update_mirror(root, fol)
    try:
        from fused_render.contrib import proposals
    except Exception:  # noqa: BLE001
        proposals = None
    settle = getattr(proposals, "settle_direct", None)
    if callable(settle) and isinstance(rec.get("proposal"), dict):
        try:
            settle(root, head)
        except Exception:  # noqa: BLE001 — published already; the proposal refresh catches up
            logger.exception("contrib: settle_direct failed for %s", root)
    _activity(root, "publish", f"Published your changes straight to {who}'s app.")
    return {"ok": True, "message": f"Your changes are now in {who}'s app."}


# ------------------------------------------------------- Merge with Claude


def conflict(root) -> dict:
    """The overlap between fused/mine and the author's latest, derived now
    (§17.2) exactly as `status` derives it — with the merged proposal's
    head as the merge base after a squash/rebase accept (`_merged_base`):
    `{"files": [...], "base": sha|None}`; `files` empty when the two merge
    cleanly or main is already in. No network. For proposals (Send/Update
    refuse on an overlap) and Merge with Claude (`merge_plan`)."""
    rec, fol = _record(root)
    none = {"files": [], "base": None}
    if rec is None or fol is None:
        return none
    head, main = replay.rev(root, "HEAD"), _main(root, fol)
    if not head or not main or replay.is_ancestor(root, main, head):
        return none
    base = _merged_base(root, rec, head, main)
    mt = replay.merge_tree(root, head, main, base=base)
    if not mt["supported"] or mt["clean"]:
        return {"files": [], "base": base}
    return {"files": list(mt["files"]) or ["(unknown)"], "base": base}


def merge_plan(root) -> dict | None:
    """What a Merge with Claude job for `root` should do (§17.4): the
    author's latest into fused/mine when the two overlap —
    `{"purpose": "update", "ours": HEAD, "theirs": main, "onto": main,
    "base": sha|None, "files"}` (`base`: the merge base to use — the merged
    proposal's head after a squash/rebase accept, else None for git's own;
    `conflict`) — derived now (nothing is recorded), or None when there is
    no overlap. No network."""
    if not _on_home(root):
        return None
    rec = store.get(root)
    b = (rec or {}).get("block") if _is_market(rec) else None
    if isinstance(b, dict) and b.get("level") == "hard" and b.get("conflict") and b.get("target"):
        # §18.4 B4: a hard block whose rollback overlaps Bob's changes — the
        # revert as a merge (theirs = the safe release, base = the blocked
        # commit), resolved by Claude; `merge_landed` opens the gate.
        head = replay.rev(root, "HEAD")
        mt = replay.merge_tree(root, head, b["target"], base=b["commit"])
        return {"purpose": "update", "ours": head, "theirs": b["target"], "onto": b["target"],
                "base": b["commit"], "files": list(mt.get("files") or []) or ["(unknown)"]}
    c = conflict(root)
    if not c["files"]:
        return None
    rec, fol = _record(root)
    head, main = replay.rev(root, "HEAD"), _main(root, fol)
    return {"purpose": "update", "ours": head, "theirs": main, "onto": main,
            "base": c["base"], "files": c["files"]}


def merge_landed(root, *, purpose, old_tip, new_tip, backup=None) -> None:
    """A Merge with Claude result just landed in the folder (`merge_job.accept`,
    §17.4): the Activity line for its purpose and the head noted. Never
    raises, never pushes — own-sync and remix bring-in hand the push to the
    own path (`ownsync.merge_landed`, else git_upstream's scheduled sync), a
    proposal's merge to `proposals.merge_accepted` (Update sends it)."""
    try:
        _note_head(root, new_tip)
        rec = store.get(root) or {}
        who = _who(rec)
        b = rec.get("block") if _is_market(rec) else None
        if purpose == "update" and isinstance(b, dict) and b.get("conflict"):
            # §18.4 B4: Claude's merge WAS the rollback of a blocked release.
            _blocks().after_merge(root, new_tip)
            _activity(root, "rollback", "Rolled back the blocked version with Claude's help. "
                                        "Your changes are kept.")
        elif purpose == "update":
            _activity(root, "combined", f"Combined your changes with {who}'s latest.",
                      backup=backup)
        elif purpose == "original":
            store.update(root, lambda r: r.__setitem__("original_update", None))
            _activity(root, "original", "Brought in the original app's latest.", backup=backup)
            gu.schedule_sync(root, "Publish after bringing in the original", push=True)
        elif purpose == "proposal":
            # Update's merge of the edits made to the proposal on GitHub:
            # proposals sends the result (no force).
            _activity(root, "proposal", "Combined your changes with the edits made to your "
                                        "proposal.", backup=backup)
            from fused_render.contrib import proposals

            proposals.merge_accepted(root)
        elif purpose == "own-sync":
            _activity(root, "own-sync", "Combined your edits with the version on GitHub.",
                      backup=backup)
            try:
                from fused_render.contrib import ownsync

                hook = getattr(ownsync, "merge_landed", None)
            except Exception:  # noqa: BLE001
                hook = None
            if callable(hook):
                hook(root, old_tip=old_tip, new_tip=new_tip)
            else:
                gu.schedule_sync(root, "Publish after merge", push=True)
    except Exception:  # noqa: BLE001
        logger.exception("contrib: merge bookkeeping failed for %s", root)


# --------------------------------------------------------------------- status

_status_lock = threading.Lock()
#: root -> ((head, main, pr state, pr head), derived history facts)
_status_cache: dict = {}
_STATUS_CACHE_MAX = 256

#: CopyStatus states (§17.2), + a marketplace copy's (§18.4): `delisted`
#: (quiet), `blocked-release` and `soft-blocked` (attention).
STATES = ("following", "changes", "proposed", "merged", "declined", "needs-merge", "blocked",
          "other-branch", "merging", "delisted", "blocked-release", "soft-blocked")


def _job_view(root):
    job = _merge_job(root)
    if not job:
        return None
    return {k: job.get(k) for k in ("id", "status", "summary", "files", "purpose",
                                    "error", "started_at")}


def _pr_head(root, p, main) -> str | None:
    """The proposal's head as this copy knows it (§17.5): Update's fetched
    ref, else the cache's `head_sha` when it is a local object, else
    `main`."""
    got = replay.rev(root, _PR_HEAD_REF)
    if got:
        return got
    sha = ((p or {}).get("cache") or {}).get("head_sha") if p else None
    return replay.rev(root, sha) if isinstance(sha, str) and sha else None


def _history(root, head, main, pr_head, base=None) -> dict:
    """The facts status derives from history alone — no network, cached on
    `(HEAD, main, pr)` by the caller. `base`: the merged proposal's head
    (`_merged_base`), the merge base a bring-in uses after a squash."""
    facts = {"ahead": 0, "behind": 0, "files": [], "conflict": [], "added": [],
             "unproposed": 0, "contains": True}
    if not main:
        return facts
    facts["ahead"] = replay.count(root, "--no-merges", f"{main}..{head}") or 0
    facts["behind"] = replay.count(root, f"{head}..{main}") or 0
    facts["contains"] = replay.is_ancestor(root, main, head)
    if facts["contains"]:
        base = None  # main is in HEAD: git's base (main itself) is exact
    mb = base or _merge_base(root, main, head)
    facts["files"] = sorted(_changed(root, mb, head)) if mb else sorted(_changed(root, main, head))
    if not facts["contains"]:
        mt = replay.merge_tree(root, head, main, base=base)
        if mt["supported"] and not mt["clean"]:
            facts["conflict"] = list(mt["files"]) or ["(unknown)"]
        # History only: which paths main adds. Whether a local file sits
        # there is a working-tree fact, checked on every status (P1 #6).
        facts["added"] = replay.added_paths(root, head, main)
    base = pr_head or main
    facts["unproposed"] = replay.count(root, "--first-parent", "--no-merges",
                                       f"{base}..{head}") or 0
    return facts


def status(root) -> dict:
    """CopyStatus (§17.2) — no network, a few local git calls, the history
    part cached on `(HEAD, main, pr)`. A folder with no record yet is
    classified in the background and answers `pending: True`. Keeps every
    v2 key: `merged_in_tip` (= pr merged), `taken_back` (always False),
    `needs_merge*` (from the overlap) — for one release. `kept_mine`: an
    Undo hold stands on the author's latest (`skip_tip` == main) and it is
    not in the copy (Bring in still offered)."""
    rec = store.get(root)
    view = {
        "kind": "own", "state": "own", "repo": None, "who": None, "author": None,
        "remix_of": None, "changes_files": [], "changes": 0, "dirty": False,
        "unproposed": 0, "behind": 0, "ahead": 0, "needs_merge": False,
        "needs_merge_purpose": None, "needs_merge_files": [], "merge_job": None,
        "blocked_files": [], "kept_mine": False, "off_default": False, "claude_busy": False,
        "merged_in_tip": False, "taken_back": False,
        "last_checked": None, "auto_sync": gu.auto_sync_enabled(),
        "original_update": False, "can_push": None, "owner_checked": True,
        "sync_error": None, "sync_error_code": None, "pending": False, "event": None,
    }
    if rec is None:
        if os.path.isdir(root):
            classify.ensure_in_background(root)
        return {**view, "pending": True}
    remix_of = rec.get("remix_of") if isinstance(rec.get("remix_of"), dict) else None
    job = _job_view(root)
    view.update(kind=rec.get("kind") or "own", repo=rec.get("repo"),
                remix_of=remix_of.get("repo") if remix_of else None,
                original_update=bool(rec.get("original_update")),
                can_push=rec.get("can_push"),
                owner_checked=bool(rec.get("owner_checked", True)),
                last_checked=rec.get("last_checked"), merge_job=job)
    err = rec.get("sync_error") if isinstance(rec.get("sync_error"), dict) else None
    if err and err.get("reason"):
        view["sync_error"] = err.get("message") or _sync_error_words(err["reason"], _who(rec))
        view["sync_error_code"] = err["reason"]
    if not classify.is_github_like(view["kind"]):
        view["state"] = "remix" if remix_of else "own"
        return view
    who = _who(rec)
    view.update(who=who, author=who)
    fol = follow.for_record(rec)
    head = replay.rev(root, "HEAD")
    if fol is None or not head:
        view["state"] = "following"
        return _market_view(root, rec, view, None) if _is_market(rec) else view
    if rec.get("last_head") != head:
        _note_head(root, head)
    main = _main(root, fol)
    p = _proposal(rec)
    pstate = _pr_state(p)
    seen = bool((p or {}).get("settled_seen"))
    pr_head = _pr_head(root, p, main) if pstate == "open" else None
    base = _merged_base(root, rec, head, main) if pstate == "merged" else None
    key = (head, main, pstate, pr_head, base)
    with _status_lock:
        hit = _status_cache.get(root)
    if hit and hit[0] == key:
        facts = hit[1]
    else:
        facts = _history(root, head, main, pr_head, base)
        with _status_lock:
            if len(_status_cache) >= _STATUS_CACHE_MAX:
                _status_cache.clear()
            _status_cache[root] = (key, facts)
    tracked = replay.tracked_edits(root)
    changes = sorted(set(facts["files"]) | set(tracked))
    blocked = [p_ for p_ in facts["added"]
               if p_ not in tracked and os.path.lexists(os.path.join(root, p_))]
    conflict = list(facts["conflict"])
    on_home = _on_home(root)
    view.update(
        changes_files=changes, changes=len(changes), dirty=bool(tracked),
        unproposed=facts["unproposed"] + (1 if tracked else 0), behind=facts["behind"],
        ahead=facts["ahead"], needs_merge=bool(conflict),
        needs_merge_purpose="update" if conflict else None, needs_merge_files=conflict,
        blocked_files=blocked, off_default=not on_home, claude_busy=_claude_busy(root),
        kept_mine=bool(rec.get("skip_tip")) and rec.get("skip_tip") == main
        and facts["behind"] > 0,
        merged_in_tip=pstate == "merged")
    job_open = bool(job) and job.get("status") in ("running", "ready", "landing", "choice")
    if not on_home:
        state = "other-branch"
    elif job_open:
        state = "merging"
    elif conflict:
        state = "needs-merge"
    elif blocked:
        state = "blocked"
    elif pstate == "merged" and not seen:
        state = "merged"
    elif pstate == "closed" and not seen:
        state = "declined"
    elif pstate == "open":
        state = "proposed"
    elif facts["ahead"] or tracked:
        state = "changes"
    else:
        state = "following"
    view["state"] = state
    if _is_market(rec):
        return _market_view(root, rec, view, main)
    return view


def _market_view(root, rec, view, main) -> dict:
    """A marketplace copy's CopyStatus additions (§18.4): its row
    (`slug`, `approved`, `listing`), the block (`blocks.view`), `released`
    (B5: Bob's merged change is in the approved commit) and `live` (the
    engine stamped it so), and the three states it adds — a standing hard
    block wins over everything, a soft one not confirmed over everything
    but a merge, delisted only over a plain `following`."""
    blocks = _blocks()
    p = _proposal(rec)
    approved = rec.get("approved") if isinstance(rec.get("approved"), dict) else None
    view.update(slug=rec.get("slug"),
                approved={k: v for k, v in approved.items() if k not in ("prev", "from_row")}
                if approved else None,
                listing=rec.get("listing"), block=blocks.view(rec.get("block")),
                released=bool(p and main and _landed_in(root, p, main)),
                live=bool(p and p.get("live")))
    b = rec.get("block") if isinstance(rec.get("block"), dict) else {}
    if blocks.standing(rec):
        view["state"] = "blocked-release"
    elif b.get("level") == "soft" and not b.get("confirmed_at") \
            and view["state"] not in ("merging", "other-branch"):
        view["state"] = "soft-blocked"
    elif view["state"] == "following" and _market().is_delisted(rec):
        view["state"] = "delisted"
    return view
