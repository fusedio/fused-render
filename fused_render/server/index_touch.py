"""Keeping the file index honest about the changes THIS APP makes.

There is no filesystem watcher (index/specs/scan.md), so the index is a
snapshot: rename a file in the explorer and the index keeps offering the old
name and cannot offer the new one until something scans that folder again. For
out-of-band edits that is the documented trade. For an edit the user just made
in this window it is not a trade, it is a visible lie.

The in-folder search used to route around it — the mutated folder, its
ancestors and its descendants were pinned to a live streamed walk for the rest
of the session (the old `indexMayAnswer` gate). With the walk gone for indexed
folders, the escape hatch has to be replaced by the fix it was standing in for:
**scan the folder the app just changed**, and let the search box say
"indexing…" while that runs.

A full rescan of the mutated FOLDER is deliberately the whole mechanism. The
alternative — patching the affected rows in the store — would be a second
implementation of what a scan already does, over a format (sorted parquet
partitions plus a dir-signature table) whose only writer is the compaction, and
the two would disagree the first time either changed.

What this module is, then, is the policy around that scan, and every rule in
it exists because the caller is a mutation endpoint rather than a person:

  * **Coalesced.** Deleting fifty files is one change to the index. Mutations
    gather for `COALESCE_S` and the folders they touch are scanned once.
  * **Outermost only.** A scan covers everything under its root, so a pending
    folder inside another pending folder is dropped.
  * **Never a mount, never `/`.** The first is the structural refusal every
    path into the scanner carries (a kernel crawl of an rclone mount can wedge
    it); the second is a whole-disk crawl one loose file in the root would
    otherwise buy.
  * **Waits out a scan already covering the folder** instead of racing it —
    and does not JOIN it, which is what `runner.start` would do on an exact
    root match: a run in flight may already have walked past the folder we
    just changed, so joining it would report success and fix nothing.

The scan root is the folder itself rather than the enclosing configured root
(what `freshness.note_folder_opened` uses for an out-of-band change). It is the
smallest walk that is certainly enough: a scan recurses, so the parent of a
renamed directory re-reads the whole subtree under its new name, and
compaction keeps every row outside the root it is given (index/store.py), so a
folder-sized scan merges into the store rather than replacing it.
"""
import logging
import os
import re
import threading

from fused_render.index.ignore import norm

# A bare Windows drive ("C:", or "C:/" before the rstrip below removes it) is
# that platform's filesystem root, the same structural case "/" is on POSIX —
# see the module docstring's "never a mount, never /". query.py's `_DRIVE`
# anchoring regex recognizes the drive-letter form on the query side; this is
# the same recognition for the root-guard side below.
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:/?$")

logger = logging.getLogger(__name__)

# How long mutations gather before the scan is started. Long enough that a
# multi-file operation (a drag of thirty files, a recursive delete) arrives as
# one burst, short enough that the search box's "indexing…" appears while the
# user is still looking at what they just did.
COALESCE_S = 1.5

# How long a folder may keep waiting for a scan that covers it to finish before
# it is scanned anyway. A whole-home incremental scan is seconds; this is far
# past that, and it exists only so a wedged worker cannot keep one folder
# circling for the lifetime of the process.
DEFER_DEADLINE_S = 120.0

# How long a folder must have been left alone before it is rescanned again.
#
# Every scan ends in a COMPACTION, and a compaction re-sorts and rewrites every
# partition in the store plus dirs.parquet — keeping the rows outside the scan
# root is a query predicate, not an incremental write (index/store.py). The
# cost of a rescan is therefore a function of the whole index rather than of
# the folder, and without a floor a caller that mutates on a timer rewrites a
# 571k-row store on that timer.
#
# A folder inside the floor is DEFERRED, never dropped, which is the whole
# difference between this and the scheduler's `SCAN_DEBOUNCE_S`: that one
# refuses a scan outright, and refusing here would leave a renamed file
# unfindable until something else happened to scan the folder — the exact
# failure this module exists to prevent. Twenty seconds rather than fifteen
# minutes for the same reason: the deferral is a delay the user waits out.
MUTATION_SCAN_FLOOR_S = 20.0

# Folders scanned per burst. A mutation batch spans one or two folders in
# practice; a hundred distinct ones is a pathological caller, and starting a
# hundred detached workers would be a worse answer to it than logging.
MAX_FOLDERS = 16


def _folder_of(path: str) -> str:
    """The folder whose contents changed, for a path that was touched.

    The parent, always — including for a directory, because a scan of the
    parent recurses into it. A renamed directory therefore needs no special
    case: both parents are noted and the subtree is re-read under its new name.
    """
    raw = str(path or "").strip()
    if not raw:
        return ""  # abspath("") is the server's cwd, which is nobody's folder
    p = norm(os.path.abspath(raw)).rstrip("/")
    if not p or p == "/" or _DRIVE_ROOT.match(p):
        return ""
    parent = os.path.dirname(p)
    return "" if parent in ("", "/") or _DRIVE_ROOT.match(parent) else parent


class RescanQueue:
    """The coalescing queue. Deps are injected so the policy is testable
    without spawning a worker or waiting on a real timer."""

    def __init__(self, start, live_run_covers, blocked, last_scan, schedule, now,
                 coalesce_s: float = COALESCE_S,
                 deadline_s: float = DEFER_DEADLINE_S,
                 floor_s: float = MUTATION_SCAN_FLOOR_S):
        self._start = start
        self._live = live_run_covers
        self._blocked = blocked
        self._last_scan = last_scan
        self._schedule = schedule
        self._now = now
        self.coalesce_s = coalesce_s
        self.deadline_s = deadline_s
        self.floor_s = floor_s
        self._lock = threading.Lock()
        # folder -> the time it was first noted, for the deferral ceiling.
        self._pending: dict = {}
        self._armed = False

    def note(self, *paths: str) -> None:
        """Record that the app changed `paths`. Returns at once; never raises."""
        try:
            folders = {f for f in (_folder_of(p) for p in paths) if f}
            if not folders:
                return
            with self._lock:
                now = self._now()
                for f in folders:
                    self._pending.setdefault(f, now)
                self._arm_locked()
        except Exception:  # noqa: BLE001 - a mutation must not fail over this
            logger.exception("could not queue an index rescan")

    def _arm_locked(self) -> None:
        if self._armed:
            return
        self._armed = True
        self._schedule(self.coalesce_s, self.fire)

    def fire(self) -> None:
        """Start the scans this burst earned. What the timer calls."""
        try:
            self._fire()
        except Exception:  # noqa: BLE001 - this runs on a bare timer thread
            logger.exception("could not start the queued index rescans")

    def _fire(self) -> None:
        with self._lock:
            self._armed = False
            pending = dict(self._pending)
            self._pending.clear()
        now = self._now()
        defer = {}
        for folder in self._outermost(pending):
            if self._blocked(folder):
                logger.info("index: not rescanning %s (nothing may scan it)",
                            folder)
                continue
            waited = now - pending[folder]
            # A run over this tree is already going to rewrite these rows —
            # unless it has walked past them, which is exactly why the folder
            # is kept rather than dropped. Same for a folder scanned moments
            # ago: the rescan is delayed, never skipped. The deadline is the
            # escape from both, so neither can hold a folder for ever.
            live = self._live(folder)
            recent = False
            if not live:
                last = self._last_scan(folder)
                recent = last is not None and (now - last) < self.floor_s
            if (live or recent) and waited < self.deadline_s:
                defer[folder] = pending[folder]
                continue
            try:
                self._start(folder)
            except Exception as e:  # noqa: BLE001 - one bad folder, not the rest
                logger.info("index: not rescanning %s (%s)", folder, e)
        if defer:
            with self._lock:
                for folder, first in defer.items():
                    self._pending.setdefault(folder, first)
                self._arm_locked()

    def _outermost(self, pending: dict) -> list:
        """The pending folders no other pending folder already covers."""
        folders = sorted(pending)
        out = []
        for f in folders:
            if out and (f == out[-1] or f.startswith(out[-1] + "/")):
                continue
            out.append(f)
        if len(out) > MAX_FOLDERS:
            logger.info("index: %d folders mutated at once; rescanning the "
                        "first %d", len(out), MAX_FOLDERS)
            out = out[:MAX_FOLDERS]
        return out


def _real_start(root: str) -> None:
    from fused_render.index import runner
    from fused_render.index.config import load_config

    started = runner.start(load_config(), root)
    logger.info("index: rescanning %s after an in-app change (run %s)",
                root, (started or {}).get("run_id"))


def _real_live(root: str) -> bool:
    from fused_render.index.config import load_config
    from fused_render.server.routers.index import _scan_in_flight

    return _scan_in_flight(load_config(), root)


def _real_last_scan(root: str):
    from fused_render.index import runner
    from fused_render.index.config import load_config

    return runner.last_scan(load_config(), root)


def _real_blocked(root: str) -> bool:
    """Whether nothing may scan `root`, for any of the three reasons.

    One question, three answers, and the caller does not care which:

      * mount-backed — `blocks`, not `blocks_root`: this is pure string work
        against the mount records, and the realpath `blocks_root` adds is a
        syscall on a path we have no reason to trust yet. `runner.start` pays
        it authoritatively.
      * excluded by the ignore rules — a save inside a `node_modules` would
        otherwise spawn a worker that walks it, indexes nothing, and rewrites
        the whole store to say so. It is the same reason the ranked route
        answers `ignored` rather than asking for a scan.
      * on another filesystem — see `foreign_device`.
    """
    from fused_render.index import runner
    from fused_render.index.config import load_config
    from fused_render.index.ignore import MountGuard, ignored_for_index

    if MountGuard(mounts_dir=runner._mounts_dir()).blocks(root):
        return True
    if ignored_for_index(load_config().rules, root, tree=True):
        return True
    return foreign_device(root)


def foreign_device(path: str) -> bool:
    """Whether `path` lives on a different filesystem than the user's home.

    Refused as a scan ROOT, and this is the one rule here that is new policy
    rather than a bug fix, so it is worth stating plainly.

    Before this phase every scan root came from the configured roots — the
    user's home, in practice. An on-demand scan takes an arbitrary folder, and
    `MountGuard` only knows about fused-render's OWN mounts dir: a user's SMB
    or NFS volume at /Volumes/share is not mount-backed as far as it is
    concerned. `scan.scan_dir_once`'s `root_dev` guard is what normally stops a
    crawl leaving the home filesystem, and it is defeated by construction when
    the root ITSELF is the network volume — the whole subtree is then fair game
    for a detached worker nobody is watching.

    The old live walk did crawl such paths, so this is not a new capability
    being taken away for nothing. But the walk was abortable, entry-capped and
    tied to a search box somebody had open; a scan is none of those, and this
    repo's history has a kernel walk permanently wedging a mount in it more
    than once. A refused folder falls back to the live walk exactly as it did
    before phase 2, which is the honest cost and a small one.

    Paid AFTER the mount guard, so a wedged fused mount is never stat'd here,
    and never raising: a path we cannot stat is one we should not scan.
    """
    import os

    try:
        return os.stat(path).st_dev != os.stat(os.path.expanduser("~")).st_dev
    except OSError:
        return True


def _real_schedule(delay: float, fn) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True  # never hold the process open for a rescan
    t.name = "index-rescan"
    t.start()


_queue = RescanQueue(start=_real_start, live_run_covers=_real_live,
                     blocked=_real_blocked, last_scan=_real_last_scan,
                     schedule=_real_schedule, now=__import__("time").time)


def note_index_mutation(*paths: str | None) -> None:
    """The app changed `paths`; rescan the folders they live in, shortly."""
    _queue.note(*[p for p in paths if isinstance(p, str) and p])
