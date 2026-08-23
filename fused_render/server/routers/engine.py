"""The warm variant of /api/run (docs/ENGINE_HOST_APPS_DESIGN.md).

`POST /api/engine` takes the SAME `{py, html, params}` body as `/api/run` and
returns the SAME result envelope — the only difference is that the worker process
running the script's `main(**params)` is kept ALIVE between calls, so module-level
imports run once and module globals persist. It is opt-in: a page reaches it only
through `fused.engine(...)`, and `/api/run` stays the always-fresh default.

Everything is resolved server-side exactly as `run.py` does — the relative `py`
against `html`, the interpreter through `projectenv` — so this adds NO code-
execution surface over `/api/run`: it runs the calling app's own resolved `.py`
on the interpreter projectenv already chooses, behind the same `X-Fused` guard.
The warm worker is supervised by `engine_host` (spawn/health/heal/idle-retire/
kill-at-shutdown) and the call is forwarded to it with the engines proxy's
heal-on-failure + cancel-on-disconnect (`routers/engines._forward`).
"""
import asyncio
import json
import os

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response

from fused_render import projectenv
from fused_render.server import engine_host
from fused_render.server.common import _error, _require_fused
from fused_render.server.routers.engines import _forward

router = APIRouter()


def _resolve_py(py, html) -> tuple[str | None, Response | None]:
    """Resolve `py` (absolute, or relative to `html`) the way run.py does.

    Returns (resolved, None) on success or (None, error-response) so the caller
    can `return` the error directly. Rejects a path that climbs out with `..`
    — the same correctness guard engines.py applies to a proxied path — while
    keeping run.py's relative-to-`html` contract.
    """
    if not py:
        return None, _error("request body must include 'py': a path to a Python file")
    if os.path.isabs(py):
        resolved = py
    else:
        if not html:
            return None, _error(
                "'py' is a relative path but 'html' was not provided; "
                "either send an absolute 'py' path or include 'html' so it can "
                "be resolved")
        resolved = os.path.normpath(os.path.join(os.path.dirname(html), py))
    return os.path.abspath(resolved), None


@router.post("/api/engine")
async def api_engine(request: Request, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except (ValueError, json.JSONDecodeError) as e:
        return _error(f"invalid request body: {e}")
    resolved, error = _resolve_py(body.get("py"), body.get("html"))
    if error is not None:
        return error
    params = body.get("params") or {}

    if not os.path.isfile(resolved):
        return _error(f"no such Python file: {resolved}", status=404)

    # The SAME interpreter /api/run would choose: the app's own sys.executable
    # when the folder declares no environment, else its venv python (PY-17).
    project = projectenv.project_env_for(resolved)
    interpreter = projectenv.interpreter_for(project)
    # Phase 1 does not build a missing venv: a warm worker cannot re-use
    # /api/run's install loader yet (design §13). Say so instead of spawning a
    # non-existent interpreter — the user runs the file once through /api/run
    # (which drives the install), then the warm path finds the venv ready.
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

    # Forward the params to the warm worker's /call, reusing the engines proxy's
    # heal-on-failure and cancel-on-disconnect. The worker returns the /api/run
    # envelope verbatim (with resolved_py), so this response is byte-identical in
    # shape to /api/run's.
    payload = json.dumps(params).encode("utf-8")
    return await _forward(child.engine_id, request, "/call", payload)


@router.post("/api/engine/forget")
async def api_engine_forget(request: Request,
                            x_fused: str | None = Header(default=None)):
    """Drop a warm worker explicitly (idle-retire covers the common case).

    Best-effort: forgetting a worker that was never started, or already retired,
    is a no-op success — the page only wants the process gone.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except (ValueError, json.JSONDecodeError) as e:
        return _error(f"invalid request body: {e}")
    resolved, error = _resolve_py(body.get("py"), body.get("html"))
    if error is not None:
        return error
    await asyncio.to_thread(engine_host.stop, engine_host.app_engine_id(resolved))
    return {"ok": True}
