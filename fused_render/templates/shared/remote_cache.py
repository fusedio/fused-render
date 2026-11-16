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


def _holder(base: str, file: str):
    """The top-level directory under `base` that holds `file` (an archive
    member previewed out of another remote archive), or None. By identity,
    walking up as `in_cache` does, so a spelling variant still counts."""
    try:
        base_st = os.stat(base)
        p = os.path.realpath(file)
    except (OSError, ValueError):
        return None
    while True:
        parent = os.path.dirname(p)
        if parent == p:
            return None
        try:
            if os.path.samestat(os.stat(parent), base_st):
                return p
        except (OSError, ValueError):
            pass
        p = parent


def _sweep(base: str, keep: list) -> None:
    """Remove `base`'s stale (untouched for `PREVIEW_TTL_S`) directories,
    except the ones in `keep`."""
    now = time.time()
    try:
        names = os.listdir(base)
    except OSError:
        return
    kept = []
    for k in keep:
        try:
            kept.append(os.stat(k))
        except OSError:
            pass
    for name in names:
        d = os.path.join(base, name)
        try:
            st = os.stat(d)
            if any(os.path.samestat(st, k) for k in kept):
                continue
            if now - st.st_mtime > PREVIEW_TTL_S:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def preview_root(file: str, kind: str) -> tuple:
    """`(dir, remote)`: a stable per-input directory for files derived from
    `file`, and whether `file` is a remote (bucket) download. `kind` names the
    template ("zip", "tar", "plist").

    For a remote input, stale previews of other inputs are swept — never this
    one's, and never the preview directory `file` itself sits in (an archive
    inside an archive), which is touched as in use."""
    key = hashlib.sha1(os.path.realpath(file).encode("utf-8")).hexdigest()[:16]
    remote = in_cache(file)
    if remote:
        base = os.path.join(cache_root(), ".previews")
        root = os.path.join(base, f"{kind}-{key}")
        os.makedirs(root, exist_ok=True)
        holder = _holder(base, file)
        for d in (root, holder):
            if d is not None:
                try:
                    os.utime(d)  # in use: not stale
                except OSError:
                    pass
        _sweep(base, [d for d in (root, holder) if d is not None])
    else:
        root = os.path.join(tempfile.gettempdir(), f"fused-render-{kind}", key)
        os.makedirs(root, exist_ok=True)
    return root, remote
