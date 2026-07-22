"""List runnable / renderable entries in a directory for the viewer sidebar.

main(dir="") -> {dir, parent, entries:[{name, path, kind, ext}]}
Default dir is the user's home. Pass any absolute directory to browse.
"""

import os

_DEFAULT_DIR = os.path.expanduser("~")

RASTER = (".tif", ".tiff", ".vrt", ".jp2", ".img")
VECTOR = (".geojson", ".json", ".shp", ".gpkg", ".fgb", ".kml", ".gml")
TABLE = (".parquet", ".geoparquet", ".csv")
PMTILES = (".pmtiles",)


def _kind(name):
    low = name.lower()
    if low.endswith(".py"):
        return "python"
    if low.endswith(PMTILES):
        return "pmtiles"
    if low.endswith(RASTER):
        return "raster"
    if low.endswith(VECTOR):
        return "vector"
    if low.endswith(TABLE):
        return "table"
    return "other"


# --- mount-safe directory listing ------------------------------------------
# A kernel listing (os.listdir/os.scandir/os.walk) on a path under a remote
# rclone NFS mount forces rclone to enumerate the ENTIRE parent S3 prefix and
# can DROP the mount, wedging the server. This module stays mount-AGNOSTIC:
# it never imports shell.mounts and never matches mount paths. Instead the UI
# passes `src` (server origin + /api/fs/raw?path=) and we ask the server whether
# a path is remote (/api/fs/stat); if so we list it via the mount-routed,
# paginated /api/fs/list — never through the kernel. _server_url + _stat are
# copied verbatim from pyramid/overview_pyramid.py.
import json as _json
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq


def _server_url(src, endpoint, path):
    u = _urlparse.urlsplit(src)
    return (f"{u.scheme}://{u.netloc}{endpoint}?path="
            + _urlparse.quote(path))


def _stat(src, path):
    url = _server_url(src, "/api/fs/stat", path)
    try:
        with _urlreq.urlopen(url, timeout=10) as r:
            return ("ok", _json.load(r))
    except _urlerr.HTTPError as e:
        if e.code == 404:
            return ("missing", None)
        return ("unreachable", None)
    except Exception:  # noqa: BLE001 — any network error -> fall back to local
        return ("unreachable", None)


def _remote_dir(src, path):
    """True iff the server says `path` is a remote (mount-backed) directory.
    No src / unreachable / missing -> False (presume local, kernel listing OK)."""
    if not src or not path:
        return False
    status, meta = _stat(src, path)
    return status == "ok" and bool(meta.get("remote"))


def _list_remote(src, path, cap=5000):
    """List `path` via the server's mount-routed, paginated /api/fs/list — never
    the kernel. Follows the cursor up to `cap` entries so a huge S3 prefix
    returns a bounded page set instead of tripping the NFS deadman."""
    entries, cursor, truncated = [], "", False
    while True:
        url = _server_url(src, "/api/fs/list", path)
        if cursor:
            url += "&cursor=" + _urlparse.quote(cursor)
        with _urlreq.urlopen(url, timeout=30) as r:
            payload = _json.load(r)
        entries.extend(payload.get("entries") or [])
        truncated = bool(payload.get("truncated"))
        cursor = payload.get("cursor") or ""
        if len(entries) >= cap or not truncated or not cursor:
            break
    return entries, truncated


def main(dir: str = "", src: str = ""):
    base = os.path.abspath(os.path.expanduser(dir.strip())) if dir else _DEFAULT_DIR

    if _remote_dir(src, base):
        # Mount-backed dir: list via /api/fs/list, never a kernel scan.
        try:
            ents, _ = _list_remote(src, base)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "dir": base, "entries": []}
        entries = []
        for ent in ents:
            name = ent["name"]
            if name.startswith(".") or name in ("__pycache__",):
                continue
            full = os.path.join(base, name)
            if ent.get("is_dir"):
                entries.append({"name": name, "path": full, "kind": "dir", "ext": ""})
                continue
            _, ext = os.path.splitext(name)
            k = _kind(name)
            if k == "other":
                continue
            entries.append({"name": name, "path": full, "kind": k, "ext": ext.lower()})
        entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
        return {"dir": base, "parent": os.path.dirname(base), "entries": entries}

    if not os.path.isdir(base):
        return {"error": f"Not a directory: {base}", "dir": base, "entries": []}

    entries = []
    for name in sorted(os.listdir(base), key=str.lower):
        if name.startswith(".") or name in ("__pycache__",):
            continue
        full = os.path.join(base, name)
        if os.path.isdir(full):
            entries.append({"name": name, "path": full, "kind": "dir", "ext": ""})
            continue
        _, ext = os.path.splitext(name)
        k = _kind(name)
        if k == "other":
            continue  # only show things we might render / navigate
        entries.append({"name": name, "path": full, "kind": k, "ext": ext.lower()})

    # dirs first, then files
    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
    return {"dir": base, "parent": os.path.dirname(base), "entries": entries}
