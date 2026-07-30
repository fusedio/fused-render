import os
import stat as stat_mod

from fused_render.server.common import _error
from fused_render.server.templates import _templates_for




# /api/fs/stat on a MOUNT path goes _mount_safe_stat -> _mount_probe ->
# rc_list_dir(parent), a full cold LIST of the parent prefix over rclone/S3
# (~1.6s) just to describe one child; opening a folder fires it, and
# re-navigating to a sibling repaid it uncached. A short check-on-read TTL cache
# (same shape as _CONDITIONS_CACHE) serves a recent stat instead. Only
# MOUNT-backed success payloads are cached — see api_fs_stat for the scope
# rationale and mutation-invalidation contract. Separate TTL from conditions so
# each can be tuned/monkeypatched on its own; kept a module attribute so tests
# can override it.
_STAT_TTL_S = 60.0
_STAT_CACHE: dict[str, tuple[float, dict]] = {}  # path -> (inserted_monotonic, payload)
# Monotonic invalidation counter for the TOCTOU guard in api_fs_stat. Every
# _invalidate_stat_cache bump happens-before the post-compute check in a stat
# that reads the generation first, so any mutation that completes while a slow
# _fs_stat is in flight is observed and blocks that stale result from refilling
# the cache. A single global counter is deliberately conservative: a concurrent
# mutation to ANY path just skips caching this one in-flight stat (rare and
# harmless) rather than requiring per-path bookkeeping.
_STAT_CACHE_GEN = 0


def _invalidate_stat_cache(*paths: object) -> None:
    # Drop cached /api/fs/stat entries for paths a mutation just touched, plus
    # their parent directories (creating/deleting a child moves the parent's
    # mtime on many backends). The editor re-stats a path right after
    # write/rename/copy/mkdir to re-arm its optimistic lock, so a stale hit here
    # would be a real clobber bug — invalidation is the correctness backbone of
    # this cache, not an optimization. Popping a key that was never cached (or a
    # non-string/None body field) is a harmless no-op, so callers can pass raw
    # body values and invalidate unconditionally without inspecting the result.
    #
    # Bump the generation UNCONDITIONALLY (even for a no-op pop): a stat for a
    # not-yet-cached path may be mid-flight, and the bump is what tells its
    # post-compute check that a mutation raced it so it must not cache a
    # pre-mutation payload. See api_fs_stat for the guard.
    global _STAT_CACHE_GEN
    _STAT_CACHE_GEN += 1
    for p in paths:
        if isinstance(p, str) and p:
            _STAT_CACHE.pop(p, None)
            _STAT_CACHE.pop(os.path.dirname(p), None)


def _writable(path: str) -> bool:
    """True iff /api/fs/write would accept this path. An existing target needs
    W_OK on itself — the atomic os.replace would otherwise bypass a read-only
    bit via the parent directory — and a new file needs W_OK on its parent.
    Templates read this off the stat payload to render read-only mode up
    front; keep the two in agreement.

    Paths under a read-only mount are never writable, whatever the permission
    bits say: the rclone VFS (CacheMode=full) takes any write into its local
    cache and only fails at the async upload, so W_OK is a lie there."""
    # Local import, like _stat_payload's: server -> shell.mounts only,
    # keeping shell ↛ server acyclic.
    from fused_render.shell.mounts import is_mount_backed, mount_read_only

    if mount_read_only(path):
        return False
    if is_mount_backed(path):
        # A writable (not read-only) mount: the rclone VFS (CacheMode=full) takes
        # writes into its local cache, so a path under it is writable regardless
        # of kernel permission bits. Return True WITHOUT a kernel
        # os.path.exists/os.access — a cold negative lookup over the mount lists
        # the whole S3 prefix and wedges it, the same trap fs/stat's os.stat is
        # routed off the kernel to avoid (mount_read_only reads mounts.json only).
        return True
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    return os.access(os.path.dirname(path) or ".", os.W_OK)


# ---------------------------------------------------------------------------
# Mount-safe existence/shape probes for the fs mutation handlers + /api/fs/raw.
#
# An rclone-backed NFS mount has no cheap point lookup: a cold NEGATIVE kernel
# probe (os.stat / os.path.exists / os.path.isdir / os.listdir) forces rclone
# to enumerate the ENTIRE parent S3 prefix (measured: 44k entries, ~64s), which
# blows the macOS NFS deadman and DROPS the mount — server threads then block
# uninterruptibly. So a mutation handler must never touch a mount-backed path
# through the kernel to decide whether it exists or what shape it is. These
# helpers answer that via the rclone rcd (operations/list, bounded by a hard
# timeout: a too-huge directory becomes a failed request, never a dead mount).


class _MountProbe:
    """Result of a mount-safe existence/shape probe. `parent_is_dir` is whether
    the path's parent is a listable directory (a mount-safe stand-in for
    os.path.isdir(parent)); `exists`/`is_dir`/`size`/`mtime` describe the path
    itself. Size/mtime come from the rc listing entry (None when absent)."""

    __slots__ = ("parent_is_dir", "exists", "is_dir", "size", "mtime")

    def __init__(self, parent_is_dir, exists, is_dir=False, size=None, mtime=None):
        self.parent_is_dir = parent_is_dir
        self.exists = exists
        self.is_dir = is_dir
        self.size = size
        self.mtime = mtime


def _mount_probe(path: str) -> _MountProbe:
    """Existence + shape of a MOUNT-BACKED path, answered by the rclone rcd
    (rc_list_dir of the parent + membership match), doing ZERO kernel FS I/O on
    the mount. The parent listing is bounded by rc_list_dir's hard timeout, so a
    huge remote directory raises RcListTimeout rather than wedging the mount.

    Returns a _MountProbe. Raises RcListUnavailable (rcd down / broken mount) or
    RcListTimeout (directory too large to enumerate) when existence is
    INDETERMINATE — the caller maps those to 503 (via _mount_list_error_response),
    never to "missing"."""
    from fused_render.shell import mounts as m

    # The mounts container and each individual mountpoint are LOCAL directories
    # the shell created to host mounts; they always exist as directories and
    # their own parent has no single mount record to list. Answer directly.
    if m.is_mounts_root(path):
        return _MountProbe(True, True, is_dir=True)
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    if m.is_mounts_root(parent):
        # A direct child of the container is a mountpoint only if a mount
        # RECORD carries its name — an unknown/removed name is a phantom and
        # must read as absent (mounts.json only, no I/O on any mount).
        exists = any(rec.get("name") == name for rec in m.list_mounts())
        return _MountProbe(True, exists, is_dir=exists)
    try:
        entries = m.rc_list_dir(parent)
    except (m.RcListUnavailable, m.RcListTimeout):
        raise  # indeterminate -> caller returns 503
    except m.RcListError:
        # The rcd rejected the listing: the parent is a file or is missing, so
        # the child cannot exist. Mount-safe equivalent of a False os.path.isdir.
        return _MountProbe(False, False)
    for ent in entries:
        if ent.get("Name") == name:
            return _MountProbe(True, True, is_dir=bool(ent.get("IsDir")),
                               size=ent.get("Size"),
                               mtime=m.rc_modtime_epoch(ent.get("ModTime")))
    return _MountProbe(True, False)  # parent listable, child absent


def _mount_stat_payload(path: str, is_dir: bool, size, mtime) -> dict:
    """The /api/fs/stat payload for a MOUNT-BACKED path, built from an rc probe
    (size/mtime) with NO kernel os.stat/os.access on the mount. `writable` is
    True by construction — the caller only reaches this after clearing the
    read-only gate (mount_read_only False)."""
    templates, template_error = _templates_for(path, is_dir)
    payload = {
        "path": path,
        "name": os.path.basename(path) or path,
        "is_dir": is_dir,
        "size": None if is_dir else size,
        "mtime": mtime,
        "writable": True,
        "remote": True,
        "templates": templates,
    }
    if template_error:
        payload["template_error"] = template_error
    return payload


def _probe_path(path: str) -> _MountProbe:
    """Existence + shape of `path`, mount-safe: a mount-backed path is answered
    through the rclone rcd (_mount_probe, zero kernel I/O), a local path with a
    plain kernel stat. Used by _fs_rename/_fs_copy, which may mix a local and a
    mount-backed side. Raises RcListUnavailable/RcListTimeout for an
    indeterminate mount probe (caller maps to 503)."""
    from fused_render.shell import mounts as m

    if m.is_mount_backed(path):
        return _mount_probe(path)
    parent_is_dir = os.path.isdir(os.path.dirname(path) or ".")
    if not os.path.exists(path):
        return _MountProbe(parent_is_dir, False)
    return _MountProbe(parent_is_dir, True, is_dir=os.path.isdir(path))


def _mutation_result_payload(path: str, is_dir: bool) -> dict:
    """The /api/fs/stat payload returned after a successful mutation, mount-safe:
    a mount-backed path is described from a fresh rc probe (no kernel os.stat),
    a local path via the ordinary _stat_payload."""
    from fused_render.shell import mounts as m

    if not m.is_mount_backed(path):
        return _stat_payload(path, is_dir)
    try:
        pr = _mount_probe(path)
    except (m.RcListUnavailable, m.RcListTimeout):
        pr = None
    size = pr.size if pr and pr.exists else None
    mtime = pr.mtime if pr and pr.exists else None
    return _mount_stat_payload(path, is_dir, size, mtime)


def _stat_or_none(path: str) -> os.stat_result | None:
    """stat() for /api/fs/raw's 404 gate: None for missing paths and
    non-regular files alike (a directory has no raw bytes to serve)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if stat_mod.S_ISREG(st.st_mode) else None


def _stat_payload(path: str, is_dir: bool, st: os.stat_result | None = None) -> dict:
    """The /api/fs/stat shape. /api/fs/write returns it too, so the editor can
    re-arm its optimistic lock from a save response. Pass a pre-fetched `st` to
    avoid a redundant stat() — one remote round-trip under a mount."""
    # Local import, like api_fs_walk's: server -> shell.mounts only, keeping
    # shell ↛ server acyclic.
    from fused_render.shell.mounts import is_mount_backed

    if st is None:
        st = _mount_safe_stat(path)
    templates, template_error = _templates_for(path, is_dir)

    payload = {
        "path": path,
        "name": os.path.basename(path) or path,
        "is_dir": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "writable": _writable(path),
        # Bytes come from a remote (the path sits under a mount). Pages use
        # this to prefer ranged HTTP reads (/api/fs/raw) over local file I/O.
        "remote": is_mount_backed(path),
        "templates": templates,
    }
    if template_error:
        payload["template_error"] = template_error
    return payload


def _mount_safe_stat(path: str) -> os.stat_result:
    """os.stat for a path that may be mount-backed, off the kernel for mounts.

    A kernel os.stat on a mount is a GETATTR that can force an S3 re-list and
    wedge the mount (the stat-storm / deadman incident); route mount paths
    through the rclone rc API (rc_stat_result) instead. It raises OSError /
    FileNotFoundError exactly like the kernel os.stat it replaces, so callers'
    existing OSError->404 handling holds — and it NEVER falls back to that kernel
    GETATTR, which is the call that killed the mount."""
    from fused_render.shell.mounts import (
        is_mount_backed, is_mounts_root, rc_stat_result)

    # The mounts CONTAINER itself is a LOCAL directory the shell created to host
    # each mountpoint as a subdir; it is is_mount_backed (kept off the kernel
    # like any remote path) but sits under NO single mount record, so
    # rc_stat_result finds nothing to stat and reports it indeterminate — which
    # _fs_stat then turns into a spurious 503 "mount is slow or unresponsive".
    # A kernel os.stat on the container reads that local dir and never traverses
    # into a mountpoint, so it is safe — matching _mount_probe's is_mounts_root
    # shortcut for the listing/mutation paths.
    if is_mount_backed(path) and not is_mounts_root(path):
        return rc_stat_result(path)
    return os.stat(path)


def _fs_stat(path: str):
    # One stat, not the exists()+isdir()+stat() trio: over a remote mount each
    # is a round-trip, so a plain metadata fetch cost 3 LISTs. _mount_safe_stat
    # keeps a mount stat off the kernel (rc API / direct probe).
    #
    # 404 vs 503: FileNotFoundError is a CONFIRMED miss (kernel ENOENT, or a
    # healthy backend's trustworthy negative) -> 404, matching os.path.exists()'s
    # OSError->False for a local path. A bare OSError on a MOUNT path is
    # rc_stat_result's "indeterminate" (rcd unreachable, rc timeout, mount slow /
    # unresponsive) — NOT proof the path is gone. Mapping it to 404 tells the
    # client a path it just opened has vanished; surface it as a retryable 503
    # instead. A non-mount OSError keeps the historical exists()->False -> 404.
    from fused_render.shell.mounts import is_mount_backed

    try:
        st = _mount_safe_stat(path)
    except FileNotFoundError:
        return _error(f"no such file or directory: {path}", status=404)
    except OSError:
        if is_mount_backed(path):
            return _error(
                f"mount is slow or unresponsive, could not stat {path}",
                status=503)
        return _error(f"no such file or directory: {path}", status=404)
    return _stat_payload(path, stat_mod.S_ISDIR(st.st_mode), st)
_STAT_TIMEOUT_S = 4.0  # a stat outliving this reports "unchanged" for this tick
