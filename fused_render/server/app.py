"""FastAPI app: static shell, filesystem API, HTML rendering, Python execution.

No path restriction anywhere — the whole filesystem is in scope by design
(see DECISIONS.md D2/D3). All `path` query params are absolute filesystem
paths. Endpoints are sync `def` so FastAPI dispatches them to its threadpool,
giving free concurrency for blocking filesystem/subprocess work; /api/run is
async (the fused engine is async; the built-in executor is offloaded).

Execution engine (D69/D70/D204): /api/run follows the persisted preference,
which **defaults to the fused local compute backend** (`engine.py`) whenever the
`fused` package is importable and to the built-in executor otherwise — D204
reversed D70's builtin-by-default. `FUSED_RENDER_ENGINE` still overrides the
whole process: `=builtin` never touches the package even if importable, `=auto`
matches the default's behaviour, `=fused` requires it (fail loudly at startup if
missing).
"""

import asyncio
import json
import os
import threading
import time

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from fused_render import calls as shell_calls
from fused_render.canvases import router as canvases_router
from fused_render.shell.bookmarks import router as bookmarks_router
from fused_render.shell.prefs import router as prefs_router
from fused_render.shell.recents import router as recents_router

from fused_render.server.ai import prewarm_ai, router as ai_router, shutdown_ai_session
from fused_render.server.common import (
    STATIC_DIR,
    close_pooled_client,
    logger,
    no_cache_and_log,
    open_pooled_client,
    unhandled_exception,
    _forced_engine,
)
from fused_render.server.routers.apps import router as apps_router
from fused_render.server.routers.app_api import router as app_api_router
from fused_render.server.routers.background_apps import router as background_apps_router
from fused_render.server.routers.claude_artifacts import router as claude_artifacts_router
from fused_render.server.routers.claude_config import router as claude_config_router
from fused_render.server.routers.claude_health import router as claude_health_router
from fused_render.server.routers.claude_sessions import router as claude_sessions_router
from fused_render.server.routers.community import router as community_router
from fused_render.server.routers.clipboard import router as clipboard_router
from fused_render.server.routers.capture import router as capture_router
from fused_render.server.routers.config import router as config_router
from fused_render.server.routers.env import router as env_router
from fused_render.server.routers.export import router as export_router
from fused_render.server.fs_mutate import router as fs_mutate_router
from fused_render.server.routers.fs_read import router as fs_read_router
from fused_render.server.routers.git_repos import router as git_repos_router
from fused_render.server.routers.git_show import router as git_show_router
from fused_render.server.routers import index as index_routes
from fused_render.server.routers.jobs import router as jobs_router
from fused_render.server.routers.engines import router as engines_router
from fused_render.server.routers.ai_models import router as ai_models_router
from fused_render.server.routers.hf_auth import router as hf_auth_router
from fused_render.server.routers.hub_models import router as hub_models_router
from fused_render.server.routers.ai_runtime import router as ai_runtime_router
from fused_render.server.routers.ai_benchmark import router as ai_benchmark_router
from fused_render.server.routers.render import router as render_router
from fused_render.server.routers.run import router as run_router
from fused_render.server.routers.app_engine import router as app_engine_router
from fused_render.server.routers.schedule import router as schedule_router
from fused_render.server.routers.search import router as search_router
from fused_render.server.routers.shell import router as shell_router
from fused_render.server.routers.current_apps import router as current_apps_router
from fused_render.server.routers.tasks import router as tasks_router
from fused_render.server.routers.update import router as update_router
# The MODULE, not `from … import TEMPLATES_DIR`: that constant is a live seam
# (tests repoint it at a staged copy before calling create_app, and
# core_templates staging is the reason it can move at all), and a by-value
# re-binding here would freeze the asset mounts on the package directory no
# matter what the module says. Same class of bug as D178's `_STAT_CACHE_GEN`.
from fused_render.server import templates as _server_templates




def set_server_origin_env(port: int, host: str = "127.0.0.1") -> str:
    """Publish the server's ACTUAL bound origin so in-process runPython
    children read store bytes from the port the server is really on.

    The zarr_aoi tile daemon (and any other child that fetches bytes back
    through ``/api/fs/raw``) reads the origin from ``FUSED_RENDER_ORIGIN``.
    Without this, it falls back to ``_branch.branch_port()`` — the baseline
    default ``1777`` — which is wrong under any ``--port`` override (e.g. the
    desktop launcher's auto-picked free port), sending every read to a dead
    port and surfacing "No group found in store" from zarr. Set it before the
    server starts serving so every child process inherits the correct origin.
    """
    origin = f"http://{host}:{port}"
    os.environ["FUSED_RENDER_ORIGIN"] = origin
    return origin


def export_app_env() -> None:
    """Publish the resolved shell dirs so template children can find them
    WITHOUT importing ``fused_render`` (SPEC PY-15 / D166).

    Templates learn their environment through ``templates/shared/appenv.py``,
    which reads only env vars. That indirection exists because the fused local
    execution backend strips ``PYTHONPATH`` from child processes: a template's
    guarded ``from fused_render.shell.mounts import ...`` then silently takes its
    fallback branch and a mount-backed path gets treated as local. Env vars cross
    that boundary intact.

    Both values are exported ALREADY RESOLVED — ``home_dir()`` includes the
    per-branch nesting (``FUSED_RENDER_BRANCH``) and ``mounts_dir()`` is
    normpath'd — so no consumer re-implements those rules. Called from the same
    place as ``set_server_origin_env``, i.e. before the server starts serving, so
    every child process inherits them; the read-only mount list is exported
    separately by ``shell.mounts.export_ro_mounts_env`` because it has to be
    refreshed on every store write, not just at startup.
    """
    from fused_render import canvases
    from fused_render import skill_plugin
    from fused_render.shell import mounts as shell_mounts
    from fused_render.shell import seed as shell_seed
    from fused_render.shell import storage as shell_storage

    os.environ["FUSED_RENDER_HOME_DIR"] = shell_storage.home_dir()
    os.environ["FUSED_RENDER_MOUNTS_DIR"] = shell_mounts.mounts_dir()
    # Where app folders live — the claude template commits a finished turn
    # into the containing app's repo, and scopes that to this workspace.
    os.environ["FUSED_RENDER_WORKSPACE_DIR"] = shell_seed.fused_dir()
    shell_mounts.export_ro_mounts_env()
    # The skill plugin the chats we spawn are handed (D216). Here rather than in
    # a startup event because this is the export path: it assembles the root and
    # publishes it as one more FUSED_RENDER_* var for every child to inherit.
    # Keep it that way only while it stays filesystem-only — this line once also
    # ran `claude --help`, and blocking here blocks the socket bind, which the
    # desktop supervisor reads as a server that failed to start.
    skill_plugin.export_skill_plugin_env()
    # And the `workbench` plugin's canvas/UDF skills, if the app's own clone of
    # them is already on disk: a SECOND --plugin-dir, handed to CANVAS-clone
    # sessions only (the gate is in templates/claude/agent.py), so a canvas
    # clone's CLAUDE.md can name the canvas.toml format reference without the
    # app ever telling the user to go install something.
    #
    # Filesystem-only HERE, deliberately: this line runs before the socket bind,
    # and a git clone on a slow network would blow the desktop supervisor's
    # readiness budget — a server that failed to start. Fetching the clone is
    # the canvases path's job (`skill_plugin.sync_workbench_plugin`, called from
    # POST /api/canvases/clone); startup only publishes what already validated,
    # and "nothing there yet" is a normal outcome.
    skill_plugin.export_workbench_plugin_env()
    # Where canvas clones live. Exported so the templates can answer "is this
    # target a canvas clone?" (appenv.canvases_root) the same way the server
    # does — re-deriving it there would drift the instant either side changes,
    # and a wrong answer silently withholds the workbench skills from the one
    # session shape that needs them.
    os.environ["FUSED_RENDER_CANVASES_DIR"] = canvases.canvases_root()
    # The `fused` CLI wrapper the chats we spawn can run (D334): a wrapper
    # script under home_dir()/fused-bin goes on PATH and its dir is published
    # as one more FUSED_RENDER_* var, so a Claude session can `fused workbench
    # canvas push` against the same environment the canvases iframe shows.
    # Filesystem-only, like the skill plugin export above.
    from fused_render import fusedcli
    fusedcli.export_fused_cli_env()
    _export_bundled_uv_path()


def _server_json_path() -> str:
    from fused_render.shell import storage as shell_storage

    return os.path.join(shell_storage.home_dir(), "server.json")


def write_server_json(port: int, host: str = "127.0.0.1") -> None:
    """Publish this server's origin + the shared-template dir to
    `<home_dir()>/server.json`, for a process the server did NOT spawn (SPEC
    PY-19, D472) — `fused_ai.py`'s `resolve_origin()` reads it as the fallback
    below `FUSED_RENDER_ORIGIN`. A server child already inherits the env var
    (`set_server_origin_env`, right above); a user-launched app inherits
    nothing and cannot compute the port itself, since the desktop launcher
    auto-picks a free one and `_branch.branch_port()` is only right for a bare
    `fused-render` run.

    Written at the SAME lifecycle point as `set_server_origin_env`/
    `export_app_env` — before the server starts serving — into
    `shell.storage.home_dir()`, which is already branch-resolved, so a
    per-branch dev server writes its own file rather than colliding with
    another branch's.

    **Best-effort and non-fatal, like every other startup export here.** This
    runs before the socket bind; a write failure (a read-only home dir, a
    disk full) must never block or crash startup — the desktop supervisor
    reads a slow start as a failed one. A stale file from a crashed server is
    expected and is the CLIENT's problem to detect (a connect probe before
    trusting it), not something this side heartbeats.
    """
    try:
        import fused_render

        path = _server_json_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shared = os.path.join(
            os.path.dirname(os.path.abspath(fused_render.__file__)),
            "templates", "shared")
        payload = {
            "origin": f"http://{host}:{port}",
            "pid": os.getpid(),
            "shared": shared,
            "version": fused_render.__version__,
            "started": time.time(),
        }
        # Write-then-rename so a reader never observes a half-written file —
        # `resolve_origin()` may be polling this path from another process at
        # the same moment.
        tmp_path = path + f".{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except OSError:
        logger.warning("could not write server.json (non-fatal)", exc_info=True)


def remove_server_json() -> None:
    """Undo `write_server_json` at shutdown. Best-effort: a file that is
    already gone, or a home dir that went away underneath the server, is not
    a reason to raise from a shutdown handler.

    **Only ever removes a file THIS process wrote.** `write_server_json` is
    last-writer-wins into one branch-resolved path with no locking, and two
    servers on the same branch (different ports — the desktop app plus a
    manually-launched `fused-render serve --port 8001`) both write it. With
    no ownership check, whichever shuts down first deletes the file the
    survivor is still relying on, and every external `resolve_origin()` then
    raises `ServerNotRunning` while a server genuinely is. Read the file
    back and compare its recorded `pid` against this process's own before
    deleting — a read failure (also best-effort) or a pid mismatch means
    "not mine", and this is a no-op either way.
    """
    path = _server_json_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("pid") != os.getpid():
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _export_bundled_uv_path() -> None:
    """Put the bundled ``uv`` on PATH so template daemons can find it.

    Five templates build their daemon's venv with uv and resolve it as
    ``shutil.which("uv")`` — geotiff, zarr_aoi and netcdf's tile servers, plus
    las and pyramid. They have to resolve it that way: a template may ASK a fact
    about its environment but never branch on how the app was installed
    (SPEC §26/MD-11, D166), so "if this is a bundle, look in Contents/Resources"
    cannot live in a template file.

    The macOS bundle ships uv at ``Contents/Resources/bin/uv``, which is neither
    beside the interpreter nor on anyone's PATH — ``envinstall._worker_env()`` was
    the only thing that prepended it, and only for the install worker. So on a DMG
    with no user-installed uv every one of those five silently fell back to
    ``sys.executable``: geotiff lost ``imagecodecs``/``pyproj`` (LZW and JPEG
    tiles stop decoding), zarr_aoi lost ``s3fs``/``gcsfs``/``crc32c`` (every
    remote store fails to open), and las/pyramid raised advice a DMG user cannot
    act on. Before PEP 723 headers were dropped from those templates the script
    venv had incidentally supplied the deps, which is why this only surfaced now
    (D174 accepted a narrower version of it back when the DMG shipped no uv at
    all — it does now, so the premise is gone).

    Fixed here, once, rather than in five templates: the /api/run child and the
    daemon it spawns both inherit this process's environment, so prepending the
    directory makes every existing ``shutil.which("uv")`` start working with no
    template edit. It is also the SAME mechanism the Linux and Windows desktop
    supervisors already use — they prepend their payload's tools dir to the
    server's PATH (``supervisor/paths.py``) — so this closes the platform gap
    instead of adding a second pattern.

    Prepended, so the bundled uv wins over an older system one, matching
    ``_worker_env``. Resolution is ``envinstall.uv_bin()``'s, shared rather than
    restated: it already knows all three packaged layouts, and a second copy would
    drift. No uv anywhere is not an error here — a dev checkout without uv is
    normal, and the templates say so themselves when they need it.
    """
    from fused_render import envinstall

    uv = envinstall.uv_bin()
    if not uv:
        return
    uv_dir = os.path.dirname(os.path.abspath(uv))
    path = os.environ.get("PATH", "")
    if uv_dir in path.split(os.pathsep):
        return  # already reachable; do not grow PATH on every call
    os.environ["PATH"] = (uv_dir + os.pathsep + path) if path else uv_dir


def create_app(start_dir: str) -> FastAPI:
    # Engine (D69/D70 + SPEC §20): validate any FUSED_RENDER_ENGINE override
    # ONCE at startup — this raises on a bad value and fails loudly for
    # `=fused` when the package is missing, and logs the choice. Dispatch
    # itself goes through the single live resolver (`prefs.effective_engine`,
    # which re-reads the override + pref + availability per request), so the
    # Preferences switch and a mid-session install both apply with no restart
    # and the page's "running" label never drifts from what actually runs.
    _forced_engine()

    app = FastAPI(title="fused-render")
    app.state.start_dir = start_dir

    # Shared keep-alive HTTP pool for the opt-in pooled /api/fs/raw proxy
    # (TASK F), the fused.ai warm Claude session (D168/D169), and the
    # unhandled-exception/access-log middleware — bodies live in
    # _server_common.py / _server_ai.py; only the app-bound registration
    # stays here (an on_event hook needs the actual `app` it's attached to).
    @app.on_event("startup")
    async def _startup_pooled_client():
        await open_pooled_client(app)

    @app.on_event("shutdown")
    async def _shutdown_pooled_client():
        await close_pooled_client(app)

    @app.on_event("startup")
    async def _startup_prewarm_ai():
        prewarm_ai()

    # Warm the fused engine off the request path (else the first /api/config pays the cold import).
    @app.on_event("startup")
    async def _startup_warm_engine():
        from fused_render import engine

        engine.warm_unless_forced_builtin()

    # Background apps (background_apps.py): resurrect every autostart-opted-in
    # app's daemon at server startup. A daemon thread, not the create_app body
    # or an unthreaded await here — same D228 rationale as
    # _startup_sync_user_plugin below: each bring-up is a subprocess spawn
    # plus a bootstrap wait (BOOTSTRAP_TIMEOUT_S), nowhere near cheap enough
    # for the pre-bind path, and one folder's failure (dead manifest, project
    # venv not built, a spawn error) must never delay server readiness or the
    # other apps' bring-up — `resurrect_autostart` already logs-and-skips
    # those itself. Autostart is opt-in (D511): only paths explicitly present
    # in the autostart store come back here — a `start()` with no `autostart`
    # call never persisted anything and does NOT return at the next launch.
    #
    # `_background_apps_shutdown` is a per-app-instance Event (a local here,
    # not a module global): a bring-up only registers its child once `_spawn`
    # returns, up to BOOTSTRAP_TIMEOUT_S (120s) later, so a shutdown landing
    # mid-spawn would otherwise have `engine_host.stop_all()` walk a
    # `_children` that does not hold it yet, and the child would start
    # running unowned right after `stop_all()` already finished. Setting this
    # on shutdown lets `resurrect_autostart`'s own thread catch that — see its
    # docstring — for exactly the race a code review caught (2026-08-26).
    _background_apps_shutdown = threading.Event()

    @app.on_event("startup")
    async def _startup_resurrect_background_apps():
        from fused_render import background_apps

        threading.Thread(target=background_apps.resurrect_autostart,
                         args=(_background_apps_shutdown,),
                         name="background-apps-resurrect", daemon=True).start()

    @app.on_event("shutdown")
    async def _shutdown_background_apps_resurrection():
        _background_apps_shutdown.set()

    # The published `fusedio/fused-render` plugin, installed or refreshed in
    # the user's own Claude config (user_plugin.py, D492) — for sessions
    # fused-render did NOT launch, the user's own `claude` in a terminal or
    # their app folder, which `--plugin-dir` cannot reach. The ones we do launch
    # are handed the local plugin root instead (export_app_env above) and owe
    # nothing to this.
    #
    # `start()` and not the sync itself: it spawns `claude` and clones over the
    # network, so it belongs on a daemon thread and emphatically not on the
    # pre-bind path (D228). A startup event rather than the create_app body for
    # the usual reason — tests build apps without running lifespan, so they
    # never reach the user's real config.
    @app.on_event("startup")
    async def _startup_sync_user_plugin():
        from fused_render import user_plugin

        user_plugin.start()

    # Scheduled Claude messages (schedule.py). A startup event and emphatically
    # NOT the create_app body: this loop SENDS things, and its first tick fires
    # everything already overdue. Tests build the app without running lifespan,
    # so under the create_app body every test that constructs an app would spawn
    # whatever the developer's own store happened to hold.
    #
    # The first tick is also the catch-up pass — it is what sends a message that
    # came due while the app was closed — so nothing here waits for a due time
    # that has already gone by.
    @app.on_event("startup")
    async def _startup_schedule():
        from fused_render import schedule

        schedule.start()

    # The Tasks page's change signal (tasks_watch.py): a stat-poll thread over
    # Claude Code's live-session registry, prompt history and live transcripts.
    # A startup event for the same reason as `_startup_schedule`: it is a
    # thread for the life of the process that reads the user's real ~/.claude,
    # and tests build apps without lifespan.
    @app.on_event("startup")
    async def _startup_tasks_watch():
        from fused_render import tasks_watch

        tasks_watch.start()

    @app.on_event("shutdown")
    async def _startup_shutdown_ai():
        await shutdown_ai_session()

    # The idle-unload reaper (SPEC AI-13): unloads a resident local model once
    # nothing has used it for the configured window (default 10 min, 0 = off).
    # A startup event and deliberately not the create_app body, for the same
    # reason as `_startup_schedule` above: tests build apps with no lifespan,
    # and this starts a thread that lives for the process — building one per
    # test-constructed app would leak a thread per test.
    @app.on_event("startup")
    async def _startup_ai_idle_reaper():
        from fused_render.ai import supervisor

        supervisor.start_reaper()

    # GPU/VRAM detection (SPEC AI-18, D519): `hw_detect.detect_hardware` is a
    # subprocess probe (nvidia-smi/rocm-smi/PowerShell+registry/sysctl),
    # 50-500ms cold — the same cost `fit._wired_limit_mb` refuses on the
    # per-request verdict path, which is why `fit.py`/`speed.py` only ever
    # read `hw_detect.cached_hardware()`. Without this hook nothing ever
    # calls the probe, and both modules take their no-GPU-known branch
    # forever (code review, 2026-08-27) — a background daemon thread, same
    # shape as the idle reaper above, not the create_app body: it fires one
    # probe immediately and then re-probes every few hours for the rest of
    # the process's life.
    @app.on_event("startup")
    async def _startup_ai_hardware_refresh():
        from fused_render.ai import supervisor

        supervisor.start_hardware_refresh()

    # Hub-metadata pre-warming (code review finding 1, on top of SPEC AI-17):
    # `ai_runtime._accepts_image`/`_capability_tags` used to call
    # `hub_metadata.get(model_id)` — a synchronous `urllib` GET with an
    # 8-second timeout — straight from `describe_catalog`, a route the AI
    # Models picker polls. They now read `hub_metadata.cached()` only (a
    # plain disk read), and this background thread is the sole writer,
    # mirroring the hardware-refresh hook immediately above for the
    # identical reason.
    @app.on_event("startup")
    async def _startup_ai_hub_metadata_refresh():
        from fused_render.ai import supervisor

        supervisor.start_hub_metadata_refresh()

    # Local model workers die with the app. They hold GIGABYTES — a stranded one
    # is not a leaked file handle, it is a machine that has quietly lost 8GB of
    # memory to a process nothing on screen mentions any more.
    # Live native recordings are finalised on the way out (SPEC §45/CP-4): a
    # .mov whose `moov` atom was never written does not play, so a server that
    # stops while one is running must not just vanish. This covers the plain
    # `fused-render` server (Ctrl-C, uvicorn's own shutdown); the packaged app
    # never gets here — it exits via `os._exit` — so `app.quit_teardown` has a
    # "capture" rung of its own.
    # Undo `write_server_json` on an ordinary shutdown (Ctrl-C, uvicorn's own
    # stop). The packaged app's other exit path (`os._exit`) runs no shutdown
    # event at all — a stale file there is exactly the case `resolve_origin()`
    # is required to connect-probe before trusting, so it is a correctness gap
    # this side does not need to close.
    @app.on_event("shutdown")
    async def _shutdown_server_json():
        remove_server_json()

    @app.on_event("shutdown")
    async def _shutdown_captures():
        from fused_render import capture

        capture.stop_all()

    @app.on_event("shutdown")
    async def _shutdown_ai_workers():
        from fused_render.ai import supervisor

        supervisor.unload_all()

    # Every managed engine dies with the app: template daemons and /api/engine
    # warm workers alike (stop_all clears both).
    @app.on_event("shutdown")
    async def _shutdown_engines():
        from fused_render.server import engine_host

        engine_host.stop_all()

    # Reclaim project venvs whose source folder is gone (SPEC PY-16). Keying a
    # venv on the folder's path means moving or renaming a project orphans its
    # environment BY DESIGN — a moved project starts clean — so without this the
    # store grows by one full environment per rename and nothing ever reclaims
    # them. Startup, once, and off the event loop: it is a directory walk plus
    # rmtree over trees that can hold tens of thousands of files.
    #
    # Best-effort like every other startup chore here: a home dir that cannot be
    # listed is a disk problem, not a reason to refuse to serve.
    @app.on_event("startup")
    async def _startup_gc_project_venvs():
        from fused_render import projectenv

        try:
            removed = await asyncio.to_thread(projectenv.gc)
        except Exception:  # noqa: BLE001 - never block startup on housekeeping
            logger.exception("could not garbage-collect orphaned project venvs")
            return
        if removed:
            logger.info("reclaimed %d orphaned project venv(s)", removed)

    app.exception_handler(Exception)(unhandled_exception)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Vendored JS libraries (marked, CodeMirror) that templates load by absolute
    # URL. Templates render at /render?path=… so a relative <script src> in a
    # template would resolve against /render, not the templates dir — hence a
    # dedicated absolute mount. Everything here is a committed local file: the
    # product has no network at runtime (no CDNs anywhere).
    app.mount(
        "/template-assets",
        StaticFiles(directory=os.path.join(_server_templates.TEMPLATES_DIR, "vendor")),
        name="template-assets",
    )
    # First-party ESM shared by the sci preview templates (geotiff/netcdf
    # sciViz core — colormaps, stretch/stats/histogram, canvas draw helpers, UI
    # kit). Same absolute-URL rationale as /template-assets above. A dedicated
    # mount (rather than nesting under templates/vendor/) keeps vendor/ strictly
    # third-party; templates/shared/ has no template.html, so it can never be
    # resolved as a template name.
    app.mount(
        "/template-shared",
        StaticFiles(directory=os.path.join(_server_templates.TEMPLATES_DIR, "shared")),
        name="template-shared",
    )

    app.middleware("http")(no_cache_and_log)

    # React shell (D52/D54): built by Vite from frontend/ into static/
    # shell-dist/. The output is NOT committed — dev machines build it
    # themselves; wheels/DMG builds run it via the hatch hook
    # (scripts/hatch_build.py). Fail at startup with the fix, not with a
    # bare 404 on first page load.
    shell_path = os.path.join(STATIC_DIR, "shell-dist", "index.html")
    if not os.path.exists(shell_path):
        raise RuntimeError(
            "React shell not built (fused_render/static/shell-dist/ missing). "
            "Run: cd frontend && npm install && npm run build"
        )
    app.state.shell_path = shell_path

    app.include_router(shell_router)

    # Shell-specific state backends live in fused_render/shell/ (bookmarks,
    # prefs, recents), kept out of this module's fs/render internals.
    app.include_router(bookmarks_router)
    app.include_router(prefs_router)
    # Local-network sharing (lan.py): the desktop's pairing + device routes.
    # Loopback only in effect — the LAN wrapper's allowlist never forwards them.
    from fused_render.lan import router as lan_router

    app.include_router(lan_router)
    app.include_router(recents_router)
    # The app call log (calls.py): GET /api/calls/config + the page-error
    # event POST. The records themselves are written by the middleware above.
    app.include_router(shell_calls.router)
    # Mounts: remote storage mounted as local paths via rclone rcd
    # (shell/mounts.py). startup() remounts every mount in a background
    # thread; mounts deliberately survive server restarts.
    from fused_render.shell import mounts as shell_mounts
    from fused_render.shell import prefetch as shell_prefetch

    app.include_router(shell_mounts.router)
    # Full Disk Access nudge (shell/fda.py): open the Settings pane + persist
    # "Not now". The state itself rides /api/config's `fda` field.
    from fused_render.shell import fda as shell_fda

    app.include_router(shell_fda.router)
    shell_mounts.startup()
    # Background mount-health monitor (shell/mounts.py): polls every mount on a
    # timer, auto-reconnects a wedged/disconnected NFS mount ONCE per disconnect
    # episode, and records an event log the Mounts panel polls. Started AFTER
    # startup() so the automount thread owns the initial attach — the monitor
    # only acts on a later healthy->disconnected transition.
    shell_mounts.start_health_monitor()

    # Mount-health telemetry (api_mounts_health), /api/config, and
    # /api/desktop/shutdown — a generic app-info/control grab-bag that doesn't
    # map to any single fs/template/ai concern (_server_config.py).
    app.include_router(config_router)
    # Native screen / microphone / still capture (routers/capture.py, SPEC §45):
    # `fused.capture.*`. macOS-only today, and it says so in `sources()` rather
    # than by the routes being absent — a page must be able to ask.
    app.include_router(capture_router)
    # Self-update triggers (routers/update.py) — POSTs that kick a manifest
    # check / an install; both carry the D3 X-Fused guard and 404 unless the
    # mac app started the update manager.
    app.include_router(update_router)
    # Managed template engines (routers/engines.py): a template's daemon rides
    # this stable origin instead of its ephemeral port, and the routes heal a
    # dead child under the URLs the page holds (engine_host.py). The map
    # template's tile daemon is the first user.
    app.include_router(engines_router)
    # The Home view's apps backend (routers/apps.py): list workspace app
    # folders + scaffold new ones from the app starter kit.
    app.include_router(apps_router)
    # The app page's API tab (routers/app_api.py): every .py in one app folder
    # described by the api template's inspector, one request per folder.
    app.include_router(app_api_router)
    # Background apps (routers/background_apps.py): enable/disable/stop/
    # restart/status for a folder's declared long-running daemon, backed by
    # engine_host's "background" child kind + background_apps.py's enabled
    # store. See the startup resurrection hook below.
    app.include_router(background_apps_router)
    # Claude Code project folders for the Explorer homepage's "Claude
    # sessions" tab (routers/claude_sessions.py) — read-only, no auth guard.
    app.include_router(claude_sessions_router)
    # Artifacts published from those sessions, recovered from the same
    # transcripts (routers/claude_artifacts.py) — read-only, no auth guard.
    app.include_router(claude_artifacts_router)
    # Scheduled Claude messages (routers/schedule.py): the durable list, and the
    # POSTs that add to and cancel from it. The loop that SENDS them is started
    # as a startup event below, not here — see there.
    app.include_router(schedule_router)
    # Tasks (routers/tasks.py): the sessions above and the schedule above,
    # joined into one noun — a task IS a Claude session, and its thread is every
    # message that entered it, typed or scheduled. Reads are unguarded; the one
    # POST marks a message read, the same weight of change as the triage POST.
    app.include_router(tasks_router)
    # The Current apps desk (fused_render/current_apps.py): GET the table,
    # DELETE one app (archiving its tasks). Fed by the tasks listing above.
    app.include_router(current_apps_router)
    # Community marketplace backend for the /apps hub's Showcase tab and the
    # explorer preview's Clone button (routers/community.py).
    app.include_router(community_router)
    # Git repositories on this machine for the Explorer homepage's "Repos" tab
    # (routers/git_repos.py) — candidates come from the file index, never a
    # fresh walk, so it cannot touch a mount. Read-only, no auth guard.
    app.include_router(git_repos_router)
    # GET /api/git/show (routers/git_show.py): one file's bytes as of one commit,
    # resolved out of the object database with nothing written to disk. Backs the
    # git sidebar's revision selection — the runtime routes readFile/rawUrl/stat
    # here while a frame carries `_rev`. Read-only, no guard; it refuses a
    # mount-backed path outright, like every other git call in the app.
    app.include_router(git_show_router)
    # What the Hugging Face cache holds on this machine, for the sidebar's
    # "AI Models" page (routers/ai_models.py). The reads are unguarded;
    # its one destructive POST (delete a repo/revision) carries the D3 X-Fused
    # guard. It never downloads anything.
    app.include_router(ai_models_router)
    # The other half of that page: what the Hugging Face Hub has that this app
    # can actually run, joined to what this disk already holds
    # (routers/hub_models.py). It downloads nothing itself — the page hands a
    # result's `capability` to the runtime's download route — and it is the only
    # outbound request this feature makes. A separate module because "what is on
    # my disk" and "what is on the network" fail differently and share nothing
    # but the join.
    app.include_router(hub_models_router)
    # Signing this machine in to the Hub (routers/hf_auth.py, D402). The app
    # stores no token: the button drives huggingface_hub's own browser login and
    # hf persists what comes back, so this router holds a device flow and
    # nothing else. The read is unguarded and carries no credential — not even
    # the value of an environment variable that may be overriding hf's store.
    app.include_router(hf_auth_router)
    # Local inference (routers/ai_runtime.py, SPEC §40): which models this
    # machine is holding in memory, what they cost, and the load/unload/download
    # that change that. Reads unguarded; the three POSTs start processes and
    # write gigabytes, so they carry the D3 X-Fused guard.
    app.include_router(ai_runtime_router)
    # The AI Models page's Benchmark tab (routers/ai_benchmark.py, SPEC AI-14):
    # run a fixed per-capability workload against a local model and keep the
    # throughput/memory/load figures forever. The read is unguarded; the run and
    # the delete carry the D3 X-Fused guard — the first spends minutes of GPU
    # time, the second destroys measurements that cannot be recomputed for an
    # app version that has moved on.
    app.include_router(ai_benchmark_router)
    # Claude Code CONFIG editing for the Preferences page's "Claude config" tab
    # (routers/claude_config.py): one dispatch POST over the
    # fused_render/claude_config/ feature modules, plus a cheap availability
    # probe. Its POSTs mutate, so they carry the D3 X-Fused guard.
    app.include_router(claude_config_router)
    # Is Claude Code usable at all (routers/claude_health.py): found / version /
    # signed-in, so the first run can be TOLD rather than left to discover it by
    # failing. Same doctrine as /api/config's sessions_mount_ready, which gates
    # a link into a bundled mount so it is never dead — this is that gate for
    # everything Claude-dependent. Its own endpoint, not a /api/config field:
    # the facts behind it are process spawns, and /api/config is read on every
    # page load. The cache is warmed by the entry points (claude_health.
    # warm_in_background), never from here — importing the server in a test must
    # not spawn the user's login shell.
    app.include_router(claude_health_router)
    # GitHub deep links (SPEC §26, D110): GET /clone confirm page +
    # POST /api/clone sparse-clone into ~/Fused. deeplink.py never
    # imports server, so the include stays acyclic like shell/*.
    from fused_render.deeplink import router as deeplink_router

    app.include_router(deeplink_router)
    # .fused single-file app export/open (SPEC §43, D385-D390): GET
    # /api/appfile/export download + the internal POST /api/appfile/open the
    # fusedapp preview template calls (no user-facing open route).
    from fused_render.server.routers.appfile import router as appfile_router

    app.include_router(appfile_router)
    # Canvases (canvases.py) — local development on legacy-workbench canvases:
    # `fused login`, list/clone via the CLI, the folder-watch → `canvas push`
    # sync loop, and the access token the workspace iframe is seeded with.
    app.include_router(canvases_router)
    # Template management (templates_api.py) — the Templates view backend:
    # inventory across sources, registry bindings edit, import/export. It owns
    # GET /api/templates/registry (the extended §2.2 shape). Imported here
    # (not at module top) because templates_api reads server helpers/dirs —
    # a lazy include keeps the server<->templates_api import acyclic.
    from fused_render.templates_api import router as templates_router

    app.include_router(templates_router)

    # The fs read routes (stat/conditions/list/walk/raw/events/reveal —
    # _server_fs_read.py), the fs mutation routes
    # (write/mkdir/delete/rename/copy — _server_fs_mutate.py), /render
    # (_server_render.py), /api/run (_server_run.py), fused.ai (_server_ai.py),
    # and /api/export (_server_export.py). GET/PUT /api/session used to lead
    # this list; the per-file session restore it served is gone (D329).
    app.include_router(fs_read_router)
    app.include_router(search_router)
    app.include_router(fs_mutate_router)
    app.include_router(render_router)
    app.include_router(run_router)
    # The warm variant of /api/run (routers/app_engine.py): POST /api/engine
    # keeps the script's worker alive between calls. Opt-in via fused.engine().
    app.include_router(app_engine_router)
    # The script-venv install loader (routers/env.py): /api/env/install,
    # /api/env/progress, /api/env/cancel — what the page shell drives after
    # /api/run's pre-flight answers `needs_install` (PY-18 / D173).
    app.include_router(env_router)
    # The background-job registry (routers/jobs.py): /api/jobs — where a page's
    # long-running work (a model download, a generation) reports its progress,
    # and where the shell's download manager reads it back (SPEC §36 / D244).
    app.include_router(jobs_router)
    app.include_router(ai_router)
    app.include_router(export_router)
    # The OS clipboard bridge (routers/clipboard.py): /api/clipboard/files, the
    # local-machine seam that lets a Copy here paste in Finder/Explorer and a
    # copy there paste here (SPEC §3).
    app.include_router(clipboard_router)
    # The filesystem metadata index (fused_render/index/): scan control,
    # status polling, stats/lookup, and the in-folder corpus the explorer's
    # search reads. Engine-side there is no HTTP; this router is the adapter.
    app.include_router(index_routes.router)

    # Keep the index warm. The scan is a detached worker, so this hook only
    # spawns it — it cannot delay serving — and it debounces on the last scan
    # of each root, so a reload loop does not queue scan after scan. First boot
    # takes seconds over a whole home; while it runs, the explorer's search
    # falls back to the live walk with no error state (SPEC server-api.md §2).
    @app.on_event("startup")
    async def _startup_index_scan():
        await index_routes.startup_scan(start_dir)
        # ...and warm the corpus path the explorer's home search reads, on a
        # detached thread, so the gitignore sweep and the duckdb import are
        # paid at idle rather than by the user's first keystroke
        # (index/specs/server-api.md §4).
        index_routes.startup_warm()

    return app
