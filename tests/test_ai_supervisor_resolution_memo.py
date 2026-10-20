"""`ready_worker` memoises the per-model engine resolution on the worker, keyed
on prefs.json's stat and the model's cache-folder signature (D1391)."""
import json
import os

from fused_render.ai import hub_cache, registry, supervisor
from fused_render.shell import prefs


def _plant(model="org/m"):
    worker = supervisor.Worker(model=model, capability=registry.TEXT_GENERATION,
                               runner_code="fake-text")
    worker.state = "ready"
    supervisor._workers[registry.TEXT_GENERATION] = worker
    return worker


def test_unchanged_prefs_and_snapshot_resolve_once(tmp_path, monkeypatch):
    prefs_file = tmp_path / "prefs.json"
    prefs_file.write_text("{}")
    monkeypatch.setattr(prefs, "_path", lambda: str(prefs_file))
    monkeypatch.setattr(hub_cache, "hub_cache_dir", lambda: str(tmp_path / "hub"))
    runner = registry.Runner(code="fake-text", capability=registry.TEXT_GENERATION,
                             folder=str(tmp_path), label="Fake")
    calls = []

    def counted(capability, model_id, pinned=None):
        calls.append(model_id)
        return registry.Resolution(runner)

    monkeypatch.setattr(registry, "resolve_for_model", counted)
    supervisor.reset()
    try:
        worker = _plant()
        assert supervisor.ready_worker(registry.TEXT_GENERATION, "org/m") is worker
        assert supervisor.ready_worker(registry.TEXT_GENERATION, "org/m") is worker
        assert len(calls) == 1
        # A prefs change invalidates the memo.
        prefs_file.write_text(json.dumps({"model_engines": {"org/m": "fake-text"}}))
        assert supervisor.ready_worker(registry.TEXT_GENERATION, "org/m") is worker
        assert len(calls) == 2
    finally:
        supervisor.reset()


def _setup(tmp_path, monkeypatch):
    prefs_file = tmp_path / "prefs.json"
    prefs_file.write_text("{}")
    monkeypatch.setattr(prefs, "_path", lambda: str(prefs_file))
    monkeypatch.setattr(hub_cache, "hub_cache_dir", lambda: str(tmp_path / "hub"))
    runner = registry.Runner(code="fake-text", capability=registry.TEXT_GENERATION,
                             folder=str(tmp_path), label="Fake")
    calls = []

    def counted(capability, model_id, pinned=None):
        calls.append(model_id)
        return registry.Resolution(runner)

    monkeypatch.setattr(registry, "resolve_for_model", counted)
    supervisor.reset()
    return prefs_file, calls


def test_a_changed_repo_signature_alone_re_resolves(tmp_path, monkeypatch):
    _, calls = _setup(tmp_path, monkeypatch)
    signature = [("a",)]
    monkeypatch.setattr(hub_cache, "repo_signature", lambda repo_id: signature[0])
    try:
        worker = _plant()
        supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
        supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
        assert len(calls) == 1
        signature[0] = ("b",)
        assert supervisor.ready_worker(registry.TEXT_GENERATION, "org/m") is worker
        assert len(calls) == 2
    finally:
        supervisor.reset()


def test_same_size_prefs_swaps_inside_one_mtime_tick_are_both_seen(
        tmp_path, monkeypatch):
    prefs_file, calls = _setup(tmp_path, monkeypatch)
    try:
        _plant()
        supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
        frozen = prefs_file.stat()
        for pin in ("fake-a", "fake-b", "fake-c"):
            prefs._store({"model_engines": {"org/m": pin}})
            os.utime(prefs_file, ns=(frozen.st_atime_ns, frozen.st_mtime_ns))
            before = len(calls)
            supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
            assert len(calls) == before + 1, pin
    finally:
        supervisor.reset()


def test_a_runner_becoming_available_re_resolves_without_any_other_change(
        tmp_path, monkeypatch):
    _, calls = _setup(tmp_path, monkeypatch)
    try:
        _plant()
        supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
        supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
        assert len(calls) == 1
        supervisor._bump_resolution_epoch()
        supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
        assert len(calls) == 2
    finally:
        supervisor.reset()


def test_the_memo_expires_after_its_ttl_as_a_backstop(tmp_path, monkeypatch):
    _, calls = _setup(tmp_path, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock[0])
    try:
        _plant()
        supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
        clock[0] += supervisor._RESOLUTION_TTL - 1
        supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
        assert len(calls) == 1
        clock[0] += 2
        supervisor.ready_worker(registry.TEXT_GENERATION, "org/m")
        assert len(calls) == 2
    finally:
        supervisor.reset()
