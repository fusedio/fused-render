"""`ready_worker` memoises the per-model engine resolution on the worker, keyed
on prefs.json's stat and the model's cache-folder signature (D1387)."""
import json

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
