"""Server-internal pressure gauges for the Monitor's Overview: the request
thread pool, event-loop lag, and the engine workers.

These answer "is the SERVER itself the slow part?", which request timings alone
cannot: a saturated 40-thread pool or a blocked event loop delays every request
before any handler runs, so each handler still looks fast.

`pool_stats` must run on the event-loop thread (anyio's limiter is per loop), so
the endpoints that call it are `async def`.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time

logger = logging.getLogger(__name__)

LAG_INTERVAL_S = 0.5
LAG_RETAIN_S = 600.0


def pool_stats() -> dict:
    """The default worker-thread limiter every sync route and
    `anyio.to_thread` call shares: tokens in use vs total."""
    import anyio.to_thread

    lim = anyio.to_thread.current_default_thread_limiter()
    return {"borrowed": int(lim.borrowed_tokens), "total": int(lim.total_tokens)}


class LoopLag:
    """How late the event loop wakes a 0.5 s sleeper — the lag every other
    coroutine sees too. A 10-minute ring of (wall time, lag ms)."""

    def __init__(self, interval: float = LAG_INTERVAL_S, clock=time.monotonic,
                 wall=time.time):
        self.interval = interval
        self._clock = clock
        self._wall = wall
        self._ring: collections.deque = collections.deque(
            maxlen=int(LAG_RETAIN_S / interval) + 8)
        self._task: asyncio.Task | None = None
        # Called on the loop thread after every tick (dashboard.record_loop_state):
        # the reads that only work on the event loop piggyback on this task.
        self.on_tick: list = []

    def record(self, lag_ms: float) -> None:
        self._ring.append((self._wall(), max(0.0, lag_ms)))

    async def _run(self) -> None:
        while True:
            t0 = self._clock()
            await asyncio.sleep(self.interval)
            self.record((self._clock() - t0 - self.interval) * 1000)
            for hook in list(self.on_tick):
                try:
                    hook()
                except Exception:  # noqa: BLE001 — a gauge must never kill the ticker
                    logger.debug("loop tick hook failed", exc_info=True)

    def start(self) -> None:
        """Start the sampler on the running loop. A bare task: it holds no
        thread and no socket, so it cannot keep the process alive, and `stop`
        (shutdown handler) cancels it."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def current(self) -> float | None:
        return round(self._ring[-1][1], 1) if self._ring else None

    def max_over(self, seconds: float = 60.0) -> float | None:
        cutoff = self._wall() - seconds
        vals = [lag for t, lag in list(self._ring) if t >= cutoff]
        return round(max(vals), 1) if vals else None

    def series(self, seconds: float = LAG_RETAIN_S) -> list[list[float]]:
        cutoff = self._wall() - seconds
        return [[round(t, 1), round(lag, 1)] for t, lag in list(self._ring) if t >= cutoff]


_lag = LoopLag()


def loop_lag() -> LoopLag:
    return _lag


def engine_workers() -> list[dict]:
    """The engine workers (`engine_host._children`, under its lock like
    `sysmon/labels._engines`). A child carries no busy flag, only `last_used`
    (stamped by every call routed to it), so the report is alive/idle-for
    rather than busy/idle — no new protocol."""
    from fused_render.server import engine_host

    with engine_host._lock:
        children = list(engine_host._children.values())
    now = time.monotonic()
    out = []
    for c in children:
        proc = getattr(c, "proc", None)
        alive = proc is not None and proc.poll() is None
        engine_id = str(getattr(c, "engine_id", ""))
        folder = str(getattr(c, "folder", "") or "")
        out.append({
            "engine_id": engine_id,
            "label": folder.rstrip("/").rsplit("/", 1)[-1] if folder else engine_id,
            "kind": "app" if engine_id.startswith("bg_") else "engine",
            "pid": int(getattr(c, "pid", 0) or 0),
            "alive": alive,
            "idle_s": round(now - float(getattr(c, "last_used", now)), 1),
            "up_s": round(now - float(getattr(c, "started_at", now)), 1),
        })
    return out
