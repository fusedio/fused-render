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
        self.downloads = []
        self.cancel_after = cancel_after

    def report(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        if self.cancel_after is not None and len(self.ticks) > self.cancel_after:
            raise self.Cancelled()

    def set_state(self, **fields):
        self.state.update(fields)

    def download_snapshot(self, model_id, **kwargs):
        self.downloads.append((model_id, kwargs))
        return f"/snapshots/{model_id.replace('/', '_')}"

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
        self.cleared = 0

    def default_device(self):
        return "GPU"

    def new_thread_unsafe_stream(self, device):
        with self._lock:
            self.made.append(device)
            return f"SHARED-{device}-STREAM"

    def set_default_stream(self, stream):
        self.pinned.append(stream)

    def get_active_memory(self):
        return 123

    def get_peak_memory(self):
        return 456

    def clear_cache(self):
        self.cleared += 1


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


def test_download_fetches_the_whole_repo_through_the_apps_fetcher(monkeypatch):
    base = FakeBase()
    worker, *_ = load_worker(monkeypatch, base)
    assert worker.download(MODEL) == "/snapshots/" + MODEL.replace("/", "_")
    assert base.downloads == [(MODEL, {})]


def test_load_hands_mlx_audio_the_snapshot_directory_not_the_id(monkeypatch):
    base = FakeBase()
    worker, _model, loaded_from, core = load_worker(monkeypatch, base)
    worker.load(MODEL, "/snapshots/x")
    assert loaded_from == ["/snapshots/x"]
    assert worker._loaded["mode"] == "preset"
    assert worker._loaded["voices"] == ["ryan", "serena"]
    assert base.state["device"] == "mps"
    assert core.made == ["CPU", "GPU"]


def test_load_refuses_a_checkpoint_that_is_not_qwen3_tts(monkeypatch):
    model = FakeModel()
    model.config.model_type = "kokoro"
    worker, *_ = load_worker(monkeypatch, FakeBase(), model=model)
    with pytest.raises(RuntimeError, match="not a Qwen3-TTS checkpoint"):
        worker.load("org/kokoro", "/snapshots/k")


def test_generate_writes_a_wav_where_the_server_said(monkeypatch, tmp_path):
    base = FakeBase()
    worker, model, *_ = load_worker(monkeypatch, base)
    worker.load(MODEL, "/snapshots/x")
    out = str(tmp_path / "speech" / "clip.wav")
    reply = worker.generate({"text": "One.\n\nTwo.", "voice": "Ryan", "language": "English",
                             "instruct": "calm", "out": out, "job": "sys:ai-speech:1"})
    assert reply["path"] == out and reply["segments"] == 2
    assert reply["sampleRate"] == 24000 and reply["voice"] == "Ryan"
    assert "refAudio" not in reply
    with wave.open(out) as handle:
        assert handle.getframerate() == 24000
        assert handle.getnchannels() == 1 and handle.getsampwidth() == 2
        assert handle.getnframes() == 2 * 2400
    assert not os.path.exists(out + ".part")
    call = model.calls[0]
    assert call["voice"] == "Ryan" and call["instruct"] == "calm"
    assert call["lang_code"] == "english" and call["split_pattern"] == "\n\n"
    assert "ref_audio" not in call


def test_generate_ticks_per_second_of_audio_per_segment(monkeypatch, tmp_path):
    base = FakeBase()
    worker, *_ = load_worker(monkeypatch, base, model=FakeModel(tokens_per_segment=24))
    worker.load(MODEL, "/snapshots/x")
    worker.generate({"text": "A.\n\nB.", "out": str(tmp_path / "a.wav"), "job": "j"})
    ticks = [t for t in base.ticks if t.get("detail", "").startswith("Segment")]
    assert [(t["done"], t["total"]) for t in ticks] == [(0, 2), (0, 2), (1, 2), (1, 2)]
    assert ticks[-1]["detail"] == "Segment 2/2 · 2s of audio"


def test_the_tqdm_hook_is_restored_after_success_and_after_cancel(monkeypatch, tmp_path):
    base = FakeBase()
    worker, *_ = load_worker(monkeypatch, base)
    worker.load(MODEL, "/snapshots/x")
    module = sys.modules["mlx_audio.tts.models.qwen3_tts.qwen3_tts"]
    original = module.tqdm
    worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    assert module.tqdm is original
    base.cancel_after = 1
    with pytest.raises(FakeBase.Cancelled):
        worker.generate({"text": "A.", "out": str(tmp_path / "b.wav")})
    assert module.tqdm is original
    assert not os.path.exists(tmp_path / "b.wav")


def test_a_missing_tqdm_hook_fails_loudly(monkeypatch, tmp_path):
    worker, *_ = load_worker(monkeypatch, FakeBase(), with_tqdm=False)
    worker.load(MODEL, "/snapshots/x")
    with pytest.raises(RuntimeError, match="no module-level `tqdm`"):
        worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})


@pytest.mark.parametrize("tts_type, body, fragment", [
    ("custom_voice", {"voice": "nobody"}, "has no voice 'nobody'"),
    ("base", {"voice": "ryan"}, "has no preset voices"),
    ("base", {}, "pass 'refAudio'"),
    ("voice_design", {}, "needs 'instruct'"),
    ("custom_voice", {"language": "klingon"}, "has no language 'klingon'"),
])
def test_generate_refuses_what_the_loaded_variant_does_not_take(monkeypatch, tmp_path,
                                                                 tts_type, body, fragment):
    worker, model, *_ = load_worker(monkeypatch, FakeBase(),
                                    model=FakeModel(tts_model_type=tts_type))
    worker.load(MODEL, "/snapshots/x")
    with pytest.raises(ValueError, match=fragment):
        worker.generate({"text": "A.", "out": str(tmp_path / "a.wav"), **body})
    assert model.calls == []


def test_clone_passes_the_sample_and_its_words(monkeypatch, tmp_path):
    worker, model, *_ = load_worker(monkeypatch, FakeBase(),
                                    model=FakeModel(tts_model_type="base", speakers=()))
    worker.load("org/base", "/snapshots/b")
    reply = worker.generate({"text": "A.", "refAudio": "/tmp/me.wav", "refText": "hello",
                             "out": str(tmp_path / "a.wav")})
    assert model.calls[0]["ref_audio"] == "/tmp/me.wav"
    assert model.calls[0]["ref_text"] == "hello"
    assert reply["refAudio"] == "/tmp/me.wav" and reply["refText"] == "hello"


def test_generate_needs_text_out_and_a_model(monkeypatch, tmp_path):
    worker, *_ = load_worker(monkeypatch, FakeBase())
    with pytest.raises(RuntimeError, match="no model is loaded"):
        worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    worker.load(MODEL, "/snapshots/x")
    with pytest.raises(ValueError, match="'text'"):
        worker.generate({"text": "  ", "out": str(tmp_path / "a.wav")})
    with pytest.raises(ValueError, match="'out'"):
        worker.generate({"text": "A."})


def test_memory_probes_and_release_use_mlx(monkeypatch):
    worker, _model, _loaded, core = load_worker(monkeypatch, FakeBase())
    assert worker.memory() == 123
    assert worker.peak_memory() == 456
    worker.release()
    assert core.cleared == 1


def test_a_preset_model_with_no_voice_named_speaks_with_its_first_speaker(monkeypatch, tmp_path):
    worker, model, *_ = load_worker(monkeypatch, FakeBase())
    worker.load(MODEL, "/snapshots/x")
    reply = worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    assert model.calls[0]["voice"] == "Ryan"
    assert reply["voice"] == "Ryan"
