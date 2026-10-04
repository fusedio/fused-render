import importlib.util
import os
import sys
import threading
import types
import wave

import pytest

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "mlx_audio_tts", "worker.py",
)

MODEL = "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16"


class FakeBase:
    class Cancelled(Exception):
        pass

    def __init__(self, cancel_after=None):
        self.ticks = []
        self.state = {}
        self.cancel_after = cancel_after

    def report(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        if self.cancel_after is not None and len(self.ticks) > self.cancel_after:
            raise self.Cancelled()

    def set_state(self, **fields):
        self.state.update(fields)

    def serve(self, **kwargs):
        return None


class FakeResult:
    def __init__(self, samples, sample_rate=24000):
        self.audio = samples
        self.sample_rate = sample_rate


class FakeModel:
    def __init__(self, tts_model_type="custom_voice", speakers=("Ryan", "Serena"),
                 tokens_per_segment=30):
        self.config = types.SimpleNamespace(model_type="qwen3_tts",
                                            tts_model_type=tts_model_type)
        self.supported_speakers = list(speakers)
        self.supported_languages = ["auto", "english", "chinese"]
        self.sample_rate = 24000
        self.tokens_per_segment = tokens_per_segment
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        hook = sys.modules["mlx_audio.tts.models.qwen3_tts.qwen3_tts"].tqdm
        segments = [s for s in kwargs["text"].split(kwargs["split_pattern"]) if s.strip()]
        for index, _segment in enumerate(segments):
            bar = hook(total=4096, desc=f"Segment {index + 1}/{len(segments)}",
                       unit="tokens", disable=True, leave=False)
            for _ in range(self.tokens_per_segment):
                bar.update(1)
            bar.close()
            yield FakeResult([0.0, 0.5, -0.5, 1.5] * 600)


class FakeMlxCore(types.ModuleType):
    def __init__(self):
        super().__init__("mlx.core")
        self.cpu = "CPU"
        self.made = []
        self.pinned = []
        self._lock = threading.Lock()

    def default_device(self):
        return "GPU"

    def new_thread_unsafe_stream(self, device):
        with self._lock:
            self.made.append(device)
            return f"SHARED-{device}-STREAM"

    def set_default_stream(self, stream):
        self.pinned.append(stream)



def load_worker(monkeypatch, base, model=None, with_tqdm=True):
    made = model if model is not None else FakeModel()
    loaded_from = []

    monkeypatch.setitem(sys.modules, "worker_base", base)
    package = types.ModuleType("mlx_audio")
    tts = types.ModuleType("mlx_audio.tts")
    utils = types.ModuleType("mlx_audio.tts.utils")

    def load_model(path):
        loaded_from.append(path)
        return made

    utils.load_model = load_model
    models = types.ModuleType("mlx_audio.tts.models")
    qwen_pkg = types.ModuleType("mlx_audio.tts.models.qwen3_tts")
    qwen_mod = types.ModuleType("mlx_audio.tts.models.qwen3_tts.qwen3_tts")
    if with_tqdm:
        qwen_mod.tqdm = object()
    qwen_pkg.qwen3_tts = qwen_mod
    for name, module in (("mlx_audio", package), ("mlx_audio.tts", tts),
                         ("mlx_audio.tts.utils", utils), ("mlx_audio.tts.models", models),
                         ("mlx_audio.tts.models.qwen3_tts", qwen_pkg),
                         ("mlx_audio.tts.models.qwen3_tts.qwen3_tts", qwen_mod)):
        monkeypatch.setitem(sys.modules, name, module)
    mlx = types.ModuleType("mlx")
    core = FakeMlxCore()
    mlx.core = core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)

    spec = importlib.util.spec_from_file_location("mlx_audio_tts_worker_under_test", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, made, loaded_from, core


def test_load_hands_mlx_audio_the_snapshot_directory(monkeypatch):
    base = FakeBase()
    worker, _model, loaded_from, core = load_worker(monkeypatch, base)
    worker.load(MODEL, "/snapshots/x")
    assert loaded_from == ["/snapshots/x"]
    assert worker._loaded["mode"] == "preset"
    assert base.state["device"] == "mps"
    assert core.made == ["CPU", "GPU"]


def test_load_refuses_a_checkpoint_that_is_not_qwen3_tts(monkeypatch):
    model = FakeModel()
    model.config.model_type = "kokoro"
    worker, *_ = load_worker(monkeypatch, FakeBase(), model=model)
    with pytest.raises(RuntimeError, match="not a Qwen3-TTS checkpoint"):
        worker.load("org/kokoro", "/snapshots/k")


def test_generate_writes_a_mono_wav_and_ticks_per_segment(monkeypatch, tmp_path):
    base = FakeBase()
    worker, model, *_ = load_worker(monkeypatch, base, model=FakeModel(tokens_per_segment=24))
    worker.load(MODEL, "/snapshots/x")
    module = sys.modules["mlx_audio.tts.models.qwen3_tts.qwen3_tts"]
    original = module.tqdm
    out = str(tmp_path / "speech" / "clip.wav")
    reply = worker.generate({"text": "One.\n\nTwo.", "voice": "Ryan", "language": "English",
                             "instruct": "calm", "out": out, "job": "j"})
    assert reply["path"] == out and reply["segments"] == 2 and reply["voice"] == "Ryan"
    with wave.open(out) as handle:
        assert (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (24000, 1, 2)
        assert handle.getnframes() == 2 * 2400
    call = model.calls[0]
    assert (call["voice"], call["instruct"], call["lang_code"]) == ("Ryan", "calm", "english")
    assert call["ref_audio"] is None
    ticks = [t for t in base.ticks if t.get("detail", "").startswith("Segment")]
    assert [(t["done"], t["total"]) for t in ticks] == [(0, 2), (0, 2), (1, 2), (1, 2)]
    assert ticks[-1]["detail"] == "Segment 2/2 · 2s of audio"
    assert module.tqdm is original


def test_cancel_restores_the_tqdm_hook(monkeypatch, tmp_path):
    base = FakeBase(cancel_after=1)
    worker, *_ = load_worker(monkeypatch, base)
    worker.load(MODEL, "/snapshots/x")
    module = sys.modules["mlx_audio.tts.models.qwen3_tts.qwen3_tts"]
    original = module.tqdm
    with pytest.raises(FakeBase.Cancelled):
        worker.generate({"text": "A.", "out": str(tmp_path / "b.wav")})
    assert module.tqdm is original
    assert not os.path.exists(tmp_path / "b.wav")


def test_a_missing_tqdm_hook_fails_loudly(monkeypatch, tmp_path):
    worker, *_ = load_worker(monkeypatch, FakeBase(), with_tqdm=False)
    worker.load(MODEL, "/snapshots/x")
    with pytest.raises(RuntimeError, match="no `tqdm`"):
        worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})


def test_generate_refuses_what_the_loaded_variant_does_not_take(monkeypatch, tmp_path):
    worker, model, *_ = load_worker(monkeypatch, FakeBase(), model=FakeModel("base"))
    worker.load(MODEL, "/snapshots/x")
    with pytest.raises(ValueError, match="needs 'refAudio'"):
        worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    assert model.calls == []


def test_clone_passes_the_sample_and_its_words(monkeypatch, tmp_path):
    worker, model, *_ = load_worker(monkeypatch, FakeBase(), model=FakeModel("base", speakers=()))
    worker.load("org/base", "/snapshots/b")
    reply = worker.generate({"text": "A.", "refAudio": "/tmp/me.wav", "refText": "hello",
                             "out": str(tmp_path / "a.wav")})
    assert (model.calls[0]["ref_audio"], model.calls[0]["ref_text"]) == ("/tmp/me.wav", "hello")
    assert reply["refAudio"] == "/tmp/me.wav"


def test_a_preset_model_with_no_voice_uses_its_first_speaker(monkeypatch, tmp_path):
    worker, model, *_ = load_worker(monkeypatch, FakeBase())
    worker.load(MODEL, "/snapshots/x")
    worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    assert model.calls[0]["voice"] == "Ryan"
