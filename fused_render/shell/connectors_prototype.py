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
        # `full` caches read ranges as sparse files: measured on a 204MB COG,
        # a first deep-zoom tile is slow either way (the tiff reader does bulk
        # native-res reads), but every subsequent read of the touched region
        # is instant (0.02s vs 107s). Chunked ranges bound each S3 GET.
        "--vfs-cache-mode", "full",
        "--vfs-read-chunk-size", "2M",
        "--vfs-read-chunk-size-limit", "16M",
        "--dir-cache-time", "10s",
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
    if conn.get("kind") == "local":
        return None  # nothing mounted by us
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
    # "local" connectors point at a path some desktop client (Google Drive,
    # OneDrive, Dropbox) already keeps synced — no mount lifecycle to own,
    # "mounted" just means the path is currently there.
    if conn.get("kind") == "local":
        return {**conn, "mountpoint": conn["path"], "mounted": os.path.isdir(conn["path"])}
    mp = _mountpoint(conn)
    return {**conn, "mountpoint": mp, "mounted": _is_mounted(mp)}


# ------------------------------------------------- provider desktop clients
#
# The "proper way" for consumer clouds: the vendor's own desktop client
# already exposes a synced local folder (macOS File Provider under
# ~/Library/CloudStorage, Dropbox's classic ~/Dropbox). Detect those and
# offer one-click connectors; when absent, the UI shows setup guidance
# instead of an OAuth dance.

_PROVIDER_CATALOG = [
    {
        "kind": "gdrive",
        "label": "Google Drive",
        "help_url": "https://www.google.com/drive/download/",
    },
    {
        "kind": "onedrive",
        "label": "OneDrive",
        "help_url": "https://www.microsoft.com/microsoft-365/onedrive/download",
    },
    {
        "kind": "dropbox",
        "label": "Dropbox",
        "help_url": "https://www.dropbox.com/install",
    },
]


def _detect_provider_paths() -> dict[str, list[dict]]:
    """kind -> [{label_suffix, path}] of synced roots present on this machine."""
    found: dict[str, list[dict]] = {"gdrive": [], "onedrive": [], "dropbox": []}
    cloud = os.path.expanduser("~/Library/CloudStorage")
    try:
        entries = sorted(os.listdir(cloud))
    except OSError:
        entries = []
    for entry in entries:
        full = os.path.join(cloud, entry)
        if not os.path.isdir(full):
            continue
        if entry.startswith("GoogleDrive-"):
            account = entry[len("GoogleDrive-"):]
            for root in ("My Drive", "Shared drives"):
                p = os.path.join(full, root)
                if os.path.isdir(p):
                    found["gdrive"].append({"label_suffix": f"{root} ({account})", "path": p})
        elif entry.startswith("OneDrive-"):
            found["onedrive"].append({"label_suffix": entry[len("OneDrive-"):], "path": full})
        elif entry.startswith("Dropbox"):
            found["dropbox"].append({"label_suffix": entry, "path": full})
    classic_dropbox = os.path.expanduser("~/Dropbox")
    if not found["dropbox"] and os.path.isdir(classic_dropbox):
        found["dropbox"].append({"label_suffix": "Dropbox", "path": classic_dropbox})
    return found


def _providers() -> list[dict]:
    detected = _detect_provider_paths()
    connected_paths = {c.get("path") for c in _read() if c.get("kind") == "local"}
    out = []
    for cat in _PROVIDER_CATALOG:
        roots = [
            {**r, "connected": r["path"] in connected_paths}
            for r in detected.get(cat["kind"], [])
        ]
        out.append({**cat, "installed": bool(roots), "roots": roots})
    return out


# ---------------------------------------------------------------- endpoints


@router.get("/api/connectors")
def get_connectors():
    return {
        "rclone": _rclone_state(),
        "providers": _providers(),
        "connectors": [_view(c) for c in _read()],
    }


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
    if body.get("kind") == "local":
        # Desktop-client folder (Google Drive / OneDrive / Dropbox): no mount
        # to manage, just register the synced root.
        path = os.path.abspath(os.path.expanduser((body.get("path") or "").strip()))
        if not os.path.isdir(path):
            return JSONResponse({"error": f"not a directory: {path}"}, status_code=400)
        if any(c.get("path") == path for c in connectors):
            return JSONResponse({"error": "that folder is already connected"}, status_code=400)
        conn = {"id": uuid.uuid4().hex[:12], "name": name, "kind": "local", "path": path}
    else:
        remote = (body.get("remote") or "").strip()
        if ":" not in remote:
            return JSONResponse({"error": "remote must be an rclone spec like 'gdrive:' or 's3:bucket/prefix'"}, status_code=400)
        conn = {"id": uuid.uuid4().hex[:12], "name": name, "kind": "rclone", "remote": remote}
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
    if conn.get("kind") == "local":
        return JSONResponse({"error": "local connectors have no mount to manage"}, status_code=400)
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
    if conn.get("kind") == "local":
        return JSONResponse({"error": "local connectors have no mount to manage"}, status_code=400)
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
    if conn.get("kind") != "local":
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
