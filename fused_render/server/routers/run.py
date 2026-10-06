"""POST /api/run — run a page's `.py` (`runPython`) on the selected engine.

A plain run door. The Claude chat used to come through here too, with a
claude-only folder gate around it; it has its own in-process router now
(`server/routers/claude_agent.py`, gate in `claude_agent/gate.py`).
"""
import asyncio
import logging

from fastapi import APIRouter, Body, Header, Request, Response

from fused_render import calls as shell_calls
from fused_render.server.common import _require_fused, resolve_py
from fused_render.executor import dumps_result, run_python
from fused_render.server import git_status
from fused_render.shell import prefs as shell_prefs

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/run")
async def api_run(request: Request, body: dict = Body(...),
                  x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    py = body.get("py")
    html = body.get("html")
    params = body.get("params") or {}

    resolved, resolve_error = resolve_py(py, html)
    if resolve_error is not None:
        return resolve_error

    # Engine dispatch (D69/§20): both paths return the same wire shape
    # ({ok, result, error:{type,message,traceback}, stdout} — the fused
    # engine adds stderr/duration_ms), so pages never see which ran.
    # Resolved per request: the Preferences switch applies to the next
    # run, no restart (a set FUSED_RENDER_ENGINE pins it instead).
    engine_used = shell_prefs.effective_engine()
    if engine_used == "fused":
        from fused_render import engine as _engine

        work = _engine.run_python(resolved, params)
    else:
        # The built-in executor blocks on a subprocess; keep the event
        # loop free (the endpoint is async now for the engine's sake).
        work = asyncio.to_thread(run_python, resolved, params)
    result = await work
    # A script that ran may have written anything, anywhere — including
    # through the git template's stage/unstage/commit (templates/git/ops.py),
    # which runs on this same subprocess-per-call path and so cannot reach
    # into THIS process's `git_status` cache itself (see that cache's own
    # `invalidate_status_cache` docstring). Dropping it here, unconditionally,
    # is the in-process mirror of `noteFsChanged()` (static/runtime.js) firing
    # on every runPython completion — success or failure, since a script that
    # wrote three files and then raised still changed the disk, and the same
    # is true of a git op that partially applied before erroring. Without
    # this, a listing fetched moments before the run — and still within
    # `_STATUS_CACHE_TTL_S` — would replay its now-stale answer to the very
    # refresh Job B's dir-watch fix triggers right after this call returns.
    git_status.invalidate_status_cache()
    # Hand the run's detail to the in-flight call record (calls.py): the
    # resolved .py, the params, the engine, and — on failure — the
    # traceback and output tails a user has since clicked away from. The
    # handler enriches; the middleware writes. (Whether the client hung up
    # mid-run is decided by the middleware — a route CANNOT see it; the
    # NOT IMPLEMENTED note above `no_cache_and_log` says why.)
    shell_calls.enrich_run(
        getattr(request.state, "fused_call", None),
        resolved=resolved, params=params, engine=engine_used, result=result,
    )
    # Tell the runtime which absolute file actually ran so it can watch it
    # for auto-reload (LR-2). Set on failed runs too, so a broken py that
    # gets fixed still triggers a reload.
    result["resolved_py"] = resolved
    # dumps_result, not JSONResponse: the in-process executor path already
    # serialized the payload (it has to, to validate it), so this reuses
    # that string instead of encoding a multi-MB result a second time. The
    # bytes are identical to JSONResponse's for every other result.
    return Response(content=dumps_result(result), media_type="application/json")
