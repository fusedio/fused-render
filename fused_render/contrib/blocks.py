"""The block list (docs/CONTRIB_LOOP.md §18.4, B4): approved commits Fused
later found to be bad, and what a copy running one does about it.

THE LIST. `blocklist.json` in the index (maintainers only, human-reviewed
PRs), served by the backend and fetched by `marketplace.blocklist(force=)`
into `<marketplace.state_dir()>/blocklist.json` — `{"blocked": [{slug,
commit, level: hard|soft, reason, what_to_do, at}], "checked_at"}`. Offline,
the last list this machine saw applies (`refresh` answers False and the file
stands); no file at all = nothing blocked. Read here memoised on the file's
stat, so the open gates and the Activity reads cost a stat.

ENFORCED WHATEVER THE SWITCHES SAY (§18.8), at four moments: server start
(`startup`, a background thread), an app opened (`git_upstream`'s open
check → `engine.sync`, before the fetch), the sweep every 30 minutes
(`refresh._sweep_loop`) and the Source panel's Check. Enforcement holds the
copy lock and `replay.slot()`; it never moves a folder while a Merge with
Claude job is open (it marks the record — the open gate still answers 423).

HARD (`_hard`): the app stops (its background process, autostart off) and
its tools leave the bots' registry (`bots/apptools.discover` skips a folder
whose gate stands); the copy goes back to the newest release that is not
blocked, does not contain a blocked commit, and is on the author's branch:
the mirror moves there (pinned, no Undo), and the worktree too — a copy
with nothing of Bob's (clean, not ahead) by `reset --hard` (HEAD pinned,
untracked files kept); one with changes by a REVERT AS A MERGE: `merge-tree
HEAD target --merge-base=<blocked>` takes back exactly what the blocked
releases added and keeps Bob's own changes, landed as a two-parent commit
(HEAD, target) "Rolled back to the reviewed version of <date>" by
`land_ff_locked`. Two parents, not one (the contract's `-p HEAD` reading):
the rollback is then a merge, so `ahead` (no merges) never counts it as
Bob's, and the next release's merge base is the target, not the blocked
commit. A conflict there is `blocked-release`: Merge with Claude (purpose
`update`, theirs = target, base = blocked, `engine.merge_plan`), the app
stays stopped. The Activity line (kind `rollback`) names the pin as `ref`:
NO Undo — Undo would run the blocked code again (M1). Every release blocked
→ stopped, and the panel offers Remove this app.

SOFT: `block.level = soft`; automatic moves pause; opening asks first (the
`/api/apps` row's `contrib.block`, `confirm_soft`).

A `github` copy running a blocked commit of a listed repo gets `block_warn`
only — its owner chose to trust the author directly (§18.10).

NEUTRALISED. A rolled-back copy with changes still has the blocked commits
in its HISTORY (the revert is a merge on top). `block.covers` lists them and
`block.rolled_back_to` is the rollback commit: while HEAD contains it, those
commits are not "running" any more, and nothing is enforced again.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

from fused_render import git_upstream as gu
from fused_render.contrib import follow, replay, store

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LEVELS = ("hard", "soft")

#: How long a page waits for the copy lock before enforcement skips (the
#: next trigger enforces; the open gate answers from the record meanwhile).
_LOCK_WAIT_S = 30.0


# ------------------------------------------------------------------ the list


def _mp():
    try:
        from fused_render import marketplace
    except Exception:  # noqa: BLE001
        logger.exception("contrib blocks: marketplace module unavailable")
        return None
    return marketplace


def path() -> str:
    """`<marketplace.state_dir()>/blocklist.json`."""
    mp = _mp()
    sd = getattr(mp, "state_dir", None) if mp else None
    if callable(sd):
        try:
            return os.path.join(sd(), "blocklist.json")
        except Exception:  # noqa: BLE001
            pass
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), "marketplace", "blocklist.json")


def _clean(entries) -> list:
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        slug, commit, level = e.get("slug"), e.get("commit"), e.get("level")
        if not isinstance(slug, str) or not isinstance(commit, str) \
                or not _SHA_RE.match(commit) or level not in LEVELS:
            continue
        out.append({"slug": slug, "commit": commit, "level": level,
                    "reason": e.get("reason") if isinstance(e.get("reason"), str) else "",
                    "what_to_do": e.get("what_to_do")
                    if isinstance(e.get("what_to_do"), str) else "",
                    "at": e.get("at") if isinstance(e.get("at"), str) else None})
    return out


_memo_lock = threading.Lock()
_memo: dict = {"stamp": None, "entries": []}


def _stamp(p):
    try:
        st = os.stat(p)
        return (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def entries() -> list:
    """Every valid entry of the list on disk ([] when there is none)."""
    p = path()
    stamp = _stamp(p)
    with _memo_lock:
        if stamp is not None and _memo["stamp"] == stamp:
            return list(_memo["entries"])
    from fused_render.shell import storage

    data = storage.read_json(p) if stamp is not None else None
    got = _clean(data.get("blocked")) if isinstance(data, dict) else []
    with _memo_lock:
        _memo.update(stamp=stamp, entries=got)
    return list(got)


def for_slug(slug) -> list:
    return [e for e in entries() if e["slug"] == slug] if isinstance(slug, str) else []


def refresh(*, force: bool = True) -> bool:
    """Ask the backend for the list (`marketplace.blocklist(force=)`, its
    own TTL/backoff) and keep it at `path()`. False offline, on an error or
    a stale answer — the last list on disk applies then."""
    mp = _mp()
    fn = getattr(mp, "blocklist", None) if mp else None
    if not callable(fn):
        return False
    try:
        ans = fn(force=force)
    except Exception:  # noqa: BLE001 — offline: the last list applies
        logger.warning("contrib blocks: block list refresh failed", exc_info=True)
        return False
    if not isinstance(ans, dict) or not isinstance(ans.get("blocked"), list) \
            or ans.get("stale") or ans.get("error"):
        return False
    got = _clean(ans["blocked"])
    if got != entries() or _stamp(path()) is None:
        from fused_render.shell import storage

        try:
            storage.write_json(path(), {"blocked": got,
                                        "checked_at": ans.get("checked_at") or time.time()})
        except Exception:  # noqa: BLE001
            logger.exception("contrib blocks: could not write %s", path())
            return False
    return True


def is_blocked_commit(slug, sha) -> dict | None:
    """The block-list entry for exactly `sha` of `slug`, or None. No git."""
    if not isinstance(sha, str) or not sha:
        return None
    for e in for_slug(slug):
        if e["commit"] == sha:
            return e
    return None


def _slug(rec) -> str | None:
    slug = rec.get("slug") if isinstance(rec, dict) else None
    if isinstance(slug, str) and slug:
        return slug
    try:
        from fused_render.contrib import market

        row = market.row_for_repo((rec or {}).get("repo"), mode="local")
    except Exception:  # noqa: BLE001
        row = None
    return row.get("slug") if isinstance(row, dict) else None


def _neutralised(root, rec, commit, head) -> bool:
    """`commit` is in HEAD's history but rolled back (module docstring)."""
    b = (rec or {}).get("block") if isinstance((rec or {}).get("block"), dict) else None
    if not b or commit not in (b.get("covers") or [b.get("commit")]):
        return False
    rb = b.get("rolled_back_to")
    return bool(rb) and not b.get("conflict") and replay.is_ancestor(root, rb, head)


def contains_blocked(root, slug, sha, mirror=None, *, rec=None, levels=LEVELS) -> dict | None:
    """The entry of a blocked commit of `slug` that `sha` contains — `sha`
    itself, or a commit of `rev-list sha ^mirror` (one ancestry question per
    entry, never a walk of history) — unless rolled back in it. None when
    there is none. "Undo never lands on a blocked commit" (M1) asks this of
    a line's backup; the engine asks it of the approved commit."""
    if not sha:
        return None
    for e in for_slug(slug):
        if e["level"] not in levels:
            continue
        c = e["commit"]
        if c == sha:
            return e
        if not replay.is_ancestor(root, c, sha):
            continue
        if mirror and replay.is_ancestor(root, c, mirror):
            continue
        if rec is not None and _neutralised(root, rec, c, sha):
            continue
        return e
    return None


def backup_blocked(root, rec, backup_ref) -> bool:
    """An Activity line's backup lands on a blocked commit (or one that
    contains a blocked commit not in the mirror): its Undo is refused and
    not offered. Only marketplace records are asked (M1 is about what a
    marketplace copy runs)."""
    if not isinstance(rec, dict) or rec.get("kind") != "marketplace":
        return False
    slug = _slug(rec)
    if not for_slug(slug):
        return False
    sha = replay.rev(root, backup_ref) if backup_ref else None
    if not sha:
        return False
    mirror = replay.rev(root, f"refs/heads/{rec.get('branch')}") if rec.get("branch") else None
    return contains_blocked(root, slug, sha, mirror, rec=rec) is not None


# ------------------------------------------------------------------- views


def view(block) -> dict | None:
    """What the page reads (`/api/apps` row `contrib.block`, the Source
    panel, the open gate's 423): `{level, reason, what_to_do, at,
    rolled_back_to, rolled_back_date, conflict, no_target, confirmed}`."""
    if not isinstance(block, dict) or block.get("level") not in LEVELS:
        return None
    from fused_render.contrib import market

    return {"level": block["level"], "reason": block.get("reason") or "",
            "what_to_do": block.get("what_to_do") or "", "at": block.get("at"),
            "rolled_back_to": block.get("rolled_back_to"),
            "rolled_back_date": market.release_date(block.get("target_at")) or None,
            "conflict": bool(block.get("conflict")), "no_target": bool(block.get("no_target")),
            "confirmed": bool(block.get("confirmed_at"))}


def standing(rec) -> bool:
    """A hard block still stands on this record: not rolled back yet, a
    rollback that conflicts, or nothing to roll back to."""
    b = (rec or {}).get("block") if isinstance(rec, dict) else None
    if not isinstance(b, dict) or b.get("level") != "hard":
        return False
    return not b.get("rolled_back_to") or bool(b.get("conflict")) or bool(b.get("no_target"))


def gate(root) -> dict | None:
    """`{"blocked": view}` while a hard block stands on the copy at `root`
    — the open gates (`render`, `/api/apps/entry`) answer 423 with it. Also
    before enforcement caught up: the last HEAD this copy was seen at is
    hard-blocked. No git; a record read and the list's stat."""
    try:
        rec = store.get(root)
        if not isinstance(rec, dict) or rec.get("kind") != "marketplace":
            return None
        if standing(rec):
            return {"blocked": view(rec.get("block"))}
        hit = is_blocked_commit(_slug(rec), rec.get("last_head"))
        if hit and hit["level"] == "hard":
            b = (rec.get("block") or {}) if isinstance(rec.get("block"), dict) else {}
            if b.get("commit") == hit["commit"] and b.get("rolled_back_to"):
                return None
            return {"blocked": view({**hit, "rolled_back_to": None})}
    except Exception:  # noqa: BLE001 — a broken gate must not lock every app
        logger.exception("contrib blocks: gate failed for %s", root)
    return None


def blocked_dirs() -> set:
    """The store keys (realpaths) of every copy whose hard block stands —
    what the bots' app-tool registry skips (`bots/apptools.discover`: an
    installed app's `mcp.toml` tools are the one way its code reaches
    Claude). One store read, no git."""
    try:
        return {k for k, rec in (store.all() or {}).items()
                if isinstance(rec, dict) and rec.get("kind") == "marketplace" and standing(rec)}
    except Exception:  # noqa: BLE001
        logger.exception("contrib blocks: blocked_dirs failed")
        return set()


def confirm_soft(root) -> None:
    """The user saw the soft-block sheet and opens the app anyway."""
    def fn(r):
        b = r.get("block")
        if isinstance(b, dict) and b.get("level") == "soft":
            b["confirmed_at"] = time.time()
    store.update(root, fn)


# ------------------------------------------------------------- enforcement


def _stop_app(root) -> None:
    try:
        from fused_render import background_apps

        background_apps.stop_and_disable(root)
    except Exception:  # noqa: BLE001
        logger.exception("contrib blocks: could not stop %s", root)


def _newest(root, hits) -> dict:
    """Of several blocked commits HEAD contains, the newest (the one the
    others are ancestors of), else the first."""
    for e in hits:
        if all(o is e or replay.is_ancestor(root, o["commit"], e["commit"]) for o in hits):
            return e
    return hits[0]


def _target(root, rec, slug, hard) -> dict | None:
    """The release a hard block rolls back to: the newest of the row's
    releases (then the record's last approved) that is not blocked, does not
    contain a hard-blocked commit, is on the author's branch, and is here."""
    from fused_render.contrib import market

    # The catalog FILE only: enforcement may run with the slot held (the
    # engine's sync), where nothing may wait on the network (§0.9).
    row = market.row_for(slug, mode="local") if slug else None
    cands = []
    for r in market._releases(row):
        cands.append(r)
    a = rec.get("approved") if isinstance(rec.get("approved"), dict) else None
    # The record's own memory too (the catalog may be unreadable — offline,
    # the last list applies): its approved commit, then the releases it ran
    # before (`approved.prev`, newest first).
    for r in ([a] if a else []) + [p for p in (a or {}).get("prev") or [] if isinstance(p, dict)]:
        if r.get("commit") and all(r["commit"] != c.get("commit") for c in cands):
            cands.append(r)
    author = replay.rev(root, f"refs/remotes/origin/{rec.get('branch')}")
    for r in cands:
        c = r.get("commit")
        if not c or is_blocked_commit(slug, c) or r.get("blocked") == "hard":
            continue
        if not replay.rev(root, c) or (author and not replay.is_ancestor(root, c, author)):
            continue
        if any(replay.is_ancestor(root, e["commit"], c) for e in hard):
            continue
        return store.approved_entry(r)
    return None


def _rollback_text(hit, target) -> str:
    from fused_render.contrib import market

    when = market.release_date((target or {}).get("at"))
    reason = (hit.get("reason") or "").strip().rstrip(".")
    text = "Fused blocked the version you were running" + (f": {reason}." if reason else ".")
    text += (f" Rolled back to the release of {when}." if when
             else " Rolled back to the last reviewed version.")
    todo = (hit.get("what_to_do") or "").strip()
    return text + (f" {todo}" if todo else "")


def _no_target_text(hit) -> str:
    reason = (hit.get("reason") or "").strip().rstrip(".")
    text = "Fused blocked the version you were running" + (f": {reason}." if reason else ".")
    text += " There's no earlier reviewed version to go back to, so the app is stopped."
    todo = (hit.get("what_to_do") or "").strip()
    return text + (f" {todo}" if todo else "")


def _set_block(root, block) -> dict | None:
    return store.update(root, lambda r: r.__setitem__("block", block))


def _hard(root, rec, hits, all_hard, *, slug) -> dict:
    """A hard block on the copy at `root` (module docstring). Copy lock and
    slot held by the caller."""
    from fused_render.contrib import engine

    hit = _newest(root, hits)
    old = rec.get("block") if isinstance(rec.get("block"), dict) else {}
    block = {"level": "hard", "commit": hit["commit"], "reason": hit["reason"],
             "what_to_do": hit["what_to_do"], "at": hit["at"], "rolled_back_to": None,
             "conflict": False, "confirmed_at": None, "no_target": False,
             "covers": sorted({e["commit"] for e in hits}), "target": None, "target_at": None}
    if old.get("commit") == hit["commit"] and old.get("conflict"):
        block.update(conflict=True, target=old.get("target"), target_at=old.get("target_at"))
    _set_block(root, block)
    _stop_app(root)
    if engine._job_open(root):
        return {"level": "hard", "action": "marked"}
    target = _target(root, rec, slug, all_hard)
    if target is None:
        block["no_target"] = True
        _set_block(root, block)
        if not old.get("no_target") or old.get("commit") != hit["commit"]:
            store.add_activity(root, "rollback", _no_target_text(hit))
        return {"level": "hard", "action": "no-target"}
    block.update(target=target["commit"], target_at=target.get("at"))
    fol = follow.Follow("pinned", follow.github_url(rec["repo"]), rec["branch"],
                        commit=target["commit"])
    m = follow.update_mirror(root, fol, why="blocked")
    pin = m.get("backup")
    # What the catalog row says now (the blocked release, or one built on
    # it): remembered so the next catalog read keeps the safe target.
    unsafe = ((rec.get("approved") or {}).get("commit")
              if isinstance(rec.get("approved"), dict) else None) or hit["commit"]
    _set_approved(root, target, unsafe)
    head = replay.rev(root, "HEAD")
    if not engine._on_home(root) or gu._operation_in_flight(root) is not None:
        _set_block(root, block)
        return {"level": "hard", "action": "marked"}
    ahead = replay.count(root, "--no-merges", f"{hit['commit']}..{head}") or 0
    tracked = replay.tracked_edits(root)
    if not ahead and not tracked:
        from fused_render.contrib import market

        ok, backup = market._hard_reset_locked(root, target["commit"], "blocked")
        if not ok:
            _set_block(root, block)
            return {"level": "hard", "action": "marked"}
        new = replay.rev(root, "HEAD")
    else:
        if tracked and not replay.commit_edits_locked(root, tracked):
            _set_block(root, block)
            return {"level": "hard", "action": "marked"}
        if tracked:
            store.add_activity(root, "edits-saved",
                               f"Saved your edit to {engine._files_words(tracked)} before "
                               "rolling back.")
        head = replay.rev(root, "HEAD")
        mt = replay.merge_tree(root, head, target["commit"], base=hit["commit"])
        if not mt["supported"] or not mt["clean"]:
            block["conflict"] = True
            _set_block(root, block)
            text = _rollback_text(hit, target).replace(
                "Rolled back to", "Your changes overlap it, so Merge with Claude can roll back to")
            if not old.get("conflict") or old.get("commit") != hit["commit"]:
                store.add_activity(root, "rollback", text, ref=pin)
            return {"level": "hard", "action": "conflict", "files": list(mt.get("files") or [])}
        from fused_render.contrib import market

        when = market.release_date(target.get("at"))
        new = replay.out(replay.git(root, *replay.identity_args(root), "commit-tree", mt["tree"],
                                    "-p", head, "-p", target["commit"], "-m",
                                    "Rolled back to the reviewed version"
                                    + (f" of {when}" if when else "")))
        refs = replay.backup_locked(root, head, why="blocked") if new else []
        why = "failed" if not new or not refs else replay.land_ff_locked(root, new)[0]
        if why:
            replay.unbackup_locked(root, refs)
            _set_block(root, block)
            return {"level": "hard", "action": "marked", "why": why}
        backup = refs[0]
    block["rolled_back_to"] = new
    _set_block(root, block)
    engine._status_cache.pop(root, None)
    engine._note_head(root, new)
    store.add_activity(root, "rollback", _rollback_text(hit, target), ref=backup or pin)
    return {"level": "hard", "action": "rolled-back", "target": target["commit"]}


def _soft(root, rec, hit) -> dict:
    old = rec.get("block") if isinstance(rec.get("block"), dict) else {}
    if old.get("level") == "soft" and old.get("commit") == hit["commit"]:
        return {"level": "soft", "action": "kept"}
    _set_block(root, {"level": "soft", "commit": hit["commit"], "reason": hit["reason"],
                      "what_to_do": hit["what_to_do"], "at": hit["at"], "rolled_back_to": None,
                      "conflict": False, "confirmed_at": None})
    return {"level": "soft", "action": "marked"}


def _enforce_locked(root, rec) -> dict | None:
    slug = _slug(rec)
    head = replay.rev(root, "HEAD")
    if not slug or not head:
        return None
    listed = for_slug(slug)
    old = rec.get("block") if isinstance(rec.get("block"), dict) else None
    hits = [e for e in listed if replay.is_ancestor(root, e["commit"], head)
            and not _neutralised(root, rec, e["commit"], head)]
    hard = [e for e in hits if e["level"] == "hard"]
    if hard:
        return _hard(root, rec, hard, [e for e in listed if e["level"] == "hard"], slug=slug)
    if old and old.get("level") == "hard" and old.get("conflict") \
            and any(e["commit"] == old.get("commit") for e in listed):
        return {"level": "hard", "action": "conflict"}
    if hits:
        return _soft(root, rec, hits[0])
    a = rec.get("approved") if isinstance(rec.get("approved"), dict) else {}
    moved_on = bool(old and old.get("level") == "hard" and old.get("rolled_back_to")
                    and not old.get("conflict") and a.get("commit")
                    and a["commit"] != old.get("target")
                    and replay.is_ancestor(root, a["commit"], head))
    if old and (old.get("level") == "soft" or moved_on
                or not any(e["commit"] == old.get("commit") for e in listed)):
        # `moved_on`: a later SAFE release is in (HEAD past the rollback's
        # target) — the notice has done its job.
        # Soft and HEAD moved past it, or the entry left the list: over.
        # A rolled-back hard block whose entry still stands stays recorded
        # (the notice) — the gate is open already.
        _set_block(root, None)
        return {"level": old.get("level"), "action": "cleared"}
    return None


def enforce(root, *, wait: bool = True) -> dict | None:
    """Enforce the list on the marketplace copy at `root` (module
    docstring); `warn` for a github copy. Takes the copy lock (`wait`: up to
    _LOCK_WAIT_S, else not at all — the engine's own sync holds the slot and
    must never wait on a button) and the slot. Never raises. Answers what it
    did (`{level, action}`) or None."""
    try:
        rec = store.get(root)
        if not isinstance(rec, dict):
            return None
        if rec.get("kind") == "github":
            warn(root)
            return None
        if rec.get("kind") != "marketplace" or follow.is_repo_root(root) is None:
            return None
        if not for_slug(_slug(rec)) and not rec.get("block"):
            return None
        from fused_render.contrib import engine

        lock = engine._copy_lock(root)
        got = lock.acquire(timeout=_LOCK_WAIT_S) if wait else lock.acquire(blocking=False)
        if not got:
            return None
        try:
            with replay.slot() as held:
                if not held:
                    return None
                rec = store.get(root) or rec
                return _enforce_locked(root, rec)
        finally:
            lock.release()
    except Exception:  # noqa: BLE001
        logger.exception("contrib blocks: enforcement failed for %s", root)
        return None


def warn(root) -> dict | None:
    """A github copy running a blocked commit of its repo's listing:
    `block_warn` set (cleared when it no longer does). No move ever."""
    try:
        rec = store.get(root)
        if not isinstance(rec, dict) or rec.get("kind") != "github":
            return None
        if not entries() and not rec.get("block_warn"):
            return None  # nothing blocked anywhere: no catalog read, no git
        slug = _slug(rec)
        head = replay.rev(root, "HEAD") if slug and for_slug(slug) else None
        hit = None
        if head:
            hit = next((e for e in for_slug(slug)
                        if replay.is_ancestor(root, e["commit"], head)), None)
        want = ({"level": hit["level"], "commit": hit["commit"], "reason": hit["reason"],
                 "what_to_do": hit["what_to_do"], "at": hit["at"], "rolled_back_to": None,
                 "conflict": False, "confirmed_at": None} if hit else None)
        have = rec.get("block_warn")
        if (have or {}).get("commit") != (want or {}).get("commit"):
            store.update(root, lambda r: r.__setitem__("block_warn", want))
        return want
    except Exception:  # noqa: BLE001
        logger.exception("contrib blocks: warn failed for %s", root)
        return None


def enforce_all() -> list:
    """`enforce` every recorded marketplace copy, `warn` every github one.
    Answers the non-empty results."""
    out = []
    for k, rec in (store.all() or {}).items():
        if not isinstance(rec, dict) or not os.path.isdir(k):
            continue
        if rec.get("kind") == "marketplace":
            res = enforce(k)
            if res:
                out.append({"path": k, **res})
        elif rec.get("kind") == "github":
            warn(k)
    return out


def after_merge(root, new_tip) -> None:
    """Merge with Claude landed the rollback of a conflicting hard block
    (`engine.merge_landed`): the block is rolled back (the gate opens)."""
    rec = store.get(root) or {}
    b = rec.get("block") if isinstance(rec.get("block"), dict) else None
    if not b or b.get("level") != "hard" or not b.get("conflict"):
        return

    def fn(r):
        q = r.get("block")
        if isinstance(q, dict):
            q["conflict"] = False
            q["rolled_back_to"] = new_tip
    store.update(root, fn)


def vet_approved(root, rec=None) -> str | None:
    """After the engine's fetch: the record's approved commit must not
    CONTAIN a hard-blocked commit (a release built on top of a blocked one
    is not safe either). When it does, the approved commit becomes the
    newest safe release (`_target`), remembered with `from_row` so the next
    catalog read does not put the unsafe one back. Answers the commit to
    follow (None: nothing safe). Slot held by the caller."""
    rec = rec or store.get(root) or {}
    a = rec.get("approved") if isinstance(rec.get("approved"), dict) else {}
    commit = a.get("commit")
    slug = _slug(rec)
    hard = [e for e in for_slug(slug) if e["level"] == "hard"]
    if not commit or not hard:
        return commit
    if not any(e["commit"] == commit or replay.is_ancestor(root, e["commit"], commit)
               for e in hard):
        return commit
    target = _target(root, rec, slug, hard)
    if target is None:
        return None
    _set_approved(root, target, commit)
    return target["commit"]


def _set_approved(root, target, unsafe) -> None:
    """The record's approved commit := the safe `target`, remembering the
    `unsafe` row commit it replaces (`from_row`) and keeping `prev`."""
    def fn(r):
        old = r.get("approved") if isinstance(r.get("approved"), dict) else {}
        r["approved"] = {**target, "from_row": unsafe,
                         "prev": [p for p in old.get("prev") or [] if isinstance(p, dict)
                                  and p.get("commit") != target["commit"]]}
    store.update(root, fn)


# ----------------------------------------------------------------- cadence

_start_lock = threading.Lock()
_started = [False]
_last_sweep = [0.0]

#: The sweep's cadence (`refresh._sweep_loop` ticks every 10 minutes).
SWEEP_EVERY_S = 30 * 60.0


def startup() -> bool:
    """Server start: `refresh() + enforce_all()` on a daemon thread (the
    caller — server/app.py — runs it after the catalog warm-up). Idempotent."""
    with _start_lock:
        if _started[0]:
            return False
        _started[0] = True

    def run():
        try:
            refresh(force=True)
            enforce_all()
        except Exception:  # noqa: BLE001
            logger.exception("contrib blocks: startup enforcement failed")

    try:
        threading.Thread(target=run, daemon=True, name="contrib-blocks").start()
    except RuntimeError:
        return False
    return True


def sweep_tick(now: float | None = None) -> bool:
    """`refresh._sweep_loop`'s tick: every SWEEP_EVERY_S, `refresh() +
    enforce_all()`. Answers whether it ran."""
    now = time.time() if now is None else now
    if now - _last_sweep[0] < SWEEP_EVERY_S:
        return False
    _last_sweep[0] = now
    refresh(force=True)
    enforce_all()
    return True
