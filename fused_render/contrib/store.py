"""The contribution loop's record store (docs/CONTRIB_LOOP.md §3).

ONE FILE, OUTSIDE EVERY APP: `<home>/contrib/apps.json`,
`{"version": 1, "apps": {<key>: Record}}`. What a copy follows, who owns it
and the proposal it has open are facts about THIS MACHINE'S copy, decided by
fused — never by a file in the repo. The repo is the author's to write (a
committed `.fused/` included), and anything that read its own install source
out of the folder would let the app (or the author) re-point where Bob's
changes go. Nothing here reads the app folder beyond `os.path.isdir`.

KEYED BY FOLDER, realpath'd. Two copies of one repo on one machine are two
records (`find_by_repo` lists both), and every caller — git_upstream's repo
roots (already realpath'd), the routers' workspace spellings, the
move/remove hooks — lands on the same key whichever spelling it holds
(macOS's `/var` → `/private/var`, a symlinked `~/Fused`).

SELF-HEALING, NOT SELF-DELETING. `get` on a key whose folder is gone answers
None, but the row stays: a Finder move leaves the record at the old key until
`classify.ensure` at the new folder adopts it (same repo, old folder gone),
which is how a proposal survives a move nobody told the server about.
`app_state_move.rewrite_stores` (`move`) and `app_state_remove.prune_stores`
(`prune`) handle the moves and deletes the server does see.

One module-level lock around every read-modify-write; atomic writes via
`storage.write_json`. Every function swallows I/O errors (None/False/0): a
broken store must never break a render, an install or a sync — the engine
reads a missing record as "not mine" and today's behaviour runs.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time

from fused_render.shell import storage

logger = logging.getLogger(__name__)

VERSION = 1

#: Newest last; the page says each once. Ten is far more than one copy
#: accumulates between two visits.
EVENTS_KEEP = 10
#: The record's list of backup refs (the refs themselves stay in the repo —
#: see replay.land); ten names the recent ones for a restore UI.
BACKUPS_KEEP = 10

EVENT_KINDS = frozenset({
    "updated", "updated-kept", "merged-back", "declined", "merge-accepted",
    "discarded", "remixed", "original-brought-in",
})

#: The kinds the app page draws as a one-time banner (`engine.status`'s
#: `event`). Every other kind is a user action the page already confirmed,
#: or a state the card draws itself: stored for the record, but born seen,
#: so it can never hide a newer banner-worthy one.
RENDERED_EVENTS = frozenset({"updated-kept", "merged-back"})

#: An orphaned row (its folder gone and no copy adopted it) is dropped once
#: this long has passed since a sweep first found it so.
ORPHAN_TTL_S = 30 * 24 * 3600

_lock = threading.Lock()


def path() -> str:
    return os.path.join(storage.home_dir(), "contrib", "apps.json")


def key(app_dir: str) -> str:
    """The record key for `app_dir`: absolute, realpath'd, no trailing slash
    (`app_state_remove.canonical` of the realpath)."""
    from fused_render.app_state_remove import canonical

    return canonical(os.path.realpath(os.path.abspath(app_dir)))


def _is_under(k: str, root_key: str) -> bool:
    return k == root_key or k.startswith(root_key.rstrip("/") + "/")


def _read() -> dict:
    data = storage.read_json(path())
    if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
        return {"version": VERSION, "apps": {}}
    return data


def _write(data: dict) -> bool:
    try:
        storage.write_json(path(), data)
        return True
    except Exception:  # noqa: BLE001 — never raise on I/O (module docstring)
        logger.exception("contrib store: could not write %s", path())
        return False


def new_record(kind: str, *, repo: str | None = None, branch: str | None = None,
               classified_by: str = "migration", owner_checked: bool = True) -> dict:
    """A fresh Record with every §3 field present (callers never have to
    `.get` a field into existence)."""
    return {
        "kind": kind,
        "repo": repo,
        "branch": branch,
        "classified_by": classified_by,
        "classified_at": time.time(),
        "owner_checked": owner_checked,
        "remix_of": None,
        "proposal": None,
        "events": [],
        "needs_merge": None,
        "original_update": None,
        "backups": [],
    }


# ----------------------------------------------------------------------- reads


def get(app_dir: str) -> dict | None:
    """The record for `app_dir`, or None — none recorded, the store
    unreadable, or the folder no longer exists (the row is kept for
    adoption; module docstring). A copy: mutating it changes nothing."""
    try:
        k = key(app_dir)
        if not os.path.isdir(k):
            return None
        with _lock:
            rec = _read()["apps"].get(k)
        return copy.deepcopy(rec) if isinstance(rec, dict) else None
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: get failed for %s", app_dir)
        return None


def get_raw(k: str) -> dict | None:
    """The record stored under the exact key `k`, folder present or not —
    for adoption (`classify.ensure`), which needs the orphaned row `get`
    hides."""
    try:
        with _lock:
            rec = _read()["apps"].get(k)
        return copy.deepcopy(rec) if isinstance(rec, dict) else None
    except Exception:  # noqa: BLE001
        return None


def all() -> dict:  # noqa: A001 — the contract's name
    """Every record, `{key: Record}`, including orphans. A copy."""
    try:
        with _lock:
            return copy.deepcopy(_read()["apps"])
    except Exception:  # noqa: BLE001
        return {}


def find_by_repo(repo: str) -> list:
    """`[(key, Record)]` for every record naming `repo` (case-insensitive —
    GitHub's own rule), folder present or not; newest classification first."""
    want = (repo or "").lower()
    if not want:
        return []
    out = [(k, r) for k, r in all().items()
           if isinstance(r, dict) and isinstance(r.get("repo"), str)
           and r["repo"].lower() == want]
    out.sort(key=lambda kr: kr[1].get("classified_at") or 0, reverse=True)
    return out


# ---------------------------------------------------------------------- writes


def put(app_dir: str, record: dict) -> bool:
    try:
        k = key(app_dir)
        with _lock:
            data = _read()
            data["apps"][k] = copy.deepcopy(record)
            return _write(data)
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: put failed for %s", app_dir)
        return False


def create(app_dir: str, record: dict) -> bool:
    """`put` only when `app_dir` has no record yet — two classifications
    racing (a router GET and the engine's background one) keep the first.
    False when one existed (or the write failed)."""
    try:
        k = key(app_dir)
        with _lock:
            data = _read()
            if isinstance(data["apps"].get(k), dict):
                return False
            data["apps"][k] = copy.deepcopy(record)
            return _write(data)
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: create failed for %s", app_dir)
        return False


def update(app_dir: str, fn) -> dict | None:
    """Apply `fn(record)` (mutating a copy in place) under the lock and
    persist; answers the new record, or None when there is no record (or the
    write failed). `fn` must not do I/O — it runs under the store lock."""
    try:
        k = key(app_dir)
        with _lock:
            data = _read()
            rec = data["apps"].get(k)
            if not isinstance(rec, dict):
                return None
            rec = copy.deepcopy(rec)
            fn(rec)
            data["apps"][k] = rec
            if not _write(data):
                return None
            return copy.deepcopy(rec)
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: update failed for %s", app_dir)
        return None


def drop(app_dir: str) -> bool:
    try:
        k = key(app_dir)
        with _lock:
            data = _read()
            if data["apps"].pop(k, None) is None:
                return False
            return _write(data)
    except Exception:  # noqa: BLE001
        return False


def drop_key(k: str) -> bool:
    try:
        with _lock:
            data = _read()
            if data["apps"].pop(k, None) is None:
                return False
            return _write(data)
    except Exception:  # noqa: BLE001
        return False


def adopt(old_key: str, app_dir: str) -> dict | None:
    """Re-key the orphaned row at `old_key` onto `app_dir` (a Finder move the
    server never saw), atomically: answers the adopted record, or None when
    `old_key` vanished meanwhile or `app_dir` already has a record."""
    try:
        new_key = key(app_dir)
        with _lock:
            data = _read()
            rec = data["apps"].get(old_key)
            if not isinstance(rec, dict) or new_key in data["apps"]:
                return None
            del data["apps"][old_key]
            rec.pop("orphaned_at", None)
            data["apps"][new_key] = rec
            if not _write(data):
                return None
            return copy.deepcopy(rec)
    except Exception:  # noqa: BLE001
        return None


def move(old_root: str, new_root: str) -> int:
    """Re-key every record under `old_root` onto `new_root` (an app folder,
    or a folder holding several, moved — `app_state_move.rewrite_stores`).

    Two guards beyond the prefix remap, because the move hook is driven by
    `.fused/meta.json` — a file a repo can commit (§0.5): a row whose
    destination already holds a record is left where it is (an install that
    just classified the folder must not be overwritten by a remap a committed
    `meta.json` named), and nothing moves while `old_root` still exists (a
    copy, not a move — `app_fused_dir._after_move` makes the same test).
    Answers how many rows moved. Idempotent."""
    try:
        old_k, new_k = key(old_root), key(new_root)
        if old_k == new_k or os.path.exists(old_k):
            return 0
        moved = 0
        with _lock:
            data = _read()
            apps = data["apps"]
            for k in [k for k in apps if _is_under(k, old_k)]:
                dest = new_k + k[len(old_k):]
                if dest in apps:
                    continue
                apps[dest] = apps.pop(k)
                moved += 1
            if moved and not _write(data):
                return 0
        return moved
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: move %s -> %s failed", old_root, new_root)
        return 0


def prune(root: str) -> int:
    """Drop every record under `root` (the app was removed). Answers how many."""
    try:
        root_k = key(root)
        with _lock:
            data = _read()
            gone = [k for k in data["apps"] if _is_under(k, root_k)]
            for k in gone:
                del data["apps"][k]
            if gone and not _write(data):
                return 0
        return len(gone)
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: prune failed for %s", root)
        return 0


def sweep_orphans(now: float | None = None) -> int:
    """Housekeeping (the engine's hourly pass): stamp `orphaned_at` on a row
    whose folder is gone, clear it on one whose folder is back, and drop a
    row orphaned for longer than ORPHAN_TTL_S — a copy deleted outside the
    server, or moved somewhere `classify.ensure` never ran. Answers how many
    rows were dropped."""
    now = time.time() if now is None else now
    try:
        with _lock:
            data = _read()
            apps = data["apps"]
            changed = dropped = 0
            for k in list(apps):
                rec = apps[k]
                if not isinstance(rec, dict):
                    continue
                if os.path.isdir(k):
                    if rec.pop("orphaned_at", None) is not None:
                        changed += 1
                elif not rec.get("orphaned_at"):
                    rec["orphaned_at"] = now
                    changed += 1
                elif now - rec["orphaned_at"] > ORPHAN_TTL_S:
                    del apps[k]
                    dropped += 1
            if (changed or dropped) and not _write(data):
                return 0
        return dropped
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: orphan sweep failed")
        return 0


# ------------------------------------------------------------ events / backups


def add_event(app_dir: str, kind: str, who: str | None = None) -> dict | None:
    """Append one Event (newest last, capped at EVENTS_KEEP). `seen` starts
    False; `ack(app_dir, "event")` marks every event seen."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown contrib event kind {kind!r}")
    event = {"kind": kind, "at": time.time(), "who": who,
             "seen": kind not in RENDERED_EVENTS}

    def fn(rec):
        events = rec.get("events") if isinstance(rec.get("events"), list) else []
        events.append(event)
        rec["events"] = events[-EVENTS_KEEP:]

    return update(app_dir, fn)


def ack(app_dir: str, kind: str) -> dict | None:
    """The page acknowledged a banner: `"event"` marks every event seen;
    `"settled"` marks a merged/declined proposal's banner seen
    (`proposal.settled_seen`)."""
    def fn(rec):
        if kind == "event":
            for e in rec.get("events") or []:
                if isinstance(e, dict):
                    e["seen"] = True
        elif kind == "settled":
            if isinstance(rec.get("proposal"), dict):
                rec["proposal"]["settled_seen"] = True
        else:
            raise ValueError(f"unknown ack kind {kind!r}")

    return update(app_dir, fn)


def add_backup(app_dir: str, ref: str, why: str) -> dict | None:
    """Name one backup ref on the record (capped at BACKUPS_KEEP; the ref
    itself is never deleted by the engine)."""
    entry = {"ref": ref, "at": time.time(), "why": why}

    def fn(rec):
        backups = rec.get("backups") if isinstance(rec.get("backups"), list) else []
        backups.append(entry)
        rec["backups"] = backups[-BACKUPS_KEEP:]

    return update(app_dir, fn)
