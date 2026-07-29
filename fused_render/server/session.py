import os
import time
from urllib.parse import parse_qsl

from fused_render.shell import storage

import fused_render.server as _srv


# Per-file sidecar <file>.json (shared with the claude chat template, which
# owns "claudeSessions", and bookmarks, which own "bookmarkHistory" — see
# templates/claude/agent.py and shell/bookmarks.py). Read/merge/write preserves
# every other key so the writers never clobber each other (single local user,
# last-write-wins on a true interleave — D3).
def _sidecar_path(file: str) -> str:
    return file + ".json"


def _read_sidecar(file: str) -> dict:
    # read_json returns None on missing/corrupt; a non-dict (a stray JSON list)
    # is treated as empty so a merge can't crash.
    data = storage.read_json(_sidecar_path(file))
    return data if isinstance(data, dict) else {}


def _has_non_mode_param(search: str) -> bool:
    # A "qualifying" query has at least one key other than _mode (mirrors the
    # frontend hasQualifyingParam). keep_blank_values so "?city=" still counts.
    return any(k != "_mode" for k, _ in parse_qsl(search, keep_blank_values=True))


def _is_file_mount_safe(path: str) -> bool:
    """os.path.isfile, but NEVER a kernel stat on a mount-backed path — a cold
    os.path.isfile there is the GETATTR that lists the whole parent prefix and
    wedges the mount (the /api/session + /api/recents open-flow wedge). Mount
    paths answered via rc_kind_for; only a confirmed "file" passes (a "dir" is
    not a file, matching os.path.isfile), while an "indeterminate" rc probe
    fails OPEN so a transient rcd hiccup never 404s a file the user just
    opened."""
    from fused_render.shell import pathops
    return pathops.is_file(path)


def _session_get(path: str):
    if not _is_file_mount_safe(path):
        return _srv._error(f"no such file: {path}", status=404)
    last = _read_sidecar(path).get("lastSession")
    return {"lastSession": last if isinstance(last, dict) else None}


def _session_put(body: dict, x_fused: str | None):
    guard = _srv._require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path")
    search = body.get("search")
    if not path or not os.path.isabs(path):
        return _srv._error("'path' must be an absolute filesystem path")
    if not _is_file_mount_safe(path):
        return _srv._error(f"no such file: {path}", status=404)
    if not isinstance(search, str):
        return _srv._error("'search' must be a string")
    # A file browsed inside a read-only remote mount can never take the
    # sidecar write: with CacheMode=full the doomed PutObject lands in the VFS
    # cache and 403-loops forever (the sidecar-write incident). Skip before
    # even reading the sidecar (that read is a network stat too) — reopening
    # the file just restores the default view. Same "skipped" shape as the
    # LSN-3 gate below.
    from fused_render.shell.mounts import mount_read_only
    if mount_read_only(path):
        return {"ok": True, "skipped": True}
    # Read-merge-write the whole dict so claudeSessions / bookmarkHistory
    # survive alongside lastSession (see _read_sidecar comment).
    data = _read_sidecar(path)
    # LSN-3 gate (authoritative, server-side): a _mode-only or empty query must
    # not START a session, but once one exists we DO record _mode-only updates
    # so the file's last _mode is remembered. Save when the query carries a
    # non-_mode param, OR (query is non-empty AND a lastSession already exists).
    # Empty query never clobbers an existing session down to "".
    has_session = isinstance(data.get("lastSession"), dict)
    if not (_has_non_mode_param(search) or (search != "" and has_session)):
        return {"ok": True, "skipped": True}
    data["lastSession"] = {"search": search, "updated_at": time.time()}
    try:
        storage.write_json(_sidecar_path(path), data)
    except OSError as e:
        return _srv._error(f"cannot write sidecar for {path}: {e}", status=400)
    return {"ok": True}
