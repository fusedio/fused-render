import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from fused_render import __version__
from fused_render import calls as shell_calls
from fused_render.installed import installed_version
from fused_render.server import dirpicker
from fused_render.server.common import get_start_dir
from fused_render._view_url_codec import canonical_fs_path
from fused_render.shell import mounts as shell_mounts
from fused_render.shell import prefs as shell_prefs
from fused_render.shell import storage as shell_storage
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
        # Version installed on disk (bundle Info.plist), None when unpackaged.
        # Drifts from `version` after a DMG install replaces the bundle under
        # this still-running process — the shell then asks for an app restart.
        "installed_version": installed_version(),
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
        # Same deal for the builtin sessions mount (the Claude Sessions
        # sub-app): the sidebar's Sessions entry only renders when true.
        "sessions_mount_ready": shell_mounts.sessions_mount_ready(),
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
        # Root of the per-file sidecar subtree (~/.fused-render/sidecar,
        # D83-reversal — see shell/storage.py's sidecar_path). The runtime
        # (static/runtime.js) mirrors sidecar_path's mapping algorithm so
        # every template can compute a sidecar location client-side without a
        # round trip per lookup; only this root has to come from the server.
        # Canonicalized for the same reason as calls_dir above.
        "sidecar_root": canonical_fs_path(os.path.join(shell_storage.home_dir(), "sidecar")),
        # Whether POST /api/fs/pick-folder can raise a REAL OS folder dialog
        # here (server/dirpicker.py). A template asking the user where to write
        # something uses the native chooser when this is true and its own in-page
        # dialog when it is false — a hosted or headless deploy has no GUI
        # session to raise a modal into, and must never be left waiting on one
        # nobody can see. Read per request: on macOS the answer depends on
        # whether an AppKit run loop is up, which is a property of the process,
        # not of the build.
        "native_dir_picker": dirpicker.available(),
    }
    # Self-update state (update/mac.py) — present only when the mac app
    # started the manager; the shell shows the sidebar badge / install panel
    # off this. Rides this endpoint so the ServerStatusBanner poll carries it.
    from fused_render.update import mac as mac_update

    if (update_manager := mac_update.manager()) is not None:
        config["update"] = update_manager.status()
    if instance := desktop_instance():
        config["desktop_instance"] = {"id": instance[0]}
        if token == instance[1]:
            config["desktop_instance"]["token"] = instance[1]
    return config

@router.get("/api/desktop/ready")
def api_desktop_ready(
    token: str | None = Header(default=None, alias="X-Fused-Desktop-Token"),
):
    # Readiness probe (desktop_probe.matching_server): echoes only the launch token, touching no mounts/rcd, so a slow cold-start subsystem can't make the supervisor kill a healthy server.
    from fused_render.paths import desktop_instance

    instance = desktop_instance()
    if instance is None:
        return {}
    echo = {"id": instance[0]}
    if token == instance[1]:
        echo["token"] = instance[1]
    return {"desktop_instance": echo}

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
