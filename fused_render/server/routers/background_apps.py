"""Background apps API (SPEC.md §46): start/stop/restart/autostart/status
for a folder's declared background daemon (`fused_render/background_apps.py`'s
manifest + autostart store), and `engine_host.ensure_background` for the
actual bring-up.

Every endpoint takes `html` — the page's own path — never a raw folder path,
and resolves the app folder from it server-side exactly as `/api/run` /
`/api/engine` resolve `py`: this adds no code-execution surface and no
path-typed API to defend (the same stance `resolve_py` documents). The
interpreter is chosen exactly as `routers/app_engine.py` chooses one for the
warm `/api/engine` worker, including its 409 when the project venv is not
built yet — building one inside a POST would block for minutes, so opening
the page once (which builds it) is the precondition here too.

Run state and autostart are two independent, orthogonal things (D511, code
review that produced this module's current shape): `start`/`stop`/`restart`
change whether the daemon is alive RIGHT NOW and never touch the persisted
autostart flag; `autostart` changes only that flag and never starts or stops
anything. **Autostart is opt-in** — `start` alone leaves it exactly where it
was (usually off), so calling `start` never silently installs a
"come back forever" daemon; only an explicit `POST .../autostart` with
`{"autostart": true}` does that. `status` reports both facts explicitly
(`running`, `autostart`) so a caller never has to infer one from the other.

`enable`/`disable` (which used to conflate the two) are gone — no back-compat
aliases; this feature is unmerged and the only caller (OpenWhisper) was
updated alongside this router.
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
    """The app folder `html` (the caller's own page path) belongs to.

    realpath'd (not just abspath'd) — D509, 2026-08-26 code review: this used
    to be a bare `os.path.dirname(os.path.abspath(html))`, which diverged
    from `background_apps.engine_id_for`'s realpath-based identity the
    moment a folder was reached through a symlink alias. `autostart`
    (compared against `autostart_paths()`, itself realpath'd — D512) and
    `running` (keyed off `engine_id_for`'s realpath hash) could then
    disagree for the exact same app — `{"autostart": False, "running": True}`
    through one alias while the other alias showed the opposite — and an
    `autostart` call through different aliases wrote/removed different store
    entries for what is really one folder. Realpath'ing here makes every
    endpoint's folder identity agree with `engine_id_for`'s, the same
    normalization `background_apps.py`'s own `daemon`-containment check and
    autostart store already apply."""
    if not isinstance(html, str) or not html:
        return None
    return os.path.realpath(os.path.dirname(os.path.abspath(html)))


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
    autostart = folder in await asyncio.to_thread(background_apps.autostart_paths)
    child = engine_host.current(engine_id)
    running = child is not None and engine_host._alive(child)
    return {
        "running": running,
        "autostart": autostart,
        "pid": child.pid if running else None,
        "version": child.version if child is not None else None,
        "engine_id": engine_id,
    }


@router.post("/api/apps/background/start")
async def api_background_start(body: dict = Body(...),
                               x_fused: str | None = Header(default=None)):
    """Spawn the daemon now. Does NOT touch the autostart flag — a `start`
    that isn't followed by an explicit `autostart` call comes back only for
    the lifetime of this server run, never at the next launch (D511: opt-in
    autostart is the whole point of this split)."""
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
    try:
        child = await asyncio.to_thread(
            engine_host.ensure_background, engine_id, interpreter,
            manifest.daemon, background_apps.cache_dir_for(engine_id), version,
            folder)
    except (engine_host.EngineError, OSError) as e:
        return _error(f"could not start {os.path.basename(folder)}'s "
                      f"background app: {e}", status=502)
    return {"ok": True, "engine_id": engine_id, "pid": child.pid,
            "version": child.version}


@router.post("/api/apps/background/autostart")
async def api_background_autostart(body: dict = Body(...),
                                   x_fused: str | None = Header(default=None)):
    """Set the persisted autostart flag for `html`'s app folder. Does NOT
    start or stop anything — pass `{"html": <path>, "autostart": true|false}`.
    """
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    folder = _folder_for(body.get("html"))
    if folder is None:
        return _error("request body must include 'html'")
    autostart = bool(body.get("autostart"))
    await asyncio.to_thread(background_apps.set_autostart, folder, autostart)
    return {"ok": True, "autostart": autostart}


@router.post("/api/apps/background/stop")
async def api_background_stop(body: dict = Body(...),
                              x_fused: str | None = Header(default=None)):
    """Kill the running daemon WITHOUT touching autostart — if autostart is
    on, the startup hook still brings it back next launch; if it's off (the
    default), it stays down until an explicit `start`. This is the "quit
    this app right now" action."""
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
    """Respawn the daemon. Does not touch autostart either. When there is no
    LIVE child to restart — the app was `stop()`ped, or this is the first
    bring-up after a server start where the resurrection hook hasn't reached
    it yet — `engine_host.restart` alone would raise "has never been
    started", an opaque 502 for a caller that just did
    `fused.daemon.stop()` then `fused.daemon.restart()` (the documented
    stop/restart contract). Falls back to a fresh `ensure_background`
    bring-up in that case: the folder is enough to recompute the interpreter
    and version from scratch, same as `start`."""
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    folder, manifest, interpreter, error = _resolve(body.get("html"))
    if error is not None:
        return error
    engine_id = background_apps.engine_id_for(folder)
    try:
        # Always recompute the version fresh (D510, 2026-08-26 code review):
        # a live child's restart used to keep `existing.version` — the
        # digest from whenever it was last brought up — which meant a
        # restart right after editing daemon.py respawned the new code
        # tagged with the OLD version. The next start()/server-start
        # resurrection would then see its own fresh digest disagree with
        # the registered one and tear the just-restarted child down and
        # spawn it a SECOND time. Computing it once here and passing it to
        # both branches keeps them in sync.
        version = background_apps.version_for(folder, interpreter)
        if engine_host.current(engine_id) is None:
            child = await asyncio.to_thread(
                engine_host.ensure_background, engine_id, interpreter,
                manifest.daemon, background_apps.cache_dir_for(engine_id), version,
                folder)
        else:
            child = await asyncio.to_thread(
                engine_host.restart, engine_id, None, version=version)
    except (engine_host.EngineError, OSError) as e:
        return _error(str(e), status=502)
    return {"ok": True, "pid": child.pid, "version": child.version}


@router.get("/api/apps/background/running")
async def api_background_running():
    """The autostart-opted-in paths with a live-child boolean each — for the
    /apps grid's running badge (Task 5). Reads only `engine_host.current`; no
    folder walk, no toml reads, so it's cheap enough to call once per grid
    render."""
    paths = await asyncio.to_thread(background_apps.autostart_paths)
    out = {}
    for path in paths:
        engine_id = background_apps.engine_id_for(path)
        child = engine_host.current(engine_id)
        out[path] = child is not None and engine_host._alive(child)
    return {"running": out}
