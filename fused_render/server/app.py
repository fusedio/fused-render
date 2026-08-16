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
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from fused_render import calls as shell_calls
from fused_render.account import router as account_router
from fused_render.deploy import router as deploy_router
from fused_render.shell.bookmarks import router as bookmarks_router
from fused_render.shell.prefs import router as prefs_router
from fused_render.shell.recents import router as recents_router
from fused_render.user_skills import sync_user_skills

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
from fused_render.server.routers.claude_artifacts import router as claude_artifacts_router
from fused_render.server.routers.claude_config import router as claude_config_router
from fused_render.server.routers.claude_sessions import router as claude_sessions_router
from fused_render.server.routers.community import router as community_router
from fused_render.server.routers.clipboard import router as clipboard_router
from fused_render.server.routers.config import router as config_router
from fused_render.server.routers.env import router as env_router
from fused_render.server.routers.export import router as export_router
from fused_render.server.fs_mutate import router as fs_mutate_router
from fused_render.server.routers.fs_read import router as fs_read_router
from fused_render.server.routers.git_repos import router as git_repos_router
from fused_render.server.routers.git_show import router as git_show_router
from fused_render.server.routers import index as index_routes
from fused_render.server.routers.jobs import router as jobs_router
from fused_render.server.routers.ai_models import router as ai_models_router
from fused_render.server.routers.ai_runtime import router as ai_runtime_router
from fused_render.server.routers.render import router as render_router
from fused_render.server.routers.run import router as run_router
from fused_render.server.routers.schedule import router as schedule_router
from fused_render.server.routers.search import router as search_router
from fused_render.server.session import router as session_router
from fused_render.server.routers.shell import router as shell_router
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
    _export_bundled_uv_path()


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

    # User-level skill sync (D185): install/refresh the canonical fused-render
    # skills in Claude Code's skills dir. Since D216 this is for sessions
    # fused-render did NOT launch — the user's own `claude` in their app folder
    # — because the ones it does launch are handed the plugin root instead
    # (export_app_env above). A startup event (not create_app body) on purpose
    # — tests build the app without running lifespan, so they never write
    # outside the redirected dirs.
    @app.on_event("startup")
    async def _startup_sync_user_skills():
        sync_user_skills()

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

    @app.on_event("shutdown")
    async def _startup_shutdown_ai():
        await shutdown_ai_session()

    # Local model workers die with the app. They hold GIGABYTES — a stranded one
    # is not a leaked file handle, it is a machine that has quietly lost 8GB of
    # memory to a process nothing on screen mentions any more.
    @app.on_event("shutdown")
    async def _shutdown_ai_workers():
        from fused_render.ai import supervisor

        supervisor.unload_all()

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
    # Self-update triggers (routers/update.py) — POSTs that kick a manifest
    # check / an install; both carry the D3 X-Fused guard and 404 unless the
    # mac app started the update manager.
    app.include_router(update_router)
    # The Home view's apps backend (routers/apps.py): list workspace app
    # folders + scaffold new ones from the app starter kit.
    app.include_router(apps_router)
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
    # Local inference (routers/ai_runtime.py, SPEC §40): which models this
    # machine is holding in memory, what they cost, and the load/unload/download
    # that change that. Reads unguarded; the three POSTs start processes and
    # write gigabytes, so they carry the D3 X-Fused guard.
    app.include_router(ai_runtime_router)
    # Claude Code CONFIG editing for the Preferences page's "Claude config" tab
    # (routers/claude_config.py): one dispatch POST over the
    # fused_render/claude_config/ feature modules, plus a cheap availability
    # probe. Its POSTs mutate, so they carry the D3 X-Fused guard.
    app.include_router(claude_config_router)
    # GitHub deep links (SPEC §26, D110): GET /clone confirm page +
    # POST /api/clone sparse-clone into ~/Documents/Fused. deeplink.py never
    # imports server, so the include stays acyclic like shell/*.
    from fused_render.deeplink import router as deeplink_router

    app.include_router(deeplink_router)
    # Cloning a DEPLOYED page (app_clone.py) — GET /api/clone-app/info previews a pasted
    # page URL, POST /api/clone-app downloads + validates + unpacks it into
    # ~/Documents/Fused. Distinct from deeplink's git clone above: no `.git`, no identity,
    # no update-in-place — every clone lands in a fresh folder. Like shell/*, it imports no
    # server module, so the include stays acyclic.
    from fused_render.app_clone import router as app_clone_router

    app.include_router(app_clone_router)
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
    app.include_router(search_router)
    app.include_router(fs_mutate_router)
    app.include_router(render_router)
    app.include_router(run_router)
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

    return app
