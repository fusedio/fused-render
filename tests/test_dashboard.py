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


@pytest.fixture(autouse=True)
def _stop_global_sampler():
    """The dashboard polls the process-wide sysmon sampler, whose daemon loop
    outlives the test and then shows up in the next file's thread counts
    (test_system_activity counts "sysmon-sampler" threads). Stop and drop it."""
    from fused_render.sysmon import sampler as smp
    yield
    s = smp._SAMPLER
    smp._SAMPLER = None
    if s is None:
        return
    if dashboard._tick in s.tick_hooks:
        s.tick_hooks.remove(dashboard._tick)
    s.idle_after = 0
    s._wake.set()
    if s._thread is not None:
        s._thread.join(timeout=5)


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
            return {"rps_10s": 4.0, "concurrency": [{"series": [0, 3]}, {"series": [5, 1]}]}
    monkeypatch.setattr(dashboard.netlog, "get", lambda: FakeLog())
    dashboard._loop_state.pool = {"borrowed": 5, "total": 40}
    dashboard._loop_state.pool_t = time.monotonic()
    pt = dashboard.timeline_point()
    assert pt["rps"] == 4.0 and pt["in_flight"] == 3 and pt["pool"] == 5
    _reset_loop_state()
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
    rows = [{"t": 1, "boot_id": "a"}, {"t": 2, "boot_id": "a"}, {"t": 200, "boot_id": "b"},
            {"t": 300, "boot_id": "c"}]
    assert dashboard.boot_changes(rows) == [{"t": 200, "boot_id": "b"}, {"t": 300, "boot_id": "c"}]


def test_interleaved_boot_ids_are_not_restarts():
    rows = []
    for i in range(30):
        rows.append({"t": i * 10, "boot_id": "abc"[i % 3]})
    rows += [{"t": 400, "boot_id": "d"}, {"t": 410, "boot_id": "a"}, {"t": 420, "boot_id": "d"},
             {"t": 430, "boot_id": "b"}]
    assert dashboard.boot_changes(rows) == [{"t": 400, "boot_id": "d"}]


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
    # platform-agnostic: pretend the tools exist wherever the OS keeps them
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(dashboard.os.path, "exists", lambda p: True)
    dashboard._run(["sysctl", "-n", "x"])
    dashboard._run(["pmset", "-g"])
    detail._lsof_ports(1)
    assert calls
    for argv, kw in calls:
        assert argv[0].startswith("/")
        assert kw["close_fds"] is False and "cwd" not in kw and kw["encoding"] == "utf-8"


def test_missing_exe_yields_none(monkeypatch):
    monkeypatch.setattr(dashboard.os.path, "exists", lambda p: False)
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: None)
    assert dashboard._run(["pmset", "-g"]) is None
    from fused_render.sysmon import detail
    assert detail._lsof_ports(1) == []
    monkeypatch.undo()
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")
    monkeypatch.setattr(dashboard, "_run", lambda argv: None)
    dashboard._machine_cache.clear()
    assert dashboard._pressure() is None
    assert dashboard._power() == {"thermal_limit": None, "low_power": None}
    dashboard._machine_cache.clear()


def test_empty_machine_read_is_unknown_and_retried(monkeypatch):
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")
    dashboard._machine_cache.clear()
    out = {"v": ""}
    monkeypatch.setattr(dashboard, "_run", lambda argv: out["v"])
    assert dashboard._pressure() is None            # int('') must not raise
    out["v"] = "2\n"
    assert dashboard._pressure() == "warn"          # the empty read was not cached
    dashboard._machine_cache.clear()


def _reset_loop_state():
    dashboard._loop_state.pool = dashboard._loop_state.pool_t = None
    dashboard._loop_state.lanes = dashboard._loop_state.lanes_t = None


def test_never_recorded_loop_state_is_unknown_not_an_error(monkeypatch):
    _reset_loop_state()
    # the off-loop fallback must be gone: neither read may be attempted
    from fused_render.server import serverstats
    monkeypatch.setattr(serverstats, "pool_stats",
                        lambda: (_ for _ in ()).throw(AssertionError("off-loop read")))
    server = dashboard._server()
    assert server["pool"] is None
    assert dashboard._index()["unavailable"] is True
    out = dashboard.build()
    assert "error" not in out["server"] and "error" not in out["index"]
    assert not any(r["key"] in ("threads", "index") for r in out["verdict"].get("reasons", []))


def test_stale_loop_state_does_not_fire_rules():
    _reset_loop_state()
    dashboard._loop_state.pool = {"borrowed": 40, "total": 40}
    dashboard._loop_state.lanes = {"waiting": 3, "abandoned": 0, "lanes": {}}
    dashboard._loop_state.pool_t = dashboard._loop_state.lanes_t = time.monotonic()
    fresh = dashboard.build()
    assert dashboard._fresh("pool") is not None
    ids = {r["key"] for r in fresh["verdict"].get("reasons", [])}
    assert {"threads", "index"} <= ids
    old = time.monotonic() - dashboard.LOOP_STATE_MAX_AGE_S - 1
    dashboard._loop_state.pool_t = dashboard._loop_state.lanes_t = old
    stale = dashboard.build()
    assert stale["server"]["pool"] is None
    ids = {r["key"] for r in stale["verdict"].get("reasons", [])}
    assert not ids & {"threads", "index"}
    _reset_loop_state()


def test_loop_ticker_records_pool_and_lanes():
    import asyncio
    from fused_render.server import serverstats
    _reset_loop_state()

    async def go():
        lag = serverstats.LoopLag(interval=0.01)
        lag.on_tick.append(dashboard.record_loop_state)
        lag.start()
        await asyncio.sleep(0.05)
        await lag.stop()
    asyncio.run(go())
    assert dashboard._fresh("pool") is not None and dashboard._fresh("lanes") is not None
    _reset_loop_state()


def test_build_polls_the_sampler_once(monkeypatch):
    n = {"c": 0}

    def payload():
        n["c"] += 1
        return {"host": {}, "procs": [], "claude": None}
    monkeypatch.setattr(dashboard, "_sampler_payload", payload)
    dashboard.build()
    assert n["c"] == 1


def test_polling_activity_does_not_set_watched(client):
    from fused_render import netlog
    netlog.get()._last_watch = netlog.get()._forced_until = float("-inf")
    assert not netlog.get().watched()
    assert client.get("/api/system/activity").status_code == 200
    assert not netlog.get().watched()


def _wait_refresh(timeout=5.0):
    """Block until the background refresh thread (if any) has finished."""
    import threading
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(t.name == "dashboard-verdict" and t.is_alive() for t in threading.enumerate()):
            return
        time.sleep(0.005)
    raise AssertionError("verdict refresh did not finish")


@pytest.fixture()
def compact_state(monkeypatch):
    # An earlier test's request (the activity endpoint) may have left a real
    # refresh running in this process: let it land before taking the state.
    _wait_refresh()
    monkeypatch.setattr(dashboard, "_compact", None)
    monkeypatch.setattr(dashboard, "_compact_refreshing", False)
    yield
    _wait_refresh()


def test_compact_verdict_first_call_is_unknown_and_never_builds_in_the_request(monkeypatch, compact_state):
    import threading
    gate = threading.Event()
    seen = {"thread": None, "n": 0}

    def slow_build():
        seen["n"] += 1
        seen["thread"] = threading.current_thread()
        gate.wait(5)  # a request that waited on this would hang the test
        return {"verdict": {"level": "warn", "headline": "slow"}}
    monkeypatch.setattr(dashboard, "build_for_verdict", slow_build)
    monkeypatch.setattr(dashboard, "build", lambda: pytest.fail("compact_verdict must not call build()"))
    t0 = time.monotonic()
    assert dashboard.compact_verdict() == {"level": "unknown", "headline": ""}
    assert time.monotonic() - t0 < 1.0           # returned while the build is still blocked
    gate.set()
    _wait_refresh()
    assert seen["thread"] is not threading.main_thread() and seen["thread"].daemon
    assert dashboard.compact_verdict()["headline"] == "slow"


def test_compact_verdict_concurrent_callers_start_one_refresh(monkeypatch, compact_state):
    import threading
    gate = threading.Event()
    n = {"c": 0}

    def fake():
        n["c"] += 1
        gate.wait(5)
        return {"verdict": {"level": "ok", "headline": ""}}
    monkeypatch.setattr(dashboard, "build_for_verdict", fake)
    results = []
    ts = [threading.Thread(target=lambda: results.append(dashboard.compact_verdict())) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]                        # every caller returned with the build still blocked
    assert len(results) == 8 and all(r["level"] == "unknown" for r in results)
    gate.set()
    _wait_refresh()
    assert n["c"] == 1


def test_compact_verdict_stale_cache_serves_old_and_refreshes_once(monkeypatch, compact_state):
    n = {"c": 0}

    def fake():
        n["c"] += 1
        return {"verdict": {"level": "bad", "headline": f"v{n['c']}"}}
    monkeypatch.setattr(dashboard, "build_for_verdict", fake)
    dashboard.compact_verdict()
    _wait_refresh()
    assert n["c"] == 1
    assert dashboard.compact_verdict()["headline"] == "v1"
    assert n["c"] == 1                            # inside the 10 s TTL: no refresh
    dashboard._compact = (time.monotonic() - 11, dashboard._compact[1])
    assert dashboard.compact_verdict()["headline"] == "v1"   # stale value served at once
    _wait_refresh()
    assert n["c"] == 2
    assert dashboard.compact_verdict()["headline"] == "v2"


def test_compact_verdict_failed_refresh_keeps_the_last_verdict_and_retries(monkeypatch, compact_state):
    calls = {"c": 0}

    def flaky():
        calls["c"] += 1
        if calls["c"] == 2:
            raise RuntimeError("boom")
        return {"verdict": {"level": "warn", "headline": "w"}}
    monkeypatch.setattr(dashboard, "build_for_verdict", flaky)
    dashboard.compact_verdict()
    _wait_refresh()
    dashboard._compact = (time.monotonic() - 11, dashboard._compact[1])
    assert dashboard.compact_verdict()["headline"] == "w"
    _wait_refresh()                               # this refresh raised
    assert dashboard._compact_refreshing is False
    assert calls["c"] == 2
    assert dashboard.compact_verdict()["headline"] == "w"   # still stale: the next poll retries
    _wait_refresh()
    assert calls["c"] == 3


def test_build_for_verdict_reads_only_the_sections_the_rules_use(monkeypatch):
    called = []

    def stub(name):
        return lambda *_: called.append(name) or {}
    monkeypatch.setattr(dashboard, "_VERDICT_READERS", {k: stub(k) for k in dashboard._VERDICT_READERS})
    out = dashboard.build_for_verdict()
    assert set(called) == {"net", "server", "memory", "cpu", "claude", "index", "disk", "errors"}
    assert not {"jobs", "calls", "ai", "installs", "baseline", "timeline"} & set(out)
    assert "verdict" in out


def test_verdict_readers_cover_every_section_the_rules_read():
    """verdict.py reads d.get(<section>) for exactly these names."""
    import re
    src = open(dashboard.sysmon_verdict.__file__, encoding="utf-8").read()
    read = set(re.findall(r'd\.get\("(\w+)"\)', src))
    assert read <= set(dashboard._VERDICT_READERS)


def test_light_verdict_readers_skip_the_expensive_work(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "server_log_errors", lambda *_: pytest.fail("log tail"))
    monkeypatch.setattr(dashboard, "_crashes", lambda: pytest.fail("crash listing"))
    monkeypatch.setattr(dashboard, "_disk_sizes", lambda: pytest.fail("dir walk"))
    monkeypatch.setattr(health, "read_outages", lambda n: [{"kind": "outage", "t_down": time.time() - 5}])
    monkeypatch.setattr("fused_render.logs.log_dir", lambda: str(tmp_path))
    e = dashboard._errors_for_verdict()
    assert 0 <= e["last_unexpected_outage_age_s"] < 60
    assert dashboard._disk_for_verdict()["free_bytes"] > 0


def test_activity_verdict_is_unknown_when_the_dashboard_raises(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(dashboard, "compact_verdict", boom)
    assert client.get("/api/system/activity").json()["verdict"] == {"level": "unknown", "headline": ""}


def test_trail_reader_spans_more_than_one_megabyte(tmp_path, monkeypatch):
    """A day of rows is ~1.2 MB: the 24 h read must reach past the old 1 MB tail."""
    p = tmp_path / "resources.jsonl"
    now = time.time()
    n = 4300  # 20 s apart: just under 24 h
    pad = "x" * 230
    rows = [{"t": now - (n - i) * 20.0, "i": i, "pad": pad} for i in range(n)]
    _write(p, rows)
    assert p.stat().st_size > 1_100_000
    monkeypatch.setattr(health, "resources_path", lambda: str(p))
    health._trail_cache.clear()
    got = health.read_trail(86400)
    assert got[0]["i"] == 0 and got[-1]["i"] == n - 1 and len(got) == n
    health._trail_cache.clear()
    short = health.read_trail(600)           # a short window stops early
    assert 25 <= len(short) <= 31 and short[-1]["i"] == n - 1
    # a window older than the whole file returns every row, no error at the start
    health._trail_cache.clear()
    assert len(health.read_trail(10 * 86400)) == n


def _outage_row(t_down, t_up, before="a", after="b"):
    return {"kind": "outage", "t_down": t_down, "t_up": t_up,
            "boot_id_before": before, "boot_id_after": after, "recovered": True}


def test_classify_outages_drops_quit_rows_and_marks_planned_restarts():
    now = time.time()
    rows = [_outage_row(now - 500, now - 480),
            {"kind": "quit", "source": "server", "received_at": now - 495},
            _outage_row(now - 100, now - 60, "c", "c")]
    out = dashboard.classify_outages(rows, now)
    assert [o["t_down"] for o in out] == [now - 100, now - 500]
    hung, planned = out
    assert hung["expected"] is False and hung["restarted"] is False and hung["duration_s"] == 40
    assert planned["clean"] is True and planned["expected"] is True and planned["restarted"] is True
    assert dashboard._last_unexpected_age(out) == pytest.approx(100, abs=2)


def test_restart_without_quit_row_is_unexpected_and_restarts_alone_do_not_count():
    now = time.time()
    out = dashboard.classify_outages([_outage_row(now - 50, now - 20)], now)
    assert out[0]["restarted"] and not out[0]["expected"]
    assert dashboard.classify_outages([{"kind": "quit", "received_at": now}], now) == []
    assert dashboard._last_unexpected_age([]) is None


def test_crashes_skip_empty_and_old_dumps(monkeypatch, tmp_path):
    import os
    from fused_render import crashlog
    monkeypatch.setattr(crashlog, "crash_dir", lambda: str(tmp_path))
    (tmp_path / "empty.log").write_text("")
    (tmp_path / "dump.log").write_text("Fatal Python error")
    old = tmp_path / "old.log"
    old.write_text("x")
    t = time.time() - 8 * 86400
    os.utime(old, (t, t))
    out = dashboard._crashes()
    assert [c["name"] for c in out] == ["dump.log"]
    assert out[0]["path"] == str(tmp_path / "dump.log")
    assert dashboard._recent_crash_name() == "dump.log"
