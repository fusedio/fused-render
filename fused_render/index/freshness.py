"""Noticing that the folder someone just opened is newer than the index, and
refreshing it in the background.

The index is a snapshot taken at startup and on demand, so a folder changed
out of band since the last scan answers search from stale rows. This module is
the whole fix: one mtime comparison, then the ORDINARY incremental scan of the
enclosing configured root. There is no new scanning path — `run_scan(...,
incremental=True)` already consults the FSEvents journal and rescans only what
moved, so a triggered run is seconds of work, and adding a second mechanism
would mean a second set of bookkeeping keyed on a root string
(`scans.json`, the applied-ignore map, the fsevents state) to get wrong.

Detection is a stat, not a walk: `dirs.parquet` already stores `mtime_ns` per
directory, so "is this folder newer than the index thinks?" is one indexed row
lookup against one `os.stat`. That also catches folders which changed while
nothing at all was watching — which a watcher tap on the pane-watch registry
would have missed.

KNOWN BOUND, by design: a directory's mtime moves only when entries are added,
removed or renamed IN THAT DIRECTORY. An edit five levels down does not flip
the mtime of the folder being viewed, so this makes the index fresher, it does
not make it correct. See specs/scan-incremental.md §5.

See specs/scan-incremental.md §5.
"""
import os
import time

from fused_render.index import runner
from fused_render.index.config import IndexConfig
from fused_render.index.ignore import MountGuard, norm

# How long a directory must have been settled before its staleness is acted on.
# A churny directory (a build tree, a cache) has a mtime that moves
# continuously, so it is never quiet and never triggers — which is what stops
# it queueing scan after scan. It costs nothing: the next open after the churn
# stops still fires.
QUIET_S = 30.0

# Floor between scans of one root STARTED by this path. Read off `scans.json`
# via runner.last_scan, which the startup scheduler and the manual buttons also
# stamp — so a scan that just ran for any reason suppresses a trigger, and the
# trigger needs no state file of its own. That is also why it cannot be dropped
# in favour of routers/index.FRESHNESS_CHECK_S: that one throttles the CHECKS
# per root, in memory, and sees nothing started by the scheduler or the buttons,
# so without this floor a folder-open could rescan seconds after either. The two
# express ONE cadence at two layers, but they do not currently ADD UP to it:
# FRESHNESS_CHECK_S is 55, under this 60, and a check stamps its own clock
# whether or not it then scans — so the check at t=55 is refused here and the
# next one is t=110, making the real folder-open cadence ~110s rather than 60.
# Known, pre-existing, and written up in full over FRESHNESS_CHECK_S, including
# why the fix is to raise THAT above this number. Still far below the startup
# debounce (SCAN_DEBOUNCE_S, 15 min): that
# one exists to stop a reload loop, this one to stop a browsing session from
# queueing.
#
# Lowered from 600s, which was itself raised from 120s on the argument that a
# completed scan invalidates every fetched corpus in the app
# (platform/lib/index-status -> index-freshness) and so a scan finishing every
# couple of minutes is a permanent "indexing…" and a permanent dimming. That
# argument no longer holds: the explorer defers a new generation instead of
# refetching under the user (apps/explorer/listing/revalidate.ts, and the
# `fetchLifecycle` pin in FilesHome.tsx), so a scan completing mid-search no
# longer disturbs the results being read.
#
# And scanning sooner is actively cheaper, not merely tolerable. Measured on a
# 588k-file index: a whole-root incremental scan is ~4.5s, of which the FSEvents
# journal replay (0.1-2.9s) and the visit of the dirs it names (1.3-2.8s) BOTH
# scale with the window since the last scan. Halving the window halves the two
# dominant terms; the fixed costs (worker spawn, dir-cache read, compaction) are
# ~1.3s and are paid either way.
#
# NOT fixed by any of this: apps/explorer/listing/index-caveat.ts still derives
# its "indexing…" caption straight from `status.scanning`, with no deferral of
# its own — so more frequent scans do mean that caption appears more often.
MIN_INTERVAL_S = 60.0


def enclosing_root(roots, path: str):
    """The configured scan root that contains `path`, deepest first, or None.

    Deepest, because roots may nest and the narrower scan answers for the
    folder sooner — firing the outer one as well would walk the subtree twice.
    Compared segment-wise so `/x/proj-old` is not read as living inside
    `/x/proj`."""
    p = norm(os.path.abspath(path))
    best = None
    for raw in roots or []:
        r = norm(os.path.abspath(os.path.expanduser(str(raw)))).rstrip("/") or "/"
        pfx = "/" if r == "/" else r + "/"
        if p == r or p.startswith(pfx):
            if best is None or len(r) > len(best):
                best = r
    return best


def indexed_mtime_ns(cfg: IndexConfig, path: str):
    """The `mtime_ns` the last scan recorded for `path`, or None.

    None covers three cases the caller treats identically — no index yet, this
    directory was never visited, and a stored 0 (the placeholder a partition
    predating the column compacts to, store._compact_locked). All three mean
    "nothing to compare against", and reading 0 as a real mtime would make the
    folder permanently stale and fire on every open.

    duckdb is imported inside the function, as everywhere else in this package:
    this runs off the folder-open path, and a call against a missing index must
    not pay the import."""
    if not os.path.exists(cfg.dirs_parquet):
        return None
    from fused_render.index.query import _q, dirs_src
    import duckdb

    try:
        row = duckdb.connect().execute(
            f"SELECT mtime_ns FROM {dirs_src(cfg)} "
            f"WHERE dir = '{_q(norm(os.path.abspath(path)))}' LIMIT 1").fetchone()
    except duckdb.Error:
        # A dirs.parquet without the column, or a generation being replaced
        # underneath us. Neither is worth an error on a housekeeping path.
        return None
    if row is None or not row[0]:
        return None
    return int(row[0])


def note_folder_opened(cfg: IndexConfig, path: str, roots, now: float | None = None):
    """The explorer opened `path`; start a rescan if the index is behind.

    Returns the root a scan was started for, or None. Every gate is ordered
    cheapest-first, so the duckdb lookup is unreachable for the common cases:
    outside the roots, mount-backed, recently scanned, still churning.

    Never raises — a listing must not fail because index housekeeping did."""
    now = time.time() if now is None else now
    root = enclosing_root(roots, path)
    if root is None:
        return None
    # BEFORE any kernel syscall on the caller's path, and pure string work
    # against the mount records: os.stat under a wedged rclone mount blocks the
    # calling thread indefinitely (this repo's documented mount-wedge class),
    # and this runs on the thread serving a listing request.
    if MountGuard(mounts_dir=runner._mounts_dir()).blocks(path):
        return None
    last = runner.last_scan(cfg, root)
    if last is not None and (now - last) < MIN_INTERVAL_S:
        return None
    try:
        disk_ns = os.stat(path).st_mtime_ns
    except OSError:
        return None  # deleted between the open and this check
    if (now - disk_ns / 1e9) < QUIET_S:
        return None
    indexed = indexed_mtime_ns(cfg, path)
    if indexed is None or disk_ns <= indexed:
        return None
    # runner.start would JOIN a live run of this root — but only on an EXACT
    # root-string match, and a triggered scan must not be the thing that
    # discovers a mismatch. Refuse outright: the run in flight is already going
    # to pick this folder up.
    if runner.active_run(cfg, root) is not None:
        return None
    runner.start(cfg, root)
    return root
