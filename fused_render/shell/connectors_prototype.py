"""PROTOTYPE — /api/connectors: remote storage mounted as local paths via rclone.

THROWAWAY CODE answering one question: can fused-render treat rclone-managed
remote mounts (Google Drive, S3-compatible) as ordinary local paths, with the
app owning the mount lifecycle, while the rest of the system (fs endpoints,
readers, tile servers) stays byte-identical "local only"?

Design (prototype-quality, not production):
  * A connector = {id, name, remote} where `remote` is an rclone remote spec
    ("mydrive:" or "s3remote:bucket/prefix"). Credentials live entirely in
    rclone's own config (~/.config/rclone/rclone.conf) — the app stores none.
  * Mountpoint: ~/.fused-render/mounts/<name>. Once mounted, the path flows
    through /api/fs/* and every reader untouched — that's the whole point.
  * macOS: `rclone nfsmount` (built-in NFS client, no macFUSE kext).
    Elsewhere: `rclone mount` (FUSE).
  * Mount processes are tracked in-memory only; atexit unmounts everything.
    A server restart orphans nothing (nfsmount dies with its parent), but
    persisted connectors simply show as unmounted after restart.
  * S3 remotes can be created in-app (non-interactive `rclone config create`).
    Google Drive uses rclone's own OAuth browser dance, spawned from here.

Same acyclic-router + X-Fused-guard conventions as shell/bookmarks.py.
"""
import atexit
import os
import shutil
import subprocess
import sys
import time
import uuid

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

router = APIRouter()

# id -> subprocess.Popen of the live rclone mount process
_procs: dict[str, subprocess.Popen] = {}


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "connectors.json")


def _mounts_dir() -> str:
    d = os.path.join(storage.home_dir(), "mounts")
    os.makedirs(d, exist_ok=True)
    return d


def _read() -> list:
    data = storage.read_json(_path())
    return data if isinstance(data, list) else []


def _write(connectors: list) -> None:
    storage.write_json(_path(), connectors)


# ------------------------------------------------------------------ rclone


def _rclone_bin() -> str | None:
    return shutil.which("rclone")


def _rclone_state() -> dict:
    bin_ = _rclone_bin()
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


def _mountpoint(conn: dict) -> str:
    return os.path.join(_mounts_dir(), conn["name"])


def _is_mounted(mountpoint: str) -> bool:
    return os.path.ismount(mountpoint)


def _mount(conn: dict) -> str | None:
    """Spawn the rclone mount for `conn`; return an error string or None."""
    bin_ = _rclone_bin()
    if not bin_:
        return "rclone is not installed"
    mp = _mountpoint(conn)
    os.makedirs(mp, exist_ok=True)
    if _is_mounted(mp):
        return None  # already mounted (e.g. double-click)
    subcmd = "nfsmount" if sys.platform == "darwin" else "mount"
    cmd = [
        bin_, subcmd, conn["remote"], mp,
        # `full` caches read ranges as sparse files: measured on a 204MB COG
        # and a 362MB parquet, cold first-reads are network-bound either way,
        # but every later read of a touched region is ~0.01s. The cache
        # survives remounts/restarts, so max-age is raised from the 1h
        # default — eviction is what makes "slow again the next morning".
        # (--vfs-read-ahead measured as a net LOSS here: it slows the parquet
        # footer read and wastes bandwidth on ranges nothing asks for.)
        "--vfs-cache-mode", "full",
        "--vfs-read-chunk-size", "8M",
        "--vfs-read-chunk-size-limit", "64M",
        "--vfs-cache-max-age", "168h",
        "--vfs-fast-fingerprint",
        "--dir-cache-time", "30s",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    # Poll until the kernel reports a mount (nfsmount takes a moment to
    # start its NFS server and self-mount).
    deadline = time.time() + 15
    while time.time() < deadline:
        if _is_mounted(mp):
            _procs[conn["id"]] = proc
            return None
        if proc.poll() is not None:
            err = (proc.stderr.read() or "").strip() if proc.stderr else ""
            return f"rclone exited ({proc.returncode}): {err[-500:] or 'no output'}"
        time.sleep(0.3)
    proc.terminate()
    return "timed out waiting for mount to appear (15s)"


def _unmount(conn: dict) -> str | None:
    mp = _mountpoint(conn)
    if _is_mounted(mp):
        umount = ["umount", mp] if sys.platform == "darwin" else ["fusermount", "-u", mp]
        r = subprocess.run(umount, capture_output=True, text=True, timeout=15)
        if r.returncode != 0 and _is_mounted(mp):
            # Common cause: a tile-server daemon still holds a file open
            # (EBUSY). Measured: quitting the daemon makes plain umount
            # succeed. Prototype answer: force it; a real feature should ask
            # daemons to release first.
            force = (
                ["umount", "-f", mp] if sys.platform == "darwin" else ["fusermount", "-uz", mp]
            )
            r2 = subprocess.run(force, capture_output=True, text=True, timeout=15)
            if r2.returncode != 0 and _is_mounted(mp):
                return (
                    "umount failed (a preview/tile server may still hold a file open): "
                    + (r.stderr or r2.stderr or "").strip()
                )
    proc = _procs.pop(conn["id"], None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return None


def _unmount_all() -> None:
    for conn in _read():
        try:
            _unmount(conn)
        except Exception:
            pass


atexit.register(_unmount_all)


def _view(conn: dict) -> dict:
    mp = _mountpoint(conn)
    return {**conn, "mountpoint": mp, "mounted": _is_mounted(mp)}


# ---------------------------------------------------------------- endpoints


@router.get("/api/connectors")
def get_connectors():
    return {"rclone": _rclone_state(), "connectors": [_view(c) for c in _read()]}


@router.post("/api/connectors")
def create_connector(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = (body.get("name") or "").strip()
    if not name or any(ch in name for ch in "/\\:") or name.startswith("."):
        return JSONResponse({"error": "name must be a plain folder-safe name"}, status_code=400)
    connectors = _read()
    if any(c["name"] == name for c in connectors):
        return JSONResponse({"error": f"a connector named '{name}' already exists"}, status_code=400)
    remote = (body.get("remote") or "").strip()
    if ":" not in remote:
        return JSONResponse({"error": "remote must be an rclone spec like 'gdrive:' or 's3:bucket/prefix'"}, status_code=400)
    conn = {"id": uuid.uuid4().hex[:12], "name": name, "remote": remote}
    err = _mount(conn)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    connectors.append(conn)
    _write(connectors)
    return _view(conn)


@router.post("/api/connectors/{cid}/mount")
def mount_connector(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    conn = next((c for c in _read() if c["id"] == cid), None)
    if conn is None:
        return JSONResponse({"error": "unknown connector"}, status_code=404)
    err = _mount(conn)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return _view(conn)


@router.post("/api/connectors/{cid}/unmount")
def unmount_connector(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    conn = next((c for c in _read() if c["id"] == cid), None)
    if conn is None:
        return JSONResponse({"error": "unknown connector"}, status_code=404)
    err = _unmount(conn)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return _view(conn)


@router.delete("/api/connectors/{cid}")
def delete_connector(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    connectors = _read()
    conn = next((c for c in connectors if c["id"] == cid), None)
    if conn is None:
        return JSONResponse({"error": "unknown connector"}, status_code=404)
    _unmount(conn)
    mp = _mountpoint(conn)
    if os.path.isdir(mp) and not _is_mounted(mp) and not os.listdir(mp):
        os.rmdir(mp)
    _write([c for c in connectors if c["id"] != cid])
    return {"ok": True}


@router.post("/api/connectors/remotes")
def create_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Create an rclone remote. type 's3': fully non-interactive from keys.
    type 'drive': spawns rclone's own OAuth flow, which opens a browser tab
    on this machine and blocks until the user approves (feasibility probe —
    a real feature would stream progress)."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bin_ = _rclone_bin()
    if not bin_:
        return JSONResponse({"error": "rclone is not installed"}, status_code=502)
    name = (body.get("name") or "").strip()
    rtype = body.get("type")
    if not name or ":" in name or "/" in name:
        return JSONResponse({"error": "invalid remote name"}, status_code=400)
    if rtype == "s3":
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
        timeout = 30
    elif rtype == "drive":
        # rclone opens http://127.0.0.1:53682 + the user's browser for OAuth.
        cmd = [bin_, "config", "create", name, "drive", "scope", "drive.readonly"]
        timeout = 180
    else:
        return JSONResponse({"error": "type must be 's3' or 'drive'"}, status_code=400)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": f"rclone config create timed out ({timeout}s)"}, status_code=502)
    if r.returncode != 0:
        return JSONResponse({"error": (r.stderr or r.stdout or "").strip()[-500:]}, status_code=502)
    return {"ok": True, "name": name + ":"}
