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
        #: Set by a test to have the NEXT tick answer "the ✕ was pressed",
        #: which is how a real cancel reaches a worker (the reply to the tick
        #: it was sending anyway).
        self.cancel_on_tick = None

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
    assert "Systran/faster-whisper-large-v3" in message
