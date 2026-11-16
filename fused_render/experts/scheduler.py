"""The experts' background pass (docs/experts.md §4 `scheduler.py`).

One daemon timer thread, `canvas_mirror`'s shape: `start()` arms it
(idempotent), the first `tick` runs `STARTUP_DELAY_S` after it and then every
`TICK_S`; `shutdown()` stops it; `reset()` forgets everything (tests).

`tick()`: capture due task sessions for the experts they are relevant to →
`consolidate.run` for every expert whose observations are due → one proposal
for the lessons that keep recurring (`routes.propose`: `proposals.request`,
which drafts in the background and shows its own card when ready; none while
one is being prepared) → `cards.tick()` for any proposal still waiting for its
card. It never raises.

ONE PROCESS TICKS. Two fused-render servers on one machine (the app and a dev
server) must not both read the same transcripts or both consolidate one
expert, so a tick runs only in the process that owns the bots
(`bots.registry.owns()`: the experts are bots, and only the owner can list
them) and only while it holds the machine-wide lease `experts-scheduler`
(`tasks_store.try_acquire_lease`, a non-blocking flock released after the
tick). A process that loses either simply skips that tick.
"""
from __future__ import annotations

import importlib
import logging
import random
import threading
import time

logger = logging.getLogger(__name__)

TICK_S = 300
STARTUP_DELAY_S = 90.0
_JITTER = 0.1
LEASE = "experts-scheduler"
#: Due sessions read per tick; the rest wait for the next one.
CAPTURE_PER_TICK = 4

_LOCK = threading.Lock()
_TICK_LOCK = threading.Lock()
_started = False
_timer_stop: threading.Event | None = None
_thread: threading.Thread | None = None


def _mod(name: str):
    return importlib.import_module("fused_render.experts." + name)


def _owner() -> bool:
    try:
        from fused_render.bots import registry
        return bool(registry.owns())
    except Exception:  # noqa: BLE001
        return False


def _acquire() -> bool:
    from fused_render import tasks_store
    return tasks_store.try_acquire_lease(LEASE, machine_wide=True)


def _release() -> None:
    try:
        from fused_render import tasks_store
        tasks_store.release_lease(LEASE)
    except Exception:  # noqa: BLE001
        logger.warning("experts scheduler: could not release the lease", exc_info=True)


def tick(stop: threading.Event | None = None) -> dict:
    """One pass. Returns what it did: `{"ran": bool, "skipped": why,
    "capture": {...}, "consolidated": [ids], "proposed": [ids], "errors": n}`."""
    out = {"ran": False, "skipped": "", "capture": {}, "consolidated": [], "proposed": [], "errors": 0}
    if not _TICK_LOCK.acquire(blocking=False):
        out["skipped"] = "a tick is already running"
        return out
    leased = False
    try:
        if not _owner():
            out["skipped"] = "this process does not own the bots"
            return out
        try:
            leased = _acquire()
        except Exception:  # noqa: BLE001
            logger.warning("experts scheduler: lease failed", exc_info=True)
            leased = False
        if not leased:
            out["skipped"] = "another process holds the experts lease"
            return out
        out["ran"] = True
        _run(out, stop)
    except Exception:  # noqa: BLE001 — never out of tick
        out["errors"] += 1
        logger.warning("experts scheduler: tick failed", exc_info=True)
    finally:
        if leased:
            _release()
        _TICK_LOCK.release()
    return out


def _stopped(stop) -> bool:
    return stop is not None and stop.is_set()


def _run(out: dict, stop) -> None:
    store = _mod("store")
    try:
        out["capture"] = _mod("capture").capture_due(limit=CAPTURE_PER_TICK, stop=stop)
    except Exception:  # noqa: BLE001
        out["errors"] += 1
        logger.warning("experts scheduler: capture failed", exc_info=True)
    if _stopped(stop):
        return
    try:
        experts = list(store.all_experts())
    except Exception:  # noqa: BLE001
        out["errors"] += 1
        logger.warning("experts scheduler: could not list experts", exc_info=True)
        experts = []
    for bot in experts:
        if _stopped(stop):
            return
        try:
            if not _mod("model").available(bot)[0]:
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            if _mod("observations").due(bot):
                res = _mod("consolidate").run(bot) or {}
                if res.get("ran"):
                    out["consolidated"].append(bot.id)
        except Exception:  # noqa: BLE001
            out["errors"] += 1
            logger.warning("experts scheduler: consolidate %s failed", bot.id, exc_info=True)
        if _stopped(stop):
            return
        try:
            out["proposed"] += _mod("routes").propose(bot)
        except Exception:  # noqa: BLE001
            out["errors"] += 1
            logger.warning("experts scheduler: proposing for %s failed", bot.id, exc_info=True)
    if _stopped(stop):
        return
    try:
        _mod("cards").tick()
    except Exception:  # noqa: BLE001
        out["errors"] += 1
        logger.warning("experts scheduler: cards.tick failed", exc_info=True)


def _timer_loop(stop: threading.Event) -> None:
    if stop.wait(STARTUP_DELAY_S):
        return
    while not stop.is_set():
        try:
            tick(stop)
        except Exception:  # noqa: BLE001 — never out of a helper thread
            logger.warning("experts scheduler: pass failed", exc_info=True)
        if stop.wait(TICK_S * random.uniform(1 - _JITTER, 1 + _JITTER)):
            return


def started() -> bool:
    with _LOCK:
        return _started


def start() -> None:
    """Arm the timer (the server's startup hook). Idempotent."""
    global _started, _timer_stop, _thread
    with _LOCK:
        if _started:
            return
        _started = True
        _timer_stop = stop = threading.Event()
        _thread = threading.Thread(target=_timer_loop, args=(stop,), daemon=True,
                                   name="experts-scheduler")
    _thread.start()


def shutdown(budget_s: float = 5.0) -> None:
    """Stop the timer; waits up to `budget_s` for a tick in flight to notice.
    A no-op when it was never started."""
    global _started, _timer_stop, _thread
    with _LOCK:
        stop, thread = _timer_stop, _thread
        _started, _timer_stop, _thread = False, None, None
    if stop is not None:
        stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(max(0.0, budget_s))


def reset() -> None:
    """Forget everything this process knows (tests)."""
    shutdown(budget_s=1.0)
    _release()
