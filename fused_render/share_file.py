"""Share any file whose extension resolves to a Fused catalog file-preview
UDF, generalising `share_app.py`'s app-only pipeline.

Reuses share_app.py's machinery wholesale rather than duplicating it: the
same record store (`shared_apps.json`, keys namespaced `file:<id>` against
the app shares' bare ids — one file, one writer, one lock), the same
`_app_lock` (keyed on path, already generic), the same `_run_shim`/
`_shim_command` plumbing that spawns `_fused_share_app.py`, the same
`_require_fused`/`_logged_in`/`_not_signed_in` sign-in guards.

Identity is `<slug>_<h6>`: `slug` is the slugified filename stem, `h6` is
`sha1(abs path)[:6]`, so re-sharing the same path updates one canvas and two
same-named files in different folders get different canvases.

Viewer resolution (`share_file_rules.py`) needs the real SDK, which the
server process does not have — `resolve_viewer` reads whatever rule table is
already cached on disk (warm in the common case: `publish` always leaves a
fresh one behind) and falls back to the built-in rules alone when there is
none yet, rather than blocking a status call on a subprocess spawn.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render import share_app
from fused_render import share_file_rules
from fused_render.fusedcli import fused_cli

logger = logging.getLogger(__name__)

router = APIRouter()

PUBLISH_TIMEOUT = share_app.PUBLISH_TIMEOUT
LOOKUP_TIMEOUT = share_app.LOOKUP_TIMEOUT
REMOVE_TIMEOUT = share_app.REMOVE_TIMEOUT
RULES_TIMEOUT = 45.0

_error = share_app._error
_not_signed_in = share_app._not_signed_in
_require_fused = share_app._require_fused
_logged_in = share_app._logged_in
_creds_stamp = share_app._creds_stamp
get_record = share_app.get_record
put_record = share_app.put_record
drop_record = share_app.drop_record


# -- identity --------------------------------------------------------------


def file_identity(path: str) -> str:
    """`<slug>_<h6>` — stable for a given absolute path, distinct for two
    same-named files in different folders."""
    stem = os.path.splitext(os.path.basename(path))[0]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").lower()[:40] or "file"
    h6 = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:6]
    return f"{slug}_{h6}"


def _record_key(file_id: str) -> str:
    return f"file:{file_id}"


def _record_for(file_id: str) -> dict | None:
    return get_record(_record_key(file_id))


# -- viewer resolution -------------------------------------------------------


def _cached_rules() -> list[dict]:
    """Disk-only: read whatever rule table the cache already holds (however
    stale) and never triggers the SDK subprocess a rebuild needs. A missing
    cache degrades to the built-in rules alone (so `.fused` still resolves)
    rather than blocking `status` on a shim spawn."""
    rules, _built_at = share_file_rules._read_cache()
    return share_file_rules._with_builtin(rules or [])


def resolve_viewer(path: str) -> dict | None:
    return share_file_rules.resolve(path, _cached_rules())


def _refusal_for(path: str, rule: dict | None) -> str | None:
    if rule is not None:
        return None
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext:
        return f"no Fused viewer opens .{ext} files"
    return "no Fused viewer opens files with no extension"


def _resolve_path(path: str) -> tuple[str | None, str | None]:
    """(abs path, refusal). A directory is refused the same as a missing one
    — Share is a file action, not a folder one (§7's `.fused` app stays on
    share_app.py's own route)."""
    if not path or not os.path.isabs(path):
        return None, "path must be an absolute file path"
    if not os.path.isfile(path):
        return None, f"no such file: {path}"
    return path, None


# -- routes --------------------------------------------------------------------


@router.get("/api/share/file/status")
def api_share_file_status(path: str = ""):
    """Disk only, and read-only, like share_app.py's status: the resolved
    viewer (or the refusal naming why not), whether Fused is signed in, and
    the local share record if there is one."""
    abspath, path_refusal = _resolve_path(path)
    if path_refusal is not None:
        return {
            "file_id": None,
            "can_share": False,
            "refusal": path_refusal,
            "viewer": None,
            "cli_found": fused_cli() is not None,
            "logged_in": _logged_in(),
            "creds_stamp": _creds_stamp(),
            "shared": None,
        }
    rule = resolve_viewer(abspath)
    refusal = _refusal_for(abspath, rule)
    file_id = file_identity(abspath)
    return {
        "file_id": file_id,
        "can_share": refusal is None,
        "refusal": refusal,
        "viewer": rule.get("name") if rule else None,
        "cli_found": fused_cli() is not None,
        "logged_in": _logged_in(),
        "creds_stamp": _creds_stamp(),
        "shared": _record_for(file_id),
    }


@router.post("/api/share/file/publish")
def api_share_file_publish(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Publish (or update) a public share of the file. A plain `def`, for the
    same reason share_app.py's publish is: the shim blocks for the whole
    upload + canvas push, and an `async def` would hold the event loop."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path") if isinstance(body.get("path"), str) else ""
    abspath, path_refusal = _resolve_path(path)
    if path_refusal is not None:
        return _error(path_refusal)
    rule = resolve_viewer(abspath)
    refusal = _refusal_for(abspath, rule)
    if refusal is not None:
        return _error(refusal)
    if not _logged_in():
        return _not_signed_in()

    lock = share_app._app_lock(abspath)
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "this file is already being shared", "code": "busy"},
                            status_code=409)
    try:
        file_id = file_identity(abspath)
        key = _record_key(file_id)
        name = os.path.basename(abspath)
        previous = get_record(key) or {}
        out, err = share_app._run_shim(
            {"action": "publish", "share_id": file_id, "viewer_token": rule["token"],
             "name": name, "file": abspath, "previous_remote": previous.get("remote")},
            PUBLISH_TIMEOUT)
        if err is not None:
            return err
        now = time.time()
        record = {
            "file_id": file_id,
            "path": abspath,
            "name": name,
            "viewer": rule["name"],
            "url": out.get("url"),
            "canvas_id": out.get("canvas_id"),
            "canvas_name": out.get("canvas_name"),
            "share_token": out.get("share_token"),
            "slug": out.get("slug"),
            "remote": out.get("remote"),
            "workbench_url": out.get("workbench_url"),
            "shared_at": previous.get("shared_at") or now,
            "updated_at": now,
        }
        put_record(key, record)
        return {"ok": True, "shared": record}
    finally:
        lock.release()


@router.post("/api/share/file/lookup")
def api_share_file_lookup(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Ask Fused whether a canvas for this file already exists — the case
    with no local record (shared from another machine)."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path") if isinstance(body.get("path"), str) else ""
    abspath, path_refusal = _resolve_path(path)
    if path_refusal is not None:
        return _error(path_refusal)
    if not _logged_in():
        return _not_signed_in()
    file_id = file_identity(abspath)
    key = _record_key(file_id)
    name = os.path.basename(abspath)
    out, err = share_app._run_shim({"action": "lookup", "share_id": file_id, "name": name},
                                   LOOKUP_TIMEOUT)
    if err is not None:
        return err
    if not out.get("found"):
        return {"found": False, "file_id": file_id}
    now = time.time()
    existing = get_record(key) or {}
    record = {
        **existing,
        "file_id": file_id,
        "path": abspath,
        "name": name,
        "url": out.get("url") or existing.get("url"),
        "canvas_id": out.get("canvas_id"),
        "canvas_name": out.get("canvas_name"),
        "share_token": out.get("share_token"),
        "slug": out.get("slug") or existing.get("slug"),
        "workbench_url": out.get("workbench_url") or existing.get("workbench_url"),
        "shared_at": existing.get("shared_at") or now,
        "updated_at": existing.get("updated_at") or now,
        "adopted": True,
    }
    put_record(key, record)
    return {"found": True, "file_id": file_id, "shared": record}


@router.post("/api/share/file/remove")
def api_share_file_remove(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Delete the canvas and the uploaded copies — Stop sharing."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path") if isinstance(body.get("path"), str) else ""
    abspath, path_refusal = _resolve_path(path)
    if path_refusal is not None:
        return _error(path_refusal)
    if not _logged_in():
        return _not_signed_in()
    file_id = file_identity(abspath)
    key = _record_key(file_id)
    lock = share_app._app_lock(abspath)
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "this file is already being shared", "code": "busy"},
                            status_code=409)
    try:
        rec = get_record(key) or {}
        out, err = share_app._run_shim(
            {"action": "remove", "share_id": file_id, "canvas_id": rec.get("canvas_id")},
            REMOVE_TIMEOUT)
        if err is not None:
            return err
        drop_record(key)
        return {"ok": True, "deleted_canvas": bool(out.get("deleted_canvas"))}
    finally:
        lock.release()
