"""The speech detector's region logic (SPEC AI-10f).

`vad.py` is two things bolted together: an ONNX session that turns 512-sample
windows into speech probabilities, and the arithmetic that turns a list of
probabilities into a list of regions. The first needs onnxruntime and a 2MB
model download, so it cannot run on CI — and it is also the part with no
decisions in it. The second is where every judgement lives (how long a silence
has to be before it ends a region, how short a run is noise, how much padding
to keep), it is pure arithmetic over a list of floats, and it is what this file
drives with the session stubbed out.

The real model WAS driven by hand on this machine before any of this was
written: `say`-generated speech + 4s of digital silence + the same speech again,
19.51s total, came back as [(0.0, 7.98), (11.61, 19.49)] against a truth of
0-7.76 and 11.76-19.51 — the two halves found, the gap dropped, the edges
widened by the padding below.
"""
import importlib.util
import os

import numpy as np
import pytest

VAD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "vad.py",
)


@pytest.fixture(scope="module")
def vad():
    """Imported by path, the way a runner reaches it: it sits at the runners
    ROOT (D319, since a second engine needed it) and is reached through the
    same `sys.path` insert that reaches `worker_base`, never as
    `fused_render.ai.runners.…`."""
    spec = importlib.util.spec_from_file_location("runners_vad", VAD_PATH)
    assert spec is not None and spec.loader is not None, VAD_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_runner_carries_a_COPY_of_the_detector():
    """The structural half of "vad: true means one thing" (AI-10f, D319).

    Silero moved to the runners root the moment a second engine needed it, and
    a `vad.py` back inside a runner folder is the drift that move exists to
    prevent: two copies of the threshold, the minimum silence and the padding,
    neither of which would fail a behavioural test because each would pass its
    own.
    """
    runners = os.path.dirname(VAD_PATH)
    for name in sorted(os.listdir(runners)):
        folder = os.path.join(runners, name)
        if not os.path.isfile(os.path.join(folder, "worker.py")):
            continue
        assert not os.path.exists(os.path.join(folder, "vad.py")), name


class FakeSession:
    """The ONNX session, returning a scripted probability per window.

    Records the STATE it was handed each time, because threading the recurrent
    state through is the one thing about the real model that a stub can still
    get wrong in the code under test.
    """

    def __init__(self, probabilities):
        self.probabilities = list(probabilities)
        self.states = []
        self.rates = []
        self._index = 0

    def run(self, outputs, feed):
        self.states.append(feed["state"].copy())
        self.rates.append(int(feed["sr"]))
        probability = self.probabilities[min(self._index, len(self.probabilities) - 1)]
        self._index += 1
        # The state the real model returns is a function of what it has seen;
        # anything that is not the zero state proves it was carried.
        state = np.full((2, 1, 128), float(self._index), dtype=np.float32)
        return np.array([[probability]], dtype=np.float32), state


def audio_for(windows, vad):
    """Enough silence to make `windows` calls — the samples are never read by
    the fake, only their length is."""
    return np.zeros(windows * vad.WINDOW, dtype=np.float32)


def per_window(vad):
    return vad.WINDOW / 16000  # 0.032s


# -- the detector's own wiring ---------------------------------------------------


def test_the_recurrent_state_is_CARRIED_between_windows(vad):
    """Silero is a streaming model: 512 samples in, one probability out, and a
    state that has to reach the next call. Passing a fresh state each time
    quietly degrades it to a frame-energy detector — which still returns
    plausible numbers, which is what makes it worth pinning."""
    session = FakeSession([0.9] * 5)
    vad.speech_regions(audio_for(5, vad), session)

    assert len(session.states) == 5
    assert not session.states[0].any(), "the first window starts from zero state"
    assert session.states[1].any(), "the second window got a fresh zero state"
    assert [s.flat[0] for s in session.states[1:]] == [1.0, 2.0, 3.0, 4.0]


def test_the_sample_rate_is_passed_as_the_model_expects(vad):
    session = FakeSession([0.1])
    vad.speech_regions(audio_for(1, vad), session)
    assert session.rates == [16000]


def test_a_trailing_PARTIAL_window_is_dropped_not_padded(vad):
    """32ms cannot move a boundary that `PAD_S` already widens by 200ms, and
    zero-padding it would feed the detector a discontinuity it was never
    trained on."""
    session = FakeSession([0.9] * 10)
    audio = np.zeros(3 * vad.WINDOW + 100, dtype=np.float32)
    vad.speech_regions(audio, session)
    assert len(session.states) == 3


# -- the arithmetic that turns probabilities into regions ------------------------


def test_speech_becomes_ONE_region_with_padding_either_side(vad):
    """A detector tuned to find speech clips its own edges: the onset of the
    first consonant and the tail of the last vowel fall below the threshold,
    and they are exactly the samples a transcript needs."""
    # 10 windows of speech, with enough trailing silence to actually END the
    # region — under `MIN_SILENCE_S` of it and the region would run to the end
    # of the recording instead, which is a different case (tested below).
    probabilities = [0.0] * 10 + [0.9] * 10 + [0.0] * 25
    session = FakeSession(probabilities)
    regions = vad.speech_regions(audio_for(len(probabilities), vad), session)

    assert len(regions) == 1
    start, end = regions[0]
    # The end is EXCLUSIVE — the boundary after the last speech window, not its
    # start — so ten windows of speech beginning at window 10 end at window 20.
    speech_start, speech_end = 10 * per_window(vad), 20 * per_window(vad)
    assert start == pytest.approx(speech_start - vad.PAD_S, abs=1e-6)
    assert end == pytest.approx(speech_end + vad.PAD_S, abs=1e-6)


def test_a_SHORT_gap_does_not_split_a_region(vad):
    """Every pause between two words would otherwise become a boundary, and the
    recording would be cut into hundreds of fragments — which costs accuracy
    (each fragment is transcribed with no context) far more than it saves."""
    short = int(0.2 / per_window(vad))  # 200ms, under MIN_SILENCE_S
    probabilities = [0.9] * 10 + [0.0] * short + [0.9] * 10
    regions = vad.speech_regions(audio_for(len(probabilities), vad),
                                 FakeSession(probabilities))
    assert len(regions) == 1


def test_a_LONG_gap_does_split_it(vad):
    long_gap = int(1.0 / per_window(vad))
    probabilities = [0.9] * 20 + [0.0] * long_gap + [0.9] * 20
    regions = vad.speech_regions(audio_for(len(probabilities), vad),
                                 FakeSession(probabilities))
    assert len(regions) == 2
    # …and they are ordered and disjoint, which everything downstream — the
    # progress mapping and the timestamp remap — relies on.
    assert regions[0][1] < regions[1][0]


def test_a_BLIP_of_speech_is_dropped(vad):
    """A click, a breath, a door. Transcribing it produces a hallucinated word
    at a timestamp where nobody spoke."""
    blip = max(1, int(0.1 / per_window(vad)))
    probabilities = [0.0] * 20 + [0.9] * blip + [0.0] * 20
    assert vad.speech_regions(audio_for(len(probabilities), vad),
                              FakeSession(probabilities)) == []


def test_todays_constants_cannot_produce_an_overlap(vad):
    """The invariant that makes the merge below unreachable — asserted so that
    changing either number is a decision rather than an accident.

    A split needs `MIN_SILENCE_S` of quiet; padding closes `2 * PAD_S`. While
    the first exceeds the second, two regions can never touch.
    """
    assert vad.MIN_SILENCE_S > 2 * vad.PAD_S


def test_regions_that_PADDING_makes_touch_are_merged(vad, monkeypatch):
    """…and the merge is kept anyway, because those two numbers are tuned
    independently. Turn the padding up and it becomes live at once — without
    it, that edit would produce overlapping regions and a transcript whose
    segments run backwards after the remap.

    Driven with the padding raised past the invariant above, which is the exact
    edit this guard is defending against.
    """
    monkeypatch.setattr(vad, "PAD_S", vad.MIN_SILENCE_S)
    gap = int((vad.MIN_SILENCE_S + 0.05) / per_window(vad))
    probabilities = [0.9] * 20 + [0.0] * gap + [0.9] * 20
    regions = vad.speech_regions(audio_for(len(probabilities), vad),
                                 FakeSession(probabilities))

    assert len(regions) == 1, "two overlapping regions were left in the list"


def test_speech_running_to_the_very_END_is_closed_off(vad):
    """A recording that ends mid-sentence has no trailing silence to end its
    last region, and a region left open would be dropped entirely."""
    probabilities = [0.0] * 20 + [0.9] * 20
    regions = vad.speech_regions(audio_for(40, vad), FakeSession(probabilities))
    assert len(regions) == 1
    assert regions[0][1] == pytest.approx(40 * per_window(vad), abs=1e-6)


def test_padding_never_runs_past_the_ENDS_of_the_recording(vad):
    """A negative start, or an end past the duration, becomes a slice that is
    empty or short — and the progress clamp would then report a position
    outside the file."""
    probabilities = [0.9] * 30
    regions = vad.speech_regions(audio_for(30, vad), FakeSession(probabilities))
    assert regions[0][0] == 0.0
    assert regions[0][1] <= 30 * per_window(vad)


def test_a_recording_with_NO_speech_returns_no_regions(vad):
    """Empty, not "the whole file" — the caller decides what nothing means, and
    `worker.py` decides to transcribe everything rather than believe it."""
    assert vad.speech_regions(audio_for(20, vad), FakeSession([0.0] * 20)) == []


def test_the_threshold_is_the_one_faster_whisper_uses(vad):
    """Both engines have to draw the line in the same place, or `vad: true`
    still means two things."""
    assert vad.THRESHOLD == 0.5


# -- slicing ---------------------------------------------------------------------


def test_slicing_takes_the_samples_the_region_names(vad):
    audio = np.arange(16000 * 4, dtype=np.float32)
    clip = vad.slice_samples(audio, (1.0, 2.5))
    assert len(clip) == int(1.5 * 16000)
    assert clip[0] == 16000


def test_slicing_is_clamped_to_the_audio_it_has(vad):
    """A padded region can name an end past the array; NumPy would silently
    return a short slice, which is fine — what must not happen is a negative
    start wrapping around to the END of the recording."""
    audio = np.arange(16000, dtype=np.float32)
    assert len(vad.slice_samples(audio, (-1.0, 0.5))) == 8000
    assert len(vad.slice_samples(audio, (0.5, 99.0))) == 8000
