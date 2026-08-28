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


# -- packing regions into as few 30-second windows as possible -------------------
#
# mlx-whisper pads every `transcribe()` call's mel to `N_FRAMES = 3000` — 30
# seconds — so a 0.8-second region costs the same encoder pass as a 30-second
# one. Decoding one region per call made `vad: true` a PESSIMISATION on the
# large models: measured on a 216-second recording that is 92% speech (31
# regions, min 0.8s, median 5.8s, max 14.0s), `large-v3-turbo` took 8.32s for
# the whole file, 23.30s for the 31 raw regions and 9.31s once packed. These
# functions are the packing, and they live here rather than in the runner
# because `vad: true` means one thing for both MLX engines (AI-10f).


def test_no_regions_pack_into_no_clips(vad):
    """The empty list, not a clip covering nothing: what a recording with no
    speech in it means is the CALLER's decision (the runner transcribes the
    whole file), and inventing a clip here would take it away."""
    assert vad.pack_regions([]) == []


def test_one_region_is_one_clip(vad):
    assert vad.pack_regions([(1.0, 3.0)]) == [[(1.0, 3.0)]]


def test_regions_summing_to_EXACTLY_the_budget_stay_in_ONE_clip(vad):
    """The boundary, and it is inclusive on purpose: the budget is the most
    speech a clip may carry, and a clip carrying exactly that fits in the
    window. Off by one the other way, a run of regions that added up perfectly
    would pay for a second nearly-empty encoder pass — the very cost this
    exists to avoid."""
    half = vad.BUDGET_S / 2
    regions = [(0.0, half), (100.0, 100.0 + half)]

    assert vad.pack_regions(regions) == [regions]


def test_one_second_MORE_than_the_budget_becomes_TWO_clips(vad):
    """And the split is at a region boundary, never inside one."""
    half = vad.BUDGET_S / 2
    regions = [(0.0, half), (100.0, 100.5 + half)]

    assert vad.pack_regions(regions) == [[regions[0]], [regions[1]]]


def test_a_region_LONGER_than_the_budget_travels_ALONE_and_is_never_split(vad):
    """Cutting mid-speech loses words, and Whisper already chunks a long input
    internally — so an over-budget region is passed through whole. Today's
    longest observed region is 14s, but a monologue with no half-second pause
    in it can exceed the budget, and when it does it must not take a neighbour
    with it either: the neighbour would be decoded inside a clip that is
    already over the window."""
    long_one = (10.0, 10.0 + vad.BUDGET_S + 5.0)
    regions = [(0.0, 1.0), long_one, (100.0, 101.0)]

    packs = vad.pack_regions(regions)

    assert [(0.0, 1.0)] in packs
    assert [long_one] in packs, "an over-budget region was merged or split"
    assert all(len(pack) == 1 or vad.packed_duration(pack) <= vad.BUDGET_S
               for pack in packs)


def test_every_region_appears_ONCE_and_in_ORDER(vad):
    """The invariant the timestamp remap rests on: the clips are a partition of
    the regions, in the order the detector found them. A dropped region is
    speech missing from the transcript, a duplicated one is a sentence
    transcribed twice, and a reordered one puts the transcript out of order —
    none of which the caller can see, because each clip decodes fine on its
    own."""
    regions = [(start, start + 3.0) for start in range(0, 200, 10)]

    packs = vad.pack_regions(regions)

    assert [region for pack in packs for region in pack] == regions
    assert all(pack for pack in packs), "an empty clip was emitted"


def test_the_budget_leaves_HEADROOM_under_whisper_s_window(vad):
    """29 rather than 30. The window is 30 seconds exactly (`N_FRAMES = 3000`
    at 100 frames a second), and a clip that tips a hair over it — by a
    rounding of the region ends, or by the padding `PAD_S` adds — buys a second
    encoder pass for a few hundred milliseconds of speech."""
    assert 25.0 <= vad.BUDGET_S < 30.0


def test_packing_CONCATENATES_the_speech_and_leaves_the_silence_behind(vad):
    """The whole point: the clip handed to the decoder is speech only. The
    rejected alternative was to slice from the first region's start to the
    last's end, which is simpler and needs no remap — and which puts the
    silence back inside the clip, giving up the reason `vad: true` exists at
    all (Whisper hallucinates in silence)."""
    audio = np.arange(16000 * 10, dtype=np.float32)
    pack = [(0.0, 1.0), (5.0, 6.0)]

    clip = vad.packed_samples(audio, pack)

    assert len(clip) == 2 * 16000
    # Second half is the SECOND region's samples, not the silence between them.
    assert clip[0] == 0 and clip[16000] == 5 * 16000


def test_a_time_in_the_clip_maps_back_through_the_JOIN(vad):
    """The inverse of the concatenation, and the silent failure it prevents: a
    transcript that looks perfect and whose every timestamp after a join is
    early by the length of the silence that was dropped."""
    pack = [(0.0, 5.0), (30.0, 35.0)]

    assert vad.original_start(pack, 0.0) == 0.0
    assert vad.original_start(pack, 2.5) == 2.5
    assert vad.original_start(pack, 7.5) == 32.5
    assert vad.original_end(pack, 2.5) == 2.5
    assert vad.original_end(pack, 7.5) == 32.5


def test_a_START_on_a_join_belongs_to_the_region_that_STARTS_there(vad):
    """Nothing was said at 5.0 in the clip that was not also said at 30.0 in the
    recording, and placing a segment's START at the first region's end would put
    it behind the silence it was cut out of — a segment that begins before the
    speech it transcribes."""
    pack = [(0.0, 5.0), (30.0, 35.0)]

    assert vad.original_start(pack, 5.0) == 30.0


def test_an_END_on_a_join_belongs_to_the_region_that_ENDS_there(vad):
    """The asymmetry, and it is the whole reason there are two functions.

    A start and an end landing on the same clip time mean different things: the
    start is the first moment of the next region, the end is the last moment of
    the previous one. Mapped with start semantics, a segment that lies ENTIRELY
    inside region one and merely ends on the join comes back stretched across
    the silence — `3.0-5.0` reported as `3.0-30.0`, an end late by the whole
    length of the pause.

    Not a float coincidence either, which is why it has its own test: the join
    is exactly where the pause was, so it is the most natural place for Whisper
    to end a segment, and region ends land on its 0.02s timestamp grid regularly
    (`WINDOW / 16000 * 5` = 0.16 and `2 * PAD_S` = 0.4 are both multiples of it).
    Downstream the stretched span is worse than a late caption:
    `diarize.speaker_for` sums turn overlap across it, so the sentence can be
    attributed to whoever the segmenter heard inside the silence Silero dropped.
    """
    pack = [(0.0, 5.0), (30.0, 35.0)]

    assert vad.original_end(pack, 5.0) == 5.0
    # One grid step PAST the join is a different case and must keep mapping into
    # the next region: a segment ending at 5.02 genuinely continues into it.
    assert vad.original_end(pack, 5.02) == 30.02


def test_the_two_ENDS_of_one_segment_are_mapped_INDEPENDENTLY(vad):
    """A segment can span a join — Whisper hears continuous speech, because the
    silence is not in the clip it was given — and then its start and its end
    fall in different source regions. Mapping the pair as one offset would
    stretch or squash it; mapping each endpoint on its own is what keeps both
    numbers true.

    Both flavours are exercised the way `worker.py` uses them, start for the
    start and end for the end, because the pair is only correct together: a
    segment that spans the join must survive, and one that merely touches it
    must not be stretched (above).
    """
    pack = [(0.0, 5.0), (30.0, 35.0)]

    assert (vad.original_start(pack, 4.0), vad.original_end(pack, 6.0)) == (
        4.0, 31.0)


def test_a_WORD_is_never_stretched_across_a_join_the_way_a_segment_may_be(vad):
    """The one asymmetry between the two callers, and the invariant behind it:
    packing only REMOVES time, so the inverse must never hand back an interval
    LONGER than the packed one it was given. A segment is exempt (the test above
    — real speech on both sides of the join, so both numbers are true); a word
    is not, because one token cannot be spoken across silence that was cut out.
    Mapped endpoint by endpoint a 0.2s word containing the join came back as
    `4.9-30.1`, 25 seconds of highlight for one word."""
    pack = [(0.0, 5.0), (30.0, 35.0)]

    # Strictly containing the join: placed in the region holding its MIDPOINT,
    # and 5.0 is that midpoint — the tie goes to the region that BEGINS there.
    assert vad.original_word_span(pack, 4.9, 5.1) == (30.0, 30.1)
    # Most of it before the join, so it stays in the first region and gives up
    # the tail it was timed on the far side of the pause.
    assert vad.original_word_span(pack, 4.5, 5.4) == (4.5, 5.0)
    # Never longer than it was timed, whichever side it lands on.
    for at, until in ((4.9, 5.1), (4.5, 5.4), (4.99, 5.01), (0.1, 9.9)):
        start, end = vad.original_word_span(pack, at, until)
        assert end - start <= (until - at) + 1e-9


def test_a_word_that_only_TOUCHES_a_join_maps_like_the_endpoints_do(vad):
    """What the straddle rule must leave alone, or it would trade one wrong
    answer for another. A word ending exactly on a join belongs to the region
    that ends there and one beginning on it to the region that begins there —
    the same asymmetry `original_start`/`original_end` carry — and a word inside
    a single region is simply an offset into it."""
    pack = [(0.0, 5.0), (30.0, 35.0)]

    assert vad.original_word_span(pack, 4.5, 5.0) == (4.5, 5.0)
    assert vad.original_word_span(pack, 5.0, 5.5) == (30.0, 30.5)
    assert vad.original_word_span(pack, 1.0, 1.4) == (1.0, 1.4)
    assert vad.original_word_span(pack, 7.5, 8.0) == (32.5, 33.0)


def test_a_WORD_past_the_speech_collapses_at_the_bound_like_a_segment_does(vad):
    """Same padding hallucination, same answer. A word the library timed past
    the packed clip has no region to hold its midpoint, so it resolves to the
    last one and clamps there — an instant the caller can see is unusable,
    rather than a span reaching into silence that was removed."""
    pack = [(0.0, 5.0), (30.0, 35.0)]

    assert vad.original_word_span(pack, 12.0, 29.0) == (35.0, 35.0)
    # And below zero, for `slice_samples`' reason.
    assert vad.original_word_span(pack, -1.0, 0.5) == (0.0, 0.5)


def test_a_time_PAST_the_speech_lands_on_the_clip_s_last_moment(vad):
    """Whisper times against a padded 30-second window, so a two-second clip
    can report a segment ending at 29 — and a hallucination in the padding can
    start there too. Clamped to the last region's end, both come out as the
    boundary, which is what lets the runner drop the zero-length result rather
    than place text inside silence that was removed."""
    pack = [(0.0, 5.0), (30.0, 35.0)]

    for at_original in (vad.original_start, vad.original_end):
        assert at_original(pack, 10.0) == 35.0
        assert at_original(pack, 29.0) == 35.0
        # And below zero, for the same reason `slice_samples` clamps: a negative
        # time must not wrap round to somewhere else in the recording.
        assert at_original(pack, -1.0) == 0.0


def test_a_time_inside_a_ONE_region_pack_is_just_an_OFFSET(vad):
    """The unfiltered path and the over-budget region both decode as a pack of
    one, where there is no join for the two flavours to disagree about — so
    they must agree, or every non-VAD transcript would depend on which one the
    runner happened to call."""
    pack = [(10.0, 12.0)]

    assert vad.original_start(pack, 0.5) == vad.original_end(pack, 0.5) == 10.5
    assert vad.original_start(pack, 2.0) == vad.original_end(pack, 2.0) == 12.0


def test_the_packed_duration_is_SPEECH_not_wall_clock(vad):
    """What the ETA and the progress clamp are denominated in: the clip is two
    seconds long even though it was cut out of six."""
    assert vad.packed_duration([(0.0, 1.0), (5.0, 6.0)]) == 2.0
