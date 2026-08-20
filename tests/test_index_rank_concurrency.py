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
    monkeypatch.setattr(os, "nice", lambda inc: seen.append(inc) or 0)
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


def test_the_compaction_connection_caps_its_threads():
    con = store.background_connect()
    got = int(con.execute("SELECT current_setting('threads')").fetchone()[0])
    assert got == store.compaction_threads()
    assert 1 <= got <= store.MAX_COMPACTION_THREADS


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
    """A real index over `root` so ranking has a full corpus to scan."""
    cfg = IndexConfig()
    shards = str(tmp_path / "prime-shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    # The root's own dirs row is what `covered` is decided on — without it the
    # route answers `uncovered` with no hits and the timing means nothing.
    sink.add(root, "s", ("sig", [], 0, 1_000_000_000, 0))
    per_dir = 100
    for d in range(n // per_dir):
        dirp = os.path.join(root, f"pre{d:04d}")
        rows = []
        for i in range(per_dir):
            name = f"file{i:03d}_alpha.txt"
            rows.append((os.path.join(dirp, name), dirp, name, "txt",
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
