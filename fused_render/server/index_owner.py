"""One live index watcher per Mac, under the low-impact indexing flag.

Every server process (the installed app, each branch dev server) used to run
its own watcher (`index_watch.start()`: one FSEvents child per root) and its
own rescan queue, so N servers paid N times for the same filesystem events.
With `index_low_impact_enabled` on, a machine-wide lease picks ONE owner:

  * `index-watcher.lock` — a non-blocking flock beside the duties lease
    (`tasks_store.try_acquire_lease`). Whoever holds it runs the watcher; the
    kernel drops it when the holder dies, so a survivor takes over on its
    next retry.
  * `index-watcher.owner.json` — `{"pid", "preferred", "since"}`, written by
    the holder, for diagnostics and tests.
  * `index-watcher.preempt.json` — `{"pid", "since"}`, written by a
    PREFERRED process (the installed baseline :1777 app, `is_preferred_owner`)
    that could not get the lease. A non-preferred holder that sees a request
    from a live pid hands over: it stops its watcher, releases the lease, and
    does not re-claim while the request stands. A request (or owner record)
    from a dead pid is stale and ignored/cleaned.

Flag OFF: nothing here runs; `index_watch.start()` is called as it always was.

`OwnerLoop` is the policy with every dependency injected (lease calls, clock,
sleeper, pid probe, file dir), so tests drive `tick()` one step at a time with
no real 30 s sleeps. `ensure_owner_waiter` / `stop_waiter` / `owns` are the
process-wide wiring.
"""
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

LEASE_NAME = "index-watcher.lock"
OWNER_FILE = "index-watcher.owner.json"
PREEMPT_FILE = "index-watcher.preempt.json"

#: How often a holder re-checks for a preempt request.
HOLD_POLL_S = 30.0
#: First retry delay after failing to acquire, doubling up to the cap.
RETRY_S = 10.0
RETRY_MAX_S = 60.0
#: Pause after handing the lease over, before trying again.
HANDOVER_PAUSE_S = 5.0


def is_preferred_owner() -> bool:
    """Whether this process is the installed baseline Render app — no branch
    ref (so it serves on the flavor's port base, 1777) and not the Bot
    flavor. That process wins the watcher whenever it is running."""
    from fused_render import _branch, _flavor

    return _branch.branch_ref() == "" and not _flavor.is_bot()


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _state_dir() -> str:
    from fused_render import tasks_store

    return tasks_store.STATE_DIR


def _default_try_acquire(name: str) -> bool:
    from fused_render import tasks_store

    return tasks_store.try_acquire_lease(name)


def _default_release(name: str) -> None:
    from fused_render import tasks_store

    tasks_store.release_lease(name)


class OwnerLoop:
    """The ownership policy. `tick()` makes one transition and returns how
    long to sleep before the next; `run()` loops it until `stop_event`."""

    def __init__(self, on_acquire, on_release, *, preferred: bool,
                 try_acquire=_default_try_acquire, release=_default_release,
                 now=time.time, sleep=None, pid_alive=_pid_alive,
                 state_dir=_state_dir, pid: int | None = None,
                 stop_event: threading.Event | None = None):
        self.on_acquire = on_acquire
        self.on_release = on_release
        self.preferred = preferred
        self._try_acquire = try_acquire
        self._release = release
        self.now = now
        self.stop_event = stop_event or threading.Event()
        self.sleep = sleep or self.stop_event.wait
        self.pid_alive = pid_alive
        self._state_dir = state_dir
        self.pid = os.getpid() if pid is None else pid
        self.owned = False
        self._retry = RETRY_S
        self._lock = threading.RLock()

    # ------------------------------------------------------------ files
    def _path(self, name: str) -> str:
        return os.path.join(self._state_dir(), name)

    def _read(self, name: str):
        try:
            with open(self._path(name), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _write(self, name: str, data: dict) -> None:
        path = self._path(name)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{self.pid}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError:
            logger.warning("index owner: could not write %s", path,
                           exc_info=True)

    def _remove(self, name: str) -> None:
        try:
            os.remove(self._path(name))
        except OSError:
            pass

    def live_preempt(self):
        """The standing preempt request from ANOTHER live process, or None.
        A request from a dead pid is deleted on sight."""
        req = self._read(PREEMPT_FILE)
        if not req:
            return None
        pid = req.get("pid")
        if pid == self.pid:
            return None
        if not self.pid_alive(pid):
            self._remove(PREEMPT_FILE)
            return None
        return req

    def _remove_own_preempt(self) -> None:
        req = self._read(PREEMPT_FILE)
        if req and req.get("pid") == self.pid:
            self._remove(PREEMPT_FILE)

    def _remove_own_owner(self) -> None:
        rec = self._read(OWNER_FILE)
        if rec is None or rec.get("pid") == self.pid:
            self._remove(OWNER_FILE)

    # ------------------------------------------------------------ policy
    def tick(self) -> float:
        with self._lock:
            if self.stop_event.is_set():
                return 0.0
            if self.owned:
                return self._tick_owned()
            return self._tick_waiting()

    def _tick_owned(self) -> float:
        if self.preferred or self.live_preempt() is None:
            return HOLD_POLL_S
        logger.info("index owner: a preferred process asked for the watcher; "
                    "handing it over")
        self._safe(self.on_release)
        self._remove_own_owner()
        self._release(LEASE_NAME)
        self.owned = False
        self._retry = RETRY_S
        return HANDOVER_PAUSE_S

    def _tick_waiting(self) -> float:
        if not self.preferred and self.live_preempt() is not None:
            # A preferred process is waiting for the lease: do not race it
            # back for it (we may have just handed it over).
            return self._backoff()
        if self._try_acquire(LEASE_NAME):
            self.owned = True
            self._retry = RETRY_S
            self._write(OWNER_FILE, {"pid": self.pid,
                                     "preferred": self.preferred,
                                     "since": self.now()})
            self._remove_own_preempt()
            logger.info("index owner: this process owns the index watcher")
            self._safe(self.on_acquire)
            return HOLD_POLL_S
        if self.preferred:
            req = self._read(PREEMPT_FILE)
            if not req or req.get("pid") != self.pid:
                self._write(PREEMPT_FILE, {"pid": self.pid,
                                           "since": self.now()})
        return self._backoff()

    def _backoff(self) -> float:
        delay = self._retry
        self._retry = min(self._retry * 2, RETRY_MAX_S)
        return delay

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - the owner loop must never die
            logger.exception("index owner: callback failed")

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                delay = self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("index owner: tick failed")
                delay = RETRY_MAX_S
            if self.stop_event.is_set():
                break
            self.sleep(delay)

    def stop(self) -> None:
        """End the loop and give back everything this process holds: the
        lease, the owner record, and any preempt request of ours. Does not
        call `on_release` — the caller decides what replaces the watcher."""
        with self._lock:
            self.stop_event.set()
            if self.owned:
                self._remove_own_owner()
                self._release(LEASE_NAME)
                self.owned = False
            self._remove_own_preempt()


# ----------------------------------------------------------------- wiring

_waiter: OwnerLoop | None = None
_waiter_lock = threading.Lock()


def ensure_owner_waiter(on_acquire, on_release) -> OwnerLoop:
    """Start, once per process, the daemon thread that competes for the
    watcher lease. Idempotent: a running waiter is returned as is."""
    global _waiter
    with _waiter_lock:
        if _waiter is not None and not _waiter.stop_event.is_set():
            return _waiter
        loop = OwnerLoop(on_acquire, on_release,
                         preferred=is_preferred_owner())
        _waiter = loop
    threading.Thread(target=loop.run, name="index-owner", daemon=True).start()
    return loop


def stop_waiter() -> None:
    """Stop the waiter and release the lease if held. Idempotent."""
    global _waiter
    with _waiter_lock:
        loop, _waiter = _waiter, None
    if loop is not None:
        loop.stop()


def waiter_running() -> bool:
    w = _waiter
    return w is not None and not w.stop_event.is_set()


def owns() -> bool:
    """Whether this process currently holds the watcher lease."""
    w = _waiter
    return w is not None and w.owned and not w.stop_event.is_set()


def on_flag_changed(enabled: bool) -> None:
    """The prefs hook for `index_low_impact_enabled`, after the write.

    Acts only when this server is actually running the live watcher (an app
    built without lifespan has neither a watcher nor a waiter, so a PUT
    there starts nothing). Idempotent both ways."""
    from fused_render.server import index_watch

    if enabled:
        if waiter_running():
            return
        if not index_watch.is_armed():
            return
        # Not the owner yet: stop the free-running watcher; the waiter
        # restarts it via on_acquire if (when) this process wins the lease.
        index_watch.stop()
        ensure_owner_waiter(on_acquire=index_watch.start,
                            on_release=index_watch.stop)
    else:
        if not waiter_running():
            return
        stop_waiter()
        # Restart rather than just start: a watch this process already owns
        # was built with the low-impact drops, and `start()` alone is a no-op
        # while it runs.
        index_watch.stop()
        index_watch.start()


def reset_for_tests() -> None:
    stop_waiter()
