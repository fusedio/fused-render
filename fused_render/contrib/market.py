"""The `marketplace` kind's row (docs/CONTRIB_LOOP.md §18.4): which commit a
marketplace copy runs, the explicit switches between it and a GitHub-link
copy, and the adoption of a copy installed from the old release packages.

WHERE THE APPROVED COMMIT COMES FROM. The catalog (`fused_render.marketplace`,
served by the backend canvas from the index repo) carries one row per listed
app: `repo`, `branch`, `commit` (the approved commit), `releases` newest first
`[{commit, at, whats_new, version, blocked, credits?}]`. A copy's record keeps
the last row it saw (`record.approved`), and `engine._run_inner` calls
`refresh_approved` before every fetch of a marketplace copy, so the mirror
(`follow.update_mirror`, pinned) moves to a new approved commit exactly when a
release lands — never to the author's tip (M1).

FAILS CLOSED (M3). A row is read defensively — any missing or malformed key
reads as "no row". The copy's approved commit changes only on a readable row
of a live listing; "delisted" needs a FRESH catalog (`status == "ok"`, not
`stale`) that lacks the slug; anything else — offline, a stale cache, an
error, a catalog of the old shape — is "unknown", and the record's own
`approved` stands (no move, no listing change). A row whose commit is
hard-blocked (the catalog's `releases[*].blocked`, or the block list file,
`contrib/blocks.py`) never becomes the approved commit: the newest release
that is not blocked does.

THE CATALOG IS READ IN FOUR WAYS, each where it belongs: `fresh` (a rebuild
now — the Check button, so the catalog's 10-minute cache never makes it a
no-op), `ttl` (the catalog's own 10-minute TTL — the engine's automatic
check, off the request thread and off the slot), `cached` (whatever is
cached, building only when nothing is), and `local` (the cache FILE or
nothing — `actions.facts`, which runs on a page GET and must never wait on
the network).

SWITCHING (D13) is always the user's explicit act, both ways, each one
Activity line (kind `switch`) whose Undo is the opposite switch
(`engine.restore`): `switch_to_link` (marketplace → github: the mirror moves
to the author's tip, then one fast-forward when nothing of Bob's is in the
way) and `switch_to_reviewed` (github → marketplace: refused while Bob has
changes of his own; the mirror and HEAD go to the approved commit, pinned).
Nothing here switches on its own (M4).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from fused_render import git_upstream as gu
from fused_render.contrib import classify, follow, replay, store

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

#: Where a legacy (release-package) install keeps its stamp.
LEGACY_STAMP = os.path.join(".fused", "marketplace.json")


@dataclass(frozen=True)
class Approved:
    """What `approved_for` answers: the commit a marketplace copy should
    run, the release it belongs to, and how sure the answer is —
    `status` "live" (a readable row), "delisted" (a fresh catalog without
    the slug) or "unknown" (the record's own `approved`, unchanged)."""
    commit: str | None
    at: str | None
    whats_new: str | None
    version: str | None
    status: str
    releases: list = field(default_factory=list)
    credits: list = field(default_factory=list)
    slug: str | None = None

    def entry(self) -> dict | None:
        """The record's `approved` shape."""
        return store.approved_entry({"commit": self.commit, "at": self.at,
                                     "whats_new": self.whats_new, "version": self.version,
                                     "credits": self.credits})


# ------------------------------------------------------------------ the catalog


def _mp():
    try:
        from fused_render import marketplace
    except Exception:  # noqa: BLE001 — no catalog module: every answer is "unknown"
        logger.exception("contrib market: marketplace module unavailable")
        return None
    return marketplace


def _local_cache(mp):
    """The cached catalog FILE, never a build (no network)."""
    reader = getattr(mp, "_read_cache", None)
    if callable(reader):
        return reader()
    path_fn = getattr(mp, "catalog_path", None)
    if not callable(path_fn):
        return None
    try:
        with open(path_fn(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _snapshot(mode: str = "cached") -> dict | None:
    """The catalog snapshot by `mode` (module docstring: fresh | ttl |
    cached | local), or None. Never raises."""
    mp = _mp()
    if mp is None:
        return None
    try:
        if mode == "fresh":
            got = mp.get_snapshot(force=True)
        elif mode == "ttl":
            got = mp.get_snapshot()
        elif mode == "local":
            got = _local_cache(mp)
        else:
            got = mp.cached_snapshot()
    except Exception:  # noqa: BLE001 — an unreadable catalog is "unknown" (M3)
        logger.exception("contrib market: catalog read (%s) failed", mode)
        return None
    snap = got[0] if isinstance(got, tuple) else got
    return snap if isinstance(snap, dict) else None


def _fresh(snap) -> bool:
    """A catalog that may say "this app is not listed": built just now or
    within its TTL from the CURRENT shape (`status == "ok"`), not a stale
    fallback after a failed refresh."""
    return isinstance(snap, dict) and snap.get("status") == "ok" and not snap.get("stale") \
        and isinstance(snap.get("apps"), (dict, list))


def _rows(snap) -> dict:
    """`{slug: row}` from the snapshot's `apps` (a dict by slug; a list of
    rows is read by each row's `slug`)."""
    apps = snap.get("apps") if isinstance(snap, dict) else None
    if isinstance(apps, dict):
        return {k: v for k, v in apps.items() if isinstance(k, str) and isinstance(v, dict)}
    if isinstance(apps, list):
        return {r["slug"]: r for r in apps if isinstance(r, dict) and isinstance(r.get("slug"), str)}
    return {}


def _row_ok(row) -> bool:
    """A row fused may act on: a repo, a branch and a 40-hex commit."""
    return (isinstance(row, dict) and isinstance(row.get("repo"), str)
            and follow._valid(row.get("repo"), row.get("branch") or "main")
            and isinstance(row.get("commit"), str) and bool(_SHA_RE.match(row["commit"])))


def row_for(slug, *, fresh=False, mode=None) -> dict | None:
    """The catalog row for `slug` (a copy, with `slug` set), or None —
    unknown slug, unreadable row, no catalog. `fresh` rebuilds the catalog
    first (`get_snapshot(force=True)`); else the cached one
    (`cached_snapshot()`); `mode` overrides (module docstring)."""
    if not isinstance(slug, str) or not slug:
        return None
    snap = _snapshot(mode or ("fresh" if fresh else "cached"))
    row = _rows(snap).get(slug)
    return {**row, "slug": slug} if _row_ok(row) else None


def row_for_repo(repo, *, fresh=False, mode=None) -> dict | None:
    """The catalog row whose `repo` is `repo` (GitHub's case-insensitive
    rule), with its `slug`, or None. The index refuses one repo under two
    slugs (§18.2 step 3); were it to happen, the first wins."""
    if not isinstance(repo, str) or "/" not in repo:
        return None
    snap = _snapshot(mode or ("fresh" if fresh else "cached"))
    want = repo.lower()
    for slug, row in _rows(snap).items():
        if _row_ok(row) and row["repo"].lower() == want:
            return {**row, "slug": slug}
    return None


def _releases(row) -> list:
    rel = row.get("releases") if isinstance(row, dict) else None
    return [r for r in rel if isinstance(r, dict) and isinstance(r.get("commit"), str)
            and _SHA_RE.match(r["commit"])] if isinstance(rel, list) else []


def hard_blocked(slug, commit, row=None) -> bool:
    """`commit` is hard-blocked for `slug`: the row says so (its top-level
    `blocked` for the row's own commit, or the release's), or the block list
    file does (`blocks.is_blocked_commit`)."""
    if isinstance(row, dict) and row.get("commit") == commit and row.get("blocked") == "hard":
        return True
    for r in _releases(row):
        if r["commit"] == commit and r.get("blocked") == "hard":
            return True
    try:
        from fused_render.contrib import blocks

        hit = blocks.is_blocked_commit(slug, commit)
    except Exception:  # noqa: BLE001 — the catalog's own flag stands
        hit = None
    return bool(hit) and hit.get("level") == "hard"


def _pick(row, slug) -> Approved | None:
    """The approved commit a row stands for: `row.commit`, unless it is
    hard-blocked — then the newest release that is not. None when every
    candidate is blocked."""
    releases = _releases(row)
    order = [row["commit"]] + [r["commit"] for r in releases if r["commit"] != row["commit"]]
    for commit in order:
        if hard_blocked(slug, commit, row):
            continue
        rel = next((r for r in releases if r["commit"] == commit), None) or {}
        src = rel if rel else row
        whats = src.get("whats_new") if isinstance(src.get("whats_new"), str) else None
        if whats is None and commit == row["commit"] and isinstance(row.get("whats_new"), str):
            whats = row["whats_new"]
        at = src.get("at") if isinstance(src.get("at"), str) else None
        if at is None and commit == row["commit"]:
            at = next((row[k] for k in ("last_release_at", "updated_at")
                       if isinstance(row.get(k), str)), None)
        credits = src.get("credits") if isinstance(src.get("credits"), list) else []
        version = src.get("version") if isinstance(src.get("version"), str) else (
            row.get("version") if commit == row["commit"] and isinstance(row.get("version"), str)
            else None)
        return Approved(commit=commit, at=at, whats_new=whats, version=version, status="live",
                        releases=releases, credits=[c for c in credits if isinstance(c, str)],
                        slug=slug)
    return None


def _unknown(record) -> Approved:
    a = record.get("approved") if isinstance(record, dict) and isinstance(
        record.get("approved"), dict) else {}
    return Approved(commit=a.get("commit"), at=a.get("at"), whats_new=a.get("whats_new"),
                    version=a.get("version"), status="unknown",
                    credits=list(a.get("credits") or []),
                    slug=(record or {}).get("slug") if isinstance(record, dict) else None)


def approved_for(record, *, fresh=False, mode=None) -> Approved:
    """What a marketplace record should run (module docstring): the row's
    commit (or the newest unblocked release) — `status` "live"; "delisted"
    when a FRESH catalog lacks the slug; "unknown" otherwise (the record's
    own `approved`). A record without a slug (a duplicate claimed from its
    marker, an older record) is found by its repo."""
    if not isinstance(record, dict):
        return _unknown(None)
    snap = _snapshot(mode or ("fresh" if fresh else "cached"))
    if snap is None:
        return _unknown(record)
    slug = record.get("slug") if isinstance(record.get("slug"), str) else None
    rows = _rows(snap)
    row = rows.get(slug) if slug else None
    if row is None and not slug and isinstance(record.get("repo"), str):
        want = record["repo"].lower()
        slug, row = next(((s, r) for s, r in rows.items() if _row_ok(r)
                          and r["repo"].lower() == want), (None, None))
    if row is not None and _row_ok(row):
        if (row.get("repo") or "").lower() != (record.get("repo") or "").lower():
            # The slug now names ANOTHER repo (a delist and a re-list by
            # someone else): never follow it — the copy is this repo's.
            return _unknown(record) if not _fresh(snap) else Approved(
                commit=None, at=None, whats_new=None, version=None, status="delisted",
                slug=slug)
        picked = _pick(row, slug)
        return picked if picked is not None else _unknown(record)
    if row is None and slug and _fresh(snap):
        return Approved(commit=None, at=None, whats_new=None, version=None,
                        status="delisted", slug=slug)
    return _unknown(record)


#: `record.approved` keys that are this module's bookkeeping, not the row's:
#: `prev` (the releases ran before, `PREV_KEEP` of them) and `from_row` (a
#: row commit the block list vetted away, `blocks.vet_approved`).
_BOOKKEEPING = ("prev", "from_row")
PREV_KEEP = 10


def refresh_approved(root, *, fresh=False, mode=None) -> Approved | None:
    """Bring `record.approved` / `record.listing` up to date with the
    catalog (`approved_for`), before the engine's fetch. A new approved
    commit ends an undo hold (`skip_tip`): "a new author tip clears it by
    itself" (§17.2) reads "a new release clears it" here. `delisted` keeps
    `approved` (the copy keeps working on its last reviewed version);
    `unknown` writes nothing. Writes only when something changed. No git,
    no slot; the catalog read may build it (`ttl` by default — off the
    request thread only). Answers the Approved, or None for any other kind."""
    rec = store.get(root)
    if not isinstance(rec, dict) or rec.get("kind") != "marketplace":
        return None
    ap = approved_for(rec, fresh=fresh, mode=mode or ("fresh" if fresh else "ttl"))
    now = time.time()
    if ap.status == "live" and ap.entry() is not None:
        entry = ap.entry()
        listing = rec.get("listing") if isinstance(rec.get("listing"), dict) else {}
        have = rec.get("approved") if isinstance(rec.get("approved"), dict) else {}
        if have.get("from_row") == entry["commit"]:
            # The block list vetted this row's commit away (it contains a
            # blocked commit, `blocks.vet_approved`): keep the safe one.
            entry = have
        same = {k: v for k, v in have.items() if k not in _BOOKKEEPING} == \
            {k: v for k, v in entry.items() if k not in _BOOKKEEPING}
        if same and listing.get("status") == "live" and rec.get("slug") == ap.slug:
            return ap

        def live(r):
            old = r.get("approved") if isinstance(r.get("approved"), dict) else {}
            new = dict(entry)
            if old.get("commit") and old.get("commit") != new["commit"]:
                # The releases this copy ran before, newest first: where a
                # hard block rolls back to when the catalog can't be read.
                prev = {k: v for k, v in old.items() if k not in _BOOKKEEPING}
                new["prev"] = ([prev] + [p for p in old.get("prev") or []
                                         if isinstance(p, dict)])[:PREV_KEEP]
            elif old.get("prev"):
                new["prev"] = old["prev"]
            r["approved"] = new
            if ap.slug:
                r["slug"] = ap.slug
            r["listing"] = {"status": "live", "checked_at": now, "delisted_at": None}
            if old.get("commit") != new["commit"]:
                r["skip_tip"] = None
        store.update(root, live)
    elif ap.status == "delisted":
        listing = rec.get("listing") if isinstance(rec.get("listing"), dict) else {}
        if listing.get("status") == "delisted":
            return ap

        def gone(r):
            r["listing"] = {"status": "delisted", "checked_at": now, "delisted_at": now}
        store.update(root, gone)
    return ap


def is_delisted(record) -> bool:
    listing = (record or {}).get("listing") if isinstance(record, dict) else None
    return isinstance(listing, dict) and listing.get("status") == "delisted"


def release_date(at) -> str:
    """"Oct 26" for an ISO time (`approved.at`), "" when unknown."""
    if not isinstance(at, str) or not at:
        return ""
    try:
        from datetime import datetime

        t = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{t.strftime('%b')} {t.day}"


def release_words(who, approved) -> str:
    """The Activity line of an automatic release move (§18.4): "Updated to
    <who>'s release of <Mon D>: <whats_new>"."""
    a = approved if isinstance(approved, dict) else {}
    when = release_date(a.get("at"))
    text = f"Updated to {who}'s release" + (f" of {when}" if when else "")
    whats = (a.get("whats_new") or "").strip()
    return text + (f": {whats}" if whats else ".")


# -------------------------------------------------------------------- switching


def _engine():
    from fused_render.contrib import engine

    return engine


def _fetch_failed(engine, reason, who) -> dict:
    if reason == "offline":
        return engine._refuse("offline", engine.OFFLINE_MESSAGE)
    return engine._refuse(reason or "failed", engine._sync_error_words(reason, who))


def _home_ready(engine, root):
    """The refusal a switch answers when the copy can't move now, else None.
    Slot held."""
    if not engine._on_home(root):
        return engine._refuse("off-default", engine.OTHER_VERSION_MESSAGE)
    if gu._operation_in_flight(root) is not None or engine._job_open(root):
        return engine._refuse("in-progress", engine.ELSEWHERE_MESSAGE)
    if engine._claude_busy(root):
        return engine._refuse("claude", engine.CLAUDE_BUSY_MESSAGE)
    if engine._unsafe_config(root):
        # A switch resets or fast-forwards the folder: the repo's own config
        # must not get to run a command during it (`replay.config_hazards`).
        return engine._refuse(engine.UNSAFE_CONFIG, replay.UNSAFE_CONFIG_MESSAGE)
    return None


def _hard_reset_locked(root, target, why) -> tuple[bool, str | None]:
    """HEAD (fused/mine) → `target` with `reset --hard`, HEAD and any edit
    it would overwrite pinned first (`backup_locked`, Undo). Untracked
    files are never removed (a switch keeps the user's data). `(ok,
    backup)`; the slot is held."""
    head = replay.rev(root, "HEAD")
    target = replay.rev(root, target)
    if not head or not target:
        return False, None
    if head == target:
        return True, None
    clobber = replay.clobber_paths(root, head, target)
    refs = replay.backup_locked(root, head, why=why, also=clobber)
    if not refs or (clobber and not any(r.endswith("-wip") for r in refs)):
        replay.unbackup_locked(root, refs)
        return False, None
    replay.protect_after_move(root)
    with replay.fused_aside(root, target):
        r = replay.git(root, "reset", "-q", "--hard", target, timeout=replay.REPLAY_TIMEOUT_S)
    if not replay.ok(r):
        logger.warning("contrib market: reset to %s failed in %s: %s", target, root, gu._text(r))
        return False, refs[0]
    if not replay.undo_race_locked(root, head, refs, hard=True):
        return False, refs[0]
    replay.protect_after_move(root)
    return True, refs[0]


def _switched(r, frm, backup) -> None:
    lst = r.get("switched") if isinstance(r.get("switched"), list) else []
    lst.append({"from": frm, "at": time.time(), "backup": backup})
    r["switched"] = lst[-20:]


LINK_TEXT = "Following {who} directly now. You'll get every change, unreviewed."
REVIEWED_TEXT = "Switched to reviewed releases."
CHANGES_FIRST = "Propose or discard your changes first."


def switch_to_link(root, *, backup=None) -> dict:
    """Marketplace → github (§18.4 D13), the user's explicit choice: the
    author's branch is fetched, the mirror moves from the approved commit
    to the author's tip (a pinned non-fast-forward when the approved commit
    is not in it), the record and the marker become `github` (the push
    guard stays), and — when nothing of Bob's is in the way (clean, not
    ahead) — one fast-forward brings the tip in, pinned for Undo. `backup`
    (Undo of a `switch_to_reviewed` line): HEAD goes back to exactly that
    commit instead. Activity kind `switch`, Undo = the opposite switch."""
    engine = _engine()
    rec = store.get(root)
    if not isinstance(rec, dict) or rec.get("kind") != "marketplace":
        return engine._refuse("not-a-copy", "This app isn't from the Community.")
    repo, branch = rec.get("repo"), rec.get("branch")
    if not follow._valid(repo, branch):
        return engine._refuse("no-source", "fused can't tell where this app comes from.")
    who = engine._who(rec)
    if engine._claude_busy(root):
        return engine._refuse("claude", engine.CLAUDE_BUSY_MESSAGE)
    link = follow.Follow("branch", follow.github_url(repo), branch)

    def go():
        with replay.slot() as held:
            if not held:
                return engine._refuse("busy", engine.BUSY_MESSAGE)
            refused = _home_ready(engine, root)
            if refused:
                return refused
            fol = follow.for_record(rec) or link
            # The pinned fetch takes the branch (and the approved commit);
            # its mirror move is a no-op unless a release landed meanwhile.
            tip, reason = follow.fetch(root, fol)
            if not tip:
                return _fetch_failed(engine, reason, who)
            m = follow.update_mirror(root, link, why="switch")
            main = replay.rev(root, engine._mirror(link))
            if main != follow.tip(root, link):
                return engine._refuse("failed", f"Couldn't switch to following {who} directly; "
                                                "nothing was changed.")

            def flip(r):
                r["kind"] = "github"
                r["block"] = None
                r["skip_tip"] = None
                _switched(r, "marketplace", m.get("backup"))
            store.update(root, flip)
            classify._forget_kind(root)
            classify.ensure_push_guard(root, "github")
            classify._set_upstream(root, branch, "github")
            head = replay.rev(root, "HEAD")
            land = None
            target = replay.rev(root, backup) if backup else None
            if target:
                ok, land = _hard_reset_locked(root, target, "switch")
                if not ok:
                    land = None
            elif (head and main and head != main and not replay.tracked_edits(root)
                  and replay.is_ancestor(root, head, main)):
                refs = replay.backup_locked(root, head, why="switch")
                if refs:
                    why, _ = replay.land_ff_locked(root, main)
                    if why:
                        replay.unbackup_locked(root, refs)
                    else:
                        land = refs[0]
        engine._status_cache.pop(root, None)
        engine._note_head(root)
        text = LINK_TEXT.format(who=who)
        engine._activity(root, store.SWITCH_KIND, text, backup=land)
        return {"ok": True, "message": text}

    return engine._user_run(root, go)


def switch_to_reviewed(root, row, *, backup=None) -> dict:
    """Github → marketplace (§18.4 D13), the user's explicit choice:
    refused with "Propose or discard your changes first." while the copy has
    commits of Bob's the author lacks or unsaved edits; else the approved
    commit of `row` is fetched (pinned), the mirror moves to it (pinned when
    that is backwards), HEAD goes to it (`reset --hard`, HEAD pinned for
    Undo — `backup`, Undo of a `switch_to_link` line, lands on exactly that
    commit instead, unless it is blocked), and the record and the marker
    become `marketplace`, upstream the mirror. Activity kind `switch`."""
    engine = _engine()
    rec = store.get(root)
    if not isinstance(rec, dict) or rec.get("kind") != "github":
        return engine._refuse("not-a-copy", "This only works for an app you follow by GitHub link.")
    if not _row_ok(row) or (row.get("repo") or "").lower() != (rec.get("repo") or "").lower():
        return engine._refuse("not-listed", "This app isn't in the Community.")
    slug = row.get("slug") if isinstance(row.get("slug"), str) else None
    ap = _pick(row, slug)
    if ap is None or ap.entry() is None:
        return engine._refuse("blocked", "Every reviewed version of this app is blocked.")
    branch = rec.get("branch") or row.get("branch")
    if not follow._valid(rec.get("repo"), branch):
        return engine._refuse("no-source", "fused can't tell where this app comes from.")
    who = engine._who(rec)
    if engine._claude_busy(root):
        return engine._refuse("claude", engine.CLAUDE_BUSY_MESSAGE)
    pinned = follow.Follow("pinned", follow.github_url(rec["repo"]), branch, commit=ap.commit)

    def go():
        with replay.slot() as held:
            if not held:
                return engine._refuse("busy", engine.BUSY_MESSAGE)
            refused = _home_ready(engine, root)
            if refused:
                return refused
            main = replay.rev(root, engine._mirror(pinned))
            head = replay.rev(root, "HEAD")
            ahead = replay.count(root, "--no-merges", f"{main}..{head}") if main and head else 0
            if ahead or replay.tracked_edits(root):
                return engine._refuse("changes", CHANGES_FIRST)
            tip, reason = follow.fetch(root, pinned)
            if not tip:
                return _fetch_failed(engine, reason, who)
            m = follow.update_mirror(root, pinned, why="switch")
            if replay.rev(root, engine._mirror(pinned)) != ap.commit:
                if m.get("reason") == follow.NOT_ON_BRANCH:
                    return engine._refuse(follow.NOT_ON_BRANCH, not_on_branch_words(who))
                if m.get("reason") == follow.BLOCKED:
                    return engine._refuse("blocked", "Fused blocked this release, so fused "
                                                     "won't switch to it. Nothing was changed.")
                return engine._refuse("failed", "Couldn't switch to the reviewed version; "
                                                "nothing was changed.")
            target = ap.commit
            if backup and replay.rev(root, backup):
                want = replay.rev(root, backup)
                if not _backup_blocked(root, slug, want, ap.commit):
                    target = want
            ok, land = _hard_reset_locked(root, target, "switch")
            if not ok:
                return engine._refuse("failed", "Couldn't switch to the reviewed version; your "
                                                "copy is saved in Activity.")

            def flip(r):
                r["kind"] = "marketplace"
                r["owner_checked"] = True
                r["slug"] = slug
                r["approved"] = ap.entry()
                r["listing"] = {"status": "live", "checked_at": time.time(),
                                "delisted_at": None}
                r["block"] = None
                r["block_warn"] = None
                r["skip_tip"] = None
                _switched(r, "github", land)
            store.update(root, flip)
            classify._forget_kind(root)
            classify.ensure_push_guard(root, "marketplace")
            classify._set_upstream(root, branch, "marketplace")
            classify._mark_unchecked(root, False)
        engine._status_cache.pop(root, None)
        engine._note_head(root)
        engine._activity(root, store.SWITCH_KIND, REVIEWED_TEXT, backup=land)
        return {"ok": True, "message": REVIEWED_TEXT}

    return engine._user_run(root, go)


def not_on_branch_words(who) -> str:
    return f"The reviewed version isn't in {who}'s app any more. Nothing was changed."


def _backup_blocked(root, slug, sha, mirror) -> bool:
    try:
        from fused_render.contrib import blocks

        return bool(blocks.contains_blocked(root, slug, sha, mirror))
    except Exception:  # noqa: BLE001 — unknown reads as blocked (M1)
        logger.exception("contrib market: block check failed for %s", root)
        return True


def undo_switch(root, entry) -> dict:
    """Undo of a `switch` Activity line (`engine.restore`): the opposite
    switch, landing on the line's backup when it has one."""
    engine = _engine()
    rec = store.get(root) or {}
    backup = (entry or {}).get("backup")
    if isinstance(backup, list):
        backup = next((b for b in backup if isinstance(b, str) and not b.endswith("-wip")), None)
    if rec.get("kind") == "marketplace":
        out = switch_to_link(root, backup=backup)
    elif rec.get("kind") == "github":
        # The catalog FILE (an Undo runs on the request thread: no build),
        # else the row this copy last ran.
        row = row_for_repo(rec.get("repo"), mode="local") or (
            {**_unknown(rec).__dict__, "repo": rec.get("repo"), "branch": rec.get("branch"),
             "slug": rec.get("slug")} if (rec.get("approved") or {}).get("commit") else None)
        if row is None:
            return engine._refuse("not-listed", "This app isn't in the Community any more.")
        out = switch_to_reviewed(root, row, backup=backup)
    else:
        return engine._refuse("nothing", "There's nothing to bring back for that.")
    if out.get("ok") and entry and entry.get("id"):
        store.mark_restored(root, entry["id"])
    return out


# ------------------------------------------------------------------- legacy


def legacy_stamp(path) -> str:
    return os.path.join(path, LEGACY_STAMP)


ADOPT_TEXT = "Switched to the reviewed version. Your edits and data stayed."


def adopt_legacy(path, row) -> dict:
    """Turn a copy installed from an old release package (`.fused/
    marketplace.json`, plain files, no `.git`) into a marketplace copy IN
    PLACE (§18.4): `git init`, origin = the row's repo, fetch the branch and
    the approved commit, the mirror and `fused/mine` at it, HEAD on
    `fused/mine`, then a MIXED reset — the index takes the approved commit's
    tree and the working tree is not touched, so the user's edits show as
    edits and their data as untracked files (tracked files the package
    never had are checked out, so they don't read as the user's deletions;
    a file the user deleted themselves comes back too — nothing is lost
    either way). The stamp goes; the record is a
    marketplace record (`record_install`). Refused for a folder that is a
    repository already, and for a row fused cannot read. `{ok, message}`."""
    def refuse(reason, message):
        return {"ok": False, "reason": reason, "message": message}

    if not _row_ok(row):
        return refuse("not-listed", "This app isn't in the Community any more.")
    slug = row.get("slug") if isinstance(row.get("slug"), str) else None
    if not os.path.isfile(legacy_stamp(path)):
        return refuse("not-legacy", "This app wasn't installed from the old app store.")
    if os.path.lexists(os.path.join(path, ".git")):
        return refuse("is-repo", "This app is already connected to another source, so fused "
                                 "won't change it.")
    ap = _pick(row, slug)
    if ap is None or ap.entry() is None:
        return refuse("blocked", "Every reviewed version of this app is blocked.")
    branch = row.get("branch") or "main"
    url = follow.github_url(row["repo"])
    home = classify.HOME_BRANCH
    with replay.slot() as held:
        if not held:
            return refuse("busy", "Busy updating this app; try again in a moment.")
        r = replay.git(path, "init", "-q")
        if not replay.ok(r):
            return refuse("failed", "Couldn't set this app up for reviewed updates.")
        try:
            # The marketplace marker goes in BEFORE the origin: from here on
            # a `classify.ensure` racing this adopt (a catalog GET's
            # `installed_copies`, an app open's kick) sees a marked folder
            # with no record and leaves it alone — it never finds a repo
            # with someone else's origin and no marker, which it would
            # record as a `github` copy following the author's unreviewed
            # tip (M1/M4). The `_Undo` below takes it away with `.git`.
            if not replay.ok(replay.git(path, "config", "--local", classify.KIND_CONFIG,
                                        "marketplace")):
                raise _Undo("failed")
            replay.git(path, "symbolic-ref", "HEAD", f"refs/heads/{home}")
            replay.git(path, "remote", "add", "origin", url)
            try:
                from fused_render import app_git

                app_git._ensure_excludes(path)
            except Exception:  # noqa: BLE001 — `.fused/` stays out by the reset below anyway
                logger.exception("contrib market: excludes for %s", path)
            fol = follow.Follow("pinned", url, branch, commit=ap.commit)
            tip, reason = follow.fetch(path, fol)
            if not tip or not replay.rev(path, ap.commit):
                raise _Undo("offline" if reason == "offline" else (reason or "failed"))
            if not replay.is_ancestor(path, ap.commit, tip):
                raise _Undo(follow.NOT_ON_BRANCH)
            for ref in (f"refs/heads/{branch}", f"refs/heads/{home}"):
                if not replay.ok(replay.git(path, "update-ref", ref, ap.commit)):
                    raise _Undo("failed")
            if not replay.ok(replay.git(path, "reset", "-q", "--mixed", ap.commit)):
                raise _Undo("failed")
            # The package never carried every tracked file (the export drops
            # dotted names — `.gitignore`, `.github/` — and the old installer
            # pruned some): those read as deletions now, which would become
            # "your changes" (proposed, committed as "Your edits"). They come
            # back from the commit — ONLY the missing paths, never a bare
            # checkout that would overwrite the user's edits.
            gone = replay.names(replay.git(path, "ls-files", "-z", "--deleted"))
            if gone:
                replay.git(path, "checkout", "-q", ap.commit, "--", *gone)
            classify._set_upstream(path, branch, "marketplace")
        except _Undo as u:
            import shutil

            shutil.rmtree(os.path.join(path, ".git"), ignore_errors=True)
            if u.reason == "offline":
                return refuse("offline", "Can't reach GitHub right now.")
            if u.reason == follow.NOT_ON_BRANCH:
                return refuse(u.reason, "The reviewed version isn't in its author's app any "
                                        "more. Nothing was changed.")
            return refuse("failed", "Couldn't set this app up for reviewed updates. Nothing "
                                    "was changed.")
    try:
        os.remove(legacy_stamp(path))
    except OSError:
        logger.warning("contrib market: could not remove %s", legacy_stamp(path), exc_info=True)
    classify.record_install(path, url, kind="marketplace",
                            market={"slug": slug, "approved": ap.entry(), "branch": branch})
    # The HEAD it starts at, for the open gate's first answer (as an install).
    store.note_head(path, ap.commit)
    store.add_activity(path, "adopt", ADOPT_TEXT)
    return {"ok": True, "message": ADOPT_TEXT, "path": path}


class _Undo(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
