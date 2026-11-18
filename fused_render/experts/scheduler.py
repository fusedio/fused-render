"""The experts' timer (docs/experts.md "The task-learn trigger").

One daemon thread: `start()` arms it (idempotent), the first `tick` runs `start_delay_s()` later and then every
`tick_s()` (developer settings `FUSED_RENDER_EXPERT_TICK_S` / `_START_DELAY_S`); `shutdown()` stops it and ends
every side session's harness process. A tick runs learn.py's pass; a dream (memory compaction) is queued by the sessions themselves
(sessions.after_write).

ONE PROCESS TICKS: two fused-render servers on one machine must not both read the same transcripts, so a tick
runs only in the process that owns the bots (`bots.registry.owns()`) and holds the bots workspace claim
(`registry.workspace_claimed()`, the same door `require_workspace` guards), and only while it holds the machine-wide
lease `experts-scheduler` (`tasks_store.try_acquire_lease`, released after the tick). A process that loses
either skips that tick.
"""
from __future__ import annotations

import logging
import math
import os
import random
import threading
import time

logger = logging.getLogger(__name__)

TICK_S = 60   # a task is read 5 minutes after its call (learn.AFTER_CALL_S): a slower tick would stretch that
STARTUP_DELAY_S = 90.0
TICK_ENV = "FUSED_RENDER_EXPERT_TICK_S"
START_DELAY_ENV = "FUSED_RENDER_EXPERT_START_DELAY_S"
MIN_OVERRIDE_S = 5.0
_JITTER = 0.1
LEASE = "experts-scheduler"

_LOCK = threading.Lock()
_TICK_LOCK = threading.Lock()
_timer_stop: threading.Event | None = None
_thread: threading.Thread | None = None


def _owner() -> bool:
    try:
        from fused_render.bots import registry
        return bool(registry.owns())
    except Exception:  # noqa: BLE001
        return False


def _workspace() -> bool:
    """The bots workspace claim (`registry.workspace_claimed`): a task-learn session writes the expert's folder."""
    try:
        from fused_render.bots import registry
        return bool(registry.workspace_claimed())
    except Exception:  # noqa: BLE001
        return False


def tick(stop: threading.Event | None = None) -> dict:
    """One pass. Returns `{"ran": bool, "skipped": why, "learn": {...}, "errors": n}`. Never raises."""
    out = {"ran": False, "skipped": "", "learn": {}, "errors": 0}
    if not _TICK_LOCK.acquire(blocking=False):
        out["skipped"] = "a tick is already running"
        return out
    leased = False
    try:
        if not _owner():
            out["skipped"] = "this process does not own the bots"
            return out
        if not _workspace():
            out["skipped"] = "another app holds the bots workspace"
            return out
        from fused_render import tasks_store
        leased = tasks_store.try_acquire_lease(LEASE, machine_wide=True)
        if not leased:
            out["skipped"] = "another process holds the experts lease"
            return out
        out["ran"] = True
        from fused_render.experts import learn
        out["learn"] = learn.run_pass(stop=stop)
    except Exception:  # noqa: BLE001 — never out of tick
        out["errors"] += 1
        logger.warning("experts scheduler: tick failed", exc_info=True)
    finally:
        if leased:
            try:
                from fused_render import tasks_store
                tasks_store.release_lease(LEASE)
            except Exception:  # noqa: BLE001
                logger.warning("experts scheduler: could not release the lease", exc_info=True)
        _TICK_LOCK.release()
    return out


def _env_seconds(name: str, default: float) -> float:
    """`default`, or `$name` floored at MIN_OVERRIDE_S when it is a positive number."""
    try:
        value = float(os.environ.get(name) or 0)
    except ValueError:
        value = 0.0
    return max(MIN_OVERRIDE_S, value) if value > 0 and math.isfinite(value) else float(default)


def tick_s() -> float:
    return _env_seconds(TICK_ENV, TICK_S)


def start_delay_s() -> float:
    return _env_seconds(START_DELAY_ENV, STARTUP_DELAY_S)


def _timer_loop(stop: threading.Event) -> None:
    if stop.wait(start_delay_s()):
        return
    while not stop.is_set():
        tick(stop)
        if stop.wait(tick_s() * random.uniform(1 - _JITTER, 1 + _JITTER)):
            return


def start() -> None:
    """Arm the timer (the server's startup hook) and let side sessions start again after a shutdown. Idempotent."""
    global _timer_stop, _thread
    from fused_render.experts import sessions
    sessions.reopen()
    with _LOCK:
        if _thread is not None:
            return
        _timer_stop = stop = threading.Event()
        _thread = threading.Thread(target=_timer_loop, args=(stop,), daemon=True, name="experts-scheduler")
        _thread.start()


def shutdown(budget_s: float = 5.0) -> None:
    """Stop the timer (waits up to `budget_s` for a tick in flight) and end every side session's harness."""
    global _timer_stop, _thread
    deadline = time.monotonic() + max(0.0, budget_s)  # one budget for the harnesses and the timer together
    with _LOCK:
        stop, thread = _timer_stop, _thread
        _timer_stop, _thread = None, None
    if stop is not None:
        stop.set()
    try:
        from fused_render.experts import sessions
        sessions.shutdown(max(0.0, budget_s))
    except Exception:  # noqa: BLE001
        logger.warning("experts scheduler: could not end the side sessions", exc_info=True)
    if thread is not None and thread is not threading.current_thread():
        thread.join(max(0.0, deadline - time.monotonic()))
