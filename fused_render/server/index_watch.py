"""A live filesystem watcher that keeps the file index honest between scans.

`index_touch.py` fixes the index for changes THIS APP makes. Nothing fixed
it for a file that lands from outside the app — a download, `touch
~/a.txt`, a sync client — because every existing trigger (the startup scan,
the folder-open freshness check) is a *pull* that guesses staleness from a
clock, and no time constant can answer "did anything change?" (see
SPEC-index-live-watch.md §1 for the three designs that failed on exactly
that question).

This module OBSERVES instead: `watchfiles` (the `notify` Rust crate —
FSEvents on macOS, inotify on Linux, ReadDirectoryChangesW on Windows) feeds
a `WatchLoop` per configured root. The loop filters at arrival (an ignored
tree, the app's own state folder, a mount — none of them may ever reach a
flush, or the index store writing itself would trigger the scan that
triggers the watcher), reduces the survivors to their parent folders,
collapses a big burst to the whole root, and forwards no more often than
`WATCH_FLUSH_FLOOR_S` to the existing coalescing policy
(`index_touch.note_index_folders`, which is `RescanQueue` underneath — this
module does not implement a second queue).

The policy in `WatchLoop` is intentionally free of `watchfiles` and of the
server: every dependency (the event source, the clock, the sleeper, the
forwarding call, the gate) is injected, so `tests/test_index_watch.py` can
drive it with a fake event source and no real watcher, thread, or timer —
the same shape `index_touch.RescanQueue`'s tests use.
"""
import logging
import os
import threading
import time

from fused_render.index.ignore import (
    MountGuard,
    ignored_for_index,
    is_inside_leaf_dir,
    norm,
)
from fused_render.server.index_touch import (
    MAX_FOLDERS,
    _folder_of,
    note_index_folders,
    outermost_folders,
)

logger = logging.getLogger(__name__)

# How often the pending folder set may be forwarded to RescanQueue. Every
# scan ends in a whole-store compaction (index_touch.py explains why), and
# churn across many different folders needs one global floor for the same
# reason RescanQueue's own per-folder floor is not enough here: this module
# forwards folders it has never seen mutated before, from a source that can
# emit a large volume of raw events over a real `~` in a short window — see
# DECISIONS.md for the live-measurement numbers that set this constant.
WATCH_FLUSH_FLOOR_S = 30.0

# The Syncthing-style backstop: if nothing forwarded a rescan of a root in
# this long AND no run is already covering it, force one on the next idle
# tick. Catches changes made while the server was off (already covered by
# the startup scan) or dropped by the kernel. The only time-based trigger
# left, and it is a floor under the watcher, never the mechanism.
WATCH_RESCAN_S = 3600.0

# How often the loop checks whether indexing has been turned back on while
# it was blocked (pref off, or no Full Disk Access yet on the packaged mac
# app).
GATE_POLL_S = 30.0

# Backoff after the watch source raises or the underlying watch cannot be
# opened: 5s, then 30s, then 120s, capped. Escalating rather than fixed so a
# genuinely wedged watch (a mount that vanished, a permissions change) does
# not spin the CPU, while a transient blip recovers fast.
BACKOFF_SCHEDULE_S = (5.0, 30.0, 120.0)


def make_dropped(rules, mounts_dir: str, index_dir: str | None = None):
    """A `path -> bool` filter: True when the watcher must never act on
    `path`. Four structural refusals — the real analogue is not
    `index_touch._real_blocked` (that one filters a scan ROOT, chosen by
    something that already decided to scan) but the FSEvents journal gate at
    `scan.py`'s `_run_fsevents`: `ignored_for_index(...) or guard.blocks(d)
    or is_inside_leaf_dir(d)`, which filters raw watched paths the same way
    this does:

      * `ignored_for_index(rules, path, tree=True)` — the ignore list,
        checked tree-wise because a watched path arrives with no vetted
        ancestors (same reason the FSEvents journal gate uses `tree=True`).
        Every branch's mounts folder is already in `default_ignore()`.
      * `MountGuard(mounts_dir=...).blocks(path)` — the structural refusal
        that survives a user emptying the ignore list; it blocks the WHOLE
        fused-render home tree, not only the mounts subdirectory — which is
        what covers the index store's own directory (`cfg.dir`) WHEN it
        sits at its default location, but `cfg.dir` is a settable config
        key, not a fixed one.
      * `index_dir` (pass `cfg.dir`) — the explicit check for the case the
        guard misses: an index dir configured OUTSIDE the fused-render home
        but under a watched root. Without this, that configuration reopens
        the self-trigger loop this filter exists to prevent (a scan writes
        parquet into `cfg.dir`, the watcher observes its own write,
        triggers the scan that triggered it). Optional only so the many
        tests that don't care about this case don't have to pass it; real
        wiring always does.
      * `is_inside_leaf_dir(path)` — whether an ANCESTOR of `path` is a leaf
        directory (`.git`, an `.app` bundle, ...). `.git` is deliberately
        NOT in the ignore names (it is a LEAF_DIR_NAME instead), so without
        this check a write to `~/repo/.git/objects/ab/cdef` would survive
        the filter and forward a folder the index deliberately never
        indexes — and an active git repo writes under `.git/objects`
        constantly, making this the hottest of the four in practice.

    Returning True for any means: this path or a change under it must never
    cause a flush. That is load-bearing, not an optimization."""
    guard = MountGuard(mounts_dir=mounts_dir)
    idx = norm(str(index_dir or ""))

    def dropped(path: str) -> bool:
        p = norm(str(path or ""))
        if not p:
            return True
        if guard.blocks(p):
            return True
        if idx and (p == idx or p.startswith(idx + "/")):
            return True
        if is_inside_leaf_dir(p):
            return True
        return ignored_for_index(rules, p, tree=True)

    return dropped


class WatchLoop:
    """The per-root policy: filter+batch a raw event stream, forward no more
    often than the flush floor, collapse an oversized burst to the root, run
    the periodic safety net on idle ticks, and back off on error.

    Every dependency is injected. The real wiring (`start`/`_real_open_source`
    below) is the only place that touches `watchfiles`, threads, or the
    server's config — this class knows about none of it, which is what lets
    `tests/test_index_watch.py` drive it with an in-memory event source."""

    def __init__(self, root: str, *, open_source, dropped, forward, now,
                 last_scan, live_run_covers, gate_open, sleep, stop_event,
                 flush_floor_s: float = WATCH_FLUSH_FLOOR_S,
                 rescan_s: float = WATCH_RESCAN_S,
                 max_folders: int = MAX_FOLDERS,
                 gate_poll_s: float = GATE_POLL_S,
                 backoff_schedule=BACKOFF_SCHEDULE_S):
        self.root = root
        self.open_source = open_source
        self.dropped = dropped
        self.forward = forward
        self.now = now
        self.last_scan = last_scan
        self.live_run_covers = live_run_covers
        self.gate_open = gate_open
        self.sleep = sleep
        self.stop_event = stop_event
        self.flush_floor_s = flush_floor_s
        self.rescan_s = rescan_s
        self.max_folders = max_folders
        self.gate_poll_s = gate_poll_s
        self.backoff_schedule = backoff_schedule
        self._backoff_i = 0
        # The loop's OWN memory of when it last asked for a periodic
        # rescan, independent of whether that ask ever turned into a
        # recorded scan (see `_maybe_periodic_rescan`).
        self._last_periodic_rescan_at: float | None = None

    def run(self) -> None:
        """Runs until `stop_event` is set. Never raises — every failure
        inside one watch attempt is caught by `_run_one_watch`, and the gate
        poll is the only thing this level does."""
        while not self.stop_event.is_set():
            if not self.gate_open():
                self.sleep(self.gate_poll_s)
                continue
            self._run_one_watch()

    def _run_one_watch(self) -> None:
        """One open-watch-until-it-ends attempt. A clean end (the generator
        stops with no exception — what `watchfiles.watch` does when
        `stop_event` is set) forwards nothing and backs off nothing: that is
        the expected shutdown path, not a failure."""
        pending: set = set()
        last_flush = None
        try:
            for batch in self.open_source(self.root):
                if self.stop_event.is_set():
                    return
                if batch:
                    # A REAL change was delivered, not just an empty
                    # `yield_on_timeout=True` tick (the real source ticks
                    # every `rust_timeout=5000` ms regardless of activity).
                    # Resetting on every tick would mean a watch that opens,
                    # gets one empty timeout tick, then raises (a vanished
                    # mount, a permissions change) restarts at the first
                    # backoff rung forever instead of escalating.
                    self._backoff_i = 0
                folders = set()
                for _change, path in batch:
                    if self.dropped(path):
                        continue
                    folder = _folder_of(path)
                    if folder:
                        folders.add(self._clamp_to_root(folder))
                pending |= folders
                now = self.now()
                if last_flush is None:
                    # Measure the floor from the first tick this attempt
                    # actually observes, not from when the watch opened —
                    # an idle watch must never "owe" a flush from clock time
                    # alone (that is exactly the time-based guessing this
                    # module replaces).
                    last_flush = now
                if pending and now - last_flush >= self.flush_floor_s:
                    self._flush(pending)
                    pending = set()
                    last_flush = now
                elif not batch and not pending:
                    self._maybe_periodic_rescan(now)
        except Exception as e:  # noqa: BLE001 - a watcher must never die
            if self.stop_event.is_set():
                return
            logger.warning("index watch: %s stopped unexpectedly (%s); "
                           "recrawling and reopening", self.root, e)
            self.forward({self.root})
            i = min(self._backoff_i, len(self.backoff_schedule) - 1)
            self.sleep(self.backoff_schedule[i])
            self._backoff_i += 1

    def _clamp_to_root(self, folder: str) -> str:
        """`_folder_of` returns a touched path's PARENT, which for a change
        ON the watched root itself (touch/chmod on `~`, a rename or delete
        of the root, a top-level event under the non-recursive fallback) is
        the root's parent — `/Users` for a root of `~`. `_real_blocked`
        would not refuse that: it is a legitimate, unignored folder, just
        one broader than any configured root. Clamp it back to the root
        rather than let one such event buy a scan wider than the watcher is
        allowed to trigger."""
        if folder == self.root or folder.startswith(self.root + "/"):
            return folder
        return self.root

    def _flush(self, pending: set) -> None:
        outermost = outermost_folders(pending)
        if len(outermost) > self.max_folders:
            # A whole-root incremental walk beats scanning each of a burst's
            # many folders separately, each ending in its own compaction —
            # and it is the honest answer to a burst this loop cannot
            # attribute to anything narrower.
            self.forward({self.root})
        else:
            self.forward(set(outermost))

    def _maybe_periodic_rescan(self, now: float) -> None:
        """The Syncthing-style backstop (spec §3.1.9). Gating on
        `last_scan(root)` alone is not enough: `last_scan` only records a
        scan that actually STARTED, and `RescanQueue._fire` can refuse the
        folder (ignored, foreign device) or `runner.start` can decline —
        neither ever writes `last_scan`. Without its own memory the loop
        would forward `{root}` again on every idle tick forever whenever a
        forward doesn't turn into a recorded scan. `_last_periodic_rescan_at`
        is that memory: once this loop has asked, it does not ask again
        until `rescan_s` has passed since either a real scan OR its own last
        ask, whichever is more recent."""
        if self.live_run_covers(self.root):
            return
        last = self.last_scan(self.root)
        if self._last_periodic_rescan_at is not None:
            last = (self._last_periodic_rescan_at if last is None
                    else max(last, self._last_periodic_rescan_at))
        if last is None or (now - last) >= self.rescan_s:
            self.forward({self.root})
            self._last_periodic_rescan_at = now


# ----------------------------------------------------------------- wiring
#
# Everything below touches `watchfiles`, threads, or the server's real
# config, and is exercised by exactly one test (the real-filesystem check in
# tests/test_index_watch.py) plus the live measurement in DECISIONS.md —
# `WatchLoop` above carries the policy tests.


def _is_watch_limit_error(exc: BaseException) -> bool:
    """Whether `exc` is Linux's inotify watch-limit failure (ENOSPC from the
    kernel when `fs.inotify.max_user_watches` is exhausted), as opposed to
    some other OSError a recursive open can raise."""
    import errno

    return isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC


def _shallow_watch_paths(root: str, rules, mounts_dir: str,
                          index_dir: str | None = None) -> list:
    """§3.2: the root plus its immediate non-ignored subdirectories, for a
    non-recursive fallback watch. `watchfiles` has no depth limit —
    `recursive` is all-or-nothing — so this is the closest a non-recursive
    open gets to the real thing: it still catches `~/Downloads/foo.dmg` and
    `~/a.txt`; anything deeper falls to the periodic rescan."""
    dropped = make_dropped(rules, mounts_dir, index_dir=index_dir)
    paths = [root]
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                p = norm(entry.path)
                if not dropped(p):
                    paths.append(p)
    except OSError:
        pass
    return paths


def _real_open_source(root: str, stop_event):
    """The real `watchfiles.watch`-backed source for one root, with the
    Linux shallow fallback on the inotify watch-limit error (§3.2)."""
    import watchfiles

    from fused_render.index.config import load_config
    from fused_render.index.runner import _mounts_dir

    cfg = load_config()
    dropped = make_dropped(cfg.rules, _mounts_dir(), index_dir=cfg.dir)

    def _filter(_change, path) -> bool:
        return not dropped(path)

    try:
        yield from watchfiles.watch(root, watch_filter=_filter,
                                    stop_event=stop_event, rust_timeout=5000,
                                    yield_on_timeout=True,
                                    ignore_permission_denied=True)
    except OSError as e:
        if not _is_watch_limit_error(e):
            raise
        logger.warning("index watch: hit the platform watch limit opening "
                       "%s (raise fs.inotify.max_user_watches); falling "
                       "back to a shallow, non-recursive watch", root)
        paths = _shallow_watch_paths(root, cfg.rules, _mounts_dir(),
                                      index_dir=cfg.dir)
        yield from watchfiles.watch(*paths, watch_filter=_filter,
                                    stop_event=stop_event, rust_timeout=5000,
                                    yield_on_timeout=True, recursive=False,
                                    ignore_permission_denied=True)


_stop_event: threading.Event | None = None
_threads: list = []


def _make_loop(root: str, stop_event: threading.Event) -> WatchLoop:
    from fused_render.index import runner
    from fused_render.index.config import load_config
    from fused_render.server.routers.index import _scan_in_flight
    from fused_render.shell import index_gate

    return WatchLoop(
        root,
        open_source=lambda r: _real_open_source(r, stop_event),
        dropped=make_dropped(load_config().rules, runner._mounts_dir(),
                              index_dir=load_config().dir),
        # `WatchLoop` calls `forward(folders)` with a single iterable
        # (`self.forward({self.root})`, `self.forward(set(outermost))`).
        # `note_index_folders(*folders)` wants those folders UNPACKED as
        # separate positional arguments — handing it the set itself as one
        # argument fails its `isinstance(f, str)` filter and queues nothing
        # (see tests/test_index_watch.py::
        # test_a_real_flush_actually_reaches_the_rescan_queue). Unpack here,
        # at the one seam where the real callable's shape has to match.
        forward=lambda folders: note_index_folders(*folders),
        now=time.time,
        last_scan=lambda r: runner.last_scan(load_config(), r),
        live_run_covers=lambda r: _scan_in_flight(load_config(), r),
        gate_open=index_gate.indexing_allowed,
        # `stop_event.wait(delay)` is `time.sleep(delay)` that returns as
        # soon as `stop_event` is set instead of ignoring it until it wakes
        # on its own — without this a thread parked in the 30s gate poll or
        # the 120s backoff keeps an open watch (and can still start a scan)
        # for up to that long after shutdown was requested. Same
        # `sleep(delay)` shape `WatchLoop` and its tests already rely on.
        sleep=stop_event.wait,
        stop_event=stop_event,
    )


def start() -> None:
    """Start one watcher thread per configured root. Idempotent — a second
    call while already running is a no-op, the same convention every other
    singleton start in this codebase follows (e.g. `shell_mounts.
    start_health_monitor`). Never starts anything a test can see: tests
    build apps without running lifespan, so this is only ever called from
    `create_app`'s startup handler.

    Never raises. `_lifespan` (app.py) awaits every startup handler with no
    try, so a raise here would stop the whole server from booting over an
    optional background feature — this degrades (log, start what it can)
    instead. `_stop_event` is assigned BEFORE the per-root loop, and each
    root's setup is caught individually, so a bad root neither orphans the
    good roots' threads (with `_stop_event` still `None`, `stop()` would
    return immediately and never reach them) nor stops the rest from
    starting."""
    global _stop_event, _threads

    if _stop_event is not None:
        return

    stop = threading.Event()
    _stop_event = stop
    _threads = []

    try:
        from fused_render.index.config import load_config
        from fused_render.server.routers import index as index_routes

        roots = index_routes.scan_roots(load_config())
    except Exception:
        logger.exception("index watch: could not determine which roots to "
                         "watch; the live watcher will not run")
        return

    for root in roots:
        try:
            loop = _make_loop(root, stop)
            t = threading.Thread(target=loop.run, name=f"index-watch-{root}",
                                 daemon=True)
            t.start()
            _threads.append(t)
        except Exception:
            logger.exception("index watch: could not start a watcher for "
                             "%s; the other roots are unaffected", root)


def stop() -> None:
    """Signal every watcher thread to stop. Does not join — the threads are
    daemons and a clean generator end (§ `_run_one_watch`'s docstring) is
    fast, but shutdown must not block on it."""
    global _stop_event, _threads

    if _stop_event is None:
        return
    _stop_event.set()
    _stop_event = None
    _threads = []
