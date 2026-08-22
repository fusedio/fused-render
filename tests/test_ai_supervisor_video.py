"""The video-generation additions to `fused_render.ai.supervisor` (SPEC §40).

**Not `tests/test_supervisor_core.py`** — that file drives the platform-neutral
OS tray supervisor (`fused_render.supervisor.core`) and is unrelated to the AI
one; there is no dedicated unit-test file for `fused_render.ai.supervisor`'s
own functions today (its job-id/timeout/unload/cancel behaviour is exercised
indirectly, through the HTTP routes, in `test_ai_runtime.py`). This file is
new for that reason — a direct, HTTP-free look at the four video pieces:
`video_job_id`'s shape, `VIDEO_TIMEOUT_S`'s wiring into the leak ceiling, and
`unload`/`cancel_generation` accepting the new capability like every other one
they already accept.
"""
import threading

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


def test_video_job_prefix_is_its_own_and_sanitizes_like_image_does():
    assert supervisor.VIDEO_JOB_PREFIX == supervisor.JOB_PREFIX.rsplit("ai-model:", 1)[0] + "ai-video:"
    assert supervisor.video_job_id("abc-123") == supervisor.VIDEO_JOB_PREFIX + "abc-123"
    # Non alnum/._- characters are stripped, exactly like `image_job_id`.
    assert supervisor.video_job_id("a b/c!d") == supervisor.VIDEO_JOB_PREFIX + "abcd"


def test_video_job_prefix_is_distinct_from_every_other_prefix():
    prefixes = {supervisor.IMAGE_JOB_PREFIX, supervisor.TRANSCRIBE_JOB_PREFIX,
                supervisor.VIDEO_JOB_PREFIX, supervisor.JOB_PREFIX}
    assert len(prefixes) == 4


def test_video_timeout_is_two_hours():
    assert supervisor.VIDEO_TIMEOUT_S == 2 * 3600.0


def test_leak_ceiling_uses_the_video_timeout_for_video_generation():
    window = 60.0
    assert supervisor._leak_ceiling(registry.VIDEO_GENERATION, window) == (
        supervisor.VIDEO_TIMEOUT_S + supervisor._LEAK_CEILING_MARGIN_S)
    # Unaffected: the other capabilities' ceilings are unchanged by this addition.
    assert supervisor._leak_ceiling(registry.SPEECH_TO_TEXT, window) == (
        supervisor.TRANSCRIBE_TIMEOUT_S + supervisor._LEAK_CEILING_MARGIN_S)
    assert supervisor._leak_ceiling(registry.IMAGE_GENERATION, window) == (
        supervisor.GENERATE_TIMEOUT_S + supervisor._LEAK_CEILING_MARGIN_S)


def _fake_worker(capability, model="MiniMaxAI/MiniMax-H3"):
    worker = supervisor.Worker(model=model, capability=capability,
                               runner_code="h3-video", token="tok-video")
    worker.state = "ready"
    with supervisor._lock:
        supervisor._workers[capability] = worker
        supervisor._worker_tokens.add(worker.token)
    return worker


def test_unload_accepts_the_video_capability(monkeypatch):
    worker = _fake_worker(registry.VIDEO_GENERATION)
    monkeypatch.setattr(supervisor, "_terminate", lambda w: None)
    assert supervisor.unload(capability=registry.VIDEO_GENERATION, reason="test") is True
    with supervisor._lock:
        assert registry.VIDEO_GENERATION not in supervisor._workers


def test_unload_by_model_also_reaches_a_video_worker(monkeypatch):
    worker = _fake_worker(registry.VIDEO_GENERATION, model="some/other-h3-model")
    monkeypatch.setattr(supervisor, "_terminate", lambda w: None)
    assert supervisor.unload(model="some/other-h3-model", reason="test") is True


def test_cancel_generation_reaches_a_video_worker(monkeypatch):
    worker = _fake_worker(registry.VIDEO_GENERATION)
    calls = {}

    def fake_worker_request(w, path, body=None, timeout=None):
        calls["path"] = path

        class _Resp:
            def close(self):
                pass
        return _Resp()

    monkeypatch.setattr(supervisor, "_worker_request", fake_worker_request)
    assert supervisor.cancel_generation(capability=registry.VIDEO_GENERATION) is True
    assert calls["path"] == "/cancel"


def test_cancel_generation_with_no_video_worker_is_a_no_op():
    assert supervisor.cancel_generation(capability=registry.VIDEO_GENERATION) is False


def test_start_video_raises_before_opening_a_job_off_apple_silicon(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    with pytest.raises(supervisor.SupervisorError, match="Apple Silicon"):
        supervisor.start_video("MiniMaxAI/MiniMax-H3", {"prompt": "x"},
                               "sys:ai-video:test")


def test_child_env_injects_the_resolved_h3_binary_for_video(monkeypatch, tmp_path):
    fake = tmp_path / "h3"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_H3_BIN", str(fake))
    env = supervisor._child_env("tok", "MiniMaxAI/MiniMax-H3", registry.VIDEO_GENERATION)
    assert env["FUSED_RENDER_H3_BIN"] == str(fake)


def test_child_env_does_not_resolve_h3_for_other_capabilities(monkeypatch):
    """The resolver itself must not even be CALLED for a non-video capability
    — a video-specific binary path has no business being derived for, say, an
    image worker's environment."""
    monkeypatch.delenv("FUSED_RENDER_H3_BIN", raising=False)
    calls = []
    monkeypatch.setattr(registry, "h3_bin", lambda: calls.append(1) or "/some/h3")
    env = supervisor._child_env("tok", "org/model", registry.IMAGE_GENERATION)
    assert not calls
    assert "FUSED_RENDER_H3_BIN" not in env


def test_child_env_omits_h3_binary_when_nothing_resolves(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_H3_BIN", raising=False)
    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    monkeypatch.setattr(registry.sys, "frozen", None, raising=False)
    env = supervisor._child_env("tok", "MiniMaxAI/MiniMax-H3", registry.VIDEO_GENERATION)
    assert "FUSED_RENDER_H3_BIN" not in env
