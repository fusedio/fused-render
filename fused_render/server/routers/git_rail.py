"""THE CLAUDE RAIL'S GIT SECTION (frontend/src/shell/claude-rail/ClaudeRail.tsx
``git``): what a page's collapsed rail says about the repository its folder
or file sits in — the branch, how far it stands from the remote's default
branch, and the last three commits that touched the folder (or the file).

Two routes, both for ANY absolute path, not only an app folder (the explorer's
folder and file views draw the rail too):

  GET  /api/git/rail?path=<abs>   -> {"ok": true, "repo": null | {...}}
  POST /api/git/rail/check {path} -> the same, after one fetch of the default
                                     branch (X-Fused; the rail's "Check for
                                     updates" seat). ``checked`` says whether
                                     the fetch reached the remote.

``repo`` is null when no ancestor carries a ``.git`` (a canvas clone, a plain
folder): the rail then draws no Git section at all. Otherwise:

  root            the repository root (realpath)
  branch          ``symbolic-ref --short HEAD``, null on a detached HEAD
  default_branch  the remote's default branch off the clone's recorded symref
                  — READ ONLY here: no ``remote set-head --auto`` re-ask,
                  which is a network call, on a GET the rail makes on every
                  page it stands on (git_upstream's own fallback stays with
                  the upstream check)
  ahead, behind   HEAD against ``origin/<default_branch>`` as last fetched
                  (git_upstream ``_ahead_behind_counts``); null with no remote
  commits         ``git log -n3`` scoped to the path (git_snapshot ``_run_log``)

Pure functions over the git helpers the two neighbouring routers already
hold, so a test runs them against a real repository with no TestClient
(tests/test_git_rail.py), the way test_git_commits.py does."""
from __future__ import annotations

import os

from fastapi import APIRouter, Header
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from fused_render import git_upstream as gu
from fused_render.server.common import _error, _require_fused
from fused_render.server.routers import git_snapshot as gs

router = APIRouter()

# The rail has room for three seats under the branch (claude-rail.css).
COMMITS = 3


def _default_branch_local(root: str) -> str | None:
    """The recorded default branch, with no network re-ask (module header)."""
    result = gu._run_safe(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not gu._ok(result):
        return None
    short = gu._out(result)
    return short.split("/", 1)[1] if "/" in short else None


def rail_status(path: str) -> dict:
    if not path or not os.path.isabs(path):
        raise gs._Refused("path must be absolute", status=400)
    real = os.path.realpath(path)
    folder = real if os.path.isdir(real) else os.path.dirname(real)
    root = gs._repo_root(folder)
    if root is None:
        return {"ok": True, "repo": None}
    rel = os.path.relpath(real, root).replace(os.sep, "/")
    if rel == ".":
        rel = ""
    default = _default_branch_local(root)
    ahead, behind = gu._ahead_behind_counts(root, default) if default else (None, None)
    commits = gs._run_log(root, rel, COMMITS) if gs._head_exists(root) else []
    return {
        "ok": True,
        "repo": {
            "root": root,
            "branch": gu._current_branch(root),
            "default_branch": default,
            "ahead": ahead,
            "behind": behind,
            "commits": commits[:COMMITS],
        },
    }


def rail_check(path: str) -> dict:
    """One fetch of the default branch (git_upstream ``check_repo``: its
    TIMEOUT_S, its one-ref fetch), then the status as it stands. ``checked``
    is False when the remote could not be reached or there is none — the
    counts shown are then the last fetch's, which is still the best answer."""
    status = rail_status(path)
    repo = status.get("repo")
    checked = False
    if repo:
        checked = gu.check_repo(repo["root"]) is not None
        status = rail_status(path)
    status["checked"] = checked
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
