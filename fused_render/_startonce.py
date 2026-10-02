"""`StartOnceThread`: the Thread+Lock+is_alive+start idempotent-startup
pattern, pulled out of its five near-identical copies
(`ai/supervisor.py`'s `start_reaper`, `start_hardware_refresh` and
`start_hub_metadata_refresh`, `schedule.start`, and — before it grew a
"done" state a plain `is_alive()` check cannot express — `queue_manager`'s
`ensure_duties_waiter`).

A LOOPING daemon thread (sleep-tick-forever, never returns on its own) is
exactly what `is_alive()` idempotence means to say: "a thread is already
doing this forever, so there is nothing for a second call to start." That
is every one of `start_reaper`/`start_hardware_refresh`/
`start_hub_metadata_refresh`/`schedule.start`. `ensure_duties_waiter` is
NOT one of these — its thread does its work once and returns, so
`is_alive()` going False means "finished", not "never started", and a
second call must not restart it (code review finding 1). It stays on its
own hand-written `_duties_state` machine rather than this helper.

No dependency on anything but `threading`, so importing this from
`ai/supervisor.py`, `schedule.py`, `tasks_watch.py` or `queue_manager.py`
never risks a cycle."""
from __future__ import annotations

import threading
from typing import Callable


class StartOnceThread:
    """One `ensure()` call per process actually starts a thread; every
    later call, from any thread, while the one already started is still
    alive, is a fast no-op. Replaces a module's own
    `_thread: Thread | None` + `_lock = Lock()` pair and the
    check-create-assign block every caller of this used to repeat around
    it.

    `make_thread()` is called (and the thread started) only when this call
    is the one that gets to start it — never speculatively — so a caller
    building an expensive closure around `make_thread` pays for that only
    once per process.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def ensure(self, make_thread: Callable[[], threading.Thread]) -> threading.Thread:
        """Return the running thread, starting it first if this is the
        first call (or the previously started thread has since died —
        which none of this file's current callers' loops ever let happen
        on their own, since each wraps its tick in a `try/except` that
        logs and continues, but a caller is free to let its thread die and
        rely on the next `ensure()` restarting it)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._thread = make_thread()
            self._thread.start()
            return self._thread

    def reset_for_tests(self) -> None:
        """Forget the started thread. Does not join or stop it — a test
        that started a real thread through this is responsible for that
        itself; this only makes the next `ensure()` call free to start a
        new one."""
        with self._lock:
            self._thread = None
