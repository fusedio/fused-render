"""Connectors — remote storage mounted as local paths via rclone.

A connector is a named mount instance: an rclone remote spec ("gdrive:" or
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

Store: home_dir()/connectors.json, whole-file last-write-wins like
shell/bookmarks.py. Same acyclic-router + X-Fused-guard conventions.
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
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
    return os.path.join(storage.home_dir(), "connectors.json")


def mounts_dir() -> str:
    return os.path.join(storage.home_dir(), "mounts")


def list_connectors() -> list:
    data = storage.read_json(_path())
    return data if isinstance(data, list) else []


def _write(connectors: list) -> None:
    storage.write_json(_path(), connectors)


def mountpoint(conn: dict) -> str:
    return os.path.join(mounts_dir(), conn["name"])


def add_connector(name: str, remote: str, automount: bool = False) -> dict:
    """Validate and persist a new connector; raises ValueError on bad input.
    Does NOT mount — the endpoint decides whether create implies mount."""
    name = (name or "").strip()
    remote = (remote or "").strip()
    if not name or any(ch in name for ch in "/\\:") or name.startswith("."):
        raise ValueError("name must be a plain folder-safe name")
    if ":" not in remote:
        raise ValueError(
            "remote must be an rclone spec like 'gdrive:' or 's3remote:bucket/prefix'"
        )
    connectors = list_connectors()
    if any(c["name"] == name for c in connectors):
        raise ValueError(f"a connector named '{name}' already exists")
    if any(c["remote"] == remote for c in connectors):
        raise ValueError(f"'{remote}' is already connected")
    conn = {"id": uuid.uuid4().hex[:12], "name": name, "remote": remote,
            "automount": bool(automount)}
    connectors.append(conn)
    _write(connectors)
    return conn


def get_connector(cid: str) -> dict | None:
    return next((c for c in list_connectors() if c["id"] == cid), None)


def remove_connector(cid: str) -> None:
    _write([c for c in list_connectors() if c["id"] != cid])


def set_automount(cid: str, enabled: bool) -> dict | None:
    connectors = list_connectors()
    conn = next((c for c in connectors if c["id"] == cid), None)
    if conn is not None:
        conn["automount"] = bool(enabled)
        _write(connectors)
    return conn


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


def ensure_rcd() -> int:
    """Port of a live rcd daemon, spawning one (detached) if none answers.
    Raises RuntimeError when rclone is not installed or the daemon won't come
    up."""
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


def mounted_paths() -> set:
    """Mountpoints rcd currently serves (empty when no daemon is live).
    Read-only: never spawns a daemon just to answer a status question."""
    port = _live_rcd_port()
    if port is None:
        return set()
    try:
        listed = _rc(port, "mount/listmounts").get("mountPoints", [])
    except RuntimeError:
        return set()
    return {m.get("MountPoint") for m in listed if isinstance(m, dict)}


def mount_connector(conn: dict) -> str | None:
    """Mount via rcd; returns an error string or None."""
    mp = mountpoint(conn)
    os.makedirs(mp, exist_ok=True)
    if os.path.ismount(mp):
        return None  # already mounted (double-click, adopted foreign mount)
    try:
        port = ensure_rcd()
        _rc(port, "mount/mount", {
            "fs": conn["remote"],
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


def unmount_connector(conn: dict) -> str | None:
    """Unmount via rcd; on failure ask the tile daemons to release their
    open files and retry once. Returns an error string or None. Never
    force-unmounts — failing loudly beats corrupted reads."""
    port = _live_rcd_port()
    if port is None:
        # No daemon: nothing rcd-owned to unmount. A foreign mount at the
        # path (pre-rcd prototype, manual rclone) is not ours to force.
        if os.path.ismount(mountpoint(conn)):
            return ("mounted outside the app (no rclone daemon running) — "
                    "unmount it from the terminal")
        return None
    params = {"mountPoint": mountpoint(conn)}
    try:
        _rc(port, "mount/unmount", params)
        return None
    except RuntimeError:
        pass
    _quit_tile_daemons()
    time.sleep(0.5)
    try:
        _rc(port, "mount/unmount", params)
        return None
    except RuntimeError as e:
        return f"unmount failed (a preview may still hold a file open): {e}"


def connector_view(conn: dict, rcd_mounts: set | None = None) -> dict:
    mp = mountpoint(conn)
    listed = mounted_paths() if rcd_mounts is None else rcd_mounts
    return {
        **conn,
        # Records written by the prototype predate the automount field.
        "automount": bool(conn.get("automount")),
        "mountpoint": mp,
        "mounted": mp in listed or os.path.ismount(mp),
    }


# ---------------------------------------------------------- automount/startup


def run_automount() -> None:
    """Mount every automount-flagged connector that isn't already mounted.
    Adoption is implicit: mount/listmounts is the status source of truth, so
    mounts that survived a server restart just show up. Best-effort — a
    failure logs and moves on, never blocks startup."""
    connectors = [c for c in list_connectors() if c.get("automount")]
    if not connectors:
        return
    live = mounted_paths()
    for conn in connectors:
        if mountpoint(conn) in live or os.path.ismount(mountpoint(conn)):
            continue
        err = mount_connector(conn)
        if err:
            logger.warning("automount of %r failed: %s", conn["name"], err)


def startup() -> None:
    """Called from create_app: automount in a daemon thread so a slow or
    missing rclone never delays server start."""
    import threading

    threading.Thread(target=run_automount, daemon=True, name="connectors-automount").start()


# ---------------------------------------------------------------- endpoints


def _rclone_state() -> dict:
    bin_ = rclone_bin()
    if not bin_:
        return {"available": False, "version": None, "remotes": []}
    try:
        version = subprocess.run(
            [bin_, "version"], capture_output=True, text=True, timeout=10
        ).stdout.splitlines()[0]
        remotes_out = subprocess.run(
            [bin_, "listremotes"], capture_output=True, text=True, timeout=10
        ).stdout
        remotes = [r.strip() for r in remotes_out.splitlines() if r.strip()]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return {"available": False, "version": None, "remotes": []}
    return {"available": True, "version": version, "remotes": remotes}


@router.get("/api/connectors")
def get_connectors():
    live = mounted_paths()
    return {
        "rclone": _rclone_state(),
        "connectors": [connector_view(c, live) for c in list_connectors()],
    }


@router.post("/api/connectors")
def create_connector(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        conn = add_connector(
            body.get("name") or "", body.get("remote") or "",
            automount=bool(body.get("automount")),
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    err = mount_connector(conn)
    if err:
        # Create implies mount; a connector that never mounted is not kept.
        remove_connector(conn["id"])
        return JSONResponse({"error": err}, status_code=502)
    return connector_view(conn)


@router.post("/api/connectors/{cid}/mount")
def mount_endpoint(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    conn = get_connector(cid)
    if conn is None:
        return JSONResponse({"error": "unknown connector"}, status_code=404)
    err = mount_connector(conn)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return connector_view(conn)


@router.post("/api/connectors/{cid}/unmount")
def unmount_endpoint(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    conn = get_connector(cid)
    if conn is None:
        return JSONResponse({"error": "unknown connector"}, status_code=404)
    err = unmount_connector(conn)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return connector_view(conn)


@router.put("/api/connectors/{cid}")
def update_connector(cid: str, body: dict = Body(...),
                     x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not isinstance(body.get("automount"), bool):
        return JSONResponse({"error": "'automount' must be a boolean"}, status_code=400)
    conn = set_automount(cid, body["automount"])
    if conn is None:
        return JSONResponse({"error": "unknown connector"}, status_code=404)
    return connector_view(conn)


@router.delete("/api/connectors/{cid}")
def delete_connector(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    conn = get_connector(cid)
    if conn is None:
        return JSONResponse({"error": "unknown connector"}, status_code=404)
    unmount_connector(conn)  # best-effort; delete proceeds regardless
    mp = mountpoint(conn)
    if os.path.isdir(mp) and not os.path.ismount(mp) and not os.listdir(mp):
        os.rmdir(mp)
    remove_connector(cid)
    return {"ok": True}


@router.post("/api/connectors/remotes")
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
