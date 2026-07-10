"""runPython target for annotate/template.html: mirror the review's comments
into the target file's `<file>.json` sidecar as a write-only LOG.

The URL `comments` param stays the sole LIVE store the annotate view reads back;
this sidecar is pure history — every comment ever seen for the file, keyed by
`id`, updated in place. A comment that has disappeared from the incoming array
is simply left as its last-seen state, forever: absence NEVER deletes (each URL
carries only its own review subset). Only an id named in `deleted_ids` — sent on
the SAME `record` call, so upsert and tombstone land in one atomic
read-merge-write with no cross-call ordering race — is stamped `deleted_at`.
The tombstone is PERMANENT: recording an id never clears it, so a stale
bookmarked URL that still carries a deleted comment cannot silently resurrect
it. Nothing here is read back into the view.

It is the SAME sidecar the claude chat template keeps next to each target
(templates/claude/agent.py) and the bookmark history mirror
(fused_render/shell/bookmarks.py) writes to, so every key this module does not
own (claudeSessions, bookmarkHistory, lastSession, ...) is preserved through a
read-merge-write — a later claude turn round-trips them instead of clobbering
them off disk.

Stdlib only (runs in a subprocess; cannot import fused_render.shell).

Actions:
  main(action="record", file=..., comments=[...], deleted_ids=[...])
    -> {"recorded": True, "count": N, "deleted": M}
"""
import json
import os
import tempfile
import time


# ------------------------------------------------------------- sidecar store

def _sidecar_path(file: str) -> str:
    return file + ".json"


def _load_sidecar(file: str) -> dict:
    # Preserve every key we don't own (claudeSessions, bookmarkHistory,
    # lastSession, ...) so a later claude turn / bookmark write round-trips them
    # instead of clobbering them off disk. Corrupt or absent -> empty dict.
    try:
        with open(_sidecar_path(file), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        data = {}
    # Keep agent.py's _load_sidecar guard happy so a claude turn round-trips our
    # comments log instead of dropping it (same defense as bookmarks.py).
    data.setdefault("claudeSessions", [])
    return data


def _save_sidecar(file: str, data: dict) -> None:
    path = _sidecar_path(file)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------- comments log

def _record(file: str, comments: list, deleted_ids: list) -> dict:
    """Upsert each incoming comment (keyed by its `id`) into the sidecar's
    top-level "comments" log, preserving claudeSessions/bookmarkHistory/
    lastSession and every other key — then tombstone each `deleted_ids` entry
    in the SAME write, so a delete can never race a concurrent record call.

    Write-only LOG semantics (mirrors bookmarks.py `_record_history`): an id
    already in the log is updated in place — non-None incoming fields merged,
    server `updated_at` bumped; a new id is appended with recorded_at+updated_at.
    A comment that has DISAPPEARED from the incoming array is left untouched: its
    last-seen state persists forever — absence never deletes (each URL carries
    only its own review subset, so a missing id means "not in this review").
    `deleted_ids` is the ONE signal that says "deleted on purpose": each named
    log entry gets `deleted_at` stamped (unknown ids are ignored). The stamp is
    permanent — re-recording the id merges its fields but keeps `deleted_at`,
    so a stale URL still carrying the comment can't undo the delete.

    `createdAt` is the comment's own ms-epoch (Date.now, from the template);
    `recorded_at`/`updated_at`/`deleted_at` are server `time.time()` SECONDS
    (matching agent.py's created_at/last_used and bookmarks.py's history
    stamps). Different units in one file, by design — do not "unify" them."""
    file = os.path.abspath(file)
    data = _load_sidecar(file)
    log = data.get("comments")
    if not isinstance(log, list):
        log = []
    by_id = {c["id"]: c for c in log
             if isinstance(c, dict) and isinstance(c.get("id"), str)}

    now = time.time()
    count = 0
    for c in comments:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if not isinstance(cid, str):
            continue
        # Undefined JS fields never reach JSON, so incoming keys are already the
        # comment's live fields; the None guard only mirrors bookmarks.py so a
        # sparse update can't clobber a stored value with null.
        fields = {k: v for k, v in c.items() if v is not None}
        fields.pop("deleted_at", None)  # the stamp is server-owned, never incoming
        existing = by_id.get(cid)
        if existing is not None:
            existing.update(fields)
            existing["updated_at"] = now
        else:
            entry = {**fields, "recorded_at": now, "updated_at": now}
            log.append(entry)
            by_id[cid] = entry
        count += 1

    deleted = 0
    for did in deleted_ids:
        entry = by_id.get(did) if isinstance(did, str) else None
        if entry is None:
            continue
        entry["deleted_at"] = now
        entry["updated_at"] = now
        deleted += 1

    # Nothing recorded AND nothing tombstoned is a true no-op: never touch
    # disk, so an emptied URL can't spuriously create/rewrite the sidecar and
    # the existing log stays exactly as last seen.
    if count == 0 and deleted == 0:
        return {"recorded": True, "count": 0, "deleted": 0}

    data["comments"] = log
    _save_sidecar(file, data)
    return {"recorded": True, "count": count, "deleted": deleted}


def main(action: str = "record", file: str = "", comments=None, deleted_ids=None) -> dict:
    if action == "record":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        if not isinstance(comments, list):
            comments = []
        if not isinstance(deleted_ids, list):
            deleted_ids = []
        return _record(file, comments, deleted_ids)
    return {"error": f"unknown action: {action}"}
