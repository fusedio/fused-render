"""Current apps — the sidebar's desk (fused_render/current_apps.py) over HTTP.

* ``GET /api/current-apps`` — the table, in added order, each row with its
  folder, name, kind (workspace / linked), entry page and whether the folder is
  still there.
* ``DELETE /api/current-apps?path=`` — take an app off the desk AND archive
  every task whose project is that folder or inside it. This is the one
  direction the desk and the tasks are still coupled (desk → tasks); the
  reverse — archiving tasks removing the app — is exactly what the redesign
  ended, so nothing here runs on an archive.

There is no POST: the table is fed by the tasks listing (`current_apps.observe`,
run inside `tasks._task_rows`) and by nothing else. An app arrives on the desk
by having work started in it.
"""
from fastapi import APIRouter, HTTPException, Query

from fused_render import current_apps
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server.routers import tasks as tasks_router

router = APIRouter()


@router.get("/api/current-apps")
def api_current_apps():
    return {"apps": current_apps.list_apps()}


@router.delete("/api/current-apps")
def api_current_apps_remove(path: str = Query(...)):
    folder = canonical_fs_path(path).rstrip("/")
    if not folder:
        raise HTTPException(status_code=400, detail="missing path")
    removed = current_apps.remove(folder)
    # Archive FIRST by collecting, then act: `_collect` is one pass over the
    # transcripts and the schedule, and each task's project is what `_place`
    # resolves for the listing — the same folder rule the observer used to
    # put the app on the desk, so what the cross archives is what the app's
    # Tasks tab showed.
    archived = 0
    cancelled = 0
    tasks = tasks_router._collect()
    for task in tasks.values():
        tasks_router._place(task)
        project = canonical_fs_path(task.get("project") or "")
        if not project or not current_apps.is_under(project, folder):
            continue
        n, filed = tasks_router.archive_task(task)
        cancelled += n
        if filed or n:
            archived += 1
    return {"ok": True, "removed": removed, "archived": archived,
            "cancelled": cancelled}
