"""runPython target for annotate/template.html: mirror the review's comments
into the target file's `<file>.json` sidecar as a write-only LOG.

The URL `comments` param stays the sole LIVE store the annotate view reads back;
this sidecar is pure history — every comment ever seen for the file, keyed by
`id`, updated in place. A comment that has disappeared from the incoming array
is simply left as its last-seen state, forever: absence NEVER deletes (each URL
carries only its own review subset). Only an explicit `delete` action tombstones
an entry with `deleted_at`; re-recording that id clears the tombstone (it is
live again). Nothing here is read back into the view.

It is the SAME sidecar the claude chat template keeps next to each target
(templates/claude/agent.py) and the bookmark history mirror
(fused_render/shell/bookmarks.py) writes to, so every key this module does not
own (claudeSessions, bookmarkHistory, lastSession, ...) is preserved through a
read-merge-write — a later claude turn round-trips them instead of clobbering
them off disk.

Stdlib only (runs in a subprocess; cannot import fused_render.shell).

Actions:
  main(action="record", file=..., comments=[...]) -> {"recorded": True, "count": N}
  main(action="delete", file=..., id=...) -> {"deleted": True/False}
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

def _record(file: str, comments: list) -> dict:
    """Upsert each incoming comment (keyed by its `id`) into the sidecar's
    top-level "comments" log, preserving claudeSessions/bookmarkHistory/
    lastSession and every other key.

    Write-only LOG semantics (mirrors bookmarks.py `_record_history`): an id
    already in the log is updated in place — non-None incoming fields merged,
    server `updated_at` bumped; a new id is appended with recorded_at+updated_at.
    A comment that has DISAPPEARED from the incoming array is left untouched: its
    last-seen state persists forever — absence never deletes. Re-recording an id
    that was explicitly tombstoned (see `_delete`) clears its `deleted_at`: the
    comment is live again. Absence itself never stamps `deleted_at`.

    `createdAt` is the comment's own ms-epoch (Date.now, from the template);
    `recorded_at`/`updated_at` are server `time.time()` SECONDS (matching
    agent.py's created_at/last_used and bookmarks.py's history stamps).
    Different units in one file, by design — do not "unify" them."""
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
        existing = by_id.get(cid)
        if existing is not None:
            existing.update(fields)
            existing.pop("deleted_at", None)  # re-recording an id makes it live again
            existing["updated_at"] = now
        else:
            entry = {**fields, "recorded_at": now, "updated_at": now}
            log.append(entry)
            by_id[cid] = entry
        count += 1

    # Nothing to record (empty or all-invalid array) is a true no-op: never
    # touch disk, so an emptied URL can't spuriously create/rewrite the sidecar
    # and the existing log stays exactly as last seen.
    if count == 0:
        return {"recorded": True, "count": 0}

    data["comments"] = log
    _save_sidecar(file, data)
    return {"recorded": True, "count": count}


def _delete(file: str, cid: str) -> dict:
    """Tombstone ONE comment: stamp `deleted_at` on the sidecar `comments` log
    entry with this `id` and bump `updated_at`, leaving every other key intact.

    This is the ONLY path that marks a comment deleted — absence from a `record`
    array never does (each URL carries only its own review subset, so a missing
    id means "not in this review", not "deleted"). A missing file or unknown id
    is a graceful no-op ({"deleted": False}), never an exception; `deleted_at` is
    server `time.time()` SECONDS, matching recorded_at/updated_at."""
    file = os.path.abspath(file)
    data = _load_sidecar(file)
    log = data.get("comments")
    if not isinstance(log, list):
        return {"deleted": False}
    entry = next((c for c in log
                  if isinstance(c, dict) and c.get("id") == cid), None)
    if entry is None:
        return {"deleted": False}
    now = time.time()
    entry["deleted_at"] = now
    entry["updated_at"] = now
    data["comments"] = log
    _save_sidecar(file, data)
    return {"deleted": True}


def main(action: str = "record", file: str = "", comments=None, id: str = "") -> dict:
    if action == "record":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        if not isinstance(comments, list):
            comments = []
        return _record(file, comments)
    if action == "delete":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        if not isinstance(id, str) or not id:
            return {"deleted": False}
        return _delete(file, id)
    return {"error": f"unknown action: {action}"}
