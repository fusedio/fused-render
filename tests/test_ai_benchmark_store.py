"""Tests for the benchmark run store and the fixed workload table (SPEC AI-14).

Two halves of Task 1: `ai/bench_store.py` (the persisted, capped, append-only
run list at ~/.fused-render/ai_benchmarks.json) and the `WORKLOADS` /
`machine()` half of `ai/benchmark.py`.

FUSED_RENDER_HOME is redirected to a tmp dir exactly as tests/test_shell_prefs.py
does it, so no test reads or writes a developer's real store.
"""
import json
from collections.abc import Mapping

from fused_render.ai import bench_store, benchmark
from fused_render.ai import registry as ai_registry


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


def _run(run_id: str, started_at: float = 1.0) -> dict:
    """A minimal run record. The store is deliberately schema-agnostic — it
    persists whatever `benchmark.run()` produced — so these tests only pin the
    two keys the store itself reads: `id` (delete) and nothing else."""
    return {"id": run_id, "startedAt": started_at, "capability": "text-generation"}


# -- bench_store ----------------------------------------------------------------


def test_empty_store_reads_as_no_runs(tmp_path, monkeypatch):
    """Absent file → empty list, never a raise: the history endpoint is a GET
    that has to answer on a machine that has never benchmarked anything."""
    _home(tmp_path, monkeypatch)
    assert bench_store.read() == []


def test_append_then_read_round_trips(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    bench_store.append(_run("a", 1.0))
    bench_store.append(_run("b", 2.0))
    runs = bench_store.read()
    assert [r["id"] for r in runs] == ["a", "b"]  # oldest first


def test_a_corrupt_file_reads_as_empty(tmp_path, monkeypatch):
    """A half-written or hand-edited file must not take the page down. Same
    contract storage.read_json already gives (absent OR corrupt → None); this
    asserts the store does not then trip over the None."""
    home = _home(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    (home / "ai_benchmarks.json").write_text("{not json", encoding="utf-8")
    assert bench_store.read() == []
    # And an append over the corruption still lands, rather than raising.
    bench_store.append(_run("a"))
    assert [r["id"] for r in bench_store.read()] == ["a"]


def test_a_wrong_shaped_file_reads_as_empty(tmp_path, monkeypatch):
    """Valid JSON of the wrong shape (a list where the envelope belongs, a
    non-list `runs`) is the same class of problem as corruption."""
    home = _home(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    path = home / "ai_benchmarks.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert bench_store.read() == []
    path.write_text(json.dumps({"version": 1, "runs": "nope"}), encoding="utf-8")
    assert bench_store.read() == []


def test_the_cap_drops_the_oldest_and_keeps_the_newest(tmp_path, monkeypatch):
    """The store is bounded by a hard run cap (the plan's stated alternative to
    ai_metrics.py's fixed ring). Pruning is oldest-first, so the run somebody
    just paid minutes of compute for is never the one dropped."""
    _home(tmp_path, monkeypatch)
    for i in range(bench_store.MAX_RUNS + 5):
        bench_store.append(_run(f"r{i}", float(i)))
    runs = bench_store.read()
    assert len(runs) == bench_store.MAX_RUNS
    assert runs[0]["id"] == "r5"
    assert runs[-1]["id"] == f"r{bench_store.MAX_RUNS + 4}"


def test_the_envelope_is_versioned(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    bench_store.append(_run("a"))
    data = json.loads((home / "ai_benchmarks.json").read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert [r["id"] for r in data["runs"]] == ["a"]


def test_delete_removes_only_the_named_ids(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    for name in ("a", "b", "c"):
        bench_store.append(_run(name))
    removed = bench_store.delete(["a", "c", "nosuch"])
    assert removed == 2  # an unknown id is not an error, it is already gone
    assert [r["id"] for r in bench_store.read()] == ["b"]


def test_clear_empties_the_store(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    bench_store.append(_run("a"))
    bench_store.clear()
    assert bench_store.read() == []


# -- the workload table ---------------------------------------------------------


def test_every_capability_has_a_workload(tmp_path, monkeypatch):
    """The guard that makes a fifth capability impossible to add without giving
    it a comparable workload: a section with no fixed workload would render a
    Run button that measures nothing defined."""
    for capability in ai_registry.capabilities():
        assert capability in benchmark.WORKLOADS, capability


def test_no_workload_names_a_capability_the_registry_does_not_know():
    """The other direction — a typo'd constant would leave a workload nothing
    can ever run."""
    known = set(ai_registry.capabilities())
    assert set(benchmark.WORKLOADS) <= known


def test_a_workload_carries_a_name_an_integer_revision_and_frozen_params():
    """`revision` is the comparability seam (the UI refuses a delta across a
    bump), so it has to be an int rather than a string that sorts oddly. The
    params are frozen because a workload somebody can mutate at runtime is not
    a fixed workload."""
    for capability, workload in benchmark.WORKLOADS.items():
        assert isinstance(workload.name, str) and workload.name, capability
        assert isinstance(workload.revision, int), capability
        assert isinstance(workload.params, Mapping), capability
        try:
            workload.params["injected"] = 1
        except TypeError:
            pass  # a MappingProxyType refuses, which is the point
        else:
            raise AssertionError(f"{capability} params are mutable")


def test_a_workload_serializes_to_the_shape_stored_on_a_run():
    workload = benchmark.WORKLOADS[ai_registry.TEXT_GENERATION]
    assert workload.as_dict() == {
        "name": workload.name,
        "revision": workload.revision,
        "params": dict(workload.params),
    }


# -- machine() ------------------------------------------------------------------


def test_machine_reports_this_host_without_touching_the_network():
    """Why a number is not portable, recorded on every run. Values are asserted
    by TYPE rather than by content: the whole point is that they differ per
    machine, and CI is not this laptop."""
    info = benchmark.machine()
    assert set(info) == {"platform", "arch", "cpuCount", "totalMemoryBytes"}
    assert isinstance(info["platform"], str) and info["platform"]
    assert isinstance(info["arch"], str)
    assert info["cpuCount"] is None or info["cpuCount"] >= 1
    assert info["totalMemoryBytes"] is None or info["totalMemoryBytes"] > 0
