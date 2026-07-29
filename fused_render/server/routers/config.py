import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from fused_render import __version__
from fused_render import calls as shell_calls
from fused_render.server.common import get_start_dir
from fused_render._view_url_codec import canonical_fs_path
from fused_render.shell import mounts as shell_mounts
from fused_render.shell import prefs as shell_prefs
from fused_render.shell.seed import fused_dir

router = APIRouter()


# Mount-health telemetry the Mounts panel polls: current per-mount state
# plus the auto-reconnect event log. A read — no X-Fused guard.
@router.get("/api/mounts/health")
def api_mounts_health():
    return shell_mounts.health_snapshot()

@router.get("/api/config")
def api_config(
    start_dir: str = Depends(get_start_dir),
    token: str | None = Header(default=None, alias="X-Fused-Desktop-Token"),
):
    from fused_render.paths import desktop_instance

    config = {
        "start_dir": start_dir,
        "home": os.path.expanduser("~"),
        # The Fused workspace dir (~/Documents/Fused, D81) — the sidebar's
        # "Fused" entry navigates here. Path only; the dir is created + seeded
        # at the process entry points (cli/app), not on this read.
        "fused_dir": fused_dir(),
        # The fused-render package version, surfaced in the sidebar brand.
        "version": __version__,
        # Which /api/run engine is in effect (D69/§20): "fused" | "builtin".
        # Read per request — it can change under the Preferences switch.
        "engine": shell_prefs.effective_engine(),
        # Root of the mounts dir (~/.fused-render/mounts). The rendered
        # page's auto-reload watcher (static/runtime.js) uses this to skip
        # watching mount-backed data files: they live on read-only remote
        # buckets that never change, so watching them buys nothing and every
        # poll is remote traffic — the stat storm that killed a mount in the
        # fs/events incident. Templates stay mount-agnostic; the skip lives
        # in runtime internals, keyed off this server-provided prefix.
        "mounts_root": os.path.abspath(shell_mounts.mounts_dir()),
        # Whether the builtin learn mount record exists (D123) — the
        # sidebar's Learn entry only renders when this is true, so it's
        # never a dead link (BUGBOT: an unpackaged run with no
        # FUSED_RENDER_LEARN_ZIP, or the brief window before the
        # background automount thread upserts the record on a packaged
        # run, would otherwise show a link to a path that doesn't exist).
        "learn_mount_ready": shell_mounts.learn_mount_ready(),
        # The call-log store (calls.py). Same job as `mounts_root` above and
        # for a sharper reason: a call-log file is APPENDED TO by the act of
        # viewing it, so a page watching one reloads, re-reads, appends, and
        # reloads again. Watching it is never useful either — the viewers that
        # want live updates (log_studio's Tail) poll instead, precisely so a
        # reload cannot rebuild the frame mid-poll. Keyed off this prefix +
        # suffix so generic templates (code, duckdb, tree) need to know
        # nothing about the call log.
        #
        # Canonicalized on the way out: `abspath` is backslashed on Windows
        # while every path the runtime holds is forward-slashed, so the
        # prefix test in `isCallLog` would never fire there. (`mounts_root`
        # above has the same shape and is deliberately left alone — changing
        # it would newly ENABLE an exclusion on Windows, which is a mount
        # behaviour change and belongs with the mount code, not here.)
        "calls_dir": canonical_fs_path(os.path.abspath(shell_calls.store_dir())),
        "calls_suffix": shell_calls.SUFFIX,
    }
    if instance := desktop_instance():
        config["desktop_instance"] = {"id": instance[0]}
        if token == instance[1]:
            config["desktop_instance"]["token"] = instance[1]
    return config

@router.post("/api/desktop/shutdown")
def api_desktop_shutdown(
    request: Request,
    token: str | None = Header(default=None, alias="X-Fused-Desktop-Token"),
):
    from fused_render.paths import desktop_instance

    instance = desktop_instance()
    if instance is None:
        raise HTTPException(status_code=404, detail="desktop supervisor is not active")
    if token != instance[1]:
        raise HTTPException(status_code=403, detail="invalid desktop supervisor token")
    uvicorn_server = getattr(request.app.state, "uvicorn_server", None)
    if uvicorn_server is None:
        raise HTTPException(status_code=503, detail="server shutdown is not ready")
    uvicorn_server.should_exit = True
    return {"ok": True}
