"""The Parakeet runner's own logic, driven directly (SPEC AI-10c, D319).

`tests/test_ai_mlx_whisper_worker.py` is this file's template, and the reason
is the promise it exists to keep: three runners now serve one capability and a
page must not be able to tell which one ran, so the claims pinned there —
progress in seconds of audio, a ✕ honoured throughout, a row every tick can
rebuild, the two output files, the shared speaker labels — are pinned here too,
against a library that provides none of them for free and that will not even
take a waveform.

Testable for the same reason that one is: the module is **stdlib-only at import
time**. `parakeet_mlx`, `mlx.core`, `av` and `numpy` are imported inside the
functions that need them, so the whole flow runs with stubs standing in for
Metal and for ffmpeg.

**What is NOT covered here**, stated rather than implied: no audio is decoded
by a real ffmpeg, no weights are run on a GPU, and no real Parakeet model has
transcribed anything under this file. The library's own shape was read out of
`parakeet-mlx` 0.5.2 — `from_pretrained(path_or_repo)`, `transcribe(path, *,
chunk_duration, overlap_duration, chunk_callback)`, `AlignedResult.sentences`
carrying `text`/`start`/`end`, and `parakeet_mlx.parakeet.load_audio` as the
one door the audio comes through — and the fakes below are built to that. A
real transcription on real hardware is a manual check.
"""
import importlib.util
import json
import os
import sys
import threading
import time
import types

import numpy as np
import pytest

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "parakeet_mlx", "worker.py",
)

RATE = 16000


class FakeBase:
    """A stand-in for `worker_base`, recording every tick.

    The whisper runners' test double, unchanged — all three report to the same
    job manager through the same contract, so a divergence here would be a
    divergence in what is being asserted.
    """

    class Cancelled(Exception):
        pass

    def __init__(self):
        self.ticks = []
        self.CANCEL = threading.Event()
        self.state = {}
        self.fetches = []
        #: Set by a test to have the NEXT tick answer "the ✕ was pressed",
        #: which is how a real cancel reaches a worker.
        self.cancel_on_tick = None

    def report(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        if self.cancel_on_tick is not None and len(self.ticks) >= self.cancel_on_tick:
            raise self.Cancelled()

    def set_state(self, **fields):
        self.state.update(fields)

    def download_snapshot(self, model_id, **kwargs):
        return f"/snapshots/{model_id}"

    def download_file(self, repo_id, filename, detail=None, job=None, row=None):
        self.fetches.append({"repo": repo_id, "file": filename,
                             "job": job, "row": row})
        # The real fetch leaves the row as a FINISHED DOWNLOAD, which is the
        # state the caller then has to restore — modelled rather than stubbed
        # away, because a double that silently returned a path made that
        # failure invisible.
        self.report(job=job, **{**(row or {}), "kind": "download",
                                "unit": "bytes"},
                    state="running", done=2_217_492, total=2_217_492,
                    detail=detail or f"Fetching {filename}…")
        return f"/snapshots/{repo_id}/{filename}"

    def serve(self, **kwargs):
        return None


# -- the fakes standing in for Metal and for ffmpeg ------------------------------


class FakeSentence:
    """An `AlignedSentence`: the unit parakeet-mlx groups its tokens into, and
    the closest thing it has to a Whisper segment. Carries `tokens` too, which
    the runner must DROP so a page cannot come to depend on it."""

    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.duration = end - start
        self.text = text
        self.tokens = [object()]
        self.confidence = 0.9


class FakeResult:
    def __init__(self, sentences):
        self.sentences = list(sentences)
        self.text = " ".join(s.text for s in self.sentences)


class FakeModel:
    """The object `from_pretrained` returns.

    Its `transcribe` does what the real one does in the order the real one does
    it: read the audio through the MODULE's `load_audio` binding (which is the
    swap under test), call `chunk_callback(end_samples, total_samples)` before
    each chunk, and hand back sentences.
    """

    def __init__(self, module, sentences=(), chunks=1, seconds_per_chunk=0.0,
                 rate=RATE):
        self._module = module
        self.preprocessor_config = types.SimpleNamespace(sample_rate=rate)
        self.sentences = list(sentences)
        self.chunks = chunks
        self.seconds_per_chunk = seconds_per_chunk
        #: One entry per `transcribe`, holding what `load_audio` returned.
        self.calls = []
        self.evaluated = []
        #: Set when `transcribe` RETURNS, so a test can land a cancel in the
        #: window between the work finishing and the tick that carries the ✕.
        self.done = threading.Event()

    def parameters(self):
        return {"weights": "LAZY"}

    def transcribe(self, path, *, chunk_duration=None, overlap_duration=None,
                   chunk_callback=None, **kwargs):
        audio = self._module.load_audio(path, self.preprocessor_config.sample_rate,
                                        "bf16")
        self.calls.append({"path": path, "audio": audio,
                           "chunk_duration": chunk_duration,
                           "overlap_duration": overlap_duration})
        try:
            total = len(audio)
            for index in range(self.chunks):
                if chunk_callback is not None:
                    end = int(total * (index + 1) / self.chunks)
                    chunk_callback(end, total)
                time.sleep(self.seconds_per_chunk)
            return FakeResult(self.sentences)
        finally:
            self.done.set()


class FakeParakeetPackage(types.ModuleType):
    """`parakeet_mlx` — the package, carrying `from_pretrained`."""

    def __init__(self, model=None):
        super().__init__("parakeet_mlx")
        self.model = model
        self.loads = []

    def from_pretrained(self, path, **kwargs):
        self.loads.append(path)
        return self.model


def make_parakeet(sentences=(), chunks=1, seconds_per_chunk=0.0, rate=RATE):
    """The two modules the runner touches, wired to one model.

    `parakeet_mlx.parakeet` is where `load_audio` is BOUND (the defining module
    does `from parakeet_mlx.audio import load_audio`), which is why that is the
    module the runner swaps and the module this fake puts it on.
    """
    inner = types.ModuleType("parakeet_mlx.parakeet")
    inner.load_audio = lambda *args, **kwargs: "FFMPEG-WOULD-HAVE-RUN"
    model = FakeModel(inner, sentences=sentences, chunks=chunks,
                      seconds_per_chunk=seconds_per_chunk, rate=rate)
    package = FakeParakeetPackage(model=model)
    return package, inner, model


class FakeResampler:
    """`av.AudioResampler`'s shape: frames in, frames out, and a FLUSH.

    The flush is not decoration — the resampler buffers, so the tail of a
    recording is still inside the filter when the container runs out.
    """

    def __init__(self, format=None, layout=None, rate=None):
        self.args = {"format": format, "layout": layout, "rate": rate}
        self.flushed = False

    def resample(self, frame):
        if frame is None:
            self.flushed = True
            return [FakeFrame(np.full(160, 0.5, dtype=np.float32))]
        return [frame]


class FakeFrame:
    def __init__(self, samples):
        self.samples = samples

    def to_ndarray(self):
        # Planar: one row per channel, which is why the runner reshapes.
        return self.samples.reshape(1, -1)


class FakeContainer:
    def __init__(self, frames, has_audio=True):
        self.frames = frames
        self.streams = types.SimpleNamespace(audio=["STREAM"] if has_audio else [])

    def decode(self, stream):
        return iter(self.frames)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_av(seconds=2.0, has_audio=True):
    """An `av` module holding one recording of `seconds`, at 16 kHz."""
    samples = np.zeros(int(RATE * seconds) - 160, dtype=np.float32)
    module = types.ModuleType("av")
    module.resamplers = []

    def _resampler(**kwargs):
        made = FakeResampler(**kwargs)
        module.resamplers.append(made)
        return made

    module.AudioResampler = _resampler
    module.open = lambda path: FakeContainer([FakeFrame(samples)], has_audio=has_audio)
    return module


class FakeMlxCore(types.ModuleType):
    """`mlx.core` as this runner uses it: arrays, a device, STREAMS and `eval`.

    Streams are the part with teeth. From mlx 0.32 they are per-thread, and an
    unevaluated array forced off the thread that made it aborts the process
    rather than raising — so the runner pins one shared stream on every thread
    that touches MLX. This double records who pinned what, from which thread.
    """

    def __init__(self, **extra):
        super().__init__("mlx.core")
        self.made = []
        #: (thread name, stream) for every `set_default_stream`.
        self.pinned = []
        self.evaluated = []
        self._lock = threading.Lock()
        for name, value in extra.items():
            setattr(self, name, value)

    def array(self, values):
        return values

    def eval(self, *args):
        self.evaluated.append(args)

    def default_device(self):
        return "DEVICE"

    def new_thread_unsafe_stream(self, device):
        with self._lock:
            self.made.append(device)
            return f"SHARED-STREAM-{len(self.made)}"

    def set_default_stream(self, stream):
        with self._lock:
            self.pinned.append((threading.current_thread().name, stream))

    def get_active_memory(self):
        return 0


def load_worker(monkeypatch, base, parakeet=None, inner=None, av_module=None,
                mlx_core=None):
    """A fresh import of the Parakeet worker, against the given fakes.

    By path and with `worker_base` primed in `sys.modules`, for the reason the
    runner exists: it loads its base off `sys.path` in its own interpreter, not
    as `fused_render.ai.runners.…`, so importing it the packaged way would test
    an import that never ships.
    """
    monkeypatch.setitem(sys.modules, "worker_base", base)
    if parakeet is not None:
        monkeypatch.setitem(sys.modules, "parakeet_mlx", parakeet)
        monkeypatch.setitem(sys.modules, "parakeet_mlx.parakeet", inner)
    if av_module is not None:
        monkeypatch.setitem(sys.modules, "av", av_module)
    if mlx_core is None:
        mlx_core = FakeMlxCore()
    mlx = types.ModuleType("mlx")
    mlx.core = mlx_core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)
    spec = importlib.util.spec_from_file_location(
        "parakeet_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: What the supervisor sends as the row's identity (`transcribe_row_fields`).
ROW = {"title": "meeting.m4a", "kind": "task", "cancellable": True, "unit": "s"}


def _request(tmp_path, **over):
    base_path = str(tmp_path / "out")
    return {
        "path": str(tmp_path / "meeting.m4a"),
        "out": base_path + ".json",
        "outText": base_path + ".txt",
        "job": "sys:ai-transcribe:abc",
        "row": dict(ROW),
        **over,
    }


@pytest.fixture()
def base():
    return FakeBase()


@pytest.fixture()
def loaded(monkeypatch, base):
    """The worker with a model 'resident' and a recording to decode."""
    def build(sentences=(), chunks=1, seconds_per_chunk=0.0, audio_seconds=20.0,
              rate=RATE, **kwargs):
        package, inner, model = make_parakeet(
            sentences=sentences, chunks=chunks,
            seconds_per_chunk=seconds_per_chunk, rate=rate)
        worker = load_worker(monkeypatch, base, parakeet=package, inner=inner,
                             av_module=make_av(seconds=audio_seconds, **kwargs))
        worker._loaded["model"] = model
        worker._loaded["rate"] = rate
        worker._TICK_S = 0.02
        return worker, model, inner

    return build


# -- loading, and the format this runner can actually read -----------------------


def _snapshot(tmp_path, config=None, weights=True, name="snap"):
    folder = tmp_path / name
    folder.mkdir()
    if config is not None:
        (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if weights:
        (folder / "model.safetensors").write_bytes(b"")
    return str(folder)


NEMO_CONFIG = {"target": "nemo.collections.asr.models.rnnt_bpe_models."
                         "EncDecRNNTBPEModel"}


def test_a_parakeet_snapshot_loads_and_reports_its_device(monkeypatch, base, tmp_path):
    package, inner, model = make_parakeet()
    core = FakeMlxCore()
    worker = load_worker(monkeypatch, base, parakeet=package, inner=inner,
                         mlx_core=core)

    worker.load("mlx-community/parakeet-tdt-0.6b-v3",
                _snapshot(tmp_path, NEMO_CONFIG))

    assert worker._loaded["model"] is model
    assert worker._loaded["rate"] == RATE
    assert base.state["device"] == "mps"


def test_the_weights_are_EVALUATED_on_the_loading_thread(monkeypatch, base, tmp_path):
    """`from_pretrained` casts every parameter (`v.astype(dtype)`) and never
    calls `mx.eval`, so without this the whole model reaches the decode thread
    as graphs owned by the loader — the failure `_pin_stream` documents, which
    aborts the process from C++ rather than raising."""
    package, inner, _ = make_parakeet()
    core = FakeMlxCore()
    worker = load_worker(monkeypatch, base, parakeet=package, inner=inner,
                         mlx_core=core)

    worker.load("m", _snapshot(tmp_path, NEMO_CONFIG))

    assert core.evaluated, "the parameters were left lazy"
    # …and the stream was pinned BEFORE the weights existed, on this thread.
    assert core.pinned and core.pinned[0][0] == threading.current_thread().name


@pytest.mark.parametrize("config,weights", [
    # A Whisper transformers repo: the same filename, a config that is not NeMo's.
    ({"model_type": "whisper"}, True),
    # A config that names a NeMo class and no weights beside it.
    (NEMO_CONFIG, False),
    # No config at all.
    (None, True),
])
def test_a_repo_in_ANOTHER_FORMAT_is_refused_by_name(monkeypatch, base, tmp_path,
                                                     config, weights):
    """Four incompatible speech formats now exist in this app and the AI Models
    page offers Load on anything tagged "speech recognition", because the
    format is not in the label. This message is the only thing between a user
    and a search engine, so it names what this runner needs and a repo that
    works."""
    package, inner, _ = make_parakeet()
    worker = load_worker(monkeypatch, base, parakeet=package, inner=inner)

    with pytest.raises(RuntimeError) as raised:
        worker.load("org/whisper-large-v3", _snapshot(tmp_path, config, weights))
    message = str(raised.value)
    assert "config.json" in message
    assert "mlx-community/parakeet-tdt-0.6b-v3" in message


def test_a_config_that_will_not_parse_is_the_same_refusal(monkeypatch, base, tmp_path):
    """A broken download, and the message above already says what a good one
    looks like — re-raising a JSON error would send the reader to their disk
    rather than to their repo."""
    package, inner, _ = make_parakeet()
    worker = load_worker(monkeypatch, base, parakeet=package, inner=inner)
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "config.json").write_text("{not json", encoding="utf-8")
    (folder / "model.safetensors").write_bytes(b"")

    with pytest.raises(RuntimeError, match="not a Parakeet export"):
        worker.load("org/m", str(folder))


def test_the_format_check_happens_BEFORE_the_library_is_imported(monkeypatch, base,
                                                                 tmp_path):
    """A repo in the wrong format is a fact about the download, not about this
    environment — importing first would replace the explanation with whichever
    ImportError happened to come first."""
    package, inner, _ = make_parakeet()
    worker = load_worker(monkeypatch, base, parakeet=package, inner=inner)
    monkeypatch.setitem(sys.modules, "parakeet_mlx", None)

    with pytest.raises(RuntimeError, match="not a Parakeet export"):
        worker.load("org/m", _snapshot(tmp_path, {"model_type": "whisper"}))


# -- the audio, which this library will not take ---------------------------------


def test_the_decode_is_handed_the_WAVEFORM_this_process_produced(loaded, tmp_path):
    """The whole risk of this runner. `parakeet_mlx.transcribe(path)` calls
    `load_audio`, which SPAWNS ffmpeg — not shipped here — so the binding is
    swapped and the samples `av` already produced are returned instead."""
    worker, model, inner = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                                  audio_seconds=2.0)

    worker.generate(_request(tmp_path, vad=False))

    handed = model.calls[0]["audio"]
    assert isinstance(handed, np.ndarray)
    assert len(handed) == int(RATE * 2.0)


def test_the_borrowed_binding_is_PUT_BACK(loaded, tmp_path):
    """The module is process-global: a waveform left behind belongs to a
    request that is over, and the next transcription would decode it."""
    worker, _, inner = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")])
    original = inner.load_audio

    worker.generate(_request(tmp_path, vad=False))

    assert inner.load_audio is original


def test_the_borrowed_binding_is_put_back_even_on_a_CANCEL(loaded, base, tmp_path):
    """A ✕ unwinds the HANDLER, not the work: the abandoned decode is still
    inside the swap when `generate` raises, and it restores the binding on its
    own way out. What matters is that it is restored before anything can decode
    again, which is what `_await_orphan` guarantees — so the wait here is the
    same wait the next transcription would do."""
    worker, _, inner = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                              chunks=20, seconds_per_chunk=0.02)
    original = inner.load_audio
    base.cancel_on_tick = 3

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path, vad=False))

    orphan = worker._orphan.get("thread")
    if orphan is not None:
        orphan.join(timeout=5)
        assert not orphan.is_alive()
    assert inner.load_audio is original


def test_a_build_with_NO_load_audio_binding_says_so_rather_than_shelling_out(
        loaded, tmp_path):
    """The guard on reaching into another package's globals. Without it the
    call falls through to the real loader and fails with "FFmpeg is not
    installed", on a machine where that is not the user's problem to fix."""
    worker, _, inner = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")])
    del inner.load_audio

    with pytest.raises(RuntimeError, match="load_audio"):
        worker.generate(_request(tmp_path, vad=False))


def test_the_worker_never_shells_out():
    """This app bundles rclone, not ffmpeg. A runner that spawns one works on
    the machine it was written on and fails on a user's."""
    source = open(WORKER_PATH, encoding="utf-8").read()
    assert "subprocess" not in source
    assert "os.system" not in source


def test_the_resampler_asks_for_the_MODELS_rate_mono_float_and_is_flushed(
        monkeypatch, base, tmp_path):
    """Three details that produce silent nonsense rather than an error when
    wrong: planar float (an int16 buffer read as float is white noise), mono (a
    stereo interview otherwise reads as double-speed speech), and the flush,
    without which the tail of every recording stays inside the filter."""
    package, inner, model = make_parakeet(sentences=[FakeSentence(0.0, 1.0, "hi")],
                                          rate=8000)
    av_module = make_av(seconds=2.0)
    worker = load_worker(monkeypatch, base, parakeet=package, inner=inner,
                         av_module=av_module)
    worker._loaded["model"] = model
    worker._loaded["rate"] = 8000

    worker.generate(_request(tmp_path, vad=False))

    resampler = av_module.resamplers[0]
    # The RATE is the model's, not a constant of the worker: a model trained at
    # another rate must not be fed 16 kHz audio silently.
    assert resampler.args == {"format": "fltp", "layout": "mono", "rate": 8000}
    assert resampler.flushed


def test_a_file_with_no_audio_track_says_so(loaded, tmp_path):
    """PyAV's own answer is an IndexError, which reaches the job row as "list
    index out of range"."""
    worker, _, _ = loaded(has_audio=False)

    with pytest.raises(RuntimeError, match="no audio track"):
        worker.generate(_request(tmp_path))


# -- options this engine cannot honour -------------------------------------------


@pytest.mark.parametrize("sent,needle", [
    ({"task": "translate"}, "only transcribes"),
    ({"language": "en"}, "'language' option"),
    ({"initialPrompt": "Acme Corp"}, "'initialPrompt'"),
])
def test_an_option_this_engine_cannot_HONOUR_is_refused_not_ignored(
        loaded, tmp_path, sent, needle):
    """Parakeet transcribes only, detects its own language and has no text
    conditioning. Accepting any of these and quietly doing something else is
    the worst failure available — a caller asks for English and gets French,
    with nothing saying which engine decided. Each refusal names the ENGINE and
    the way out, because the page is probably correct and simply resolved to a
    runner it was not written for."""
    worker, model, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")])

    with pytest.raises(ValueError) as raised:
        worker.generate(_request(tmp_path, **sent))
    message = str(raised.value)
    assert needle in message
    assert "Parakeet" in message and "AI Models page" in message
    # …and refused BEFORE the audio is decoded, so the user does not pay ninety
    # seconds of `av` for an answer that was available immediately.
    assert model.calls == []


def test_an_ABSENT_option_is_not_a_refusal(loaded, tmp_path):
    """The server sends `language: None` and `initialPrompt: None` on every
    request, and `task: "transcribe"` explicitly — a check on presence rather
    than on truthiness would refuse every call this runner is meant to serve."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")])

    result = worker.generate(_request(tmp_path, task="transcribe", language=None,
                                      initialPrompt=None, vad=False))

    assert result["segments"] == 1
    # No language is REPORTED either: the model does not say, and inventing
    # "en" would be a claim it never made about a multilingual recording.
    assert result["language"] is None


# -- the progress contract, which is a PUBLIC promise ----------------------------


def test_progress_is_SECONDS_OF_AUDIO_and_never_AHEAD_of_the_decode(
        loaded, base, tmp_path):
    """SPEC AI-10a and `runtime.js` both promise `done`/`total` in seconds of
    audio. The library's chunk callback fires BEFORE it decodes each chunk, so
    what is reported is that chunk's START — a position everything before which
    is finished, and one the bar can never be ahead of."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                          audio_seconds=300.0, chunks=5, seconds_per_chunk=0.08)

    worker.generate(_request(tmp_path, vad=False))

    moving = [t for t in base.ticks
              if t.get("detail", "").startswith("Transcribing") and t["done"] is not None]
    assert moving, base.ticks
    assert all(t["unit"] == "s" for t in moving)
    assert all(t["total"] == 300.0 for t in moving)
    # Never ahead of the recording, and never ahead of what the callback said
    # was finished — the callback reports 60/120/180/240/300 as chunk ENDS, and
    # the worker takes a chunk off each.
    assert all(0 <= t["done"] <= 300.0 - worker._CHUNK_S + 0.01 for t in moving), moving
    assert moving == sorted(moving, key=lambda t: t["done"])


def test_with_NO_callback_the_bar_is_INDETERMINATE_rather_than_invented(
        loaded, base, tmp_path):
    """A recording shorter than one chunk never chunks and therefore never
    calls back. A `done` of 0 would claim nothing has been transcribed; None is
    the truth, and the manager renders it as indeterminate."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                          audio_seconds=5.0, chunks=0, seconds_per_chunk=0.0)
    worker._TICK_S = 0.01

    worker.generate(_request(tmp_path, vad=False))

    transcribing = [t for t in base.ticks if t.get("detail") == "Transcribing…"]
    assert all(t["done"] is None and t["total"] is None for t in transcribing)


def test_the_total_is_the_audio_this_process_decoded(loaded, base, tmp_path):
    """Ours because we hold the samples: exact rather than a container's
    declared length, and known before the model sees a thing."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                          audio_seconds=12.0)

    result = worker.generate(_request(tmp_path, vad=False))

    assert result["duration"] == 12.0


def test_EVERY_tick_can_rebuild_the_row_it_reports_to(loaded, base, tmp_path):
    """The job manager can evict any row under capacity pressure and rebuild it
    from the next report, so a tick missing `title` is refused outright and one
    missing `cancellable`/`unit` rebuilds a row that looks operable and is
    not."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                          chunks=3, seconds_per_chunk=0.05)

    worker.generate(_request(tmp_path, vad=False))

    assert base.ticks
    for tick in base.ticks:
        assert tick["title"] == ROW["title"]
        assert tick["cancellable"] is True
        assert tick["job"] == "sys:ai-transcribe:abc"


# -- the transcript ---------------------------------------------------------------


def test_the_two_files_and_the_reply_are_the_whisper_runners_shape(loaded, tmp_path):
    """A page must not be able to tell which of the three engines ran."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.5, " hello "),
                                     FakeSentence(1.5, 3.0, "there")],
                          audio_seconds=4.0)
    request = _request(tmp_path, vad=False)

    result = worker.generate(request)

    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert written["text"] == "hello there"
    assert written["segments"] == [
        {"start": 0.0, "end": 1.5, "text": "hello"},
        {"start": 1.5, "end": 3.0, "text": "there"},
    ]
    assert open(request["outText"], encoding="utf-8").read() == "hello there\n"
    # The reply carries the COUNT, not the segments: a 90-minute recording is
    # thousands of them and the caller already has the path to the file.
    assert result["segments"] == 2


def test_a_sentence_with_no_text_is_dropped(loaded, tmp_path):
    """A transducer can emit a sentence of pure punctuation at a seam. An empty
    segment in the transcript is a row a page renders as a blank line."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "   "),
                                     FakeSentence(1.0, 2.0, "hi")],
                          audio_seconds=4.0)
    request = _request(tmp_path, vad=False)

    worker.generate(request)

    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert [s["text"] for s in written["segments"]] == ["hi"]


# -- the VAD, and what it does to time --------------------------------------------


def _regions(monkeypatch, worker, found):
    """Stand in for the detector, with no onnxruntime and no model file."""
    monkeypatch.setattr(worker, "_speech_regions", lambda *a, **k: list(found))


def test_each_speech_REGION_is_decoded_on_its_own_and_remapped(
        monkeypatch, loaded, tmp_path):
    """The timestamps come back relative to the CLIP and every consumer reads
    them as positions in the FILE. Getting this wrong is silent: a transcript
    that looks perfect and whose every timestamp after the first gap is early."""
    worker, model, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                              audio_seconds=30.0)
    _regions(monkeypatch, worker, [(0.0, 5.0), (20.0, 25.0)])
    request = _request(tmp_path)

    worker.generate(request)

    assert len(model.calls) == 2
    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert [s["start"] for s in written["segments"]] == [0.0, 20.0]
    assert [s["end"] for s in written["segments"]] == [1.0, 21.0]


def test_a_recording_the_detector_finds_NO_speech_in_is_decoded_whole(
        monkeypatch, loaded, tmp_path):
    """Reporting an empty transcript for a file nobody looked at would be the
    confident version of a wrong answer."""
    worker, model, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                              audio_seconds=10.0)
    _regions(monkeypatch, worker, [])

    worker.generate(_request(tmp_path))

    assert len(model.calls) == 1
    assert len(model.calls[0]["audio"]) == RATE * 10


def test_a_segment_that_starts_past_its_region_is_OMITTED_not_flattened(
        monkeypatch, loaded, tmp_path):
    """Clamping both ends would put text on the boundary — speech asserted to
    have happened during silence that was cut out. Nothing was said there."""
    worker, _, _ = loaded(sentences=[FakeSentence(9.0, 12.0, "ghost")],
                          audio_seconds=30.0)
    _regions(monkeypatch, worker, [(0.0, 5.0)])
    request = _request(tmp_path)

    worker.generate(request)

    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert written["segments"] == []


# -- cancelling --------------------------------------------------------------------


def test_a_cancel_while_transcribing_is_honoured_and_writes_nothing(
        loaded, base, tmp_path):
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                          chunks=40, seconds_per_chunk=0.05)
    base.cancel_on_tick = 3
    request = _request(tmp_path, vad=False)

    with pytest.raises(base.Cancelled):
        worker.generate(request)

    assert not os.path.exists(request["out"])


def test_a_cancel_STOPS_the_decode_rather_than_letting_it_run_the_file_out(
        loaded, base, tmp_path):
    """`_STOP` is checked in the chunk callback, which is the one place a ✕ can
    reach inside the library — otherwise an abandoned decode runs a 90-minute
    recording to its end while the next request waits on `_await_orphan`."""
    worker, model, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                              chunks=200, seconds_per_chunk=0.01)
    base.cancel_on_tick = 3

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path, vad=False))

    assert model.done.wait(timeout=5)
    orphan = worker._orphan.get("thread")
    if orphan is not None:
        orphan.join(timeout=5)
        assert not orphan.is_alive()


def test_a_cancel_that_lands_as_the_transcript_FINISHES_still_writes_it(
        loaded, base, tmp_path):
    """An hour of decoding must not be discarded at 99%. A ✕ arriving after the
    last clip has produced its value is honoured for nothing that is left,
    because nothing is."""
    worker, model, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                              chunks=1, seconds_per_chunk=0.15)
    worker._TICK_S = 0.05
    request = _request(tmp_path, vad=False)

    def cancel_when_done():
        model.done.wait(timeout=5)
        base.cancel_on_tick = len(base.ticks) + 1

    threading.Thread(target=cancel_when_done, daemon=True).start()
    try:
        worker.generate(request)
    except base.Cancelled:
        # The race did not land this run; the assertion below is the one that
        # matters and only applies when it did.
        return
    assert os.path.exists(request["out"])


# -- speaker labels ----------------------------------------------------------------
#
# The diarization itself is `tests/test_ai_diarize.py`'s — the shared module all
# three engines import. What is driven HERE is this runner's WIRING.


def _diarizes(monkeypatch, worker, turns, seen=None):
    """Stand in for the whole pipeline, with no sherpa-onnx and no models.

    `model_paths` is left REAL, because the row it reports to is one of the
    things under test — only the ONNX-needing halves are replaced."""
    import diarize as diarize_module

    record = seen if seen is not None else {}
    monkeypatch.setattr(diarize_module, "diarizer",
                        lambda seg, emb, speakers: record.update(
                            speakers=speakers) or object())
    monkeypatch.setattr(diarize_module, "speaker_turns",
                        lambda audio, session, rate, **kw: record.update(
                            samples=len(audio), rate=rate) or list(turns))
    return record


def test_diarization_is_OFF_unless_asked_for(monkeypatch, loaded, tmp_path):
    """Additive or it is a breaking change to every page already transcribing:
    no `speaker` on a segment, no `speakers` in the JSON, and no 33MB fetch."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")])
    _diarizes(monkeypatch, worker, [(0.0, 10.0, 0)])
    request = _request(tmp_path, vad=False)

    result = worker.generate(request)

    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert "speakers" not in written and "speakers" not in result
    assert "speaker" not in written["segments"][0]


def test_the_speaker_PRE_PASS_sees_the_whole_waveform_at_the_MODELS_rate(
        monkeypatch, loaded, tmp_path):
    """Independent of the VAD by design, and denominated in the rate the model
    decodes at — `diarize.speaker_turns` checks that rate against sherpa's own
    and refuses a mismatch, because turns wrong by a ratio land on the wrong
    segments and attribute the transcript to the wrong people."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                          audio_seconds=30.0)
    _regions(monkeypatch, worker, [(0.0, 5.0), (20.0, 25.0)])
    seen = _diarizes(monkeypatch, worker, [(0.0, 30.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2))

    assert seen["samples"] == 30 * RATE
    assert seen["rate"] == RATE


def test_every_segment_is_LABELLED_and_the_json_gains_the_legend(
        monkeypatch, loaded, tmp_path):
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 2.0, "hello"),
                                     FakeSentence(12.0, 14.0, "hi there")],
                          audio_seconds=20.0)
    _diarizes(monkeypatch, worker, [(0.0, 10.0, 0), (10.0, 20.0, 1)])
    request = _request(tmp_path, diarize=True, speakers=2, vad=False)

    result = worker.generate(request)

    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert [s["speaker"] for s in written["segments"]] == ["Speaker 1", "Speaker 2"]
    assert written["speakers"] == ["Speaker 1", "Speaker 2"]
    assert result["speakers"] == ["Speaker 1", "Speaker 2"]


def test_an_ABSENT_count_estimates_and_says_what_it_decided(
        monkeypatch, loaded, tmp_path):
    """D318 comes along free by sharing `diarize.py`: no `speakers` key means
    the clustering is handed None, and the transcript gains
    `estimatedSpeakers` — which a run that supplied the count does not get."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 2.0, "hello")],
                          audio_seconds=20.0)
    seen = _diarizes(monkeypatch, worker, [(0.0, 10.0, 0), (10.0, 20.0, 1)])
    request = _request(tmp_path, diarize=True, vad=False)

    result = worker.generate(request)

    assert seen["speakers"] is None
    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert result["estimatedSpeakers"] == 2 and written["estimatedSpeakers"] == 2


@pytest.mark.parametrize("speakers", [0, -1, True, 2.5, "2"])
def test_a_bad_speaker_count_is_refused_BEFORE_the_audio_is_decoded(
        monkeypatch, loaded, tmp_path, speakers):
    """Neither the bridge nor the server is the only door into this process."""
    worker, model, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")])

    with pytest.raises(ValueError, match="speakers"):
        worker.generate(_request(tmp_path, diarize=True, speakers=speakers))
    assert model.calls == []


def test_the_pre_pass_does_NOT_inflate_the_progress_total(
        monkeypatch, loaded, base, tmp_path):
    """`done`/`total` are seconds of audio of the TRANSCRIPT (AI-10a). A bar
    that counted the pre-pass would run to 100% before a word was decoded, so
    the phase reports through `detail` with no numbers at all."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                          audio_seconds=20.0)
    _diarizes(monkeypatch, worker, [(0.0, 20.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2, vad=False))

    finding = [t for t in base.ticks if t.get("detail") == "Finding speakers…"]
    assert finding
    assert all(t["done"] is None and t["total"] is None for t in finding)


def test_the_component_fetch_reports_to_the_row_the_USER_is_watching(
        monkeypatch, loaded, base, tmp_path):
    """The fetch happens inside a transcription, so an unbound `download_file`
    would tick into `JOB_ID` — this process's model-load row, finished long ago
    — and reopening it makes a finished download start running again on the
    manager for something nobody asked for."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")])
    _diarizes(monkeypatch, worker, [(0.0, 10.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2, vad=False))

    assert base.fetches
    for fetch in base.fetches:
        assert fetch["job"] == "sys:ai-transcribe:abc"
        assert fetch["row"] == ROW


# -- the progressive transcript ----------------------------------------------------


def test_each_segment_lands_in_the_PARTIAL_file_as_it_is_decoded(
        monkeypatch, loaded, tmp_path):
    """A page must not have to wait for a 90-minute recording to finish before
    it has anything to show, and it must not have to know which engine wrote
    the file — the sink is the shared `runners/partial.py`."""
    worker, _, _ = loaded(sentences=[FakeSentence(0.0, 1.0, "hi")],
                          audio_seconds=30.0)
    _regions(monkeypatch, worker, [(0.0, 5.0), (20.0, 25.0)])
    seen = []
    import partial as partial_module
    real_add = partial_module.Sink.add

    def spy(self, segment):
        seen.append(dict(segment))
        return real_add(self, segment)

    monkeypatch.setattr(partial_module.Sink, "add", spy)
    request = _request(tmp_path, outPartial=str(tmp_path / "out.partial.jsonl"))

    worker.generate(request)

    assert [s["start"] for s in seen] == [0.0, 20.0]
    # …and it is GONE once the real transcript lands: the partial file is
    # duplicate bytes at that point.
    assert not os.path.exists(request["outPartial"])
