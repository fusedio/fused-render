"""The image-to-3D additions to `fused_render.ai.supervisor` (SPEC §48).

Mirrors `test_ai_supervisor_video.py` — a direct, HTTP-free look at the mesh
capability's own pieces: `mesh_job_id`'s shape, `MESH_TIMEOUT_S`'s wiring
into the leak ceiling, and `unload`/`cancel_generation` accepting the new
capability like every other one they already accept.
"""
import pytest

from fused_render.ai import registry, supervisor


@pytest.fixture(autouse=True)
def _clean_workers():
    """`supervisor._workers` is process-global state; a test that adds a
    fake resident worker must not leak it into the next test."""
    yield
    with supervisor._lock:
        supervisor._workers.clear()
        supervisor._worker_tokens.clear()


def test_mesh_job_prefix_is_its_own_and_sanitizes_like_image_does():
    assert supervisor.MESH_JOB_PREFIX == supervisor.JOB_PREFIX.rsplit("ai-model:", 1)[0] + "ai-mesh:"
    assert supervisor.mesh_job_id("abc-123") == supervisor.MESH_JOB_PREFIX + "abc-123"
    # Non alnum/._- characters are stripped, exactly like `image_job_id`.
    assert supervisor.mesh_job_id("a b/c!d") == supervisor.MESH_JOB_PREFIX + "abcd"


def test_mesh_job_prefix_is_distinct_from_every_other_prefix():
    prefixes = {supervisor.IMAGE_JOB_PREFIX, supervisor.TRANSCRIBE_JOB_PREFIX,
                supervisor.VIDEO_JOB_PREFIX, supervisor.MESH_JOB_PREFIX, supervisor.JOB_PREFIX}
    assert len(prefixes) == 5


def test_mesh_timeout_is_thirty_minutes():
    assert supervisor.MESH_TIMEOUT_S == 1800.0


def test_leak_ceiling_uses_the_mesh_timeout_for_image_to_3d():
    window = 60.0
    assert supervisor._leak_ceiling(registry.IMAGE_TO_3D, window) == (
        supervisor.MESH_TIMEOUT_S + supervisor._LEAK_CEILING_MARGIN_S)
    # Unaffected: the other capabilities' ceilings are unchanged by this addition.
    assert supervisor._leak_ceiling(registry.VIDEO_GENERATION, window) == (
        supervisor.VIDEO_TIMEOUT_S + supervisor._LEAK_CEILING_MARGIN_S)
    assert supervisor._leak_ceiling(registry.IMAGE_GENERATION, window) == (
        supervisor.GENERATE_TIMEOUT_S + supervisor._LEAK_CEILING_MARGIN_S)


def _fake_worker(capability, model="dgrauet/hunyuan3d-2.1-mlx"):
    worker = supervisor.Worker(model=model, capability=capability,
                               runner_code="hunyuan3d-mlx", token="tok-mesh")
    worker.state = "ready"
    with supervisor._lock:
        supervisor._workers[capability] = worker
        supervisor._worker_tokens.add(worker.token)
    return worker


def test_unload_accepts_the_mesh_capability(monkeypatch):
    worker = _fake_worker(registry.IMAGE_TO_3D)
    monkeypatch.setattr(supervisor, "_terminate", lambda w: None)
    assert supervisor.unload(capability=registry.IMAGE_TO_3D, reason="test") is True
    with supervisor._lock:
        assert registry.IMAGE_TO_3D not in supervisor._workers


def test_unload_by_model_also_reaches_a_mesh_worker(monkeypatch):
    worker = _fake_worker(registry.IMAGE_TO_3D, model="some/other-mesh-model")
    monkeypatch.setattr(supervisor, "_terminate", lambda w: None)
    assert supervisor.unload(model="some/other-mesh-model", reason="test") is True


def test_cancel_generation_reaches_a_mesh_worker(monkeypatch):
    """Pins `_RUNNERS` to `hunyuan3d-mlx` alone, matching `_fake_worker`'s
    hardcoded `runner_code` — see `test_ai_supervisor_video.py`'s matching
    test for why this pin removes a hardware dependency (`ready_worker`'s
    "a mismatch EVICTS" contract would otherwise fire on any Apple Silicon
    box running this suite)."""
    monkeypatch.setattr(registry, "_RUNNERS", (registry.by_code("hunyuan3d-mlx"),))
    worker = _fake_worker(registry.IMAGE_TO_3D)
    calls = {}

    def fake_worker_request(w, path, body=None, timeout=None):
        calls["path"] = path

        class _Resp:
            def close(self):
                pass
        return _Resp()

    monkeypatch.setattr(supervisor, "_worker_request", fake_worker_request)
    assert supervisor.cancel_generation(capability=registry.IMAGE_TO_3D) is True
    assert calls["path"] == "/cancel"


def test_cancel_generation_with_no_mesh_worker_is_a_no_op():
    assert supervisor.cancel_generation(capability=registry.IMAGE_TO_3D) is False


def test_start_mesh_raises_before_opening_a_job_off_apple_silicon(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    with pytest.raises(supervisor.SupervisorError, match="Apple Silicon"):
        supervisor.start_mesh("dgrauet/hunyuan3d-2.1-mlx", {"image": "x.png"},
                              "sys:ai-mesh:test")
