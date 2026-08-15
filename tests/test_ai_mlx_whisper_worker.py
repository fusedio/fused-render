"""The MLX transcription runner's own logic, driven directly (SPEC AI-10c).

`tests/test_ai_whisper_worker.py` is this file's sibling and its template: the
two runners serve one capability and must be indistinguishable to a page, so
the claims pinned there — progress in seconds of audio, a ✕ honoured
throughout, a row every tick can rebuild, the two output files — are pinned
here as well, against a backend that provides none of them for free.

Testable for the same reason that one is: the module is **stdlib-only at import
time**. `mlx_whisper`, `mlx.core`, `av` and `numpy` are imported inside the
functions that need them, so the whole flow can be driven with stubs standing
in for Metal and for ffmpeg. What is still NOT covered here is a real
transcription: no audio is decoded by a real ffmpeg and no weights are run on a
GPU. Those were verified by hand on an Apple Silicon machine against
mlx-whisper 0.4.3 — an `av` decode of a real recording feeding an ndarray
straight into `mlx_whisper.transcribe`, whose internal frame counter reported
seven windows over a 198-second file.
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
    "fused_render", "ai", "runners", "mlx_whisper", "worker.py",
)


class FakeBase:
    """A stand-in for `worker_base`, recording every tick.

    The whisper runner's test double, unchanged — the two runners report to the
    same job manager through the same contract, so a divergence here would be a
    divergence in what is being asserted.
    """

    class Cancelled(Exception):
        pass

    def __init__(self):
        self.ticks = []
        self.CANCEL = threading.Event()
        self.state = {}
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

    def serve(self, **kwargs):
        return None


# -- the fakes standing in for Metal and for ffmpeg ------------------------------


class FakeTranscribeModule(types.ModuleType):
    """`mlx_whisper.transcribe` — the MODULE, which in the real package is
    shadowed by a function of the same name (see `_transcribe_module`).

    Carries the two things the runner touches: `ModelHolder` (which owns the
    resident weights) and `tqdm` (the binding the progress hook borrows).
    """

    def __init__(self, windows=(), segments=(), language="en", seconds_per_window=0.0):
        super().__init__("mlx_whisper.transcribe")
        self.calls = []
        self.loads = []
        #: Frame counts the fake decoder will "finish", one per window.
        self.windows = list(windows)
        self.result_segments = list(segments)
        self.language = language
        self.seconds_per_window = seconds_per_window
        self.max_concurrent = 0
        self._live = 0
        self._lock = threading.Lock()

        holder = self

        class ModelHolder:
            @staticmethod
            def get_model(path, dtype):
                holder.loads.append((path, dtype))
                return "MODEL"

        self.ModelHolder = ModelHolder
        # The real module imports tqdm at the top; the hook only engages when
        # this attribute is present, so its presence/absence is a test axis.
        self.tqdm = types.SimpleNamespace(tqdm=object())

    def transcribe(self, audio, **kwargs):
        self.calls.append({"audio": audio, **kwargs})
        with self._lock:
            self._live += 1
            self.max_concurrent = max(self.max_concurrent, self._live)
        try:
            total_frames = int(round(len(audio) / 16000 * 100))
            # `_NoBar` stands in for a real tqdm: a version that no longer
            # exports the binding still HAS a progress bar of its own, it is
            # just not one this runner can borrow.
            factory = self.tqdm.tqdm if self.tqdm is not None else _NoBar
            with factory(total=total_frames, unit="frames",
                         disable=kwargs.get("verbose") is not False) as bar:
                for frames in self.windows:
                    time.sleep(self.seconds_per_window)
                    bar.update(frames)
            return {"text": " ".join(s["text"] for s in self.result_segments),
                    "segments": self.result_segments, "language": self.language}
        finally:
            with self._lock:
                self._live -= 1


class _NoBar:
    """A progress bar nothing can read — what the library keeps when the `tqdm`
    binding the hook borrows is not there to be swapped."""

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, n):
        pass


class FakeResampler:
    """`av.AudioResampler`'s shape: frames in, frames out, and a FLUSH.

    The flush is not decoration — the resampler buffers, so the tail of a
    recording is still inside the filter when the container runs out, and a
    runner that never calls `resample(None)` truncates every transcript by a
    fraction of a second.
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
    samples = np.zeros(int(16000 * seconds) - 160, dtype=np.float32)
    module = types.ModuleType("av")
    module.resamplers = []

    def _resampler(**kwargs):
        made = FakeResampler(**kwargs)
        module.resamplers.append(made)
        return made

    module.AudioResampler = _resampler
    module.open = lambda path: FakeContainer([FakeFrame(samples)], has_audio=has_audio)
    return module


def load_worker(monkeypatch, base, transcribe_module=None, av_module=None,
                mlx_core=None):
    """A fresh import of the MLX whisper worker, against the given fakes.

    By path and with `worker_base` primed in `sys.modules`, for the reason the
    runner exists: it loads its base off `sys.path` in its own interpreter, not
    as `fused_render.ai.runners.…`, so importing it the packaged way would be
    testing an import that never ships.

    The fakes go in through `monkeypatch.setitem` and therefore stay there for
    the whole test rather than only for the import: this runner imports
    `mlx_whisper`, `av` and `mlx.core` INSIDE the functions that need them (the
    property that makes it testable at all), so a stub withdrawn after the
    import would be gone by the time anything looked for it.
    """
    monkeypatch.setitem(sys.modules, "worker_base", base)
    if transcribe_module is not None:
        package = types.ModuleType("mlx_whisper")
        # The shadowing the real package does: `from .transcribe import
        # transcribe` makes the package attribute the FUNCTION, which is the
        # trap `_transcribe_module` exists to step around.
        package.transcribe = transcribe_module.transcribe
        monkeypatch.setitem(sys.modules, "mlx_whisper", package)
        monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", transcribe_module)
    if av_module is not None:
        monkeypatch.setitem(sys.modules, "av", av_module)
    if mlx_core is not None:
        mlx = types.ModuleType("mlx")
        mlx.core = mlx_core
        monkeypatch.setitem(sys.modules, "mlx", mlx)
        monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)
    spec = importlib.util.spec_from_file_location(
        "mlx_whisper_worker_under_test", WORKER_PATH)
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


def _segment(start, end, text):
    """A segment as mlx-whisper returns one — with the four extra fields the
    runner must DROP, so a page cannot come to depend on them."""
    return {"start": start, "end": end, "text": text, "tokens": [1, 2],
            "avg_logprob": -0.2, "no_speech_prob": 0.01, "temperature": 0.0,
            "seek": 0, "id": 0, "compression_ratio": 1.2}


@pytest.fixture()
def base():
    return FakeBase()


@pytest.fixture()
def loaded(monkeypatch, base, tmp_path):
    """The worker with a model 'resident' and a 20-second recording to decode."""
    def build(windows=(1000, 1000), segments=(), seconds_per_window=0.0,
              audio_seconds=20.0, **kwargs):
        transcribe = FakeTranscribeModule(
            windows=windows, segments=list(segments),
            seconds_per_window=seconds_per_window)
        worker = load_worker(monkeypatch, base, transcribe_module=transcribe,
                             av_module=make_av(seconds=audio_seconds, **kwargs))
        worker._loaded["path"] = str(tmp_path / "snap")
        worker._TICK_S = 0.02
        return worker, transcribe

    return build


# -- the progress contract, which is a PUBLIC promise ---------------------------


def test_progress_is_SECONDS_OF_AUDIO_like_the_other_whisper_runner(
        loaded, base, tmp_path):
    """SPEC AI-10a and `runtime.js` both promise `onProgress` gives done/total
    in seconds of audio, and faster-whisper delivers that for free by yielding
    segments. `mlx_whisper.transcribe()` is ONE blocking call, so the naive
    port reports 0 for the length of the recording and then jumps to done — a
    page written against the documented contract draws a frozen bar for
    eighteen minutes. The library's own window counter is borrowed instead.
    """
    worker, _ = loaded(windows=(500, 700, 300), seconds_per_window=0.1,
                       audio_seconds=20.0)

    worker.generate(_request(tmp_path))

    progress = [t for t in base.ticks if t.get("done") and t.get("unit") == "s"]
    assert progress, base.ticks
    dones = [t["done"] for t in progress]
    # 100 frames per second of audio, so the windows land at 5s, 12s and 15s of
    # a 20-second recording. WHICH of those a tick sees depends on when it
    # fires — this is a sampled bar, not an event stream — so the claim is that
    # every position reported is a real one, that more than one is seen, and
    # that the bar only ever moves forwards.
    assert set(dones) <= {5.0, 12.0, 15.0}, dones
    assert len(set(dones)) >= 2, dones
    assert dones == sorted(dones), dones
    assert {t["total"] for t in progress} == {20.0}


def test_the_total_is_the_audio_this_process_decoded(loaded, base, tmp_path):
    """Unlike faster-whisper there is no `info.duration` to read — but this
    runner holds the samples, so the duration is exact and is known BEFORE the
    model sees anything, which is what lets the first transcribing tick carry a
    total at all."""
    worker, _ = loaded(windows=(100,), seconds_per_window=0.08, audio_seconds=42.0)

    result = worker.generate(_request(tmp_path))

    assert result["duration"] == 42.0


def test_with_no_counter_to_borrow_the_bar_is_INDETERMINATE_not_invented(
        loaded, base, tmp_path):
    """The hook reaches into another package's module globals, so it may not be
    there in a future mlx-whisper. That must cost the resolution and nothing
    else: the transcription still runs, and the ticks carry no numbers rather
    than a percentage nobody measured."""
    worker, transcribe = loaded(windows=(500, 500), seconds_per_window=0.08)
    transcribe.tqdm = None  # a version that no longer keeps a progress bar

    result = worker.generate(_request(tmp_path))

    transcribing = [t for t in base.ticks if "Transcribing" in str(t.get("detail"))]
    assert transcribing, base.ticks
    assert all(t["done"] is None and t["total"] is None for t in transcribing)
    assert result["duration"] == 20.0  # the run itself is unaffected


def test_the_borrowed_binding_is_put_back(loaded, base, tmp_path):
    """It is a process-global in somebody else's module. A `_Ticker` left
    behind would be handed to the next decode with a progress slot belonging to
    a request that is over."""
    worker, transcribe = loaded(windows=(500,))
    original = transcribe.tqdm

    worker.generate(_request(tmp_path))

    assert transcribe.tqdm is original


def test_the_borrowed_binding_is_put_back_even_on_a_CANCEL(loaded, base, tmp_path):
    worker, transcribe = loaded(windows=(500, 500), seconds_per_window=0.15)
    original = transcribe.tqdm
    base.cancel_on_tick = 3

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))

    assert transcribe.tqdm is original


def test_the_decode_is_asked_to_transcribe_a_WAVEFORM_not_a_path(
        loaded, base, tmp_path):
    """The single most important constraint in this runner: this app bundles
    rclone, not ffmpeg, and `mlx_whisper.transcribe(path)` calls
    openai-whisper's `load_audio()`, which spawns `ffmpeg`. Passing the
    waveform is what keeps the runner working on a machine that is not the one
    it was written on."""
    worker, transcribe = loaded(windows=(500,), audio_seconds=3.0)

    worker.generate(_request(tmp_path))

    audio = transcribe.calls[0]["audio"]
    assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
    assert not isinstance(audio, str)
    # 3 seconds at 16 kHz, plus the flushed tail.
    assert abs(len(audio) - 3 * 16000) < 400


def test_the_worker_never_shells_out():
    """The rule `faster_whisper/pyproject.toml` states, pinned rather than
    trusted: a `subprocess` call to a binary the app does not ship works on the
    machine it was written on and fails on a user's."""
    source = open(WORKER_PATH, encoding="utf-8").read()
    # The CALL, not the word: the module docstring and the folder's
    # `pyproject.toml` both explain at length why there is no subprocess here,
    # and a test forbidding the word would forbid the explanation.
    assert "import subprocess" not in source
    assert "subprocess." not in source
    assert "os.system" not in source and "Popen" not in source


# -- decoding audio in this process --------------------------------------------


def test_the_resampler_is_flushed_and_asks_for_16k_MONO_float(
        monkeypatch, base, tmp_path):
    """Four ways to be silently wrong, none of which raises.

    `fltp` because int16 read as float is white noise at full scale; `mono`
    because a stereo interview arriving interleaved reads as speech at double
    speed; 16 kHz because Whisper's mel front end assumes it and anything else
    transcribes a chipmunk. And the FLUSH, because the resampler buffers — the
    tail of the recording is still inside the filter when the container runs
    out, so dropping `resample(None)` truncates every transcript by a fraction
    of a second, invisible until it eats a word.
    """
    av_module = make_av(seconds=5.0)
    transcribe = FakeTranscribeModule(windows=(100,))
    worker = load_worker(monkeypatch, base, transcribe_module=transcribe, av_module=av_module)
    worker._loaded["path"] = str(tmp_path / "snap")
    worker._TICK_S = 0.02

    worker.generate(_request(tmp_path))

    assert av_module.resamplers, "no resampler was built"
    resampler = av_module.resamplers[0]
    assert resampler.flushed, "the tail of the recording was left in the filter"
    assert resampler.args == {"format": "fltp", "layout": "mono", "rate": 16000}


def test_a_file_with_no_audio_track_says_so(monkeypatch, base, tmp_path):
    """PyAV's own answer is an IndexError, which reaches the job row as "list
    index out of range" — a sentence with nothing in it for the user who
    dropped a silent screen recording on the page."""
    transcribe = FakeTranscribeModule(windows=())
    worker = load_worker(monkeypatch, base, transcribe_module=transcribe,
                         av_module=make_av(has_audio=False))
    worker._loaded["path"] = str(tmp_path / "snap")

    with pytest.raises(RuntimeError) as caught:
        worker.generate(_request(tmp_path))
    assert "no audio track" in str(caught.value)


def test_the_audio_decode_phase_ticks_while_it_runs(monkeypatch, base, tmp_path):
    """It is the one phase with no hook inside it, and on a 90-minute recording
    it is a real wait — so it ticks without numbers rather than sitting
    silent, which is also what keeps the ✕ answerable through it."""
    slow = make_av(seconds=4.0)
    real_open = slow.open

    def _slow_open(path):
        time.sleep(0.25)
        return real_open(path)

    slow.open = _slow_open
    transcribe = FakeTranscribeModule(windows=(100,))
    worker = load_worker(monkeypatch, base, transcribe_module=transcribe, av_module=slow)
    worker._loaded["path"] = str(tmp_path / "snap")
    worker._TICK_S = 0.05

    worker.generate(_request(tmp_path))

    decoding = [t for t in base.ticks if "Decoding" in str(t.get("detail"))]
    assert len(decoding) > 2, base.ticks
    assert all(t["done"] is None for t in decoding[1:])


# -- cancelling ------------------------------------------------------------------


def test_a_cancel_while_transcribing_is_honoured_and_writes_nothing(
        loaded, base, tmp_path):
    """A blocking call returns the whole transcript at once, so a ✕ before it
    returns means there is nothing on disk to save — unlike the CT2 runner,
    where a cancel on the last segment still writes a finished transcript."""
    worker, _ = loaded(windows=(500, 500, 500), seconds_per_window=0.15)
    request = _request(tmp_path)
    base.cancel_on_tick = 3

    with pytest.raises(base.Cancelled):
        worker.generate(request)

    assert not os.path.exists(request["out"])
    assert not os.path.exists(request["outText"])


def test_a_cancel_STOPS_the_decode_rather_than_letting_it_run_the_file_out(
        loaded, base, tmp_path):
    """The improvement over the CT2 runner, which can only abandon its eager
    phase and wait. The progress hook is inside the library's window loop, so a
    cancelled `transcribe()` raises at the next window instead of decoding a
    90-minute recording nobody is waiting for."""
    worker, transcribe = loaded(windows=(100,) * 200, seconds_per_window=0.02)
    base.cancel_on_tick = 3

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))

    # It stopped early: nothing like all 200 windows were decoded.
    deadline = time.time() + 5
    while worker._orphan["thread"] is not None and \
            worker._orphan["thread"].is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert not worker._orphan["thread"].is_alive(), "the abandoned decode ran on"


def test_a_cancelled_decode_is_WAITED_FOR_before_the_next_one_starts(
        loaded, base, tmp_path):
    """A cancel unwinds the handler, not the work. Press ✕ and re-submit and two
    decodes would run at once on one process and one model — exactly what
    `GENERATE_LOCK` exists to prevent, and it is released the moment the handler
    replies."""
    worker, transcribe = loaded(windows=(100,) * 50, seconds_per_window=0.02)
    base.cancel_on_tick = 3

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))

    base.cancel_on_tick = None
    worker.generate(_request(tmp_path, out=str(tmp_path / "b.json"),
                             outText=str(tmp_path / "b.txt")))

    assert transcribe.max_concurrent == 1, "two decodes ran on one model at once"


def test_a_WEDGED_abandoned_decode_is_refused_rather_than_waited_on_forever(
        monkeypatch, base, tmp_path):
    """"Bounded by the file" is only true of a decoder that returns. An
    unbounded join blocks every later transcription for the life of the
    process, with unloading the model the only escape — a hang with a spinner,
    which is the worst failure available."""
    stuck = threading.Event()
    entered = threading.Event()

    transcribe = FakeTranscribeModule(windows=())

    def _wedged(audio, **kwargs):
        transcribe.calls.append({"audio": audio, **kwargs})
        entered.set()
        stuck.wait(30)
        return {"text": "", "segments": [], "language": "en"}

    transcribe.transcribe = _wedged
    worker = load_worker(monkeypatch, base, transcribe_module=transcribe, av_module=make_av())
    worker._loaded["path"] = str(tmp_path / "snap")
    worker._TICK_S = 0.02
    worker._ORPHAN_WAIT_S = 0.2
    base.cancel_on_tick = 3

    try:
        with pytest.raises(base.Cancelled):
            worker.generate(_request(tmp_path))
        assert entered.wait(5)

        base.cancel_on_tick = None
        with pytest.raises(RuntimeError) as caught:
            worker.generate(_request(tmp_path, out=str(tmp_path / "b.json"),
                                     outText=str(tmp_path / "b.txt")))
        message = str(caught.value)
        assert "cancelled" in message and "unload" in message.lower()
        assert len(transcribe.calls) == 1, "a second decode started on one model"
    finally:
        stuck.set()
        worker._orphan["thread"] = None


# -- what the row is told --------------------------------------------------------


def test_EVERY_tick_can_rebuild_the_row_it_reports_to(loaded, base, tmp_path):
    """The job manager evicts the least recently updated running row once
    `MAX_JOBS` bites, and a transcription QUEUE is what pushes the count past
    it — so any tick can be the one that has to re-create the row. A tick
    without `title` is refused outright, which kills the row permanently: the ✕
    goes dead and the page is told a run that succeeds minutes later failed."""
    worker, _ = loaded(windows=(500, 500), seconds_per_window=0.1)

    worker.generate(_request(tmp_path))

    assert len(base.ticks) >= 3, base.ticks
    for tick in base.ticks:
        missing = [k for k, v in ROW.items() if tick.get(k) != v]
        assert not missing, f"tick cannot rebuild its row, missing {missing}: {tick}"
        assert tick.get("state") == "running", tick


def test_every_tick_carries_the_job_the_route_opened(loaded, base, tmp_path):
    worker, _ = loaded(windows=(100,))
    worker.generate(_request(tmp_path, job="sys:ai-transcribe:zzz"))
    assert {t["job"] for t in base.ticks} == {"sys:ai-transcribe:zzz"}


def test_the_clock_agrees_with_the_other_runners(loaded):
    """Two formatters encoding one rule have to encode all of it — including
    the hours field and the exact half where Python's banker's `round` and
    JavaScript's `Math.round` disagree. Same cases as
    `test_ai_whisper_worker.py`, because the manager renders both runners' rows
    with the same JavaScript."""
    worker, _ = loaded()
    assert worker._clock(9) == "0:09"
    assert worker._clock(185) == "3:05"
    assert worker._clock(5400) == "1:30:00"
    assert worker._clock(89.6) == "1:30"
    assert worker._clock(88.5) == "1:29"
    assert worker._clock(89.5) == "1:30"


# -- what lands on disk ----------------------------------------------------------


def test_the_result_is_the_SAME_SHAPE_the_other_whisper_runner_produces(
        loaded, base, tmp_path):
    """A page must not be able to tell which backend transcribed for it. The
    library's segments carry tokens, logprobs and temperatures; only the three
    fields the CT2 runner publishes survive, so nothing can come to depend on
    a field one runner has and the other does not."""
    worker, _ = loaded(windows=(300,), audio_seconds=3.0,
                       segments=[_segment(0.0, 1.5, " hello"),
                                 _segment(1.5, 3.0, " world ")])
    request = _request(tmp_path)

    result = worker.generate(request)

    written = json.load(open(request["out"], encoding="utf-8"))
    assert written["text"] == "hello world"
    assert written["segments"] == [
        {"start": 0.0, "end": 1.5, "text": "hello"},
        {"start": 1.5, "end": 3.0, "text": "world"},
    ]
    assert written["language"] == "en" and written["duration"] == 3.0
    assert open(request["outText"], encoding="utf-8").read() == "hello world\n"
    # The reply COUNTS the segments rather than carrying them.
    assert result["segments"] == 2
    assert set(result) == {"path", "output", "outputText", "model", "task",
                           "language", "duration", "seconds", "segments"}


def test_a_recording_with_no_speech_writes_an_empty_transcript(loaded, tmp_path):
    """The honest answer to "what did this recording say"; an error would send
    the user hunting for a fault that is not there."""
    worker, _ = loaded(windows=(100,), audio_seconds=42.0, segments=[])
    request = _request(tmp_path)

    result = worker.generate(request)

    assert result["segments"] == 0 and result["duration"] == 42.0
    assert json.load(open(request["out"], encoding="utf-8"))["text"] == ""
    assert open(request["outText"], encoding="utf-8").read() == "\n"


def test_the_two_whisper_directions_reach_the_model(loaded, tmp_path):
    worker, transcribe = loaded(windows=(100,))
    worker.generate(_request(tmp_path, task="translate", language="fr"))
    assert transcribe.calls[0]["task"] == "translate"
    assert transcribe.calls[0]["language"] == "fr"


def test_an_absent_language_means_auto_detect_not_an_empty_code(loaded, tmp_path):
    worker, transcribe = loaded(windows=(100,))
    worker.generate(_request(tmp_path, language=""))
    assert transcribe.calls[0]["language"] is None


def test_vad_false_is_HONOURED_as_the_no_speech_threshold(loaded, tmp_path):
    """mlx-whisper has no separate VAD filter to switch off — silence handling
    IS `no_speech_threshold`, applied per window. So the flag maps onto it
    rather than being accepted and ignored, which would answer a request the
    caller made with silence."""
    worker, transcribe = loaded(windows=(100,))
    worker.generate(_request(tmp_path, vad=False))
    assert transcribe.calls[0]["no_speech_threshold"] is None

    worker, transcribe = loaded(windows=(100,))
    worker.generate(_request(tmp_path, vad=True))
    assert "no_speech_threshold" not in transcribe.calls[0]

    # An explicit JSON null is "not specified", never False — the inversion the
    # CT2 runner shipped once.
    worker, transcribe = loaded(windows=(100,))
    worker.generate(_request(tmp_path, vad=None))
    assert "no_speech_threshold" not in transcribe.calls[0]


def test_the_library_is_told_to_be_QUIET_and_to_keep_its_bar(loaded, tmp_path):
    """`verbose=False` is load-bearing twice: `True` prints the whole
    transcript into the worker log, and the library only CONSTRUCTS the
    progress bar when verbose is False (`disable=verbose is not False`) — with
    the default `None` there would be nothing to borrow."""
    worker, transcribe = loaded(windows=(100,))
    worker.generate(_request(tmp_path))
    assert transcribe.calls[0]["verbose"] is False


def test_generating_with_no_model_loaded_says_so(monkeypatch, base, tmp_path):
    worker = load_worker(monkeypatch, base, transcribe_module=FakeTranscribeModule(),
                         av_module=make_av())
    with pytest.raises(RuntimeError):
        worker.generate(_request(tmp_path))


# -- loading ---------------------------------------------------------------------


def test_loading_PRIMES_the_librarys_holder_rather_than_keeping_a_handle(
        monkeypatch, base, tmp_path):
    """`mlx_whisper` keeps its model in a module-level holder keyed by path, and
    `transcribe()` looks it up there. Priming it is what makes the weights
    resident at load time instead of inside the first request, having already
    reported ready — and the dtype must be the one `transcribe()` derives from
    its own `fp16` default, or the model is silently loaded twice and costs the
    memory of both."""
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "weights.npz").write_bytes(b"")
    transcribe = FakeTranscribeModule()
    mlx_core = types.SimpleNamespace(float16="FLOAT16", get_active_memory=lambda: 0)
    worker = load_worker(monkeypatch, base, transcribe_module=transcribe, mlx_core=mlx_core)

    worker.load("mlx-community/whisper-large-v3-turbo", str(snapshot))

    assert transcribe.loads == [(str(snapshot), "FLOAT16")]
    assert worker._loaded["path"] == str(snapshot)
    # The device is reported rather than deduced by the supervisor: it is the
    # answer to "why is this so much faster than it was".
    assert base.state == {"device": "mps"}


@pytest.mark.parametrize("weights", ["weights.npz", "weights.safetensors"])
def test_either_MLX_weight_spelling_loads(monkeypatch, base, tmp_path, weights):
    """`.npz` is what mlx-community publishes today; `load_models` prefers
    `.safetensors` when it is there. Requiring the older spelling would refuse
    a re-upload that works."""
    snapshot = tmp_path / weights.replace(".", "_")
    snapshot.mkdir()
    (snapshot / weights).write_bytes(b"")
    transcribe = FakeTranscribeModule()
    mlx_core = types.SimpleNamespace(float16="FLOAT16")
    worker = load_worker(monkeypatch, base, transcribe_module=transcribe, mlx_core=mlx_core)

    worker.load("mlx-community/whisper-medium-mlx", str(snapshot))

    assert transcribe.loads


def test_the_other_two_whisper_formats_are_named_as_the_cause(monkeypatch, base, tmp_path):
    """There are now THREE incompatible Whisper formats in this app — CT2, MLX
    and transformers — and the AI Models page offers Load on anything whose
    task label says "speech recognition", because the format is not in the
    label. This message is the only thing between a user and a search engine.

    The check runs BEFORE `mlx_whisper` is imported, so the explanation does not
    depend on the runner environment being importable here.
    """
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "model.bin").write_bytes(b"")  # a CTranslate2 conversion
    worker = load_worker(monkeypatch, base)  # no mlx_whisper in sys.modules at all

    with pytest.raises(RuntimeError) as caught:
        worker.load("Systran/faster-whisper-medium", str(snapshot))
    message = str(caught.value)
    assert "weights.npz" in message
    assert "model.bin" in message and "model.safetensors" in message
    assert "mlx-community/whisper-large-v3-turbo" in message


def test_memory_is_MLXs_accounting_and_not_RSS(monkeypatch, base, tmp_path):
    """MLX memory-maps its weights and its arrays are lazy, so RSS right after
    a load reports the interpreter and not the model — and Metal's unified
    memory makes that worse rather than better, since the buffers are real and
    RSS still cannot see them. Without this the AI Models page reports a
    resident Whisper model as costing nothing."""
    mlx_core = types.SimpleNamespace(get_active_memory=lambda: 1_234_567_890)
    worker = load_worker(monkeypatch, base, mlx_core=mlx_core)
    assert worker.memory() == 1_234_567_890


def test_memory_falls_back_to_the_OLD_mlx_spelling(monkeypatch, base):
    """`get_active_memory` moved out of `mlx.core.metal` into `mlx.core`. A
    version skew should cost the better number, not raise inside `/health`."""
    mlx_core = types.ModuleType("mlx.core")
    mlx_core.metal = types.SimpleNamespace(get_active_memory=lambda: 42)
    worker = load_worker(monkeypatch, base, mlx_core=mlx_core)
    assert worker.memory() == 42


def test_memory_answers_None_rather_than_raising(monkeypatch, base):
    """A memory probe must never break `/health` — a worker that cannot say
    what it costs is still a worker that is answering."""
    mlx_core = types.SimpleNamespace(get_active_memory=lambda: 0)
    worker = load_worker(monkeypatch, base, mlx_core=mlx_core)
    assert worker.memory() is None
