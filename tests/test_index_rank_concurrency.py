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

import pytest

from fused_render.index import store, worker


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
