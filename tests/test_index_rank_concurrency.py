"""A background scan must not starve the interactive home search.

The rank read path is lock-free, so the only thing a concurrent scan can take
from it is CPU: up to ten detached worker processes, each with a 16-thread
stat pool, plus a DuckDB compaction that defaults to every core. The scan
therefore yields — it nices itself and caps the compaction's threads — and
`/api/index/rank` keeps answering in tens of milliseconds.
"""
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from fused_render.index import store, worker
from fused_render.server import create_app

from index_perf_harness import (build_tree as _tree,
                                 prime_index as _prime_index,
                                 sample_rank_latencies)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


# -- the two knobs, unit-tested ----------------------------------------------

def test_the_worker_nices_itself_at_startup(monkeypatch, tmp_path):
    """Self-nicing in the child, never a preexec_fn: this repo's spawns must
    stay on posix_spawn (PROJ's atfork handler SIGSEGVs a fork)."""
    seen = []
    # `raising=False`: `os.nice` does not exist on Windows at all, so
    # asserting it existed first (monkeypatch's default) would fail before
    # the fake is ever installed — this line means "give the module this
    # attribute for the test", not "override an attribute already there".
    monkeypatch.setattr(os, "nice", lambda inc: seen.append(inc) or 0,
                        raising=False)
    ran = []
    monkeypatch.setattr(worker, "run_scan", ran.append)
    assert worker.main([str(tmp_path)]) == 0
    assert seen == [worker.SCAN_NICE_INCREMENT]
    assert ran == [str(tmp_path)]


def test_a_worker_on_a_platform_without_nice_still_scans(monkeypatch, tmp_path):
    monkeypatch.delattr(os, "nice", raising=False)
    ran = []
    monkeypatch.setattr(worker, "run_scan", ran.append)
    assert worker.main([str(tmp_path)]) == 0
    assert ran == [str(tmp_path)]


@pytest.mark.skipif(os.name == "nt", reason="no nice on Windows")
def test_renicing_really_lowers_a_real_process_priority():
    """The monkeypatched test above proves `main` calls it; this one proves the
    call does something, in a process that is not this one (nicing is one-way,
    so pytest must not do it to itself)."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import os;from fused_render.index.worker import _renice_self;"
         "_renice_self();print(os.getpriority(os.PRIO_PROCESS, 0))"],
        capture_output=True, text=True, check=True)
    assert int(out.stdout.strip()) >= worker.SCAN_NICE_INCREMENT


def test_the_worker_sets_a_background_io_policy_at_startup(monkeypatch, tmp_path):
    """Nicing only yields CPU; the scan must also yield the disk queue, or an
    interactive rank's read_parquet still queues behind the scan's stat pool
    and compaction I/O."""
    seen = []
    monkeypatch.setattr(worker, "_set_background_io_policy",
                        lambda: seen.append(True) or True)
    ran = []
    monkeypatch.setattr(worker, "run_scan", ran.append)
    assert worker.main([str(tmp_path)]) == 0
    assert seen == [True]
    assert ran == [str(tmp_path)]


def test_set_background_io_policy_invokes_setiopolicy_np_on_darwin(monkeypatch):
    """The ctypes call must ask for IOPOL_TYPE_DISK / IOPOL_SCOPE_PROCESS /
    IOPOL_THROTTLE — the constants from <sys/resource.h>."""
    calls = []

    class FakeLib:
        def setiopolicy_np(self, iotype, scope, policy):
            calls.append((iotype, scope, policy))
            return 0

    monkeypatch.setattr(worker.sys, "platform", "darwin")
    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *a, **k: FakeLib())
    assert worker._set_background_io_policy() is True
    assert calls == [(worker.IOPOL_TYPE_DISK, worker.IOPOL_SCOPE_PROCESS,
                       worker.IOPOL_THROTTLE)]


@pytest.mark.parametrize("machine,nr", [("x86_64", 251), ("aarch64", 30)])
def test_set_background_io_policy_invokes_ioprio_set_on_linux(monkeypatch,
                                                                machine, nr):
    """`ioprio_set` has no libc wrapper, so this goes through the raw
    syscall table — the number is arch-specific."""
    calls = []

    class FakeLib:
        def syscall(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.platform, "machine", lambda: machine)
    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *a, **k: FakeLib())
    assert worker._set_background_io_policy() is True
    expected_prio = (worker.IOPRIO_CLASS_IDLE << worker.IOPRIO_CLASS_SHIFT) | 0
    assert calls == [(nr, worker.IOPRIO_WHO_PROCESS, 0, expected_prio)]


def test_set_background_io_policy_skips_unknown_linux_arch(monkeypatch):
    """An arch this repo hasn't mapped a syscall number for is a silent
    no-op, not a guess at the wrong number."""
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.platform, "machine", lambda: "riscv64")
    # If this reached ctypes at all the test should fail loudly, not by
    # coincidence, so make CDLL blow up.
    monkeypatch.setattr(worker.ctypes, "CDLL",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert worker._set_background_io_policy() is False


def test_set_background_io_policy_invokes_set_priority_class_on_win32(
        monkeypatch):
    """Windows has no `os.nice` at all, so this is its first scan
    mitigation, not just its I/O half — PROCESS_MODE_BACKGROUND_BEGIN."""
    calls = []

    class FakeKernel32:
        def GetCurrentProcess(self):
            return 1234

        def SetPriorityClass(self, handle, flag):
            calls.append((handle, flag))
            return 1  # nonzero == success, per SetPriorityClass's contract

    class FakeWindll:
        kernel32 = FakeKernel32()

    monkeypatch.setattr(worker.sys, "platform", "win32")
    monkeypatch.setattr(worker.ctypes, "windll", FakeWindll(), raising=False)
    assert worker._set_background_io_policy() is True
    assert calls == [(1234, worker.PROCESS_MODE_BACKGROUND_BEGIN)]


def test_an_unmatched_platform_is_a_silent_no_op(monkeypatch):
    monkeypatch.setattr(worker.sys, "platform", "some-future-os")
    assert worker._set_background_io_policy() is False


def test_a_worker_where_io_policy_fails_still_scans(monkeypatch, tmp_path):
    """Best-effort on every platform: a missing symbol or a raise must
    never take the scan down with it."""
    class FakeLib:
        def setiopolicy_np(self, *a, **k):
            raise AttributeError("no such symbol")

    monkeypatch.setattr(worker.sys, "platform", "darwin")
    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *a, **k: FakeLib())
    assert worker._set_background_io_policy() is False
    ran = []
    monkeypatch.setattr(worker, "run_scan", ran.append)
    assert worker.main([str(tmp_path)]) == 0
    assert ran == [str(tmp_path)]


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="setiopolicy_np is a macOS-only syscall wrapper")
def test_the_io_policy_really_lands_on_a_real_process():
    """The monkeypatched tests above prove the helper is wired in; this one
    proves the syscall actually sticks, in a subprocess (policy changes are
    one-way, so pytest must not do this to itself)."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import ctypes;"
         "from fused_render.index.worker import _set_background_io_policy;"
         "_set_background_io_policy();"
         "lib = ctypes.CDLL(None, use_errno=True);"
         "print(lib.getiopolicy_np(0, 0))"],
        capture_output=True, text=True, check=True)
    assert int(out.stdout.strip()) == 3  # IOPOL_THROTTLE


def test_the_compaction_connection_caps_its_threads():
    con = store.background_connect()
    got = int(con.execute("SELECT current_setting('threads')").fetchone()[0])
    assert got == store.compaction_threads()
    assert 1 <= got <= store.MAX_COMPACTION_THREADS


def test_search_threads_is_half_the_machine_capped():
    """The interactive counterpart to `compaction_threads` — see its
    docstring in store.py. Half rather than a quarter (a user IS waiting on
    these), still capped so a burst of typing cannot saturate the box."""
    got = store.search_threads()
    assert 1 <= got <= store.MAX_SEARCH_THREADS
    cpu = os.cpu_count() or 4
    assert got == max(1, min(store.MAX_SEARCH_THREADS, cpu // 2))
    # The whole point: half the machine is allowed at least as much as a
    # quarter, on any core count, so the interactive path is never MORE
    # throttled than the background one.
    assert got >= store.compaction_threads()


@pytest.mark.parametrize("cpu_count", [16, 32, 64])
def test_search_threads_strictly_exceeds_compaction_threads_on_a_big_machine(
        monkeypatch, cpu_count):
    """`got >= compaction_threads()` above passes even when the two caps are
    EQUAL — exactly what happened when both `MAX_SEARCH_THREADS` and
    `MAX_COMPACTION_THREADS` were 4: on any machine with >=16 cores both
    `// 4` and `// 2` clamp to their shared ceiling and the "half, not a
    quarter" rationale (D701) went inert. Pin strict inequality on machines
    with enough cores that the two ceilings would otherwise collide, with
    `os.cpu_count()` monkeypatched so this does not depend on the host."""
    monkeypatch.setattr(os, "cpu_count", lambda: cpu_count)
    assert store.search_threads() > store.compaction_threads()


def test_query_py_and_guarded_query_read_connections_cap_their_threads(home, tmp_path, monkeypatch):
    """`stats`, `search_under`, `search_ranked` (query.py) and guarded_query's
    `_connect` each open a bare `duckdb.connect()` — this pins that all four
    apply `search_threads()` rather than defaulting to one thread per core.

    NOT exhaustive over every bare `duckdb.connect()` on the interactive read
    path (D701 correction / D706) — this repo also has POST /api/search/files
    (routers/search.py, `_index_entries`) and GET /api/git-repos
    (routers/git_repos.py, `_repos`), which are covered by their own tests
    nearer their code (test_search_index.py / test_git_repos_api.py) rather
    than duplicated here, since exercising them needs a running app and a
    real index rather than a bare `IndexConfig`. `index/freshness.py`'s
    `indexed_mtime_ns` is deliberately UNCAPPED and excluded from that claim
    entirely — it is a single point lookup (`WHERE dir = '...' LIMIT 1`) on a
    heavily-debounced background housekeeping path (at most once per root
    every ~60s, see routers/index.py's FRESHNESS_CHECK_S comment), not a
    per-request interactive query, and DuckDB's thread count buys it nothing
    a single-row lookup can use."""
    from fused_render.index import guarded_query
    from fused_render.index.query import search_ranked, search_under, stats

    root = str(home / "r")
    cfg = _prime_index(tmp_path, root, n=100)

    # Every caller here closes its connection in a `finally` before this test
    # ever gets to inspect it, so the setting is read the moment the
    # connection is opened — immediately after `duckdb.connect()`, which is
    # also the ONLY point `guarded_query._connect` allows it (its `SET
    # threads` has to run before the lockdown, same as production code).
    seen_threads = []
    import duckdb as real_duckdb
    real_connect = real_duckdb.connect

    class _SpyingConnection:
        """Delegates everything to the real connection — `DuckDBPyConnection`
        methods are native and read-only, so this is a thin Python-level
        proxy rather than a monkeypatched instance method."""

        def __init__(self, con):
            self._con = con

        def execute(self, sql, *ea, **ekw):
            if "SET threads" in sql:
                seen_threads.append(sql)
            return self._con.execute(sql, *ea, **ekw)

        def __getattr__(self, name):
            return getattr(self._con, name)

    def spying_connect(*a, **kw):
        return _SpyingConnection(real_connect(*a, **kw))

    monkeypatch.setattr(real_duckdb, "connect", spying_connect)

    stats(cfg, root=root)
    search_under(cfg, root)
    search_ranked(cfg, root, q="alpha")
    guarded_query._connect(cfg)

    assert len(seen_threads) == 4
    for sql in seen_threads:
        assert sql == f"SET threads TO {store.search_threads()}"


# -- the regression ----------------------------------------------------------
#
# `_tree` and `_prime_index` (the synthetic corpus + real-index builders) and
# the latency sampling loop now live in `index_perf_harness`, reusable by any
# test that needs to overlap a real scan with real `/api/index/rank` calls —
# see that module's docstring. This test is the harness's original case,
# refactored onto it rather than rewritten: build a tree, prime an index over
# it, start a real full scan, and sample rank latency while it runs.

def test_rank_route_stays_fast_while_a_real_scan_is_running(home, tmp_path):
    root = _tree(str(tmp_path / "src"))
    _prime_index(tmp_path, root)
    client = TestClient(create_app(start_dir=root))

    started = client.post("/api/index/scan",
                          json={"root": root, "full": True},
                          headers={"X-Fused": "1"})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]

    def running():
        return client.get("/api/index/status",
                          params={"run_id": run_id}).json()["running"]

    try:
        if not running():
            pytest.skip("the scan finished before the first rank request; "
                        "cannot observe overlap on this machine")
        report = sample_rank_latencies(client, root, running)
    finally:
        client.post("/api/index/cancel", json={"run_id": run_id},
                    headers={"X-Fused": "1"})
        deadline = time.time() + 60
        while time.time() < deadline and running():
            time.sleep(0.1)

    report.assert_ceiling(2.0)
