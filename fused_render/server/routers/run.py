import asyncio
import os
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Body, Header, Request, Response

from fused_render import calls as shell_calls
from fused_render.server.common import _error, _require_fused
from fused_render.executor import dumps_result, run_python
from fused_render.shell import prefetch as shell_prefetch
from fused_render.shell import prefs as shell_prefs
from fused_render.shell import mounts as shell_mounts

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

    # Cold mount-backed reads: swap the raw-proxy source_url for the
    # store's own URL before the reader sees it. The /api/fs/raw 307
    # already sends cold ranged GETs to the store, but a redirect
    # defeats httpfs connection pooling — duckdb re-follows it per
    # range read and opens a fresh TLS connection to the store each
    # time (measured ~3x on a cold open: schema 8.5s vs 3.4s, a
    # 9-column page 14.5s vs 3.8s). Handing the reader the direct URL
    # up front lets httpfs pool its store connections normally. Done
    # here in the server, not in templates: pages keep sending the raw
    # URL and stay mount-agnostic. Warm files (prefetch landed) keep
    # the raw URL so the serve replays ranges from local disk; the
    # explicit schedule() below matters because a direct-reading run
    # never touches /api/fs/raw, which is otherwise the only place the
    # prefetch learns a file is in use.
    src = params.get("source_url")
    if isinstance(src, str):
        parts = urlsplit(src)
        fpath = dict(parse_qsl(parts.query)).get("path")
        if parts.path.endswith("/api/fs/raw") and fpath:
            upstream = shell_mounts.serve_url_for(fpath)
            if upstream is not None and not shell_prefetch.is_done(fpath):
                shell_prefetch.schedule(fpath, upstream)
                direct = await asyncio.to_thread(
                    shell_mounts.upstream_url_for, fpath)
                if direct:
                    params = dict(params, source_url=direct)

    if not py:
        return _error("request body must include 'py': a path to a Python file")

    if os.path.isabs(py):
        resolved = py
    else:
        if not html:
            return _error(
                "'py' is a relative path but 'html' was not provided; "
                "either send an absolute 'py' path or include 'html' so it can be resolved"
            )
        resolved = os.path.normpath(os.path.join(os.path.dirname(html), py))

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
