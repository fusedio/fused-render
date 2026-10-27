"""Own copies — the divergence fix (docs/CONTRIB_LOOP.md §16.7).

An `own` copy is the author's: today's auto-sync (`git_upstream._sync_plain`)
fetches it, fast-forwards it and pushes it, unchanged (§16.0 P5). The one
case it used to give up on is the diverged one — this copy has edits GitHub
lacks AND GitHub has something this copy lacks (the author's other machine,
an accepted proposal, GitHub's own web editor). There `_sync_plain` hands
over to `resolve` (slot held) instead of recording a `diverged` failure.

THE ONE QUESTION: may fused combine the two on its own? Only when GitHub's
version still holds everything it ever showed this copy. The record of
what it showed is git's own: the reflog of `refs/remotes/origin/<b>`, every
value the tracking ref ever had here (a fetch, a push — old and new sides),
plus the tip just fetched — `R`. Of those, the ones HEAD contains are what
this copy built on; if each is inside the new tip (`ok`), GitHub only GREW
and this copy's never-published commits `U = HEAD --not tip R…` can go on
top of it with nothing GitHub removed coming back. Computed as:

* `H = rev-list HEAD ^tip` (one process): an R value in H is one HEAD has
  and the tip lacks — GitHub dropped something this copy saw there
  (rewound, rewritten, or a different repo altogether): NOT ok.
* some R value must be inside HEAD at all — otherwise there is no evidence
  of what was published (no reflog): NOT ok. The fetch that just ran logged
  its own old side, so in practice the previous tip answers this in one
  `merge-base` call.
* `U = rev-list --parents HEAD --not tip R…` (one process), oldest first.

ok → U picked onto the tip (`replay.pick`, in memory), or — when U holds a
merge, which a pick would flatten — the tip merged into HEAD; landed with
`replay.land_locked` (backup + `reset --keep`), then pushed when the sync
asked to push. A conflict leaves the tree alone: `own_state = needs-merge`,
for Merge with Claude (`merge_job` purpose `own-sync`).

NOT ok → `own_state = github-changed`. Nothing moves, automatically, ever:
three buttons — Use the version on GitHub, Put my edits on top (exactly U,
refused when U holds a merge, since a merge would carry the removed
commits back), Publish mine over it (confirm; a push that replaces GitHub's
version only if it is still the one the person looked at).

Claude busy (§16.0 P8) gates the tree moves only; a push never waits for
it. No url_rewrites guard here: that guard is against a contributor's
hostile config, and an own copy's config is the author's own (audit G).

Every move writes one Activity line (`store.add_activity`); every stuck
state is `own_state` with a plain sentence (§0.1: no git words) and the ids
of the buttons that fix it (`fixes`), which `actions.for_app` turns into
buttons.
"""
from __future__ import annotations

import logging
import os
import time

from fused_render import git_upstream as gu
from fused_render.contrib import replay, store

logger = logging.getLogger(__name__)

#: How many R values `_published_in_head` asks git about one by one before
#: answering "no evidence" (normally the first, the previous tip, answers).
_ANCESTRY_PROBES = 16
#: Reflog lines read (newest last in the file).
_REFLOG_LINES = 500
_ZERO = "0" * 40

CLAUDE_BUSY = "Claude is still working on this app — try again when it finishes."
OFFLINE = "Couldn't reach GitHub. Try again when you're online."
BUSY = "This app is busy with another update. Try again in a moment."
NOT_MAIN = "Switch this app back to its main version first."
UNREADABLE = "Couldn't read this app's history. Try again."
PUBLISH_OVER_CONFIRM = ("Replace the version on GitHub with this copy? Anything removed "
                        "from GitHub since this copy last saved there comes back, and "
                        "changes made on GitHub since then are replaced. What's on GitHub "
                        "now is kept on this Mac for 30 days.")
#: GitHub tips fused saw, kept on the record (`seen_tips`, newest last).
SEEN_KEEP = 20

MESSAGES = {
    "published": "Everything here is saved on GitHub.",
    "unpublished": "Some of your edits aren't on GitHub yet.",
    "github-new": "GitHub has a newer version of this app.",
    "needs-merge": ("Your edits and the newer version on GitHub both change {files}. "
                    "Claude can combine them for you."),
    "github-changed": ("The version on GitHub was replaced or rolled back since this "
                       "copy last saved there, so fused won't combine them on its own. "
                       "Nothing here has changed — choose which version to keep."),
    "edits": ("You have unsaved edits to {files} that the newer version on GitHub "
              "also changes."),
    "publish-failed": "Couldn't save your edits to GitHub. Try again.",
    "publish-hook": ("Your own check in this app's folder stopped the publish, so your edits "
                     "aren't on GitHub yet. Fix what it found, then try again."),
    "publish-auth": ("GitHub didn't accept this Mac's sign-in, so your edits aren't "
                     "saved there yet."),
    "merging": "Claude is combining your edits with the version on GitHub.",
    "no-github": "This app isn't connected to GitHub.",
    "held": "This is your app. {edits} ready to publish.",
    "kept": ("You undid bringing in GitHub's version. New edits wait here until you "
             "bring it in again."),
    "unknown": ("fused can't tell whether the version on GitHub was rolled back, so "
                     "your edits are waiting here. Publish them, or switch to the version "
                     "on GitHub."),
    "github-changed-unknown": ("fused can't tell what changed on GitHub since this copy "
                               "last saved there, so it won't combine them on its own. "
                               "Nothing here has changed — choose which version to keep."),
}

#: Which buttons fix each state (`actions.for_app` filters by flags).
FIXES = {
    "unpublished": ["publish-now"],
    "github-new": ["check"],
    # Not publish-over: GitHub only grew here (ok), and replacing it would
    # drop the other machine's edits with no rewind to justify it.
    "needs-merge": ["merge-claude", "use-github"],
    "github-changed": ["use-github", "put-on-top", "publish-over"],
    "edits": ["save-edits"],
    "publish-failed": ["publish-now"],
    "publish-auth": ["publish-now"],
    "publish-hook": ["publish-now"],
    "held": ["publish-now"],
    "unknown": ["publish-now", "use-github"],
    "kept": ["bring-in"],
}


# --------------------------------------------------------------------- helpers


def _tracking(branch: str) -> str:
    return f"refs/remotes/origin/{branch}"


def _files_words(files) -> str:
    names = [os.path.basename(f) or f for f in files or ()]
    if not names:
        return "the same files"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]}, {names[1]} and {len(names) - 2} more"


def _message(state: str, files=(), n: int = 0) -> str:
    edits = ("1 edit is" if n == 1 else f"{n} edits are") if n else "Your edits are"
    return MESSAGES.get(state, "").format(files=_files_words(files), edits=edits)


def _edits_word(n: int) -> str:
    return "1 edit" if n == 1 else f"{n} edits"


def _activity(root, kind, text, *, backup=None) -> None:
    """One Activity line; a failed write must never fail the sync."""
    try:
        store.add_activity(root, kind, text, backup=backup or None)
    except Exception:  # noqa: BLE001
        logger.exception("ownsync: activity write failed for %s", root)


def _set_state(root, state: str | None, **fields) -> None:
    value = None if state is None else {"state": state, "at": time.time(), **fields}

    def fn(rec):
        rec["own_state"] = value

    if state is None and not (store.get(root) or {}).get("own_state"):
        return
    store.update(root, fn)


def _settle(root) -> None:
    """An automatic outcome with nothing stuck: clear the stuck state. The
    hold on publishing is a separate field (`hold`) no state write touches."""
    if _own_state(root):
        _set_state(root, None)


# ------------------------------------------------------------------ the hold
#
# THE HOLD (§16.12 round 2): a record field `hold = {why, count, at}`, apart
# from `own_state`, so no stuck state (needs-merge, github-changed, edits)
# and no landed merge can wipe it. While it is set nothing publishes on its
# own — `resolve` never pushes. `why`: "upgrade" (ws-guard: a sign-in made a
# GitHub-link copy the author's own while it had unpublished edits) or
# "unknown" (ours: only ahead, and nothing says whether GitHub was rolled
# back under these edits). Only Publish now — and the buttons that publish
# or drop these edits on the person's say-so (Use the version on GitHub,
# Put my edits on top, Publish mine over it) — clear it.


def hold_of(root) -> dict | None:
    """The record's hold, or None. A hold an earlier build wrote into
    `own_state` (`{"state": "held"}`) is moved into `hold` first, so no
    state write can wipe it from then on."""
    rec = store.get(root) or {}
    h = rec.get("hold")
    if isinstance(h, dict) and h.get("why"):
        return h
    st = rec.get("own_state")
    if isinstance(st, dict) and st.get("state") == "held":
        moved = {"why": st.get("why") or "upgrade", "count": st.get("count"),
                 "at": st.get("at") or time.time()}

        def fn(r):
            if isinstance(r.get("own_state"), dict) and r["own_state"].get("state") == "held":
                r["own_state"] = None
            if not (isinstance(r.get("hold"), dict) and r["hold"].get("why")):
                r["hold"] = moved

        store.update(root, fn)
        return moved
    return None


def set_hold(root, why: str, *, count: int | None = None) -> None:
    """Hold publishing (`why` "upgrade" | "unknown"); an existing hold keeps
    its reason."""
    def fn(rec):
        if not (isinstance(rec.get("hold"), dict) and rec["hold"].get("why")):
            rec["hold"] = {"why": why, "count": count, "at": time.time()}

    store.update(root, fn)


def release_hold(root) -> None:
    """The person said go (or dropped the held edits): publishing may run."""
    if hold_of(root):
        store.update(root, lambda rec: rec.__setitem__("hold", None))


def _own_state(root) -> dict | None:
    """The stuck state (never the hold — `hold_of`)."""
    st = (store.get(root) or {}).get("own_state")
    return st if isinstance(st, dict) and st.get("state") not in (None, "held") else None


def _seen(root) -> list[str]:
    """The GitHub tips fused saw here, newest first (record `seen_tips`)."""
    seen = (store.get(root) or {}).get("seen_tips")
    return [s for s in reversed(seen) if isinstance(s, str)] if isinstance(seen, list) else []


def note_seen(root, tip: str | None = None) -> None:
    """Remember GitHub's tip (`tip`, else origin's tracking ref) as a
    published point (§16.12): appended when it differs from the last one,
    the newest SEEN_KEEP kept. Evidence for the rewind check when there is
    no reflog. Never raises."""
    try:
        if tip is None:
            branch = gu._default_branch(root)
            tip = replay.rev(root, _tracking(branch)) if branch else None
        if not tip:
            return

        def fn(rec):
            seen = [s for s in rec.get("seen_tips") or [] if isinstance(s, str)]
            if not seen or seen[-1] != tip:
                seen.append(tip)
            rec["seen_tips"] = seen[-SEEN_KEEP:]

        store.update(root, fn)
    except Exception:  # noqa: BLE001
        logger.exception("ownsync: noting GitHub's tip failed for %s", root)


def _claude_busy(root) -> bool:
    """A Claude turn is open in this folder right now (§16.0 P8) — the
    engine's one rule (`engine.claude_busy`, `transcript_turn_open`).
    Unreadable reads as not busy."""
    try:
        from fused_render.contrib import engine

        return bool(engine.claude_busy(root))
    except Exception:  # noqa: BLE001
        logger.debug("ownsync: claude liveness check failed for %s", root, exc_info=True)
        return False


def _identity(*, cached: bool) -> dict | None:
    """`{name, email}` the commits ownsync makes carry: the author's GitHub
    name + noreply address when known, else None (the repo's own / Fused,
    `replay.identity_args`). `cached=True` inside the sync slot: never a
    GitHub call there (§0.9)."""
    try:
        from fused_render.contrib import gh

        ident = gh.cached_identity() if cached else gh.identity()
    except Exception:  # noqa: BLE001 — unknown identity: the fallback
        return None
    if not isinstance(ident, dict) or not ident.get("email"):
        return None
    return {"name": ident.get("name") or ident.get("login"), "email": ident["email"]}


_OPEN_JOB = ("running", "ready", "landing")


def _job_open(root) -> bool:
    """An own-sync Merge with Claude job is open here. Only that purpose
    (merge_job keeps one open job per purpose): a proposal merge in the
    inbox runs in its own scratch tree and must not stall this copy or read
    as "merging" (§16.12)."""
    try:
        from fused_render.contrib import merge_job

        job = merge_job.current(root, purpose="own-sync")
    except Exception:  # noqa: BLE001
        logger.exception("ownsync: merge_job.current failed for %s", root)
        return False
    return isinstance(job, dict) and job.get("purpose") == "own-sync" \
        and job.get("status") in _OPEN_JOB


def _reflog_values(root, path_out: str) -> list[str]:
    """Every value the tracking ref's reflog records, newest first, old
    sides included (`rev-list -g` would list only the new ones). Read from
    the log file `rev-parse --git-path` named; empty when there is none."""
    path = path_out
    if path and not os.path.isabs(path):
        path = os.path.join(root, path)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-_REFLOG_LINES:]
    except OSError:
        return []
    seen: dict = {}
    for line in reversed(lines):
        parts = line.split(" ", 2)
        if len(parts) < 2:
            continue
        for sha in (parts[1], parts[0]):
            if len(sha) == 40 and sha != _ZERO:
                seen.setdefault(sha, None)
    return list(seen)


def _lines(result) -> list[str]:
    return gu._out(result).splitlines() if gu._ok(result) else []


def _ahead_behind(root, branch):
    a, b = gu._ahead_behind_counts(root, branch)
    return a or 0, b or 0


def _repo_state(root, branch) -> dict:
    """`check_repo`-shaped, for git_upstream's `_state` cache."""
    a, b = _ahead_behind(root, branch)
    return {"root": root, "branch": branch, "default_branch": branch, "on_default": True,
            "ahead": a, "behind": b, "checked_at": time.time()}


def _dirty_tracked(root) -> set:
    r = replay.git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    if not gu._ok(r):
        return set()
    found = set()
    items = iter(r[1].decode("utf-8", "replace").split("\0"))
    for item in items:
        if len(item) > 3:
            found.add(item[3:])
            if item[0] in "RC":
                next(items, None)
    return found


def _still_edited(root, files) -> bool:
    """Whether any of `files` still has an unsaved edit (the person may have
    undone it by hand, which ends the `edits` state)."""
    wanted = set(files or ())
    return any(p in wanted for _code, p in replay.edits(root))


def _changed(root, a, b) -> set:
    r = replay.git(root, "diff", "--name-only", "-z", a, b)
    if not gu._ok(r):
        return set()
    return {p for p in r[1].decode("utf-8", "replace").split("\0") if p}


# ------------------------------------------------------------------- the facts


class _Facts:
    __slots__ = ("branch", "head", "tip", "tip_in_head", "ok", "unknown", "u", "u_merges",
                 "reason")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def _facts(root, branch) -> _Facts | None:
    """HEAD, the fetched tip, `ok` and `U` (module docstring) — four git
    processes when diverged, three when only ahead and ok (then nothing is
    picked, so U is not needed: `u` stays None)."""
    ref = _tracking(branch)
    lines = _lines(replay.git(root, "rev-parse", "--git-path", f"logs/{ref}", "HEAD", ref))
    if len(lines) != 3:
        return None
    log_path, head, tip = lines
    # R: the tip, every reflog value, and the tips fused itself saw (the
    # record — a repo without a reflog is not blind, §16.12). Nothing but
    # the tip = "unknown": no evidence of what GitHub showed before. (Not
    # the version a folder was cloned at: an edit published after the
    # clone by hand leaves no trace, and the clone would vouch for it.)
    earlier = [*_reflog_values(root, log_path), *_seen(root)]
    values = list(dict.fromkeys([tip, *earlier]))
    if head == tip:
        return _Facts(branch=branch, head=head, tip=tip, tip_in_head=True, ok=True,
                      unknown=False, u=[], u_merges=False)
    mb = gu._out(replay.git(root, "merge-base", head, tip)) or None
    held = set(_lines(replay.git(root, "rev-list", head, f"^{tip}")))
    dropped = any(v in held for v in values)
    # Unknown: no evidence at all, or none of it inside HEAD (X empty) —
    # then nothing says which of HEAD's commits GitHub once had.
    unknown = not dropped and (not earlier
                               or not _published_in_head(root, head, tip, mb, values))
    if unknown and not earlier and _made_here(root, held):
        # A fresh clone (a second Mac): R is only the tip, but every commit
        # HEAD has beyond it was made in this folder, with reflogs on — had
        # any been published from here, the tracking ref's reflog would say
        # so. Nothing in U was ever on GitHub: safe.
        unknown = False
    ok = not unknown and not dropped
    f = _Facts(branch=branch, head=head, tip=tip, tip_in_head=mb == tip, ok=ok,
               unknown=unknown, reason=None if ok else ("unknown" if unknown else "dropped"))
    if not (ok and f.tip_in_head):
        u_rows = _lines(replay.git(root, "rev-list", "--topo-order", "--reverse", "--parents",
                                head, "--not", *values))
        f.u = [row.split()[0] for row in u_rows if row]
        f.u_merges = any(len(row.split()) > 2 for row in u_rows)
    return f


#: HEAD reflog messages that record a commit CREATED in this folder.
_MADE_HERE = ("commit:", "commit (", "cherry-pick:", "revert:", "rebase")


def _made_here(root, commits) -> bool:
    """Every commit in `commits` was created in this folder — HEAD's reflog
    records it as a local commit/merge/amend/cherry-pick/revert/rebase
    step — and reflogs are on (`core.logAllRefUpdates` not false), so a
    push from here would have logged the tracking ref. False when unsure."""
    if not commits:
        return False
    logging_on = gu._out(replay.git(root, "config", "--bool", "core.logAllRefUpdates"))
    if logging_on == "false":
        return False
    path = gu._out(replay.git(root, "rev-parse", "--git-path", "logs/HEAD"))
    if path and not os.path.isabs(path):
        path = os.path.join(root, path)
    made = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split(" ", 2)
                msg = line.partition("\t")[2]
                if len(parts) >= 2 and (msg.startswith(_MADE_HERE)
                                        or (msg.startswith("merge ") and "Merge made" in msg)):
                    made.add(parts[1])
    except OSError:
        return False
    return all(c in made for c in commits)


def _published_in_head(root, head, tip, mb, values) -> bool:
    """Some R value is inside HEAD — evidence of what this copy built on.
    The merge-base answers it when it is one of them (the usual case: the
    previous tip, or the tip itself); else a bounded probe, newest first."""
    if mb and mb in values:
        return True
    for v in [v for v in values if v not in (tip, mb)][:_ANCESTRY_PROBES]:
        if replay.is_ancestor(root, v, head):
            return True
    return False


# ------------------------------------------------------------------- outcomes


def _result(root, branch, status, why=None, *, pulled=0, pushed=False, ok=True) -> dict:
    out = {"ok": ok, "status": status, "pulled": pulled, "pushed": pushed,
           "state": _repo_state(root, branch)}
    if why:
        out["why"] = why
    return out


def _stuck(root, f: _Facts, state: str, *, files=(), then=None, unknown=False) -> dict:
    """Record a state that needs a button; the tree is untouched."""
    _set_state(root, state, head=f.head, tip=f.tip, files=list(files),
               unpublished=len(f.u or ()), u_merges=bool(f.u_merges), then=then,
               unknown=bool(unknown))
    return _result(root, f.branch, "skipped", state)


def _push(root, branch) -> tuple[bool, str | None]:
    """`git push -- origin <b>` (the author's own remote, never forced).
    `(pushed, why-not)`: why-not is `offline`, `rejected` (GitHub moved
    again: the next sync combines and pushes), `hook`, `auth` or
    `git-failed`."""
    r = _publish_git(root, "push", "--", "origin", branch)
    if gu._ok(r):
        return True, None
    why = _push_failure(root, r)
    logger.info("ownsync: push from %s refused (%s): %s", root, why, gu._text(r))
    return False, why


def _publish_git(root, *args):
    """A push of the author's OWN copy to her own GitHub repo (§16.12 P5):
    plain `git_upstream._run` — her hooks (a secret scanner in pre-push)
    and her fsmonitor run exactly as for a push she makes herself. Only
    fused's private operations (reads, in-memory picks/merges, backups) go
    through `replay.git`'s hook-free SAFE_CONFIG."""
    return gu._run(root, *args)


def _push_failure(root, result) -> str:
    """Why a push failed: `gu._classify`'s reason, or `hook` when it is a
    git-level failure and the repo has its own pre-push check (the only
    thing on this side that refuses a push without a remote answer)."""
    why = gu._classify(result, push=True)
    if why == "git-failed":
        path = gu._out(gu._run(root, "rev-parse", "--git-path", "hooks/pre-push"))
        if path and not os.path.isabs(path):
            path = os.path.join(root, path)
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return "hook"
    return why


def _after_push(root, f: _Facts, *, pulled, pushed, why) -> dict:
    if pushed or why is None:
        _set_state(root, None)
        return _result(root, f.branch, "synced", pulled=pulled, pushed=pushed)
    if why == "offline":
        _settle(root)
        return _result(root, f.branch, "offline", pulled=pulled)
    if why == "rejected":
        _settle(root)
        return _result(root, f.branch, "skipped", "rejected", pulled=pulled)
    state = {"auth": "publish-auth", "hook": "publish-hook"}.get(why, "publish-failed")
    head = replay.rev(root, "HEAD")
    _set_state(root, state, head=head, tip=replay.rev(root, _tracking(f.branch)), files=[])
    return _result(root, f.branch, "skipped", state, pulled=pulled)


def _combine(root, f: _Facts, *, allow_merge: bool, identity=None) -> dict:
    """`{tip}` | `{conflict, files}` | `{}` (git could not say): U picked
    onto the tip, or (U holds a merge, or the pick conflicts and
    `allow_merge`) the tip merged into HEAD — as `identity` (`_identity`)."""
    if not f.u:
        return {"tip": f.tip}
    if not f.u_merges:
        p = replay.pick(root, onto=f.tip, commits=f.u, identity=identity)
        if p.get("ok") and p.get("tip"):
            return {"tip": p["tip"]}
        if not allow_merge:
            return {"conflict": True, "files": p.get("files") or []} \
                if p.get("conflict") or not p.get("supported", True) else {}
    elif not allow_merge:
        return {}
    m = replay.merge_into(root, head=f.head, onto=f.tip,
                          message="Combine with the version on GitHub", identity=identity)
    if m.get("ok") and m.get("tip"):
        return {"tip": m["tip"]}
    if m.get("conflict") or not m.get("supported", True):
        return {"conflict": True, "files": m.get("files") or []}
    return {}


def _land(root, f: _Facts, new, *, why) -> tuple[str | None, list]:
    """Move the branch HEAD → `new` (backup + `reset --keep`). `(refusal,
    backup refs)`; refusal None = moved."""
    refused, backup = replay.land_locked(root, branch=f.branch, old_tip=f.head,
                                         new_tip=new, why=why)
    return refused, _with_wip(root, backup)


def _with_wip(root, backup) -> list[str]:
    """A backup ref and its `-wip` (the uncommitted edits), when written."""
    if not backup:
        return []
    return [backup] + ([f"{backup}-wip"] if replay.rev(root, f"{backup}-wip") else [])


def _blocking(root, head, new) -> list[str]:
    """Uncommitted tracked edits to files the move changes, plus untracked
    files the new version adds where one already sits."""
    dirty = _dirty_tracked(root)
    changed = _changed(root, head, new)
    files = sorted(dirty & changed)
    files += [p for p in replay.clobber_paths(root, head, new) if p not in files]
    return files


def _move(root, f: _Facts, new, *, why, then=None) -> tuple[dict | None, list]:
    """Land `new`. `(None, backup refs)` when it moved, else `(the outcome
    that says why not, [])`: an uncommitted edit in the way is the `edits`
    state (its button saves them and carries on with `then`)."""
    blocking = sorted(_dirty_tracked(root) & _changed(root, f.head, new))
    if blocking:
        return _stuck(root, f, "edits", files=blocking, then=then), []
    refused, refs = _land(root, f, new, why=why)
    if refused is None:
        return None, refs
    if refused == "reset-refused":
        files = _blocking(root, f.head, new)
        if files:
            return _stuck(root, f, "edits", files=files, then=then), []
    return _result(root, f.branch, "skipped", refused or "deferred"), []


# ----------------------------------------------------------------- resolve


def resolve(root, *, action, push) -> dict | None:
    """`_sync_plain`'s diverged branch for an own copy (slot held, origin
    just fetched). None = not handled (the contribution loop is off): the
    caller records today's `diverged` row. Otherwise `_sync_plain`'s shape,
    `{ok, status, pulled, pushed, state, why?}` — never a failure row."""
    try:
        from fused_render.contrib import flags

        if not flags.sources():
            return None
    except Exception:  # noqa: BLE001 — unknown flags read as off: today's row
        logger.exception("ownsync: flags unreadable")
        return None
    branch = gu._current_branch(root)
    if not branch:
        return None
    with replay.slot_held():
        try:
            return _resolve_locked(root, branch, push=push)
        except Exception:  # noqa: BLE001 — never break auto-sync
            logger.exception("ownsync: resolve failed for %s (%s)", root, action)
            return {"ok": True, "status": "skipped", "why": "own-sync-error",
                    "pulled": 0, "pushed": False}
        finally:
            # This fetch's tip is a published point for every later check —
            # recorded only now, so the decision above saw the list as it
            # stood before it.
            note_seen(root, replay.rev(root, _tracking(branch)) or "")


def _resolve_locked(root, branch, *, push) -> dict:
    f = _facts(root, branch)
    if f is None:
        return {"ok": True, "status": "skipped", "why": "unreadable", "pulled": 0,
                "pushed": False}
    held = hold_of(root)
    st = _own_state(root)
    if st and st.get("state") in ("needs-merge", "edits") and st.get("head") == f.head \
            and st.get("tip") == f.tip and (st["state"] == "needs-merge" or _still_edited(
                root, st.get("files"))):
        # Standing: its button is on the page; a background sync must not
        # turn it into another state under the person's cursor.
        return _result(root, branch, "skipped", st["state"])
    if not f.ok:
        if f.unknown and f.tip_in_head:
            # Only ahead, and nothing says whether GitHub was rolled back
            # under these edits: wait for the person, with a button.
            set_hold(root, "unknown")
            _settle(root)
            return _result(root, branch, "skipped", "held")
        return _stuck(root, f, "github-changed", unknown=f.unknown)
    if f.tip_in_head:
        # Nothing to bring in (a rewind that kept every published point is
        # still ok): only the push — never waits on Claude or a merge job.
        if not push:
            _settle(root)
            return _result(root, branch, "synced")
        if held:
            _settle(root)
            return _result(root, branch, "skipped", "held")
        pushed, why = _push(root, branch)
        return _after_push(root, f, pulled=0, pushed=pushed, why=why)
    if _job_open(root):
        return _result(root, branch, "skipped", "merge-job")
    rec = store.get(root) or {}
    if rec.get("skip_tip"):
        if rec["skip_tip"] == f.tip:
            # The person undid the last automatic combine: it stays undone
            # until GitHub's version moves again (or they press Check now).
            return _result(root, branch, "skipped", "kept")
        store.update(root, lambda r: r.__setitem__("skip_tip", None))
    if _claude_busy(root):
        return _result(root, branch, "skipped", "claude")
    if held:
        push = False  # bring GitHub's version in; publishing waits for the button
    behind = replay.count(root, f"{f.head}..{f.tip}") or 0
    combined = _combine(root, f, allow_merge=True, identity=_identity(cached=True))
    if combined.get("conflict"):
        return _stuck(root, f, "needs-merge", files=combined.get("files") or [])
    new = combined.get("tip")
    if not new:
        return _result(root, branch, "skipped", "deferred")
    refused, refs = _move(root, f, new, why="own-sync")
    if refused is not None:
        return refused
    gu._record_pull(root, behind)
    if f.u:
        text = (f"Brought in the newer version from GitHub and kept your "
                f"{_edits_word(len(f.u))}.")
    else:
        text = "Brought in the newer version from GitHub."
    _activity(root, "own-sync", text, backup=refs)
    if not push:
        _settle(root)
        return _result(root, branch, "synced", pulled=behind)
    pushed, why = _push(root, branch)
    return _after_push(root, f, pulled=behind, pushed=pushed, why=why)


# ------------------------------------------------------------ manual buttons
#
# Each answers `{ok, message, **status(root)}`. The tree-moving ones refuse
# while Claude is working (P8); the publishing ones never wait for it.


def _answer(root, ok: bool, message: str, **extra) -> dict:
    return {**status(root), "ok": ok, "message": message, **extra}


def _sync_answer(root, res: dict) -> dict:
    """A `sync_repo` outcome as a button's answer."""
    res = res or {}
    st = status(root)
    if res.get("busy"):
        return {**st, "ok": False, "message": BUSY}
    if res.get("status") == "offline":
        return {**st, "ok": False, "message": OFFLINE}
    if res.get("status") == "failed":
        return {**st, "ok": False, "message": MESSAGES["publish-failed"]}
    if res.get("why") == "claude":
        return {**st, "ok": False, "message": CLAUDE_BUSY}
    if res.get("status") == "synced":
        return {**st, "ok": True}
    return {**st, "ok": st["state"] in ("published", "unpublished", "github-new")}


def publish_now(root) -> dict:
    """Publish now: today's sync, pushing (never waits for Claude). Ends the
    hold — the person's go-ahead — and vouches for the GitHub version this
    copy holds now (`note_seen`), so an "unknown" rewind state does not
    hold the same edits again."""
    release_hold(root)
    note_seen(root)
    return _sync_answer(root, gu.sync_repo(root, action=gu.PUBLISH_NOW, push=True))


def check_now(root) -> dict:
    """Check now: fetch and bring in what GitHub has, without publishing —
    including a version an Undo had set aside (`skip_tip`)."""
    if (store.get(root) or {}).get("skip_tip"):
        store.update(root, lambda r: r.__setitem__("skip_tip", None))
    return _sync_answer(root, gu.sync_repo(root, action="Check now", push=False))


def bring_in(root) -> dict:
    """Bring in and publish (the `kept` state's button): drop what an Undo
    set aside (`skip_tip`), bring GitHub's version in and publish — the
    hold, if any, still decides the publishing."""
    if (store.get(root) or {}).get("skip_tip"):
        store.update(root, lambda r: r.__setitem__("skip_tip", None))
    return _sync_answer(root, gu.sync_repo(root, action="Bring in and publish", push=True))


def _fetched_facts(root):
    """Under the slot: fetch origin's branch and gather the facts. `(facts,
    None)` or `(None, answer)`."""
    branch = gu._default_branch(root)
    if not branch or gu._current_branch(root) != branch:
        return None, _answer(root, False, NOT_MAIN)
    if gu._operation_in_flight(root) is not None:
        return None, _answer(root, False, BUSY)
    fetched = replay.git(root, "fetch", "--", "origin", branch)
    if not gu._ok(fetched):
        why = gu._classify(fetched)
        return None, _answer(root, False, OFFLINE if why == "offline"
                             else "Couldn't get the version on GitHub. Try again.")
    f = _facts(root, branch)
    if f is None:
        return None, _answer(root, False, UNREADABLE)
    return f, None


def use_github(root) -> dict:
    """Use the version on GitHub: this copy becomes GitHub's version. Its
    edits — saved and not — are kept in a backup the Activity line can
    restore."""
    if _claude_busy(root):
        return _answer(root, False, CLAUDE_BUSY)
    with replay.slot() as held:
        if not held:
            return _answer(root, False, BUSY)
        f, refusal = _fetched_facts(root)
        if refusal:
            return refusal
        if f.head == f.tip:
            _set_state(root, None)
            return _answer(root, True, MESSAGES["published"])
        refused, refs = _land(root, f, f.tip, why="use-github")
        if refused == "reset-refused":
            refused, refs = _hard_move(root, f)
        if refused is not None:
            return _answer(root, False, BUSY if refused != "reset-refused"
                           else "Couldn't switch to the version on GitHub. Try again.")
        _set_state(root, None)
        release_hold(root)  # the held edits are not this copy's version any more
        _activity(root, "use-github", "Switched to the version on GitHub. Your edits "
                  "are saved in case you want them back.", backup=refs)
    return _answer(root, True, "This app now matches the version on GitHub.")


def _hard_move(root, f: _Facts) -> tuple[str | None, list]:
    """`use_github` when an uncommitted edit overlaps: back up HEAD and
    every edit (untracked files too), then a hard move. Untracked files the
    author made stay where they are (only the ones GitHub's version adds
    are overwritten — and they are in the backup)."""
    clobber = replay.clobber_paths(root, f.head, f.tip)
    refs = replay.backup_locked(root, f.head, why="use-github", also=clobber)
    if not refs or not any(r.endswith("-wip") for r in refs):
        replay.unbackup_locked(root, refs)
        return "reset-refused", []
    replay.protect_after_move(root)  # see land_locked: the S bit lets .fused/ through
    with replay.fused_aside(root, f.tip):
        moved = replay.git(root, "reset", "--hard", f.tip, timeout=replay.REPLAY_TIMEOUT_S)
    if not gu._ok(moved) or not replay.undo_race_locked(root, f.head, refs, hard=True):
        # A failed hard reset may be partial, and a raced one dropped the
        # edits: either way the backup is their only copy — named.
        logger.warning("ownsync: hard move did not happen in %s: %s", root,
                       gu._text(moved) if moved else "no git")
        _activity(root, "backup", "Saved a copy of your work before changing it.",
                  backup=refs)
        return "moved", []
    replay.protect_after_move(root)
    return None, refs


def put_on_top(root) -> dict:
    """Put my edits on top: exactly this copy's never-published edits (U)
    onto GitHub's version, then publish. Refused when U holds a merge — it
    would carry back what GitHub removed."""
    if _claude_busy(root):
        return _answer(root, False, CLAUDE_BUSY)
    ident = _identity(cached=False)  # outside the slot: may ask GitHub
    with replay.slot() as held:
        if not held:
            return _answer(root, False, BUSY)
        f, refusal = _fetched_facts(root)
        if refusal:
            return refusal
        if f.ok and f.tip_in_head:  # GitHub only lacks what is here: just publish
            return _answer(root, False, "Nothing here needs to go on top — use Publish now.")
        if f.unknown:
            # Nothing says which of these edits GitHub already had (and
            # maybe removed): picking them could publish one again.
            return _answer(root, False, "fused can't tell which of your edits were already "
                           "on GitHub, so it can't put only the new ones on top. Use the "
                           "version on GitHub, or publish yours over it.")
        if not f.u and not _dirty_tracked(root):
            return _answer(root, False, "There are no new edits here to put on top. Use the "
                           "version on GitHub, or publish yours over it.")
        if f.u_merges:
            return _answer(root, False, "Your edits include a combined version that would "
                           "bring back what was removed on GitHub, so they can't be put on "
                           "top. Use the version on GitHub, or publish yours over it.")
        combined = _combine(root, f, allow_merge=False, identity=ident)
        if combined.get("conflict"):
            files = combined.get("files") or []
            return _answer(root, False, f"Your edits and the version on GitHub both change "
                           f"{_files_words(files)}, so yours can't simply go on top. Use the "
                           "version on GitHub, or publish yours over it.", files=files)
        new = combined.get("tip")
        if not new:
            return _answer(root, False, "Couldn't put your edits on top. Try again.")
        refused, refs = _move(root, f, new, why="put-on-top", then="put-on-top")
        if refused is not None:
            return _answer(root, False, status(root)["message"]
                           if refused.get("why") == "edits" else BUSY)
        _activity(root, "put-on-top", f"Put your {_edits_word(len(f.u))} on top of the "
                  "version on GitHub." if f.u else "Put your edits on top of the version "
                  "on GitHub.", backup=refs)
        if not f.u:  # only unsaved edits rode along: nothing to publish
            _set_state(root, None)
            return _answer(root, True, "Your edits are on top of the version on GitHub.")
        release_hold(root)  # the button publishes, on the person's say-so
        pushed, why = _push(root, f.branch)
        _after_push(root, f, pulled=0, pushed=pushed, why=why)
        note_seen(root, replay.rev(root, _tracking(f.branch)) or "")
    if not pushed:
        return _answer(root, False, OFFLINE if why == "offline"
                       else MESSAGES["publish-hook"] if why == "hook"
                       else "Your edits are on top here, but aren't saved to GitHub yet. "
                            "Try Publish now.")
    return _answer(root, True, "Your edits are on top of the version on GitHub and saved "
                   "there.")


def publish_over(root) -> dict:
    """Publish mine over it: GitHub's version becomes this copy's, but only
    if it is still the version the person looked at (a lease on the tip
    the stuck state recorded). GitHub's old version is kept in a backup.
    The person has confirmed already (`PUBLISH_OVER_CONFIRM`, asked by the
    action route before it calls this)."""
    with replay.slot() as held:
        if not held:
            return _answer(root, False, BUSY)
        branch = gu._default_branch(root)
        if not branch or gu._current_branch(root) != branch:
            return _answer(root, False, NOT_MAIN)
        st = _own_state(root) or {}
        seen = st.get("tip") or replay.rev(root, _tracking(branch))
        if not seen or not replay.rev(root, "HEAD"):
            return _answer(root, False, UNREADABLE)
        pin = _pin(root, seen, "publish-over")
        r = _publish_git(root, "push", f"--force-with-lease={branch}:{seen}", "--",
                         "origin", f"HEAD:refs/heads/{branch}")
        if not gu._ok(r):
            if pin:
                replay.git(root, "update-ref", "-d", pin)
            why = _push_failure(root, r)
            logger.info("ownsync: publish over refused in %s (%s): %s", root, why, gu._text(r))
            if why == "offline":
                return _answer(root, False, OFFLINE)
            if why == "rejected":
                return _answer(root, False, "The version on GitHub changed again since you "
                               "looked. Check again before replacing it.")
            return _answer(root, False, MESSAGES["publish-hook" if why == "hook"
                                                 else "publish-failed"])
        _set_state(root, None)
        release_hold(root)  # published, on the person's say-so
        # Not undoable here: putting this copy back on GitHub's old version
        # would not un-publish anything. The pin ages out like any backup.
        _activity(root, "publish-over", "Replaced the version on GitHub with this copy. "
                  "GitHub's previous version is kept on this Mac for 30 days.")
    return _answer(root, True, "The version on GitHub is now this copy.")


def _pin(root, sha, why) -> str | None:
    """A backup ref at `sha` (GitHub's version, about to be replaced)."""
    name = f"{replay.BACKUP_PREFIX}{int(time.time() * 1000)}-{why}"
    return name if gu._ok(replay.git(root, "update-ref", name, sha)) else None


def save_edits(root) -> dict:
    """Save my edits and continue: save the edits standing in the way (the
    `edits` state's files, nothing else) as the author, then carry on with
    what they blocked."""
    st = _own_state(root) or {}
    if st.get("state") != "edits":
        return _answer(root, False, "There's nothing to save right now.")
    if _claude_busy(root):
        return _answer(root, False, CLAUDE_BUSY)
    files = st.get("files") or []
    ident = _identity(cached=False)  # outside the slot: may ask GitHub
    with replay.slot() as held:
        if not held:
            return _answer(root, False, BUSY)
        if not replay.commit_edits_locked(root, files, message="Saved edits", identity=ident):
            return _answer(root, False, "Couldn't save your edits. Try again.")
        _set_state(root, None)
        _activity(root, "save-edits", f"Saved your edits to {_files_words(files)}.")
    if st.get("then") == "put-on-top":
        return put_on_top(root)
    # Carry on with the sync they blocked — NOT Publish now: a hold on
    # publishing stays until its own button (resolve never pushes held).
    return _sync_answer(root, gu.sync_repo(root, action="Save my edits and continue",
                                           push=True))


def start_merge(root) -> dict:
    """Merge with Claude for the `needs-merge` state: `merge_job` purpose
    `own-sync`, combining GitHub's version into this copy's."""
    plan = merge_plan(root)
    if plan is None:
        return {"ok": False, "message": "There's nothing to combine right now."}
    from fused_render.contrib import merge_job

    job = merge_job.start(root, purpose=plan["purpose"], ours=plan["ours"],
                          theirs=plan["theirs"], identity=_identity(cached=False))
    return {"ok": True, "job": job}


def restore(root, activity_id) -> dict:
    """Undo one Activity line that names a backup: this copy goes back to
    the version the backup holds (and its unsaved edits, when the line
    saved some). What is here now is backed up first and named by a new
    line, so a restore can itself be undone."""
    entry = store.activity_entry(root, activity_id) or {}
    refs = entry.get("backup")
    refs = [r for r in (refs if isinstance(refs, list) else [refs]) if isinstance(r, str) and r]
    tip_ref = next((r for r in refs if not r.endswith("-wip")), None)
    if not tip_ref or entry.get("restored_at"):
        return _answer(root, False, "There's nothing to undo here.")
    if _claude_busy(root):
        return _answer(root, False, CLAUDE_BUSY)
    with replay.slot() as held:
        if not held:
            return _answer(root, False, BUSY)
        branch = gu._default_branch(root)
        if not branch or gu._current_branch(root) != branch:
            return _answer(root, False, NOT_MAIN)
        target, head = replay.rev(root, tip_ref), replay.rev(root, "HEAD")
        if not target or not head:
            return _answer(root, False, "That earlier version is no longer available.")
        wip = replay.rev(root, f"{tip_ref}-wip") if f"{tip_ref}-wip" in refs else None
        saved = []
        if target != head:
            refused, backup = replay.land_locked(root, branch=branch, old_tip=head,
                                                 new_tip=target, why="restore")
            if refused == "reset-refused":
                return _answer(root, False, "You have unsaved edits to files the earlier "
                               "version changes. Save or undo them first.")
            if refused is not None:
                return _answer(root, False, BUSY)
            saved = _with_wip(root, backup)
        if wip and not _put_back_edits(root, target, wip, saved):
            return _answer(root, False, "Couldn't put back your unsaved edits. Try again.")
        store.mark_restored(root, activity_id)
        _settle(root)  # a hold on publishing outlives the move
        if entry.get("kind") == "own-sync":
            # Undoing an automatic combine sticks until GitHub's version
            # moves (or Check now): the next sync must not redo it (§16.12).
            tip = replay.rev(root, _tracking(branch))
            store.update(root, lambda r: r.__setitem__("skip_tip", tip))
        _activity(root, "restore", "Put back an earlier version of this app. What was here "
                  "is saved too.", backup=saved or None)
    return _answer(root, True, "This app is back to the earlier version.")


def _put_back_edits(root, target, wip, saved: list) -> bool:
    """Write the unsaved edits a backup's `-wip` holds (relative to
    `target`) back into the folder, unstaged. Edits made here since are
    backed up first when the move itself saved nothing (`saved`)."""
    r = replay.git(root, "diff", "--name-status", "--no-renames", "-z", target, wip)
    if not gu._ok(r):
        return False
    fields = [p for p in r[1].decode("utf-8", "replace").split("\0") if p]
    pairs = list(zip(fields[0::2], fields[1::2]))
    if not pairs:
        return True
    if not saved and replay.dirty(root):
        refs = replay.backup_locked(root, target, why="restore")
        if not any(x.endswith("-wip") for x in refs):
            replay.unbackup_locked(root, refs)
            return False
        saved.extend(refs)
    keep = [p for code, p in pairs if code != "D"]
    if keep and not gu._ok(replay.git(root, "--literal-pathspecs", "restore", f"--source={wip}",
                                   "--worktree", "--", *keep)):
        return False
    for code, p in pairs:
        if code == "D":
            try:
                os.remove(os.path.join(root, p))
            except OSError:
                pass
    return True


def merge_plan(root) -> dict | None:
    """`{purpose, ours, theirs, files}` for the recorded `needs-merge` state
    while it still applies (HEAD and GitHub's tip unchanged), else None."""
    st = _own_state(root) or {}
    if st.get("state") != "needs-merge" or not st.get("head") or not st.get("tip"):
        return None
    branch = gu._default_branch(root)
    if not branch or replay.rev(root, "HEAD") != st["head"] \
            or replay.rev(root, _tracking(branch)) != st["tip"]:
        return None
    return {"purpose": "own-sync", "ours": st["head"], "theirs": st["tip"],
            "files": list(st.get("files") or [])}


def merge_landed(root, *, old_tip=None, new_tip=None) -> None:
    """A Merge with Claude job (purpose `own-sync`) landed `new_tip` over
    `old_tip` — called by `engine.merge_landed`, which writes the Activity
    line: the stuck state is over; publish it (HEAD now contains GitHub's
    version, so today's plain sync pushes)."""
    _set_state(root, None)
    gu.schedule_sync(root, "Publish combined version", push=True)


# ------------------------------------------------------------------ status


def status(root) -> dict:
    """`{state, unpublished, github_new, files, message, fixes}` — no
    network. A recorded stuck state stands while HEAD and the GitHub tip it
    was about are unchanged; otherwise the state is read off the counts."""
    branch = gu._default_branch(root)
    head = replay.rev(root, "HEAD")
    tip = replay.rev(root, _tracking(branch)) if branch else None
    if not branch or not head or not tip:
        return {"state": "no-github", "unpublished": 0, "github_new": 0, "files": [],
                "message": MESSAGES["no-github"], "fixes": []}
    ahead, behind = _ahead_behind(root, branch)
    rec = store.get(root) or {}
    st = _own_state(root) or {}
    held = hold_of(root)
    variant = None
    if _job_open(root):
        state, files = "merging", st.get("files") or []
    elif st.get("state") and st.get("head") == head and st.get("tip") == tip and (
            st["state"] != "edits" or _still_edited(root, st.get("files"))):
        # A stuck state shows first (its buttons); a hold outlives it.
        state, files = st["state"], st.get("files") or []
        if state == "github-changed":
            # What Put my edits on top would carry: the never-published
            # edits, +1 for unsaved ones (they ride along too).
            ahead = int(st.get("unpublished") or 0) + (1 if _dirty_tracked(root) else 0)
            variant = "github-changed-unknown" if st.get("unknown") else None
    elif held and ahead:
        # The hold stands until Publish now, whatever HEAD does meanwhile.
        state, files = ("unknown" if held["why"] == "unknown" else "held"), []
    elif rec.get("skip_tip") == tip and behind:
        state, files = "kept", []
    else:
        files = []
        state = ("published" if not ahead and not behind else
                 "unpublished" if not behind else "github-new")
    fixes = list(FIXES.get(state, []))
    if state == "github-changed" and (st.get("u_merges") or st.get("unknown") or not ahead):
        # Put my edits on top would only refuse: a merge among the edits
        # would carry back what GitHub removed, nothing says which edits
        # GitHub already had, or there is nothing to carry.
        fixes.remove("put-on-top")
    return {"state": state, "unpublished": ahead, "github_new": behind, "files": files,
            "message": _message(variant or state, files, ahead), "fixes": fixes,
            "hold": held["why"] if held else None}
