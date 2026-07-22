"""pathops — mount-aware path operations facade.

The one place that knows the mount-vs-local branching for the two operations
that were, before this module, re-implemented across server.py: listing a
directory (the direct→rc ladder) and probing whether a path is an existing file
(the tri-state, fail-open kernel-free probe). Predicates and the low-level rc /
direct primitives still live in mounts.py; this module only composes them so the
composition can't drift between call sites.

Layering: pathops imports ONLY mounts.py (never server). The shell↛server
acyclic rule (see shell/bookmarks.py) holds — server depends on pathops, not the
other way around.

The mount-wedge invariant is load-bearing: on a mount-backed path these helpers
NEVER issue a kernel os.scandir/os.stat/os.path.* — not even on an error path. A
cold kernel READDIR/GETATTR over an rclone NFS mount forces rclone to enumerate
the whole remote prefix, which trips the macOS NFS deadman and kills the mount
(the mur-sst / stat-storm incidents documented in mounts.py). Every mount branch
here routes through the rcd rc API or the direct S3/GCS pager instead.

The mounts-module functions are looked up as ATTRIBUTES at call time (never bound
at import), so tests that monkeypatch e.g. mounts.rc_list_dir / rc_kind_for —
often via late imports — take effect here too.
"""
import logging
import os
import time

from fused_render.shell import mounts

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- directory listing


def _accumulate_direct_pages(path, cursor, max_entries, *,
                             page_timeout=None, overall_timeout=None):
    """Accumulate raw direct-listing entries (Name/Size/IsDir/ModTime dicts, the
    shared rc/direct shape) for a mount-backed dir on an anonymous S3 or GCS
    remote, up to `max_entries`. The one page-accumulation loop shared by
    /api/fs/list and the walk; the backend (S3 ListObjectsV2 vs GCS
    objects.list) is picked per-path by mounts.direct_list_page.

    Each page requests only min(1000, remaining) keys, so the accumulation never
    overshoots `max_entries` (a whole extra 1000-key page could otherwise push a
    LIST_MAX_ENTRIES=10k cap to 10,999) while the returned continuation token
    still resumes cleanly. `overall_timeout` bounds total wall time across pages
    so a slow bucket can't stall a request unboundedly; the FIRST page always
    gets the full `page_timeout` (progress guarantee), later pages shrink toward
    the deadline.

    Returns (entries, next_token); a non-None token means the listing is partial
    (either the cap bit or the budget expired) and is resumable. Raises
    mounts.DirectListError only when the FIRST page fails (nothing to return); a
    mid-listing page failure logs and returns what was accumulated as a partial
    with the last good token, since discarding thousands of fetched entries to
    re-list via rc (which can't paginate at all) would turn a partial success
    into a guaranteed failure."""
    entries: list = []
    token = cursor or None
    deadline = None if overall_timeout is None else time.monotonic() + overall_timeout
    while True:
        remaining = max_entries - len(entries)
        if remaining <= 0:
            break
        t = page_timeout
        # The first page always runs with the full page timeout (the progress
        # guarantee — even a zero budget returns one page). Later pages shrink
        # to the budget's remainder, and stop before it reaches zero: a
        # non-positive timeout would hit urlopen as a ValueError, not a
        # DirectListError (Bugbot), and token is already a valid resume point.
        if deadline is not None and entries:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            t = left if t is None else min(t, left)
        try:
            page, next_token = mounts.direct_list_page(
                path, max_keys=min(1000, remaining), continuation=token, timeout=t)
        except mounts.DirectListError:
            if not entries:
                raise
            logger.warning("direct-listing page for %r failed mid-listing; "
                           "returning %d accumulated entries as partial",
                           path, len(entries))
            return entries, token
        token = next_token
        entries.extend(page)
        if token is None:
            break
        # Budget checked AFTER a page so the loop always makes progress; the
        # token from the last fetched page is a valid resume point.
        if deadline is not None and time.monotonic() >= deadline:
            break
    return entries, token


class MountListing:
    """The raw result of listing a mount-backed directory through the direct→rc
    ladder, before any caller-specific shaping (sort / cap / entry adaptation /
    error→HTTP mapping — those stay with the caller).

      * entries — the operations/list-shape dicts (Name/Size/IsDir/ModTime), from
        either the direct S3/GCS pager or the rcd rc listing;
      * token   — a resume cursor for the DIRECT route (non-None ⇒ the listing is
        partial and resumable); always None on the rc route (rclone can't
        paginate a listing at any layer);
      * direct  — True when the direct pager produced this, False for the rc
        route. Callers shape the two differently (the direct route is resumable
        and pre-capped at max_entries; the rc route returns the whole listing and
        the caller sorts-then-caps), so they need to know which one ran.
    """

    __slots__ = ("entries", "token", "direct")

    def __init__(self, entries, token, direct):
        self.entries = entries
        self.token = token
        self.direct = direct


def list_mount_dir(path, *, cursor=None, max_entries, page_timeout=None,
                   overall_timeout=None, rc_timeout=None, allow_rc_fallback=True):
    """List a MOUNT-BACKED directory via the direct→rc ladder, off the kernel.

    The ladder, single-sourced here so /api/fs/list and the fs/walk can't drift:
      1. direct_list_capable (anonymous plain AWS S3 / anonymous GCS): page the
         store's own listing API — rclone can't paginate its listing, so a
         million-key prefix would time out on the rc route. Accumulate up to
         `max_entries` within the `page_timeout`/`overall_timeout` budget.
      2. otherwise, or (when `allow_rc_fallback`) after a direct page failure:
         the rcd rc listing (operations/list), bounded by `rc_timeout`
         (rc_list_dir's own default when None).

    Returns a MountListing. Does ZERO kernel I/O on `path`. Raises exactly what
    the underlying primitives raise, for the caller to map:
      * mounts.DirectListError — only when the direct route fails AND
        allow_rc_fallback is False (a cursored /api/fs/list request that must not
        silently re-list page 1 via rc);
      * mounts.RcListTimeout / RcListUnavailable / RcListError — from the rc
        route (the caller maps these to 503 / 400 as it already did).

    `path` is assumed mount-backed (the caller gates on is_mount_backed); this
    function does not re-check, and the mounts_root special case and the local
    scandir route stay with the caller (they diverge per call site — see the
    server rewires). `allow_rc_fallback=True` is the fs/walk's full-ladder use;
    /api/fs/list drives the direct route with allow_rc_fallback=False and keeps
    its own warning / cursor-503 / rc handling as HTTP response shaping."""
    if mounts.direct_list_capable(path):
        try:
            entries, token = _accumulate_direct_pages(
                path, cursor, max_entries,
                page_timeout=page_timeout, overall_timeout=overall_timeout)
            return MountListing(entries, token, direct=True)
        except mounts.DirectListError:
            if not allow_rc_fallback:
                raise
            # fall through to the rc route on the same request
    listed = mounts.rc_list_dir(path, timeout=rc_timeout)
    return MountListing(listed, None, direct=False)


# ----------------------------------------------------------- file existence probe

# rc_kind_for outcomes that count as "this is a file". "indeterminate" is
# included deliberately — the probe FAILS OPEN: an rcd hiccup / timeout must not
# 404 a file the user just opened. Only a confirmed "dir" or "missing" is a
# negative. Single-sourced here so _is_file_mount_safe and recents' _mount_exists
# can't drift on the fail-open contract.
_MOUNT_FILE_OK = ("file", "indeterminate")


def mount_is_file(path) -> bool:
    """os.path.isfile for a KNOWN mount-backed path, answered by the rcd rc API
    (mounts.rc_kind_for) and NEVER a kernel stat — a cold GETATTR over the mount
    is the call that lists the whole parent prefix and wedges it. Fails OPEN on
    an indeterminate probe (see _MOUNT_FILE_OK). For callers that have already
    established the path is mount-backed (they must not pay a second gate)."""
    return mounts.rc_kind_for(path) in _MOUNT_FILE_OK


def local_is_file(path) -> bool:
    """os.path.isfile for a LOCAL (non-mount-backed) path. A plain kernel stat is
    safe and cheap here — the mount-wedging GETATTR concern only applies under a
    managed mount. The OSError guard mirrors the callers it replaces; os.path
    already swallows OSError, so this changes nothing observable."""
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def is_file(path) -> bool:
    """Mount-safe os.path.isfile: dispatches on is_mount_backed, then answers a
    mount-backed path through the rc API (never the kernel — the wedge class) and
    a local path through the kernel. Fails OPEN on an indeterminate mount probe.

    The single gate+dispatch; callers that already know the side (recents
    pre-gates to pick an executor) use mount_is_file / local_is_file directly so
    a local path never pays a second is_mount_backed (whose symlink re-check is a
    kernel realpath)."""
    if mounts.is_mount_backed(path):
        return mount_is_file(path)
    return local_is_file(path)
