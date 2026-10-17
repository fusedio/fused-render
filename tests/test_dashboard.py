"""health.py's resource-trail reader and GET /api/system/dashboard: sections are
isolated, the verdict rides along, and the activity payload carries a compact
verdict."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import health
from fused_render.server import create_app, dashboard


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_trail_reader_returns_recent_rows_and_caches(tmp_path, monkeypatch):
    p = tmp_path / "resources.jsonl"
    now = time.time()
    _write(p, [{"t": now - 7200, "load": 1.0}, {"t": now - 60, "load": 2.0}, {"t": now - 5, "load": 3.0}])
    monkeypatch.setattr(health, "resources_path", lambda: str(p))
    health._trail_cache.clear()
    rows = health.read_trail(3600)
    assert [r["load"] for r in rows] == [2.0, 3.0]
    _write(p, [{"t": now, "load": 9.0}])
    assert [r["load"] for r in health.read_trail(3600)] == [2.0, 3.0]   # cached 30 s
    health._trail_cache.clear()
    assert [r["load"] for r in health.read_trail(3600)] == [9.0]


def test_trail_reader_skips_garbage_and_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "resources_path", lambda: str(tmp_path / "none.jsonl"))
    health._trail_cache.clear()
    assert health.read_trail(60) == []
    p = tmp_path / "r.jsonl"
    p.write_text('not json\n{"t": %f, "load": 1}\n' % time.time())
    monkeypatch.setattr(health, "resources_path", lambda: str(p))
    health._trail_cache.clear()
    assert len(health.read_trail(60)) == 1


def test_swap_growth_over_window():
    now = 1000.0
    rows = [{"t": now - 700, "swap_used": 100}, {"t": now - 500, "swap_used": 200},
            {"t": now - 5, "swap_used": 900}]
    assert dashboard.swap_growth(rows, now, 600) == 700
    assert dashboard.swap_growth([], now, 600) is None
    assert dashboard.swap_growth([{"t": now, "swap_used": None}], now, 600) is None


def test_sections_are_isolated(monkeypatch):
    def boom():
        raise RuntimeError("nope")
    monkeypatch.setitem(dashboard.SECTIONS, "disk", boom)
    out = dashboard.build()
    assert out["disk"] == {"error": "RuntimeError: nope"}
    for key in ("net", "server", "memory", "cpu", "claude", "index", "jobs", "errors"):
        assert key in out
    assert out["verdict"]["level"] in ("ok", "warn", "bad")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_dashboard_endpoint_and_watch(client):
    from fused_render import netlog
    r = client.get("/api/system/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert {"net", "server", "verdict", "claude", "index"} <= set(body)
    assert netlog.get().watched()


def test_activity_payload_carries_compact_verdict(client):
    body = client.get("/api/system/activity").json()
    v = body["verdict"]
    assert set(v) == {"level", "headline"}


# ---- off the event loop -----------------------------------------------------

def test_build_async_keeps_blocking_work_off_the_loop(monkeypatch):
    """The async handler must not run a subprocess (sysctl/pmset), a file read
    or a dir walk on the loop thread: the page that diagnoses slowness cannot
    itself stall every request. Only the thread-limiter read stays on the loop."""
    import asyncio
    import subprocess
    import threading

    seen = {"subprocess": [], "pool": []}
    real_run = subprocess.run

    def spy_run(*a, **k):
        seen["subprocess"].append(threading.current_thread())
        return real_run(["true"], capture_output=True, text=True)
    monkeypatch.setattr(dashboard.subprocess, "run", spy_run)
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")
    dashboard._machine_cache.clear()

    from fused_render.server import serverstats
    real_pool = serverstats.pool_stats

    def spy_pool():
        seen["pool"].append(threading.current_thread())
        return real_pool()
    monkeypatch.setattr(serverstats, "pool_stats", spy_pool)

    async def go():
        loop_thread = threading.current_thread()
        out = await dashboard.build_async()
        return loop_thread, out

    loop_thread, out = asyncio.run(go())
    assert seen["subprocess"], "the spy never ran: the test proves nothing"
    assert all(t is not loop_thread for t in seen["subprocess"])
    assert seen["pool"] and all(t is loop_thread for t in seen["pool"])
    assert out["server"]["pool"]["total"] > 0


def test_system_handlers_offload_blocking_calls():
    import inspect
    from fused_render.server.routers import system
    for fn in (system.api_system_dashboard, system.api_system_activity_detail,
               system.api_system_record_start, system.api_system_record_status):
        assert inspect.iscoroutinefunction(fn)
        assert "to_thread" in inspect.getsource(fn) or "build_async" in inspect.getsource(fn)


# ---- the remaining sections -------------------------------------------------

def test_calls_overhead_rollup():
    recs = [{"entrypoint": "a.py", "server_ms": 100, "run_ms": 40, "outcome": "ok"},
            {"entrypoint": "a.py", "server_ms": 300, "run_ms": 100, "outcome": "ok"},
            {"entrypoint": "b.py", "server_ms": 50, "run_ms": 45, "outcome": "ok"},
            {"entrypoint": "b.py", "server_ms": 9000, "run_ms": 1, "outcome": "superseded"},
            {"entrypoint": "c.py", "server_ms": 10, "outcome": "ok"}]   # no run_ms: not countable
    out = dashboard.calls_rollup(recs)
    by = {t["entrypoint"]: t for t in out["targets"]}
    assert set(by) == {"a.py", "b.py"}
    assert by["a.py"]["count"] == 2
    assert by["a.py"]["overhead_p95_ms"] == 200 and by["a.py"]["run_p95_ms"] == 100
    assert by["b.py"]["overhead_p50_ms"] == 5
    assert [t["entrypoint"] for t in out["top_overhead"]] == ["a.py", "b.py"]


def test_baseline_medians():
    rows = [{"t": 1, "load": 1.0, "swap_used": 100, "rss": 10},
            {"t": 2, "load": 3.0, "swap_used": 300, "rss": 30},
            {"t": 3, "load": 2.0, "swap_used": None, "rss": 20}]
    assert dashboard.baseline_from_rows(rows) == {"load": 2.0, "swap_used": 200, "rss": 20,
                                                  "samples": 3}
    assert dashboard.baseline_from_rows([]) == {"load": None, "swap_used": None, "rss": None,
                                                "samples": 0}


def test_baseline_is_cached_five_minutes(monkeypatch):
    calls = []
    monkeypatch.setattr(health, "read_trail",
                        lambda w: calls.append(w) or [{"t": 1, "load": 4.0}])
    dashboard._slow_cache.clear()
    assert dashboard._baseline()["load"] == 4.0
    dashboard._baseline()
    assert calls == [86400]


def test_server_log_errors_tails_only_error_lines(tmp_path):
    p = tmp_path / "fused-render-1.log"
    lines = [f"2026-01-01 00:00:{i:02d},000 ERROR [1 T] x: boom {i}\n" for i in range(30)]
    p.write_text("".join(["2026 INFO [1 T] x: fine\n"] + lines + ["2026 WARNING [1 T] x: hm\n"]))
    out = dashboard.server_log_errors(str(p), limit=20)
    assert len(out) == 20 and out[-1].endswith("boom 29") and out[0].endswith("boom 10")
    assert dashboard.server_log_errors(str(tmp_path / "none.log")) == []


def test_server_log_errors_reads_only_the_tail(tmp_path):
    p = tmp_path / "big.log"
    p.write_text("2026 ERROR [1 T] x: ancient\n" + ("2026 INFO [1 T] x: pad\n" * 40000)
                 + "2026 ERROR [1 T] x: recent\n")
    assert dashboard.server_log_errors(str(p)) == ["2026 ERROR [1 T] x: recent"]


def test_dir_size_and_cached_disk_sizes(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"x" * 100)
    (tmp_path / "g").write_bytes(b"y" * 50)
    assert dashboard.dir_size(str(tmp_path)) == 150
    assert dashboard.dir_size(str(tmp_path / "missing")) == 0
    walks = []
    monkeypatch.setattr(dashboard, "dir_size", lambda p: walks.append(p) or 7)
    dashboard._slow_cache.clear()
    first = dashboard._disk_sizes()
    dashboard._disk_sizes()
    assert set(first) == {"log_home", "call_store", "index_db"}
    assert len(walks) == 3          # the second call came from the cache


def test_timeline_ring_keeps_ten_minutes():
    tl = dashboard.Timeline(retain_s=600)
    tl.add({"t": 1000.0, "rps": 1})
    tl.add({"t": 1500.0, "rps": 2})
    tl.add({"t": 1700.0, "rps": 3})
    assert [p["rps"] for p in tl.points()] == [2, 3]
    assert tl.points(since=1600.0)[0]["rps"] == 3


def test_timeline_point_reads_the_sections_it_can(monkeypatch):
    class FakeLog:
        def summary(self):
            return {"rps_10s": 4.0, "in_flight": [{"in_flight": 3}, {"in_flight": 1}]}
    monkeypatch.setattr(dashboard.netlog, "get", lambda: FakeLog())
    dashboard._loop_state.pool = {"borrowed": 5, "total": 40}
    pt = dashboard.timeline_point()
    assert pt["rps"] == 4.0 and pt["in_flight"] == 3 and pt["pool"] == 5
    assert "t" in pt and "lag_ms" in pt and "cpu" in pt and "swap" in pt


def test_sampler_tick_feeds_the_timeline():
    from fused_render.sysmon import sampler as s
    smp = s.Sampler(backend=None, threaded=False)
    fired = []
    smp.tick_hooks.append(lambda: fired.append(1))
    smp._last_poll = smp._clock()
    smp.tick()
    assert fired == [1]


def test_new_sections_registered_and_isolated():
    for key in ("calls", "ai", "installs", "baseline", "timeline"):
        assert key in dashboard.SECTIONS
    out = dashboard.build()
    for key in ("calls", "ai", "installs", "baseline", "timeline"):
        assert key in out
    assert "server_log_errors" in out["errors"] and "crashes" in out["errors"]
    assert {"log_home", "call_store", "index_db"} <= set(out["disk"])
    assert "scans" in out["index"]


def test_restarts_counted_from_boot_id_changes():
    rows = [{"t": 1, "boot_id": "a"}, {"t": 2, "boot_id": "a"}, {"t": 3, "boot_id": "b"},
            {"t": 4, "boot_id": "c"}]
    assert dashboard.boot_changes(rows) == [{"t": 3, "boot_id": "b"}, {"t": 4, "boot_id": "c"}]


def test_spawns_use_posix_spawn_shape(monkeypatch):
    """sysctl/pmset/lsof: absolute exe, close_fds=False, no cwd, utf-8 (a fork
    with libproj resident SIGSEGVs; see app_git.py)."""
    import subprocess
    import types
    from fused_render.sysmon import detail
    calls = []

    def spy(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(stdout="")
    monkeypatch.setattr(subprocess, "run", spy)
    dashboard._run(["sysctl", "-n", "x"])
    dashboard._run(["pmset", "-g"])
    detail._lsof_ports(1)
    assert calls
    for argv, kw in calls:
        assert argv[0].startswith("/")
        assert kw["close_fds"] is False and "cwd" not in kw and kw["encoding"] == "utf-8"


def test_empty_machine_read_is_unknown_and_retried(monkeypatch):
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")
    dashboard._machine_cache.clear()
    out = {"v": ""}
    monkeypatch.setattr(dashboard, "_run", lambda argv: out["v"])
    assert dashboard._pressure() is None            # int('') must not raise
    out["v"] = "2\n"
    assert dashboard._pressure() == "warn"          # the empty read was not cached
    dashboard._machine_cache.clear()
