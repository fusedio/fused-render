"""serverstats.py (thread pool, loop lag, engine workers) and the index lane
stats."""
import asyncio

import pytest

from fused_render.server import serverstats


def test_pool_stats_reads_the_running_loops_limiter():
    async def go():
        import anyio.to_thread
        before = serverstats.pool_stats()
        assert before["total"] >= 1 and before["borrowed"] == 0
        gate = asyncio.Event()
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def hold():
            loop.call_soon_threadsafe(started.set)
            import time
            time.sleep(0.2)

        t = asyncio.create_task(anyio.to_thread.run_sync(hold))
        await started.wait()
        assert serverstats.pool_stats()["borrowed"] == 1
        await t
        assert gate is not None
    asyncio.run(go())


def test_loop_lag_measures_a_blocked_loop_and_stops_cleanly():
    async def go():
        lag = serverstats.LoopLag(interval=0.05)
        lag.start()
        await asyncio.sleep(0.12)
        import time
        time.sleep(0.25)          # block the loop
        await asyncio.sleep(0.12)
        assert lag.max_over(60) >= 150
        assert lag.current() is not None
        assert lag.series()
        await lag.stop()
        assert lag._task is None
        await lag.stop()          # idempotent
    asyncio.run(go())


def test_loop_lag_ring_is_bounded_and_time_windowed():
    now = [1000.0]
    lag = serverstats.LoopLag(interval=0.5, wall=lambda: now[0])
    assert lag.current() is None and lag.max_over() is None
    lag.record(10)
    now[0] += 100
    lag.record(5)
    assert lag.max_over(60) == 5.0 and lag.max_over(600) == 10.0
    for _ in range(5000):
        lag.record(1)
    assert len(lag._ring) <= 1300


def test_loop_lag_does_not_survive_its_loop():
    # the task is owned by the loop that ran it: closing the loop must not hang
    lag = serverstats.LoopLag(interval=0.05)

    async def go():
        lag.start()
        await asyncio.sleep(0.06)
    asyncio.run(go())


def test_engine_workers_reports_alive_and_idle(monkeypatch):
    from fused_render.server import engine_host

    class P:
        def poll(self):
            return None

    class C:
        engine_id = "bg_x"
        folder = "/apps/foo"
        pid = 4242
        last_used = 0.0
        started_at = 0.0
        proc = P()

    monkeypatch.setattr(engine_host, "_children", {"bg_x": C()})
    rows = serverstats.engine_workers()
    assert rows[0]["label"] == "foo" and rows[0]["kind"] == "app"
    assert rows[0]["alive"] is True and rows[0]["pid"] == 4242 and rows[0]["idle_s"] > 0


def test_index_lane_stats_counts_permits_and_waiters():
    from fused_render.server.routers import index as idx

    async def go():
        assert idx.lane_stats()["in_use"] == 0
        sem = idx._interactive_read_concurrency()
        await sem.acquire()
        await sem.acquire()
        waiter = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)
        st = idx.lane_stats()
        assert st["lanes"]["interactive"] == {"width": 2, "in_use": 2, "waiting": 1}
        assert st["waiting"] == 1 and st["abandoned"] == 0
        waiter.cancel()
        sem.release()
        sem.release()
    asyncio.run(go())


def test_index_lane_stats_outside_a_loop_is_idle():
    from fused_render.server.routers import index as idx
    st = idx.lane_stats()
    assert st["in_use"] == 0 and st["waiting"] == 0
    assert "last_503_at" in st


def test_app_lifespan_starts_and_cancels_the_lag_task(tmp_path):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app

    with TestClient(create_app(start_dir=str(tmp_path))):
        task = serverstats.loop_lag()._task
        assert task is not None and not task.done()
    assert serverstats.loop_lag()._task is None
