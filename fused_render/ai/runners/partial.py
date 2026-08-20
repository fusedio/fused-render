"""The progressive transcript: segments on disk as they are decoded (SPEC §41).

`fused.ai.transcribe` writes its `.json` and `.txt` once, at the end. On a
90-minute recording that is a page watching a clock for twenty minutes with
nothing to show. This module is the second file that fixes it: a
`<out_base>.partial.jsonl` beside the output the request already named, one
JSON object per line, appended and **flushed per segment** so anything tailing
it sees whole lines and nothing else.

**Why a file rather than the two channels that already exist.** The job row is
a download-manager record — `done`/`total` and a capped one-line `detail` — and
putting a transcript through it would make every poll carry the whole
accumulated text to every watcher of every row. `worker_base._stream()`'s
chunked NDJSON is the other, and it is what the text runners use, but the
supervisor relays that stream only while the browser holds the connection open;
transcription is deliberately job-shaped, fire-and-forget, outliving the page
that asked and surviving a navigation. A file keeps that property — a tab that
comes back can read what landed while it was gone — and costs the run one
`write` per segment.

**This module is the whole of the feature and it sits at the runners ROOT**,
beside `worker_base.py`, `formats.py` and `diarize.py`, for the reason AI-10c
states: two engines serve one capability and a page must not be able to tell
which one ran. The line shape, the flush rule and the lifecycle exist once, and
`tests/test_ai_partial_transcript.py` pins that structurally — a second
`partial.py` under either runner folder would fail no behavioural test, because
both copies would pass their own.

**Stdlib only, and no import of `fused_render`.** The same constraint
`formats.py` and `diarize.py` document, for the same reason (each runner runs
on its own interpreter with the app's package off its path) — plus one of its
own: the server imports this module too, to derive the path it advertises to
the page, and it must not need either runner venv to do that.

**It never mutates the segments it is shown.** They ARE the list the worker is
about to dump, and `diarize.assign_speakers` owns the `speaker` key on it at
the end. The sink labels its own copy of each line, which is what keeps a
transcript's final bytes identical to one written before this existed.
"""

from __future__ import annotations

import json
import os
import sys

# `diarize` sits in THIS directory. Two loaders reach it — the runner puts
# `runners/` on `sys.path`, the server imports this file as
# `fused_render.ai.runners.partial` — and the package-relative one must not be
# bypassed: a second copy of the overlap arithmetic under a second module name
# is exactly the drift `diarize.py` exists to prevent.
try:
    from . import diarize
except ImportError:  # pragma: no cover - the runner reading, exercised in prod
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import diarize


#: What replaces the transcript's `.json`. `.partial` alone would be a file the
#: preview machinery has no mode for; `out.json.partial.jsonl` would sort beside
#: the transcript and read like one, in a directory (`ai/transcripts/`) a user
#: browses. `.jsonl` is what it is: newline-delimited JSON, tailable.
SUFFIX = ".partial.jsonl"


def partial_path(out: str | None) -> str | None:
    """Where the partial transcript for `out` goes, or None if there is no out.

    Derived in ONE place because three parties have to name the same file: the
    route advertises it, the worker writes it, and `runtime.js` tails it. A
    page is never asked to string-munge one path out of another — that is the
    rule `/api/ai/transcribe` already follows for `output`/`outputText`.
    """
    if not out:
        return None
    return os.path.splitext(out)[0] + SUFFIX


class Sink:
    """An append-only view of a transcript being decoded.

    Use it as a context manager around the whole decode INCLUDING the final
    write, because the exit is the lifecycle: a clean exit means the real
    output landed and the partial file is now duplicate bytes, so it goes; an
    exception in `cancelled` means the user pressed ✕ and does not want this
    transcript at all, so it goes too; any other exception LEAVES it, because a
    run that died at minute 80 of 90 has 80 minutes of transcript in it and
    that file is the only salvage there is.

    `cancelled` is an argument rather than an import of `worker_base.Cancelled`
    for the reason `diarize.model_paths` takes its downloader as one: this
    module is read by the server, where `worker_base` is not the right import
    and its side effects are not wanted.
    """

    def __init__(self, path: str | None, turns=None, spans=None, cancelled=()):
        self.path = path or None
        self._turns = turns
        self._spans = spans
        self._cancelled = tuple(cancelled)
        self._handle = None

    def add(self, segment) -> None:
        """Append one decoded segment, complete and visible before returning.

        One `write` of a whole line then a `flush`: the reader is tailing this
        file rather than waiting for it, so a buffered writer would show
        nothing until the run ended (the failure this feature exists to fix)
        and a flush landing mid-line would hand it half a JSON object.
        """
        if self.path is None:
            return
        line = {"start": segment.get("start"), "end": segment.get("end"),
                "text": segment.get("text")}
        # **Word timings ride along, and this line is why the rebuild above is a
        # trap** (D392). The dict is built KEY BY KEY rather than copied, which
        # is deliberate — it is what keeps an engine's logprobs and temperatures
        # out of a file a page reads — but it also means a genuinely public field
        # is dropped unless it is named here. `words` was, and the only symptom
        # was `onSegment` handing a page segments with no timings while the final
        # `.json` had them: the reader counts DELIVERED LINES, so a segment sent
        # live without its words is never re-sent with them, and a karaoke view
        # built on the callback stayed empty for the whole run while the same
        # page worked off `rec.segments`. Anything added to a segment that a
        # caller is meant to see has to be added here too.
        if segment.get("words") is not None:
            line["words"] = segment["words"]
        # Labelled HERE rather than read off the segment, because the segment
        # does not have a speaker yet — `assign_speakers` runs once at the end,
        # over the whole list. It can be labelled this early because the turns
        # come from a pre-pass over the full waveform that finished before the
        # first word was decoded (D309), and it is the SAME arithmetic, so the
        # label a page renders now is the one the final file will carry.
        #
        # **`spans` is part of "the same arithmetic", not a refinement of it.**
        # `mlx_whisper` packs VAD regions into one decode (AI-10f, D366), so a
        # segment can straddle a pause that was never transcribed, and the final
        # `assign_speakers` masks its scoring to where the speech actually was.
        # Without the same mask here the two labellings disagree on exactly those
        # segments — the page renders the speaker who was heard in the removed
        # silence and then watches the name and the colour change when the run
        # finishes, which is the failure the paragraph above claims cannot happen.
        # None (every engine that drops no silence) scores the whole span.
        if self._turns is not None:
            index = diarize.speaker_for(line["start"], line["end"], self._turns,
                                        spans=self._spans)
            line["speaker"] = None if index is None else diarize.label(index)
        if self._handle is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # "w", not "a": the uid in `out_base` makes a collision impossible
            # in practice, and "impossible in practice" is how a transcript ends
            # up with a previous run's sentences at the top of it.
            self._handle = open(self.path, "w", encoding="utf-8")
        # `ensure_ascii=False`, matching the final dump — and load-bearing
        # beyond consistency: the reader tails this file by BYTE offset, and
        # escaping here would not corrupt anything but would make every
        # non-ASCII transcript's offsets disagree with its own content length.
        self._handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._handle.flush()

    def discard(self) -> None:
        """Close and remove the file, if there is one. Best-effort by design.

        This runs on the way OUT of the context manager, which a clean exit
        reaches only after the real `.json` and `.txt` have already been
        written — so anything raised here reports a finished transcript as a
        failed run, in exchange for a tidier directory. That trade is never
        worth taking, so every `os.remove` failure is swallowed, not just the
        absent-file one: a run cancelled during the audio decode arrives having
        never written a segment (FileNotFoundError), and a page tailing this
        file through `/api/fs/raw` can be holding a Windows lock over it
        (PermissionError) at the exact moment the transcript lands. A partial
        file that outlives its run is duplicate bytes; a crash here is the
        result."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self.path is None:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass

    def close(self) -> None:
        """Stop writing but KEEP the file — what an error exit does."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "Sink":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None or (self._cancelled and issubclass(exc_type,
                                                               self._cancelled)):
            self.discard()
        else:
            self.close()
        return False


def sink(path: str | None, turns=None, spans=None, cancelled=()) -> Sink:
    """A `Sink` for `path`, or a working no-op one when `path` is falsy.

    The no-op is what keeps this additive: a worker request from before this
    feature carries no `outPartial`, and it must run exactly as it did — no
    file, no directory made, and no `if partial:` branch in either decode loop.

    `spans` is `diarize.speaker_for`'s scoring mask, passed through so a caller
    that drops silence out of the middle of its clips says so ONCE — to this and
    to `assign_speakers` — rather than having two labellings that agree only for
    as long as somebody keeps them in step.
    """
    return Sink(path, turns=turns, spans=spans, cancelled=cancelled)
