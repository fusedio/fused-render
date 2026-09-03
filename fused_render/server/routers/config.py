import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from fused_render import __version__
from fused_render import calls as shell_calls
from fused_render import selffix
from fused_render.installed import installed_version
from fused_render.server import dirpicker
from fused_render.server.common import get_start_dir
from fused_render._view_url_codec import canonical_fs_path
from fused_render.shell import fda as shell_fda
from fused_render.shell import mounts as shell_mounts
from fused_render.shell import prefs as shell_prefs
from fused_render.shell.storage import home_dir as shell_home_dir
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
        # The Fused workspace dir (~/Fused, D81) — the sidebar's
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
        # Where shell code may write SCRATCH files — bytes the app made and can
        # remake, never the user's own documents. `~/.fused-render/cache`, under
        # the same branch-aware home every other piece of shell state lives in
        # (storage.home_dir), so a worktree's leftovers are its own. It exists so
        # a surface that has to put bytes somewhere has ONE answer that is not
        # the user's home: the image playground's webcam captures are the first,
        # and a capture in `~/ai/images` — a folder the user browses, holding
        # renders — is a file nobody can tell from a generated one. Path only;
        # whoever writes there creates it, exactly as `fused_dir` above is a
        # path and not a mkdir on this read. Canonicalized for `calls_dir`'s
        # reason: every path above the OS in this app is forward-slashed.
        "cache_dir": canonical_fs_path(
            os.path.abspath(os.path.join(shell_home_dir(), "cache"))),
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
    # A Claude session changed this installation (selffix.py, SPEC §48) — the
    # sidebar's version chip turns amber and leads to the report. Rides this
    # endpoint rather than getting a poll of its own, like `update` above; it is
    # one small JSON read, and the PANEL's contents (report list, reinstall
    # instructions, which cost a directory walk and a brew probe) are a separate
    # GET /api/selffix the shell makes only when the chip is clicked.
    if (modified := selffix.status()) is not None:
        config["modified_install"] = modified
    # This installation cannot be written to, so a self-fix session here can only
    # DIAGNOSE (SPEC §48, SF-13). PRESENT ONLY WHEN READ-ONLY, like
    # `modified_install` above and for the same reason: the ordinary install is
    # one the user owns, and a field that is always there invites a truthiness
    # check that `{"read_only": False}` would silently pass.
    #
    # It rides /api/config — rather than the GET /api/selffix snapshot that also
    # reports it — because the download manager's failed rows need it to LABEL a
    # button before anyone clicks it, and that snapshot costs a directory walk
    # and a brew probe. This is one `os.access` call.
    if not selffix.writable():
        config["read_only"] = True
    # Full Disk Access nudge state (shell/fda.py) — present only on the
    # packaged mac app when the probe is conclusive; absent = render nothing.
    if (fda := shell_fda.snapshot()) is not None:
        config["fda"] = fda
    # First-run wizard flag (shell/onboarding.py) — always present; the shell
    # auto-shows the wizard while both timestamps are null.
    from fused_render.shell import onboarding as shell_onboarding

    config["onboarding"] = shell_onboarding.snapshot()
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
