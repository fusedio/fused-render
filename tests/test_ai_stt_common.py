"""What the two speech-to-text runners share (`runners/stt_common.py`, SPEC AI-10).

Both workers' own tests pin their wiring; this pins the shared rule once.
`runners/` is put on `sys.path` the way a runner's own interpreter has it.
"""

import os
import sys

RUNNERS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "fused_render", "ai", "runners")
if RUNNERS not in sys.path:
    sys.path.insert(0, RUNNERS)

import stt_common  # noqa: E402


def test_a_word_that_inverts_collapses_to_an_instant_and_never_vanishes():
    """The invariant a caller builds on: a segment's words are its text, in
    order, complete. A word timed past the end of its segment (into padding)
    is clamped to the nearest bound as a zero-length span, not dropped."""
    pack = [(10.0, 13.0), (20.0, 22.0)]  # a 5s clip
    words = stt_common.words_in_original_time(
        pack, [(0.0, 0.5, " one"), (4.9, 9.0, " late"), (9.5, 9.9, " past")],
        at=10.0, until=22.0)

    assert [w["word"] for w in words] == [" one", " late", " past"]
    for word in words:
        assert 10.0 <= word["start"] <= word["end"] <= 22.0
    assert words[-1]["start"] == words[-1]["end"] == 22.0


def test_a_word_is_clamped_into_its_segment_and_empty_text_is_skipped():
    words = stt_common.words_in_original_time(
        [(0.0, 10.0)], [(0.0, 3.0, " a"), (3.0, 4.0, ""), (4.0, 12.0, " b")],
        at=1.0, until=8.0)

    assert words == [{"start": 1.0, "end": 3.0, "word": " a"},
                     {"start": 4.0, "end": 8.0, "word": " b"}]
