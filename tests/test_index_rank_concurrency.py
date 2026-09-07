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

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from fused_render.index import store, worker
from fused_render.index.config import IndexConfig
from fused_render.index.runner import canonical_root
from fused_render.index.store import Sink, compact
from fused_render.server import create_app


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


def test_every_interactive_read_connection_caps_its_threads(home, tmp_path, monkeypatch):
    """`stats`, `search_under`, `search_ranked` (query.py) and guarded_query's
    `_connect` each open a bare `duckdb.connect()` — this pins that all four
    apply `search_threads()` rather than defaulting to one thread per core."""
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

def _tree(root, n_dirs=400, per_dir=100):
    """A tree big enough that a full scan of it overlaps a burst of ranks."""
    os.makedirs(root, exist_ok=True)
    for d in range(n_dirs):
        sub = os.path.join(root, f"dir{d:03d}")
        os.makedirs(sub, exist_ok=True)
        for i in range(per_dir):
            with open(os.path.join(sub, f"file{i:03d}_alpha.txt"), "w") as f:
                f.write("x")
    return root


def _prime_index(tmp_path, root, n=4000):
    """A real index over `root` so ranking has a full corpus to scan.

    Stored under `canonical_root(root)`, not the caller's raw `root`: the
    scan/rank routes canonicalize whatever root they are given before
    querying (platform.md §1), so a row filed under the un-normalized
    literal — a no-op on POSIX, backslash-native on Windows — would leave
    `/api/index/rank` answering `covered: false` with no hits before the
    real scan below ever gets a chance to overlap it."""
    cfg = IndexConfig()
    shards = str(tmp_path / "prime-shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    root = canonical_root(root)
    # The root's own dirs row is what `covered` is decided on — without it the
    # route answers `uncovered` with no hits and the timing means nothing.
    sink.add(root, "s", ("sig", [], 0, 1_000_000_000, 0))
    per_dir = 100
    for d in range(n // per_dir):
        dirp = canonical_root(os.path.join(root, f"pre{d:04d}"))
        rows = []
        for i in range(per_dir):
            name = f"file{i:03d}_alpha.txt"
            rows.append((dirp + "/" + name, dirp, name, "txt",
                         10 + i, 100.0 + i))
        sink.add(dirp, "s", ("sig", rows, sum(r[4] for r in rows),
                             1_000_000_000, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


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

    latencies = []
    try:
        if not running():
            pytest.skip("the scan finished before the first rank request; "
                        "cannot observe overlap on this machine")
        for _ in range(15):
            if not running():
                break
            t0 = time.perf_counter()
            resp = client.get("/api/index/rank",
                              params={"root": root, "q": "alpha", "limit": 50})
            latencies.append(time.perf_counter() - t0)
            assert resp.status_code == 200, resp.text
            # Timing an answer the route declined to compute would measure
            # nothing: every request has to have taken the full ranking plan.
            body = resp.json()
            assert body["covered"] is True, body
            assert body["hits"], body
    finally:
        client.post("/api/index/cancel", json={"run_id": run_id},
                    headers={"X-Fused": "1"})
        deadline = time.time() + 60
        while time.time() < deadline and running():
            time.sleep(0.1)

    assert latencies, "no rank request overlapped the scan"
    assert max(latencies) < 2.0, latencies
