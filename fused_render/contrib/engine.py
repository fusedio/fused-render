"""The contribution loop's git engine (docs/CONTRIB_LOOP.md §6): what a copy's
state is, and the moves that keep a GitHub-link copy on the author's latest
with Bob's changes on top — never losing them, never pushing them anywhere
he did not choose.

THE MODEL (§6.1). A GitHub-link copy stays on its local default branch.
Bob's changes are the commits on it the follow tip does not have; the engine
only ever REBASES them (in a scratch worktree — replay.py), so the branch
never holds a merge and "changes" is a plain `rev-list` count. Nothing else
in the app (the Switch card, Doctor's git row, `↑N` marks) ever sees an
off-default branch, because there is none.

WHERE IT RUNS. `git_upstream._sync_locked` — both auto-sync triggers (an app
open, an app-made commit) pass through it — asks `owns(root)` first and hands
an owned root to `sync`, slot already held. Everything else here is a user
action (the routers): `discard`, `push_to_main`, `return_to_following`,
each acquiring the slot itself (`replay.slot`), with every GitHub call made
OUTSIDE it (§0.9).

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
clears any standing one): an update that cannot land yet — an uncommitted
edit in the way, the slot busy, the author unreachable — DEFERS and is
retried on the next trigger; an overlap with the author's change is recorded
as `needs_merge` (the page's one button, Merge with Claude) with Bob's tree
untouched. The only things the page is ever told are events (`updated`,
`updated-kept`, `merged-back`, `discarded`) and that one decision.

WHAT "BASE" MEANS. `refs/fused/base` is the follow tip Bob's changes sit on
(advanced by every land); `upstream` for a replay is that base when HEAD
contains it, else the merge-base. With an author force-push the merge-base
would reach back into the author's OWN rewritten history and replay it as if
it were Bob's; the base never does.

A PROPOSAL SURVIVES AN AUTO-INTEGRATE. Rebasing Bob's commits gives them new
ids, so `proposal.proposed_head` would fall out of HEAD's history and every
"N edits not in your proposal" count would become "all of them". The replay
is split at `proposed_head` (`replay.replay(mark=…)`) and the engine writes
where it landed back into the proposal (and `refs/fused/proposed`, which
pins the object).
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


def _base(root, head, new_tip) -> str | None:
    """The commit Bob's changes sit on (module docstring "WHAT BASE MEANS")."""
    mb = gu._out(gu._run(root, "merge-base", head, new_tip)) or None
    base = replay.rev(root, replay.BASE_REF)
    if base and replay.is_ancestor(root, base, head):
        if mb and replay.is_ancestor(root, base, mb):
            return mb
        return base
    return mb


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


def _done(root, branch, *, status="synced", why=None, pulled=0, pushed=False, tip=None):
    out = {"ok": True, "status": status, "pulled": pulled, "pushed": pushed,
           "state": _state(root, branch, tip=tip)}
    if why:
        out["why"] = why
    return out


def _set_needs_merge(root, *, onto, upstream, head, purpose, files):
    def fn(r):
        old = r.get("needs_merge") if isinstance(r.get("needs_merge"), dict) else {}
        r["needs_merge"] = {
            "onto": onto, "upstream": upstream, "head": head, "purpose": purpose,
            "files": list(files or [])[:50],
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


def _carry_proposal(root, rec, mark_tip):
    """A replay moved the proposed commits: point the proposal (and its pin
    ref) at where they landed."""
    if not mark_tip or not isinstance(rec.get("proposal"), dict):
        return

    def fn(r):
        if isinstance(r.get("proposal"), dict):
            r["proposal"]["proposed_head"] = mark_tip
    store.update(root, fn)
    gu._run(root, "update-ref", replay.PROPOSED_REF, mark_tip)


def _proposed_mark(root, rec, head, base):
    p = rec.get("proposal") if isinstance(rec.get("proposal"), dict) else None
    sha = p.get("proposed_head") if p else None
    if not sha or p.get("returned_at"):
        return None
    if replay.is_ancestor(root, sha, head) and not (base and replay.is_ancestor(root, sha, base)):
        return sha
    return None


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
            # be the author's own repo). A fresh install is active at once —
            # with no GitHub identity it could not push anyway.
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
            if rec.get("kind") == "github":
                return _sync_github(root, rec, action=action)
            return _sync_own(root, rec, action=action, push=push)
        finally:
            gu._clear_failures(root)


def _preflight(root, branch):
    if not os.path.isdir(root):
        return gu._skipped("unavailable")
    if gu._current_branch(root) != branch:
        return gu._skipped("not-default")
    if gu._operation_in_flight(root) is not None:
        return gu._skipped("in-progress")
    return None


def _sync_github(root, rec, *, action):
    fol = follow.for_record(rec)
    if fol is None:
        return gu._skipped("no-remote")
    branch = fol.branch
    refusal = _preflight(root, branch)
    if refusal:
        return refusal
    new_tip, _reason = follow.fetch_detail(root, fol)
    if new_tip is None:
        # Offline, the repo gone or private to this account: silent, like
        # every unreachable remote in auto-sync; retried next trigger.
        return {"ok": True, "status": "offline"}
    if flags.sources():
        replay.protect_locked(root)
    if not gu.auto_sync_enabled():
        return _done(root, branch, why="auto-sync-off", status="skipped", tip=new_tip)
    head = replay.rev(root, "HEAD")
    if not head:
        return gu._skipped("unreadable")
    who = _owner(rec)

    # A4 first: a merged proposal the follow tip now contains is a RETURN,
    # not an overlap — integrating Bob's original commits onto the author's
    # squash of them would conflict for no reason.
    if flags.loop() and _proposal_merged_in(root, rec, new_tip):
        result = _return_locked(root, rec, fol, new_tip)
        status = "synced" if result.get("ok") and result.get("landed") else "skipped"
        return _done(root, branch, status=status, why=None if status == "synced" else "return",
                     tip=new_tip)

    if _job_open(root):
        return _done(root, branch, status="skipped", why="merge-job", tip=new_tip)

    if replay.is_ancestor(root, new_tip, head):
        # Nothing of the author's is missing: following, or changes on top.
        replay.advance_base_locked(root, head)
        if rec.get("needs_merge"):
            _clear(root, "needs_merge")
        return _done(root, branch, tip=new_tip)

    behind = replay.count(root, f"{head}..{new_tip}") or 0
    if replay.is_ancestor(root, head, new_tip):
        # No changes of Bob's: a plain fast-forward (reset --keep carries
        # any uncommitted edit the update does not touch; one it does
        # defers the update).
        if not replay.land_locked(root, branch=branch, old_tip=head, new_tip=new_tip,
                                  why="update", app_dir=root):
            return _done(root, branch, status="skipped", why="deferred", tip=new_tip)
        gu._record_pull(root, behind)
        store.add_event(root, "updated", who=who)
        _clear(root, "needs_merge")
        return _done(root, branch, pulled=behind, tip=new_tip)

    # Bob has changes AND the author moved: replay them onto the new tip.
    nm = rec.get("needs_merge") if isinstance(rec.get("needs_merge"), dict) else None
    if nm and nm.get("onto") == new_tip and nm.get("head") == head:
        return _done(root, branch, status="skipped", why="needs-merge", tip=new_tip)
    base = _base(root, head, new_tip)
    if not base:
        return _done(root, branch, status="skipped", why="unrelated", tip=new_tip)
    if set(_dirty_paths(root)) & _changed_between(root, base, new_tip):
        # An uncommitted edit to a file the author changed: the land would
        # refuse anyway, so do not pay for the replay. Retried next trigger.
        return _done(root, branch, status="skipped", why="deferred", tip=new_tip)
    res = replay.replay(root, onto=new_tip, upstream=base, branch=head,
                        mark=_proposed_mark(root, rec, head, base))
    if res["ok"]:
        if not replay.land_locked(root, branch=branch, old_tip=head, new_tip=res["tip"],
                                  why="update-kept", app_dir=root):
            return _done(root, branch, status="skipped", why="deferred", tip=new_tip)
        _carry_proposal(root, rec, res["mark_tip"])
        gu._record_pull(root, behind)
        store.add_event(root, "updated-kept", who=who)
        _clear(root, "needs_merge")
        return _done(root, branch, pulled=behind, tip=new_tip)
    if res["conflict"]:
        _set_needs_merge(root, onto=new_tip, upstream=base, head=head,
                         purpose="needs-merge", files=res["files"])
        return _done(root, branch, status="skipped", why="needs-merge", tip=new_tip)
    return _done(root, branch, status="skipped", why="deferred", tip=new_tip)


def _sync_own(root, rec, *, action, push):
    """An own copy with a GitHub repo (§6.4): today's fast-forward and push,
    except that a divergence (local unpushed commits while origin moved —
    e.g. the author accepted a proposal on GitHub) is replayed onto origin's
    tip and pushed instead of standing as a failure; an overlap is a
    `needs_merge` (purpose own-sync)."""
    fol = follow.for_record(rec)
    if fol is None or not gu._ok(gu._run(root, "remote", "get-url", "origin")):
        return gu._skipped("no-remote")
    branch = fol.branch
    refusal = _preflight(root, branch)
    if refusal:
        return refusal
    origin_tip, reason = follow.fetch_detail(root, fol)
    if origin_tip is None:
        if reason not in (None, "offline"):
            _note_sync_error(root, reason)
        return {"ok": True, "status": "offline"}
    if flags.sources():
        replay.protect_locked(root)
    if not gu.auto_sync_enabled():
        return _done(root, branch, why="auto-sync-off", status="skipped", tip=origin_tip)
    head = replay.rev(root, "HEAD")
    if not head:
        return gu._skipped("unreadable")
    if _job_open(root):
        return _done(root, branch, status="skipped", why="merge-job", tip=origin_tip)

    pulled = 0
    if not replay.is_ancestor(root, origin_tip, head):
        behind = replay.count(root, f"{head}..{origin_tip}") or 0
        if replay.is_ancestor(root, head, origin_tip):
            if not replay.land_locked(root, branch=branch, old_tip=head, new_tip=origin_tip,
                                      why="update", app_dir=root):
                return _done(root, branch, status="skipped", why="deferred", tip=origin_tip)
        else:
            nm = rec.get("needs_merge") if isinstance(rec.get("needs_merge"), dict) else None
            if nm and nm.get("onto") == origin_tip and nm.get("head") == head:
                return _done(root, branch, status="skipped", why="needs-merge", tip=origin_tip)
            base = gu._out(gu._run(root, "merge-base", head, origin_tip)) or None
            if not base:
                return _done(root, branch, status="skipped", why="unrelated", tip=origin_tip)
            if set(_dirty_paths(root)) & _changed_between(root, base, origin_tip):
                return _done(root, branch, status="skipped", why="deferred", tip=origin_tip)
            res = replay.replay(root, onto=origin_tip, upstream=base, branch=head)
            if res["conflict"]:
                _set_needs_merge(root, onto=origin_tip, upstream=base, head=head,
                                 purpose="own-sync", files=res["files"])
                return _done(root, branch, status="skipped", why="needs-merge", tip=origin_tip)
            if not (res["ok"] and replay.land_locked(
                    root, branch=branch, old_tip=head, new_tip=res["tip"],
                    why="own-sync", app_dir=root)):
                return _done(root, branch, status="skipped", why="deferred", tip=origin_tip)
        pulled = behind
        gu._record_pull(root, pulled)
        _clear(root, "needs_merge")
        head = replay.rev(root, "HEAD")

    pushed = False
    if push and head and not replay.is_ancestor(root, head, origin_tip):
        r = gu._run(root, "push", "--", "origin", branch)
        if not gu._ok(r):
            why = gu._classify(r, push=True)
            if why == "offline":
                return {"ok": True, "status": "offline", "pulled": pulled}
            if why == "rejected":
                # origin moved again since the fetch: the next trigger
                # integrates and pushes.
                return _done(root, branch, status="skipped", why="deferred",
                             pulled=pulled, tip=origin_tip)
            _note_sync_error(root, why)
            return _done(root, branch, status="skipped", why=why, pulled=pulled, tip=origin_tip)
        pushed = True
        origin_tip = head  # the push moved origin's tracking ref there too
    _clear(root, "sync_error")
    return _done(root, branch, pulled=pulled, pushed=pushed, tip=origin_tip)


# ------------------------------------------------------------ A4: the return


def _proposal_merged_in(root, rec, tip) -> bool:
    p = rec.get("proposal") if isinstance(rec.get("proposal"), dict) else None
    if not p or p.get("returned_at"):
        return False
    cache = p.get("cache") if isinstance(p.get("cache"), dict) else {}
    sha = cache.get("merge_commit_sha")
    return cache.get("state") == "merged" and bool(sha) and replay.is_ancestor(root, sha, tip)


def _return_locked(root, rec, fol, tip) -> dict:
    """Bob's proposal is in the author's latest: move to that tip, keeping
    only the commits he never proposed (`proposed_head..HEAD`). Slot held.
    Answers `{"ok", "landed", "conflict", "onto", "upstream"}`."""
    head = replay.rev(root, "HEAD")
    p = rec.get("proposal") or {}
    proposed = p.get("proposed_head")
    if not head:
        return {"ok": False, "landed": False, "conflict": False}
    if proposed and replay.is_ancestor(root, proposed, head):
        upstream = proposed
    else:
        # The proposed commits are not in HEAD's history any more (a merge
        # job rewrote them): replay everything since the merge-base and let
        # the rebase drop what the author's tip already holds.
        upstream = gu._out(gu._run(root, "merge-base", head, tip)) or None
    if not upstream:
        return {"ok": False, "landed": False, "conflict": False}
    if upstream == head:
        new_tip = tip
    else:
        res = replay.replay(root, onto=tip, upstream=upstream, branch=head)
        if res["conflict"]:
            _set_needs_merge(root, onto=tip, upstream=upstream, head=head,
                             purpose="return", files=res["files"])
            return {"ok": True, "landed": False, "conflict": True,
                    "onto": tip, "upstream": upstream}
        if not res["ok"]:
            return {"ok": False, "landed": False, "conflict": False}
        new_tip = res["tip"]
    if not replay.land_locked(root, branch=fol.branch, old_tip=head, new_tip=new_tip,
                              why="merged-back", app_dir=root):
        return {"ok": False, "landed": False, "conflict": False, "deferred": True}

    def fn(r):
        r["needs_merge"] = None
        if isinstance(r.get("proposal"), dict):
            r["proposal"]["returned_at"] = time.time()
    store.update(root, fn)
    store.add_event(root, "merged-back", who=_owner(rec))
    return {"ok": True, "landed": True, "conflict": False}


def return_to_following(root) -> dict:
    """A4 (§6.4), user/refresh-triggered: fetch, then replay the unproposed
    edits onto the author's latest. A clean result lands (event
    `merged-back`); an overlap starts a Merge with Claude job (purpose
    `return`) after the slot is released."""
    rec = store.get(root)
    if not rec or rec.get("kind") != "github" or not isinstance(rec.get("proposal"), dict):
        return {"ok": False, "reason": "no-proposal", "message": "There is no proposal to finish."}
    fol = follow.for_record(rec)
    if fol is None:
        return {"ok": False, "reason": "no-source", "message": "This copy has no source to return to."}
    with replay.slot() as held:
        if not held:
            return {"ok": False, "reason": "busy", "message": "Busy updating this app; try again in a moment."}
        refusal = _preflight(root, fol.branch)
        if refusal:
            return {"ok": False, "reason": refusal.get("why"),
                    "message": "This app is in the middle of something else; try again later."}
        tip = follow.fetch(root, fol) or follow.tip(root, fol)
        rec = store.get(root) or rec
        if not tip or not _proposal_merged_in(root, rec, tip):
            return {"ok": False, "reason": "not-yet",
                    "message": f"{_owner(rec)}'s latest version doesn't include your change yet."}
        result = _return_locked(root, rec, fol, tip)
    if result.get("landed"):
        return {"ok": True, "message": f"You're on {_owner(rec)}'s latest version."}
    if result.get("conflict"):
        try:
            from fused_render.contrib import merge_job

            job = merge_job.start(root, purpose="return", onto=result["onto"],
                                  upstream=result["upstream"])
        except Exception:  # noqa: BLE001 — the needs_merge record stands; the page offers the button
            logger.exception("contrib: could not start the return merge for %s", root)
            job = None
        return {"ok": True, "job": job,
                "message": f"{_owner(rec)} changed the same parts you did."}
    if result.get("deferred"):
        return {"ok": False, "reason": "edits",
                "message": "Your unsaved edits overlap the update; it will finish once they are saved."}
    return {"ok": False, "reason": "failed", "message": "Couldn't finish updating this app right now."}


# ------------------------------------------------------- Merge with Claude


def merge_plan(root) -> dict | None:
    """What a Merge with Claude job for `root` should replay, from the
    recorded overlap: `{"purpose", "onto", "upstream"}`, or None when there
    is none (or its commits are gone). `upstream` is exactly what the
    engine's own replay used — the base for needs-merge, origin's tip
    before for own-sync, `proposed_head` for return. No network."""
    rec = store.get(root)
    nm = rec.get("needs_merge") if rec and isinstance(rec.get("needs_merge"), dict) else None
    if not nm:
        return None
    onto, upstream = replay.rev(root, nm.get("onto") or ""), replay.rev(root, nm.get("upstream") or "")
    if not (onto and upstream):
        return None
    purpose = nm.get("purpose") if nm.get("purpose") in ("needs-merge", "own-sync", "return") \
        else "needs-merge"
    return {"purpose": purpose, "onto": onto, "upstream": upstream}


def merge_landed(root, *, purpose, old_tip, new_tip) -> None:
    """A Merge with Claude job's result just landed (`merge_job.accept`
    called `replay.land`): the record bookkeeping for its purpose. Never
    raises."""
    try:
        rec = store.get(root) or {}
        who = _owner(rec)
        now = time.time()

        def fn(r):
            r["needs_merge"] = None
            if purpose == "return" and isinstance(r.get("proposal"), dict):
                r["proposal"]["returned_at"] = now
            if purpose == "original":
                r["original_update"] = None
        store.update(root, fn)
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


def discard(root, *, withdraw: bool) -> dict:
    """Discard my changes (§6.4): back up HEAD and the dirty tree, then move
    to the author's latest. `withdraw` also withdraws an open proposal
    (default no — the handoff's default). Untracked files Bob made are left
    where they are; nothing untracked is ever deleted."""
    rec = store.get(root)
    if not rec or rec.get("kind") != "github":
        return {"ok": False, "reason": "not-a-copy",
                "message": "Only a copy of someone else's app can go back to their version."}
    fol = follow.for_record(rec)
    if fol is None:
        return {"ok": False, "reason": "no-source", "message": "This copy has no source to go back to."}
    who = _owner(rec)
    job = _merge_job(root)
    if job and job.get("id") and job.get("status") in ("running", "ready"):
        try:
            from fused_render.contrib import merge_job

            merge_job.discard(job["id"])
        except Exception:  # noqa: BLE001
            logger.exception("contrib: could not discard the merge job for %s", root)
    with replay.slot() as held:
        if not held:
            return {"ok": False, "reason": "busy", "message": "Busy updating this app; try again in a moment."}
        if gu._current_branch(root) != fol.branch or gu._operation_in_flight(root):
            return {"ok": False, "reason": "in-progress",
                    "message": "This app is in the middle of something else; try again later."}
        tip = follow.fetch(root, fol) or follow.tip_or_origin(root, fol)
        if not tip:
            return {"ok": False, "reason": "offline",
                    "message": f"Can't reach {who}'s version right now; try again when you're online."}
        if not replay.reset_hard_locked(root, branch=fol.branch, target=tip, why="discard",
                                        app_dir=root):
            return {"ok": False, "reason": "failed",
                    "message": "Couldn't go back to the original version; nothing was changed."}
        _clear(root, "needs_merge")
        store.add_event(root, "discarded", who=who)
    out = {"ok": True, "message": f"Back to {who}'s version. Your changes are saved in a backup."}
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
    gh = classify._gh()
    if gh is None or fol is None:
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
    with replay.slot() as held:
        if not held:
            return {"ok": False, "reason": "busy", "message": "Busy updating this app; try again in a moment."}
        sync_result = _sync_github(root, store.get(root) or rec, action="Push to main")
        rec = store.get(root) or rec
        if rec.get("needs_merge"):
            return {"ok": False, "reason": "needs-merge",
                    "message": f"{who} changed the same parts you did. Merge first."}
        tip = follow.tip(root, fol)
        head = replay.rev(root, "HEAD")
        if sync_result.get("status") == "offline" or not tip or not head:
            return {"ok": False, "reason": "offline", "message": "Can't reach GitHub right now."}
        if not replay.is_ancestor(root, tip, head):
            return {"ok": False, "reason": "deferred",
                    "message": f"{who}'s latest isn't in your copy yet; try again in a moment."}
        if head == tip:
            return {"ok": True, "message": "Nothing to send."}
    try:
        gh.push(root, head, rec["repo"], fol.branch)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": getattr(exc, "reason", "failed"),
                "message": getattr(exc, "message", None) or "Couldn't send your changes."}
    with replay.slot() as held:
        if held:
            gu._run(root, "update-ref", replay.FOLLOW_REF, head)
            replay.advance_base_locked(root, head)
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
    r = gu._run(root, "cherry", proposed, head, *( [tip] if tip else []))
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
        "merge_job": None, "event": None, "original_update": False, "can_push": None,
        "behind": 0, "sync_error": None, "sync_error_code": None, "pending": False,
    }
    if rec is None:
        if os.path.isdir(root):
            classify.ensure_in_background(root)
        return {**base_view, "pending": True}
    remix_of = rec.get("remix_of") if isinstance(rec.get("remix_of"), dict) else None
    view = {
        **base_view,
        "kind": rec.get("kind") or "own",
        "repo": rec.get("repo"),
        "remix_of": remix_of.get("repo") if remix_of else None,
        "needs_merge": bool(rec.get("needs_merge")),
        "merge_job": _job_view(root),
        "event": _event(rec),
        "original_update": bool(rec.get("original_update")),
        "can_push": rec.get("can_push"),
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
    tip = (refs.get(fol.ref) or refs.get(f"refs/remotes/origin/{fol.branch}")) if fol else None
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
