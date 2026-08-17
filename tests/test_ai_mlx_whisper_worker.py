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
        #: Every `download_file` call, with the row it was told to report to.
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
        # Present because `_speech_regions` reads it off the base to hand to
        # `vad.model_path`, and a base that lacks it makes every VAD test fail
        # as an AttributeError from a line that is not what is under test.
        #
        # `job`/`row` are recorded rather than ignored: a component fetched
        # DURING a transcription must tick into the row the user is watching,
        # not into `JOB_ID` — which is this process's model-load row, finished
        # long ago, and which reopening is how a finished download starts
        # running again on the manager for something nobody asked for.
        self.fetches.append({"repo": repo_id, "file": filename,
                             "job": job, "row": row})
        # The real `fetch_with_progress` leaves the row as a FINISHED DOWNLOAD:
        # `kind="download"`, `unit="bytes"`, `done == total`. Modelled here
        # rather than stubbed away, because the row state a fetch leaves behind
        # is the whole of what the caller then has to restore — a double that
        # silently returned a path made that failure invisible to every test.
        self.report(job=job, **{**(row or {}), "kind": "download",
                                "unit": "bytes"},
                    state="running", done=2_217_492, total=2_217_492,
                    detail=detail or f"Fetching {filename}…")
        return f"/snapshots/{repo_id}/{filename}"

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
        #: Set when `transcribe` RETURNS, so a test can land a cancel in the
        #: window between the work finishing and the tick that carries the ✕
        #: being answered — the race the last-second guard is about.
        self.done = threading.Event()
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
            self.done.set()


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


class FakeMlxCore(types.ModuleType):
    """`mlx.core` as this runner uses it: a dtype, a device, and STREAMS.

    Streams are the part with teeth. From mlx 0.32 they are per-thread, and an
    array evaluated off the thread that made it aborts the process rather than
    raising — so the runner pins one shared stream on every thread that touches
    MLX. This double records who pinned what, and from which thread, which is
    the only way to assert that from outside.
    """

    def __init__(self, **extra):
        super().__init__("mlx.core")
        self.float16 = "FLOAT16"
        self.made = []
        #: (thread name, stream) for every `set_default_stream`.
        self.pinned = []
        self._lock = threading.Lock()
        for name, value in extra.items():
            setattr(self, name, value)

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
    # `mlx.core` is no longer only the loader's business: every decode pins the
    # shared stream on its own thread (`_pin_stream`), so a transcription test
    # that left it out would be testing an import that cannot happen in
    # production. A caller may still hand in its own — a namespace missing
    # `get_active_memory`, say — and gets exactly that.
    if mlx_core is None:
        mlx_core = FakeMlxCore()
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


def test_a_cancel_that_lands_as_the_transcript_FINISHES_still_writes_it(
        monkeypatch, loaded, base, tmp_path):
    """The race, driven directly: the ✕ arrives during the very tick that
    carries it, while `transcribe()` is returning.

    Liveness is read before the report and the report is a round trip to the
    server — up to `JOB_TIMEOUT_S`, 3 seconds — so a transcription that finished
    inside that window used to take the cancel branch anyway: it orphaned a
    thread that had already stopped and raised with the finished transcript in
    hand and nothing on disk. An hour of decoding discarded at 99%, which is the
    failure the CT2 runner documents on its last SEGMENT, reached here through
    the last REPORT.

    The double `is_alive()` — before the report, and again after it — is what
    tells "still working" from "finished while we were asking", so the fake
    tick below finishes the work IN THE MIDDLE of the report, which is the only
    way to exercise the guard rather than the clean-cancel path beside it.
    """
    worker, transcribe = loaded(windows=(500,), seconds_per_window=0.05,
                                segments=[_segment(0.0, 1.5, "hello"),
                                          _segment(1.5, 3.0, "world")])
    request = _request(tmp_path)

    # The tick that carries the ✕ waits for the work to finish before answering
    # — which is what a slow round trip does by accident, and the only way to
    # land a cancel in the window this guard is about.
    real_report = base.report_or_cancel

    def slow_tick(job=None, **fields):
        if "Transcribing" in str(fields.get("detail") or ""):
            assert transcribe.done.wait(5), "the fake decode never finished"
            base.cancel_on_tick = len(base.ticks) + 1
        return real_report(job=job, **fields)

    monkeypatch.setattr(base, "report_or_cancel", slow_tick)

    result = worker.generate(request)

    # The transcript survived, in both files, exactly as an uncancelled run.
    assert result["segments"] == 2
    assert json.load(open(request["out"], encoding="utf-8"))["text"] == "hello world"
    assert open(request["outText"], encoding="utf-8").read() == "hello world\n"
    # And nothing was orphaned: there was never a live thread to abandon, and a
    # phantom orphan would make the NEXT transcription wait on it.
    assert worker._orphan["thread"] is None


def test_a_cancel_as_the_AUDIO_DECODE_finishes_is_still_honoured(
        monkeypatch, base, tmp_path):
    """The other side of the same guard, and the reason the decision belongs to
    the CALLER rather than to `_call_with_ticks`.

    A ✕ landing as the decode returns has lost nothing — but the transcription
    has not started, and that is all of the work. So the salvage is refused
    here: a cancel is worth honouring exactly while there is work left to stop,
    and letting a completed decode carry on into a run the user asked not to
    have would be the rule upside down.
    """
    av_module = make_av(seconds=5.0)
    real_open = av_module.open

    def _slow_open(path):
        time.sleep(0.15)
        return real_open(path)

    av_module.open = _slow_open
    transcribe = FakeTranscribeModule(windows=(500,))
    worker = load_worker(monkeypatch, base, transcribe_module=transcribe,
                         av_module=av_module)
    worker._loaded["path"] = str(tmp_path / "snap")
    worker._TICK_S = 0.02
    request = _request(tmp_path)

    # The tick answers only once the decode has finished — the same window the
    # test above exercises, on the phase where the answer must be different.
    real_report = base.report_or_cancel

    def slow_tick(job=None, **fields):
        if "Decoding" in str(fields.get("detail") or ""):
            time.sleep(0.3)
            base.cancel_on_tick = len(base.ticks) + 1
        return real_report(job=job, **fields)

    monkeypatch.setattr(base, "report_or_cancel", slow_tick)

    with pytest.raises(base.Cancelled):
        worker.generate(request)
    assert not transcribe.calls, "a cancelled run still went on to transcribe"
    assert not os.path.exists(request["out"])


def test_work_that_FAILED_as_the_cancel_landed_stays_cancelled(
        monkeypatch, base, tmp_path):
    """Only a VALUE is salvaged. Work that finished by RAISING has nothing worth
    keeping, so the ✕ is the outcome — reporting the failure of a run the user
    abandoned sends them looking for a fault that does not matter."""
    av_module = make_av(seconds=5.0)

    def _explode(path):
        time.sleep(0.15)
        raise RuntimeError("moov atom not found")

    av_module.open = _explode
    transcribe = FakeTranscribeModule(windows=(500,))
    worker = load_worker(monkeypatch, base, transcribe_module=transcribe,
                         av_module=av_module)
    worker._loaded["path"] = str(tmp_path / "snap")
    worker._TICK_S = 0.02

    real_report = base.report_or_cancel

    def slow_tick(job=None, **fields):
        if "Decoding" in str(fields.get("detail") or ""):
            time.sleep(0.3)  # the decode raises during this
            base.cancel_on_tick = len(base.ticks) + 1
        return real_report(job=job, **fields)

    monkeypatch.setattr(base, "report_or_cancel", slow_tick)

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))


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
        # A fetch tick deliberately overrides `kind`/`unit` — for the length of
        # a download the row IS one, and bytes are what a person wants to see
        # (`worker_base.fetch_with_progress`). The IDENTITY half is not
        # negotiable on any tick: without `title` the row is not re-created at
        # all, and without `cancellable` it comes back with a dead ✕.
        expected = dict(ROW)
        if tick.get("kind") == "download":
            expected.pop("kind")
            expected.pop("unit")
            assert tick.get("unit") == "bytes", tick
        missing = [k for k, v in expected.items() if tick.get(k) != v]
        assert not missing, f"tick cannot rebuild its row, missing {missing}: {tick}"
        assert tick.get("state") == "running", tick


def test_every_tick_carries_the_job_the_route_opened(loaded, base, tmp_path):
    worker, _ = loaded(windows=(100,))
    worker.generate(_request(tmp_path, job="sys:ai-transcribe:zzz"))
    assert {t["job"] for t in base.ticks} == {"sys:ai-transcribe:zzz"}


# -- speaker labels -------------------------------------------------------------
#
# The diarization itself is `tests/test_ai_diarize.py`'s — the shared module
# both engines import, and the only place the models, the labels and the
# overlap arithmetic exist. What is driven HERE is this runner's WIRING: that
# the pre-pass sees the whole waveform rather than the VAD's regions, that it
# does not touch the progress contract, that the output is additive, and that
# the component fetch reports to the row the user is watching.


def _diarizes(monkeypatch, worker, turns):
    """Stand in for the whole pipeline, with no sherpa-onnx and no models.

    `model_paths` is left REAL, because the row it reports to is one of the
    things under test — only the ONNX-needing halves are replaced."""
    import diarize as diarize_module

    monkeypatch.setattr(diarize_module, "diarizer",
                        lambda seg, emb, speakers: {"speakers": speakers})
    monkeypatch.setattr(diarize_module, "speaker_turns",
                        lambda audio, session, rate, **kw: list(turns))


def test_diarization_is_OFF_unless_asked_for(monkeypatch, loaded, tmp_path):
    """Additive or it is a breaking change to every page already transcribing:
    no `speaker` on a segment, no `speakers` in the JSON, and no 33MB fetch."""
    worker, _ = loaded(windows=(100,), segments=[_segment(0.0, 1.0, "hi")])
    _diarizes(monkeypatch, worker, [(0.0, 10.0, 0)])

    result = worker.generate(_request(tmp_path))

    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    assert "speakers" not in written and "speakers" not in result
    assert "speaker" not in written["segments"][0]


def test_the_speaker_PRE_PASS_sees_the_whole_waveform_not_the_vad_regions(
        monkeypatch, loaded, tmp_path):
    """Independent of the VAD by design: the segmenter finds its own silence
    and is better at it, and feeding it regions cut for a different purpose
    would cluster voices across those cuts."""
    worker, _ = loaded(windows=(100,), audio_seconds=30.0)
    _regions(monkeypatch, worker, [(0.0, 5.0), (20.0, 25.0)])
    seen = {}

    import diarize as diarize_module
    monkeypatch.setattr(diarize_module, "diarizer", lambda *a: object())
    monkeypatch.setattr(diarize_module, "speaker_turns",
                        lambda audio, session, rate, **kw: seen.update(
                            samples=len(audio), rate=rate) or [(0.0, 30.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2))

    assert seen["samples"] == 30 * 16000, seen
    assert seen["rate"] == worker.SAMPLE_RATE


def test_the_speaker_count_reaches_the_clustering(monkeypatch, loaded, tmp_path):
    worker, _ = loaded(windows=(100,))
    seen = {}
    import diarize as diarize_module
    monkeypatch.setattr(diarize_module, "diarizer",
                        lambda seg, emb, speakers: seen.update(speakers=speakers))
    monkeypatch.setattr(diarize_module, "speaker_turns",
                        lambda *a, **k: [(0.0, 10.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=4))
    assert seen == {"speakers": 4}


def test_an_ABSENT_count_reaches_the_clustering_as_None_to_estimate(
        monkeypatch, loaded, tmp_path):
    """D318: the request simply has no `speakers` key, and what the clustering
    is handed is None — which `diarize.diarizer` turns into threshold
    clustering. The worker does not invent a number of its own on the way."""
    worker, _ = loaded(windows=(100,))
    seen = {}
    import diarize as diarize_module
    monkeypatch.setattr(diarize_module, "diarizer",
                        lambda seg, emb, speakers: seen.update(speakers=speakers))
    monkeypatch.setattr(diarize_module, "speaker_turns",
                        lambda *a, **k: [(0.0, 10.0, 0)])

    worker.generate(_request(tmp_path, diarize=True))
    assert seen == {"speakers": None}


@pytest.mark.parametrize("speakers", [0, -1, True, 2.5, "2"])
def test_a_bad_speaker_count_is_refused_BEFORE_the_audio_is_decoded(
        monkeypatch, loaded, tmp_path, speakers):
    """The bridge and the server refuse it first, but neither is the only door
    into this process — and a refusal that arrives after ninety seconds of `av`
    is a refusal the user paid for. `None` is NOT in this list since D318: it
    is the estimating path, not a typo."""
    worker, transcribe = loaded(windows=(100,))
    with pytest.raises(ValueError, match="speakers"):
        worker.generate(_request(tmp_path, diarize=True, speakers=speakers))
    assert transcribe.calls == []


def test_an_ESTIMATED_count_is_reported_and_a_GIVEN_one_is_not(
        monkeypatch, loaded, tmp_path):
    """`estimatedSpeakers` means "worked out", not "resolved" (D318): a run
    that supplied the count already knows it and its transcript keeps exactly
    the bytes it had before estimation existed.

    The estimate is the SEGMENTER's count, so it can exceed the legend — here
    three voices were clustered and only two of them said anything Whisper
    transcribed."""
    worker, _ = loaded(windows=(100,), audio_seconds=20.0,
                       segments=[_segment(0.0, 2.0, "hello"),
                                 _segment(12.0, 14.0, "hi there")])
    turns = [(0.0, 10.0, 0), (10.0, 20.0, 1), (20.0, 25.0, 2)]

    _diarizes(monkeypatch, worker, turns)
    request = _request(tmp_path, diarize=True, vad=False)
    result = worker.generate(request)
    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert result["estimatedSpeakers"] == 3
    assert written["estimatedSpeakers"] == 3
    assert written["speakers"] == ["Speaker 1", "Speaker 2"]

    _diarizes(monkeypatch, worker, turns)
    request = _request(tmp_path, diarize=True, speakers=3, vad=False)
    result = worker.generate(request)
    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert "estimatedSpeakers" not in result
    assert "estimatedSpeakers" not in written


def test_every_segment_is_LABELLED_and_the_json_gains_the_legend(
        monkeypatch, loaded, tmp_path):
    worker, _ = loaded(windows=(100,), audio_seconds=20.0,
                       segments=[_segment(0.0, 2.0, "hello"),
                                 _segment(12.0, 14.0, "hi there")])
    _diarizes(monkeypatch, worker, [(0.0, 10.0, 0), (10.0, 20.0, 1)])
    request = _request(tmp_path, diarize=True, speakers=2, vad=False)

    result = worker.generate(request)

    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert [s["speaker"] for s in written["segments"]] == ["Speaker 1", "Speaker 2"]
    assert written["speakers"] == ["Speaker 1", "Speaker 2"]
    # …and the reply carries the legend too, so a caller that never reads the
    # file still knows who was in the recording.
    assert result["speakers"] == ["Speaker 1", "Speaker 2"]


def test_the_pre_pass_does_NOT_inflate_the_progress_total(
        monkeypatch, loaded, tmp_path, base):
    """`done`/`total` are SECONDS OF AUDIO of the TRANSCRIPT (AI-10a, and
    `runtime.js` promises it to pages). Diarization is a fast pre-pass over the
    same recording, so a bar that counted it would either run to 100% before a
    word was decoded or report a total longer than the file."""
    worker, _ = loaded(windows=(500, 500), seconds_per_window=0.1,
                       audio_seconds=20.0)
    _diarizes(monkeypatch, worker, [(0.0, 20.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2, vad=False))

    # Only the ticks denominated in SECONDS — a fetch tick's total is bytes,
    # and comparing the two units in one set is how a download's size would be
    # read as a claim about the recording's length.
    totals = {t.get("total") for t in base.ticks
              if t.get("total") is not None and t.get("unit") == "s"}
    assert totals == {20.0}, totals
    # …and the stage is its own line on the row, with an indeterminate bar —
    # what the job record already offers for a phase with no position to report,
    # and what the audio decode above it already does.
    finding = [t for t in base.ticks if t.get("detail") == "Finding speakers…"]
    assert finding, base.ticks
    assert all(t.get("done") is None and t.get("total") is None for t in finding)


def test_the_component_fetch_reports_to_the_row_the_USER_is_watching(
        monkeypatch, loaded, tmp_path, base):
    """It happens inside a TRANSCRIPTION, so an unbound `download_file` ticks
    into this process's `JOB_ID` — the model's own load row, finished long ago —
    reopening it as a running download of something nobody asked for, while the
    row the page is watching says nothing at all."""
    worker, _ = loaded(windows=(100,))
    import diarize as diarize_module
    monkeypatch.setattr(diarize_module, "diarizer", lambda *a: object())
    monkeypatch.setattr(diarize_module, "speaker_turns",
                        lambda *a, **k: [(0.0, 10.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2, vad=False,
                             job="sys:ai-transcribe:zzz"))

    assert len(base.fetches) == 2, base.fetches
    for fetch in base.fetches:
        assert fetch["job"] == "sys:ai-transcribe:zzz", fetch
        # …with the row's IDENTITY, because the manager can evict and rebuild
        # any row at any tick and a report with no `title` is refused outright.
        assert fetch["row"] == ROW, fetch


def test_no_stage_is_left_showing_a_FINISHED_DOWNLOAD_while_it_works(
        monkeypatch, loaded, base, tmp_path):
    """A component fetch leaves the row at `kind="download"`, `unit="bytes"`,
    `done == total` — correct while it downloads, and a completed 2MB download
    sitting over a 90-minute transcription if nothing restores the row.

    Both fetching stages must therefore report the row's own `kind`/`unit` back
    BEFORE the work they fetched for begins, and the window in between must be
    the load of the model rather than the work. This is an invariant over every
    fetching stage rather than a test of one, because the failure is invisible:
    the transcript is perfect and the row simply lies for minutes.
    """
    worker, _ = loaded(windows=(100,), audio_seconds=20.0)
    _diarizes(monkeypatch, worker, [(0.0, 20.0, 0)])
    # The VAD fetches too, on a machine whose whisper download predates AI-10f.
    import vad as vad_module
    monkeypatch.setattr(vad_module, "model_path",
                        lambda download: download("onnx-community/silero-vad",
                                                  "onnx/model.onnx"))
    monkeypatch.setattr(vad_module, "session", lambda path: object())
    monkeypatch.setattr(vad_module, "speech_regions", lambda audio, sess: [(0.0, 20.0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2))

    # Every tick that says `download` is a fetch reporting its own bytes; what
    # matters is that a task tick follows each RUN of them before any work.
    kinds = [t.get("kind") for t in base.ticks]
    assert "download" in kinds, base.ticks
    for index, kind in enumerate(kinds):
        if kind != "download":
            continue
        rest = kinds[index + 1:]
        assert "task" in rest, (
            f"tick {index} left the row as a download with no task tick after "
            f"it: {base.ticks[index:]}")
    # …and the last word on the row is the transcription's, not a download's.
    assert kinds[-1] == "task", base.ticks[-1]


def test_a_cancel_during_the_pre_pass_is_translated_into_the_workers_own(
        monkeypatch, loaded, tmp_path):
    """`diarize.py` cannot name `worker_base.Cancelled` — it is imported by the
    server too — so it raises its own type and the runner translates. Untranslated,
    a ✕ pressed while finding speakers would reach the supervisor as an ERROR
    and tell the user the transcription they cancelled had failed."""
    worker, transcribe = loaded(windows=(100,))
    import diarize as diarize_module
    monkeypatch.setattr(diarize_module, "diarizer", lambda *a: object())
    monkeypatch.setattr(diarize_module, "speaker_turns",
                        _raise(diarize_module.DiarizationCancelled()))

    with pytest.raises(worker_base_cancelled(worker)):
        worker.generate(_request(tmp_path, diarize=True, speakers=2))
    assert transcribe.calls == []


def worker_base_cancelled(worker):
    """The `Cancelled` this worker's base module carries — the fake one under
    test, not the real `worker_base`'s."""
    import worker_base

    return worker_base.Cancelled


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


def test_vad_false_means_the_DETECTOR_IS_NOT_RUN(monkeypatch, loaded, tmp_path):
    """The flag now means what it means on the CT2 runner: run Silero, or do not.

    It used to map onto mlx-whisper's per-window `no_speech_threshold`, which
    was the closest thing available before this runner had a real filter — and
    which left that threshold DISABLED on the false branch, something
    `vad_filter=False` does not do over there. Both engines now answer the same
    flag the same way, and neither touches the threshold.
    """
    asked = []
    worker, transcribe = loaded(windows=(100,))
    monkeypatch.setattr(worker, "_speech_regions",
                        lambda *a, **k: asked.append(True) or [])

    worker.generate(_request(tmp_path, vad=False))
    assert asked == [], "the detector ran for a caller who said not to"
    assert "no_speech_threshold" not in transcribe.calls[0]

    # An explicit JSON null is "not specified", never False — the inversion the
    # CT2 runner shipped once. It means the default, which is ON.
    worker.generate(_request(tmp_path, vad=None, out=str(tmp_path / "b.json"),
                             outText=str(tmp_path / "b.txt")))
    assert asked == [True]


# -- the VAD filter, and what it does to time ------------------------------------
#
# The detector itself is `tests/test_ai_mlx_whisper_vad.py`'s. What is driven
# here is the WIRING: how many calls the regions produce, where their timestamps
# land, what the progress bar is denominated in, and what a cancel means once
# there is more than one call to cancel between.


def _regions(monkeypatch, worker, found):
    """Stand in for the detector, with no onnxruntime and no model file."""
    monkeypatch.setattr(worker, "_speech_regions", lambda *a, **k: list(found))


def _raise(error):
    """A stand-in for a `vad` function that fails with `error`."""
    def fail(*_args, **_kwargs):
        raise error
    return fail


def test_each_speech_region_is_transcribed_SEPARATELY(monkeypatch, loaded, tmp_path):
    """Cutting at a VAD boundary is cutting in silence, which is where a
    sentence has already ended — unlike the fixed-offset chunking this runner
    refuses, which cuts through one."""
    worker, transcribe = loaded(windows=(100,), audio_seconds=30.0)
    _regions(monkeypatch, worker, [(0.0, 5.0), (20.0, 25.0)])

    worker.generate(_request(tmp_path))

    assert len(transcribe.calls) == 2
    # Each call got ITS region's samples, not the whole recording.
    assert [len(c["audio"]) for c in transcribe.calls] == [5 * 16000, 5 * 16000]


def test_timestamps_are_mapped_back_to_ORIGINAL_recording_time(
        monkeypatch, loaded, tmp_path):
    """The silent failure this remap exists to prevent: a transcript that looks
    perfect and whose every timestamp after the first gap is early. The library
    times each clip from zero; every consumer reads the numbers as positions in
    the FILE."""
    worker, transcribe = loaded(windows=(100,), audio_seconds=40.0,
                                segments=[_segment(0.0, 2.0, "hello")])
    _regions(monkeypatch, worker, [(0.0, 5.0), (30.0, 35.0)])
    request = _request(tmp_path)

    worker.generate(request)

    written = json.load(open(request["out"], encoding="utf-8"))
    assert [(s["start"], s["end"]) for s in written["segments"]] == [
        (0.0, 2.0), (30.0, 32.0)]


def test_a_segment_running_past_its_region_is_CLAMPED(monkeypatch, loaded, tmp_path):
    """Whisper times against a padded 30-second window, so the last segment of
    a short clip can end past the clip. Unclamped it would place speech inside
    the silence that was removed — and could overlap the next region, putting
    the transcript out of order."""
    worker, transcribe = loaded(windows=(100,), audio_seconds=40.0,
                                segments=[_segment(0.0, 29.0, "hello")])
    _regions(monkeypatch, worker, [(10.0, 12.0)])
    request = _request(tmp_path)

    worker.generate(request)

    written = json.load(open(request["out"], encoding="utf-8"))
    assert written["segments"][0]["end"] == 12.0


def test_a_segment_STARTING_past_its_region_is_dropped(monkeypatch, loaded, tmp_path):
    """The same 30-second padded window, seen from the other end.

    If a relative END of 29.0 on a two-second clip is a shape this file accepts
    — and the clamp test above says it is — then a relative START past the clip
    is the same shape, and it is what a hallucination in the padding looks like.
    Clamping only the end emitted `{"start": 15.0, "end": 12.0}`: a segment that
    runs backwards, sorts wrongly against the next region, and would make a
    caption track or a seeking player do something visibly strange.

    Dropped rather than pinned to the region's end, because a zero-length
    segment at the boundary is text asserted to have been spoken during silence
    that was cut. There is nothing there; the honest transcript omits it.
    """
    worker, transcribe = loaded(
        windows=(100,), audio_seconds=40.0,
        segments=[_segment(0.0, 1.0, "hello"), _segment(5.0, 8.0, "thanks for watching")])
    _regions(monkeypatch, worker, [(10.0, 12.0)])
    request = _request(tmp_path)

    worker.generate(request)

    written = json.load(open(request["out"], encoding="utf-8"))
    assert [(s["start"], s["end"]) for s in written["segments"]] == [(10.0, 11.0)]
    assert "watching" not in written["text"]


def test_a_segment_starting_INSIDE_its_region_survives_a_long_end(
        monkeypatch, loaded, tmp_path):
    """The drop above must not swallow the clamp's own case: a segment that
    begins in the region and merely overruns it is real speech, and keeping it
    (clamped) is the whole point of the clamp."""
    worker, transcribe = loaded(windows=(100,), audio_seconds=40.0,
                                segments=[_segment(1.5, 29.0, "hello")])
    _regions(monkeypatch, worker, [(10.0, 12.0)])
    request = _request(tmp_path)

    worker.generate(request)

    written = json.load(open(request["out"], encoding="utf-8"))
    assert [(s["start"], s["end"]) for s in written["segments"]] == [(11.5, 12.0)]


def test_the_ETA_rate_is_measured_in_SPEECH_not_in_recording_time(
        monkeypatch, loaded, tmp_path):
    """`done` is remapped into original-recording time so the BAR means what
    SPEC AI-10a says it means — but `_eta`'s rate is `elapsed / done_audio`, and
    that has to be audio actually DECODED or the rate is nonsense.

    Handed the remapped position, a 100-second recording whose only speech is
    its last 10 seconds reports `done ≈ 90` on the first tick, having decoded
    about a second: a rate of ~0.006s of wall clock per second of audio, and
    "~0s left" for the whole decode. The mistake runs the other way for an
    EARLY region, which ends up promising minutes on a job about to finish.

    The `done`/`total` pair itself is NOT what this fixes — that they denominate
    the recording while the decode chews through speech is the accepted,
    documented trade the CT2 runner makes too. Only the ETA's own two numbers
    move, and they move together.
    """
    worker, _transcribe = loaded(windows=(100, 100), seconds_per_window=0.05,
                                 audio_seconds=100.0)
    _regions(monkeypatch, worker, [(90.0, 100.0)])

    seen = []
    real_eta = worker._eta

    def spy(remaining_audio, elapsed, done_audio):
        seen.append((remaining_audio, done_audio))
        return real_eta(remaining_audio, elapsed, done_audio)

    monkeypatch.setattr(worker, "_eta", spy)

    worker.generate(_request(tmp_path))

    assert seen, "no tick landed during the decode"
    # Both numbers are seconds of SPEECH — the 10-second region, never the
    # 100-second file. `done_audio` above 10 is the bug: it is the bar's
    # position being read as a quantity of decoding.
    assert all(done <= 10.0 for _remaining, done in seen), seen
    assert all(0.0 < remaining <= 10.0 for remaining, _done in seen), seen


def test_the_ETA_does_not_charge_the_DIARIZATION_to_the_transcript(
        monkeypatch, loaded, tmp_path):
    """`transcribing_since` starts below the speaker pre-pass, not above it.

    `_eta` divides `elapsed` by seconds of speech decoded, so every second on
    that clock before a word exists inflates the rate. Started above the
    pre-pass, a 90-minute recording whose diarization takes three minutes
    charges all three to the first window — the exact failure the variable was
    introduced to prevent (the audio decode), reintroduced one phase later.

    Driven by making the pre-pass take real time and asserting the elapsed the
    ETA is handed excludes it. `faster_whisper/worker.py` starts its clock after
    both pre-passes for the same reason.
    """
    pre_pass_seconds = 0.6
    # Long enough to tick (`_TICK_S` is 0.02 here) but far SHORTER than the
    # pre-pass, so the two are unambiguous in the elapsed the ETA is handed.
    worker, _ = loaded(windows=(100, 100), seconds_per_window=0.05,
                       audio_seconds=60.0)
    _regions(monkeypatch, worker, [(0.0, 60.0)])

    import diarize as diarize_module
    monkeypatch.setattr(diarize_module, "diarizer", lambda *a: object())
    monkeypatch.setattr(
        diarize_module, "speaker_turns",
        lambda *a, **k: (time.sleep(pre_pass_seconds), [(0.0, 60.0, 0)])[1])

    seen = []
    real_eta = worker._eta
    monkeypatch.setattr(worker, "_eta",
                        lambda remaining, elapsed, done: (
                            seen.append(elapsed) or real_eta(remaining, elapsed, done)))

    worker.generate(_request(tmp_path, diarize=True, speakers=2))

    assert seen, "no tick landed during the decode"
    # The pre-pass alone is 0.6s. An elapsed that includes it would start at or
    # above that on the very first tick; the clock reset means it starts near
    # zero and only counts the decoding.
    assert min(seen) < pre_pass_seconds, seen


def test_progress_is_seconds_of_the_ORIGINAL_audio_not_of_the_speech(
        monkeypatch, loaded, base, tmp_path):
    """The contract SPEC AI-10a and `runtime.js` both state, and the thing
    filtering silence would quietly redefine.

    The borrowed counter denominates seconds of whatever waveform was handed to
    the library — once the silence is dropped, that is seconds of SPEECH. Left
    alone, a 40-second recording with 30 seconds of silence would report
    `done` climbing to 10 against a `total` of 40 and stop there: a bar that
    can never finish, in a unit that is not the one the page was promised.
    """
    worker, transcribe = loaded(windows=(500,), seconds_per_window=0.1,
                                audio_seconds=40.0)
    # Five seconds of speech at the very end of a 40-second file.
    _regions(monkeypatch, worker, [(35.0, 40.0)])

    worker.generate(_request(tmp_path))

    progress = [t for t in base.ticks if t.get("done")]
    assert progress, "no progress was reported at all"
    # Every position sits INSIDE the region, in original-recording time —
    # never at 5s, which is where the speech-only counter would have put it.
    assert all(35.0 <= t["done"] <= 40.0 for t in progress), progress
    assert {t["total"] for t in progress} == {40.0}


def test_a_recording_the_detector_finds_no_speech_in_is_still_transcribed(
        monkeypatch, loaded, tmp_path):
    """Reporting an empty transcript for a file nobody looked at is the
    confident version of a wrong answer. The detector is tuned for speech, not
    for whispering, singing or a bad microphone, and Whisper's own no-speech
    handling is the better final word."""
    worker, transcribe = loaded(windows=(100,), audio_seconds=12.0)
    _regions(monkeypatch, worker, [])

    worker.generate(_request(tmp_path))

    assert len(transcribe.calls) == 1
    assert len(transcribe.calls[0]["audio"]) == 12 * 16000


def test_a_missing_DETECTOR_degrades_to_transcribing_everything_and_says_so(
        monkeypatch, loaded, base, tmp_path, capsys):
    """A `vad: true` that quietly did nothing is exactly the two-engines-one-flag
    difference this runner exists to remove — but failing the transcription
    outright would be worse: the VAD is an optimisation over a run that works
    without it, and this is what an offline machine with no cached detector
    looks like. So it degrades, and it says so on the row the user is watching.
    """
    worker, transcribe = loaded(windows=(100,), audio_seconds=8.0)
    # Forced rather than left to the environment: onnxruntime is absent from
    # the test venv today, so this path is taken for free — but the day
    # somebody installs it for another reason, a test relying on that would
    # start reaching for a 2MB download instead of asserting anything.
    #
    # An OSError because that is the shape the real failure has: every
    # huggingface_hub download error worth degrading on is one (HfHubHTTPError
    # is declared `(HTTPError, OSError)`), as is a socket that never connected.
    import vad as vad_module

    monkeypatch.setattr(vad_module, "model_path", _raise(
        OSError("could not reach huggingface.co")))

    worker.generate(_request(tmp_path))

    assert len(transcribe.calls) == 1, "it should have transcribed the whole file"
    said = [t for t in base.ticks
            if "unavailable" in str(t.get("detail"))]
    assert said, "the degradation was silent"
    assert "speech detection unavailable" in capsys.readouterr().err.lower()


def test_a_BROKEN_onnx_session_degrades_too(monkeypatch, loaded, base, tmp_path):
    """The other half of "could not be obtained": the file arrived and the
    runtime refused it. A truncated 2MB download is a corrupt model file, which
    is the same story for the user as never having got one."""
    worker, transcribe = loaded(windows=(100,), audio_seconds=8.0)
    import vad as vad_module

    monkeypatch.setattr(vad_module, "model_path", lambda download: "/nowhere.onnx")
    monkeypatch.setattr(vad_module, "session", _raise(
        FileNotFoundError("the speech detector is missing at /nowhere.onnx")))

    worker.generate(_request(tmp_path))

    assert len(transcribe.calls) == 1, "it should have transcribed the whole file"
    assert [t for t in base.ticks if "unavailable" in str(t.get("detail"))]


@pytest.mark.parametrize("attribute, error", [
    ("session", TypeError("session() got an unexpected keyword argument")),
    ("model_path", AttributeError("module 'vad' has no attribute 'REPO'")),
    ("speech_regions", ValueError("cannot reshape array of size 512 into (1, 480)")),
])
def test_a_BUG_in_the_detector_is_not_degraded_away(monkeypatch, loaded, tmp_path,
                                                    attribute, error):
    """The degrade above is for a detector that could not be OBTAINED, and it is
    the whole reason this catch has to stay narrow: a `TypeError` from `vad.py`
    absorbed by it would reach the user as "Speech detection unavailable" — a
    sentence that reads like a flaky network and sends nobody to the bug.

    `speech_regions` is in the list because it is where a detector bug actually
    lives (a bad tensor shape, a state threaded wrong), and it used to sit
    inside the same `try` as the fetch.
    """
    worker, _transcribe = loaded(windows=(100,), audio_seconds=8.0)
    import vad as vad_module

    monkeypatch.setattr(vad_module, "model_path", lambda download: "/nowhere.onnx")
    monkeypatch.setattr(vad_module, "session", lambda path: object())
    monkeypatch.setattr(vad_module, attribute, _raise(error))

    with pytest.raises(type(error), match=r"."):
        worker.generate(_request(tmp_path))


def test_a_CANCEL_during_the_detector_fetch_is_not_degraded_away(
        monkeypatch, loaded, base, tmp_path):
    """`worker_base.Cancelled` is an `Exception`, so the catch that used to be
    here could have eaten a ✕ and gone on to transcribe the whole file the user
    had just stopped. Latent rather than live — no download reports through
    `report_or_cancel` today — and pinned so it stays that way if one does.
    """
    worker, _transcribe = loaded(windows=(100,), audio_seconds=8.0)
    import vad as vad_module

    monkeypatch.setattr(vad_module, "model_path", _raise(base.Cancelled()))

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))


def test_a_cancel_between_REGIONS_is_honoured_and_writes_nothing(
        monkeypatch, loaded, base, tmp_path):
    """Keeping the first region of five and writing it out would present a
    fifth of a transcript as a whole one. A cancel is worth honouring exactly
    while there is work left to stop, and between regions there is."""
    worker, transcribe = loaded(windows=(500,), seconds_per_window=0.15,
                                audio_seconds=60.0,
                                segments=[_segment(0.0, 1.0, "one")])
    _regions(monkeypatch, worker, [(0.0, 5.0), (10.0, 15.0), (20.0, 25.0)])
    request = _request(tmp_path)

    real_report = base.report_or_cancel

    def slow_tick(job=None, **fields):
        if "Transcribing" in str(fields.get("detail") or ""):
            assert transcribe.done.wait(5)
            base.cancel_on_tick = len(base.ticks) + 1
        return real_report(job=job, **fields)

    monkeypatch.setattr(base, "report_or_cancel", slow_tick)

    with pytest.raises(base.Cancelled):
        worker.generate(request)
    assert not os.path.exists(request["out"])


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


# -- the progressive transcript --------------------------------------------------
#
# The writer, its line shape and its lifecycle are `runners/partial.py`'s and
# are driven directly in `tests/test_ai_partial_transcript.py`; the lifecycle is
# driven end-to-end through a whole `generate()` in `tests/test_ai_whisper_
# worker.py`. What is proved HERE is what only this engine can get wrong: that
# it feeds the sink at all, and that it feeds it the REMAPPED timestamps rather
# than the clip-relative ones the library hands back.


def _partial_lines(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_segments_reach_the_partial_file_in_ORIGINAL_recording_time(
        monkeypatch, loaded, tmp_path):
    """The remap is per-region and happens after the library returns, so a sink
    fed one line earlier — inside `_decode_clip`, say — would publish `0.0-2.0`
    for speech that is 30 seconds into the file. A page seeking a player off
    that lands in the wrong minute, and the final `.json` would disagree with
    the lines the same page had already rendered."""
    worker, _ = loaded(windows=(100,), audio_seconds=40.0,
                       segments=[_segment(0.0, 2.0, "hello")])
    _regions(monkeypatch, worker, [(0.0, 5.0), (30.0, 35.0)])
    request = _request(tmp_path, outPartial=str(tmp_path / "out.partial.jsonl"))
    seen = []
    monkeypatch.setattr(
        worker.partial.Sink, "add",
        lambda self, segment, _real=worker.partial.Sink.add: (
            _real(self, segment),
            seen.append(_partial_lines(self.path)[-1]))[0])

    worker.generate(request)

    assert [(line["start"], line["end"]) for line in seen] == [
        (0.0, 2.0), (30.0, 32.0)]
    written = json.load(open(request["out"], encoding="utf-8"))
    assert [(s["start"], s["end"]) for s in written["segments"]] == [
        (0.0, 2.0), (30.0, 32.0)]
    assert not os.path.exists(request["outPartial"])


def test_a_segment_the_remap_DROPS_never_reaches_the_partial_file(
        monkeypatch, loaded, tmp_path):
    """A segment starting past its region is omitted from the transcript (it
    would run backwards, or claim speech inside silence that was cut). A page
    tailing the file must not be shown a line the final transcript will not
    have — there is no retraction in an append-only stream."""
    worker, _ = loaded(windows=(100,), audio_seconds=40.0,
                       segments=[_segment(0.0, 1.0, "real"),
                                 _segment(15.0, 20.0, "padding")])
    _regions(monkeypatch, worker, [(10.0, 12.0)])
    request = _request(tmp_path, outPartial=str(tmp_path / "out.partial.jsonl"))
    seen = []
    monkeypatch.setattr(
        worker.partial.Sink, "add",
        lambda self, segment, _real=worker.partial.Sink.add: (
            _real(self, segment), seen.append(dict(segment)))[0])

    worker.generate(request)

    assert [line["text"] for line in seen] == ["real"]
    written = json.load(open(request["out"], encoding="utf-8"))
    assert [s["text"] for s in written["segments"]] == ["real"]


def test_this_engine_writes_the_SAME_final_bytes_with_and_without_one(
        monkeypatch, loaded, tmp_path):
    """The additive promise, pinned on this engine too — `outPartial` is the
    only difference between the two runs, and the transcript is not allowed to
    notice it."""
    def run(**over):
        worker, _ = loaded(windows=(100,), audio_seconds=20.0,
                           segments=[_segment(0.0, 1.5, " hello"),
                                     _segment(1.5, 3.0, " wörld ")])
        request = _request(tmp_path, **over)
        worker.generate(request)
        written = json.loads(open(request["out"], encoding="utf-8").read())
        # `seconds` is wall time and differs between any two runs; re-dumped
        # with the writer's own arguments so a reordered key or a changed
        # indent still reads as a byte difference.
        return (json.dumps(written | {"seconds": 0}, ensure_ascii=False,
                           indent=1).encode(),
                open(request["outText"], "rb").read())

    plain = run()
    progressive = run(outPartial=str(tmp_path / "out.partial.jsonl"))
    assert plain == progressive


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


def _pinned_run(monkeypatch, base, tmp_path, mlx_core, clips=1):
    """A load and a transcription, on the threads production uses."""
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "weights.npz").write_bytes(b"")
    transcribe = FakeTranscribeModule(windows=(100,),
                                      segments=[_segment(0.0, 1.0, "hi")])
    worker = load_worker(monkeypatch, base, transcribe_module=transcribe,
                         av_module=make_av(seconds=2.0), mlx_core=mlx_core)
    worker._TICK_S = 0.02
    # `load` runs on the bring-up thread in production (`worker_base.serve`),
    # never on the one that decodes — which is the whole of the bug.
    loader = threading.Thread(
        target=worker.load, args=("mlx-community/whisper-small-mlx", str(snapshot)),
        name="load")
    loader.start()
    loader.join()
    worker.generate(_request(tmp_path, vad=False))
    return worker, transcribe


def test_the_load_and_the_decode_share_ONE_mlx_stream(monkeypatch, base, tmp_path):
    """From mlx 0.32 a stream belongs to the THREAD that made it, and an array
    evaluated anywhere else throws a C++ exception nothing catches — the process
    aborts. This runner loads on the bring-up thread and decodes on a fresh
    thread per clip, so every MLX Whisper transcription died the moment
    `transcribe()` touched the primed weights: "the transcription process did
    not answer: Remote end closed connection without response", with one
    `libc++abi: … There is no Stream(gpu, 1) in current thread` line in the
    worker log.

    One shared stream, pinned on every thread that touches MLX, is the fix — so
    what this pins is that BOTH threads pinned, and pinned the SAME stream.
    """
    mlx_core = FakeMlxCore()

    _pinned_run(monkeypatch, base, tmp_path, mlx_core)

    threads = {name for name, _stream in mlx_core.pinned}
    streams = {stream for _name, stream in mlx_core.pinned}
    assert len(threads) > 1, f"only one thread pinned a stream: {mlx_core.pinned}"
    assert streams == {"SHARED-STREAM-1"}, mlx_core.pinned
    # One stream for the process, not one per thread: a second would be a second
    # owner, which is the thing being prevented.
    assert mlx_core.made == ["DEVICE"]


def test_an_mlx_without_thread_local_streams_is_left_alone(monkeypatch, base, tmp_path):
    """Streams were process-wide before 0.32 and there was nothing to pin. A
    runner that insisted on the newer call would turn a version skew into a
    worker that cannot transcribe at all."""
    mlx_core = types.SimpleNamespace(float16="FLOAT16", get_active_memory=lambda: 0)

    _worker, transcribe = _pinned_run(monkeypatch, base, tmp_path, mlx_core)

    assert transcribe.calls, "the transcription never ran"


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
