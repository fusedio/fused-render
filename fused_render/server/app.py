"""FastAPI app: static shell, filesystem API, HTML rendering, Python execution.

No path restriction anywhere — the whole filesystem is in scope by design
(see DECISIONS.md D2/D3). All `path` query params are absolute filesystem
paths. Endpoints are sync `def` so FastAPI dispatches them to its threadpool,
giving free concurrency for blocking filesystem/subprocess work; /api/run is
async (the fused engine is async; the built-in executor is offloaded).

Execution engine (D69/D70): /api/run runs the built-in executor by **default**,
whether or not the `fused` package is installed — set `FUSED_RENDER_ENGINE=auto`
(use fused if importable, else fall back) or `=fused` (require it — fail loudly
at startup if missing) to opt in to the local compute backend (`engine.py`).
"""

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from fused_render import calls as shell_calls
from fused_render.account import router as account_router
from fused_render.deploy import router as deploy_router
from fused_render.shell.bookmarks import router as bookmarks_router
from fused_render.shell.prefs import router as prefs_router
from fused_render.shell.recents import router as recents_router

from fused_render.server.ai import prewarm_ai, router as ai_router, shutdown_ai_session
from fused_render.server.common import (
    STATIC_DIR,
    close_pooled_client,
    no_cache_and_log,
    open_pooled_client,
    unhandled_exception,
    _forced_engine,
)
from fused_render.server.routers.app_resolve import router as app_resolve_router
from fused_render.server.routers.call import router as call_router
from fused_render.server.routers.config import router as config_router
from fused_render.server.routers.export import router as export_router
from fused_render.server.fs_mutate import router as fs_mutate_router
from fused_render.server.routers.fs_read import router as fs_read_router
from fused_render.server.routers.render import router as render_router
from fused_render.server.routers.run import router as run_router
from fused_render.server.session import router as session_router
from fused_render.server.routers.shell import router as shell_router
from fused_render.server.templates import TEMPLATES_DIR




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
    from fused_render.shell import mounts as shell_mounts
    from fused_render.shell import storage as shell_storage

    os.environ["FUSED_RENDER_HOME_DIR"] = shell_storage.home_dir()
    os.environ["FUSED_RENDER_MOUNTS_DIR"] = shell_mounts.mounts_dir()
    shell_mounts.export_ro_mounts_env()


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

    @app.on_event("shutdown")
    async def _startup_shutdown_ai():
        await shutdown_ai_session()

    app.exception_handler(Exception)(unhandled_exception)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Vendored JS libraries (marked, CodeMirror) that templates load by absolute
    # URL. Templates render at /render?path=… so a relative <script src> in a
    # template would resolve against /render, not the templates dir — hence a
    # dedicated absolute mount. Everything here is a committed local file: the
    # product has no network at runtime (no CDNs anywhere).
    app.mount(
        "/template-assets",
        StaticFiles(directory=os.path.join(TEMPLATES_DIR, "vendor")),
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
        StaticFiles(directory=os.path.join(TEMPLATES_DIR, "shared")),
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
    # GitHub deep links (SPEC §26, D110): GET /clone confirm page +
    # POST /api/clone sparse-clone into ~/Documents/Fused. deeplink.py never
    # imports server, so the include stays acyclic like shell/*.
    from fused_render.deeplink import router as deeplink_router

    app.include_router(deeplink_router)
    # Deploy (hosted publish through the fused CLI) — export + `fused share`
    # orchestration and the per-page deployment pointer store (deploy.py).
    app.include_router(deploy_router)
    # Fused account (in-app `fused cloud login/logout`, account.py) — the
    # sign-in the managed-env deploys need, without a terminal.
    app.include_router(account_router)
    # Template management (templates_api.py) — the Templates view backend:
    # inventory across sources, registry bindings edit, import/export. It owns
    # GET /api/templates/registry (the extended §2.2 shape). Imported here
    # (not at module top) because templates_api reads server helpers/dirs —
    # a lazy include keeps the server<->templates_api import acyclic.
    from fused_render.templates_api import router as templates_router

    app.include_router(templates_router)

    # Per-file session restore (LSN-*/_server_session.py), the fs read routes
    # (stat/conditions/list/walk/raw/events/reveal — _server_fs_read.py), the
    # fs mutation routes (write/mkdir/delete/rename/copy — _server_fs_mutate.py),
    # /render (_server_render.py), /api/run (_server_run.py), fused.ai
    # (_server_ai.py), and /api/export (_server_export.py).
    app.include_router(session_router)
    app.include_router(fs_read_router)
    app.include_router(fs_mutate_router)
    app.include_router(render_router)
    app.include_router(run_router)
    app.include_router(ai_router)
    app.include_router(export_router)
    # /api/app/resolve — nearest enclosing fused_app for fused.navigate()
    # (routers/app_resolve.py).
    app.include_router(app_resolve_router)
    # /call/<app dir> — fused_app .py pages as REST endpoints (routers/call.py).
    app.include_router(call_router)

    return app
