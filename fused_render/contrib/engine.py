"""The contribution loop's git engine (docs/CONTRIB_LOOP.md §6): what a copy's
state is, and the moves that keep a GitHub-link copy on the author's latest
with Bob's changes on top — never losing them, never pushing them anywhere
he did not choose.

THE MODEL (§6.1). A GitHub-link copy stays on its local default branch.
Bob's changes are the commits on it the follow tip does not have. The
engine brings the author's latest in by REBASING them onto it (in a scratch
worktree — replay.py), so "changes" is a `rev-list --no-merges` count and
nothing else in the app ever sees an off-default branch. Every clean rebase
is held to the three-way merge of the same two sides
(`replay._replay_unsound`) before it may land; only when that check fails —
a merge commit in the range (Claude or Bob ran `git merge`) carried a hand
resolution a rebase drops — is the update brought in with a merge commit
instead.

ONE DECISION (`decide`). Every integrate — background sync, Bring in,
push-to-main — gathers the facts first (`Facts`: HEAD, the tip, the base,
the proposal, a recorded overlap, an open job, blocking edits, files the
update would overwrite, a Claude turn running), asks the pure `decide` (its
docstring is the decision table, one row per situation), and only then
executes. A situation the table does not name cannot happen by accident.

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
and is retried on the next trigger. An overlap with the author's change is
recorded as `needs_merge` (the page's one button, Merge with Claude) with
Bob's tree untouched; a local file the update would overwrite (an ignored
`.env` the author now tracks) is a `needs_merge` with no button and a
sentence naming the file to rename.

A DELIBERATE EXCEPTION TO D245 ("a manual edit commits nothing"), for
GitHub-link copies only: uncommitted edits to files the AUTHOR's side of an
update changes (`base..tip` — never a file only Bob touched) would make
`reset --keep` refuse that update on every trigger, forever and silently.
So exactly those paths are committed first as one "Your edits" commit
(`replay.commit_edits_locked(paths=blocking_edits)` — an untracked file
only Bob made stays out) and ride the update like the rest of Bob's
changes. They stay local — nothing leaves the machine without Propose — and
an own app is never committed for: there they are a `needs_merge` with no
button and a sentence naming the file.

WHAT "BASE" MEANS. `refs/fused/base` is the follow tip Bob's changes sit on
(advanced by every land); `upstream` for a replay is that base when HEAD
contains it, else origin's tracking ref when HEAD contains that (a fresh
install), else the merge-base. With an author force-push the merge-base
would reach back into the author's OWN rewritten history and replay it as if
it were Bob's; the base never does.

AN OWN COPY NEVER RE-PUBLISHES WHAT ITS AUTHOR REMOVED (reviews r01, r2,
r3 — one invariant, `decide`'s docstring). `synced` (`_synced`) is the
newest BY HISTORY of every published point HEAD contains: the tracking
ref's value pinned before each engine fetch (`refs/fused/origin-seen`,
forward-only), every value its REFLOG records (old and new sides — every
value that ref ever held came from GitHub), and `refs/fused/base`. An
automatic push, and the replay that would feed one, happens only when
GitHub's new tip CONTAINS `synced`. A commit the author deliberately
removed (a leaked key, rewritten away or simply rewound) was published, so
it is in `synced`, so a tip without it is never pushed onto: a no-button
sentence instead, the edits staying where they are. No reflog and no pin
means `synced` is unknown — the same answer.

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

from dataclasses import dataclass

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
#: Each copy's aged-out update backups are pruned at most once a day.
_PRUNE_EVERY_S = 24 * 3600.0
_last_prune: dict = {}

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
    # --no-merges: a merge commit the engine made to bring the author's
    # latest in (the merge strategy) is not an edit of Bob's.
    args = ["rev-list", "--count", "--no-merges", head]
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
                     mark=None, message=None, rewritten=False, button=True):
    """Record the decision the page is waiting for. `button` False: no Merge
    with Claude for this one (`merge_plan` answers None) — the page shows
    `message`, the plain sentence of what the user can do instead."""
    def fn(r):
        old = r.get("needs_merge") if isinstance(r.get("needs_merge"), dict) else {}
        r["needs_merge"] = {
            "onto": onto, "upstream": upstream, "head": head, "purpose": purpose,
            "strategy": strategy, "mark": mark, "message": message,
            "rewritten": bool(rewritten), "button": bool(button) and not rewritten,
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


def _land(root, branch, old, new, why, *, mark_tip=None) -> str | None:
    """The only write of an integration: backup + final check + move, under
    the slot. None when it landed; else the `deferred` why — `slot-busy`,
    `claude` (a turn is running in the folder), or `land_locked`'s refusal
    (`moved`: the branch moved since the plan was made, a fresh plan is the
    next trigger's; `in-progress`; `reset-refused`)."""
    with replay.slot() as held:
        if not held:
            return "slot-busy"
        if _claude_busy(root):
            return "claude"
        refused = replay.land_locked(root, branch=branch, old_tip=old, new_tip=new, why=why,
                                     app_dir=root)
        if refused:
            return refused
        _carry_proposal_locked(root, mark_tip)
        return None


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
            k = store.key(root)
            if now - _last_prune.get(k, 0.0) > _PRUNE_EVERY_S:
                _last_prune[k] = now
                replay.prune_backups_locked(root, now)  # slot held here
            lock = _copy_lock(root)
            if not lock.acquire(blocking=False):
                return gu._skipped("integrating")
            try:
                with replay.slot_released():
                    res = _integrate(root, rec, push=push, user=False)
            finally:
                lock.release()
            if replay.slot_lost() and res["status"] != "synced":
                # The slot could not be taken back (`slot_released`): every
                # write inside already ran under its own slot, so nothing is
                # half-done — report the stretch as busy and let the next
                # trigger look again. A land that DID happen stays reported.
                return gu._skipped("busy")
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
    """Fetch, gather the facts, `decide`, execute — the one integrate path
    for every caller (sync, bring_in_latest, push_to_main)."""
    kind = rec.get("kind")
    fol = follow.for_record(rec)
    if fol is None or (kind == "own"
                       and not gu._ok(gu._run(root, "remote", "get-url", "origin"))):
        return _out("skipped", "no-remote")
    refusal = _preflight(root, fol.branch)
    if refusal:
        return refusal
    tip, reason = _fetch(root, fol, own=kind == "own")
    if tip is None:
        if reason == "busy":
            return _out("deferred", "busy")
        if reason == "redirected":
            _note_sync_error(root, "redirected")
            return _out("skipped", "redirected")
        if kind == "own" and reason not in (None, "offline"):
            _note_sync_error(root, reason)
            return _out("skipped", reason)
        # Offline, the repo gone or private to this account: silent, like
        # every unreachable remote in auto-sync; retried next trigger.
        return _out("offline")
    if kind == "github" and (rec.get("sync_error") or {}).get("reason") == "redirected":
        _clear(root, "sync_error")
    _protect_soon(root)
    if not user and not gu.auto_sync_enabled():
        # Fetched for the page's "updates waiting" count (CopyStatus.behind);
        # the move is the user's (`bring_in_latest`).
        return _out("skipped", "auto-sync-off", branch=fol.branch, tip=tip)
    for _round in range(2):  # a second round only after committing "Your edits"
        rec = store.get(root) or rec
        facts = _facts(root, rec, fol, tip, push=push)
        if facts is None:
            return _out("skipped", "unreadable")
        action = decide(facts)
        res = _execute(root, rec, fol, facts, action)
        if res is not _AGAIN:
            return res
    return _out("deferred", "edits", branch=fol.branch, tip=tip)


def _preflight(root, branch):
    """None, or an outcome saying why nothing may move."""
    if not os.path.isdir(root):
        return _out("skipped", "unavailable")
    if gu._current_branch(root) != branch:
        return _out("skipped", "not-default")
    if gu._operation_in_flight(root) is not None:
        return _out("skipped", "in-progress")
    return None


def _fetch(root, fol, *, own) -> tuple:
    """`(tip, reason)`. A GitHub-link copy's fetch writes only its private
    follow ref and no FETCH_HEAD, so it runs without the slot; an own copy's
    `git fetch origin <branch>` writes FETCH_HEAD and origin's tracking ref —
    the files a plain auto-sync fetch writes too — so it holds the slot, for
    the length of one bounded fetch — and pins the published point the
    fetch is about to move away from (`_pin_origin_seen`) first."""
    if not own:
        return follow.fetch_detail(root, fol)
    with replay.slot() as held:
        if not held:
            return None, "busy"
        _pin_origin_seen(root, fol)
        return follow.fetch_detail(root, fol)


def _pin_origin_seen(root, fol) -> None:
    """Before an own copy's fetch: write the tracking ref's current value to
    `refs/fused/origin-seen` when HEAD contains it and it descends from the
    old pin — FORWARD ONLY, so a rewind or rewrite the fetch brings in can
    never pull it back while HEAD still holds the old pin. An old pin HEAD
    no longer contains (the copy followed a rewrite) no longer counts in
    `_synced`, so it gives way and the pin stays live. Every value of that
    ref came from GitHub, so this is a published point `_synced` keeps even
    with no reflog. Slot held."""
    cur = replay.rev(root, fol.ref)
    head = replay.rev(root, "HEAD")
    if not cur or not head or not replay.is_ancestor(root, cur, head):
        return
    old = replay.rev(root, replay.ORIGIN_SEEN_REF)
    if old == cur or (old and replay.is_ancestor(root, old, head)
                      and not replay.is_ancestor(root, old, cur)):
        return
    gu._run(root, "update-ref", replay.ORIGIN_SEEN_REF, cur)


# ------------------------------------------------------------ the decision


@dataclass(frozen=True)
class Facts:
    """Everything `decide` looks at, gathered once per round (`_facts`).
    Plain values only — `decide` runs no git and reads no store."""
    kind: str                  # "github" | "own"
    branch: str
    head: str
    tip: str                   # the follow tip (github) / origin's tip (own)
    base: str | None           # what HEAD's own commits sit on (github: base;
                               # own: `synced`, the newest published point)
    tip_in_head: bool          # nothing of the author's / GitHub's is missing
    head_in_tip: bool          # nothing of this copy's is missing from tip
    rewritten: bool            # own: `base` unknown, or GitHub's tip lacks it
                               # (THE INVARIANT — nothing may be pushed)
    claude_busy: bool
    job_open: bool
    merged_proposal: bool      # proposal merged on GitHub and inside `tip`
    loop: bool                 # flags.loop()
    standing: str | None       # purpose of a needs_merge that still applies
    blocking_edits: tuple      # uncommitted edits to files the other side changes
                               # (github: base..tip; own: synced..tip)
    clobber: tuple             # IGNORED local files `tip` would newly track
    push: bool                 # own: a push was asked for
    who: str | None


@dataclass(frozen=True)
class Action:
    kind: str                  # see `decide`'s table
    why: str | None = None
    event: str | None = None


def decide(f: Facts) -> Action:
    """The integrate decision, as one table. Rows are checked top to bottom;
    the first that matches decides. No git, no store, no clock: given the
    same facts, the same action.

    THE INVARIANT (own copies; reviews r01, r2, r3). `synced` is the most
    recent BY HISTORY of every published point HEAD contains —
    `refs/fused/origin-seen` (the tracking ref pinned before each engine
    fetch, forward-only), every value the tracking ref's reflog records (old
    and new sides), and `refs/fused/base` (`_synced`). An automatic push,
    and an own replay that would feed one, is allowed ONLY when GitHub's new
    tip contains `synced`; otherwise (`rewritten`, which also covers
    `synced` unknown: no reflog and no pin) nothing is pushed or replayed —
    a no-button sentence, or, when everything here was published, a move to
    GitHub's tip. Own row 8 is the only row that checks it; only rows below
    it may push.

    GitHub-link copy (`kind == "github"`):

    | # | situation                                   | action        | event        | needs_merge            |
    |---|---------------------------------------------|---------------|--------------|------------------------|
    | 1 | a Claude turn is running in the folder      | wait          | —            | unchanged              |
    | 2 | a Merge with Claude job is open             | skip          | —            | unchanged (the job's)  |
    | 3 | an overlap is recorded for this tip and HEAD| standing      | —            | unchanged              |
    |   |   (a merged proposal's: only a return one)  |               |              |                        |
    | 4 | tip newly tracks an IGNORED local file      | blocked       | —            | set, no button, says   |
    |   |                                             |               |              |   which file to rename |
    | 5 | uncommitted edits to files the author's     | commit-edits  | —            | — (decide again)       |
    |   |   side changes (only those are committed)   |               |              |                        |
    | 6 | proposal merged, in tip, loop on            | return        | merged-back  | cleared / return       |
    | 7 | proposal merged, in tip, loop off           | wait          | —            | unchanged              |
    | 8 | tip ⊆ HEAD                                  | noop          | —            | cleared                |
    | 9 | HEAD ⊆ tip, or HEAD is just an old tip      | move          | updated      | cleared                |
    |   |   (nothing of Bob's on top)                 |               |              |                        |
    |10 | otherwise (Bob's changes + author moved)    | integrate     | updated-kept | cleared / needs-merge  |

    Rows 4-5 sit above the merged-proposal rows: the return lands with the
    same `reset --keep`, so an ignored file it would overwrite or a dirty
    overlap it would refuse on stops it exactly as it stops an update.

    Own copy with a GitHub repo (`kind == "own"`) — `base` is `synced`:

    | # | situation                                   | action        | event        | needs_merge            |
    |---|---------------------------------------------|---------------|--------------|------------------------|
    | 1 | a Claude turn is running in the folder      | wait          | —            | unchanged              |
    | 2 | a Merge with Claude job is open             | skip          | —            | unchanged              |
    | 3 | HEAD == GitHub's tip                        | noop          | —            | cleared                |
    | 4 | an overlap is recorded for this tip and HEAD| standing      | —            | unchanged              |
    | 5 | tip newly tracks an IGNORED local file      | blocked       | —            | set, no button         |
    | 6 | uncommitted edits to files GitHub's side    | blocked       | —            | set, no button, names  |
    |   |   changes (`synced..tip`) — never committed |               |              |   the file             |
    | 7 | HEAD ⊆ tip                                  | move          | —            | cleared                |
    | 8 | THE INVARIANT fails (`rewritten`):          |               |              |                        |
    |   |   synced == HEAD (all published)            | move          | —            | cleared                |
    |   |   else                                      | rewritten     | —            | set, no button         |
    | 9 | tip ⊆ HEAD                                  | noop (+push)  | —            | cleared                |
    |10 | otherwise                                   | integrate     | —            | cleared / own-sync     |
    |   |   (+push)                                   |               |              |                        |

    `integrate` replays the copy's own commits (`base..HEAD`) onto the tip
    in a scratch worktree and holds the result to the three-way merge; only
    when that check fails (a merge commit in the range carried a hand
    resolution a rebase drops) does it fall back to a merge commit. A
    conflict is a needs_merge with the strategy that produced it. A land
    that cannot happen defers with why it could not (`_land`: `slot-busy`,
    `claude`, `moved`, `in-progress`, `reset-refused`)."""
    if f.claude_busy:
        return Action("wait", "claude")
    if f.job_open:
        return Action("skip", "merge-job")
    if f.kind == "github":
        if f.standing and (not f.merged_proposal or f.standing == "return"):
            return Action("standing", "needs-merge")
        if f.clobber:
            return Action("blocked", "clobber")
        if f.blocking_edits:
            return Action("commit-edits")
        if f.merged_proposal:
            if f.loop:
                return Action("return", event="merged-back")
            return Action("wait", "merged-proposal")
        if f.tip_in_head:
            return Action("noop")
        if f.head_in_tip or f.base == f.head:
            return Action("move", "update", event="updated")
        return Action("integrate", "update-kept", event="updated-kept")
    if f.tip_in_head and f.head_in_tip:
        return Action("noop")
    if f.standing:
        return Action("standing", "needs-merge")
    if f.clobber:
        return Action("blocked", "clobber")
    if f.blocking_edits:
        return Action("blocked", "edits")
    if f.head_in_tip:
        return Action("move", "own-sync")
    if f.rewritten or not f.base:
        if f.base and f.base == f.head:
            return Action("move", "own-sync")
        return Action("rewritten", "rewritten")
    if f.tip_in_head:
        return Action("noop")
    return Action("integrate", "own-sync")


def _facts(root, rec, fol, tip, *, push) -> Facts | None:
    head = replay.rev(root, "HEAD")
    if not head:
        return None
    kind = rec.get("kind")
    tip_in_head = replay.is_ancestor(root, tip, head)
    head_in_tip = replay.is_ancestor(root, head, tip)
    if kind == "github":
        base = _base(root, head, tip, fol.branch)
        rewritten = False
    else:
        base = _synced(root, fol, head)
        rewritten = not base or not replay.is_ancestor(root, base, tip)
    nm = rec.get("needs_merge") if isinstance(rec.get("needs_merge"), dict) else None
    standing = None
    if nm and nm.get("button", True) and not nm.get("rewritten") \
            and nm.get("onto") == tip and nm.get("head") == head:
        standing = nm.get("purpose") or "needs-merge"
    blocking = ()
    clobber = ()
    # An own copy GitHub rewound (tip ⊆ HEAD, invariant failing) still
    # moves its tree: the same checks as any move.
    if not tip_in_head or (kind == "own" and head != tip and rewritten):
        edited = set(_dirty_paths(root))
        side = base if kind == "github" else (
            base or gu._out(gu._run(root, "merge-base", head, tip)) or None)
        if edited and side:
            blocking = tuple(sorted(edited & _changed_between(root, side, tip)))
        clobber = tuple(p for p in replay.clobber_paths(root, head, tip) if p not in edited)
    return Facts(
        kind=kind, branch=fol.branch, head=head, tip=tip, base=base,
        tip_in_head=tip_in_head, head_in_tip=head_in_tip, rewritten=rewritten,
        claude_busy=_claude_busy(root), job_open=_job_open(root),
        merged_proposal=kind == "github" and _proposal_merged_in(root, rec, tip),
        loop=flags.loop(), standing=standing, blocking_edits=blocking,
        clobber=clobber, push=bool(push), who=_owner(rec))


def _synced(root, fol, head) -> str | None:
    """An own copy's newest PUBLISHED point that HEAD contains, by HISTORY
    (`decide`'s invariant): of `refs/fused/origin-seen`, the tracking ref,
    EVERY value its reflog records (no first-hit stop — a plain fetch that
    logged a rewind S→c1 lists c1 first, and S is the one that matters) and
    `refs/fused/base`, the one HEAD contains that contains all the others.
    None — unknown, so nothing is pushed — when there is neither a reflog
    nor an origin-seen pin, or the candidates are not one line of history."""
    seen = replay.rev(root, replay.ORIGIN_SEEN_REF)
    logged = _reflog_values(root, fol.ref)
    if not seen and not logged:
        return None
    cands = [seen, replay.rev(root, replay.BASE_REF), replay.rev(root, fol.ref), *logged]
    return _latest(root, [c for c in cands if c and replay.is_ancestor(root, c, head)])


_ZERO = "0" * 40


def _reflog_values(root, ref, limit=200) -> list[str]:
    """Every value `ref`'s reflog records, newest first — the OLD side of
    each entry included (`rev-list -g` lists only the new sides, so the
    value a fetch moved the ref away from would be missing). Read from the
    log file itself; empty when there is none."""
    r = gu._run(root, "rev-parse", "--git-path", f"logs/{ref}")
    path = gu._out(r) if gu._ok(r) else ""
    if path and not os.path.isabs(path):
        path = os.path.join(root, path)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        parts = line.split(" ", 2)
        for sha in (parts[1:2] + parts[0:1]) if len(parts) >= 2 else ():
            if len(sha) == 40 and sha != _ZERO and sha not in out:
                out.append(sha)
    return out


_AGAIN = object()


def _clobber_message(f: Facts) -> str:
    name = os.path.basename(f.clobber[0])
    if f.kind == "own":
        return (f"The version on GitHub adds a file you already have here ({name}). "
                "Rename yours to get the update.")
    return (f"{f.who or 'The author'}'s update adds a file you already have here ({name}). "
            "Rename yours to get the update.")


def _edits_message(f: Facts) -> str:
    """An own copy's uncommitted edit in the way of GitHub's version (own row
    6): fused never commits for an own app, so the person decides."""
    name = os.path.basename(f.blocking_edits[0])
    return (f"You have changes to {name} that aren't committed yet, and the version on "
            "GitHub changed it too. Commit or undo them to keep this app in sync.")


def _execute(root, rec, fol, f: Facts, a: Action):
    """Carry out `a` (see `decide`). Answers an outcome, or `_AGAIN` after
    committing "Your edits" (the caller decides once more)."""
    done = {"branch": f.branch, "tip": f.tip}
    behind = replay.count(root, f"{f.head}..{f.tip}") or 0 if not f.tip_in_head else 0
    if a.kind == "wait":
        return _out("deferred", a.why, **done)
    if a.kind == "skip":
        return _out("skipped", a.why, **done)
    if a.kind == "standing":
        return _out("needs-merge", "needs-merge", **done)
    if a.kind == "blocked":
        files = f.clobber if a.why == "clobber" else f.blocking_edits
        message = _clobber_message(f) if a.why == "clobber" else _edits_message(f)
        _set_needs_merge(root, onto=f.tip, upstream=f.base, head=f.head, purpose="needs-merge",
                         files=list(files), message=message, button=False)
        return _out("needs-merge", a.why, **done)
    if a.kind == "rewritten":
        _set_needs_merge(root, onto=f.tip, upstream=None, head=f.head, purpose="own-sync",
                         files=[], message=REWRITTEN_MESSAGE, rewritten=True, button=False)
        return _out("needs-merge", "rewritten", **done)
    if a.kind == "commit-edits":
        return (_AGAIN if _commit_edits(root, f.head, f.blocking_edits)
                else _out("deferred", "edits", **done))
    if a.kind == "return":
        return _return(root, rec, fol, f.head, f.tip)
    if a.kind == "noop":
        _after_settle(root, f, landed=False)
        return _push_if_asked(root, f, fol.url, pulled=0, status="noop")

    # move | integrate
    mark_tip = None
    if a.kind == "move":
        new = f.tip
    else:
        res = _integrate_result(root, rec, f)
        if res.get("needs_merge"):
            _set_needs_merge(root, onto=f.tip, upstream=f.base, head=f.head,
                             purpose="needs-merge" if f.kind == "github" else "own-sync",
                             files=res["files"], strategy=res["strategy"], mark=res.get("mark"))
            return _out("needs-merge", "needs-merge", **done)
        if not res.get("tip"):
            return _out("deferred", "replay", **done)
        new, mark_tip = res["tip"], res.get("mark_tip")
    refused = _land(root, f.branch, f.head, new, a.why, mark_tip=mark_tip)
    if refused:
        return _out("deferred", refused, **done)
    gu._record_pull(root, behind)
    if a.event:
        store.add_event(root, a.event, who=f.who)
    _after_settle(root, f, landed=True)
    return _push_if_asked(root, f, fol.url, pulled=behind, status="synced",
                          had_changes=a.kind == "integrate")


def _integrate_result(root, rec, f: Facts) -> dict:
    """`integrate`'s work: replay first; the merge strategy only when the
    replay is unsound (`replay._replay_unsound`: a merge commit carried a
    resolution) or conflicts across a merge a rebase had to linearize.
    Answers `{tip, mark_tip}` | `{needs_merge, files, strategy, mark}` |
    `{}` (defer)."""
    mark = _proposed_mark(root, rec, f.head, f.base) if f.kind == "github" else None
    res = replay.replay(root, onto=f.tip, upstream=f.base, branch=f.head, mark=mark)
    if res["ok"]:
        return {"tip": res["tip"], "mark_tip": res.get("mark_tip")}
    merges = replay.has_merges(root, f.base, f.head)
    if not (res.get("mismatch") or (res["conflict"] and merges)):
        if res["conflict"]:
            return {"needs_merge": True, "files": res["files"], "strategy": "rebase",
                    "mark": mark}
        return {}
    # (An own copy reaches here only when GitHub's tip contains `synced` —
    # `decide`'s invariant — so a merge brings back nothing GitHub removed.)
    who = f.who or "the author"
    msg = (f"Bring in {who}'s latest" if f.kind == "github"
           else "Combine with the version saved on GitHub")
    m = replay.merge_into(root, head=f.head, onto=f.tip, message=msg)
    if m["ok"]:
        return {"tip": m["tip"], "mark_tip": None}
    if m["conflict"] or not m["supported"]:
        return {"needs_merge": True, "files": m["files"] or res["files"], "strategy": "merge"}
    return {}


def _after_settle(root, f: Facts, *, landed):
    """Bookkeeping once a copy holds its follow tip: needs_merge cleared,
    the base advanced (github: the follow tip; own: GitHub's tip)."""
    if (store.get(root) or {}).get("needs_merge"):
        _clear(root, "needs_merge")
    with replay.slot(block=not landed) as held:
        if not held:
            return
        if f.kind == "github":
            replay.advance_base_locked(root, replay.rev(root, "HEAD") or f.head)
            return
        # Own: forward only. A rewound or rewritten tip that does not
        # contain the old base never pulls it back (`_synced` would then
        # forget the published point the rewind removed).
        old = replay.rev(root, replay.BASE_REF)
        if old != f.tip and (not old or replay.is_ancestor(root, old, f.tip)):
            gu._run(root, "update-ref", replay.BASE_REF, f.tip)


def _push_if_asked(root, f: Facts, url, *, pulled, status, had_changes=False) -> dict:
    """An own copy, push asked, HEAD has what GitHub lacks: today's plain
    push (never forced). Every other case: the outcome as it is."""
    done = {"branch": f.branch, "tip": f.tip}
    if f.kind != "own" or not f.push:
        if f.kind == "own":
            _clear(root, "sync_error")
        return _out(status, pulled=pulled, had_changes=had_changes, **done)
    head = replay.rev(root, "HEAD")
    pushed = False
    if head and not replay.is_ancestor(root, head, f.tip):
        if follow.github_redirected(root, url):
            _note_sync_error(root, "redirected")
            return _out("skipped", "redirected", pulled=pulled, **done)
        r = gu._run(root, "push", "--", "origin", f.branch)
        if not gu._ok(r):
            why = gu._classify(r, push=True)
            if why == "offline":
                return _out("offline", pulled=pulled)
            if why == "rejected":
                # GitHub moved again since the fetch: the next trigger
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
    return _out("synced" if pushed else status, pulled=pulled, pushed=pushed,
                had_changes=had_changes, **done)


def _commit_edits(root, head, paths) -> str | None:
    """Commit the uncommitted edits standing in an update's way (`paths`,
    the facts' `blocking_edits` — nothing else) as "Your edits" (module
    docstring, the D245 exception); the new HEAD, or None (defer)."""
    if not paths:
        return None
    with replay.slot() as held:
        if not held or _claude_busy(root) or replay.rev(root, "HEAD") != head:
            return None
        return replay.commit_edits_locked(root, paths)


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
    only the commits he never proposed. HEAD ⊆ tip → the tip itself (no
    merge commit for a copy with nothing of Bob's). `proposed_head..HEAD`
    when HEAD still contains the proposed commits; else (a Merge with Claude
    job or a loop switched off rewrote them) the commits since the base
    whose patch the proposal does not hold. Runs without the slot (`_land`
    takes it)."""
    branch, who = fol.branch, _owner(rec)
    done = {"branch": branch, "tip": tip}
    p = rec.get("proposal") or {}
    proposed = replay.rev(root, p.get("proposed_head") or "")
    strategy, upstream, res, new = "rebase", None, None, None
    message = f"Bring in {who or 'the author'}'s latest"
    if replay.is_ancestor(root, head, tip):
        new = tip
    elif proposed and replay.is_ancestor(root, proposed, head):
        upstream = proposed
        if proposed == head:
            new = tip
        else:
            res = replay.replay(root, onto=tip, upstream=proposed, branch=head)
            if not res["ok"] and (res.get("mismatch")
                                  or (res["conflict"] and replay.has_merges(root, proposed, head))):
                strategy = "merge"
                res = replay.merge_into(root, head=head, onto=tip, message=message)
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
                        res = replay.merge_into(root, head=head, onto=tip, message=message)
        else:
            strategy = "merge"
            res = replay.merge_into(root, head=head, onto=tip, message=message)
    if new is None:
        if res["conflict"] or not res.get("supported", True):
            _set_needs_merge(root, onto=tip, upstream=upstream, head=head, purpose="return",
                             files=res["files"], strategy=strategy)
            return _out("needs-merge", "needs-merge", onto=tip, upstream=upstream,
                        strategy=strategy, **done)
        if not res["ok"]:
            return _out("deferred", "replay", **done)
        new = res["tip"]
    refused = _land(root, branch, head, new, "merged-back")
    if refused:
        return _out("deferred", refused, **done)

    def fn(r):
        r["needs_merge"] = None
        if isinstance(r.get("proposal"), dict):
            r["proposal"]["returned_at"] = time.time()
    store.update(root, fn)
    store.add_event(root, "merged-back", who=who)
    with replay.slot(block=False) as held:
        if held:
            replay.advance_base_locked(root, new)
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
    if not nm or nm.get("rewritten") or nm.get("button") is False:
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
    if why == "reset-refused":
        return {"ok": False, "reason": why,
                "message": "Uncommitted edits here are in the way of the update; "
                           "fused will try again."}
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
        res = _integrate(root, store.get(root) or rec, push=False, user=True)
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
        if follow.github_redirected(root, fol.url):
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
        "needs_merge_message": None, "merge_button": False, "merge_job": None, "event": None,
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
        # Merge with Claude is offered for this overlap (`merge_plan` not None).
        "merge_button": bool(nm) and nm.get("button", True) is not False
        and not nm.get("rewritten"),
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
