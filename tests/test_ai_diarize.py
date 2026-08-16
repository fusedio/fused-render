"""Speaker diarization's arithmetic and its one required argument (SPEC §40).

`diarize.py` is three things bolted together: the `speakers` validation rule,
an ONNX pipeline (sherpa-onnx over two downloaded models), and the arithmetic
that joins the pipeline's turns to Whisper's segments. The middle one needs
sherpa-onnx and 33MB of downloads, so it cannot run on CI — and it is also the
part with no decisions in it. The other two are where every judgement lives, and
they are what this file drives with the session stubbed out.

The real pipeline WAS driven by hand on this machine before any of this was
written, against `csukuangfj/speaker-embedding-models`' own two-speaker English
test recording (`1-two-speakers-en.wav`, 16.0s): four turns came back —
1.58-3.41 and 4.40-6.46 as speaker 0, 9.35-11.47 and 12.16-14.64 as speaker 1 —
which is the file's own truth, two people taking one half each.

That run also settled the embedding repo. `Wespeaker/wespeaker-voxceleb-
resnet34-LM` — the WeSpeaker team's own, ungated, CC BY 4.0, carrying exactly
the ONNX the name promises — has no `model_type` in its ONNX metadata, and
sherpa-onnx does not raise on that: it prints "Unknown model type for speaker
embedding extractor!" and aborts the process from C++, killing the worker with
no Python traceback to report. `csukuangfj/speaker-embedding-models` is the same
weights with sherpa's metadata added, and is what ships.
"""
import importlib.util
import os

import pytest

DIARIZE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "diarize.py",
)


@pytest.fixture(scope="module")
def diarize():
    """Imported by PATH, the way a runner reaches it — which is the reading
    that ships. The server's own reading (`fused_render.ai.runners.diarize`) is
    a separate test below, because the two resolve `formats` differently and a
    module that works under one loader and not the other is half a feature."""
    spec = importlib.util.spec_from_file_location("runners_diarize", DIARIZE_PATH)
    assert spec is not None and spec.loader is not None, DIARIZE_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the argument that cannot be guessed -----------------------------------------


@pytest.mark.parametrize("value", [1, 2, 7, 100])
def test_a_whole_number_of_people_is_accepted(diarize, value):
    assert diarize.speakers_or_raise(value) == value


def test_an_ABSENT_count_is_refused_with_the_reason_and_an_example(diarize):
    """The refusal has to carry both, because the caller reading it is a page
    author who passed `diarize: true` and expected it to be enough."""
    for missing in (None, ""):
        with pytest.raises(ValueError) as raised:
            diarize.speakers_or_raise(missing)
        message = str(raised.value)
        assert "'speakers' is required" in message
        assert "diarize: true, speakers: 2" in message


def test_TRUE_is_refused_rather_than_read_as_one_speaker(diarize):
    """`True` is an `int` in Python, so `{diarize: true, speakers: true}` — a
    plausible copy-paste — would otherwise cluster to one speaker and label the
    entire transcript "Speaker 1", which is a wrong answer delivered with total
    confidence rather than a refusal."""
    with pytest.raises(ValueError, match="whole number"):
        diarize.speakers_or_raise(True)


@pytest.mark.parametrize("value", [2.0, 2.7, "2", [2]])
def test_anything_that_is_not_an_INT_is_refused_not_coerced(diarize, value):
    """`2.0` is harmless and `2.7` is a caller who computed the count and got
    it wrong; there is no reading of "2.7 speakers" worth truncating to 2, and
    a rule that accepts one float has to accept the other."""
    with pytest.raises(ValueError, match="whole number"):
        diarize.speakers_or_raise(value)


@pytest.mark.parametrize("value", [0, -1])
def test_fewer_than_one_speaker_is_refused(diarize, value):
    with pytest.raises(ValueError, match="at least 1"):
        diarize.speakers_or_raise(value)


def test_an_absurd_count_is_refused_rather_than_clustered(diarize):
    """A typo far more often than a conference call, and the cost of taking it
    at its word is minutes of clustering for an answer nobody wanted."""
    with pytest.raises(ValueError, match="at most 100"):
        diarize.speakers_or_raise(diarize.MAX_SPEAKERS + 1)


# -- the rate the turns are denominated in ---------------------------------------


class FakeDiarization:
    """sherpa's `OfflineSpeakerDiarization`, with the ONNX taken out.

    The real one needs 33MB of downloads and a C++ runtime, and it is the part
    with no decisions in it. What a stub CAN still catch is everything around
    the call: the rate check, the cancel translation, and the shape of what
    comes back."""

    sample_rate = 16000

    def __init__(self, turns=(), chunks=1):
        self._turns = list(turns)
        self._chunks = chunks
        self.samples = None

    def process(self, samples, callback=None):
        self.samples = samples
        for index in range(self._chunks):
            if callback is not None and callback(index + 1, self._chunks):
                break
        turns = self._turns

        class Result:
            def sort_by_start_time(self):
                return [type("Segment", (), {"start": s, "end": e, "speaker": k})()
                        for s, e, k in sorted(turns)]

        return Result()


def test_a_MISMATCHED_sample_rate_is_refused_rather_than_silently_rescaled(diarize):
    """Every turn's start/end is samples over the rate sherpa assumes, so a
    mismatch does not fail — it returns turns wrong by a ratio, which land on
    the wrong segments and attribute the transcript to the wrong people."""
    import numpy as np

    with pytest.raises(ValueError, match="16000 Hz"):
        diarize.speaker_turns(np.zeros(16000, dtype=np.float32),
                              FakeDiarization(), 44100)


def test_turns_come_back_as_plain_sorted_tuples(diarize):
    import numpy as np

    session = FakeDiarization([(4.0, 6.0, 1), (0.0, 2.0, 0)])
    turns = diarize.speaker_turns(np.zeros(160, dtype=np.float32), session, 16000)
    assert turns == [(0.0, 2.0, 0), (4.0, 6.0, 1)]
    assert all(isinstance(t[2], int) for t in turns)


def test_a_stop_ABORTS_rather_than_returning_a_partial_diarization(diarize):
    """The aborted result is a prefix of the recording. Returning it would
    label the first minute and leave the rest None — a transcript that looks
    diarized and is not."""
    import numpy as np

    session = FakeDiarization([(0.0, 2.0, 0)], chunks=4)
    with pytest.raises(diarize.DiarizationCancelled):
        diarize.speaker_turns(np.zeros(160, dtype=np.float32), session, 16000,
                              should_stop=lambda: True)


def test_no_stop_predicate_means_the_callback_never_aborts(diarize):
    import numpy as np

    session = FakeDiarization([(0.0, 2.0, 0)], chunks=4)
    assert diarize.speaker_turns(np.zeros(160, dtype=np.float32), session,
                                 16000) == [(0.0, 2.0, 0)]


# -- the labels ------------------------------------------------------------------


def test_speakers_are_named_from_ONE_not_from_zero(diarize):
    """Read by people, and there is no zero-th person in a room."""
    assert diarize.label(0) == "Speaker 1"
    assert diarize.label(1) == "Speaker 2"


# -- the overlap join ------------------------------------------------------------


def test_a_segment_inside_one_turn_takes_that_speaker(diarize):
    turns = [(0.0, 10.0, 0), (10.0, 20.0, 1)]
    assert diarize.speaker_for(2.0, 4.0, turns) == 0
    assert diarize.speaker_for(12.0, 14.0, turns) == 1


def test_a_segment_SPANNING_a_boundary_goes_to_whoever_said_most_of_it(diarize):
    """A Whisper segment is a sentence and a sentence routinely straddles a
    hand-over. 9.0-11.0 is 1s each side; 9.0-13.0 is 1s then 3s."""
    turns = [(0.0, 10.0, 0), (10.0, 20.0, 1)]
    assert diarize.speaker_for(9.0, 13.0, turns) == 1
    assert diarize.speaker_for(7.0, 11.0, turns) == 0


def test_overlap_is_summed_PER_SPEAKER_not_per_turn(diarize):
    """One speaker can hold several turns inside one segment — a short
    interjection splits theirs in two. Taking the single longest TURN would
    hand the segment to the interrupter (2s in one turn) over the person who
    actually spoke for 6s across two."""
    turns = [(0.0, 3.0, 0), (3.0, 5.0, 1), (5.0, 8.0, 0)]
    assert diarize.speaker_for(0.0, 8.0, turns) == 0


def test_a_segment_overlapping_NOTHING_is_labelled_None_not_guessed(diarize):
    """Whisper heard words where the segmenter heard nobody. Inventing a
    speaker there is the confident version of a wrong answer."""
    turns = [(0.0, 5.0, 0), (10.0, 15.0, 1)]
    assert diarize.speaker_for(6.0, 9.0, turns) is None


def test_a_segment_that_merely_TOUCHES_a_turn_does_not_count_as_overlap(diarize):
    """Zero-width contact is not speech. Counting it would let a segment that
    starts exactly where a turn ends inherit that speaker."""
    assert diarize.speaker_for(5.0, 8.0, [(0.0, 5.0, 0)]) is None


def test_a_ZERO_LENGTH_segment_is_labelled_None(diarize):
    """It overlaps nothing by construction, and the honest answer to "who spoke
    during no time at all" is nobody."""
    assert diarize.speaker_for(3.0, 3.0, [(0.0, 10.0, 0)]) is None


def test_an_empty_turn_list_labels_nothing(diarize):
    assert diarize.speaker_for(0.0, 10.0, []) is None


def test_an_exact_TIE_goes_to_whoever_started_speaking_first(diarize):
    """Rare, and either answer is defensible — what is not defensible is dict
    iteration order deciding it, because then the same recording labels
    differently on two runs and a page's speaker colours shuffle between them."""
    turns = [(0.0, 10.0, 0), (10.0, 20.0, 1)]
    assert diarize.speaker_for(9.0, 11.0, turns) == 0
    # …and the rule reads the TURNS, not the list order: the same tie with the
    # later-starting speaker listed first still answers with the earlier one.
    assert diarize.speaker_for(9.0, 11.0, list(reversed(turns))) == 0


def test_a_tie_between_speakers_who_START_together_falls_back_to_the_index(diarize):
    """Two turns beginning at the same instant is not physical, but the rule
    has to terminate somewhere that is not iteration order."""
    turns = [(0.0, 5.0, 3), (0.0, 5.0, 1)]
    assert diarize.speaker_for(0.0, 5.0, turns) == 1


def test_a_FLOAT_hair_of_difference_is_still_a_tie(diarize):
    """Overlaps are differences of floats and two turns equal on paper rarely
    are in binary. Without the tolerance the winner is whichever rounding error
    happened to be larger, which is exactly the nondeterminism the tie-break
    exists to remove."""
    turns = [(0.0, 10.0, 0), (10.0, 20.0, 1)]
    # 1s each side, expressed so that the two overlaps differ in the last bits.
    assert diarize.speaker_for(9.0, 11.0 + 1e-12, turns) == 0


# -- assignment over a whole transcript ------------------------------------------


def test_assignment_labels_every_segment_and_returns_the_legend(diarize):
    segments = [
        {"start": 0.0, "end": 2.0, "text": "hello"},
        {"start": 11.0, "end": 13.0, "text": "hi there"},
        {"start": 30.0, "end": 31.0, "text": "(music)"},
    ]
    turns = [(0.0, 10.0, 0), (10.0, 20.0, 1)]
    legend = diarize.assign_speakers(segments, turns)
    assert [s["speaker"] for s in segments] == ["Speaker 1", "Speaker 2", None]
    assert legend == ["Speaker 1", "Speaker 2"]


def test_the_legend_lists_only_speakers_a_SEGMENT_was_given(diarize):
    """A speaker the segmenter heard during a stretch Whisper transcribed no
    words from would otherwise appear in a legend nothing in the transcript
    refers to, which reads as a bug in the page rendering it."""
    segments = [{"start": 0.0, "end": 2.0, "text": "hello"}]
    turns = [(0.0, 10.0, 0), (10.0, 20.0, 1), (20.0, 30.0, 2)]
    assert diarize.assign_speakers(segments, turns) == ["Speaker 1"]


def test_the_legend_sorts_by_INDEX_so_ten_comes_after_two(diarize):
    """The reason `speaker_turns` carries integers this far: "Speaker 10" sorts
    BEFORE "Speaker 2" as a string, and a legend in that order is the kind of
    detail nobody notices until a recording has ten people in it."""
    turns = [(0.0, 1.0, 9), (1.0, 2.0, 1)]
    segments = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]
    assert diarize.assign_speakers(segments, turns) == ["Speaker 2", "Speaker 10"]


def test_assignment_MUTATES_the_segments_it_was_given(diarize):
    """They ARE the transcript both engines are about to write; copying would
    leave two lists to keep in step."""
    segments = [{"start": 0.0, "end": 1.0}]
    diarize.assign_speakers(segments, [(0.0, 5.0, 0)])
    assert segments[0]["speaker"] == "Speaker 1"


# -- the property both engines rest on -------------------------------------------


def test_BOTH_engines_label_identically_because_there_is_one_implementation(diarize):
    """AI-10c's promise, made structural rather than tested twice.

    `mlx_whisper/worker.py` and `faster_whisper/worker.py` reach the same
    `runners/diarize.py` through the same `sys.path` insert that reaches
    `worker_base` — there is no second copy to drift. This test pins the
    property that makes that worth doing: identical turns and identical
    segments produce byte-identical labels, whatever produced the segments.
    """
    turns = [(0.0, 4.7, 1), (4.7, 9.2, 0), (9.2, 12.0, 1)]
    shape = [(0.0, 2.5), (2.5, 5.0), (5.0, 8.0), (8.0, 10.0), (10.0, 12.0)]

    from_mlx = [{"start": a, "end": b, "text": "x"} for a, b in shape]
    from_ct2 = [{"start": a, "end": b, "text": "x"} for a, b in shape]
    assert (diarize.assign_speakers(from_mlx, turns)
            == diarize.assign_speakers(from_ct2, turns))
    assert ([s["speaker"] for s in from_mlx]
            == [s["speaker"] for s in from_ct2]
            == ["Speaker 2", "Speaker 2", "Speaker 1", "Speaker 1", "Speaker 2"])


def test_both_workers_import_the_ONE_implementation_rather_than_a_copy(diarize):
    """The structural half of the test above: a second `diarize.py` under
    either runner folder is the drift AI-10c forbids, and it would not fail any
    behavioural test — both copies would pass their own."""
    runners = os.path.dirname(DIARIZE_PATH)
    for folder in ("mlx_whisper", "faster_whisper"):
        assert not os.path.exists(os.path.join(runners, folder, "diarize.py")), folder
        source = open(os.path.join(runners, folder, "worker.py"), encoding="utf-8").read()
        assert "import diarize" in source, folder


# -- the two readings of this module ---------------------------------------------


def test_the_SERVER_reaches_the_same_module_through_the_package(diarize):
    """Two loaders, one module. The runner imports it by path (the fixture
    above); the server imports it as `fused_render.ai.runners.diarize` to
    enforce `speakers` without the bridge's help. A module that resolved
    `formats` under only one of them would be half a feature, and the failure
    would land in production rather than here."""
    from fused_render.ai.runners import diarize as packaged

    assert packaged.speakers_or_raise(3) == 3
    assert packaged.label(0) == diarize.label(0)
    assert packaged.SEGMENTATION_FILE == diarize.SEGMENTATION_FILE
    assert packaged.EMBEDDING_FILE == diarize.EMBEDDING_FILE


def test_the_server_reading_shares_ONE_component_registry():
    """…and reaches `formats` through the package rather than re-importing it
    as a top-level module, which would put two `COMPONENT_REPOS` dicts in one
    process — the exact drift `formats.py` exists to prevent."""
    from fused_render.ai.runners import diarize as packaged
    from fused_render.ai.runners import formats

    assert packaged.formats is formats


def test_the_diarization_models_read_their_files_from_the_registry():
    """Same rule as the VAD: the module under the runner folder names the repo
    and the page names it too, and one copy is what keeps a downloaded file
    from becoming a row the page cannot explain."""
    from fused_render.ai.runners import diarize as packaged
    from fused_render.ai.runners import formats

    for repo, filename in ((packaged.SEGMENTATION_REPO, packaged.SEGMENTATION_FILE),
                           (packaged.EMBEDDING_REPO, packaged.EMBEDDING_FILE)):
        assert repo in formats.COMPONENT_REPOS
        assert formats.COMPONENT_REPOS[repo]["file"] == filename
        # `of: None` — they belong to the CAPABILITY, not to a model: the same
        # two files serve every transcription whichever whisper repo is loaded,
        # on either engine.
        assert formats.COMPONENT_REPOS[repo]["of"] is None


def test_the_embedding_models_licence_is_ATTRIBUTED_where_the_page_shows_it():
    """CC BY 4.0 requires attribution, and the `what` text is what the AI
    Models page renders — so that is where the credit has to be, not in a
    comment nobody deploys."""
    from fused_render.ai.runners import diarize as packaged
    from fused_render.ai.runners import formats

    what = formats.COMPONENT_REPOS[packaged.EMBEDDING_REPO]["what"]
    assert "WeSpeaker" in what
    assert "CC BY 4.0" in what
