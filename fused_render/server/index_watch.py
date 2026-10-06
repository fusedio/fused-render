"""A live filesystem watcher that keeps the file index honest between scans.

`index_touch.py` fixes the index for changes THIS APP makes. Nothing fixed
it for a file that lands from outside the app — a download, `touch
~/a.txt`, a sync client — because every existing trigger (the startup scan,
the folder-open freshness check) is a *pull* that guesses staleness from a
clock, and no time constant can answer "did anything change?" (see
SPEC-index-live-watch.md §1 for the three designs that failed on exactly
that question).

This module OBSERVES instead: `watchfiles` (the `notify` Rust crate —
FSEvents on macOS, inotify on Linux, ReadDirectoryChangesW on Windows),
running in a watcher process per configured root (`fused_render.index.
watcher`, which explains why it cannot run in this process and how it
prunes what it watches), feeds a `WatchLoop` per root over a pipe. The loop
filters at arrival (an ignored
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
import json
import logging
import queue
import subprocess
import sys
import threading
import time

from fused_render.index.watcher import make_dropped
from fused_render.server.index_touch import (
    MAX_FOLDERS,
    _canon_folder,
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


class WatchLoop:
    """The per-root policy: filter+batch a raw event stream, forward no more
    often than the flush floor, collapse an oversized burst to the root, run
    the periodic safety net on idle ticks, and back off on error.

    Every dependency is injected. The real wiring (`start`/`_real_open_source`
    below) is the only place that touches the watcher process, threads, or the
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
        # `_folder_of` (used on every touched path below) canonicalizes
        # through `norm(os.path.abspath(...))` before `_clamp_to_root` ever
        # sees the result — on Windows that adds a drive letter, turning
        # "/home/me/proj/a.txt" into "C:/home/me/proj". `root` is handed to
        # this class as-is and never goes through that same pipeline, so
        # comparing a folder against raw `root` compares two different
        # canonical forms on Windows and every folder fails containment
        # (see DECISIONS.md for the CI failure this fixed). Canonicalize
        # once, the same way, so the comparison is apples to apples.
        self._root_canon = _canon_folder(root) or root
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
        stops with no exception — what the process source does when
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
                    # tick (the process source ticks every `TICK_S`
                    # seconds while nothing arrives).
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
            # `hinted=False`: this carries no observed dirs at all — the
            # watch itself broke, so a real scan (or a journal-derived hint)
            # is what recovers whatever it missed, not a forced,
            # non-recursive visit of just the root (SPEC-scan-cost.md part
            # 2's "correctness trap", the same reasoning as the periodic
            # backstop below).
            self.forward({self.root}, hinted=False)
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
        root = self._root_canon
        if folder == root or folder.startswith(root + "/"):
            return folder
        return self.root

    def _flush(self, pending: set) -> None:
        outermost = outermost_folders(pending)
        if len(outermost) > self.max_folders:
            # A whole-root incremental walk beats scanning each of a burst's
            # many folders separately, each ending in its own compaction —
            # and it is the honest answer to a burst this loop cannot
            # attribute to anything narrower. `hinted=False`: with this many
            # distinct folders involved, a forced non-recursive visit of
            # just the root would cover almost none of them.
            self.forward({self.root}, hinted=False)
        else:
            # The RAW pending set, not `outermost` — `note_folders()`
            # (index_touch.py's `RescanQueue`) does its OWN outermost
            # collapse, and needs every originally-observed folder to hint
            # each one a collapse absorbs (SPEC-scan-cost.md part 2: "stops
            # collapsing a root-level file change into a recursive scan of
            # the root"). Collapsing here first would throw away exactly the
            # folders that collapse needs to hint correctly — e.g. a root
            # touch alongside a deep change would forward only `{root}`,
            # and a forced hint of `root` alone does not reach a deep,
            # already-cached folder the way a full/journal scan would.
            self.forward(set(pending))

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
            # `hinted=False`: the backstop exists precisely for changes this
            # loop never observed (server was off, the kernel dropped
            # events) — there is no observed-dirs set to hint, and a forced,
            # non-recursive visit of just the root would not be the real
            # scan (or journal replay) this is supposed to fall back to.
            self.forward({self.root}, hinted=False)
            self._last_periodic_rescan_at = now


# ----------------------------------------------------------------- wiring
#
# Everything below touches the watcher process, threads, or the server's
# real config. `WatchLoop` above carries the policy tests; the process
# plumbing has its own tests in tests/test_index_watch.py, including one
# against a real watch.

WATCHER_MODULE = "fused_render.index.watcher"

# How often the process source yields an empty tick while nothing changes,
# which is what drives `WatchLoop`'s flush floor and periodic backstop.
TICK_S = 5.0

# How often a source blocked on the pipe re-checks `stop_event`.
STOP_POLL_S = 0.5


def _watcher_argv(root: str) -> list:
    """The command for one root's watcher process, carrying the config the
    process needs to prune the watch and filter its events the same way
    `WatchLoop.dropped` does."""
    from fused_render.index.config import load_config

    cfg = load_config()
    spec = {"root": root, "ignore": list(cfg.rules.patterns),
            "index_dir": cfg.dir}
    return [sys.executable, "-m", WATCHER_MODULE, json.dumps(spec)]


def _end_process(proc: subprocess.Popen) -> None:
    """Close the watcher's stdin (it exits on EOF), then escalate. The
    escalation matters while the watcher is still setting up its watches:
    that runs with the child's GIL held, so it cannot read the EOF yet."""
    try:
        proc.stdin.close()
    except OSError:
        pass
    for step in (proc.terminate, proc.kill):
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            step()
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        logger.warning("index watch: watcher process %s did not exit", proc.pid)


def _spawn_kwargs() -> dict:
    """Popen kwargs for the watcher. On POSIX, `close_fds=False` (with an
    absolute `sys.executable`, no `cwd` and no `start_new_session`) keeps
    CPython on `posix_spawn`: a fork() of a server that has loaded PROJ runs
    its `pthread_atfork` handler and the child dies with SIGSEGV before exec.
    Leaving fds open costs nothing, because every fd Python opens is
    non-inheritable (PEP 446): the child gets its two pipes and not the
    server's listening socket, and no other child holds the watcher's stdin
    open past the server's exit. Same rule as `index.runner._detach_kwargs`."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"close_fds": False}


def _process_source(argv: list, stop_event, *, tick_s: float = TICK_S,
                    poll_s: float = STOP_POLL_S):
    """Run `argv` (a watcher process speaking `fused_render.index.watcher`'s
    JSON-lines protocol) and yield its changes in `watchfiles.watch`'s
    shape: a set of `(Change, path)` per batch, and an empty set every
    `tick_s` while nothing arrives. Raises when the process reports an error
    or exits on its own, which `WatchLoop` answers with a recrawl and a
    backoff. The process is ended whenever the generator is."""
    from watchfiles import Change

    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            **_spawn_kwargs())
    lines: queue.Queue = queue.Queue()

    def pump() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=pump, name="index-watch-pipe", daemon=True).start()
    try:
        last = time.monotonic()
        while not stop_event.is_set():
            try:
                line = lines.get(timeout=poll_s)
            except queue.Empty:
                if time.monotonic() - last >= tick_s:
                    last = time.monotonic()
                    yield set()
                continue
            if line is None:
                if stop_event.is_set():
                    return
                raise RuntimeError(
                    f"watcher process exited with code {proc.wait()}")
            try:
                msg = json.loads(line)
            except ValueError:
                logger.warning("index watch: unreadable watcher line %r", line)
                continue
            if "changes" in msg:
                last = time.monotonic()
                yield {(Change(c), p) for c, p in msg["changes"]}
            elif "log" in msg:
                logger.info("index watch: %s", msg["log"])
            elif "error" in msg:
                raise OSError(msg["error"])
    finally:
        _end_process(proc)


def _real_open_source(root: str, stop_event):
    """The real source for one root: a watcher process, re-spawned (with a
    freshly loaded config) every time `WatchLoop` reopens the watch."""
    return _process_source(_watcher_argv(root), stop_event)


_stop_event: threading.Event | None = None
_threads: list = []
#: True from `create_app`'s startup `start()` until its shutdown `stop()`.
#: `pause()` / `resume()` (the indexing-preference toggle) run only inside
#: that window, so a `PUT /api/prefs` against a test app that never ran
#: lifespan — and so never started a watcher — cannot start one either.
_armed = False


def _make_loop(root: str, stop_event: threading.Event) -> WatchLoop:
    from fused_render.index import runner
    from fused_render.index.config import load_config
    from fused_render.server.routers.index import _scan_in_flight
    from fused_render.shell import index_gate

    return WatchLoop(
        root,
        open_source=lambda r: _real_open_source(r, stop_event),
        dropped=make_dropped(load_config().rules,
                              index_dir=load_config().dir),
        # `WatchLoop` calls `forward(folders)` (or `forward(folders,
        # hinted=False)` for the burst-overflow, error-recovery and
        # periodic-backstop cases) with a single iterable
        # (`self.forward({self.root})`, `self.forward(set(pending))`).
        # `note_index_folders(*folders)` wants those folders UNPACKED as
        # separate positional arguments — handing it the set itself as one
        # argument fails its `isinstance(f, str)` filter and queues nothing
        # (see tests/test_index_watch.py::
        # test_a_real_flush_actually_reaches_the_rescan_queue). Unpack here,
        # at the one seam where the real callable's shape has to match, and
        # pass `hinted` straight through (SPEC-scan-cost.md part 2).
        forward=lambda folders, hinted=True: note_index_folders(
            *folders, hinted=hinted),
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
    singleton start in this codebase follows (e.g. `start_index_job_bridge`). Never starts anything a test can see: tests
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
    global _stop_event, _threads, _armed

    _armed = True
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
    global _armed

    _armed = False
    _signal_stop()


def _signal_stop() -> None:
    global _stop_event, _threads

    if _stop_event is None:
        return
    _stop_event.set()
    _stop_event = None
    _threads = []


def pause() -> None:
    """The indexing preference was turned off: end every open watch NOW.

    `WatchLoop.run` only consults the gate between watch attempts; an attempt
    already inside `_run_one_watch` keeps its watcher process, FSEvents stream
    and pipe thread alive until the server restarts, decoding and filtering
    every filesystem event only for `note_index_folders` to drop the result at
    the gate. Setting the stop event is what the open watch actually listens
    to (`_process_source`), so this is the one way to close it before
    shutdown. `_armed` is left as is: the preference pausing the watcher is
    not the server stopping it, and `resume()` needs to know the difference."""
    _signal_stop()


def resume() -> None:
    """The indexing preference was turned on: start watching again, if the
    server had the watcher running in the first place. A no-op when it
    didn't (`_armed` False), and idempotent when it still does (`start()`'s
    own early return). Call it AFTER the preference is written: the new
    thread's first gate poll reads prefs.json, and reading the old value
    there costs a full `GATE_POLL_S` before the watch opens."""
    if _armed:
        start()
