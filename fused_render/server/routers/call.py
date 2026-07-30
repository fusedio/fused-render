"""GET /call/<fs path of app dir>?route=<name>&<params> — fused_app REST
endpoints.

A fused_app manifest page whose `file` ends in `.py` is an ENDPOINT, not a
renderable view: this route executes its `main(**params)` and returns the
result as a plain JSON body. The URL shape is exactly parallel to
/view/<fs path> (same per-segment encoding; FastAPI's `{path:path}` hands the
segments back decoded, and the leading slash is re-added like the frontend's
`rootedFsPath`), with `view` swapped for `call`.

Deliberately NO X-Fused guard: unlike /api/run this endpoint exists to be hit
from OUTSIDE the app — curl, cron, another service. The app-dir gate below is
the correctness boundary (only a folder that IS a valid fused_app, and only
files its manifest names, are reachable), the same trust model as every other
route (D3: local trusted user).

Query params other than `route` and `_`-prefixed keys become main(**params)
kwargs — strings on the wire, coerced by the parameter annotations exactly as
/api/run does (executor._binding). Always the BUILT-IN executor: an endpoint
is a local file next to its manifest; engine dispatch (D69) is a preview-page
preference and does not apply here.
"""
import asyncio
import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import Response

import re

from fused_render.executor import run_python
from fused_render.server.common import _error
from fused_render.server.routers.app_resolve import (
    _fused_app_condition,
    _read_manifest,
)
from fused_render.server.templates import _run_condition

router = APIRouter()


def _rooted_fs_path(joined: str) -> str:
    """URL path remainder -> absolute fs path; mirrors router.ts
    rootedFsPath: POSIX paths get their leading slash back, Windows
    drive-letter paths don't (a bare drive gets its trailing slash)."""
    if re.match(r"^[A-Za-z]:$", joined):
        return joined + "/"
    if re.match(r"^[A-Za-z]:/", joined):
        return joined
    return "/" + joined


def _route_name(path) -> str:
    """Manifest `pages[].path` -> route name; same normalization as the
    fused_app template's routeName(): strip leading/trailing slashes."""
    return str(path or "").strip("/")


@router.get("/call/{path:path}")
async def api_call(request: Request, path: str, route: str | None = None):
    app_dir = _rooted_fs_path(path)
    if route is None:
        return _error("missing required query param 'route'")

    # The dir must BE a valid fused_app — same gate as /api/fs/conditions and
    # /api/app/resolve (no ancestor walk here: the URL names the app dir).
    condition = _fused_app_condition()
    allowed = False
    if condition is not None:
        allowed, _err = _run_condition(condition, app_dir)
    if not allowed:
        return _error(f"not a fused_app directory: {app_dir}", status=404)
    try:
        manifest = _read_manifest(app_dir)
    except Exception:
        return _error(f"cannot read fused_app.json in {app_dir}", status=404)

    wanted = _route_name(route)
    page = None
    for p in manifest.get("pages") or []:
        if isinstance(p, dict) and _route_name(p.get("path")) == wanted:
            page = p
            break
    if page is None:
        return _error(f"unknown route: {route!r}", status=404)
    file = page.get("file")
    if not isinstance(file, str) or not file.lower().endswith(".py"):
        return _error(f"route {route!r} is not a Python endpoint", status=404)
    # Same containment rule as the condition gate's entry check: endpoints
    # live inside the app folder.
    if file.startswith("/") or ".." in file.split("/"):
        return _error(f"invalid endpoint file for route {route!r}", status=404)
    resolved = os.path.normpath(os.path.join(app_dir, file))
    if not os.path.isfile(resolved):
        return _error(f"endpoint file not found: {file}", status=404)

    params = {
        key: value
        for key, value in request.query_params.items()
        if key != "route" and not key.startswith("_")
    }
    # Built-in executor, off the event loop (blocking subprocess).
    result = await asyncio.to_thread(run_python, resolved, params)
    if not result.get("ok"):
        payload = {"error": result.get("error")}
        if result.get("stdout"):
            payload["stdout"] = result["stdout"]
        return Response(
            content=json.dumps(payload),
            status_code=500,
            media_type="application/json",
        )
    # The body is the bare `result` value (not the /api/run envelope) — an
    # external caller wants the data, not the runner's wire shape. Reuse the
    # executor's pre-encoded payload when present (dumps_result does for the
    # full envelope; here we slice just the result the same way).
    payload_json = getattr(result, "payload_json", None)
    body = payload_json if payload_json is not None else json.dumps(result.get("result"))
    return Response(content=body, media_type="application/json")
