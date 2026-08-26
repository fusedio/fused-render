"""Background apps API (SPEC.md §46): enable/disable/stop/restart/status
for a folder's declared background daemon (`fused_render/background_apps.py`'s
manifest + enabled store), and `engine_host.ensure_background` for the actual
bring-up.

Every endpoint takes `html` — the page's own path — never a raw folder path,
and resolves the app folder from it server-side exactly as `/api/run` /
`/api/engine` resolve `py`: this adds no code-execution surface and no
path-typed API to defend (the same stance `resolve_py` documents). The
interpreter is chosen exactly as `routers/app_engine.py` chooses one for the
warm `/api/engine` worker, including its 409 when the project venv is not
built yet — building one inside a POST would block for minutes, so opening
the page once (which builds it) is the precondition here too.

`stop` and `disable` are deliberately two different actions: `stop` kills the
running daemon but leaves it enabled, so the startup resurrection hook brings
it back next launch; `disable` kills it AND unpersists, so it stays down.
"""
import asyncio
import os

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render import background_apps
from fused_render.server import engine_host
from fused_render.server.common import _error, _require_fused

router = APIRouter()


def _folder_for(html) -> str | None:
    if not isinstance(html, str) or not html:
        return None
    return os.path.dirname(os.path.abspath(html))


def _resolve(html) -> tuple[str, background_apps.Manifest, str, None] | tuple[None, None, None, JSONResponse]:
    """folder/manifest/interpreter for `html`, or a ready-to-return error.

    Interpreter choice (background_apps.interpreter_for) mirrors
    routers/app_engine.py:36-62 exactly — the same fused-vs-builtin dispatch
    /api/engine uses for its warm worker — including the 409 when the project
    venv is not built yet: opening the page once (or running it via
    /api/run) installs it, this endpoint never builds one itself.
    """
    folder = _folder_for(html)
    if folder is None:
        return None, None, None, _error("request body must include 'html'")
    manifest = background_apps.load_manifest(folder)
    if manifest is None:
        return None, None, None, _error(
            f"{os.path.basename(folder)} has no [tool.fused-render.app] "
            "background manifest", status=404)
    interpreter = background_apps.interpreter_for(folder)
    if not os.path.isfile(interpreter):
        return None, None, None, _error(
            f"{os.path.basename(folder)} needs its project environment built "
            "before its background app can start; open it once (or call "
            "fused.runPython) to install it, then retry.", status=409)
    return folder, manifest, interpreter, None


@router.get("/api/apps/background/status")
async def api_background_status(html: str = ""):
    # Read-only, same posture as every other GET here — no X-Fused guard.
    folder = _folder_for(html)
    if folder is None:
        return _error("query must include 'html'")
    engine_id = background_apps.engine_id_for(folder)
    enabled = folder in await asyncio.to_thread(background_apps.enabled_paths)
    child = engine_host.current(engine_id)
    running = child is not None and engine_host._alive(child)
    return {
        "enabled": enabled,
        "running": running,
        "pid": child.pid if running else None,
        "version": child.version if child is not None else None,
        "engine_id": engine_id,
    }


@router.post("/api/apps/background/enable")
async def api_background_enable(body: dict = Body(...),
                                x_fused: str | None = Header(default=None)):
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    folder, manifest, interpreter, error = _resolve(body.get("html"))
    if error is not None:
        return error
    engine_id = background_apps.engine_id_for(folder)
    try:
        version = background_apps.version_for(folder, interpreter)
    except OSError as e:
        return _error(f"could not read {os.path.basename(folder)}'s manifest: {e}",
                      status=400)
    # Persist BEFORE bring-up: ensure_background validates the daemon against
    # the enabled store, so an enable that fails to spawn still leaves the app
    # enabled — the startup hook (or a retried enable) will bring it up once
    # whatever failed is fixed, matching the "sticky enable" model.
    background_apps.set_enabled(folder, True)
    try:
        child = await asyncio.to_thread(
            engine_host.ensure_background, engine_id, interpreter,
            manifest.daemon, background_apps.cache_dir_for(engine_id), version)
    except (engine_host.EngineError, OSError) as e:
        return _error(f"could not start {os.path.basename(folder)}'s "
                      f"background app: {e}", status=502)
    return {"ok": True, "engine_id": engine_id, "pid": child.pid,
            "version": child.version}


@router.post("/api/apps/background/disable")
async def api_background_disable(body: dict = Body(...),
                                 x_fused: str | None = Header(default=None)):
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    folder = _folder_for(body.get("html"))
    if folder is None:
        return _error("request body must include 'html'")
    engine_id = background_apps.engine_id_for(folder)
    await asyncio.to_thread(engine_host.stop, engine_id)
    background_apps.set_enabled(folder, False)
    return {"ok": True}


@router.post("/api/apps/background/stop")
async def api_background_stop(body: dict = Body(...),
                              x_fused: str | None = Header(default=None)):
    """Kill the running daemon WITHOUT disabling it — the app stays enabled and
    the startup hook (or the next `enable`/`restart`) brings it back. This is
    the "quit this app right now" action; `disable` is "turn it off"."""
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    folder = _folder_for(body.get("html"))
    if folder is None:
        return _error("request body must include 'html'")
    engine_id = background_apps.engine_id_for(folder)
    await asyncio.to_thread(engine_host.stop, engine_id)
    return {"ok": True}


@router.post("/api/apps/background/restart")
async def api_background_restart(body: dict = Body(...),
                                 x_fused: str | None = Header(default=None)):
    """Respawn the daemon. When there is no LIVE child to restart — the app
    was `stop()`ped, or this is the first bring-up after a server start where
    the resurrection hook hasn't reached it yet — `engine_host.restart` alone
    would raise "has never been started", an opaque 502 for a caller that
    just did `fused.app.stop()` then `fused.app.restart()` (the documented
    stop/restart contract). Falls back to a fresh `ensure_background`
    bring-up in that case: the folder is enough to recompute the interpreter
    and version from scratch, same as `enable`."""
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    folder, manifest, interpreter, error = _resolve(body.get("html"))
    if error is not None:
        return error
    engine_id = background_apps.engine_id_for(folder)
    try:
        if engine_host.current(engine_id) is None:
            version = background_apps.version_for(folder, interpreter)
            child = await asyncio.to_thread(
                engine_host.ensure_background, engine_id, interpreter,
                manifest.daemon, background_apps.cache_dir_for(engine_id), version)
        else:
            child = await asyncio.to_thread(engine_host.restart, engine_id)
    except (engine_host.EngineError, OSError) as e:
        return _error(str(e), status=502)
    return {"ok": True, "pid": child.pid, "version": child.version}


@router.get("/api/apps/background/running")
async def api_background_running():
    """The enabled paths with a live-child boolean each — for the /apps grid's
    running badge (Task 5). Reads only `engine_host.current`; no folder walk,
    no toml reads, so it's cheap enough to call once per grid render."""
    paths = await asyncio.to_thread(background_apps.enabled_paths)
    out = {}
    for path in paths:
        engine_id = background_apps.engine_id_for(path)
        child = engine_host.current(engine_id)
        out[path] = child is not None and engine_host._alive(child)
    return {"running": out}
