"""Uninstall an app from every list that shows it — one door, a policy per
kind (app_catalog.kind_for):

* ``own-repo`` / ``monorepo`` / ``workspace`` — the folder goes to the OS
  Trash where there is one (the undo), else a hard delete behind the
  client's second confirm. The shared `local` repo records the removal
  (fs_mutate._record_app_removal) as it always has.
* ``linked`` — the folder is outside the workspace: we never delete what we
  did not create. The registry entry goes, nothing else.
* ``appfile`` — ``forget`` drops the `.fused` from the hub and recents and
  leaves the file; ``delete_file`` trashes it too.
* ``showcase`` — a subfolder of the showcase clone: deleting it dirties the
  repo and the next sync restores it, so the path is hidden instead.

Every mode then prunes the stores (app_state_remove), writes a tombstone so
a surviving duplicate is never mistaken for a move of this one
(app_catalog.tombstone), archives the folder's tasks (the desk's own
gesture) and drops the hub snapshot so the card is gone on the next fetch,
not after the 20 s TTL.

`preflight` is the confirm dialog's data: what the user is about to lose.
"""
from __future__ import annotations

import logging
import os
import subprocess

from fastapi import APIRouter, Body, Header, Query
from fastapi.responses import JSONResponse

from fused_render import app_catalog, app_fused_dir, app_state_remove, exported_apps
from fused_render.server.common import _error, _require_fused
from fused_render.server.routers import apps as apps_router
from fused_render.server.routers import current_apps as desk_router

logger = logging.getLogger(__name__)

router = APIRouter()

MODES = ("trash", "delete", "unlink", "forget", "delete_file", "hide")


def _default_mode(kind: str) -> str:
    if kind == app_catalog.KIND_LINKED:
        return "unlink"
    if kind == app_catalog.KIND_APPFILE:
        return "forget"
    if kind == app_catalog.KIND_SHOWCASE:
        return "hide"
    return "trash"


def _allowed_modes(kind: str) -> tuple[str, ...]:
    if kind == app_catalog.KIND_LINKED:
        return ("unlink",)
    if kind == app_catalog.KIND_APPFILE:
        return ("forget", "delete_file")
    if kind == app_catalog.KIND_SHOWCASE:
        return ("hide",)
    return ("trash", "delete")


def _dir_size(path: str) -> tuple[int, int]:
    """(bytes, files) under `path`; 0, 0 when missing. Bounded by the app's
    own data dir, which is the only thing this is asked about."""
    total = files = 0
    for dirpath, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
                files += 1
            except OSError:
                continue
    return total, files


def _uncommitted(path: str, kind: str) -> int | None:
    """Changed paths git reports for the app, or None when no repo owns it."""
    from fused_render import app_git

    scope = app_git._repo_scope(os.path.abspath(path))
    if scope is None:
        return None
    repo, spec = scope
    try:
        out = subprocess.run(
            ["git", "-C", repo, "status", "--porcelain", "--", app_git._pathspec(spec)],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def _bots_using(path: str) -> list[str]:
    from fused_render.bots import store as bots_store

    root = os.path.abspath(path)
    names = []
    for b in bots_store.builds_read():
        d = b.get("dir")
        if isinstance(d, str) and d and app_state_remove._is_under(d, root):
            names.append(str(b.get("name") or os.path.basename(d)))
    return names


def _app_id_for(path: str, kind: str) -> str | None:
    from fused_render import app_id as app_identity
    from fused_render import app_listing, appfile

    if kind == app_catalog.KIND_APPFILE:
        try:
            return appfile.app_id_of(appfile.read_manifest(path))
        except Exception:  # noqa: BLE001
            return None
    try:
        entry = app_listing.app_entry(path)
    except OSError:
        return None
    return app_identity.app_id(entry) if entry else None


def _dupes(path: str, app_id: str | None) -> list[str]:
    if not app_id:
        return []
    from fused_render import app_listing
    from fused_render.shell.seed import fused_dir

    me = os.path.normcase(os.path.realpath(path))
    out = []
    for row in app_listing.discover_workspace_apps(fused_dir()):
        if os.path.normcase(row["path"]) == me:
            continue
        if app_catalog.row_app_id(row) == app_id:
            out.append(row["path"])
    return out


@router.get("/api/apps/uninstall/preflight")
def api_uninstall_preflight(path: str = Query(...)):
    """What uninstalling `path` would cost — the confirm dialog's numbers."""
    from fused_render.server import fs_mutate

    if not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    exists = os.path.exists(path)
    kind = app_catalog.kind_for(path)
    app_id = _app_id_for(path, kind) if exists else None
    data_bytes = data_files = 0
    if exists and kind != app_catalog.KIND_APPFILE:
        data_bytes, data_files = _dir_size(app_fused_dir.data_dir(path))
    folder = app_state_remove.canonical(path)
    tasks = desk_router._folder_tasks(folder) if kind != app_catalog.KIND_APPFILE else []
    return {
        "path": folder,
        "name": os.path.splitext(os.path.basename(path))[0]
        if kind == app_catalog.KIND_APPFILE else os.path.basename(path),
        "exists": exists,
        "kind": kind,
        "app_id": app_id,
        "mode": _default_mode(kind),
        "modes": list(_allowed_modes(kind)),
        "trash_supported": fs_mutate._trash_supported(),
        "data_bytes": data_bytes,
        "data_files": data_files,
        "uncommitted": _uncommitted(path, kind) if exists and kind in (
            app_catalog.KIND_OWN_REPO, app_catalog.KIND_MONOREPO) else None,
        "tasks": len(tasks),
        "bots": _bots_using(path),
        "dupes": _dupes(path, app_id) if exists else [],
    }


@router.post("/api/apps/uninstall")
def api_uninstall(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render.server import fs_mutate

    path = body.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    kind = app_catalog.kind_for(path)
    mode = body.get("mode") or _default_mode(kind)
    if mode not in _allowed_modes(kind):
        return _error(f"mode {mode!r} is not allowed for a {kind} app")
    exists = os.path.exists(path)
    app_id = _app_id_for(path, kind) if exists else None
    folder = app_state_remove.canonical(path)

    # The tombstone goes down BEFORE the folder: a render of a duplicate
    # racing this request must already see the deletion as deliberate.
    app_catalog.tombstone(path, app_id)

    result: dict = {"ok": True, "path": folder, "kind": kind, "mode": mode,
                    "app_id": app_id, "removed_files": False}
    if mode in ("trash", "delete", "delete_file") and exists:
        trash = mode != "delete"
        out = fs_mutate._fs_delete({"path": path, "trash": trash, "recursive": True}, x_fused)
        if isinstance(out, JSONResponse):
            if trash and out.status_code == 501:
                # No bin for this path: the client asked for the recoverable
                # delete and only that. Nothing was removed; tell it so it can
                # offer the hard delete behind its own second confirm.
                return JSONResponse({"error": "trash unsupported"}, status_code=501)
            return out
        fs_mutate._note_index_mutation(out, path)
        result["removed_files"] = True
        if out.get("trashed_to"):
            result["trashed_to"] = out["trashed_to"]
    elif mode == "hide" or (mode == "forget"):
        app_catalog.hide(path)
    if kind == app_catalog.KIND_APPFILE:
        exported_apps.forget(path)
    else:
        archived = desk_router._archive_folder(folder)
        result.update(archived)
    result["pruned"] = app_state_remove.prune_stores(path)
    apps_router._reset_snapshot()
    exported_apps.reset_cache()
    logger.info("uninstalled %s (%s, %s)", folder, kind, mode)
    return result
