"""Current apps — the sidebar's desk (fused_render/current_apps.py) over HTTP.

* ``GET /api/current-apps`` — the table, in added order, each row with its
  folder, name, kind (workspace / linked), entry page and whether the folder is
  still there.
* ``DELETE /api/current-apps?path=`` — take an app off the desk AND archive
  every task whose project is that folder or inside it. This is the one
  direction the desk and the tasks are still coupled (desk → tasks); the
  reverse — archiving tasks removing the app — is exactly what the redesign
  ended, so nothing here runs on an archive.

The row's right-click menu adds three more verbs, all folder-scoped:

* ``POST /api/current-apps/rename`` — rename the FOLDER on disk and settle the
  move the way an out-of-band move is settled (D548): stores repointed, Claude
  sessions carried along, `.fused/meta.json` repointed.
* ``POST /api/current-apps/read`` — mark every message of every task under the
  folder read, in one pass.
* ``POST /api/current-apps/archive`` — the DELETE's task half without its desk
  half: archive every task under the folder, keep the row.

There is no POST that adds a row: the table is fed by the tasks listing
(`current_apps.observe`, run inside `tasks._task_rows`) and by nothing else. An
app arrives on the desk by having work started in it.
"""
import os
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from fused_render import current_apps, tasks_store, tasks_watch
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server.routers import tasks as tasks_router

router = APIRouter()


@router.get("/api/current-apps")
def api_current_apps():
    return {"apps": current_apps.list_apps()}


def _require_folder(path: str) -> str:
    folder = canonical_fs_path(path).rstrip("/")
    if not folder:
        raise HTTPException(status_code=400, detail="missing path")
    return folder


def _folder_tasks(folder: str) -> list[dict]:
    """Every collected task whose project is `folder` or inside it — the same
    one-pass collect + `_place` the DELETE has always run, shared now by the
    read-all and archive-all verbs."""
    matched = []
    for task in tasks_router._collect().values():
        tasks_router._place(task)
        project = canonical_fs_path(task.get("project") or "")
        if project and current_apps.is_under(project, folder):
            matched.append(task)
    return matched


def _archive_folder(folder: str) -> dict:
    """Archive every task under `folder` — the gesture the DELETE has always
    run, and the whole of the archive-all verb. `_collect` is one pass over
    the transcripts and the schedule, and each task's project is what `_place`
    resolves for the listing — the same folder rule the observer used to put
    the app on the desk, so what gets archived is what the app's Tasks tab
    showed."""
    archived = 0
    cancelled = 0
    keys = set()
    for task in _folder_tasks(folder):
        n, filed = tasks_router.archive_task(task)
        cancelled += n
        if filed or n:
            archived += 1
            keys.add(task["key"])
    if keys:
        tasks_watch.notify(keys)
    return {"archived": archived, "cancelled": cancelled}


@router.delete("/api/current-apps")
def api_current_apps_remove(path: str = Query(...)):
    folder = _require_folder(path)
    removed = current_apps.remove(folder)
    result = _archive_folder(folder)
    return {"ok": True, "removed": removed, **result}


class FolderPatch(BaseModel):
    path: str


@router.post("/api/current-apps/archive")
def api_current_apps_archive(patch: FolderPatch):
    """The cross's task half without its desk half: archive every task under
    the folder, keep the row."""
    folder = _require_folder(patch.path)
    return {"ok": True, **_archive_folder(folder)}


@router.post("/api/current-apps/read")
def api_current_apps_read(patch: FolderPatch):
    """Mark every message of every task under the folder read — the row's
    green dot, cleared as one gesture. The per-task body is
    `tasks._read_whole_task`'s, inlined over ONE collect rather than one per
    task: ids come from the messages unread right now, so nothing pending is
    swept in."""
    folder = _require_folder(patch.path)
    now = time.time()
    marked = 0
    keys = set()
    for task in _folder_tasks(folder):
        messages = tasks_router._thread(task, tasks_store.read_state(), now)
        unread_ids = [m["message_id"] for m in messages if m["unread"]]
        if not unread_ids:
            continue
        tasks_store.mark_read_many(task["key"], unread_ids)
        marked += len(unread_ids)
        keys.add(task["key"])
    if keys:
        tasks_watch.notify(keys)
    return {"ok": True, "marked": marked, "tasks": len(keys)}


class RenamePatch(BaseModel):
    path: str
    name: str


@router.post("/api/current-apps/rename")
def api_current_apps_rename(patch: RenamePatch):
    """Rename the app's FOLDER on disk, then settle the move exactly as an
    out-of-band move is settled (D548, app_fused_dir): the server's stores are
    repointed (the desk row included) and the folder's Claude sessions are
    carried to the new path. When the folder carries a `.fused/meta.json`
    witnessing the old path, `ensure` runs the whole settlement itself;
    otherwise the two halves are run directly. A live session in the folder is
    left pending, retried on the next open — rename never blocks on it."""
    folder = _require_folder(patch.path)
    name = patch.name.strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="invalid name")
    if not os.path.isdir(folder):
        raise HTTPException(status_code=404, detail="no such folder")
    state = current_apps.read_state()
    if not any(a["path"] == folder for a in state["apps"]):
        raise HTTPException(status_code=404, detail="not a current app")
    new = canonical_fs_path(
        os.path.join(os.path.dirname(folder), name)).rstrip("/")
    if new == folder:
        return {"ok": True, "path": folder}
    # Case-only renames (foo -> Foo) pass the exists check on a
    # case-insensitive filesystem; os.rename handles them fine.
    if os.path.normcase(new) != os.path.normcase(folder) and os.path.exists(new):
        raise HTTPException(status_code=409, detail="a folder with that name "
                            "already exists")
    try:
        os.rename(folder, new)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"rename failed: {e}")
    from fused_render import app_fused_dir, app_state_move, claude_session_move

    recorded = app_fused_dir.recorded_app_dir(new)
    if recorded and canonical_fs_path(
            os.path.abspath(recorded)).rstrip("/") == folder:
        # The witness fired: ensure settles the move — stores, sessions, meta.
        app_fused_dir.ensure(new)
    else:
        app_state_move.rewrite_stores(folder, new)
        claude_session_move.relocate(folder, new)
        app_fused_dir.ensure(new)
    return {"ok": True, "path": new}
