"""Background apps API (SPEC.md §46): start/stop/restart/autostart/status
for a folder's declared background daemon (`fused_render/background_apps.py`'s
manifest + autostart store), and `engine_host.ensure_background` for the
actual bring-up.

Every endpoint takes `html` — the page's own path — never a raw folder path,
and resolves the app folder from it server-side exactly as `/api/run`
resolves `py`: this adds no code-execution surface and no path-typed API to
defend (the same stance `resolve_py` documents). The interpreter is chosen by
`background_apps.interpreter_for`, falling back to `sys.executable` when the
project venv it would prefer is not built yet, so a daemon always starts
rather than blocking a POST for however long building a venv would take
(D631) — unlike `/api/run`'s fused-engine dispatch, which answers a missing
venv with a structured `needs_install` response instead of ever substituting
an interpreter that lacks the folder's declared packages. When the
substituted `sys.executable` then can't run the daemon (it never has the
folder's declared deps), `start`/`restart` report `background_apps.
unbuilt_deps_reason`'s actionable message instead of the generic spawn
failure, matching what `resurrect_autostart` already logs for the identical
condition.

Run state and autostart are two independent, orthogonal things (D511):
`start`/`stop`/`restart` change whether the daemon is alive RIGHT NOW and
never touch the persisted autostart flag; `autostart` changes only that flag
and never starts or stops anything. **Autostart is opt-in** — `start` alone
leaves it exactly where it was (usually off), so calling `start` never
silently installs a "come back forever" daemon; only an explicit
`POST .../autostart` with `{"autostart": true}` does that. `status` reports
both facts explicitly (`running`, `autostart`) so a caller never has to infer
one from the other.
"""
import asyncio
import os
import sys

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render import background_apps
from fused_render.server import engine_host
from fused_render.server.common import _error, _require_fused

router = APIRouter()


def _folder_for(html) -> str | None:
    """The app folder `html` (the caller's own page path) belongs to.

    realpath'd (not just abspath'd) — D509 — so every endpoint's folder
    identity agrees with `background_apps.engine_id_for`'s realpath-based
    identity: `autostart` (compared against `autostart_paths()`, itself
    realpath'd — D512) and `running` (keyed off `engine_id_for`'s realpath
    hash) must resolve the same folder for a page reached through a symlink
    alias as for one reached directly, or an `autostart` call through one
    alias would write/remove a different store entry than a `running` check
    through another for what is really one folder. This is the same
    normalization `background_apps.py`'s own `daemon`-containment check and
    autostart store already apply."""
    if not isinstance(html, str) or not html:
        return None
    return os.path.realpath(os.path.dirname(os.path.abspath(html)))


def _resolve(html) -> (
        tuple[str, background_apps.Manifest, str, str | None, None]
        | tuple[None, None, None, None, JSONResponse]):
    """folder/manifest/interpreter/unbuilt-deps-reason for `html`, or a
    ready-to-return error.

    Interpreter choice (background_apps.interpreter_for) falls back to
    `sys.executable` when the declared project venv is not built yet,
    rather than refusing to start until the folder is opened once to build
    it (D631). The fourth element is `background_apps.unbuilt_deps_reason`'s
    verdict on that fallback — None when the interpreter didn't need one, or
    an actionable reason for the caller to report if the fallback attempt
    then fails.
    """
    folder = _folder_for(html)
    if folder is None:
        return None, None, None, None, _error("request body must include 'html'")
    manifest = background_apps.load_manifest(folder)
    if manifest is None:
        return None, None, None, None, _error(
            f"{os.path.basename(folder)} has no [tool.fused-render.app] "
            "background manifest", status=404)
    interpreter = background_apps.interpreter_for(folder)
    unbuilt_reason = background_apps.unbuilt_deps_reason(folder, interpreter)
    if unbuilt_reason is not None:
        interpreter = sys.executable
    return folder, manifest, interpreter, unbuilt_reason, None


def _protocol_for(manifest: background_apps.Manifest | None) -> str | None:
    """The folder's declared bring-up shape: `"main"` for a `main =` folder,
    `"daemon"` for a `daemon =` folder, `None` for a folder with no valid
    manifest at all — the field that lets `runtime.js`'s `run()`/`call()`
    tell which of the two page-side methods a folder actually wants. Shared
    by `status`, `start` and `restart` so a page never has to re-fetch
    `status()` after `start()`/`restart()` just to learn what those two
    already had in hand from the same manifest their own `_resolve` load."""
    return None if manifest is None else ("main" if manifest.main else "daemon")


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
    manifest = await asyncio.to_thread(background_apps.load_manifest, folder)
    protocol = _protocol_for(manifest)
    return {
        "running": running,
        "autostart": autostart,
        "pid": child.pid if running else None,
        "version": child.version if child is not None else None,
        "engine_id": engine_id,
        "protocol": protocol,
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
    folder, manifest, interpreter, unbuilt_reason, error = _resolve(body.get("html"))
    if error is not None:
        return error
    engine_id = background_apps.engine_id_for(folder)
    try:
        version = background_apps.version_for(folder, interpreter)
    except OSError as e:
        return _error(f"could not read {os.path.basename(folder)}'s manifest: {e}",
                      status=400)
    daemon, module = background_apps.bring_up_args(manifest)
    try:
        child = await asyncio.to_thread(
            engine_host.ensure_background, engine_id, interpreter,
            daemon, background_apps.cache_dir_for(engine_id), version,
            folder, manifest.idle_timeout_s, module,
            retry_post=manifest.retry_post)
    except (engine_host.EngineError, OSError) as e:
        # The sys.executable fallback above (D631) already tried; when it
        # then fails, the folder's own unbuilt venv is the known, actionable
        # cause — report that instead of ensure_background's generic spawn
        # failure, which names no fix.
        detail = unbuilt_reason if unbuilt_reason is not None else str(e)
        return _error(f"could not start {os.path.basename(folder)}'s "
                      f"background app: {detail}", status=502)
    return {"ok": True, "engine_id": engine_id, "pid": child.pid,
            "version": child.version, "protocol": _protocol_for(manifest)}


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
    folder, manifest, interpreter, unbuilt_reason, error = _resolve(body.get("html"))
    if error is not None:
        return error
    engine_id = background_apps.engine_id_for(folder)
    try:
        # Always recompute the version fresh (D510): a restart must tag the
        # respawned child with the digest of the code it's actually running,
        # not a stale `existing.version`, so the next start()/server-start
        # resurrection sees its own fresh digest agree with the registered
        # one instead of tearing the child down and spawning it again.
        # Computing it once here and passing it to both branches keeps them
        # in sync.
        version = background_apps.version_for(folder, interpreter)
        if engine_host.current(engine_id) is None:
            daemon, module = background_apps.bring_up_args(manifest)
            child = await asyncio.to_thread(
                engine_host.ensure_background, engine_id, interpreter,
                daemon, background_apps.cache_dir_for(engine_id), version,
                folder, manifest.idle_timeout_s, module,
                retry_post=manifest.retry_post)
        else:
            child = await asyncio.to_thread(
                engine_host.restart, engine_id, None, version=version)
    except (engine_host.EngineError, OSError) as e:
        # Same reasoning as `start`'s except-clause above: the fallback
        # interpreter already tried, so a failure here is best explained by
        # the folder's own unbuilt venv when that's what triggered it.
        return _error(unbuilt_reason if unbuilt_reason is not None else str(e),
                      status=502)
    return {"ok": True, "pid": child.pid, "version": child.version,
            "protocol": _protocol_for(manifest)}


@router.get("/api/apps/background/running")
async def api_background_running():
    """The set of app folders with a live background child RIGHT NOW — for
    the /apps grid's running badge (Task 5).

    Enumerated from `engine_host`'s own in-memory children, not
    `background_apps.autostart_paths()`: `start()` doesn't persist anything
    (D511 keeps run state and autostart independent), so a daemon started
    without opting into autostart has no row in the autostart store even
    while it's genuinely running, and the grid's badge needs the live set
    instead. `engine_host.background_running_folders` is a dict
    comprehension plus a `Popen.poll()` per background child; no folder walk,
    no toml reads, so this stays cheap enough to call once per grid render."""
    folders = await asyncio.to_thread(engine_host.background_running_folders)
    return {"running": {folder: True for folder in folders}}
