"""The experts' background pass (docs/experts.md §4 `scheduler.py`).

One daemon timer thread, `canvas_mirror`'s shape: `start()` arms it
(idempotent), the first `tick` runs `start_delay_s()` after it and then every
`tick_s()` (`STARTUP_DELAY_S` / `TICK_S` unless overridden by a developer
setting, docs/experts.md "Developer settings"); `shutdown()` stops it; `reset()` forgets everything (tests).

`tick()`: capture due task sessions for the experts they are relevant to
(skipped outright, sessions not even listed, when no available expert is
offered to any target or was ever consulted) → `consolidate.run` (Learn) for
every expert whose manifest has `learning` on; the run itself decides whether
anything is due. `poke(bot)` starts a Learn run for one expert right away, in
the background (chat calls it after the user teaches or forgets something).
It never raises.

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
import math
import os
import random
import threading
import time

logger = logging.getLogger(__name__)

TICK_S = 300
STARTUP_DELAY_S = 90.0
TICK_ENV = "FUSED_RENDER_EXPERT_TICK_S"
START_DELAY_ENV = "FUSED_RENDER_EXPERT_START_DELAY_S"
#: The smallest override either developer setting accepts.
MIN_OVERRIDE_S = 5.0
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
    "capture": {...}, "consolidated": [ids], "errors": n}`."""
    out = {"ran": False, "skipped": "", "capture": {}, "consolidated": [], "errors": 0}
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


def _takes_sessions(bot) -> bool:
    """Whether any task session could be relevant to `bot` (`capture.relevant`): it is offered to some target, or
    some session has consulted it. Cheap: the manifest and the newest consult record, no transcript."""
    try:
        if (_mod("store").manifest(bot).get("offered_to") or {}).get("targets"):
            return True
        return bool(_mod("consult").log(bot, limit=1))
    except Exception:  # noqa: BLE001 — unsure: let capture decide
        return True


def _available(experts) -> list:
    out = []
    for bot in experts:
        try:
            if _mod("model").available(bot)[0]:
                out.append(bot)
        except Exception:  # noqa: BLE001
            continue
    return out


def _run(out: dict, stop) -> None:
    store = _mod("store")
    try:
        experts = _available(list(store.all_experts()))
        listed = True
    except Exception:  # noqa: BLE001
        out["errors"] += 1
        logger.warning("experts scheduler: could not list experts", exc_info=True)
        experts, listed = [], False
    # Capture lists and stats task sessions (and reads a due one's transcript tail), so it runs only when some
    # available expert could find a session relevant at all (or the experts could not be listed: capture decides).
    if not listed or any(_takes_sessions(b) for b in experts):
        try:
            out["capture"] = _mod("capture").capture_due(limit=CAPTURE_PER_TICK, stop=stop)
        except Exception:  # noqa: BLE001
            out["errors"] += 1
            logger.warning("experts scheduler: capture failed", exc_info=True)
    else:
        out["capture"] = {"sessions": 0, "observations": 0, "errors": 0, "skipped": "no expert takes sessions"}
    if _stopped(stop):
        return
    for bot in experts:
        if _stopped(stop):
            return
        try:
            if _learning(bot) and (_mod("consolidate").run(bot) or {}).get("ran"):
                out["consolidated"].append(bot.id)
        except Exception:  # noqa: BLE001
            out["errors"] += 1
            logger.warning("experts scheduler: learning for %s failed", bot.id, exc_info=True)


def _learning(bot) -> bool:
    return _mod("store").manifest(bot).get("learning") is not False


def poke(bot, force=False) -> bool:
    """Run Learn for `bot` soon: `consolidate.start`, a background thread, one run per expert at a time. False
    when learning is off for it, a run is already going, or nothing is due. Never raises, never blocks."""
    try:
        return _learning(bot) and bool(_mod("consolidate").start(bot, force=force))
    except Exception:  # noqa: BLE001
        logger.warning("experts scheduler: poke %s failed", getattr(bot, "id", "?"), exc_info=True)
        return False


def _env_seconds(name: str, default: float) -> float:
    """`default`, or `$name` floored at `MIN_OVERRIDE_S` when it is a positive
    number; a malformed or non-positive value is ignored. Read at call time."""
    raw = os.environ.get(name)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0 and math.isfinite(value):
            return max(MIN_OVERRIDE_S, value)
    return float(default)


def tick_s() -> float:
    return _env_seconds(TICK_ENV, TICK_S)


def start_delay_s() -> float:
    return _env_seconds(START_DELAY_ENV, STARTUP_DELAY_S)


def _timer_loop(stop: threading.Event) -> None:
    if stop.wait(start_delay_s()):
        return
    while not stop.is_set():
        try:
            tick(stop)
        except Exception:  # noqa: BLE001 — never out of a helper thread
            logger.warning("experts scheduler: pass failed", exc_info=True)
        if stop.wait(tick_s() * random.uniform(1 - _JITTER, 1 + _JITTER)):
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
