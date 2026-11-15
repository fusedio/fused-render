"""Where a template puts a file it DERIVES from its input (an archive member,
a converted plist), keeping downloaded bucket content inside the guarded area.

A file opened from an `s3://` location is a download in the server's remote
cache (`<home>/cache/remote/…`, `fused_render/remote_fetch.py`). The server
treats everything under that root as untrusted: `/render` refuses it and
`/api/fs/raw` serves it sandboxed. A template that extracted a member of such
an archive into the system temp dir would move bucket bytes OUT of that guard
— so for an input inside the cache, derived files go under
`<cache root>/.previews/` instead (a bucket name can never start with ".", so
it cannot collide with a bucket directory), and the caller is told the input
is remote so its page never frames a derived file as a page (`_render`).
Inputs anywhere else keep the exact temp-dir path they always had.

Stdlib only, never imports `fused_render` (see the
adding-a-shared-template-utility skill); the home dir comes from the sibling
`appenv.py`, loaded by path.
"""
import hashlib
import importlib.util
import os
import shutil
import tempfile
import time

#: Previews of remote inputs untouched for this long are swept.
PREVIEW_TTL_S = 24 * 3600


def _appenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appenv.py")
    spec = importlib.util.spec_from_file_location("_remote_cache_appenv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_root() -> str:
    """The server's remote cache root (mirrors `remote_fetch.cache_root`)."""
    return os.path.join(_appenv().home_dir(), "cache", "remote")


def in_cache(path: str) -> bool:
    """Whether `path` lies inside the remote cache — by file identity, walking
    up from the resolved path, so a case-variant spelling or a symlink still
    counts (mirrors `remote_fetch.in_cache`)."""
    try:
        root_st = os.stat(cache_root())
        p = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    while True:
        try:
            if os.path.samestat(os.stat(p), root_st):
                return True
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


def _sweep(base: str, keep: str) -> None:
    now = time.time()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        d = os.path.join(base, name)
        if d == keep:
            continue
        try:
            if now - os.stat(d).st_mtime > PREVIEW_TTL_S:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def preview_root(file: str, kind: str) -> tuple:
    """`(dir, remote)`: a stable per-input directory for files derived from
    `file`, and whether `file` is a remote (bucket) download. `kind` names the
    template ("zip", "tar", "plist")."""
    key = hashlib.sha1(os.path.realpath(file).encode("utf-8")).hexdigest()[:16]
    remote = in_cache(file)
    if remote:
        base = os.path.join(cache_root(), ".previews")
        root = os.path.join(base, f"{kind}-{key}")
        _sweep(base, root)
    else:
        root = os.path.join(tempfile.gettempdir(), f"fused-render-{kind}", key)
    os.makedirs(root, exist_ok=True)
    if remote:
        os.utime(root)  # in use: not stale
    return root, remote
