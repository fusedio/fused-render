"""The progressive transcript: segments readable WHILE the file decodes.

`fused.ai.transcribe` is job-shaped on purpose — it outlives the tab that asked
for it — so the words cannot ride a held-open stream the way `generate_text`'s
tokens do, and they cannot ride the job row either, which is a download-manager
record and not a data channel. What they ride instead is a second FILE beside
the one the request already named: `<out_base>.partial.jsonl`, one segment per
line, appended and flushed as each is decoded.

This module is that file's whole implementation, and it sits at the runners
ROOT for the reason `diarize.py` and the VAD do (AI-10c): two engines serve one
capability and a page must not be able to tell which one ran. A second copy
under `faster_whisper/` would be free to drift on the line shape, the flush
rule and the lifecycle — and would fail no behavioural test, because both
copies would pass their own. The structural half of that is pinned below.

The rules being pinned here, in order of how expensive they are to get wrong:

- **The final `.json`/`.txt` do not move.** The partial file is additive; a run
  that never asked for one writes the bytes it always did. The sink therefore
  never mutates the segment dicts it is handed — `assign_speakers` still owns
  the `speaker` key on the list that gets dumped at the end.
- **A reader never sees half a line.** One `write` of a complete line plus a
  flush per segment, because the whole point is that somebody is tailing it.
- **Removed on success and on cancel, LEFT on error.** A failed run's partial
  file is the only salvage from work that died halfway; a successful one's is
  duplicate bytes that would otherwise accumulate in `~/.fused-render` forever.
"""
import importlib.util
import json
import os

import pytest

_RUNNERS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners",
)
PARTIAL_PATH = os.path.join(_RUNNERS, "partial.py")


def _by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def partial():
    """Imported by PATH, the way a runner reaches it — which is the reading
    that ships. `diarize.py` documents the same two-loader hazard; this module
    is imported by the server too (the route derives the path it advertises
    from the same helper), so both readings have to work."""
    return _by_path("runners_partial", PARTIAL_PATH)


@pytest.fixture(scope="module")
def diarize():
    return _by_path("runners_diarize_for_partial",
                    os.path.join(_RUNNERS, "diarize.py"))


def _lines(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class Cancelled(Exception):
    """Stands in for `worker_base.Cancelled`, which this module deliberately
    does not import — the exception types that count as a cancel are passed in,
    for the reason `diarize.py` takes its downloader as an argument."""


# -- the path the route and the workers must agree on ----------------------------


def test_the_partial_path_is_a_SIBLING_of_the_json_the_request_named(partial):
    """Derived in one place because three parties have to name the same file:
    the route (which advertises it), the worker (which writes it) and nobody
    else — a page is never expected to string-munge one path out of another."""
    assert partial.partial_path("/x/y/20260101-120000-clip-abc.json") == (
        "/x/y/20260101-120000-clip-abc.partial.jsonl")


def test_the_partial_path_replaces_the_extension_rather_than_appending(partial):
    """`out.json.partial.jsonl` would sort beside the transcript and read like
    a transcript, and `_transcripts_dir()` is a directory a user browses."""
    assert partial.partial_path("/x/out.json").endswith("/out.partial.jsonl")


def test_an_empty_out_path_has_no_partial_path(partial):
    assert partial.partial_path("") is None
    assert partial.partial_path(None) is None


# -- the line shape --------------------------------------------------------------


def test_each_added_segment_is_ONE_json_line(partial, tmp_path):
    out = str(tmp_path / "t.partial.jsonl")
    with partial.sink(out) as sink:
        sink.add({"start": 0.0, "end": 1.5, "text": "hello"})
        sink.add({"start": 1.5, "end": 2.0, "text": "there"})
        assert _lines(out) == [{"start": 0.0, "end": 1.5, "text": "hello"},
                               {"start": 1.5, "end": 2.0, "text": "there"}]


def test_a_line_is_COMPLETE_the_moment_add_returns(partial, tmp_path):
    """The reader is tailing this file, not waiting for it. A buffered writer
    that flushed on close would show a page nothing until the run finished,
    which is the failure the whole feature exists to fix; a flush mid-line
    would hand it `{"start": 0.0, "en` and a JSON error."""
    out = str(tmp_path / "t.partial.jsonl")
    with partial.sink(out) as sink:
        sink.add({"start": 0.0, "end": 1.0, "text": "one"})
        with open(out, "rb") as handle:
            first = handle.read()
        assert first.endswith(b"\n") and len(first.splitlines()) == 1
        sink.add({"start": 1.0, "end": 2.0, "text": "two"})
        with open(out, "rb") as handle:
            second = handle.read()
        assert second.startswith(first) and second.endswith(b"\n")
        assert len(second.splitlines()) == 2


def test_the_line_carries_the_SAME_keys_the_final_json_will(partial, tmp_path):
    """Not a different shape for the preview — a page that renders a partial
    segment and then re-renders the final one must not need two code paths."""
    out = str(tmp_path / "t.partial.jsonl")
    with partial.sink(out) as sink:
        sink.add({"start": 0.0, "end": 1.0, "text": "hi"})
        assert sorted(_lines(out)[0]) == ["end", "start", "text"]


def test_non_ascii_text_is_written_UNESCAPED_like_the_final_dump(partial, tmp_path):
    """`json.dump(..., ensure_ascii=False)` is what both workers write, and a
    partial line that escaped instead would still parse — but the byte offsets
    a tailing reader counts would stop matching what it decoded."""
    out = str(tmp_path / "t.partial.jsonl")
    with partial.sink(out) as sink:
        sink.add({"start": 0.0, "end": 1.0, "text": "café"})
        assert "café" in open(out, encoding="utf-8").read()


def test_the_sink_does_NOT_mutate_the_segment_it_was_given(partial, tmp_path):
    """The list it is being shown IS the transcript about to be dumped. This is
    what keeps the final bytes identical to a run that wrote no partial file."""
    out = str(tmp_path / "t.partial.jsonl")
    segment = {"start": 0.0, "end": 1.0, "text": "hi"}
    with partial.sink(out, turns=[(0.0, 5.0, 0)]) as sink:
        sink.add(segment)
    assert segment == {"start": 0.0, "end": 1.0, "text": "hi"}


# -- speakers, available immediately -----------------------------------------------


def test_a_diarized_line_carries_its_SPEAKER(partial, tmp_path):
    """The turns exist before a word is decoded — diarization is a pre-pass over
    the full waveform (D309) — so a partial line can be labelled at the moment
    it is written rather than at the end with the rest."""
    out = str(tmp_path / "t.partial.jsonl")
    with partial.sink(out, turns=[(0.0, 5.0, 0), (5.0, 10.0, 1)]) as sink:
        sink.add({"start": 0.0, "end": 2.0, "text": "mine"})
        sink.add({"start": 6.0, "end": 7.0, "text": "yours"})
        assert [(l["text"], l["speaker"]) for l in _lines(out)] == [
            ("mine", "Speaker 1"), ("yours", "Speaker 2")]


def test_a_partial_label_is_the_SAME_one_the_final_json_lands_on(partial, diarize,
                                                                 tmp_path):
    """Two labellings of one segment — the sink's, as it is written, and
    `assign_speakers`' at the end — and a page that saw them disagree would
    watch a speaker change colour after the run finished. They agree because
    there is one arithmetic, not because both were written carefully."""
    out = str(tmp_path / "t.partial.jsonl")
    turns = [(0.0, 4.7, 1), (4.7, 9.2, 0), (9.2, 12.0, 1)]
    shape = [(0.0, 2.5), (2.5, 5.0), (5.0, 8.0), (8.0, 10.0), (10.0, 12.0)]
    segments = [{"start": a, "end": b, "text": "x"} for a, b in shape]
    with partial.sink(out, turns=turns) as sink:
        for segment in segments:
            sink.add(segment)
        as_written = [line["speaker"] for line in _lines(out)]
    diarize.assign_speakers(segments, turns)
    assert as_written == [s["speaker"] for s in segments]
    assert as_written == ["Speaker 2", "Speaker 2", "Speaker 1", "Speaker 1",
                          "Speaker 2"]


def test_a_JOIN_SPANNING_segment_is_labelled_the_same_LIVE_as_it_is_finally(
        partial, diarize, tmp_path):
    """The same "one arithmetic" promise as above, at the one place the two
    labellings could still diverge: a segment that straddles a dropped silence.

    `mlx_whisper` packs VAD regions into one `transcribe()` call (AI-10f, D364),
    so a segment can span a pause that was never transcribed, and the FINAL
    labelling masks its scoring to the speech (`spans=`, D364) — otherwise the
    turn living inside the pause outvotes the words. The sink has to be handed
    the same mask or the page watches the speaker and its colour flip the moment
    the run finishes, which is exactly the failure the comment above
    `Sink.add`'s call claims cannot happen.

    Here speaker 2 holds ten seconds inside a 25-second pause and said nothing
    that was transcribed; speaker 1 said the words either side of it.
    """
    out = str(tmp_path / "t.partial.jsonl")
    turns = [(0.0, 5.0, 0), (10.0, 20.0, 1), (30.0, 35.0, 0)]
    spans = [(0.0, 5.0), (30.0, 35.0)]
    segments = [{"start": 3.0, "end": 31.0, "text": "either side of the pause"}]

    with partial.sink(out, turns=turns, spans=spans) as sink:
        sink.add(segments[0])
        live = [line["speaker"] for line in _lines(out)]
    diarize.assign_speakers(segments, turns, spans=spans)

    assert live == [s["speaker"] for s in segments] == ["Speaker 1"]
    # And the unmasked reading is genuinely different, or this test would pass
    # for the wrong reason — it is the mask being threaded through that makes
    # the two agree, not the two happening to agree anyway.
    assert diarize.speaker_for(3.0, 31.0, turns) == 1


def test_NO_spans_scores_the_whole_span_exactly_as_before(partial, tmp_path):
    """The default every other engine keeps: `faster_whisper`, `parakeet_mlx` and
    a non-VAD `mlx_whisper` run drop no silence out of the middle of a segment,
    pass no mask, and must label exactly as they did before `spans` existed."""
    out = str(tmp_path / "t.partial.jsonl")
    turns = [(0.0, 5.0, 0), (10.0, 20.0, 1), (30.0, 35.0, 0)]

    with partial.sink(out, turns=turns) as sink:
        sink.add({"start": 3.0, "end": 31.0, "text": "straight through"})
        assert [line["speaker"] for line in _lines(out)] == ["Speaker 2"]


def test_a_segment_overlapping_no_turn_is_labelled_NULL_not_dropped(partial, tmp_path):
    """`speaker: null` is `diarize.py`'s answer for "Whisper heard words where
    the segmenter heard nobody", and a partial line that omitted the key
    instead would read as "not diarized" to the page consuming it."""
    out = str(tmp_path / "t.partial.jsonl")
    with partial.sink(out, turns=[(0.0, 1.0, 0)]) as sink:
        sink.add({"start": 5.0, "end": 6.0, "text": "?"})
        assert _lines(out)[0]["speaker"] is None


def test_WITHOUT_turns_there_is_no_speaker_key_at_all(partial, tmp_path):
    """Additive, exactly as the final file is: an undiarized run's line is the
    line it would have been before this feature existed."""
    out = str(tmp_path / "t.partial.jsonl")
    with partial.sink(out) as sink:
        sink.add({"start": 0.0, "end": 1.0, "text": "hi"})
        assert "speaker" not in _lines(out)[0]


# -- the lifecycle ---------------------------------------------------------------


def test_a_run_that_FINISHES_leaves_no_partial_file(partial, tmp_path):
    """The final `.json` is the answer once it lands; the partial file is then
    a duplicate of it, in a directory the user browses, forever."""
    out = str(tmp_path / "t.partial.jsonl")
    with partial.sink(out) as sink:
        sink.add({"start": 0.0, "end": 1.0, "text": "hi"})
        assert os.path.exists(out)
    assert not os.path.exists(out)


def test_a_CANCELLED_run_leaves_no_partial_file(partial, tmp_path):
    """A ✕ means the user does not want this transcript. Leaving half of it on
    disk answers a question nobody asked, and the row already says cancelled."""
    out = str(tmp_path / "t.partial.jsonl")
    with pytest.raises(Cancelled):
        with partial.sink(out, cancelled=(Cancelled,)) as sink:
            sink.add({"start": 0.0, "end": 1.0, "text": "hi"})
            raise Cancelled()
    assert not os.path.exists(out)


def test_a_run_that_ERRORS_keeps_what_it_decoded(partial, tmp_path):
    """The one case where the file is the only thing left. A 90-minute
    recording that died at minute 80 has 80 minutes of transcript in it, and
    deleting that to keep the directory tidy is the worst trade here."""
    out = str(tmp_path / "t.partial.jsonl")
    with pytest.raises(RuntimeError):
        with partial.sink(out, cancelled=(Cancelled,)) as sink:
            sink.add({"start": 0.0, "end": 1.0, "text": "hi"})
            raise RuntimeError("the model fell over")
    assert _lines(out) == [{"start": 0.0, "end": 1.0, "text": "hi"}]


def test_a_cancel_type_is_only_what_the_CALLER_named(partial, tmp_path):
    """`partial.py` does not import `worker_base` — same rule `diarize.py`
    follows — so "which exception is a cancel" is an argument. With none
    passed, nothing is a cancel and every exception keeps the file."""
    out = str(tmp_path / "t.partial.jsonl")
    with pytest.raises(Cancelled):
        with partial.sink(out) as sink:
            sink.add({"start": 0.0, "end": 1.0, "text": "hi"})
            raise Cancelled()
    assert os.path.exists(out)


def test_discarding_a_file_that_was_never_written_is_not_an_error(partial, tmp_path):
    """A run cancelled during the audio decode never reaches a segment, and
    that path must not turn a cancel into a FileNotFoundError."""
    out = str(tmp_path / "nothing.partial.jsonl")
    with partial.sink(out):
        pass
    assert not os.path.exists(out)


def test_a_partial_file_that_will_not_DELETE_does_not_fail_a_finished_run(
        partial, monkeypatch, tmp_path):
    """The cleanup runs AFTER the real `.json` and `.txt` have landed, so a
    failure here reports a finished transcript as a failed one.

    It is reachable: the page tails this file through `/api/fs/raw`, and on
    Windows a read still open over it makes `os.remove` raise PermissionError
    rather than FileNotFoundError. Antivirus and a backup agent hold the same
    lock. The tidy-up is worth nothing next to the result it would destroy, so
    every `os.remove` failure is swallowed, not just the absent-file one.
    """
    out = str(tmp_path / "t.partial.jsonl")

    def locked(path):
        raise PermissionError(32, "The process cannot access the file")

    with partial.sink(out) as sink:
        sink.add({"start": 0.0, "end": 1.0, "text": "hi"})
        monkeypatch.setattr(partial.os, "remove", locked)
    # The file survives — nothing can be done about that — but the run does not
    # die on it, and the handle is closed either way.
    assert os.path.exists(out)


def test_a_partial_file_left_by_an_EARLIER_run_is_not_appended_to(partial, tmp_path):
    """The uid in `out_base` makes a collision impossible in practice, and
    "impossible in practice" is how a transcript ends up with somebody else's
    sentences in it. Truncate on open."""
    out = str(tmp_path / "t.partial.jsonl")
    with open(out, "w", encoding="utf-8") as handle:
        handle.write('{"start": 0.0, "end": 1.0, "text": "stale"}\n')
    with partial.sink(out) as sink:
        sink.add({"start": 0.0, "end": 1.0, "text": "fresh"})
        assert [l["text"] for l in _lines(out)] == ["fresh"]


# -- no path at all ----------------------------------------------------------------


def test_no_path_means_a_sink_that_does_nothing_at_all(partial, tmp_path):
    """A worker request from before this feature (or from a caller that
    supplies its own `out`) has no `outPartial`, and must run exactly as it
    did — no file, no directory created, no branch in the decode loop."""
    with partial.sink(None) as sink:
        sink.add({"start": 0.0, "end": 1.0, "text": "hi"})
        sink.discard()
    assert os.listdir(tmp_path) == []


def test_the_parent_directory_is_created_if_it_is_missing(partial, tmp_path):
    """`_transcripts_dir()` is made by the route, but the worker makes its own
    output directory before writing too — a sink that assumed one existed would
    fail the run for a reason that has nothing to do with the transcript."""
    out = str(tmp_path / "deep" / "t.partial.jsonl")
    with partial.sink(out) as sink:
        sink.add({"start": 0.0, "end": 1.0, "text": "hi"})
        assert os.path.exists(out)


# -- one implementation ------------------------------------------------------------


def test_BOTH_engines_write_the_SAME_bytes_because_there_is_one_writer(partial,
                                                                      tmp_path):
    """AI-10c's promise for this feature: identical segments produce an
    identical partial file whichever engine produced them, because the same
    module wrote both."""
    turns = [(0.0, 4.0, 1), (4.0, 9.0, 0)]
    shape = [(0.0, 2.5), (2.5, 5.0), (5.0, 8.0)]
    written = []
    for name in ("from_mlx", "from_ct2"):
        out = str(tmp_path / (name + ".partial.jsonl"))
        with partial.sink(out, turns=turns) as sink:
            for a, b in shape:
                sink.add({"start": a, "end": b, "text": "x"})
            written.append(open(out, "rb").read())
    assert written[0] == written[1]


def test_both_workers_import_the_ONE_writer_rather_than_a_copy(partial):
    """The structural half: a second `partial.py` under either runner folder is
    the drift AI-10c forbids, and no behavioural test would catch it — both
    copies would pass their own."""
    for folder in ("mlx_whisper", "faster_whisper"):
        assert not os.path.exists(os.path.join(_RUNNERS, folder, "partial.py")), folder
        with open(os.path.join(_RUNNERS, folder, "worker.py"), encoding="utf-8") as fh:
            source = fh.read()
        assert "import partial" in source, folder


def test_the_SERVER_reaches_the_same_module_through_the_package(partial):
    """Two loaders, one module — the route derives the path it advertises from
    `partial_path`, so a second copy under a second module name would let the
    route and the worker name different files."""
    from fused_render.ai.runners import partial as packaged

    assert packaged.partial_path("/x/y.json") == partial.partial_path("/x/y.json")


def test_the_writer_needs_NO_third_party_import_to_be_read(partial):
    """`diarize.py` keeps sherpa inside functions so the server can read the
    validation rule without it; this module has the same constraint one step
    further — it must import on the server's interpreter, where neither runner
    venv's packages exist."""
    source = open(PARTIAL_PATH, encoding="utf-8").read()
    assert "import fused_render" not in source
    assert "import numpy" not in source
