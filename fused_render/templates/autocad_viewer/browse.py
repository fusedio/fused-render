"""List the CAD drawings in a folder, for the viewer's Open panel.

Stdlib only (runs on the app interpreter, no dependencies) and returns
JSON-native values. A bad folder comes back as `error` rather than raising, so
the panel shows a message instead of a traceback overlay.

Mount safety: a kernel scan of a remote-mounted folder can stall or DROP the
mount (see canvas/reader.py). So when the server reports the folder is `remote`
we list it through /api/fs/list — routed via rclone's rc, never the kernel —
rather than scanning it ourselves; local folders use a single os.scandir pass.
The browser passes `src` = the server origin, trusted only for scheme+host.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

_CAD_EXT = (".dxf", ".dwg")
_MAX = 2000  # bound a local scan so a pathological directory can't build a huge response


def _norm(p: str) -> str:
    # Mirror the shell's URL codec (router.ts urlForFsPath): normalize ONLY
    # drive-letter paths to forward slashes; on POSIX a backslash is a legal
    # filename character and must survive untouched.
    return p.replace("\\", "/") if re.match(r"^[A-Za-z]:[\\/]", p) else p


def _entry(folder, name, size):
    return {"name": name, "path": _norm(os.path.join(folder, name)), "size": size}


def _server_url(src, endpoint, path):
    u = urllib.parse.urlsplit(src)
    return f"{u.scheme}://{u.netloc}{endpoint}?path=" + urllib.parse.quote(path)


def _is_remote(src, folder):
    """True/False when the server can say whether `folder` is a remote mount;
    None when it is unreachable, so the caller falls back to a local scan."""
    try:
        with urllib.request.urlopen(_server_url(src, "/api/fs/stat", folder), timeout=10) as r:
            return bool(json.load(r).get("remote"))
    except urllib.error.HTTPError:
        return False
    except Exception:
        return None


def _list_remote(src, folder):
    try:
        with urllib.request.urlopen(_server_url(src, "/api/fs/list", folder), timeout=15) as r:
            payload = json.load(r)
    except Exception as e:
        return {"folder": _norm(folder), "files": [], "error": str(e)}
    files = [
        _entry(folder, e["name"], e.get("size"))
        for e in (payload.get("entries") or [])
        if e.get("name") and not e.get("is_dir") and e["name"].lower().endswith(_CAD_EXT)
    ]
    return {"folder": _norm(folder), "files": files}


def _list_local(folder):
    files = []
    try:
        with os.scandir(folder) as it:
            for i, de in enumerate(it):
                if i >= _MAX:
                    break
                if not de.name.lower().endswith(_CAD_EXT):
                    continue
                try:
                    if de.is_file():
                        files.append(_entry(folder, de.name, de.stat().st_size))
                except OSError:
                    continue
    except OSError as e:
        return {"folder": _norm(folder), "files": [], "error": str(e)}
    files.sort(key=lambda f: f["name"].lower())
    return {"folder": _norm(folder), "files": files}


def main(folder: str, src: str = "") -> dict:
    if src and _is_remote(src, folder) is True:
        return _list_remote(src, folder)
    return _list_local(folder)
