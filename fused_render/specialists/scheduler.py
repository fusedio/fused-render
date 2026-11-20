"""The daily update's timer: one daemon thread, a tick every ten minutes while the app is open and the flag is on,
starting each specialist's update once it is due (`update.due`)."""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

TICK_S = 600.0
_LOCK = threading.Lock()
_stop: threading.Event | None = None


def tick() -> None:
    from fused_render.specialists import store, update

    now = time.time()
    for spec in store.all():
        try:
            update.settle(spec)
            if update.due(spec, now):
                update.start(spec)
        except Exception:  # noqa: BLE001 — one specialist's failure must not stop the others'
            logger.warning("specialists: the update of %s failed to start", spec.name, exc_info=True)


def _loop(stop: threading.Event) -> None:
    while not stop.wait(TICK_S):
        tick()


def start() -> None:
    """Idempotent."""
    global _stop
    with _LOCK:
        if _stop is None:
            _stop = threading.Event()
            threading.Thread(target=_loop, args=(_stop,), daemon=True, name="specialists-scheduler").start()


def stop() -> None:
    global _stop
    with _LOCK:
        if _stop is not None:
            _stop.set()
            _stop = None
