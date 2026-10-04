import importlib.util
import json
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
HOOK = "mlx_audio.tts.models.qwen3_tts.qwen3_tts"


class FakeBase:
    class Cancelled(Exception):
        pass

    def __init__(self, cancel_after=None):
        self.CANCEL = threading.Event()
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


class FakeModel:
    sample_rate = 24000

    def __init__(self, tokens_per_chunk=30):
        self.tokens_per_chunk = tokens_per_chunk
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        bar = sys.modules[HOOK].tqdm(total=4096, desc="Segment 1/1", disable=True)
        for _ in range(self.tokens_per_chunk):
            bar.update(1)
        bar.close()
        yield types.SimpleNamespace(audio=[0.0, 0.5, -0.5, 1.5] * 600)


class FakeMlxCore(types.ModuleType):
    def __init__(self):
        super().__init__("mlx.core")
        self.cpu = "CPU"
        self.made = []
        self._lock = threading.Lock()

    def default_device(self):
        return "GPU"

    def new_thread_unsafe_stream(self, device):
        with self._lock:
            self.made.append(device)
            return f"SHARED-{device}-STREAM"

    def set_default_stream(self, stream):
        pass


def snapshot(tmp_path, tts_model_type="custom_voice", speakers=("serena", "ryan")):
    folder = tmp_path / f"snapshot-{tts_model_type}"
    folder.mkdir(exist_ok=True)
    (folder / "config.json").write_text(json.dumps({
        "model_type": "qwen3_tts", "tts_model_type": tts_model_type,
        "talker_config": {"spk_id": {name: i for i, name in enumerate(speakers)},
                          "codec_language_id": {"english": 1, "chinese": 2,
                                                "sichuan_dialect": 3}},
    }), encoding="utf-8")
    return str(folder)


def load_worker(monkeypatch, base, model=None, with_tqdm=True):
    made = model if model is not None else FakeModel()
    loaded_from = []
    monkeypatch.setitem(sys.modules, "worker_base", base)
    utils = types.ModuleType("mlx_audio.tts.utils")
    utils.load_model = lambda path: (loaded_from.append(path), made)[1]
    qwen_pkg = types.ModuleType("mlx_audio.tts.models.qwen3_tts")
    qwen_mod = types.ModuleType(HOOK)
    if with_tqdm:
        qwen_mod.tqdm = object()
    qwen_pkg.qwen3_tts = qwen_mod
    for name, module in (("mlx_audio", types.ModuleType("mlx_audio")),
                         ("mlx_audio.tts", types.ModuleType("mlx_audio.tts")),
                         ("mlx_audio.tts.utils", utils),
                         ("mlx_audio.tts.models", types.ModuleType("mlx_audio.tts.models")),
                         ("mlx_audio.tts.models.qwen3_tts", qwen_pkg), (HOOK, qwen_mod)):
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


def test_load_reads_the_config_and_hands_mlx_audio_the_snapshot(monkeypatch, tmp_path):
    base = FakeBase()
    worker, _model, loaded_from, core = load_worker(monkeypatch, base)
    path = snapshot(tmp_path)
    worker.load(MODEL, path)
    assert loaded_from == [path]
    assert worker._loaded["traits"] == {"mode": "preset", "voices": ["ryan", "serena"],
                                        "languages": ["chinese", "english"]}
    assert base.state["device"] == "mps"
    assert core.made == ["CPU", "GPU"]


def test_load_refuses_a_checkpoint_that_is_not_qwen3_tts(monkeypatch, tmp_path):
    worker, _model, loaded_from, _core = load_worker(monkeypatch, FakeBase())
    with pytest.raises(RuntimeError, match="not a Qwen3-TTS checkpoint"):
        worker.load("org/kokoro", str(tmp_path))
    assert loaded_from == []


def test_speech_chunks_keep_paragraphs_and_pack_sentences_under_the_limit(monkeypatch):
    worker, *_ = load_worker(monkeypatch, FakeBase())
    text = "One. Two!  Three?\n\n\n Four\nfive.\n\n   \n"
    assert worker.speech_chunks(text) == [["One. Two! Three?"], ["Four five."]]
    assert worker.speech_chunks("Aa aa. Bb bb. Cc cc.", limit=13) == [["Aa aa. Bb bb.", "Cc cc."]]
    assert worker.speech_chunks("一二三。四五六。", limit=4) == [["一二三。", "四五六。"]]
    assert worker.speech_chunks("aaaa bbbb cccc", limit=6) == [["aaaa", "bbbb", "cccc"]]
    assert worker.speech_chunks("x" * 10, limit=4) == [["xxxx", "xxxx", "xx"]]


def test_long_text_is_made_in_parts_with_a_pause_between_paragraphs(monkeypatch, tmp_path):
    base = FakeBase()
    worker, model, *_ = load_worker(monkeypatch, base, model=FakeModel(tokens_per_chunk=24))
    worker.load(MODEL, snapshot(tmp_path))
    out = str(tmp_path / "speech" / "clip.wav")
    reply = worker.generate({"text": "One.\n\nTwo.", "voice": "Ryan", "language": "English",
                             "instruct": "calm", "out": out, "job": "j"})
    assert [call["text"] for call in model.calls] == ["One.", "Two."]
    call = model.calls[0]
    assert (call["voice"], call["instruct"], call["lang_code"]) == ("ryan", "calm", "english")
    assert call["split_pattern"] == "" and call["ref_audio"] is None
    assert reply["parts"] == 2 and reply["voice"] == "ryan"
    with wave.open(out) as handle:
        assert (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (24000, 1, 2)
        assert handle.getnframes() == 2 * 2400 + 12000
    ticks = [t for t in base.ticks if t.get("detail", "").startswith("Part")]
    assert [(t["done"], t["total"]) for t in ticks] == [(0, 2), (0, 2), (1, 2), (1, 2)]
    assert ticks[-1]["detail"] == "Part 2/2 · 4s of audio"
    assert not hasattr(sys.modules[HOOK].tqdm, "update")


def test_cancel_restores_the_tqdm_hook(monkeypatch, tmp_path):
    worker, *_ = load_worker(monkeypatch, FakeBase(cancel_after=1))
    worker.load(MODEL, snapshot(tmp_path))
    original = sys.modules[HOOK].tqdm
    with pytest.raises(FakeBase.Cancelled):
        worker.generate({"text": "A.", "out": str(tmp_path / "b.wav")})
    assert sys.modules[HOOK].tqdm is original
    assert not os.path.exists(tmp_path / "b.wav")


def test_a_missing_tqdm_hook_fails_loudly(monkeypatch, tmp_path):
    worker, *_ = load_worker(monkeypatch, FakeBase(), with_tqdm=False)
    worker.load(MODEL, snapshot(tmp_path))
    with pytest.raises(RuntimeError, match="no `tqdm`"):
        worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})


def test_generate_applies_the_shared_option_rules(monkeypatch, tmp_path):
    worker, model, *_ = load_worker(monkeypatch, FakeBase())
    worker.load(MODEL, snapshot(tmp_path, "base", speakers=()))
    with pytest.raises(ValueError, match="needs 'refAudio', 'refText'"):
        worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    assert model.calls == []
    reply = worker.generate({"text": "A.", "refAudio": "/tmp/me.wav", "refText": "hello",
                             "out": str(tmp_path / "a.wav")})
    assert (model.calls[0]["ref_audio"], model.calls[0]["ref_text"]) == ("/tmp/me.wav", "hello")
    assert reply["refAudio"] == "/tmp/me.wav"


def test_a_preset_model_with_no_voice_uses_the_first_listed_voice(monkeypatch, tmp_path):
    worker, model, *_ = load_worker(monkeypatch, FakeBase())
    worker.load(MODEL, snapshot(tmp_path))
    worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    assert model.calls[0]["voice"] == "ryan"


def test_fused_ai_cancel_stops_between_tokens(monkeypatch, tmp_path):
    base = FakeBase()
    model = FakeModel(tokens_per_chunk=5)
    worker, *_ = load_worker(monkeypatch, base, model=model)
    worker.load(MODEL, snapshot(tmp_path))
    base.CANCEL.set()
    with pytest.raises(FakeBase.Cancelled):
        worker.generate({"text": "A.\n\nB.", "out": str(tmp_path / "c.wav")})
    assert len(model.calls) == 1
    assert sys.modules[HOOK].tqdm is not None and not hasattr(sys.modules[HOOK].tqdm, "update")
    assert not os.path.exists(tmp_path / "c.wav")
