"""Mounts — remote storage mounted as local paths via rclone.

A mount is a named remote-storage mount: an rclone remote spec ("gdrive:" or
"s3remote:bucket/prefix") plus a mountpoint under home_dir()/mounts/<name>.
Once mounted, the path flows through /api/fs/* and every reader untouched —
the app itself still only ever sees local absolute paths (D2/D3 reframed:
remoteness lives in the mount layer). Credentials live exclusively in
rclone's own config; this module stores none.

Mount lifecycle goes through `rclone rcd`, rclone's remote-control daemon,
over its local HTTP API (mount/mount, mount/unmount, mount/listmounts) —
one cross-platform mount API instead of per-OS umount commands. The daemon
is spawned detached with its {port, pid} recorded in home_dir()/rcd.json
and reused across server runs (the spawn-or-reuse pattern of the tile-server
daemons, templates/geotiff/tile_server.py). Mounts therefore deliberately
SURVIVE server restarts; a fresh server adopts them via mount/listmounts
instead of orphaning them. Unmount is an explicit user action.

Store: home_dir()/mounts.json, whole-file last-write-wins like
shell/bookmarks.py. Same acyclic-router + X-Fused-guard conventions.
"""
import configparser
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

logger = logging.getLogger(__name__)

router = APIRouter()

# The vfs options every mount gets, validated against a 204MB COG and a 362MB
# parquet (see DECISIONS): `full` caches read ranges as sparse files so warm
# reads are ~0.01s; max-age raised from the 1h default because eviction is
# what makes revisits slow again; read-ahead measured a net loss and left out.
VFS_OPT = {
    "CacheMode": "full",
    "ChunkSize": "8M",
    "ChunkSizeLimit": "64M",
    "CacheMaxAge": "24h",
    "FastFingerprint": True,
    "DirCacheTime": "30s",
}

# Tile-server daemon state files — the two parallel implementations that can
# hold files open under a mount (geotiff, and the grid server shared by
# zarr + netcdf). Unmount asks each to /quit before retrying (EBUSY fix).
DAEMON_STATE_FILES = (
    os.path.expanduser("~/.cache/fused-render-geotiff-v2/daemon.json"),
    os.path.expanduser("~/.cache/fused-render-gridv2/daemon.json"),
)


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to keep shell↛server
    # acyclic (see shell/bookmarks.py).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


# ------------------------------------------------------------------- store


def _path() -> str:
    p = os.path.join(storage.home_dir(), "mounts.json")
    # Prototype-era migration: rename a legacy connectors.json to mounts.json
    # the first time we read, so early users don't lose their entries.
    if not os.path.exists(p):
        legacy = os.path.join(storage.home_dir(), "connectors.json")
        if os.path.exists(legacy):
            try:
                os.rename(legacy, p)
            except OSError:
                pass
    return p


def mounts_dir() -> str:
    return os.path.join(storage.home_dir(), "mounts")


def list_mounts() -> list:
    data = storage.read_json(_path())
    return data if isinstance(data, list) else []


def _write(mounts: list) -> None:
    storage.write_json(_path(), mounts)


def mountpoint(m: dict) -> str:
    return os.path.join(mounts_dir(), m["name"])


def add_mount(name: str, remote: str, automount: bool = False) -> dict:
    """Validate and persist a new mount; raises ValueError on bad input.
    Does NOT mount — the endpoint decides whether create implies mount."""
    name = (name or "").strip()
    remote = (remote or "").strip()
    if not name or any(ch in name for ch in "/\\:") or name.startswith("."):
        raise ValueError("name must be a plain folder-safe name")
    if ":" not in remote:
        raise ValueError(
            "remote must be an rclone spec like 'gdrive:' or 's3remote:bucket/prefix'"
        )
    mounts = list_mounts()
    if any(c["name"] == name for c in mounts):
        raise ValueError(f"a mount named '{name}' already exists")
    if any(c["remote"] == remote for c in mounts):
        raise ValueError(f"'{remote}' is already connected")
    m = {"id": uuid.uuid4().hex[:12], "name": name, "remote": remote,
         "automount": bool(automount)}
    mounts.append(m)
    _write(mounts)
    return m


def get_mount(cid: str) -> dict | None:
    return next((c for c in list_mounts() if c["id"] == cid), None)


def remove_mount(cid: str) -> None:
    _write([c for c in list_mounts() if c["id"] != cid])


def set_automount(cid: str, enabled: bool) -> dict | None:
    mounts = list_mounts()
    m = next((c for c in mounts if c["id"] == cid), None)
    if m is not None:
        m["automount"] = bool(enabled)
        _write(mounts)
    return m


# -------------------------------------------------------------- rcd client


def _rcd_state_path() -> str:
    return os.path.join(storage.home_dir(), "rcd.json")


def write_rcd_state(port: int, pid: int) -> None:
    storage.write_json(_rcd_state_path(), {"port": port, "pid": pid})


def _rc(port: int, method: str, params: dict | None = None, timeout: float = 30):
    """One rc call. Returns the decoded JSON on 200; raises RuntimeError with
    rclone's error message on any failure."""
    raw = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{method}",
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            detail = ""
        raise RuntimeError(detail or f"rclone rc {method}: HTTP {e.code}") from e
    except OSError as e:
        raise RuntimeError(f"rclone rc {method}: {e}") from e


def _live_rcd_port() -> int | None:
    """The recorded daemon's port iff it answers core/pid; never spawns."""
    state = storage.read_json(_rcd_state_path())
    if not isinstance(state, dict) or not state.get("port"):
        return None
    try:
        _rc(state["port"], "core/pid", timeout=3)
    except RuntimeError:
        return None
    return state["port"]


def rclone_bin() -> str | None:
    return shutil.which("rclone")


# Spawn-or-reuse must be serialized: the startup automount thread and a user
# mount request can race between "no live daemon" and Popen, each starting
# its own rcd and clobbering rcd.json (the loser's daemon is orphaned).
_rcd_lock = threading.Lock()


def ensure_rcd() -> int:
    """Port of a live rcd daemon, spawning one (detached) if none answers.
    Raises RuntimeError when rclone is not installed or the daemon won't come
    up."""
    with _rcd_lock:
        return _ensure_rcd_locked()


def _ensure_rcd_locked() -> int:
    port = _live_rcd_port()
    if port is not None:
        return port
    bin_ = rclone_bin()
    if not bin_:
        raise RuntimeError("rclone is not installed")
    # Pick the port ourselves (parsing rcd's stderr for a :0 bind is brittle).
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.Popen(
        [bin_, "rcd", "--rc-no-auth", f"--rc-addr=127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # outlives this server on purpose
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            pid = _rc(port, "core/pid", timeout=2).get("pid", 0)
            write_rcd_state(port, pid)
            return port
        except RuntimeError:
            time.sleep(0.2)
    raise RuntimeError("rclone rcd did not come up within 10s")


def rcd_mount_map() -> dict:
    """{mountpoint: remote fs} for every mount rcd currently serves (empty
    when no daemon is live). Read-only: never spawns a daemon just to answer
    a status question."""
    port = _live_rcd_port()
    if port is None:
        return {}
    try:
        listed = _rc(port, "mount/listmounts").get("mountPoints", [])
    except RuntimeError:
        return {}
    return {m.get("MountPoint"): m.get("Fs") for m in listed if isinstance(m, dict)}


def mounted_paths() -> set:
    return set(rcd_mount_map())


def attach_mount(m: dict) -> str | None:
    """Mount via rcd; returns an error string or None."""
    mp = mountpoint(m)
    os.makedirs(mp, exist_ok=True)
    if os.path.ismount(mp):
        # Already a kernel mount — but is it OURS? A stale mount left by a
        # deleted mount of the same name would otherwise pass for the
        # new remote. rcd knows the fs of every mount it serves; a mismatch
        # is an error, not a silent adopt. (A mount rcd doesn't know about
        # has no queryable fs — adopted as-is, the pre-rcd prototype case.)
        fs = rcd_mount_map().get(mp)
        if fs is not None and fs != m["remote"]:
            return (f"mountpoint already serves '{fs}' — unmount it before "
                    f"mounting '{m['remote']}'")
        return None  # already mounted (double-click, adopted foreign mount)
    try:
        port = ensure_rcd()
        _rc(port, "mount/mount", {
            "fs": m["remote"],
            "mountPoint": mp,
            "mountType": "nfsmount" if sys.platform == "darwin" else "mount",
            "vfsOpt": VFS_OPT,
        }, timeout=60)
    except RuntimeError as e:
        return str(e)
    return None


def _quit_tile_daemons() -> None:
    """Best-effort /quit to every live tile-server daemon so they release
    open files under the mount (measured EBUSY cause). Absent/corrupt state
    files and dead ports are skipped silently."""
    for state_file in DAEMON_STATE_FILES:
        state = storage.read_json(state_file)
        if not isinstance(state, dict) or not state.get("port"):
            continue
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{state['port']}/quit", timeout=3).read()
        except OSError:
            continue


def detach_mount(m: dict) -> str | None:
    """Unmount via rcd; on failure ask the tile daemons to release their
    open files and retry once. Returns an error string or None. Never
    force-unmounts — failing loudly beats corrupted reads."""
    port = _live_rcd_port()
    if port is None:
        # No daemon: nothing rcd-owned to unmount. A foreign mount at the
        # path (pre-rcd prototype, manual rclone) is not ours to force.
        if os.path.ismount(mountpoint(m)):
            return ("mounted outside the app (no rclone daemon running) — "
                    "unmount it from the terminal")
        return None
    params = {"mountPoint": mountpoint(m)}
    try:
        _rc(port, "mount/unmount", params)
        return None
    except RuntimeError as e:
        # Quitting the tile daemons only helps when the failure is an
        # open-file busy error ("resource busy", "device busy" — macOS and
        # Linux both say "busy"); on any other failure quitting them would
        # tear down previews of unrelated LOCAL files for nothing.
        if "busy" not in str(e).lower():
            return f"unmount failed: {e}"
    _quit_tile_daemons()
    time.sleep(0.5)
    try:
        _rc(port, "mount/unmount", params)
        return None
    except RuntimeError as e:
        return f"unmount failed (a preview may still hold a file open): {e}"


def mount_view(m: dict, rcd_mounts: set | None = None) -> dict:
    mp = mountpoint(m)
    listed = mounted_paths() if rcd_mounts is None else rcd_mounts
    return {
        **m,
        # Records written by the prototype predate the automount field.
        "automount": bool(m.get("automount")),
        "mountpoint": mp,
        "mounted": mp in listed or os.path.ismount(mp),
    }


# ---------------------------------------------------------- automount/startup


def run_automount() -> None:
    """Mount every automount-flagged mount that isn't already mounted.
    Adoption is implicit: mount/listmounts is the status source of truth, so
    mounts that survived a server restart just show up. Best-effort — a
    failure logs and moves on, never blocks startup."""
    mounts = [c for c in list_mounts() if c.get("automount")]
    if not mounts:
        return
    live = mounted_paths()
    for m in mounts:
        if mountpoint(m) in live or os.path.ismount(mountpoint(m)):
            continue
        err = attach_mount(m)
        if err:
            logger.warning("automount of %r failed: %s", m["name"], err)


def startup() -> None:
    """Called from create_app: automount in a daemon thread so a slow or
    missing rclone never delays server start."""
    threading.Thread(target=run_automount, daemon=True, name="mounts-automount").start()


# ---------------------------------------------------------------- endpoints


# --------------------------------------------------- credential auto-detection
# Rather than re-entering keys, surface credentials the user already has in the
# usual dotfiles as ready-to-mount remotes. Each materializes (on first use,
# via /remotes/detect) into a *keyless* rclone remote — env_auth=true, so
# rclone resolves the real credentials from the environment at mount time and
# nothing sensitive is ever written to rclone's config or to this store.


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "profile"


def _aws_profiles() -> list[str]:
    """Profile names from ~/.aws/credentials and ~/.aws/config, honoring the
    AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE overrides. ~/.aws/config
    names non-default profiles '[profile foo]'; credentials uses bare '[foo]'."""
    names: set[str] = set()
    for path, is_config in (
        (os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or "~/.aws/credentials", False),
        (os.environ.get("AWS_CONFIG_FILE") or "~/.aws/config", True),
    ):
        parser = configparser.RawConfigParser()
        try:
            parser.read(os.path.expanduser(path))
        except (configparser.Error, OSError):
            continue
        for section in parser.sections():
            if is_config and section.startswith("profile "):
                names.add(section[len("profile "):].strip())
            else:
                names.add(section.strip())
    return sorted(n for n in names if n)


def _credential_suggestions() -> list[dict]:
    """Remotes offerable from already-present credentials. Full specs (rclone
    backend + params) — the endpoint consumes these; the API view (below)
    exposes only id/label/remote_name."""
    out: list[dict] = []
    for prof in _aws_profiles():
        out.append({
            "id": f"aws-profile:{prof}",
            "label": f"AWS S3 — {prof} profile",
            "remote_name": "aws" if prof == "default" else f"aws-{_slug(prof)}",
            "backend": "s3",
            "params": {"provider": "AWS", "env_auth": "true", "profile": prof},
        })
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        out.append({
            "id": "aws-env",
            "label": "AWS S3 — environment credentials",
            "remote_name": "aws-env",
            "backend": "s3",
            "params": {"provider": "AWS", "env_auth": "true"},
        })
    if os.path.exists(os.path.expanduser(
            "~/.config/gcloud/application_default_credentials.json")):
        out.append({
            "id": "gcs-adc",
            "label": "Google Cloud Storage — application default credentials",
            "remote_name": "gcs",
            "backend": "google cloud storage",
            "params": {"env_auth": "true"},
        })
    return out


def _suggestions_view(remotes: list[str]) -> list[dict]:
    """Public shape, minus any suggestion already materialized as a remote."""
    return [
        {"id": s["id"], "label": s["label"], "remote_name": s["remote_name"]}
        for s in _credential_suggestions()
        if f'{s["remote_name"]}:' not in remotes
    ]


def _rclone_state() -> dict:
    bin_ = rclone_bin()
    if not bin_:
        return {"available": False, "version": None, "remotes": [], "suggested": []}
    try:
        version = subprocess.run(
            [bin_, "version"], capture_output=True, text=True, timeout=10
        ).stdout.splitlines()[0]
        remotes_out = subprocess.run(
            [bin_, "listremotes"], capture_output=True, text=True, timeout=10
        ).stdout
        remotes = [r.strip() for r in remotes_out.splitlines() if r.strip()]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return {"available": False, "version": None, "remotes": [], "suggested": []}
    return {"available": True, "version": version, "remotes": remotes,
            "suggested": _suggestions_view(remotes)}


@router.get("/api/mounts")
def get_mounts():
    live = mounted_paths()
    return {
        "rclone": _rclone_state(),
        "mounts": [mount_view(c, live) for c in list_mounts()],
    }


@router.post("/api/mounts")
def create_mount(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        m = add_mount(
            body.get("name") or "", body.get("remote") or "",
            automount=bool(body.get("automount")),
        )
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


@router.post("/api/mounts/{cid}/unmount")
def unmount_endpoint(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = detach_mount(m)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.put("/api/mounts/{cid}")
def update_mount(cid: str, body: dict = Body(...),
                 x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not isinstance(body.get("automount"), bool):
        return JSONResponse({"error": "'automount' must be a boolean"}, status_code=400)
    m = set_automount(cid, body["automount"])
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    return mount_view(m)


@router.delete("/api/mounts/{cid}")
def delete_mount(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = detach_mount(m)
    mp = mountpoint(m)
    if err and os.path.ismount(mp):
        # Deleting the record while the filesystem is still mounted would
        # strand a live mount (and let a re-added name silently reuse it).
        return JSONResponse({"error": f"not deleted — {err}"}, status_code=502)
    if os.path.isdir(mp) and not os.path.ismount(mp) and not os.listdir(mp):
        os.rmdir(mp)
    remove_mount(cid)
    return {"ok": True}


@router.post("/api/mounts/remotes")
def create_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Create an S3-compatible rclone remote non-interactively from keys.
    OAuth backends (Drive etc.) are deliberately NOT handled here — users run
    `rclone config` in a terminal; the page explains that. Credentials go
    straight into rclone's own config, never through the store."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bin_ = rclone_bin()
    if not bin_:
        return JSONResponse({"error": "rclone is not installed"}, status_code=502)
    name = (body.get("name") or "").strip()
    if not name or ":" in name or "/" in name:
        return JSONResponse({"error": "invalid remote name"}, status_code=400)
    p = body.get("params") or {}
    cmd = [
        bin_, "config", "create", name, "s3",
        "provider", p.get("provider") or "Other",
        "access_key_id", p.get("access_key_id") or "",
        "secret_access_key", p.get("secret_access_key") or "",
        "env_auth", "false",
    ]
    if p.get("endpoint"):
        cmd += ["endpoint", p["endpoint"]]
    if p.get("region"):
        cmd += ["region", p["region"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "rclone config create timed out (30s)"}, status_code=502)
    if r.returncode != 0:
        return JSONResponse({"error": (r.stderr or r.stdout or "").strip()[-500:]}, status_code=502)
    return {"ok": True, "name": name + ":"}


@router.post("/api/mounts/remotes/detect")
def create_detected_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Materialize a keyless rclone remote from an auto-detected credential
    source (see _credential_suggestions). The spec comes from the server's own
    detection keyed by `id` — never from client-supplied rclone params — and
    env_auth=true means no keys are written. Idempotent: an already-created
    remote is returned as-is."""
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
    if f"{name}:" in _rclone_state().get("remotes", []):
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
    return {"ok": True, "name": name + ":"}
