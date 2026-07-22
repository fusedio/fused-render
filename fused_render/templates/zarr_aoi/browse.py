"""Directory listing for the nc_preview file explorer.

Returns the sub-directories and files of `dir` so the HTML can render a
navigable picker — no need to type paths by hand. Stdlib only.

Mount-safety: a kernel directory listing (os.listdir) or a cold os.path.* probe
on a path backed by a remote rclone NFS mount forces rclone to enumerate the
ENTIRE parent S3 prefix, which can exceed the NFS deadman timeout and DROP the
mount, wedging the whole server. So when the UI passes a `src` (the server
origin, built as `${origin}/api/fs/raw?path=`), we first ask /api/fs/stat
whether `dir` is `remote`; if it is, we list via /api/fs/list (mount-routed,
paginated, capped server-side) and NEVER touch the kernel. Local dirs (or an
unreachable server) keep the faster kernel os.listdir. The template stays
mount-AGNOSTIC — it never imports shell.mounts and never matches mount paths.
The two helpers below are duplicated verbatim from pyramid/overview_pyramid.py
(templates avoid cross-template imports); _list is the listing sibling of _stat.
"""

import json as _json
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq


def _server_url(src, endpoint, path):
    """Server URL built from `src`'s ORIGIN and our own normalized `path`. src
    is trusted only for scheme+netloc: its ?path= carries the browser's raw
    file param (possibly ~-prefixed / relative) and the fs endpoints do no
    expansion — judging remote-ness on one path string and range-reading another
    would 404. So we quote OUR path onto the endpoint, ignoring src's path."""
    u = _urlparse.urlsplit(src)
    return f"{u.scheme}://{u.netloc}{endpoint}?path=" + _urlparse.quote(path)


def _stat(src, path):
    """Ask /api/fs/stat about `path`. Returns:
    ("ok", payload)      — payload has bool `remote` and int `size`
    ("missing", None)    — server says the path does not exist (404)
    ("unreachable", None)— server could not be reached / errored; the caller
                           falls back to a local kernel probe (presumed local).
    """
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


def _list(src, path):
    """Ask /api/fs/list to enumerate `path` (mount-routed, capped at 10k).
    Returns ("ok", payload) with payload["entries"], or ("error", None) on any
    HTTP/network failure. On error we do NOT fall back to a kernel listdir — the
    whole point is to never enumerate a remote prefix through the kernel."""
    url = _server_url(src, "/api/fs/list", path)
    try:
        with _urlreq.urlopen(url, timeout=30) as r:
            return ("ok", _json.load(r))
    except Exception:  # noqa: BLE001
        return ("error", None)


def main(
    dir: str = "~",
    exts: str = ".zarr",
    show_all: bool = False,
    store_exts: str = ".zarr",
    src: str = "",
):
    import os

    dir = os.path.abspath(os.path.expanduser(dir or "~"))

    allow = tuple(e.strip().lower() for e in exts.split(",") if e.strip())
    stores = tuple(e.strip().lower() for e in store_exts.split(",") if e.strip())

    # Decide route: remote dirs MUST be listed over HTTP (never the kernel).
    remote = False
    if src:
        status, payload = _stat(src, dir)
        if status == "missing":
            return {"error": f"cannot list {dir}", "dir": dir, "parent": os.path.dirname(dir)}
        if status == "ok" and payload.get("remote"):
            remote = True
            # stat replaces the kernel isdir probe: if `dir` is actually a file,
            # descend to its parent (pure string op — no kernel I/O).
            if not payload.get("is_dir", True):
                dir = os.path.dirname(dir) or "/"

    # Gather (name, is_dir, size) triples — via HTTP for remote, kernel for local.
    if remote:
        status, listing = _list(src, dir)
        if status != "ok":
            return {"error": f"cannot list {dir}", "dir": dir, "parent": os.path.dirname(dir)}
        triples = []
        for e in listing.get("entries", []):
            name = e.get("name")
            if not name or name.startswith("."):  # hide dotfiles
                continue
            triples.append((name, bool(e.get("is_dir")), e.get("size")))
    else:
        if not os.path.isdir(dir):
            dir = os.path.dirname(dir) or "/"
        try:
            names = os.listdir(dir)
        except OSError as e:
            return {"error": f"cannot list {dir}: {e}", "dir": dir, "parent": os.path.dirname(dir)}
        triples = []
        for name in names:
            if name.startswith("."):  # hide dotfiles
                continue
            full = os.path.join(dir, name)
            try:
                is_dir = os.path.isdir(full)
                size = None if is_dir else os.path.getsize(full)
            except OSError:
                continue
            triples.append((name, is_dir, size))

    dirs, files = [], []
    for name, is_dir, size in triples:
        full = os.path.join(dir, name)
        if is_dir:
            # a directory whose name ends in a store extension (e.g. .zarr) is a
            # loadable store, not just a folder to descend into.
            if any(name.lower().endswith(s) for s in stores):
                files.append(
                    {
                        "name": name,
                        "path": full,
                        "is_dir": True,
                        "size": None,
                        "ext": ".zarr",
                        "loadable": True,
                    }
                )
            else:
                dirs.append({"name": name, "path": full, "is_dir": True})
        else:
            ext = os.path.splitext(name)[1].lower()
            loadable = ext in allow
            if loadable or show_all:
                files.append(
                    {
                        "name": name,
                        "path": full,
                        "is_dir": False,
                        "size": size,
                        "ext": ext,
                        "loadable": loadable,
                    }
                )

    dirs.sort(key=lambda e: e["name"].lower())
    files.sort(key=lambda e: e["name"].lower())

    # breadcrumb segments: [(label, path), ...] from root to here
    parts, acc = [], ""
    for seg in dir.strip("/").split("/"):
        acc += "/" + seg
        parts.append({"label": seg, "path": acc})

    return {
        "dir": dir,
        "parent": os.path.dirname(dir),
        "crumbs": parts,
        "dirs": dirs,
        "files": files,
        "n_hidden_files": 0 if show_all else None,
    }


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass
