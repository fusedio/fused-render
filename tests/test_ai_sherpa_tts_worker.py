"""The Kokoro text-to-speech worker (`runners/sherpa_tts/worker.py`).

Loaded by path with a fake `worker_base` and a fake `sherpa_onnx` in
`sys.modules`, the way `test_ai_mlx_audio_tts_worker.py` does it: the worker
imports only the stdlib and numpy at module level, so no engine is needed.
"""
import importlib.util
import os
import sys
import threading
import types
import wave

import pytest

from fused_render.ai.runners import formats

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "sherpa_tts", "worker.py",
)

MODEL = "csukuangfj/kokoro-multi-lang-v1_1"


class FakeBase:
    class Cancelled(Exception):
        pass

    def __init__(self, cancel_after=None):
        self.CANCEL = threading.Event()
        self.ticks = []
        self.state = {}
        self.served = None
        self.downloaded = []
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
        self.downloaded.append((model_id, kwargs))
        return "/cache/" + model_id

    def serve(self, **kwargs):
        self.served = kwargs


class FakeSherpa(types.ModuleType):
    """Classes that record the keyword arguments they were built with."""

    def __init__(self, samples_per_second=24000, seconds=0.1):
        super().__init__("sherpa_onnx")
        self.built = []
        self.calls = []
        outer = self

        def config_class(name):
            class Config:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs
                    outer.built.append((name, kwargs))

            Config.__name__ = name
            return Config

        self.OfflineTtsKokoroModelConfig = config_class("kokoro")
        self.OfflineTtsModelConfig = config_class("model")
        self.OfflineTtsConfig = config_class("config")

        class OfflineTts:
            def __init__(self, config):
                self.config = config
                outer.tts = self

            def generate(self, text, sid=0, speed=1.0):
                outer.calls.append({"text": text, "sid": sid, "speed": speed})
                return types.SimpleNamespace(
                    samples=[0.0, 0.5, -0.5, 1.5] * int(samples_per_second * seconds / 4),
                    sample_rate=samples_per_second)

        self.OfflineTts = OfflineTts


def kokoro_dir(tmp_path):
    folder = tmp_path / "kokoro"
    folder.mkdir()
    for name in ("model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt",
                 "lexicon-zh.txt"):
        (folder / name).write_bytes(b"x")
    (folder / "espeak-ng-data").mkdir()
    (folder / "dict").mkdir()
    return str(folder)


def load_worker(monkeypatch, base, sherpa=None):
    fake = sherpa if sherpa is not None else FakeSherpa()
    monkeypatch.setitem(sys.modules, "worker_base", base)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    spec = importlib.util.spec_from_file_location("sherpa_tts_worker_under_test", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, fake


def test_download_takes_the_whole_repo(monkeypatch):
    base = FakeBase()
    worker, _ = load_worker(monkeypatch, base)
    assert worker.download(MODEL) == "/cache/" + MODEL
    assert base.downloaded == [(MODEL, {})]


def test_load_refuses_a_directory_that_is_not_a_kokoro_export(monkeypatch, tmp_path):
    worker, sherpa = load_worker(monkeypatch, FakeBase())
    (tmp_path / "model.onnx").write_bytes(b"x")
    with pytest.raises(RuntimeError) as error:
        worker.load("org/other", str(tmp_path))
    message = str(error.value)
    assert "org/other" in message and "not a Kokoro sherpa-onnx export" in message
    for name in ("model.onnx", "voices.bin", "tokens.txt", "espeak-ng-data"):
        assert name in message
    assert sherpa.built == []


def test_load_builds_the_config_from_the_six_paths_on_the_cpu(monkeypatch, tmp_path):
    base = FakeBase()
    worker, sherpa = load_worker(monkeypatch, base)
    path = kokoro_dir(tmp_path)
    worker.load(MODEL, path)
    built = dict(sherpa.built)
    assert built["kokoro"] == {
        "model": f"{path}/model.onnx", "voices": f"{path}/voices.bin",
        "tokens": f"{path}/tokens.txt", "data_dir": f"{path}/espeak-ng-data",
        "dict_dir": f"{path}/dict",
        "lexicon": f"{path}/lexicon-us-en.txt,{path}/lexicon-zh.txt"}
    assert built["model"]["provider"] == "cpu" and built["model"]["debug"] is False
    assert built["model"]["num_threads"] == min(4, os.cpu_count() or 1)
    assert built["config"]["max_num_sentences"] == 1
    assert worker._loaded["traits"] == formats.kokoro_traits()
    assert base.state["device"] == "cpu"


def test_load_leaves_out_a_lexicon_the_export_does_not_ship(monkeypatch, tmp_path):
    worker, sherpa = load_worker(monkeypatch, FakeBase())
    path = kokoro_dir(tmp_path)
    os.remove(os.path.join(path, "lexicon-zh.txt"))
    worker.load(MODEL, path)
    assert dict(sherpa.built)["kokoro"]["lexicon"] == f"{path}/lexicon-us-en.txt"


def test_generate_makes_each_part_and_pauses_between_paragraphs(monkeypatch, tmp_path):
    base = FakeBase()
    worker, sherpa = load_worker(monkeypatch, base)
    worker.load(MODEL, kokoro_dir(tmp_path))
    out = str(tmp_path / "speech" / "clip.wav")
    reply = worker.generate({"text": "One.\n\nTwo.", "voice": "bf_vale", "language": "english",
                             "out": out, "job": "j"})
    assert [(c["text"], c["sid"], c["speed"]) for c in sherpa.calls] == [
        ("One.", 2, 1.0), ("Two.", 2, 1.0)]
    with wave.open(out) as handle:
        assert (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (
            24000, 1, 2)
        assert handle.getnframes() == 2 * 2400 + 12000
    assert reply["path"] == out and reply["parts"] == 2
    assert reply["audioSeconds"] == round((2 * 2400 + 12000) / 24000, 2)
    assert reply["voice"] == "bf_vale" and reply["language"] == "english"
    assert isinstance(reply["seconds"], float)
    running = [t for t in base.ticks if t.get("detail", "").startswith("Part")]
    assert [(t["done"], t["total"]) for t in running] == [(0, 2), (1, 2)]
    assert all(t["job"] == "j" and t["kind"] == "task" and t["unit"] == "" for t in base.ticks)
    assert running[-1]["detail"] == "Part 2/2 · 0s of audio"
    assert base.ticks[0]["state"] == "running" and base.ticks[0]["detail"] == "Speaking"


def test_a_chinese_voice_maps_to_its_speaker_id_and_language_is_passed_through(
        monkeypatch, tmp_path):
    worker, sherpa = load_worker(monkeypatch, FakeBase())
    worker.load(MODEL, kokoro_dir(tmp_path))
    reply = worker.generate({"text": "你好。", "voice": "ZF_001", "language": "Chinese",
                             "out": str(tmp_path / "zh.wav")})
    assert sherpa.calls[0]["sid"] == formats.KOKORO_VOICES.index("zf_001") == 3
    assert (reply["voice"], reply["language"]) == ("zf_001", "chinese")


def test_no_voice_means_the_first_listed_voice(monkeypatch, tmp_path):
    worker, sherpa = load_worker(monkeypatch, FakeBase())
    worker.load(MODEL, kokoro_dir(tmp_path))
    reply = worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    assert sherpa.calls[0]["sid"] == 0 and reply["voice"] == "af_maple"
    assert reply["language"] == "auto"


def test_an_unknown_voice_names_three_that_exist(monkeypatch, tmp_path):
    worker, sherpa = load_worker(monkeypatch, FakeBase())
    worker.load(MODEL, kokoro_dir(tmp_path))
    with pytest.raises(ValueError) as error:
        worker.generate({"text": "A.", "voice": "nobody", "out": str(tmp_path / "a.wav")})
    assert "has no voice 'nobody'" in str(error.value)
    assert "af_maple, af_sol, bf_vale" in str(error.value)
    assert sherpa.calls == []


def test_a_style_line_is_refused(monkeypatch, tmp_path):
    worker, sherpa = load_worker(monkeypatch, FakeBase())
    worker.load(MODEL, kokoro_dir(tmp_path))
    with pytest.raises(ValueError, match="has no style line; it takes 'voice', not 'instruct'"):
        worker.generate({"text": "A.", "instruct": "calm", "out": str(tmp_path / "a.wav")})
    assert sherpa.calls == []


def test_clone_options_are_refused(monkeypatch, tmp_path):
    worker, _ = load_worker(monkeypatch, FakeBase())
    worker.load(MODEL, kokoro_dir(tmp_path))
    with pytest.raises(ValueError, match="not 'refAudio', 'refText'"):
        worker.generate({"text": "A.", "refAudio": "/a.wav", "refText": "hi",
                         "out": str(tmp_path / "a.wav")})


def test_generate_needs_text_out_and_a_loaded_model(monkeypatch, tmp_path):
    worker, _ = load_worker(monkeypatch, FakeBase())
    with pytest.raises(RuntimeError, match="no model is loaded"):
        worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    worker.load(MODEL, kokoro_dir(tmp_path))
    with pytest.raises(ValueError, match="'text' and 'out' are required"):
        worker.generate({"text": "  ", "out": str(tmp_path / "a.wav")})


def test_an_engine_that_returns_no_samples_is_an_error(monkeypatch, tmp_path):
    worker, _ = load_worker(monkeypatch, FakeBase(), sherpa=FakeSherpa(seconds=0))
    worker.load(MODEL, kokoro_dir(tmp_path))
    with pytest.raises(RuntimeError, match="returned no audio"):
        worker.generate({"text": "A.", "out": str(tmp_path / "a.wav")})
    assert not os.path.exists(tmp_path / "a.wav")


def test_cancel_stops_between_parts_and_writes_nothing(monkeypatch, tmp_path):
    base = FakeBase()
    worker, sherpa = load_worker(monkeypatch, base)
    worker.load(MODEL, kokoro_dir(tmp_path))
    base.CANCEL.set()
    with pytest.raises(FakeBase.Cancelled):
        worker.generate({"text": "A.\n\nB.", "out": str(tmp_path / "c.wav")})
    assert len(sherpa.calls) <= 1
    assert not os.path.exists(tmp_path / "c.wav")


def test_the_worker_is_served_without_streaming_or_release(monkeypatch):
    base = FakeBase()
    worker, _ = load_worker(monkeypatch, base)
    worker.main()
    assert base.served["streaming"] is False
    assert base.served["memory"] is worker.memory
    assert "release" not in base.served
    assert worker.memory() is None or worker.memory() > 0
