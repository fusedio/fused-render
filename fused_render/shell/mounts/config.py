"""Shared mount config: the canonical rclone VFS option set (mount side and
HTTP-serve side, kept from drifting apart — see VFS_OPT below) and the small
read-only/NFS-option helpers derived from it, plus the X-Fused endpoint guard
shared by every handler in endpoints.py."""

import logging
import re
import time

from fastapi.responses import JSONResponse

from .store import _ismount, mountpoint

logger = logging.getLogger(__name__)


VFS_OPT = {
    "CacheMode": "full",
    "ChunkSize": "8M",
    "ChunkSizeLimit": "64M",
    "ChunkStreams": 4,
    "CacheMaxAge": "24h",
    "CacheMaxSize": "20Gi",
    "FastFingerprint": True,
    "DirCacheTime": "30s",
}


_VFS_OPT_TO_SERVE_PARAM = {
    "CacheMode": "vfs_cache_mode",
    "ChunkSize": "vfs_read_chunk_size",
    "ChunkSizeLimit": "vfs_read_chunk_size_limit",
    "ChunkStreams": "vfs_read_chunk_streams",
    "CacheMaxAge": "vfs_cache_max_age",
    "CacheMaxSize": "vfs_cache_max_size",
    "FastFingerprint": "vfs_fast_fingerprint",
    "DirCacheTime": "dir_cache_time",
    # The per-mount ReadOnly (added by _vfs_opt_for) maps to the serve's
    # --read-only flag (NOT --vfs-read-only, which rcd silently ignores — see
    # _serve_vfs_opt_for's history). Listed here so _serve_params derives it
    # rather than any caller hand-writing "read_only".
    "ReadOnly": "read_only",
}


def _serve_params(vfs_opt: dict) -> dict:
    """Map a mount/mount vfsOpt dict to the HTTP serve's flat rc params, via the
    single _VFS_OPT_TO_SERVE_PARAM table. Values are stringified because
    serve/list echoes them back as strings (bool -> "true"/"false", int -> "4")
    and sync_serves' drift check compares against that echo.

    A KeyError here is deliberate: a vfsOpt key with no serve mapping must not
    silently fall out of the serve's option set (that would re-split the VFS
    into a second instance) — add the mapping to _VFS_OPT_TO_SERVE_PARAM
    instead. The guard now covers the per-mount ReadOnly key too, not just the
    canonical VFS_OPT set."""
    return {
        _VFS_OPT_TO_SERVE_PARAM[k]: ("true" if v else "false") if isinstance(v, bool) else str(v)
        for k, v in vfs_opt.items()
    }


SERVE_VFS_OPT = _serve_params(VFS_OPT)


NFS_MOUNT_OPT = {"ExtraOptions": ["timeo=600", "retrans=2", "nobrowse"]}


def _vfs_opt_for(m: dict) -> dict:
    """The mount's vfsOpt: the canonical VFS_OPT plus ReadOnly driven by the
    record's read_only flag. Explicit False (not omission) so a read_write
    mount reads back ReadOnly:false in vfs/stats and matches its serve's
    read_only=false — the two option sets must agree exactly for the mount
    and serve to share one VFS."""
    return {**VFS_OPT, "ReadOnly": bool(m.get("read_only"))}


def _effective_serve_read_only(m: dict) -> bool:
    """The ReadOnly a mount's HTTP serve must carry so it SHARES the live mount's
    VFS. rcd keys VFS reuse on (fs, vfsOpt); a ReadOnly disagreement forks a
    SECOND VFS instead of sharing the mount's — the documented split-brain wedge
    (INCIDENT 2026-07-16). A live kernel mount's VFS was baked with
    `mounted_read_only` at mount/mount time; the record's `read_only` can since
    have drifted AHEAD of the live mount — detection flipped it, or the win32
    adopt-under-mismatch path deliberately DEFERRED the remount that would
    reapply it (no WinFsp to remount with). So whenever a live mount exists and
    we know what it baked (`mounted_read_only` recorded), the serve must mirror
    THAT, not the record. With no live mount there is no VFS to share yet — the
    next attach will bake the record's read_only — so fall back to the record
    (also the legacy/foreign case where mounted_read_only was never recorded)."""
    if m.get("mounted_read_only") is not None and _ismount(mountpoint(m)):
        return bool(m.get("mounted_read_only"))
    return bool(m.get("read_only"))


def _serve_vfs_opt_for(m: dict) -> dict:
    """The HTTP serve's flat vfs params for this mount: SERVE_VFS_OPT plus
    read_only, the serve-side spelling of the mount's vfsOpt.ReadOnly (the CLI
    flag is --read-only, NOT --vfs-read-only — an unknown rc param is silently
    ignored, and an ignored one here leaves the serve's VFS at ReadOnly:false,
    which both defeats the write guard AND splits the mount/serve VFS in two;
    verified live against rcd: read_only joins the mount's VFS, vfs_read_only
    forked a second instance per remote). Stringified like the rest of
    SERVE_VFS_OPT because serve/list echoes params back as strings, and
    sync_serves' drift check compares against that echo.

    Derived from _vfs_opt_for through _serve_params — NOT hand-written — so the
    mount's non-ReadOnly vfsOpt and the serve's flat params can never drift
    (drift is what split the VFS in two; the module's derive-don't-hand-write
    rule). ReadOnly is the one field that must track the LIVE mount's baked value
    rather than the record when the two disagree (see _effective_serve_read_only),
    so a serve always shares the mount's VFS even mid-mismatch."""
    vfs_opt = _vfs_opt_for(m)
    vfs_opt["ReadOnly"] = _effective_serve_read_only(m)
    return _serve_params(vfs_opt)


def _nfs_mount_opt(m: dict) -> dict:
    """macOS-only mountOpt for this mount: the NFS transport tuning plus, for a
    read_only record, "rdonly" so the kernel mount itself rejects writes (a
    belt-and-suspenders companion to the VFS ReadOnly above — the VFS stops the
    app and rclone, rdonly stops anything that reaches the kernel mount, e.g.
    Finder dropping a .DS_Store). Same nfsmount-only gating as the timeo/retrans
    options: the Linux FUSE path takes different mount flags and ignores these,
    so this is only ever passed on darwin (see attach_mount)."""
    extra = list(NFS_MOUNT_OPT["ExtraOptions"])
    if m.get("read_only"):
        extra.append("rdonly")
    return {"ExtraOptions": extra}


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to keep shell↛server
    # acyclic (see shell/bookmarks.py).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None
