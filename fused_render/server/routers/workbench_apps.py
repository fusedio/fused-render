"""HTTP surface for compiling and deploying apps to Fused Workbench."""
from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import APIRouter, Body, Header

from fused_render.export import ExportError
from fused_render.server.common import _error, _require_fused
from fused_render.workbench_app import compile_workbench_app
from fused_render.workbench_deploy import (
    WorkbenchDeployError,
    deploy_workbench_app,
    deployment_plan,
    list_deployments,
)


router = APIRouter()


def _request(body: dict):
    page = body.get("page")
    if not isinstance(page, str) or not os.path.isabs(page):
        return None, _error("'page' must be an absolute path to an app entry .html file")
    canvas_name = body.get("canvas_name")
    if not isinstance(canvas_name, str) or not canvas_name.strip():
        return None, _error("'canvas_name' must be a non-empty string")
    include = body.get("include") or []
    exclude = body.get("exclude") or []
    for name, value in (("include", include), ("exclude", exclude)):
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return None, _error(f"'{name}' must be an array of relative paths")
    cache = body.get("cache_max_age") or "0s"
    if not isinstance(cache, str):
        return None, _error("'cache_max_age' must be a string such as '0s' or '5m'")
    return {
        "html_path": page,
        "canvas_name": canvas_name.strip(),
        "include": include,
        "exclude": exclude,
        "cache_max_age": cache,
    }, None


@router.post("/api/apps/workbench/plan")
def api_workbench_app_plan(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    request, error = _request(body)
    if error is not None:
        return error
    try:
        compiled = compile_workbench_app(**request)
    except ExportError as exc:
        return _error(str(exc))
    return deployment_plan(compiled)


@router.post("/api/apps/workbench/deploy")
def api_workbench_app_deploy(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    request, error = _request(body)
    if error is not None:
        return error
    share = body.get("share", True)
    if not isinstance(share, bool):
        return _error("'share' must be a boolean")
    try:
        record = deploy_workbench_app(**request, share=share)
    except ExportError as exc:
        return _error(str(exc))
    except WorkbenchDeployError as exc:
        return _error(str(exc), 502)
    return asdict(record)


@router.get("/api/apps/workbench/deployments")
def api_workbench_app_deployments(page: str | None = None):
    if page is not None and not os.path.isabs(page):
        return _error("'page' must be an absolute path")
    return {"deployments": list_deployments(page)}
