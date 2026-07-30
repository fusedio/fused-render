"""The /api/mounts/* FastAPI surface: CRUD, mount/unmount/reconnect,
restart, and remote-credential detection endpoints."""

import logging
import os
import re
import subprocess
import threading
import time

from fastapi.responses import JSONResponse
from fastapi import APIRouter, Body, Header

from .config import _require_fused
from .credentials import (
    CloudUrlError,
    _detected_credential_error,
    _rclone_state,
    resolve_cloud_url,
)
from .lifecycle import PROBE_TIMEOUT, attach_mount, mount_view, sync_serves
from .signing import _invalidate_upstream_caches
from .store import _ismount, add_mount, get_mount, mountpoint, remove_mount

logger = logging.getLogger(__name__)


router = APIRouter()


@router.get("/api/mounts/resolve")
def resolve_url_endpoint(url: str = ""):
    """Path bar support: turn a bucket URL into the local path under the mount
    that covers it. Read-only and side-effect free, so no X-Fused guard (same
    as GET /api/mounts)."""
    try:
        return {"path": resolve_cloud_url(url)}
    except CloudUrlError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)


@router.get("/api/mounts")
def get_mounts():
    from fused_render.shell.mounts import (
        _mount_credential_status,
        list_mounts,
        mount_state,
        mounted_paths,
        rclone_bin,
    )
    live = mounted_paths()
    mounts = list_mounts()
    bin_ = rclone_bin()
    # Probe states — AND, for a broken mount, its credential status — concurrently:
    # each disconnected/wedged mount blocks its state probe for the full
    # PROBE_TIMEOUT, and the credential `rclone lsd` can take ~10s more. Serially
    # (states threaded but the credential probe left to the mount_view loop) a
    # few broken aws mounts would stall the polled Mounts page for tens of
    # seconds — exactly when the user is polling to recover. So do BOTH in the
    # per-mount worker and hand the results to mount_view, which never probes.
    states: list[str | None] = [None] * len(mounts)
    cred_statuses: list[str] = ["n/a"] * len(mounts)
    threads = []
    for i, m in enumerate(mounts):
        def probe(i=i, m=m):
            st = mount_state(m, live)
            states[i] = st
            # Credentials only matter for a broken mount; a healthy/unmounted
            # one never pays the lsd probe.
            if st in ("disconnected", "stale"):
                cred_statuses[i] = _mount_credential_status(m, bin_)
        t = threading.Thread(target=probe, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        # Allow for the state probe PLUS the credential lsd (30s cap) so a slow
        # probe still lands in this listing rather than defaulting to unknown.
        t.join(PROBE_TIMEOUT + 31)
    return {
        "rclone": _rclone_state(),
        "mounts": [
            mount_view(m, live, state=s or "disconnected", cred_status=cs)
            for m, s, cs in zip(mounts, states, cred_statuses)
        ],
    }


@router.post("/api/mounts")
def create_mount(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        # An explicit read_only in the body wins over attach-time detection —
        # the caller knows their credentials better than the probe does.
        # add_mount validates it (strict bool or absent).
        m = add_mount(body.get("name") or "", body.get("remote") or "",
                      read_only=body.get("read_only"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    err = attach_mount(m)
    if err:
        # Create implies mount; a mount that never mounted is not kept.
        remove_mount(m["id"])
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.post("/api/mounts/{cid}/mount")
def mount_endpoint(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = attach_mount(m)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.post("/api/mounts/{cid}/reconnect")
def reconnect_endpoint(cid: str, x_fused: str | None = Header(default=None)):
    """Repair a disconnected mount: force-clear the dead mountpoint, remount."""
    from fused_render.shell.mounts import reconnect_mount
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    # Belt-and-braces like restart_endpoint below: reconnect_mount contracts to
    # return an error string, but it drives kernel unmounts and stat()s on a
    # mountpoint that is by definition broken — a surprise from down there
    # should read as a 502 on the Mounts page, never a raw 500 traceback.
    try:
        err = reconnect_mount(m)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.post("/api/mounts/restart")
def restart_endpoint(x_fused: str | None = Header(default=None, alias="X-Fused")):
    """Global recovery: restart the rcd daemon and re-mount everything. The one
    tool that fixes a stale-credential daemon (a fresh daemon re-reads refreshed
    keys) and applies changed mount params — see restart_rcd. Sync def so the
    multi-second unmount+kill+spawn+remount runs in the threadpool, never the
    event loop. Returns the same payload as GET /api/mounts."""
    from fused_render.shell.mounts import restart_rcd
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        restart_rcd()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return get_mounts()


@router.post("/api/mounts/{cid}/unmount")
def unmount_endpoint(cid: str, force: str = "0",
                     x_fused: str | None = Header(default=None)):
    from fused_render.shell.mounts import detach_mount
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = detach_mount(m, force=force == "1")
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.delete("/api/mounts/{cid}")
def delete_mount(cid: str, x_fused: str | None = Header(default=None)):
    from fused_render.shell.mounts import detach_mount
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    if m.get("builtin"):
        # BUGBOT: nothing stopped this — a deleted builtin record only
        # reappears at the next full SERVER restart (ensure_learn_mount runs
        # once, from run_automount at startup), while the already-open
        # Sidebar's learnMountReady state never rechecks once true, leaving
        # a dead Learn link for the rest of the session. Bundled read-only
        # content isn't something a user action should be able to
        # permanently remove out from under a running session anyway —
        # unmounting (POST .../unmount) still works to free the mountpoint.
        return JSONResponse(
            {"error": "this is a bundled default mount and can't be deleted"},
            status_code=400,
        )
    err = detach_mount(m)
    mp = mountpoint(m)
    if err and _ismount(mp):
        # Deleting the record while the filesystem is still mounted would
        # strand a live mount (and let a re-added name silently reuse it).
        return JSONResponse({"error": f"not deleted — {err}"}, status_code=502)
    if os.path.isdir(mp) and not _ismount(mp) and not os.listdir(mp):
        os.rmdir(mp)
    remove_mount(cid)
    sync_serves()  # stop the deleted mount's HTTP serve, drop its map entry
    _invalidate_upstream_caches()  # the gone remote's memoized facts mustn't linger
    return {"ok": True}


@router.post("/api/mounts/remotes")
def create_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Create an S3-compatible rclone remote non-interactively from keys.
    OAuth backends (Drive etc.) are deliberately NOT handled here — users run
    `rclone config` in a terminal; the page explains that. Credentials go
    straight into rclone's own config, never through the store."""
    from fused_render.shell.mounts import _rc, ensure_rcd, rclone_bin
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # Validate the request body (a 400 client error) BEFORE probing for rclone
    # (a 502 environment error): a bad name is bad on every platform, and where
    # rclone is absent (e.g. a macOS host with no bundled/PATH binary) the
    # availability check would otherwise mask the real 400 with a 502.
    name = (body.get("name") or "").strip()
    if not name or ":" in name or "/" in name:
        return JSONResponse({"error": "invalid remote name"}, status_code=400)
    bin_ = rclone_bin()
    if not bin_:
        return JSONResponse({"error": "rclone is not installed"}, status_code=502)
    p = body.get("params") or {}
    parameters = {
        "provider": p.get("provider") or "Other",
        "access_key_id": p.get("access_key_id") or "",
        "secret_access_key": p.get("secret_access_key") or "",
        "env_auth": "false",
    }
    if p.get("endpoint"):
        parameters["endpoint"] = p["endpoint"]
    if p.get("region"):
        parameters["region"] = p["region"]
    # Created through the rc daemon (JSON over loopback HTTP) rather than
    # `rclone config create` on argv: the latter would put the plaintext
    # secret_access_key in the process's command line, visible to any other
    # local user via `ps` (same rationale as the rcd auth secret in
    # _rcd_child_env).
    try:
        port = ensure_rcd()
        _rc(port, "config/create",
            {"name": name, "type": "s3", "parameters": parameters}, timeout=30)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)[-500:]}, status_code=502)
    _invalidate_upstream_caches()  # new/changed keys must be picked up without restart
    return {"ok": True, "name": name + ":"}


@router.post("/api/mounts/remotes/detect")
def create_detected_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Materialize a keyless rclone remote from an auto-detected credential
    source (see _credential_suggestions). The spec comes from the server's own
    detection keyed by `id` — never from client-supplied rclone params — and
    env_auth=true means no keys are written. Idempotent: an already-created
    remote is returned as-is — but a detected (env_auth) one is re-probed
    first, since its creds may have expired since creation."""
    from fused_render.shell.mounts import _credential_suggestions, rclone_bin
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bin_ = rclone_bin()
    if not bin_:
        return JSONResponse({"error": "rclone is not installed"}, status_code=502)
    sid = (body.get("id") or "").strip()
    sugg = next((s for s in _credential_suggestions() if s["id"] == sid), None)
    if sugg is None:
        return JSONResponse({"error": f"unknown credential source {sid!r}"}, status_code=404)
    name = sugg["remote_name"]
    # Public (anonymous) remotes carry no credentials to go stale; only the
    # detected, env_auth-backed ones get the validity probe (an rclone `lsd`).
    detected = sugg.get("kind", "detected") == "detected"
    # remotes are {name,label} objects now — match on the bare rclone spec.
    if any(r["name"] == f"{name}:" for r in _rclone_state().get("remotes", [])):
        # Idempotent re-entry: the remote already exists. Don't report it
        # healthy on faith — a detected remote's creds may have expired since
        # it was created, and returning {"ok": True} here would invite a doomed
        # mount just as surely as a freshly created stale one. Re-probe (one
        # `lsd`) so an expired detected remote is never reported ok; anonymous
        # remotes carry nothing that expires and return quickly.
        if detected:
            cred_err = _detected_credential_error(bin_, name)
            if cred_err:
                return JSONResponse({"error": cred_err}, status_code=502)
        return {"ok": True, "name": name + ":"}
    cmd = [bin_, "config", "create", name, sugg["backend"]]
    for k, v in sugg["params"].items():
        cmd += [k, v]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "rclone config create timed out (30s)"}, status_code=502)
    if r.returncode != 0:
        return JSONResponse({"error": (r.stderr or r.stdout or "").strip()[-500:]}, status_code=502)
    # A detected remote whose creds turn out expired is rolled back so the
    # broken thing doesn't linger under Remotes inviting doomed mounts.
    if detected:
        cred_err = _detected_credential_error(bin_, name)
        if cred_err:
            # Roll back the just-created remote. If the delete itself fails
            # (non-zero exit or an OSError/timeout) the remote may still exist,
            # so say so rather than returning the bare cred error as if cleanup
            # succeeded — a silently-lingering remote would be reported ok on
            # the next detect and re-invite the doomed mount.
            try:
                d = subprocess.run([bin_, "config", "delete", name],
                                   capture_output=True, text=True, timeout=30)
                removed = d.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                removed = False
            if not removed:
                cred_err += (" (the half-created remote could not be removed "
                             "automatically — delete it manually before "
                             "retrying)")
            return JSONResponse({"error": cred_err}, status_code=502)
    _invalidate_upstream_caches()  # new/changed keys must be picked up without restart
    return {"ok": True, "name": name + ":"}
