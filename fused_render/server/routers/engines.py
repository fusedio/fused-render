"""Managed template engines through the stable server origin (docs/ENGINE_HOST_DESIGN.md).

The browser never learns a daemon's ephemeral port or token: a template rewrites
its own descriptor URLs to /api/engines/{engine_id}/proxy/... paths, and every
request here is proxied (via engine_forward) to the child engine_host manages for
that id. A child that died or wedged is restarted and the request retried once —
the healing that makes a proxied URL a page holds survive the daemon's whole
lifecycle, which the old client-side re-describe hack never could.

The control plane (ensure/reinit/forget) and every proxied POST carry the D3
X-Fused guard because they reach the child's executing side; proxied GETs are
read-only and same-origin like every other read. Nothing here is template-aware:
the engine_id and the proxied paths are opaque.
"""
import contextlib
import json

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import Response

from fused_render.server import engine_host
from fused_render.server.common import _error, _require_fused
from fused_render.server.engine_forward import _forward

router = APIRouter()


@router.post("/api/engines/{engine_id}/ensure")
def api_engine_ensure(engine_id: str, payload: dict = Body(...),
                      x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    try:
        child = engine_host.ensure(
            engine_id=engine_id,
            python=str(payload.get("python") or ""),
            daemon=str(payload.get("daemon") or ""),
            cache=str(payload.get("cache") or ""),
            version=str(payload.get("version") or ""),
        )
    except engine_host.EngineError as error:
        return _error(str(error), status=400)
    return {"ok": True, "version": child.version, "pid": child.pid}


@router.post("/api/engines/{engine_id}/reinit")
def api_engine_reinit(engine_id: str, payload: dict = Body(...),
                      x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    key = str(payload.get("key") or "")
    path = str(payload.get("path") or "")
    request = payload.get("payload")
    if not key or not path or not isinstance(request, dict):
        return _error("reinit needs key, path and payload", status=400)
    engine_host.reinit(engine_id, key, path, request)
    return {"ok": True}


@router.post("/api/engines/{engine_id}/forget")
def api_engine_forget(engine_id: str, payload: dict = Body(...),
                      x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    key = str(payload.get("key") or "")
    if key:
        engine_host.forget(engine_id, key)
    return {"ok": True}


@router.get("/api/engines/running")
def api_engines_running():
    """Every live engine child — what the status bar's Engines section lists
    (D591). NO `X-Fused` GUARD: this is a read-only, same-origin GET, the same
    posture this module's docstring states for proxied GETs and the same as
    `GET /api/apps/background/running`.

    The work is `engine_host.running_engines()`, which owns the lock discipline
    (snapshot under `_lock`, `Popen.poll()` outside it) — this router must not
    touch `_children` or that lock itself.
    """
    return {"engines": engine_host.running_engines()}


@router.post("/api/engines/{engine_id}/stop")
def api_engine_stop(engine_id: str,
                    x_fused: str | None = Header(default=None)):
    """Stop one engine child. `X-Fused` guarded like its `ensure`/`reinit`/
    `forget` siblings, since it reaches the child's executing side.

    NOT A DESTRUCTIVE ROUTE, despite the name — stopping any of the three
    kinds is safe and recoverable, which is why it is offered as a plain
    button in the status bar:

      * a `template` engine respawns on the next `ensure`;
      * a warm `app` worker respawns on the next call to it, and is already
        idle-reaped on a timer anyway (`APP_IDLE_RETIRE_S`);
      * a `background` app daemon going down IS the documented "quit this app
        right now" action (see `background_apps.py`'s own `/stop` docstring).

    Deliberately NOT routed through `POST /api/apps/background/stop`: that
    endpoint takes an `html` PAGE path and derives the folder with
    `_folder_for` (a `dirname()`), so handing it a folder would resolve to the
    folder's PARENT — and it only covers `kind="background"`, missing template
    engines and app workers entirely.

    `engine_host.stop` already does the whole teardown (kills the child, drops
    the replay registry and the busy count) and is idempotent for an id that
    is not running, so an unknown id is a no-op rather than an error.
    """
    if (error := _require_fused(x_fused)) is not None:
        return error
    engine_host.stop(engine_id)
    return {"ok": True}


@router.api_route(
    "/api/engines/{engine_id}/proxy/{path:path}",
    methods=["GET", "HEAD", "POST"],
)
async def api_engine_proxy(engine_id: str, path: str, request: Request,
                           x_fused: str | None = Header(default=None),
                           x_engine_reinit: str | None = Header(default=None)):
    # /ping is the daemon's private liveness path (engine_host probes it directly
    # with the token); it is never a page resource, so it is not proxied.
    if path == "ping":
        return _error("not found", status=404)
    # The path is opaque and forwarded verbatim, but no segment may climb out of
    # the namespace the child serves — a correctness guard for every engine.
    if ".." in path.split("/") or "\\" in path:
        return _error("not found", status=404)
    if request.method == "POST" and (error := _require_fused(x_fused)) is not None:
        return error
    body = await request.body() if request.method == "POST" else b""
    # Per-kind retry policy on a POST: a background app's proxied POST
    # (fused.daemon.call) can run arbitrary side-effecting daemon code, the same
    # shape as the warm /api/engine worker's own /call — which already passes
    # at_most_once=True so a heal-restart surfaces the failure instead of
    # silently re-sending it. A template daemon's POST traffic (e.g. a
    # "describe" request) stays pooled/retry-friendly by default, since most
    # of it is safely re-runnable and a blanket at_most_once here would give
    # up that resilience for no reason tied to background apps.
    child = engine_host.current(engine_id)
    at_most_once = (request.method == "POST"
                    and child is not None and child.kind == "background")
    response = await _forward(engine_id, request, "/" + path, body,
                              at_most_once=at_most_once)
    # A POST the caller marks replayable (X-Engine-Reinit: <key>) that the child
    # accepted is recorded here, atomically with the request — so a restart
    # re-runs it and the registration can never be lost to a separate call. The
    # body is opaque; the host only re-POSTs it.
    if (request.method == "POST" and x_engine_reinit
            and isinstance(response, Response)
            and 200 <= response.status_code < 300 and response.status_code != 204):
        with contextlib.suppress(ValueError):
            engine_host.reinit(engine_id, x_engine_reinit, "/" + path,
                               json.loads(body or b"{}"))
    return response
