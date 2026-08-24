"""The warm variant of /api/run (docs/ENGINE_HOST_APPS_DESIGN.md).

`POST /api/engine` takes the same `{py, html, params}` body as `/api/run` and
returns the same envelope, but the worker running `main(**params)` is kept alive
between calls. Everything is resolved server-side exactly as `run.py` does, so
this adds no code-execution surface over `/api/run`. The worker is supervised by
`engine_host`; the call is forwarded via `routers/engines._forward` (which brings
heal-on-failure + cancel-on-disconnect).
"""
import asyncio
import json
import os
import sys

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import Response

from fused_render import projectenv
from fused_render.server import engine_host
from fused_render.server.common import _error, _require_fused, resolve_py
from fused_render.server.routers.engines import _forward
from fused_render.shell import prefs as shell_prefs

router = APIRouter()


def _resolve_py(py, html) -> tuple[str | None, Response | None]:
    """resolve_py (shared with run.py), returning an absolute path."""
    resolved, error = resolve_py(py, html)
    return (os.path.abspath(resolved), None) if error is None else (None, error)


@router.post("/api/engine")
async def api_engine(request: Request, body: dict = Body(...),
                     x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    resolved, error = _resolve_py(body.get("py"), body.get("html"))
    if error is not None:
        return error
    params = body.get("params") or {}

    if not os.path.isfile(resolved):
        return _error(f"no such Python file: {resolved}", status=404)

    # The same interpreter /api/run would choose (PY-17): the built-in executor
    # always runs on sys.executable and builds no venv, so only the fused engine
    # gets the project venv python — mirroring /api/run's own dispatch.
    if shell_prefs.effective_engine() == "fused":
        project = projectenv.project_env_for(resolved)
        interpreter = projectenv.interpreter_for(project)
    else:
        interpreter = sys.executable
    # The warm path does not build a missing venv yet: say so instead of spawning
    # a non-existent interpreter. Opening once via /api/run installs it.
    if not os.path.isfile(interpreter):
        return _error(
            f"{os.path.basename(resolved)} needs its project environment built "
            "before it can run warm; open it once (or call fused.runPython) to "
            "install it, then retry.", status=409)

    try:
        child = await asyncio.to_thread(engine_host.ensure_app, resolved, interpreter)
    except (engine_host.EngineError, OSError) as e:
        return _error(f"could not start a warm worker for "
                      f"{os.path.basename(resolved)}: {e}", status=502)

    # Forward to the worker's /call; it returns the /api/run envelope verbatim.
    # Mark the engine busy (by id, so a heal-restart mid-call still counts) to
    # keep the idle reaper from retiring it, and stamp last_used at completion so
    # idle is timed from the call's end, not its start. call_timeout bounds the
    # wait at the /api/run budget: a slow call becomes a 504 with the worker left
    # running (it does not kill its own thread), never a heal that would restart
    # the worker, re-run main(), and kill the calls running concurrently on it.
    payload = json.dumps(params).encode("utf-8")
    engine_id = child.engine_id
    engine_host.mark_busy(engine_id)
    try:
        return await _forward(engine_id, request, "/call", payload,
                              call_timeout=engine_host.APP_CALL_TIMEOUT_S,
                              at_most_once=True)
    finally:
        engine_host.mark_idle(engine_id)


@router.post("/api/engine/forget")
async def api_engine_forget(body: dict = Body(...),
                            x_fused: str | None = Header(default=None)):
    """Drop a warm worker explicitly (idle-retire covers the common case).

    Best-effort: forgetting a worker that was never started is a no-op success.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    resolved, error = _resolve_py(body.get("py"), body.get("html"))
    if error is not None:
        return error
    await asyncio.to_thread(engine_host.stop, engine_host.app_engine_id(resolved))
    return {"ok": True}
