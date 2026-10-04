import pytest

from fused_render.ai import registry, supervisor


@pytest.fixture(autouse=True)
def _clean_workers():
    yield
    with supervisor._lock:
        supervisor._workers.clear()
        supervisor._worker_tokens.clear()


def test_speech_job_prefix_is_its_own_and_sanitizes_like_image_does():
    assert supervisor.SPEECH_JOB_PREFIX == supervisor.JOB_PREFIX.rsplit("ai-model:", 1)[0] + "ai-speech:"
    assert supervisor.speech_job_id("abc-123") == supervisor.SPEECH_JOB_PREFIX + "abc-123"
    assert supervisor.speech_job_id("a b/c!d") == supervisor.SPEECH_JOB_PREFIX + "abcd"


def test_speech_job_prefix_is_distinct_from_every_other_prefix():
    prefixes = {supervisor.IMAGE_JOB_PREFIX, supervisor.TRANSCRIBE_JOB_PREFIX,
                supervisor.VIDEO_JOB_PREFIX, supervisor.TEXT_JOB_PREFIX,
                supervisor.SPEECH_JOB_PREFIX, supervisor.JOB_PREFIX}
    assert len(prefixes) == 6


def test_leak_ceiling_uses_the_generate_timeout_for_speech():
    assert supervisor._leak_ceiling(registry.TEXT_TO_SPEECH, 60.0) == (
        supervisor.GENERATE_TIMEOUT_S + supervisor._LEAK_CEILING_MARGIN_S)


def _fake_worker(model="mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16"):
    worker = supervisor.Worker(model=model, capability=registry.TEXT_TO_SPEECH,
                               runner_code="mlx-audio-tts", token="tok-speech")
    worker.state = "ready"
    with supervisor._lock:
        supervisor._workers[registry.TEXT_TO_SPEECH] = worker
        supervisor._worker_tokens.add(worker.token)
    return worker


def test_unload_accepts_the_speech_capability(monkeypatch):
    _fake_worker()
    monkeypatch.setattr(supervisor, "_terminate", lambda w: None)
    assert supervisor.unload(capability=registry.TEXT_TO_SPEECH, reason="test") is True
    with supervisor._lock:
        assert registry.TEXT_TO_SPEECH not in supervisor._workers


def test_start_speech_titles_the_row_with_the_text(monkeypatch):
    opened = []
    monkeypatch.setattr(supervisor, "_runner_or_raise", lambda capability: None)
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    monkeypatch.setattr(supervisor, "_report", lambda job, **fields: opened.append(fields))
    monkeypatch.setattr(supervisor, "generate_speech",
                        lambda model, request, job: {"path": request["out"]})
    supervisor.start_speech("org/tts", {"text": "Read this aloud.", "out": "/tmp/x.wav"},
                            supervisor.speech_job_id("t1"))
    assert opened[0]["title"] == "Read this aloud."


def test_start_speech_raises_before_opening_a_row_with_no_runner(monkeypatch):
    opened = []
    monkeypatch.setattr(supervisor, "_report", lambda job, **fields: opened.append(fields))

    def refuse(capability):
        raise supervisor.SupervisorError("needs Apple Silicon")

    monkeypatch.setattr(supervisor, "_runner_or_raise", refuse)
    with pytest.raises(supervisor.SupervisorError):
        supervisor.start_speech("org/tts", {"text": "x", "out": "/tmp/x.wav"}, "sys:ai-speech:x")
    assert opened == []
