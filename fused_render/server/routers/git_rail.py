"""THE CLAUDE RAIL'S GIT SECTION (frontend/src/shell/claude-rail/ClaudeRail.tsx
``git``): what a page's collapsed rail says about the repository its folder
or file sits in — the branch, how far it stands from the remote's default
branch, and the last three commits that touched the folder (or the file).

Two routes, both for ANY absolute path, not only an app folder (the explorer's
folder and file views draw the rail too):

  GET  /api/git/rail?path=<abs>   -> {"ok": true, "repo": null | {...}}
  POST /api/git/rail/check {path} -> the same, after git_upstream's own
                                     user-triggered check (``force_check``:
                                     its process-wide fetch slot, its engine-
                                     owned-copy guard, its bounded wait —
                                     X-Fused; the rail's "Check for updates"
                                     seat). ``checked`` says whether a fresh
                                     fetch landed inside that wait.

``repo`` is null when no ancestor carries a ``.git`` (a canvas clone, a plain
folder): the rail then draws no Git section at all. Otherwise:

  root            the repository root (realpath)
  branch          ``symbolic-ref --short HEAD``, null on a detached HEAD
  has_remote      an ``origin`` remote is configured
  default_branch  the remote's default branch off the clone's recorded symref
                  — on the GET read with no ``remote set-head --auto`` re-ask
                  (a network call, on a read the rail makes on every page it
                  stands on); the POST may ask, so a clone made by hand
                  (``git init`` + ``remote add`` + push) learns it on its
                  first Check
  ahead, behind   HEAD against ``origin/<default_branch>`` as last fetched
                  (git_upstream ``_ahead_behind_counts``); null with no remote
  commits         ``git log -n3`` scoped to the path (git_snapshot ``_run_log``)

Pure functions over the git helpers the two neighbouring routers already
hold, so a test runs them against a real repository with no TestClient
(tests/test_git_rail.py), the way test_git_commits.py does."""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Header
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from fused_render import git_upstream as gu
from fused_render.server.common import _error, _require_fused
from fused_render.server.routers import git_snapshot as gs

router = APIRouter()

# The rail has room for three seats under the branch (claude-rail.css).
COMMITS = 3


def _has_remote(root: str) -> bool:
    return gu._ok(gu._run_safe(root, "remote", "get-url", "origin"))


def _folder_of(path: str) -> str:
    real = os.path.realpath(path)
    return real if os.path.isdir(real) else os.path.dirname(real)


def rail_status(path: str, *, ask: bool = False) -> dict:
    if not path or not os.path.isabs(path):
        raise gs._Refused("path must be absolute", status=400)
    real = os.path.realpath(path)
    root = gs._repo_root(_folder_of(path))
    if root is None:
        return {"ok": True, "repo": None}
    rel = os.path.relpath(real, root).replace(os.sep, "/")
    if rel == ".":
        rel = ""
    has_remote = _has_remote(root)
    default = gu._default_branch(root, ask=ask) if has_remote else None
    ahead, behind = gu._ahead_behind_counts(root, default) if default else (None, None)
    commits = gs._run_log(root, rel, COMMITS) if gs._head_exists(root) else []
    return {
        "ok": True,
        "repo": {
            "root": root,
            "branch": gu._current_branch(root),
            "has_remote": has_remote,
            "default_branch": default,
            "ahead": ahead,
            "behind": behind,
            "commits": commits[:COMMITS],
        },
    }


def rail_check(path: str) -> dict:
    """git_upstream's own user-triggered check (``force_check``, module
    header), then the status as it stands — read with ``ask=True``, so a
    clone with no recorded default-branch symref resolves it now. ``checked``
    is True only when a fetch LANDED inside force_check's bounded wait; False
    means no remote, unreachable, the fetch slot held by another check, or
    still running — the counts shown are then the last fetch's, which is
    still the best answer, and the rail's next read picks up a late one."""
    if not path or not os.path.isabs(path):
        raise gs._Refused("path must be absolute", status=400)
    root = gs._repo_root(_folder_of(path))
    if root is None:
        return {"ok": True, "repo": None, "checked": False}
    started = time.time()
    state = gu.force_check(path)
    status = rail_status(path, ask=True)
    status["checked"] = bool(state) and float(state.get("checked_at") or 0) >= started
    return status


class _CheckBody(BaseModel):
    path: str


@router.api_route("/api/git/rail", methods=["GET"])
async def api_git_rail(path: str):
    try:
        return await run_in_threadpool(rail_status, path)
    except gs._Refused as e:
        return _error(e.message, status=e.status)


@router.post("/api/git/rail/check")
async def api_git_rail_check(body: _CheckBody, x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    try:
        return await run_in_threadpool(rail_check, body.path)
    except gs._Refused as e:
        return _error(e.message, status=e.status)
