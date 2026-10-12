"""Uninstall an app from every list that shows it — one door, a policy per
kind (app_catalog.kind_for):

* ``own-repo`` / ``monorepo`` / ``workspace`` — the folder goes to the OS
  Trash where there is one (the undo), else a hard delete behind the
  client's second confirm. The shared `local` repo records the removal
  (fs_mutate._record_app_removal) as it always has.
* ``linked`` — the folder is outside the workspace: we never delete what we
  did not create. The registry entry goes, nothing else.
* ``appfile`` — ``forget`` drops the `.fused` from the hub and recents and
  leaves the file; ``delete_file`` trashes it too, ``delete`` removes it
  outright (the same 501 → second-confirm path as a folder).
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

# Workspace tag folders that are shelves apps sit on, never an app themselves.
_TAG_SHELVES = frozenset({"app", "local", "showcase", "bots", "sandbox"})


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
        return ("forget", "delete_file", "delete")
    if kind == app_catalog.KIND_SHOWCASE:
        return ("hide",)
    return ("trash", "delete")


def _refuse_shelf(path: str) -> JSONResponse | None:
    """The workspace root and its tag folders, by name, whether or not they
    exist on disk right now: `kind_for` would class a missing root as
    `linked` and `unlink` would then archive every task and prune every
    store row under it. A desk row CAN name a missing path (its folder was
    deleted by hand), so the existence check is not the guard here."""
    from fused_render.shell.seed import fused_dir

    ap = os.path.abspath(path)
    root = os.path.abspath(fused_dir())
    # The root and the tag shelves by name — a depth-1 folder with a page is
    # a legitimate app (app_listing lists depths 1-3), so this is a short
    # explicit list, not "every direct child".
    if os.path.normcase(ap) == os.path.normcase(root) or (
            os.path.normcase(os.path.dirname(ap)) == os.path.normcase(root)
            and os.path.basename(ap).lower() in _TAG_SHELVES):
        return _error("that is the workspace or one of its folders, not an app", status=400)
    return None


def _refuse_non_app(path: str, kind: str) -> JSONResponse | None:
    """Only an APP: a folder with a declared entry page, or a `.fused` file.
    The endpoint is reachable by any X-Fused caller and `kind_for` would
    happily class the workspace root as `linked` (prune everything) or
    `local/` as `own-repo` (trash the monorepo); "uninstall" promises app
    semantics and nothing wider. For a path that exists; `_refuse_shelf`
    runs on every request."""
    from fused_render import app_listing

    ap = os.path.abspath(path)
    if kind == app_catalog.KIND_APPFILE:
        if not (os.path.isfile(ap) and ap.lower().endswith(".fused")):
            return _error("not a .fused file", status=400)
        return None
    try:
        if not os.path.isdir(ap) or app_listing.app_entry(ap) is None:
            return _error("not an app folder (no page declares fused-app)", status=400)
    except OSError:
        return _error("cannot read that folder", status=400)
    return None


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


def _settle_copies(removed: str, dupes: list[str]) -> list[str]:
    """Repoint each duplicate whose witness names `removed` at itself. Safe
    by construction: the origin is gone, so anything recording it is a copy.
    Best-effort — a copy that cannot be settled is caught by the tombstone
    on its next open instead."""
    key = os.path.normcase(os.path.abspath(removed))
    out = []
    for d in dupes:
        try:
            rec = app_fused_dir.recorded_app_dir(d)
            if rec and os.path.normcase(os.path.abspath(rec)) == key:
                app_fused_dir._settle_as_copy(d, rec)
                out.append(d)
        except Exception:  # noqa: BLE001 — the delete already happened
            logger.exception("could not settle the copy at %s", d)
    return out


@router.get("/api/apps/uninstall/preflight")
def api_uninstall_preflight(path: str = Query(...)):
    """What uninstalling `path` would cost — the confirm dialog's numbers."""
    from fused_render.server import fs_mutate

    if not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    exists = os.path.exists(path)
    kind = app_catalog.kind_for(path)
    refused = _refuse_shelf(path) or (_refuse_non_app(path, kind) if exists else None)
    if refused is not None:
        return refused
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
    refused = _refuse_shelf(path) or (_refuse_non_app(path, kind) if exists else None)
    if refused is not None:
        return refused
    app_id = _app_id_for(path, kind) if exists else None
    folder = app_state_remove.canonical(path)
    # Read while the origin is still here: a copy's witness names it, and
    # once it is gone those copies are settled below, not on their next open.
    dupes = _dupes(path, app_id) if exists else []

    result: dict = {"ok": True, "path": folder, "kind": kind, "mode": mode,
                    "app_id": app_id, "removed_files": False}
    if mode in ("trash", "delete", "delete_file") and exists:
        trash = mode != "delete"  # "delete" is the hard delete for a folder AND a .fused
        out = fs_mutate._fs_delete({"path": path, "trash": trash, "recursive": True}, x_fused)
        if isinstance(out, JSONResponse):
            # Nothing was removed, so nothing below runs either — no
            # tombstone, no prune: a folder still on disk is still an app.
            if trash and out.status_code == 501:
                # No bin for this path: the client asked for the recoverable
                # delete and only that. Tell it so it can offer the hard
                # delete behind its own second confirm.
                return JSONResponse({"error": "trash unsupported"}, status_code=501)
            return out
        fs_mutate._note_index_mutation(out, path)
        result["removed_files"] = True
        if out.get("trashed_to"):
            result["trashed_to"] = out["trashed_to"]
    elif mode == "hide" or (mode == "forget"):
        app_catalog.hide(path)
    # After the delete, not before: a tombstone for a folder that is still
    # there would turn a later hand-move of it into a "copy" and strand its
    # sessions. The window a render of a duplicate could race is the
    # milliseconds between the rename above and this write.
    app_catalog.tombstone(path, app_id)
    # Every surviving copy whose meta.json still records THIS folder as its
    # origin is settled now, so the witness never fires for it — not on its
    # next open (which may come after the tombstone's TTL), now.
    result["settled"] = _settle_copies(path, dupes)
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
