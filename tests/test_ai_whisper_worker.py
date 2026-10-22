"""The transcription runner's own logic, driven directly (SPEC AI-10, AI-10a).

`tests/test_ai_runtime.py` proves the SUPERVISOR's half against a fake worker;
`tests/test_ai_worker_base.py` proves the CONTRACT. Neither touches what
`faster_whisper/worker.py` actually does with a model, and the claims that
matter most in this feature live exactly there: that progress is seconds of
audio, and that a ✕ is honoured throughout the run.

Those claims are testable here because the module is **stdlib-only at import
time** — `faster_whisper` and `ctranslate2` are imported inside the functions
that need them, never at the top — so the decode loop can be driven with a stub
model standing in for Whisper. That is the same reason `worker_base` is
testable, applied one level down. What is still NOT covered is Whisper itself:
no audio is decoded here, and the numbers the stub feeds in are the numbers a
real `info.duration` and a real `segment.end` would have to supply.
"""
import importlib.util
import json
import os
import re
import sys
import threading
import time
import types

import pytest

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "faster_whisper", "worker.py",
)


class FakeBase:
    """A stand-in for `worker_base`, recording every tick.

    Injected into `sys.modules` rather than monkeypatched onto the real module,
    so a test can assert on the ticks without mutating the contract module every
    other AI test imports.
    """

    class Cancelled(Exception):
        pass

    def __init__(self):
        self.ticks = []
        self.CANCEL = threading.Event()
        #: Every `download_file` call, with the row it was told to report to.
        self.fetches = []
        #: Set by a test to have the NEXT tick answer "the ✕ was pressed",
        #: which is how a real cancel reaches a worker (the reply to the tick
        #: it was sending anyway).
        self.cancel_on_tick = None
        #: `set_state` fields, as `worker_base.STATE` would hold them.
        self.state = {}
        #: `download_snapshot` calls, and the folder it "downloaded" into.
        self.snapshots = []
        self.snapshot_dir = ""

    def set_state(self, **fields):
        self.state.update(fields)

    def download_file(self, repo_id, filename, detail=None, job=None, row=None):
        # `job`/`row` recorded rather than ignored: the diarization models are
        # fetched DURING a transcription, so a tick without them goes to this
        # process's `JOB_ID` — the model's own finished load row — reopening it
        # as a running download while the row the page watches says nothing.
        self.fetches.append({"repo": repo_id, "file": filename,
                             "job": job, "row": row})
        return f"/snapshots/{repo_id}/{filename}"

    def download_snapshot(self, model_id, **kwargs):
        self.snapshots.append({"model": model_id, **kwargs})
        return self.snapshot_dir

    def report(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        return None

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        if self.cancel_on_tick is not None and len(self.ticks) >= self.cancel_on_tick:
            raise self.Cancelled()
        return None


def _load_worker(base):
    """A fresh import of the whisper worker, against `base`.

    By path and with `worker_base` primed in `sys.modules`, for the reason the
    runner exists: it loads its base off `sys.path` in its own interpreter, not
    as `fused_render.ai.runners.…`, so importing it the packaged way would be
    testing an import that never ships.
    """
    saved = sys.modules.get("worker_base")
    sys.modules["worker_base"] = base
    try:
        spec = importlib.util.spec_from_file_location(
            "faster_whisper_worker_under_test", WORKER_PATH)
        # Both are Optional in the stubs, and a None here would otherwise
        # surface as an AttributeError three lines later with no clue that the
        # runner had simply been moved or renamed.
        assert spec is not None and spec.loader is not None, WORKER_PATH
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            sys.modules.pop("worker_base", None)
        else:
            sys.modules["worker_base"] = saved


@pytest.fixture()
def base():
    return FakeBase()


@pytest.fixture()
def worker(base):
    return _load_worker(base)


class FakeSegment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class FakeModel:
    """Whisper's shape: `transcribe()` returns `(generator, info)` — but only
    after doing the eager work first, which is the property under test."""

    def __init__(self, segments, duration=180.0, decode_seconds=0.0, language="en"):
        self.segments = segments
        self.info = types.SimpleNamespace(duration=duration, language=language)
        # The rate `transcribe()` would have decoded a path at. Read rather than
        # assumed, because it is NOT an argument of `transcribe()` — an array
        # decoded at a different rate transcribes a chipmunk with no error.
        self.feature_extractor = types.SimpleNamespace(sampling_rate=16000)
        self.decode_seconds = decode_seconds
        self.calls = []
        #: How many decodes were ever running at once. One model, one process —
        #: anything above 1 is the thing `GENERATE_LOCK` exists to prevent.
        self.max_concurrent = 0
        self._live = 0
        self._lock = threading.Lock()

    def transcribe(self, source, **kwargs):
        self.calls.append({"source": source, **kwargs})
        with self._lock:
            self._live += 1
            self.max_concurrent = max(self.max_concurrent, self._live)
        try:
            # The eager phase: PyAV decodes the whole file and (with the VAD on)
            # silero runs over all of it, BEFORE the generator is handed back.
            time.sleep(self.decode_seconds)
            return iter(self.segments), self.info
        finally:
            with self._lock:
                self._live -= 1


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


# -- device and compute type, and the same trade taken on both -----------------


def _fake_ctranslate2(cuda_devices):
    def get_cuda_device_count():
        if isinstance(cuda_devices, Exception):
            raise cuda_devices
        return cuda_devices
    return types.SimpleNamespace(get_cuda_device_count=get_cuda_device_count)


def test_placement_without_a_cuda_device_is_int8_on_cpu(worker, monkeypatch):
    monkeypatch.setitem(sys.modules, "ctranslate2", _fake_ctranslate2(0))
    assert worker._placement() == ("cpu", "int8")


def test_placement_with_a_cuda_device_is_int8_float16_not_plain_float16(
        worker, monkeypatch):
    """The CUDA branch takes the same memory-for-a-small-WER-cost trade the
    CPU branch documents, not the opposite one — `int8_float16`, which keeps
    the non-quantized layers computing in float16, not bare `int8`."""
    monkeypatch.setitem(sys.modules, "ctranslate2", _fake_ctranslate2(1))
    assert worker._placement() == ("cuda", "int8_float16")


def test_placement_falls_back_to_CPU_when_the_cuda_probe_itself_fails(
        worker, monkeypatch):
    monkeypatch.setitem(sys.modules, "ctranslate2",
                        _fake_ctranslate2(RuntimeError("no driver")))
    assert worker._placement() == ("cpu", "int8")


# -- the eager phase, which is neither lazy nor free -----------------------------


def test_the_decode_phase_ticks_while_it_runs(worker, base, tmp_path):
    """`transcribe()` LOOKS lazy — it returns a generator — but faster-whisper
    decodes the whole file and runs the VAD before handing it back. On a long
    recording that is minutes, and the row must not sit silent through them."""
    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 3.0, "hi")],
                                        decode_seconds=0.35)
    worker._TICK_S = 0.05

    worker.generate(_request(tmp_path))

    decoding = [t for t in base.ticks if "Decoding" in str(t.get("detail"))]
    assert len(decoding) > 2, base.ticks


def test_a_cancel_during_DECODING_is_honoured_before_any_segment(worker, base, tmp_path):
    """The claim that made this a bug: the docstring promised the ✕ was
    honoured through the run, while the only tick before the first segment was
    a plain `report` that cannot carry a cancel back. A user pressing ✕ over a
    90-minute file waited out the whole decode."""
    model = FakeModel([FakeSegment(0.0, 3.0, "hi")], decode_seconds=0.4)
    worker._loaded["model"] = model
    worker._TICK_S = 0.05
    base.cancel_on_tick = 2  # the first tick INSIDE the decode wait

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))
    # It stopped INSIDE the decode, not at the first segment — no tick ever
    # carried a `done`, which is what a segment tick is. Without this the test
    # passes on the very bug it exists for.
    assert not [t for t in base.ticks if t.get("done")], base.ticks
    # And nothing was written: a cancelled run leaves no half-transcript behind.
    assert not os.path.exists(_request(tmp_path)["out"])


def test_a_cancelled_decode_is_WAITED_FOR_before_the_next_one_starts(worker, base, tmp_path):
    """A cancel unwinds the handler, but not the decode it abandoned.

    `_call_with_ticks` raises on the handler thread; `worker_base._single`
    catches it, replies, and leaves its `with GENERATE_LOCK` block — while the
    decode thread is still inside `model.transcribe()`, holding the whole mel
    buffer. Press ✕ and immediately re-submit and two decodes run at once on
    one process and one `WhisperModel`, which is exactly what that lock exists
    to prevent. The lock is the worker base's and not ours to change, so the
    abandoned thread has to be waited for here.
    """
    model = FakeModel([FakeSegment(0.0, 3.0, "hi")], decode_seconds=0.4)
    worker._loaded["model"] = model
    worker._TICK_S = 0.05
    base.cancel_on_tick = 2

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))

    # Straight back in, the way a user who pressed ✕ and retried would.
    base.cancel_on_tick = None
    worker.generate(_request(tmp_path, out=str(tmp_path / "second.json"),
                             outText=str(tmp_path / "second.txt")))

    assert model.max_concurrent == 1, "two decodes ran on one model at once"
    assert len(model.calls) == 2


def test_a_WEDGED_abandoned_decode_is_refused_rather_than_waited_on_forever(
        worker, base, tmp_path):
    """"Bounded by the file" is only true of a decoder that returns.

    A malformed container is exactly where PyAV or the VAD hangs, and then
    `model.transcribe()` never returns. An unbounded join blocked every later
    transcription on "Finishing a cancelled decode…" for the life of the
    process, with unloading the model the only escape — a hang with a spinner,
    which is the worst failure available because nothing says what is wrong.

    The wedged thread is a daemon and is deliberately left running: it may yet
    finish, and the next request would then sail through. What must not happen
    is an unbounded wait or a second decode on the same model.
    """
    stuck = threading.Event()
    entered = threading.Event()

    class WedgedModel(FakeModel):
        def transcribe(self, source, **kwargs):
            self.calls.append({"source": source, **kwargs})
            entered.set()
            stuck.wait(30)  # never set within the test
            return iter([]), self.info

    worker._loaded["model"] = WedgedModel([])
    worker._TICK_S = 0.02
    worker._ORPHAN_WAIT_S = 0.2
    base.cancel_on_tick = 2

    try:
        with pytest.raises(base.Cancelled):
            worker.generate(_request(tmp_path))
        assert entered.wait(5)

        # Straight back in: bounded refusal, not a hang.
        base.cancel_on_tick = None
        with pytest.raises(RuntimeError) as caught:
            worker.generate(_request(tmp_path, out=str(tmp_path / "b.json"),
                                     outText=str(tmp_path / "b.txt")))
        message = str(caught.value)
        assert "cancelled" in message and "unload" in message.lower()
        # And it refused rather than starting a second decode on one model:
        # `transcribe` was entered exactly once.
        assert len(worker._loaded["model"].calls) == 1
    finally:
        stuck.set()
        worker._orphan["thread"] = None


def test_an_abandoned_decode_that_FINISHES_lets_the_next_one_through(
        worker, base, tmp_path):
    """The other half of the deadline: refusing is temporary, not sticky. The
    orphan slot is kept on the deadline precisely so a decode that does finish
    clears it and the next request proceeds normally."""
    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 1.0, "hi")],
                                        decode_seconds=0.3)
    worker._TICK_S = 0.02
    worker._ORPHAN_WAIT_S = 5.0
    base.cancel_on_tick = 2
    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))

    base.cancel_on_tick = None
    result = worker.generate(_request(tmp_path, out=str(tmp_path / "b.json"),
                                     outText=str(tmp_path / "b.txt")))
    assert result["segments"] == 1
    assert worker._orphan["thread"] is None


def test_a_cancel_between_segments_is_still_honoured(worker, base, tmp_path):
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 3.0, "one"), FakeSegment(3.0, 6.0, "two")])
    base.cancel_on_tick = 2

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))


# -- what the row is told -------------------------------------------------------


def test_progress_is_SECONDS_OF_AUDIO_against_the_duration(worker, base, tmp_path):
    """`done` is the last segment's end timestamp and `total` is the audio's
    duration — the unit a person watching a recording is thinking in, and the
    one SPEC AI-10a promises."""
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 12.0, "one"), FakeSegment(12.0, 30.0, "two")],
        duration=90.0)

    worker.generate(_request(tmp_path))

    segment_ticks = [t for t in base.ticks if t.get("unit") == "s" and t.get("done")]
    assert [t["done"] for t in segment_ticks] == [12.0, 30.0]
    assert {t["total"] for t in segment_ticks} == {90.0}


def test_EVERY_tick_can_rebuild_the_row_it_reports_to(worker, base, tmp_path):
    """The invariant, pinned over all of this process's reporters at once.

    The job manager evicts the least recently updated running row once
    `MAX_JOBS` bites, and a transcription QUEUE is what pushes the count past
    it — so any tick can be the one that has to re-create the row rather than
    update it. A tick without `title` is refused outright (`upsert` will not
    open a row without one), which kills the row permanently: the ✕ goes dead,
    `watch()` resolves null, and the page is told a run that succeeds minutes
    later has failed. `cancellable` and `unit` are the quieter half — they
    rebuild a row the manager draws with a dismiss cross instead of a cancel
    one, and a seconds clock reverted to a bare pair of numbers.

    Asserted over EVERY tick rather than over the ones a test happened to
    think of, because the failure is one reporter forgetting, and this file
    drives all four (the opening report, the eager-decode ticks, the
    orphan wait, and the per-segment loop).
    """
    model = FakeModel([FakeSegment(0.0, 1.5, "a"), FakeSegment(1.5, 3.0, "b")],
                      decode_seconds=0.2)
    worker._loaded["model"] = model
    worker._TICK_S = 0.05

    worker.generate(_request(tmp_path))

    assert len(base.ticks) >= 4, base.ticks
    for tick in base.ticks:
        missing = [k for k, v in ROW.items() if tick.get(k) != v]
        assert not missing, f"tick cannot rebuild its row, missing {missing}: {tick}"
        # `state` too: a row the manager has FORGOTTEN (aged out or dismissed)
        # only reopens for a report that says `running` outright — anything
        # else is answered as a late tick from work already closed.
        assert tick.get("state") == "running", tick


def test_the_orphan_wait_also_carries_the_row(worker, base, tmp_path):
    """The reporter that is easiest to forget, since it only runs after a
    cancel — exactly when nobody is looking."""
    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 3.0, "hi")],
                                        decode_seconds=0.4)
    worker._TICK_S = 0.05
    base.cancel_on_tick = 2
    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))

    base.cancel_on_tick = None
    base.ticks.clear()
    worker.generate(_request(tmp_path, out=str(tmp_path / "b.json"),
                             outText=str(tmp_path / "b.txt")))
    waits = [t for t in base.ticks if "cancelled decode" in str(t.get("detail"))]
    assert waits, base.ticks
    for tick in waits:
        assert tick["title"] == ROW["title"] and tick["cancellable"] is True


def test_every_tick_carries_the_job_the_route_opened(worker, base, tmp_path):
    """Per-request row, not the worker's own load row — two renders would
    otherwise overwrite each other's progress."""
    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    worker.generate(_request(tmp_path, job="sys:ai-transcribe:zzz"))
    assert {t["job"] for t in base.ticks} == {"sys:ai-transcribe:zzz"}


def test_a_cancel_on_the_LAST_segment_still_writes_the_finished_transcript(
        worker, base, tmp_path):
    """The decode is over; the ✕ arrived too late to save anything.

    The per-segment tick runs after the segment is appended, so a cancel
    landing on the FINAL one raised out of the loop and skipped the write
    entirely — an hour of decoding discarded at 99%, with nothing to show for
    it. A cancel can only be worth honouring when there is work left to stop.
    """
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 1.5, "hello"), FakeSegment(1.5, 3.0, "world")],
        duration=3.0)
    request = _request(tmp_path)
    # Ticks: one for the opening report, then one per segment — so this fires
    # on the second and last segment.
    base.cancel_on_tick = 3

    result = worker.generate(request)

    assert result["segments"] == 2
    assert json.load(open(request["out"], encoding="utf-8"))["text"] == "hello world"
    assert open(request["outText"], encoding="utf-8").read() == "hello world\n"


def test_a_DECODE_ERROR_in_the_cancel_probe_stays_a_cancel(worker, base, tmp_path):
    """The probe asks the generator for one more segment, and the tail of a file
    is exactly where a container or codec error surfaces.

    An exception there would REPLACE the `Cancelled` in flight, so
    `worker_base._single` answers `{"ok": false}` and the row ends in error —
    telling the user the transcription they cancelled had failed. A ✕ is the
    outcome; a fault in the code that checks whether the ✕ was worth honouring
    is not.
    """
    def exploding_tail():
        yield FakeSegment(0.0, 1.0, "one")
        raise RuntimeError("moov atom not found")

    model = FakeModel([])
    model.segments = exploding_tail()
    worker._loaded["model"] = model
    base.cancel_on_tick = 2

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))


def test_a_cancel_with_segments_still_to_come_is_honoured_and_writes_nothing(
        worker, base, tmp_path):
    """The other side of it: a cancel is real work stopped, and a half
    transcript presented as a whole one would be worse than none."""
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 1.0, "one"), FakeSegment(1.0, 2.0, "two"),
         FakeSegment(2.0, 3.0, "three")],
        duration=3.0)
    request = _request(tmp_path)
    base.cancel_on_tick = 2  # the first segment, with two still to decode

    with pytest.raises(base.Cancelled):
        worker.generate(request)
    assert not os.path.exists(request["out"])
    assert not os.path.exists(request["outText"])


def test_a_recording_with_NO_speech_writes_an_empty_transcript(worker, base, tmp_path):
    """Found auditing the cancel guards for a second route around them, and
    kept rather than closed — the behaviour is right, it was just untested.

    A recording the VAD finds no speech in yields zero segments, so the loop
    that carries the per-segment cancel check never runs. A ✕ pressed over such
    a run is therefore never honoured, and that is CONSISTENT with the rule the
    last-segment guard states: a cancel is only worth honouring while there is
    work left to stop, and here the decode is already over. The empty
    transcript is the honest answer to "what did this recording say", and an
    error would send the user hunting for a fault that is not there.
    """
    worker._loaded["model"] = FakeModel([], duration=42.0)
    request = _request(tmp_path)
    base.cancel_on_tick = 1

    result = worker.generate(request)

    assert result["segments"] == 0 and result["duration"] == 42.0
    assert json.load(open(request["out"], encoding="utf-8"))["text"] == ""
    assert open(request["outText"], encoding="utf-8").read() == "\n"


def test_the_clock_rolls_over_to_HOURS(worker):
    """`90:00` for ninety minutes is the same ambiguity as `720 / 5400`, and
    worse for sitting one line under `jobAmount` rendering it as `1:30:00`."""
    assert worker._clock(9) == "0:09"
    assert worker._clock(185) == "3:05"
    assert worker._clock(5400) == "1:30:00"
    assert worker._clock(3661) == "1:01:01"


def test_the_clock_ROUNDS_the_way_the_manager_does(worker):
    """Same invariant as the hours field, broken by a different half-second.

    `jobAmount`'s `clock()` uses Math.round; truncating here made the detail
    line disagree with the row above it on any fractional segment end — 89.6s
    reading "1:30" in the manager and "1:29" one line below. Segment ends are
    fractional essentially always, so this was the common case, not an edge."""
    assert worker._clock(89.6) == "1:30"
    assert worker._clock(89.4) == "1:29"
    assert worker._clock(3599.7) == "1:00:00"
    # An EXACT half, where Python's banker's `round` would disagree with
    # JavaScript's `Math.round`: round(88.5) is 88, Math.round(88.5) is 89.
    # Segment ends arrive at two decimals, so this is reachable, not academic.
    assert worker._clock(88.5) == "1:29"
    assert worker._clock(89.5) == "1:30"


def test_the_ETA_does_not_charge_the_decode_to_the_first_segment(worker, base, tmp_path):
    """The eager phase produced no segments, so counting its seconds against
    the first one makes the rate read as wildly slower than it is — a 90-minute
    file that takes ~18 minutes announced "~1079 min left"."""
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 1.0, "hi")], duration=100.0, decode_seconds=0.5)
    worker._TICK_S = 0.05

    worker.generate(_request(tmp_path))

    detail = [t["detail"] for t in base.ticks if "Transcribing" in str(t.get("detail"))][0]
    # 99 seconds of audio left, decoded at effectively zero elapsed — the ETA
    # must come from the transcribing rate, not from 0.5s spent before it.
    # Charging the decode would read ~50s here.
    match = re.search(r"~(\d+)s left", detail)
    assert match, detail
    assert int(match.group(1)) < 5, detail


# -- what lands on disk ---------------------------------------------------------


def test_both_transcript_files_are_written_where_the_server_said(worker, tmp_path):
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 1.5, " hello"), FakeSegment(1.5, 3.0, " world ")],
        duration=3.0)
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
    # The reply COUNTS the segments rather than carrying them: a 90-minute
    # recording is thousands, and the caller already has the file.
    assert result["segments"] == 2


def test_the_two_whisper_directions_reach_the_model(worker, tmp_path):
    model = FakeModel([FakeSegment(0.0, 1.0, "bonjour")])
    worker._loaded["model"] = model
    worker.generate(_request(tmp_path, task="translate", language="fr"))
    assert model.calls[0]["task"] == "translate"
    assert model.calls[0]["language"] == "fr"


def test_an_absent_language_means_auto_detect_not_an_empty_code(worker, tmp_path):
    """`""` would be passed through as a language code matching nothing;
    Whisper reads None as "detect it", which is the documented default."""
    model = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    worker._loaded["model"] = model
    worker.generate(_request(tmp_path, language=""))
    assert model.calls[0]["language"] is None


def test_an_explicit_null_vad_means_the_DEFAULT_not_off(worker, tmp_path):
    """`bool(body.get("vad", True))` reads a JSON null as False rather than as
    "not specified", so it inverted: a page spreading an options object with an
    unset key silently turned the VAD OFF. `task` and `language` use `or
    <default>` and are null-safe; this was the one that flipped."""
    for value, expected in ((None, True), (True, True), (False, False)):
        model = FakeModel([FakeSegment(0.0, 1.0, "hi")])
        worker._loaded["model"] = model
        worker.generate(_request(tmp_path, vad=value,
                                out=str(tmp_path / f"v{value}.json"),
                                outText=str(tmp_path / f"v{value}.txt")))
        assert model.calls[0]["vad_filter"] is expected, value
    # And an absent key is the same as an explicit null.
    model = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    worker._loaded["model"] = model
    worker.generate(_request(tmp_path))
    assert model.calls[0]["vad_filter"] is True


def test_generating_with_no_model_loaded_says_so(worker, tmp_path):
    with pytest.raises(RuntimeError):
        worker.generate(_request(tmp_path))


# -- speaker labels, and the waveform this runner does not normally hold ---------
#
# The diarization itself is `tests/test_ai_diarize.py`'s — the ONE module both
# engines import. What is driven here is this runner's particular problem:
# `transcribe()` is handed a PATH and decodes inside the library, so the samples
# sherpa-onnx needs do not exist in this process at all.


def _decoder(monkeypatch, samples=None, rate_seen=None):
    """Stand in for `faster_whisper.audio.decode_audio`, which `_waveform`
    imports at call time — so injecting the module is enough."""
    import numpy as np

    calls = []

    def decode_audio(input_file, sampling_rate=16000, **kwargs):
        calls.append({"path": input_file, "rate": sampling_rate})
        return np.zeros(samples if samples is not None else 160, dtype=np.float32)

    monkeypatch.setitem(sys.modules, "faster_whisper.audio",
                        types.SimpleNamespace(decode_audio=decode_audio))
    return calls


def _diarizes(monkeypatch, turns, seen=None):
    """The pipeline, with no sherpa-onnx and no models. `model_paths` stays
    REAL — the row its fetch reports to is one of the things under test."""
    import diarize as diarize_module

    # `seen if seen is not None`, never `seen or {}`: an empty dict is falsy,
    # so the `or` would hand every update to a throwaway and the caller's dict
    # would stay empty — a stub that records nothing and fails silently.
    record = seen if seen is not None else {}
    monkeypatch.setattr(diarize_module, "diarizer",
                        lambda seg, emb, speakers: record.update(
                            speakers=speakers) or object())
    monkeypatch.setattr(diarize_module, "speaker_turns",
                        lambda audio, session, rate, **kw: record.update(
                            samples=len(audio), rate=rate) or list(turns))


def test_diarization_is_OFF_unless_asked_for(worker, tmp_path, monkeypatch):
    """Additive, and the ordinary path does not even change shape: without
    `diarize` the PATH still goes to `transcribe()`, so nothing about an
    existing transcription is decoded, reported or written differently."""
    model = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    worker._loaded["model"] = model
    calls = _decoder(monkeypatch)
    request = _request(tmp_path)

    result = worker.generate(request)

    assert calls == [], "an undiarized run must not decode the file itself"
    assert model.calls[0]["source"] == request["path"]
    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert "speakers" not in written and "speakers" not in result
    assert "speaker" not in written["segments"][0]


def test_diarizing_decodes_ONCE_and_hands_the_ARRAY_to_transcribe(
        worker, tmp_path, monkeypatch):
    """The whole risk of putting diarization on this engine. `transcribe(path)`
    would decode a second time — a 90-minute recording twice over — so the
    samples come from faster-whisper's OWN `decode_audio` (the function
    `transcribe()` calls on a path) and go back to `transcribe()`, which takes
    an ndarray on the same argument. One decode, one library, no new dependency
    and no second definition of what 16 kHz mono means."""
    import numpy as np

    model = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    worker._loaded["model"] = model
    calls = _decoder(monkeypatch, samples=16000 * 4)
    seen = {}
    _diarizes(monkeypatch, [(0.0, 4.0, 0)], seen)
    request = _request(tmp_path, diarize=True, speakers=2)

    worker.generate(request)

    assert len(calls) == 1, calls
    assert calls[0]["path"] == request["path"]
    # …at the rate the MODEL wants, not a hardcoded one: `transcribe()` does not
    # expose `sampling_rate`, so an array decoded at another rate is a chipmunk
    # with no error anywhere.
    assert calls[0]["rate"] == model.feature_extractor.sampling_rate
    assert isinstance(model.calls[0]["source"], np.ndarray)
    assert len(model.calls[0]["source"]) == 16000 * 4
    # The pre-pass saw the SAME samples, whole — not the VAD's regions.
    assert seen["samples"] == 16000 * 4 and seen["rate"] == 16000


def test_the_speaker_count_reaches_the_clustering(worker, tmp_path, monkeypatch):
    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    _decoder(monkeypatch)
    seen = {}
    _diarizes(monkeypatch, [(0.0, 4.0, 0)], seen)

    worker.generate(_request(tmp_path, diarize=True, speakers=5))
    assert seen["speakers"] == 5


def test_an_ABSENT_count_reaches_the_clustering_as_None_to_estimate(
        worker, tmp_path, monkeypatch):
    """D318, on this engine too: no `speakers` key means the clustering is
    handed None and works the count out by distance. Both engines must mean the
    same thing by the same request (AI-10c)."""
    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    _decoder(monkeypatch)
    seen = {}
    _diarizes(monkeypatch, [(0.0, 4.0, 0)], seen)

    worker.generate(_request(tmp_path, diarize=True))
    assert seen["speakers"] is None


def test_an_ESTIMATED_count_is_reported_and_a_GIVEN_one_is_not(
        worker, tmp_path, monkeypatch):
    """The MLX runner's key, written by the same rule here (D318) — a page must
    not be able to tell which engine served it."""
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 2.0, "hello")], duration=20.0)
    _decoder(monkeypatch, samples=16000 * 20)
    _diarizes(monkeypatch, [(0.0, 10.0, 0), (10.0, 20.0, 1)])
    request = _request(tmp_path, diarize=True)

    result = worker.generate(request)

    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert result["estimatedSpeakers"] == 2
    assert written["estimatedSpeakers"] == 2
    # …and the legend stays the narrower list: only Speaker 1 said anything.
    assert written["speakers"] == ["Speaker 1"]


def test_a_run_that_was_GIVEN_the_count_reports_no_estimate(
        worker, tmp_path, monkeypatch):
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 2.0, "hello")], duration=20.0)
    _decoder(monkeypatch, samples=16000 * 20)
    _diarizes(monkeypatch, [(0.0, 10.0, 0), (10.0, 20.0, 1)])
    request = _request(tmp_path, diarize=True, speakers=2)

    result = worker.generate(request)

    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert "estimatedSpeakers" not in result
    assert "estimatedSpeakers" not in written


@pytest.mark.parametrize("speakers", [0, -1, True, 2.5, "2"])
def test_a_bad_speaker_count_is_refused_BEFORE_anything_is_decoded(
        worker, tmp_path, monkeypatch, speakers):
    """Neither the bridge nor the server is the only door into this process.
    `None` is absent from this list since D318 — it is the estimating path."""
    model = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    worker._loaded["model"] = model
    calls = _decoder(monkeypatch)

    with pytest.raises(ValueError, match="speakers"):
        worker.generate(_request(tmp_path, diarize=True, speakers=speakers))
    assert calls == [] and model.calls == []


def test_every_segment_is_LABELLED_and_the_json_gains_the_legend(
        worker, tmp_path, monkeypatch):
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 2.0, "hello"), FakeSegment(12.0, 14.0, "hi there")],
        duration=20.0)
    _decoder(monkeypatch, samples=16000 * 20)
    _diarizes(monkeypatch, [(0.0, 10.0, 0), (10.0, 20.0, 1)])
    request = _request(tmp_path, diarize=True, speakers=2)

    result = worker.generate(request)

    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert [s["speaker"] for s in written["segments"]] == ["Speaker 1", "Speaker 2"]
    assert written["speakers"] == ["Speaker 1", "Speaker 2"]
    assert result["speakers"] == ["Speaker 1", "Speaker 2"]


def test_the_pre_pass_does_NOT_inflate_the_progress_total(
        worker, base, tmp_path, monkeypatch):
    """`done`/`total` are SECONDS OF AUDIO of the TRANSCRIPT (AI-10a). A bar
    that counted the pre-pass would run to 100% before a word was decoded."""
    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 12.0, "hi")],
                                        duration=180.0)
    _decoder(monkeypatch)
    _diarizes(monkeypatch, [(0.0, 180.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2))

    totals = {t.get("total") for t in base.ticks if t.get("total") is not None}
    assert totals == {180.0}, totals
    # …and the stage is its own line with an indeterminate bar — what the job
    # record already offers for a phase with no position to report.
    finding = [t for t in base.ticks if t.get("detail") == "Finding speakers…"]
    assert finding, base.ticks
    assert all(t.get("done") is None and t.get("total") is None for t in finding)


def test_the_component_fetch_reports_to_the_row_the_USER_is_watching(
        worker, base, tmp_path, monkeypatch):
    """It happens inside a TRANSCRIPTION, so an unbound `download_file` ticks
    into this process's `JOB_ID` — the model's own load row, finished long ago."""
    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    _decoder(monkeypatch)
    _diarizes(monkeypatch, [(0.0, 4.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2,
                             job="sys:ai-transcribe:zzz"))

    assert len(base.fetches) == 2, base.fetches
    for fetch in base.fetches:
        assert fetch["job"] == "sys:ai-transcribe:zzz", fetch
        assert fetch["row"] == ROW, fetch


def test_BOTH_engines_fetch_the_SAME_two_component_repos(worker, base, tmp_path,
                                                         monkeypatch):
    """"Same models" is half of AI-10c's parity promise, and it is structural:
    both runners call `diarize.model_paths`, which reads the ids out of
    `formats.COMPONENT_REPOS`. This pins that the ids reaching the network are
    those two and not a copy that drifted."""
    import diarize as diarize_module

    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    _decoder(monkeypatch)
    _diarizes(monkeypatch, [(0.0, 4.0, 0)])

    worker.generate(_request(tmp_path, diarize=True, speakers=2))

    assert [(f["repo"], f["file"]) for f in base.fetches] == [
        (diarize_module.SEGMENTATION_REPO, diarize_module.SEGMENTATION_FILE),
        (diarize_module.EMBEDDING_REPO, diarize_module.EMBEDDING_FILE),
    ]


# -- the progressive transcript --------------------------------------------------
#
# `runners/partial.py` owns the line shape, the flush rule and the lifecycle,
# and `tests/test_ai_partial_transcript.py` drives all three directly. What is
# proved HERE is only what that file cannot see: that this engine actually
# feeds it, at the right moment, and that doing so leaves the transcript it
# always wrote untouched.


def _partial_lines(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_a_segment_is_on_disk_BEFORE_the_run_that_produced_it_ends(
        worker, base, tmp_path):
    """The whole feature in one assertion. Read from inside the decode — the
    tick that follows each segment is the only moment a test can observe a run
    that is still going — so this fails if the writer buffers, if it is fed
    after the loop, or if it is fed at all only on the way out."""
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 1.0, "one"), FakeSegment(1.0, 2.0, "two")],
        duration=2.0)
    request = _request(tmp_path, outPartial=str(tmp_path / "out.partial.jsonl"))
    mid_run = []
    ticked = base.report_or_cancel

    def peek(job=None, **fields):
        mid_run.append([line["text"] for line in _partial_lines(request["outPartial"])])
        return ticked(job=job, **fields)

    base.report_or_cancel = peek

    worker.generate(request)

    # One line after the first segment, two after the second — the transcript
    # accumulating, not appearing at the end.
    assert mid_run == [["one"], ["one", "two"]]


def test_the_partial_file_is_GONE_once_the_real_transcript_lands(
        worker, base, tmp_path):
    """It is duplicate bytes from the moment the `.json` exists, in a directory
    (`ai/transcripts/`) the user browses."""
    worker._loaded["model"] = FakeModel([FakeSegment(0.0, 1.0, "hi")], duration=1.0)
    request = _request(tmp_path, outPartial=str(tmp_path / "out.partial.jsonl"))

    worker.generate(request)

    assert os.path.exists(request["out"])
    assert not os.path.exists(request["outPartial"])


def test_a_run_that_DIES_leaves_what_it_had_decoded(worker, base, tmp_path):
    """The only salvage there is. A 90-minute recording that fails at minute 80
    writes no `.json` at all, and without this the 80 minutes go with it."""
    def dying():
        yield FakeSegment(0.0, 1.0, "one")
        yield FakeSegment(1.0, 2.0, "two")
        raise RuntimeError("the container is truncated")

    worker._loaded["model"] = FakeModel(dying(), duration=90.0)
    request = _request(tmp_path, outPartial=str(tmp_path / "out.partial.jsonl"))

    with pytest.raises(RuntimeError):
        worker.generate(request)

    assert not os.path.exists(request["out"]), "no half transcript as a whole one"
    assert [line["text"] for line in _partial_lines(request["outPartial"])] == [
        "one", "two"]


def test_a_CANCELLED_run_leaves_no_partial_file_either(worker, base, tmp_path):
    """A ✕ means the user does not want this transcript, and the run already
    refuses to write a partial one as `out` (the test above this section). The
    salvage file would quietly contradict that."""
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 1.0, "one"), FakeSegment(1.0, 2.0, "two"),
         FakeSegment(2.0, 3.0, "three")],
        duration=3.0)
    request = _request(tmp_path, outPartial=str(tmp_path / "out.partial.jsonl"))
    base.cancel_on_tick = 2  # the first segment, with two still to decode

    with pytest.raises(base.Cancelled):
        worker.generate(request)

    assert not os.path.exists(request["out"])
    assert not os.path.exists(request["outPartial"])


def test_the_FINAL_files_are_byte_identical_with_and_without_a_partial_one(
        worker, base, tmp_path):
    """The promise that makes this additive. Everything a caller has ever read
    off `output`/`outputText` is unchanged, down to the bytes — not "the same
    fields", which a reordered key or a different indent would also satisfy."""
    def run(**over):
        """The same request twice into the same directory — the second run
        overwrites the first — so the only difference between the two readings
        is `outPartial`, and not the absolute paths the transcript records."""
        worker._loaded["model"] = FakeModel(
            [FakeSegment(0.0, 1.5, " hello"), FakeSegment(1.5, 3.0, " wörld ")],
            duration=3.0)
        request = _request(tmp_path, **over)
        worker.generate(request)
        written = json.loads(open(request["out"], encoding="utf-8").read())
        # `seconds` is wall time and would differ between two runs whatever the
        # code did, so it is the one key blanked rather than pinned. Re-dumped
        # with the writer's own arguments, so a changed `indent` or a reordered
        # key still shows up as a byte difference.
        blank = json.dumps(written | {"seconds": 0}, ensure_ascii=False,
                           indent=1).encode()
        return blank, open(request["outText"], "rb").read()

    plain = run()
    progressive = run(outPartial=str(tmp_path / "out.partial.jsonl"))
    assert plain == progressive


def test_a_DIARIZED_partial_line_carries_the_speaker_it_will_end_up_with(
        worker, base, tmp_path, monkeypatch):
    """The turns exist before a word is decoded (D309), so a page tailing the
    file gets labels immediately rather than a transcript that gains speakers
    at the end. It is the same arithmetic, so the label does not change under
    the reader when the final file lands."""
    worker._loaded["model"] = FakeModel(
        [FakeSegment(0.0, 2.0, "mine"), FakeSegment(6.0, 7.0, "yours")],
        duration=7.0)
    _decoder(monkeypatch)
    _diarizes(monkeypatch, [(0.0, 5.0, 0), (5.0, 10.0, 1)])
    request = _request(tmp_path, diarize=True, speakers=2,
                       outPartial=str(tmp_path / "out.partial.jsonl"))
    seen = []
    ticked = base.report_or_cancel

    def peek(job=None, **fields):
        seen[:] = _partial_lines(request["outPartial"])
        return ticked(job=job, **fields)

    base.report_or_cancel = peek

    worker.generate(request)

    assert [(line["text"], line["speaker"]) for line in seen] == [
        ("mine", "Speaker 1"), ("yours", "Speaker 2")]
    written = json.load(open(request["out"], encoding="utf-8"))
    assert [s["speaker"] for s in written["segments"]] == ["Speaker 1", "Speaker 2"]


# -- the format trap ------------------------------------------------------------


def test_a_transformers_format_repo_is_named_as_the_cause(worker, tmp_path):
    """The AI Models page offers Load on anything whose TASK maps to a
    capability, and the format is not in the task — so `openai/whisper-large-v3`
    gets a button it cannot honour. The check runs BEFORE `faster_whisper` is
    imported, so the explanation does not depend on the runner environment
    being importable here."""
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "model.safetensors").write_bytes(b"")

    with pytest.raises(RuntimeError) as caught:
        worker.load("openai/whisper-large-v3", str(snapshot))
    message = str(caught.value)
    assert "model.bin" in message and "CTranslate2" in message
    assert "Systran/faster-whisper-small" in message


# -- Parakeet: the runner's SECOND format (sherpa-onnx transducer, D1423) --------
#
# What is faked is exactly what the real thing cannot be on CI: the recognizer
# (`sherpa_onnx`), the decoder (`faster_whisper.audio`) and Silero. Everything
# else — the packing, the remap through `vad.py`, the segment and word building,
# the ticks, the partial sink, the join with diarization — is the real code.


def _sherpa_files(folder):
    folder.mkdir(exist_ok=True)
    for name in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx",
                 "tokens.txt"):
        (folder / name).write_bytes(b"")
    return str(folder)


def _tokens(*spans):
    """`(start, "text")` pairs → sherpa's parallel `(tokens, timestamps)`, where a
    token that begins a word carries a LEADING SPACE (what 1.13.8 returns for
    Parakeet — measured; the SentencePiece `▁` is already decoded)."""
    return [text for _at, text in spans], [at for at, _text in spans]


class FakeSherpa:
    """`sherpa_onnx` — a recognizer that answers each decode from a queue
    (`results`: `(text, tokens, timestamps)`) and records what it was built with
    and handed."""

    def __init__(self):
        self.results = []
        self.built = []
        self.clips = []
        sherpa = self

        class Stream:
            def __init__(self):
                self.result = None

            def accept_waveform(self, rate, samples):
                sherpa.clips.append({"rate": rate, "seconds": len(samples) / rate})

        class Recognizer:
            def create_stream(self):
                return Stream()

            def decode_stream(self, stream):
                text, tokens, stamps = sherpa.results.pop(0)
                stream.result = types.SimpleNamespace(
                    text=text, tokens=tokens, timestamps=stamps)

        class OfflineRecognizer:
            @staticmethod
            def from_transducer(**kwargs):
                sherpa.built.append(kwargs)
                return sherpa.recognizer

        self.recognizer = Recognizer()
        self.OfflineRecognizer = OfflineRecognizer


@pytest.fixture()
def sherpa(monkeypatch):
    fake = FakeSherpa()
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    return fake


def _loaded_parakeet(worker, sherpa, tmp_path):
    worker.load("csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
                _sherpa_files(tmp_path / "snap"))


def _no_vad(monkeypatch, worker, regions=None):
    """Pin what the detector finds, as the MLX tests do (`_speech_regions` is
    the seam; Silero itself is `tests/test_ai_vad.py`'s)."""
    monkeypatch.setattr(worker, "_speech_regions", lambda *a, **k: regions)


def test_load_dispatches_on_FORMAT_a_sherpa_export_builds_a_transducer(
        worker, base, tmp_path, sherpa):
    import diarize as diarize_module

    _loaded_parakeet(worker, sherpa, tmp_path)

    (built,) = sherpa.built
    snap = str(tmp_path / "snap")
    assert built["encoder"] == os.path.join(snap, "encoder.int8.onnx")
    assert built["decoder"] == os.path.join(snap, "decoder.int8.onnx")
    assert built["joiner"] == os.path.join(snap, "joiner.int8.onnx")
    assert built["tokens"] == os.path.join(snap, "tokens.txt")
    # `nemo_transducer` is what makes sherpa read a NeMo TDT export; the default
    # `transducer` loads and then decodes garbage.
    assert built["model_type"] == "nemo_transducer"
    assert built["provider"] == "cpu"
    assert built["num_threads"] == diarize_module.NUM_THREADS
    assert worker._loaded["kind"] == "sherpa"
    assert worker._loaded["device"] == "cpu"
    assert base.state == {"device": "cpu"}
    assert worker._loaded["model"] is sherpa.recognizer


def test_load_of_a_CT2_snapshot_still_builds_a_whisper_model(
        worker, tmp_path, monkeypatch):
    snapshot = tmp_path / "ct2"
    snapshot.mkdir()
    (snapshot / "model.bin").write_bytes(b"")
    built = []
    monkeypatch.setitem(sys.modules, "ctranslate2", _fake_ctranslate2(0))
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(
        WhisperModel=lambda path, **kw: built.append((path, kw)) or "whisper"))

    worker.load("Systran/faster-whisper-small", str(snapshot))

    assert built == [(str(snapshot), {"device": "cpu", "compute_type": "int8"})]
    assert worker._loaded["model"] == "whisper"
    assert worker._loaded["kind"] == "ct2"


def test_the_refusal_names_all_three_models_and_both_formats(worker, tmp_path):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "model.safetensors").write_bytes(b"")

    with pytest.raises(RuntimeError) as caught:
        worker.load("openai/whisper-large-v3", str(snapshot))

    message = str(caught.value)
    assert "deepdml/faster-whisper-large-v3-turbo-ct2" in message
    assert "Systran/faster-whisper-small" in message
    assert "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8" in message
    assert "sherpa-onnx" in message and "CTranslate2" in message


def test_a_half_a_sherpa_export_is_refused_not_loaded(worker, tmp_path):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "encoder.int8.onnx").write_bytes(b"")
    with pytest.raises(RuntimeError):
        worker.load("x/y", str(snapshot))


def test_the_refusal_in_the_worker_names_only_models_the_catalog_offers():
    from fused_render.ai import catalog

    offered = {row["id"] for row in catalog.SUGGESTIONS["faster-whisper"]}
    assert "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8" in offered
    assert "deepdml/faster-whisper-large-v3-turbo-ct2" in offered
    assert "Systran/faster-whisper-small" in offered


def test_parakeet_refuses_translate_before_any_work(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    calls = _decoder(monkeypatch)

    with pytest.raises(ValueError) as caught:
        worker.generate(_request(tmp_path, task="translate"))

    assert "Parakeet transcribes only" in str(caught.value)
    assert "deepdml/faster-whisper-large-v3-turbo-ct2" in str(caught.value)
    assert calls == [] and base.ticks == []


def test_the_VAD_packs_become_segments_on_the_RECORDING_clock(
        worker, base, tmp_path, sherpa, monkeypatch):
    """One pack of two regions, 10-13s and 20-22s of the recording, decoded as a
    5-second clip. The second sentence starts 3.0s into the clip — exactly on
    the join — so it belongs to the region that BEGINS there (20.0), and the
    first one's end, being the next sentence's start, to the region that ENDS
    there (13.0), not stretched across the dropped silence."""
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 30)
    _no_vad(monkeypatch, worker, regions=[(10.0, 13.0), (20.0, 22.0)])
    tokens, stamps = _tokens((0.0, " First"), (0.8, " sentence"), (1.6, "."),
                             (3.0, " Second"), (3.8, " one"), (4.2, "."))
    sherpa.results = [("First sentence. Second one.", tokens, stamps)]

    result = worker.generate(_request(tmp_path))

    assert [c["seconds"] for c in sherpa.clips] == [5.0]
    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    assert [(s["start"], s["end"], s["text"]) for s in written["segments"]] == [
        (10.0, 13.0, "First sentence."), (20.0, 22.0, "Second one.")]
    assert result["segments"] == 2
    assert written["text"] == "First sentence. Second one."


def _detector_fetches(monkeypatch, error=None):
    import vad as vad_module

    fetched = []

    def model_path(fetch):
        fetched.append(fetch)
        if error is not None:
            raise error
        return "/silero.onnx"

    monkeypatch.setattr(vad_module, "model_path", model_path)
    return fetched


def test_downloading_a_sherpa_repo_also_prefetches_the_speech_detector(
        worker, base, tmp_path, monkeypatch):
    """Parakeet leans on Silero for its cuts, so a "Download" that leaves the
    detector unfetched degrades the first OFFLINE run to fixed 29 s slices."""
    base.snapshot_dir = _sherpa_files(tmp_path / "snap")
    fetched = _detector_fetches(monkeypatch)

    assert worker.download("csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8") \
        == base.snapshot_dir
    assert fetched == [base.download_file]


def test_a_failing_detector_prefetch_does_not_fail_the_download(
        worker, base, tmp_path, monkeypatch):
    base.snapshot_dir = _sherpa_files(tmp_path / "snap")
    _detector_fetches(monkeypatch, error=OSError("hub down"))

    assert worker.download("x/parakeet") == base.snapshot_dir


def test_a_cancel_during_the_detector_prefetch_stands(
        worker, base, tmp_path, monkeypatch):
    base.snapshot_dir = _sherpa_files(tmp_path / "snap")
    _detector_fetches(monkeypatch, error=base.Cancelled())

    with pytest.raises(base.Cancelled):
        worker.download("x/parakeet")


def test_downloading_a_whisper_repo_does_not_fetch_the_detector(
        worker, base, tmp_path, monkeypatch):
    """faster-whisper ships its own Silero; only the sherpa path needs ours."""
    snap = tmp_path / "ct2"
    snap.mkdir()
    (snap / worker._CT2_WEIGHTS).write_bytes(b"")
    base.snapshot_dir = str(snap)
    fetched = _detector_fetches(monkeypatch)

    worker.download("Systran/faster-whisper-small")

    assert fetched == []


def test_a_word_is_a_space_led_token_run_with_its_end_at_the_next_start(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 4)
    _no_vad(monkeypatch, worker)
    tokens, stamps = _tokens((0.0, " A"), (0.24, "sk"), (0.4, " not"),
                             (0.64, " what"), (1.0, "?"))
    sherpa.results = [("Ask not what?", tokens, stamps)]

    worker.generate(_request(tmp_path, words=True))

    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    (segment,) = written["segments"]
    assert segment["text"] == "Ask not what?"
    assert segment["words"] == [
        {"start": 0.0, "end": 0.4, "word": " Ask"},
        {"start": 0.4, "end": 0.64, "word": " not"},
        # The punctuation token has no leading space, so it belongs to "what".
        {"start": 0.64, "end": 4.0, "word": " what?"},
    ]


def test_words_are_monotonic_and_inside_their_segment_across_joins(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 40)
    _no_vad(monkeypatch, worker, regions=[(2.0, 4.0), (30.0, 33.0)])
    # A word straddling the join at clip 2.0 must not be stretched over 26s.
    tokens, stamps = _tokens((0.0, " one"), (1.9, " straddle"), (2.4, " three"),
                             (3.5, "."))
    sherpa.results = [("one straddle three.", tokens, stamps)]

    worker.generate(_request(tmp_path, words=True))

    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    (segment,) = written["segments"]
    words = segment["words"]
    assert len(words) == 3
    previous = segment["start"]
    for word in words:
        assert segment["start"] <= word["start"] <= word["end"] <= segment["end"]
        assert word["start"] >= previous
        previous = word["start"]
    assert max(w["end"] - w["start"] for w in words) < 3.0


def test_words_are_ABSENT_unless_asked_for(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 2)
    _no_vad(monkeypatch, worker)
    sherpa.results = [("Hi.", *_tokens((0.0, " Hi"), (0.3, ".")))]

    worker.generate(_request(tmp_path))

    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    assert "words" not in written["segments"][0]


def test_without_a_detector_a_long_file_is_sliced_to_the_vad_budget(
        worker, base, tmp_path, sherpa, monkeypatch):
    """`vad: false` (and a detector that could not be fetched) must still never
    hand sherpa an unbounded utterance: one pack per BUDGET_S of recording."""
    import vad as vad_module

    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 70)
    sherpa.results = [("a.", *_tokens((0.0, " a"), (0.2, "."))),
                      ("b.", *_tokens((0.0, " b"), (0.2, "."))),
                      ("c.", *_tokens((0.0, " c"), (0.2, ".")))]
    never = []
    monkeypatch.setattr(worker, "_speech_regions",
                        lambda *a, **k: never.append(1) or [])

    worker.generate(_request(tmp_path, vad=False))

    assert never == [], "vad: false must not even consult the detector"
    assert [c["seconds"] for c in sherpa.clips] == [
        vad_module.BUDGET_S, vad_module.BUDGET_S, 70.0 - 2 * vad_module.BUDGET_S]
    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    assert [s["start"] for s in written["segments"]] == [
        0.0, vad_module.BUDGET_S, 2 * vad_module.BUDGET_S]


def test_one_long_speech_region_is_cut_at_the_budget_never_one_utterance(
        worker, base, tmp_path, sherpa, monkeypatch):
    """`vad.pack_regions` lets an over-budget region through alone; sherpa's
    memory grows with the utterance, so an 8-minute monologue must not."""
    import vad as vad_module

    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 490)
    _no_vad(monkeypatch, worker, regions=[(5.0, 485.0)])
    count = 17  # ceil(480 / 29)
    sherpa.results = [(f"w{n}.", *_tokens((0.0, f" w{n}"), (0.2, ".")))
                      for n in range(count)]

    worker.generate(_request(tmp_path))

    seconds = [c["seconds"] for c in sherpa.clips]
    assert len(seconds) == count
    assert all(0 < s <= vad_module.BUDGET_S for s in seconds)
    assert sum(seconds) == pytest.approx(480.0)
    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    starts = [seg["start"] for seg in written["segments"]]
    assert starts == [round(5.0 + n * vad_module.BUDGET_S, 2) for n in range(count)]


def test_bounded_packs_are_contiguous_cover_the_region_and_leave_small_ones_alone(
        worker):
    import vad as vad_module

    budget = vad_module.BUDGET_S
    packs = worker._bounded_packs([[(5.0, 485.0)], [(500.0, 510.0), (520.0, 530.0)]])

    long_part, small = packs[:-1], packs[-1]
    assert small == [(500.0, 510.0), (520.0, 530.0)]
    assert all(len(p) == 1 for p in long_part)
    assert long_part[0][0][0] == 5.0 and long_part[-1][0][1] == 485.0
    for before, after in zip(long_part, long_part[1:]):
        assert before[0][1] == after[0][0]
    assert all(p[0][1] - p[0][0] <= budget for p in long_part)


def test_only_a_missing_or_empty_detector_result_is_time_sliced(
        worker, base, tmp_path, sherpa, monkeypatch):
    """The branch is on what the detector RETURNED, not on the pack's shape: a
    region that happens to span the whole file is the detector's answer."""
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 10)
    sliced = []
    real = worker._time_slices
    monkeypatch.setattr(worker, "_time_slices",
                        lambda duration: sliced.append(duration) or real(duration))
    for regions, expect_sliced in (([(0.0, 10.0)], False), (None, True), ([], True)):
        sliced.clear()
        _no_vad(monkeypatch, worker, regions=regions)
        sherpa.results = [("a.", *_tokens((0.0, " a"), (0.2, ".")))]
        worker.generate(_request(tmp_path))
        assert bool(sliced) is expect_sliced, regions
    sliced.clear()
    sherpa.results = [("a.", *_tokens((0.0, " a"), (0.2, ".")))]
    worker.generate(_request(tmp_path, vad=False))
    assert sliced == [10.0]


def test_a_detector_that_is_unavailable_degrades_to_the_whole_file(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 10)
    _no_vad(monkeypatch, worker, regions=None)
    sherpa.results = [("Hello.", *_tokens((0.5, " Hello"), (1.0, ".")))]

    worker.generate(_request(tmp_path))

    assert [c["seconds"] for c in sherpa.clips] == [10.0]


def test_a_pack_that_decodes_to_nothing_adds_no_segment(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 10)
    _no_vad(monkeypatch, worker, regions=[(0.0, 2.0), (40.0, 41.0)])
    sherpa.results = [("", [], [])]

    result = worker.generate(_request(tmp_path))

    assert result["segments"] == 0
    assert open(_request(tmp_path)["outText"]).read() == "\n"


def test_a_result_with_no_usable_timestamps_is_one_segment_over_the_pack(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 6)
    _no_vad(monkeypatch, worker)
    sherpa.results = [("No times here.", [], [])]

    worker.generate(_request(tmp_path, words=True))

    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    (segment,) = written["segments"]
    assert (segment["start"], segment["end"], segment["text"]) == (
        0.0, 6.0, "No times here.")
    # Asked for and not timed is an EMPTY list (`_ct2_words`' rule), never a
    # missing key — "none were timed" must not read as "this engine declined".
    assert segment["words"] == []

    sherpa.results = [("No times here.", [], [])]
    worker.generate(_request(tmp_path))
    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    assert "words" not in written["segments"][0]


def test_the_reply_language_is_none_because_sherpa_reports_none(
        worker, base, tmp_path, sherpa, monkeypatch):
    """sherpa reports no language, so the reply must not invent one — and an
    "auto" request is the one spelling of `language` that is not refused."""
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000)
    _no_vad(monkeypatch, worker)
    sherpa.results = [("Hi.", *_tokens((0.0, " Hi"), (0.3, "."))),
                      ("Hi.", *_tokens((0.0, " Hi"), (0.3, ".")))]

    assert worker.generate(_request(tmp_path))["language"] is None
    assert worker.generate(_request(tmp_path, language="auto"))["language"] is None


@pytest.mark.parametrize("option,value", [("language", "de"),
                                          ("initialPrompt", "Acme, Fused")])
def test_parakeet_refuses_language_and_initial_prompt_before_any_work(
        worker, base, tmp_path, sherpa, monkeypatch, option, value):
    """Accepted-and-ignored is the failure `engine_options` rates worst: a page
    that pinned German and got a guess has nothing on the reply to tell it."""
    _loaded_parakeet(worker, sherpa, tmp_path)
    calls = _decoder(monkeypatch)

    with pytest.raises(ValueError) as caught:
        worker.generate(_request(tmp_path, **{option: value}))

    assert "Parakeet" in str(caught.value) and option in str(caught.value)
    assert "deepdml/faster-whisper-large-v3-turbo-ct2" in str(caught.value)
    assert calls == [] and base.ticks == []


def test_each_pack_ticks_seconds_of_audio_and_a_cancel_between_packs_stops(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 100)
    _no_vad(monkeypatch, worker, regions=[(0.0, 25.0), (50.0, 75.0)])
    sherpa.results = [("a.", *_tokens((0.0, " a"), (0.2, "."))),
                      ("b.", *_tokens((0.0, " b"), (0.2, ".")))]

    worker.generate(_request(tmp_path))

    ticks = [t for t in base.ticks if t.get("detail", "").startswith("Transcribing")]
    assert [(t["done"], t["total"]) for t in ticks] == [(25.0, 100.0), (75.0, 100.0)]
    assert all(t["job"] == "sys:ai-transcribe:abc" for t in ticks)

    sherpa.results = [("a.", *_tokens((0.0, " a"), (0.2, "."))),
                      ("b.", *_tokens((0.0, " b"), (0.2, ".")))]
    base.ticks.clear()
    base.cancel_on_tick = 1
    request = _request(tmp_path, out=str(tmp_path / "c.json"),
                       outText=str(tmp_path / "c.txt"))
    with pytest.raises(base.Cancelled):
        worker.generate(request)
    assert not os.path.exists(request["out"])


def _cancel_during_decode(sherpa, base, nth):
    """Make the `nth` decode (1-based) outlast a tick, with the ✕ landing on it:
    the next `report_or_cancel` answers "cancelled", as the real reply does."""
    import time as time_module

    recognizer = sherpa.recognizer
    real = type(recognizer).decode_stream
    seen = []

    def decode_stream(stream):
        seen.append(1)
        if len(seen) == nth:
            base.cancel_on_tick = 1
            time_module.sleep(0.15)
        return real(recognizer, stream)

    recognizer.decode_stream = decode_stream


def test_a_cancel_during_the_LAST_pack_decode_is_late_and_keeps_the_transcript(
        worker, base, tmp_path, sherpa, monkeypatch):
    """CT2 and MLX both refuse to throw away a finished transcript at 99%; the
    ✕ pressed while the last pack is decoding is the same case."""
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 100)
    _no_vad(monkeypatch, worker, regions=[(0.0, 25.0), (50.0, 75.0)])
    monkeypatch.setattr(worker, "_TICK_S", 0.01)
    sherpa.results = [("a.", *_tokens((0.0, " a"), (0.2, "."))),
                      ("b.", *_tokens((0.0, " b"), (0.2, ".")))]
    _cancel_during_decode(sherpa, base, nth=2)
    request = _request(tmp_path)

    result = worker.generate(request)

    assert result["segments"] == 2
    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert [s["text"] for s in written["segments"]] == ["a.", "b."]


def test_a_cancel_during_an_EARLIER_pack_decode_still_stops_the_run(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 100)
    _no_vad(monkeypatch, worker, regions=[(0.0, 25.0), (50.0, 75.0)])
    monkeypatch.setattr(worker, "_TICK_S", 0.01)
    sherpa.results = [("a.", *_tokens((0.0, " a"), (0.2, "."))),
                      ("b.", *_tokens((0.0, " b"), (0.2, ".")))]
    _cancel_during_decode(sherpa, base, nth=1)
    request = _request(tmp_path)

    with pytest.raises(base.Cancelled):
        worker.generate(request)
    assert not os.path.exists(request["out"])


def test_a_trailing_pack_too_short_to_decode_still_advances_the_bar_to_the_end(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 40)
    # 29s fills the budget, so the 0.05s region is its own (undecodable) pack.
    _no_vad(monkeypatch, worker, regions=[(0.0, 29.0), (35.0, 35.05)])
    sherpa.results = [("a.", *_tokens((0.0, " a"), (0.2, ".")))]

    worker.generate(_request(tmp_path))

    ticks = [t for t in base.ticks if t.get("detail", "").startswith("Transcribing")]
    assert [t["done"] for t in ticks] == [29.0, 35.05]
    assert len(sherpa.clips) == 1


def test_parakeet_segments_reach_the_partial_file_as_each_pack_lands(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    _decoder(monkeypatch, samples=16000 * 70)
    _no_vad(monkeypatch, worker, regions=[(0.0, 20.0), (30.0, 50.0), (60.0, 70.0)])
    # 20s + 20s don't fit one 29s budget, so three single-region packs.
    sherpa.results = [("one.", *_tokens((0.0, " one"), (0.2, "."))),
                      ("two.", *_tokens((0.0, " two"), (0.2, "."))),
                      ("three.", *_tokens((0.0, " three"), (0.2, ".")))]
    request = _request(tmp_path, outPartial=str(tmp_path / "out.partial.jsonl"))
    mid_run = []
    ticked = base.report_or_cancel

    def peek(job=None, **fields):
        mid_run.append([l["text"] for l in _partial_lines(request["outPartial"])])
        return ticked(job=job, **fields)

    base.report_or_cancel = peek

    worker.generate(request)

    assert mid_run == [["one."], ["one.", "two."], ["one.", "two.", "three."]]
    assert not os.path.exists(request["outPartial"])


def test_parakeet_diarization_decodes_once_and_labels_the_segments(
        worker, base, tmp_path, sherpa, monkeypatch):
    _loaded_parakeet(worker, sherpa, tmp_path)
    calls = _decoder(monkeypatch, samples=16000 * 20)
    seen = {}
    _diarizes(monkeypatch, [(0.0, 10.0, 0), (10.0, 20.0, 1)], seen)
    _no_vad(monkeypatch, worker, regions=[(0.0, 5.0), (12.0, 17.0)])
    sherpa.results = [("Hello there. Hi back.", *_tokens(
        (0.0, " Hello"), (0.5, " there"), (1.0, "."), (5.0, " Hi"), (5.5, " back"),
        (6.0, ".")))]
    request = _request(tmp_path, diarize=True, speakers=2)

    worker.generate(request)

    assert len(calls) == 1, "the file is decoded ONCE for VAD, diarization and Parakeet"
    assert seen["samples"] == 16000 * 20 and seen["speakers"] == 2
    written = json.loads(open(request["out"], encoding="utf-8").read())
    assert [s["speaker"] for s in written["segments"]] == ["Speaker 1", "Speaker 2"]
    assert written["speakers"] == ["Speaker 1", "Speaker 2"]


def test_the_ct2_path_asks_for_word_timestamps_only_when_words_are_wanted(
        worker, tmp_path):
    model = FakeModel([FakeSegment(0.0, 1.0, "hi")])
    worker._loaded["model"] = model

    worker.generate(_request(tmp_path))
    worker.generate(_request(tmp_path, words=True))
    worker.generate(_request(tmp_path, words=True, task="translate"))

    assert [c.get("word_timestamps", False) for c in model.calls] == [
        False, True, False]


def test_the_ct2_path_maps_segment_words_into_the_shared_shape(worker, tmp_path):
    segment = FakeSegment(0.0, 1.0, " hello there")
    segment.words = [
        types.SimpleNamespace(start=0.0, end=0.5, word=" hello", probability=0.9),
        types.SimpleNamespace(start=0.5123, end=1.0, word=" there", probability=0.8),
    ]
    worker._loaded["model"] = FakeModel([segment])

    worker.generate(_request(tmp_path, words=True))

    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    # No `probability`: it is an engine-tell (AI-10c), and 2dp like everything.
    assert written["segments"][0]["words"] == [
        {"start": 0.0, "end": 0.5, "word": " hello"},
        {"start": 0.51, "end": 1.0, "word": " there"}]


def test_the_ct2_path_writes_no_words_key_when_not_asked(worker, tmp_path):
    segment = FakeSegment(0.0, 1.0, "hi")
    segment.words = [types.SimpleNamespace(start=0.0, end=1.0, word=" hi")]
    worker._loaded["model"] = FakeModel([segment])

    worker.generate(_request(tmp_path))

    written = json.loads(open(_request(tmp_path)["out"], encoding="utf-8").read())
    assert "words" not in written["segments"][0]
