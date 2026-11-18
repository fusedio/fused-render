"""The specialists' timer (PLAN A.2 "Nightly scheduler", C.3).

One daemon thread, started only while the flag is on (`start`, the server's startup hook and the prefs switch;
idempotent). The first tick runs `start_delay_s()` after it, then every `tick_s()` (`FUSED_RENDER_SPECIALIST_TICK_S`
/ `_START_DELAY_S`, floor 5 s). A tick runs only while it holds `flock(<root>/.state/scheduler.lock)`, so two
server processes never review or update the same specialist twice. Each tick: the due reviews
(`reviewer.review_pass`), then at most ONE due nightly run (`nightly.due`).

`start` first recovers what a stopped server left half done: a nightly run cut short is closed or finished
(`nightly.recover`); a review left pending is simply due again.
"""
from __future__ import annotations

import logging
import math
import os
import random
import threading
import time

logger = logging.getLogger(__name__)

TICK_S = 60
START_DELAY_S = 90.0
TICK_ENV = "FUSED_RENDER_SPECIALIST_TICK_S"
START_DELAY_ENV = "FUSED_RENDER_SPECIALIST_START_DELAY_S"
MIN_OVERRIDE_S = 5.0
_JITTER = 0.1

_LOCK = threading.Lock()
_stop: threading.Event | None = None
_thread: threading.Thread | None = None


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or 0)
    except ValueError:
        value = 0.0
    return max(MIN_OVERRIDE_S, value) if value > 0 and math.isfinite(value) else float(default)


def tick_s() -> float:
    return _env_seconds(TICK_ENV, TICK_S)


def start_delay_s() -> float:
    return _env_seconds(START_DELAY_ENV, START_DELAY_S)


def app_idle_for(now: float) -> float:
    """Seconds since the newest agent run dir changed (any task or chat in the app), or a very large number."""
    try:
        from fused_render.claude_agent import agent_module
        runs = agent_module().RUNS
        newest = 0.0
        with os.scandir(runs) as it:
            for e in it:
                try:
                    newest = max(newest, e.stat().st_mtime)
                except OSError:
                    continue
        return now - newest if newest else float("inf")
    except Exception:  # noqa: BLE001
        return float("inf")


def _specialists() -> list:
    try:
        from fused_render.specialists import store
        return list(store.all())
    except Exception:  # noqa: BLE001
        logger.warning("specialists scheduler: could not list specialists", exc_info=True)
        return []


def _lock_path() -> str:
    from fused_render.specialists import paths
    return os.path.join(paths.state_root(), "scheduler.lock")


def tick(stop: threading.Event | None = None, now: float | None = None) -> dict:
    """One pass. Returns `{"ran", "skipped", "reviews": {...}, "nightly": {...} | None, "errors"}`. Never raises."""
    out = {"ran": False, "skipped": "", "reviews": {}, "nightly": None, "errors": 0}
    try:
        from fused_render.specialists import flag
        if not flag.on():
            out["skipped"] = "specialists are off"
            return out
        specs = _specialists()
        if not specs:
            out["skipped"] = "no specialists"
            return out
        from fused_render.specialists.counts import file_lock
        with file_lock(_lock_path(), blocking=False) as held:
            if not held:
                out["skipped"] = "another process holds the scheduler"
                return out
            out["ran"] = True
            from fused_render.specialists import nightly, reviewer
            out["reviews"] = reviewer.review_pass(now=now, stop=stop)
            now = time.time() if now is None else now
            idle = app_idle_for(now)
            for spec in specs:
                if stop is not None and stop.is_set():
                    break
                if nightly.due(spec.id, now, idle):
                    out["nightly"] = nightly.run_job(spec.id)
                    break
    except Exception:  # noqa: BLE001 — never out of tick
        out["errors"] += 1
        logger.warning("specialists scheduler: tick failed", exc_info=True)
    return out


def recover() -> None:
    """What a stopped server left half done (nightly runs cut short). Never raises."""
    try:
        from fused_render.specialists import nightly
        for spec in _specialists():
            nightly.recover(spec.id)
    except Exception:  # noqa: BLE001
        logger.warning("specialists scheduler: recovery failed", exc_info=True)


def _loop(stop: threading.Event) -> None:
    recover()
    if stop.wait(start_delay_s()):
        return
    while not stop.is_set():
        tick(stop)
        if stop.wait(tick_s() * random.uniform(1 - _JITTER, 1 + _JITTER)):
            return


def start() -> None:
    """Arm the timer when the flag is on. Idempotent; with the flag off nothing starts."""
    global _stop, _thread
    try:
        from fused_render.specialists import flag
        if not flag.on():
            return
    except Exception:  # noqa: BLE001
        return
    try:
        from fused_render.specialists import harness
        harness.reopen()  # side sessions may start again after a stop
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        if _thread is not None and _thread.is_alive():
            return
        _stop = ev = threading.Event()
        _thread = threading.Thread(target=_loop, args=(ev,), daemon=True, name="specialists-scheduler")
        _thread.start()


def running() -> bool:
    with _LOCK:
        return _thread is not None and _thread.is_alive()


def stop(budget_s: float = 2.0) -> None:
    """Stop the timer (shutdown, flag off). A nightly run in progress is ended by `harness.shutdown`; its state
    says `running` and the next start closes it."""
    global _stop, _thread
    deadline = time.monotonic() + max(0.0, budget_s)
    with _LOCK:
        ev, th = _stop, _thread
        _stop, _thread = None, None
    if ev is not None:
        ev.set()
    if th is not None and th is not threading.current_thread():
        th.join(max(0.0, deadline - time.monotonic()))
