"""The contribution loop's git engine for a GitHub-link copy
(docs/CONTRIB_LOOP.md §16.3): what the copy's state is, and the moves that
keep it on the author's latest with Bob's changes kept — never losing them,
never sending them anywhere he did not choose.

THE MODEL (§16.0). A `github` record's copy stays on its local default
branch. "The author's latest" is git's own `refs/remotes/origin/<branch>`
(one upstream, P3), fetched by the RECORD's URL (`follow.fetch`). Bob's
changes are whatever HEAD has that the tip does not. The author's latest
comes in by MERGING it (P4): a fast-forward when Bob has nothing of his own,
else a merge commit whose first parent is Bob's HEAD — so Bob's commits keep
their ids forever (a version-history link he kept still works, a proposal's
`sent` stays in HEAD) and a merge drops nothing, so it needs no backup. The
only rewrite of Bob's commits is A4's return (row 7), which re-applies
exactly the edits he made after sending onto the author's tip.

THE ENGINE NEVER PUSHES (P2). No automatic path here sends anything
anywhere; `push_to_main` is the one push, an explicit collaborator action,
by URL. An `own` copy is never the engine's (P5: git_upstream's plain
auto-sync + ownsync).

ONE DECISION (`decide`, its docstring is the table). Every move —
background sync, Bring in, push-to-main's bring-in — gathers the facts
first (`Facts`), asks the pure `decide`, and only then executes. A state
is recorded in the form whose UI is visible (§16.12): rows 8–11 with a
clean merge run whatever the flags (an automatic, local, undoable move is
not a stuck state); rows 5, 6 and an 11 that overlaps become v2 states
(blocked / "Your edits" / needs_merge) only with `flags.sources()` on —
their fix buttons live under it — and otherwise today's git_upstream
failure row (`git-failed` / `dirty` / `diverged`) in plain words, tree
untouched. Row 7 needs a proposal, so `flags.changes()`.

WHERE IT RUNS. git_upstream's intercept hands a `github` root to `sync`,
slot held. `sync` gives the slot up for the whole run (`slot_released`) and
every write takes it back briefly: the fetch (it writes a ref git's own
fetches write too), a "Your edits" commit, a land (backup + final check +
`reset --keep`). GitHub reads (`proposals.refresh`) never run under it
(§0.9). One run per copy at a time (`_copy_lock`). A Claude turn open in the
folder gates tree moves only (P8, `_claude_busy`). Every user action here
takes the same lock and slot around its writes.

EVERY AUTOMATIC MOVE SAYS SO (P6). Each move writes one Activity line
(`store.add_activity`) — with Undo (a backup ref) when it moved something
away — and each is also a button (`bring_in`, `keep_mine`, `use_theirs`,
`move_aside`, `go_back_main`, `discard`, `restore`, `push_to_main`). Every
stuck state is a plain sentence with a fix in `status`. No git word ever
reaches a returned message (§0.1).

A DELIBERATE EXCEPTION TO D245 ("a manual edit commits nothing"), for
GitHub-link copies only (row 6): uncommitted edits to TRACKED files the
author's side of an update changes would make `reset --keep` refuse that
update on every trigger, forever and silently. So exactly those paths are
committed first as one "Your edits" commit (Bob's identity) and the update
merges over it. They stay local — nothing leaves the machine without
Propose. An untracked file the update would overwrite is never committed:
that is row 5 (Move my file aside).

v1 → v2 (P3) is `classify.settle_locked`'s (git_upstream's intercept runs it
before every sync; a button that finds a v1 record runs it too): v1 refs
deleted, a v1 `needs_merge` cleared, v1 `backups` mapped to Activity lines,
`v: 2`.
"""
from __future__ import annotations

from dataclasses import dataclass

import hashlib
import logging
import os
import threading
import time

from fused_render import git_upstream as gu
from fused_render.contrib import classify, flags, follow, replay, store

logger = logging.getLogger(__name__)

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

# --------------------------------------------------------------------- helpers


def _who(rec) -> str:
    repo = rec.get("repo") if isinstance(rec, dict) else None
    return repo.split("/", 1)[0] if isinstance(repo, str) and "/" in repo else "the author"


def _out(status, why=None, **extra) -> dict:
    """A run's outcome: `status` is `synced` | `noop` | `skipped` |
    `offline` | `needs-merge` | `blocked` | `deferred`, plus `why`,
    `move` (which row moved), `pulled`, `files`, `tip`, `branch`."""
    return {"status": status, "why": why, "pulled": 0, "pushed": False, **extra}


#: The Merge with Claude purposes that run on a GitHub-link copy.
_COPY_PURPOSES = ("update", "return", "proposal")


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
    subs = [s.strip() for s in gu._out(r).splitlines() if s.strip()] if gu._ok(r) else []
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
    if not rec or rec.get("kind") != "github":
        return None, None
    return rec, follow.for_record(rec)


# ------------------------------------------------------------------ v1 → v2


def _migrate_locked(root, rec) -> dict:
    """The first v2 run on this repo when git_upstream's intercept has not
    settled it yet (a button): `classify.settle_locked` — v1 record
    converted (`store.migrate_v2`), v1 refs deleted, the copy's guards.
    Slot held. Answers the record."""
    return classify.settle_locked(root, "github") or rec


# ----------------------------------------------------------------------- sync


def sync(root, *, action, push) -> dict:
    """One background sync of a `github` root (§16.3), called by
    git_upstream's intercept with the slot held. Same return shape as
    `_sync_locked`; never a push (`push` is ignored: P2). A `failed` status
    only for the flags-off stuck rows (§16.12, `_fail`); every other outcome
    clears a standing failure row."""
    with replay.slot_held():
        res = None
        try:
            rec = store.get(root)
            if rec is None or rec.get("kind") != "github":
                return gu._skipped("unavailable")
            _housekeep(root, rec)
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


def _as_sync_result(root, res) -> dict:
    st = res["status"]
    if st == "offline":
        return {"ok": True, "status": "offline", "pulled": 0}
    if st == "failed":
        out = {"ok": False, "status": "failed", "failure": res.get("failure"), "pulled": 0,
               "pushed": False}
        if res.get("branch") and res.get("tip"):
            out["state"] = _state(root, res["branch"], res["tip"])
        return out
    out = {"ok": True, "pulled": res.get("pulled", 0), "pushed": False}
    if st in ("synced", "noop"):
        out["status"] = "synced"
    else:
        out["status"] = "skipped"
        out["why"] = res.get("why") or st
    if res.get("branch") and res.get("tip"):
        out["state"] = _state(root, res["branch"], res["tip"])
    return out


def _state(root, branch, tip) -> dict:
    """A `check_repo`-shaped dict, so git_upstream refreshes its cache and
    App Doctor reads the same picture."""
    head = replay.rev(root, "HEAD")
    ahead = (replay.count(root, f"{tip}..{head}") or 0) if head else 0
    behind = (replay.count(root, f"{head}..{tip}") or 0) if head else 0
    return {"root": root, "branch": branch, "default_branch": branch, "on_default": True,
            "ahead": ahead, "behind": behind, "checked_at": time.time()}


def _run(root, *, user, action="Bring in") -> dict:
    """Fetch, refresh a proposal the new tip may have merged, gather the
    facts, `decide`, execute — the one path for `sync` and every button
    that brings the author's latest in."""
    _inside.depth = getattr(_inside, "depth", 0) + 1
    try:
        return _run_inner(root, user=user, action=action)
    finally:
        _inside.depth -= 1


def _run_inner(root, *, user, action) -> dict:
    rec, fol = _record(root)
    if rec is None:
        return _out("skipped", "not-a-copy")
    if fol is None:
        return _out("skipped", "no-source")
    if not os.path.isdir(root):
        return _out("skipped", "unavailable")
    with replay.slot() as held:
        if not held:
            return _out("deferred", "slot-busy")
        if rec.get("v") != store.RECORD_V:
            rec = _migrate_locked(root, rec)
        # Fetch first whatever is checked out, so the page can still say how
        # far behind the copy is (V5); only the move needs the default branch.
        tip, reason = follow.fetch(root, fol)
        off_default = gu._current_branch(root) != fol.branch
        in_flight = gu._operation_in_flight(root) is not None
    if tip is None:
        if reason == "offline":
            return _out("offline")
        _note_sync_error(root, reason)
        return _out("skipped", reason)
    _note_checked(root)
    if off_default:
        return _out("skipped", "not-default")
    if in_flight:
        return _out("skipped", "in-progress")
    done = {"branch": fol.branch, "tip": tip}
    if not user and not gu.auto_sync_enabled():
        # Fetched for the page's "updates waiting"; the move is the user's.
        return _out("skipped", "auto-sync-off", **done)
    if flags.changes():
        _refresh_if_moved(root, tip)
    for _round in range(3):  # another round only after committing "Your edits"
        rec = store.get(root) or rec
        f = _facts(root, rec, fol, tip, user=user)
        if f is None:
            return _out("skipped", "unreadable", **done)
        res = _execute(root, rec, fol, f, decide(f), action=action)
        if res is not _AGAIN:
            return res
    return _out("deferred", "edits", **done)


def _proposal_unsettled(p) -> bool:
    if not isinstance(p, dict) or not p.get("number") or p.get("returned_at"):
        return False
    state = (p.get("cache") or {}).get("state")
    return state in ("open", "merged", None)


def _refresh_if_moved(root, tip) -> None:
    """The author's tip moved since the proposal was last checked against
    it: ask GitHub first (`proposals.refresh(force=True, tip=)`, slot NOT
    held), so a merged proposal is known BEFORE the tip is merged in — row
    7, not row 11 (audit R3). refresh records `checked_tip = tip` only on a
    good read, so an offline read asks again next run."""
    rec = store.get(root) or {}
    p = rec.get("proposal")
    if not _proposal_unsettled(p) or p.get("checked_tip") == tip:
        return
    try:
        from fused_render.contrib import proposals

        proposals.refresh(root, force=True, tip=tip)
    except Exception:  # noqa: BLE001 — the cached state stands; next run asks again
        logger.exception("contrib: proposal refresh failed for %s", root)


# ------------------------------------------------------------ the decision


@dataclass(frozen=True)
class Facts:
    """Everything `decide` looks at, gathered once per round (`_facts`).
    Plain values only — `decide` runs no git and reads no store."""
    head: str
    tip: str
    who: str
    user: bool                 # a button (Bring in), not a background sync
    sources_on: bool           # flags.sources() — v2 states for rows 5, 6, 11-overlap
    changes_on: bool           # flags.changes() — row 7 (a proposal) needs it
    claude_busy: bool          # a Claude turn is open in the folder
    job_open: bool             # a Merge with Claude job is open
    standing: bool             # needs_merge recorded for exactly (tip, HEAD)
    kept_mine: bool            # skip_tip == tip (Keep mine)
    clobber: tuple             # untracked/ignored local files the tip adds
    blocking: tuple            # tracked uncommitted edits the move would refuse on
    merged: bool               # proposal merged on GitHub, its merge commit ⊆ tip
    tip_in_head: bool
    head_in_tip: bool
    same_tree: bool            # tree(HEAD) == tree(tip)


@dataclass(frozen=True)
class Action:
    kind: str                  # see `decide`'s table
    why: str | None = None


def decide(f: Facts) -> Action:
    """The v2 table (§16.3). Rows are checked top to bottom; the first that
    matches decides. No git, no store, no clock: same facts, same action.

    | # | situation                                    | action                  |
    |---|----------------------------------------------|-------------------------|
    | 1 | a Claude turn is open in the folder          | wait (`claude`)         |
    | 2 | a Merge with Claude job is open              | skip (`merge-job`)      |
    | 3 | needs_merge recorded for this (tip, HEAD)    | standing                |
    | 4 | skip_tip == tip (Keep mine), not a button    | skip (`kept-mine`)      |
    | 5 | an untracked/ignored local file is a path    | blocked (`clobber`) →   |
    |   |   the tip adds                               |   fix Move my file aside|
    | 6 | uncommitted tracked edits to files the move  | commit-edits ("Your     |
    |   |   changes (merge-base..tip; + HEAD..tip for  |   edits"), decide again |
    |   |   a return)                                  |                         |
    | 7 | proposal merged, its merge commit ⊆ tip      | return                  |
    | 8 | tip ⊆ HEAD                                   | noop                    |
    | 9 | HEAD ⊆ tip                                   | ff (`reset --keep tip`) |
    |10 | tree(HEAD) == tree(tip)                      | move (backup, Undo)     |
    |11 | otherwise                                    | merge (merge commit, or |
    |   |                                              |   needs_merge `update`) |

    Flags (§16.12): rows 8–11 with a clean merge run whatever the flags.
    With `sources_on` False, row 5 is `fail` (`git-failed`), row 6 `fail`
    (`dirty`), and an overlapping row 11 records `diverged` instead of a
    needs_merge (`_execute`) — today's git_upstream failure row, in plain
    words, tree untouched — because the v2 fix buttons live under sources.
    Row 7 runs only with `changes_on` (it needs a proposal).

    Rows 5–6 sit above 7 on purpose: the return lands with the same
    `reset --keep`, so a local file it would overwrite or a dirty edit it
    would refuse on stops it exactly as it stops an update. Their facts are
    only gathered when the tree would move (`_facts`), so they never fire
    on a noop. `return` re-applies `HEAD ^sent ^tip` (first parent, no
    merges — the edits made after sending) onto the tip; a conflict there
    is a needs_merge `return`. A land that cannot happen DEFERS with why
    (`slot-busy`, `claude`, `moved`, `in-progress`, `reset-refused`) and the
    next trigger looks again."""
    if f.claude_busy:
        return Action("wait", "claude")
    if f.job_open:
        return Action("skip", "merge-job")
    if f.standing:
        return Action("standing", "needs-merge")
    if f.kept_mine and not f.user:
        return Action("skip", "kept-mine")
    if f.clobber:
        return Action("blocked", "clobber") if f.sources_on else Action("fail", "git-failed")
    if f.blocking:
        return Action("commit-edits") if f.sources_on else Action("fail", "dirty")
    if f.merged and f.changes_on:
        return Action("return")
    if f.tip_in_head:
        return Action("noop")
    if f.head_in_tip:
        return Action("ff")
    if f.same_tree:
        return Action("move")
    return Action("merge")


def _merge_in_tip(root, p, tip) -> bool:
    """The proposal is merged on GitHub and its merge commit is in `tip`."""
    cache = p.get("cache") if isinstance(p, dict) and isinstance(p.get("cache"), dict) else {}
    sha = cache.get("merge_commit_sha")
    return cache.get("state") == "merged" and bool(sha) and replay.is_ancestor(root, sha, tip)


def _merged_in(root, rec, tip) -> bool:
    """Row 7's fact: a merged proposal in the tip, not returned yet."""
    p = rec.get("proposal") if isinstance(rec.get("proposal"), dict) else None
    return bool(p) and not p.get("returned_at") and _merge_in_tip(root, p, tip)


def _taken_back(root, p, tip) -> bool:
    """The author's latest no longer holds what was merged (merged, then
    reverted): re-applying it to the tip is clean but changes the tip. What
    is re-applied: the sent version when the tip does not contain it (a
    squash or rebase merge), else the merge commit's own change (on its
    first parent). An overlap (maintainer edits) is not called taken back."""
    sent = replay.rev(root, (p or {}).get("sent") or "")
    msha = replay.rev(root, ((p or {}).get("cache") or {}).get("merge_commit_sha") or "")
    if sent and not replay.is_ancestor(root, sent, tip):
        mt = replay.merge_tree(root, tip, sent)
    elif msha and replay.rev(root, f"{msha}^1"):
        mt = replay.merge_tree(root, tip, msha, base=replay.rev(root, f"{msha}^1"))
    else:
        return False
    return mt["supported"] and mt["clean"] and mt["tree"] != replay.tree_of(root, tip)


def _facts(root, rec, fol, tip, *, user) -> Facts | None:
    head = replay.rev(root, "HEAD")
    if not head:
        return None
    tip_in_head = replay.is_ancestor(root, tip, head)
    head_in_tip = replay.is_ancestor(root, head, tip)
    merged = _merged_in(root, rec, tip)
    nm = rec.get("needs_merge") if isinstance(rec.get("needs_merge"), dict) else None
    standing = bool(nm) and nm.get("tip") == tip and nm.get("head") == head
    clobber = blocking = ()
    if not tip_in_head or merged:
        tracked = set(replay.tracked_edits(root))
        if tracked:
            side = _changed(root, _merge_base(root, head, tip), tip)
            if merged:
                side |= _changed(root, head, tip)
            blocking = tuple(sorted(tracked & side))
        clobber = tuple(p for p in replay.clobber_paths(root, head, tip) if p not in tracked)
    return Facts(
        head=head, tip=tip, who=_who(rec), user=user, sources_on=flags.sources(),
        changes_on=flags.changes(),
        claude_busy=_claude_busy(root), job_open=_job_open(root), standing=standing,
        kept_mine=bool(rec.get("skip_tip")) and rec.get("skip_tip") == tip,
        clobber=clobber, blocking=blocking, merged=merged,
        tip_in_head=tip_in_head, head_in_tip=head_in_tip,
        same_tree=replay.tree_of(root, head) == replay.tree_of(root, tip))


_AGAIN = object()


def _land(root, branch, old, new, why) -> tuple:
    """A move's only write: under the slot, a Claude turn re-checked, then
    `replay.land_locked` (backup + final check + `reset --keep`).
    `(None, backup)` when it landed, else `(why, None)`."""
    with replay.slot() as held:
        if not held:
            return "slot-busy", None
        if _claude_busy(root):
            return "claude", None
        return replay.land_locked(root, branch=branch, old_tip=old, new_tip=new, why=why)


def _note_head(root, head=None) -> None:
    try:
        store.note_head(root, head or replay.rev(root, "HEAD"))
    except Exception:  # noqa: BLE001 — bookkeeping for adoption only
        logger.exception("contrib: note_head failed for %s", root)


def _settled(root, *, clear_proposal=False):
    """After a land or a noop: a recorded overlap and a Keep mine no longer
    apply (HEAD contains the tip now); the record notes where HEAD is
    (`store.note_head`, adoption after a Finder move)."""
    _note_head(root)

    def fn(r):
        r["needs_merge"] = None
        r["skip_tip"] = None
        if clear_proposal and isinstance(r.get("proposal"), dict):
            r["proposal"]["returned_at"] = time.time()
    rec = store.get(root) or {}
    if clear_proposal or rec.get("needs_merge") or rec.get("skip_tip"):
        store.update(root, fn)


def _fail_words(f: Facts, why, files) -> str:
    names = _files_words(files)
    if why == "dirty":
        return (f"You have unsaved edits to {names}, and {f.who}'s update changes "
                f"{'that file' if len(files) == 1 else 'those files'} too. Save or undo "
                "them to get the update.")
    if why == "git-failed":
        return (f"{f.who}'s update adds {names}, which you already have here. Rename "
                "yours to get the update.")
    return (f"Your copy and {f.who}'s latest both changed {names}, so fused can't bring "
            "the update in on its own.")


def _fail(root, fol, f: Facts, why, files, *, action) -> dict:
    """The flags-off form of a stuck row (§16.12): today's git_upstream
    failure row (`git-failed` / `dirty` / `diverged`) with a plain sentence,
    tree untouched, nothing recorded on the contrib record. A button gets
    the sentence back instead."""
    done = {"branch": fol.branch, "tip": f.tip}
    words = _fail_words(f, why, files)
    failure = None
    if not f.user:
        failure = gu.record_sync_failure(
            root, why, action=action, push=False, message=words,
            command=f"git pull --ff-only -- origin {fol.branch}").get("failure")
    return _out("failed", why, failure=failure, message=words, files=list(files), **done)


def _execute(root, rec, fol, f: Facts, a: Action, *, action="Bring in"):
    """Carry out `a` (see `decide`). Answers an outcome, or `_AGAIN` after
    committing "Your edits" (the caller decides once more)."""
    done = {"branch": fol.branch, "tip": f.tip}
    if a.kind == "fail":
        return _fail(root, fol, f, a.why, f.clobber if a.why == "git-failed" else f.blocking,
                     action=action)
    if a.kind == "wait":
        return _out("deferred", a.why, **done)
    if a.kind == "skip":
        return _out("skipped", a.why, **done)
    if a.kind == "standing":
        return _out("needs-merge", "needs-merge", **done)
    if a.kind == "blocked":
        return _out("blocked", "clobber", files=list(f.clobber), **done)
    if a.kind == "commit-edits":
        return _commit_edits(root, f) or _out("deferred", "edits", **done)
    if a.kind == "return":
        return _return(root, rec, fol, f)
    if a.kind == "noop":
        _settled(root)
        return _out("noop", **done)

    behind = replay.count(root, f"{f.head}..{f.tip}") or 0
    subjects = _subjects(root, _merge_base(root, f.head, f.tip), f.tip)
    if a.kind in ("ff", "move"):
        new = f.tip
    else:  # merge
        m = replay.merge_into(root, head=f.head, onto=f.tip,
                              message=f"Bring in {f.who}'s latest")
        if m["conflict"] and not f.sources_on:
            return _fail(root, fol, f, "diverged", m["files"], action=action)
        if m["conflict"]:
            def fn(r):
                old = r.get("needs_merge") if isinstance(r.get("needs_merge"), dict) else {}
                r["needs_merge"] = {"purpose": "update", "tip": f.tip, "head": f.head,
                                    "files": list(m["files"])[:50],
                                    "since": old.get("since") or time.time()}
            store.update(root, fn)
            return _out("needs-merge", "needs-merge", files=list(m["files"]), **done)
        if not m["ok"]:
            return _out("deferred", "merge", **done)
        new = m["tip"]
    why, backup = _land(root, fol.branch, f.head, new, "update")
    if why:
        return _out("deferred", why, **done)
    gu._record_pull(root, behind)
    _settled(root)
    if a.kind == "ff":
        text = f"Got {f.who}'s latest" + (f": {subjects}." if subjects else ".")
    elif a.kind == "move":
        text = f"Got {f.who}'s latest. Your copy already matched it."
    else:
        text = (f"Brought in {f.who}'s {subjects}, your changes kept." if subjects
                else f"Brought in {f.who}'s latest, your changes kept.")
    _activity(root, "update", text, backup=backup)
    return _out("synced", move=a.kind, pulled=behind, **done)


def _commit_edits(root, f: Facts):
    """Row 6: commit exactly the blocking paths as "Your edits" (module
    docstring, the D245 exception). `_AGAIN`, or None (defer)."""
    with replay.slot() as held:
        if not held or _claude_busy(root) or replay.rev(root, "HEAD") != f.head:
            return None
        made = replay.commit_edits_locked(root, f.blocking)
    if not made:
        return None
    _activity(root, "edits-saved",
              f"Saved your edit to {_files_words(f.blocking)} so {f.who}'s update could come in.")
    return _AGAIN


def _return(root, rec, fol, f: Facts) -> dict:
    """Row 7 (A4): Bob's proposal is in the author's latest. New HEAD = the
    tip plus exactly the edits made after sending
    (`HEAD ^sent ^tip`, first parent, no merges), re-applied in memory or
    scratch (`replay.pick`); none → the tip itself. Lands with a backup
    (the proposed commits leave HEAD's history). A conflict is a needs_merge
    `return` (Merge with Claude then merges the tip into HEAD instead)."""
    done = {"branch": fol.branch, "tip": f.tip}
    p = rec.get("proposal") or {}
    sent = replay.rev(root, p.get("sent") or "")
    spec = [f.head, "^" + f.tip] + (["^" + sent] if sent else [])
    r = replay.git(root, "rev-list", "--reverse", "--first-parent", "--no-merges", *spec)
    picks = gu._out(r).split() if gu._ok(r) else []
    if picks:
        res = replay.pick(root, onto=f.tip, commits=picks)
        if res["conflict"] or not res["supported"]:
            def fn(rr):
                rr["needs_merge"] = {"purpose": "return", "tip": f.tip, "head": f.head,
                                     "files": list(res["files"])[:50], "since": time.time()}
            store.update(root, fn)
            return _out("needs-merge", "needs-merge", files=list(res["files"]), **done)
        if not res["ok"]:
            return _out("deferred", "return", **done)
        new, kept = res["tip"], res["kept"]
    else:
        new, kept = f.tip, 0
    why, backup = _land(root, fol.branch, f.head, new, "return")
    if why:
        return _out("deferred", why, **done)
    _settled(root, clear_proposal=True)
    _activity(root, "return", _return_text(f.who, kept, taken_back=_taken_back(root, p, f.tip)),
              backup=backup)
    return _out("synced", move="return", **done)


def _return_text(who, kept, *, taken_back=False) -> str:
    text = (f"{who} merged your change, then took it back." if taken_back
            else f"{who} merged your change.") + f" You're on {who}'s latest version."
    if kept:
        text += f" {kept} edit{'s' if kept != 1 else ''} you made since kept."
    return text


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
    if why == "not-default":
        return _refuse("off-default", "You're on a different version of this app. Go back to "
                                      "the main version first.")
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
    """Bring in the author's latest now (§16.3: the same table, `user`
    True — so Keep mine does not stop it, and it runs with auto-sync off).
    `{ok, message, reason?, needs_merge?, files?}`."""
    rec, fol = _record(root)
    if rec is None or fol is None:
        return _refuse("not-a-copy", "There is nothing to update here.")
    who = _who(rec)
    if getattr(_inside, "depth", 0):
        return _refuse("busy", BUSY_MESSAGE)
    res = _user_run(root, lambda: _run(root, user=True))
    return _bring_in_answer(res, who)


def _bring_in_answer(res, who) -> dict:
    st = res["status"]
    if st == "synced":
        msg = {"merge": f"Brought in {who}'s latest, your changes kept.",
               "return": _return_text(who, 0)}.get(res.get("move"), f"You're on {who}'s latest.")
        return {"ok": True, "message": msg}
    if st == "noop":
        return {"ok": True, "message": f"You already have {who}'s latest."}
    if st == "needs-merge":
        return {"ok": True, "needs_merge": True, "files": res.get("files") or [],
                "message": f"{who} changed the same parts you did. Claude can combine "
                           "them for you, or you can keep one version."}
    if st == "blocked":
        files = res.get("files") or []
        return _refuse("blocked", f"{who}'s update adds {_files_words(files)}, which you "
                                  "already have here. Move yours aside to get the update.",
                       files=files)
    if st == "failed":  # a stuck row with the v2 buttons hidden (§16.12)
        return _refuse(res.get("why") or "failed", res.get("message") or "",
                       files=res.get("files") or [])
    return _deferred_message(res, who)


def keep_mine(root) -> dict:
    """Keep mine: stop bringing in THIS tip of the author's on its own
    (`skip_tip`), and drop a recorded overlap; Bring in stays offered."""
    rec, fol = _record(root)
    if rec is None or fol is None:
        return _refuse("not-a-copy", "There is nothing to keep here.")
    who, tip = _who(rec), follow.tip(root, fol)
    _discard_job(root)

    def fn(r):
        r["skip_tip"] = tip
        r["needs_merge"] = None
    store.update(root, fn)
    _activity(root, "keep-mine", f"Kept your version. {who}'s latest is waiting until you "
                                 "bring it in.")
    return {"ok": True, "message": f"Kept your version. You can bring in {who}'s latest "
                                   "any time."}


def _hard_to_tip(root, *, why, kind, text, message) -> dict:
    """Use theirs / Discard: backup (incl. every uncommitted edit), then
    `reset --hard` the author's tip. Refused while a Claude turn is open."""
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
            if gu._current_branch(root) != fol.branch or gu._operation_in_flight(root):
                return _refuse("in-progress", ELSEWHERE_MESSAGE)
            # Only now, with the move sure to be attempted: a ready Merge
            # with Claude result is not thrown away by a refused click.
            _discard_job(root)
            tip, _reason = follow.fetch(root, fol)
            tip = tip or follow.tip(root, fol)
            if not tip:
                return _refuse("offline", f"Can't reach {who}'s version right now; try again "
                                          "when you're online.")
            if _claude_busy(root):
                return _refuse("claude", CLAUDE_BUSY_MESSAGE)
            moved, backup = replay.reset_hard_locked(root, branch=fol.branch, target=tip,
                                                     why=why)
        if not moved:
            return _refuse("failed", f"Couldn't switch to {who}'s version; nothing was "
                                     "changed.")
        _settled(root)
        _activity(root, kind, text.format(who=who), backup=backup)
        return {"ok": True, "message": message.format(who=who)}

    return _user_run(root, go)


def use_theirs(root) -> dict:
    """Use <who>'s version: the author's tip exactly, mine saved (Undo)."""
    return _hard_to_tip(
        root, why="use-theirs", kind="use-theirs",
        text="Switched to {who}'s version. Your version is saved.",
        message="You're on {who}'s version. Your version is saved in Activity.")


def discard(root, *, withdraw: bool = False) -> dict:
    """Discard my changes: as Use theirs; `withdraw` also withdraws an open
    proposal (default no)."""
    rec = store.get(root) or {}
    out = _hard_to_tip(
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


def _aside_root(root) -> str:
    from fused_render.shell import storage

    tag = hashlib.sha1(store.key(root).encode("utf-8")).hexdigest()[:16]
    return os.path.join(storage.home_dir(), "contrib", "aside", tag)


def move_aside(root) -> dict:
    """Row 5's fix: move the local files the author's update adds out of the
    folder (to `<home>/contrib/aside/<key>/<ms>/…`, Restore puts them back),
    then Bring in."""
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
            tip, head = follow.tip(root, fol), replay.rev(root, "HEAD")
            tracked = set(replay.tracked_edits(root))
            files = [p for p in replay.clobber_paths(root, head, tip) if p not in tracked]
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
    """Another version of the app (a branch, or a detached HEAD) is checked
    out — Claude ran `checkout -b`, say — so nothing updates. Go back to the
    default branch: when it is part of what is checked out, the default
    branch MOVES there (nothing left behind); else it is checked out and
    the other version is saved (Undo). Uncommitted tracked edits → a plain
    sentence, nothing moved."""
    rec, fol = _record(root)
    if rec is None or fol is None:
        return _refuse("not-a-copy", "There is nothing to change here.")
    if gu._current_branch(root) == fol.branch:
        return {"ok": True, "message": "You're already on the main version."}
    if _claude_busy(root):
        return _refuse("claude", CLAUDE_BUSY_MESSAGE)

    def go():
        with replay.slot() as held:
            if not held:
                return _refuse("busy", BUSY_MESSAGE)
            if gu._operation_in_flight(root) is not None:
                return _refuse("in-progress", ELSEWHERE_MESSAGE)
            if replay.tracked_edits(root):
                return _refuse("dirty", "You have unsaved edits in this version. Save or undo "
                                        "them, then try again.")
            head = replay.rev(root, "HEAD")
            main = replay.rev(root, f"refs/heads/{fol.branch}")
            backup = None
            if main is None or replay.is_ancestor(root, main, head):
                r = replay.git(root, "checkout", "-q", "-B", fol.branch)
                text = "Went back to the main version of this app, with your edits."
            else:
                # `checkout` overwrites an ignored local file the default
                # branch tracks without asking: it goes into the backup too.
                clobber = replay.clobber_paths(root, head, main)
                refs = replay.backup_locked(root, head, why="go-back", also=clobber)
                if not refs or (clobber and not any(x.endswith("-wip") for x in refs)):
                    replay.unbackup_locked(root, refs)
                    return _refuse("failed", "Couldn't save this version first; nothing was "
                                             "changed.")
                backup = refs[0]
                with replay.fused_aside(root, main):
                    r = replay.git(root, "checkout", "-q", fol.branch)
                text = "Went back to the main version of this app. The other version is saved."
            if not gu._ok(r):
                logger.warning("contrib: go back failed in %s: %s", root, gu._text(r))
                if backup:
                    replay.unbackup_locked(root, [backup, backup + "-wip"])
                return _refuse("failed", "Couldn't go back to the main version; a file here is "
                                         "in the way. Nothing was changed.")
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
    too, can be undone), then move to the line's backup (`reset --keep`,
    else `--hard` — everything current is in the new backup) with its
    uncommitted edits put back, and/or put its set-aside files back."""
    rec, fol = _record(root)
    if rec is None or fol is None:
        return _refuse("not-a-copy", "There is nothing to bring back here.")
    entry = store.activity_entry(root, activity_id)
    backup = (entry or {}).get("backup")
    if isinstance(backup, list):  # a tip and its -wip: the tip names both
        backup = next((b for b in backup if isinstance(b, str)
                       and not b.endswith("-wip")), None)
    aside = (entry or {}).get("aside") if isinstance((entry or {}).get("aside"), dict) else None
    if not entry or not (backup or aside) or entry.get("restored_at"):
        return _refuse("nothing", "There's nothing to bring back for that.")
    if _claude_busy(root):
        return _refuse("claude", CLAUDE_BUSY_MESSAGE)

    def go():
        with replay.slot() as held:
            if not held:
                return _refuse("busy", BUSY_MESSAGE)
            new_backup = None
            if backup:
                target = replay.rev(root, backup)
                if not target:
                    return _refuse("gone", "That saved version is no longer on this computer.")
                if gu._current_branch(root) != fol.branch or gu._operation_in_flight(root):
                    return _refuse("in-progress", ELSEWHERE_MESSAGE)
                head = replay.rev(root, "HEAD")
                # As `land_locked`: an ignored local file the restored version
                # tracks is overwritten by the move, so it rides in the backup.
                clobber = replay.clobber_paths(root, head, target)
                refs = replay.backup_locked(root, head, why="restore", also=clobber)
                if not refs or (clobber and not any(x.endswith("-wip") for x in refs)):
                    replay.unbackup_locked(root, refs)
                    return _refuse("failed", "Couldn't save what's here first; nothing was "
                                             "changed.")
                new_backup = refs[0]
                with replay.fused_aside(root, target):
                    r = replay.git(root, "reset", "-q", "--keep", target)
                    if not gu._ok(r):
                        r = replay.git(root, "reset", "-q", "--hard", target)
                if not gu._ok(r):
                    return _refuse("failed", "Couldn't bring that version back; what you had is "
                                             "saved in Activity.")
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
        _settled(root)
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
    """A collaborator's explicit "publish straight to <who>'s app" (§16.5):
    bring the author's latest in first (it must land clean), push HEAD to
    the author's branch BY URL without force, mark the tip as published
    here, and close an open proposal (`proposals.settle_direct`)."""
    rec, fol = _record(root)
    if rec is None or fol is None or not rec.get("repo"):
        return _refuse("not-a-copy", "This isn't a copy of someone else's app.")
    who = _who(rec)
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
    try:
        from fused_render.contrib import proposals
    except Exception:  # noqa: BLE001
        proposals = None
    credit = getattr(proposals, "credit_once", None)
    if callable(credit):
        c = credit(root) or {}
        if c.get("ok") is False:
            return _refuse(c.get("reason") or "failed",
                           c.get("message") or "Couldn't get your changes ready to send.")
    res = bring_in(root)
    if not res.get("ok") or res.get("needs_merge"):
        if res.get("needs_merge"):
            return _refuse("needs-merge", f"{who} changed the same parts you did. Combine them "
                                          "first.")
        return res
    tip, head = follow.tip(root, fol), replay.rev(root, "HEAD")
    if not tip or not head or not replay.is_ancestor(root, tip, head):
        return _refuse("deferred", f"{who}'s latest isn't in your copy yet; try again in a "
                                   "moment.")
    if head == tip:
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
            replay.git(root, "update-ref", fol.ref, head)
    settle = getattr(proposals, "settle_direct", None)
    if callable(settle) and isinstance(rec.get("proposal"), dict):
        try:
            settle(root, head)
        except Exception:  # noqa: BLE001 — published already; the proposal refresh catches up
            logger.exception("contrib: settle_direct failed for %s", root)
    _activity(root, "publish", f"Published your changes straight to {who}'s app.")
    return {"ok": True, "message": f"Your changes are now in {who}'s app."}


# ------------------------------------------------------- Merge with Claude


def merge_plan(root) -> dict | None:
    """What a Merge with Claude job for `root` should do, from the recorded
    overlap: `{"purpose": "update"|"return", "ours", "theirs", "onto",
    "files"}` (`onto` = `theirs`, the v1 key), or None when there is none
    or HEAD moved since it was recorded. No network."""
    rec = store.get(root)
    nm = rec.get("needs_merge") if rec and isinstance(rec.get("needs_merge"), dict) else None
    if not nm or nm.get("purpose") not in ("update", "return"):
        return None
    ours, theirs = replay.rev(root, nm.get("head") or ""), replay.rev(root, nm.get("tip") or "")
    if not (ours and theirs) or replay.rev(root, "HEAD") != ours:
        return None
    return {"purpose": nm["purpose"], "ours": ours, "theirs": theirs, "onto": theirs,
            "files": list(nm.get("files") or [])}


def merge_landed(root, *, purpose, old_tip, new_tip, backup=None, **_legacy) -> None:
    """A Merge with Claude result just landed in the folder (`merge_job.accept`):
    the record and Activity for its purpose. Never raises, never pushes —
    own-sync and remix bring-in hand the push to the own path
    (`ownsync.merge_landed`, else git_upstream's scheduled sync), a
    proposal's merge to `proposals.merge_accepted` (Update sends it)."""
    try:
        _note_head(root, new_tip)
        rec = store.get(root) or {}
        who = _who(rec)
        if purpose in ("update", "return"):
            _settled(root, clear_proposal=purpose == "return")
            text = (_return_text(who, 0) if purpose == "return"
                    else f"Combined your changes with {who}'s latest.")
            _activity(root, purpose, text, backup=backup)
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
_status_cache: dict = {}  # root -> (key, (changes_files, behind, unproposed))
_taken_cache: dict = {}   # (root, tip, sent, merge commit) -> taken_back
_STATUS_CACHE_MAX = 256



def _job_view(root):
    job = _merge_job(root)
    if not job:
        return None
    return {k: job.get(k) for k in ("id", "status", "summary", "files", "purpose",
                                    "error", "started_at")}


def _counts(root, head, tip, sent) -> tuple:
    """`(changes_files, behind, unproposed commits)` against the tip — no
    network. `changes_files` diffs the tip's merge-base with HEAD, so files
    only the author changed since are never "your changes"; it equals
    `diff tip HEAD` whenever HEAD contains the tip."""
    files = []
    behind = 0
    if tip:
        mb = _merge_base(root, tip, head)
        files = sorted(_changed(root, mb, head)) if mb else sorted(_changed(root, tip, head))
        behind = replay.count(root, f"{head}..{tip}") or 0
    spec = [head] + (["^" + tip] if tip else []) + (["^" + sent] if sent else [])
    unproposed = replay.count(root, "--first-parent", "--no-merges", *spec) or 0
    return files, behind, unproposed


def status(root) -> dict:
    """CopyStatus (§16.3) — no network, a few local git calls, the history
    part cached on the refs it depends on. A folder with no record yet is
    classified in the background and answers `pending: True`."""
    rec = store.get(root)
    view = {
        "kind": "own", "state": "own", "repo": None, "who": None, "author": None,
        "remix_of": None, "changes_files": [], "changes": 0, "dirty": False,
        "unproposed": 0, "behind": 0, "needs_merge": False, "needs_merge_purpose": None,
        "needs_merge_files": [], "merge_job": None, "blocked_files": [],
        "kept_mine": False, "off_default": False, "claude_busy": False, "merged_in_tip": False,
        "taken_back": False,
        "last_checked": None, "auto_sync": gu.auto_sync_enabled(),
        "original_update": False, "can_push": None, "owner_checked": True,
        "sync_error": None, "sync_error_code": None, "pending": False, "event": None,
    }
    if rec is None:
        if os.path.isdir(root):
            classify.ensure_in_background(root)
        return {**view, "pending": True}
    remix_of = rec.get("remix_of") if isinstance(rec.get("remix_of"), dict) else None
    view.update(kind=rec.get("kind") or "own", repo=rec.get("repo"),
                remix_of=remix_of.get("repo") if remix_of else None,
                original_update=bool(rec.get("original_update")),
                can_push=rec.get("can_push"),
                owner_checked=bool(rec.get("owner_checked", True)),
                last_checked=rec.get("last_checked"), merge_job=_job_view(root))
    err = rec.get("sync_error") if isinstance(rec.get("sync_error"), dict) else None
    if err and err.get("reason"):
        view["sync_error"] = err.get("message") or _sync_error_words(err["reason"], _who(rec))
        view["sync_error_code"] = err["reason"]
    if view["kind"] != "github":
        view["state"] = "remix" if remix_of else "own"
        return view
    who = _who(rec)
    view.update(who=who, author=who)
    fol = follow.for_record(rec)
    head = replay.rev(root, "HEAD")
    if fol is None or not head:
        view["state"] = "following"
        return view
    if rec.get("last_head") != head:
        _note_head(root, head)
    tip = follow.tip(root, fol)
    proposal = rec.get("proposal") if isinstance(rec.get("proposal"), dict) else None
    sent = replay.rev(root, (proposal or {}).get("sent") or "")
    key = (head, tip, sent)
    with _status_lock:
        hit = _status_cache.get(root)
    if hit and hit[0] == key:
        files, behind, unproposed = hit[1]
    else:
        files, behind, unproposed = _counts(root, head, tip, sent)
        with _status_lock:
            if len(_status_cache) >= _STATUS_CACHE_MAX:
                _status_cache.clear()
            _status_cache[root] = (key, (files, behind, unproposed))
    tracked = replay.tracked_edits(root)
    changes = sorted(set(files) | set(tracked))
    nm = rec.get("needs_merge") if isinstance(rec.get("needs_merge"), dict) else None
    view.update(
        changes_files=changes, changes=len(changes), dirty=bool(tracked),
        unproposed=unproposed + (1 if tracked else 0), behind=behind,
        needs_merge=bool(nm), needs_merge_purpose=(nm or {}).get("purpose"),
        needs_merge_files=list((nm or {}).get("files") or []),
        kept_mine=bool(tip) and rec.get("skip_tip") == tip and behind > 0,
        off_default=gu._current_branch(root) != fol.branch,
        claude_busy=_claude_busy(root),
        merged_in_tip=bool(proposal and tip) and _merge_in_tip(root, proposal, tip))
    # Merged, then reverted by the author (the merged sentence says so).
    if view["merged_in_tip"]:
        tkey = (root, tip, sent, (proposal.get("cache") or {}).get("merge_commit_sha"))
        with _status_lock:
            hit = _taken_cache.get(tkey)
        if hit is None:
            hit = _taken_back(root, proposal, tip)
            with _status_lock:
                if len(_taken_cache) >= _STATUS_CACHE_MAX:
                    _taken_cache.clear()
                _taken_cache[tkey] = hit
        view["taken_back"] = hit
    if tip and behind:
        view["blocked_files"] = [p for p in replay.clobber_paths(root, head, tip)
                                 if p not in tracked]
    pstate = ((proposal or {}).get("cache") or {}).get("state") if proposal else None
    seen = bool((proposal or {}).get("settled_seen"))
    if nm:
        state = "needs-merge"
    elif pstate == "merged" and not seen:
        state = "merged"
    elif pstate == "closed" and not seen:
        state = "declined"
    elif pstate == "open":
        state = "proposed"
    elif changes:
        state = "changes"
    else:
        state = "following"
    view["state"] = state
    return view
