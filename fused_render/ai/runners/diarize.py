"""Speaker diarization: who spoke when, and which words are theirs (SPEC §40).

`fused.ai.transcribe({diarize: true, speakers: N})` puts a `speaker` on every
segment. **This module is the whole of that feature**, and it sits at the
runners ROOT — beside `worker_base.py` and `formats.py` — rather than inside
either whisper folder, for the reason AI-10c states about `vad`: two engines
serve one capability, and a page must not be able to tell which one ran. A
second copy under `faster_whisper/` would be two implementations of one promise,
free to drift on the labels, the tie-breaks and the output shape. There is one,
and both venvs import it through the same `sys.path` insert that reaches
`worker_base`.

**Stdlib, numpy and sherpa-onnx only — no import of `fused_render`.** The same
constraint `formats.py` documents, for the same reason: each runner runs on its
own interpreter with the app's package deliberately off its path. The server
imports this module too (for `speakers_or_raise`, so the bridge is not the only
door that enforces the argument), which is why every heavy import below is
inside a function: reading the validation rule must not need sherpa-onnx.

**sherpa-onnx, not pyannote.audio**, and the dependency is the decision. pyannote
needs PyTorch, which `mlx_whisper/pyproject.toml` refuses on principle for a
small helper model — gigabytes in the venv whose selling point is that it is
small — and `pyannote/speaker-diarization-3.1` is `gated: auto`, so an
unattended download fails without a Hub token on a machine nobody is sitting at.
sherpa-onnx runs on ONNX Runtime, which the MLX runner already carries for the
VAD, and both models below are ungated.

**The count is REQUIRED, never guessed.** `speakers` is not an optimisation:
sherpa's clustering either takes a cluster count or a distance threshold, and a
threshold is a number nobody outside a lab can set meaningfully — the same
recording answers 2, 4 or 7 across its plausible range. Guessing produces a
transcript that is confidently wrong about how many people are in the room,
which is worse than a refusal. So it is validated in three places (the bridge,
the server and each worker) and the rule is written down once, here.

**Diarization runs on the FULL waveform, independent of the VAD.** It is the
segmenter's own job to find the silence, it is much better at it than a
threshold over Silero probabilities, and feeding it VAD regions would mean
clustering voices across cuts that were made for a different purpose. Turns come
back in original-recording time; `_transcribe_regions` already maps every
segment back into that same clock, so `assign_speakers` is a join in one
coordinate system rather than a remap.
"""

from __future__ import annotations

import os
import sys

# `formats` sits in THIS directory. Under a runner it is reached as a top-level
# module (the worker puts `runners/` on `sys.path`); under the server this file
# is `fused_render.ai.runners.diarize` and the package-relative import is the
# one that must not be bypassed — a second copy of `COMPONENT_REPOS` under a
# second module name is exactly the drift `formats.py` exists to prevent.
try:
    from . import formats
except ImportError:  # pragma: no cover - the runner reading, exercised in prod
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import formats


#: Who is speaking WHEN — a pyannote segmentation model, ONNX-exported.
#:
#: `csukuangfj/…` rather than `pyannote/segmentation-3.0` itself: the original
#: is MIT but it is a PyTorch checkpoint and it is gated, and this is the
#: ungated ONNX re-export sherpa-onnx is built to read. See `formats.py`.
SEGMENTATION_REPO = "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0"

#: …and which turns are the SAME person — a speaker-embedding model.
#:
#: **Not `Wespeaker/wespeaker-voxceleb-resnet34-LM`, and this was measured
#: rather than assumed.** That is the WeSpeaker team's own repo, it is ungated,
#: it is CC BY 4.0, and it carries a `voxceleb_resnet34_LM.onnx` — but the file
#: has no `model_type` in its ONNX metadata, and sherpa-onnx's extractor does
#: not raise on that, it prints "Unknown model type" and ABORTS THE PROCESS from
#: C++. A worker that died with no Python traceback would reach the job row as
#: "the transcription process did not answer". `csukuangfj/speaker-embedding-
#: models` is the same weights with sherpa's metadata added; the attribution
#: that CC BY 4.0 asks for is in this repo's `COMPONENT_REPOS` entry, which is
#: what the AI Models page renders.
EMBEDDING_REPO = "csukuangfj/speaker-embedding-models"

#: Read from `formats.COMPONENT_REPOS` — the ONE place the ids and filenames of
#: repos this app downloads on its own behalf are written down, so a row on the
#: AI Models page can never be a file nobody can explain.
SEGMENTATION_FILE = formats.COMPONENT_REPOS[SEGMENTATION_REPO]["file"]
EMBEDDING_FILE = formats.COMPONENT_REPOS[EMBEDDING_REPO]["file"]

#: sherpa's own defaults, restated so the two engines cannot pick up different
#: ones from different library versions: a turn shorter than `MIN_DURATION_ON`
#: is dropped, and a gap shorter than `MIN_DURATION_OFF` does not end one.
MIN_DURATION_ON = 0.3
MIN_DURATION_OFF = 0.5

#: How much of a lead counts as a real one when two speakers overlap a segment.
#: Overlaps are differences of floats and two turns that are equal on paper
#: rarely are in binary, so a bare `==` tie-break would never fire and the
#: winner would be whichever rounding error was larger. A microsecond is far
#: below any boundary either model can resolve.
_TIE_S = 1e-6

#: The most speakers this will accept. Not a property of the model — sherpa
#: clusters whatever it is given — but a wrong `speakers` is a typo far more
#: often than it is a conference call, and 100 embeddings from a 30-second clip
#: is minutes of clustering for an answer nobody wanted.
MAX_SPEAKERS = 100


# ------------------------------------------------------------------ the argument


def speakers_or_raise(value):
    """`speakers` as an int, or `ValueError` naming what is wrong with it.

    The rule in one place, called from the server (turned into a 400) and from
    each worker (a `ValueError` the supervisor reports). `runtime.js` states the
    same rule a fourth time in JavaScript, so the caller fails before a job row
    exists — three implementations of one sentence, which is why the sentence is
    pinned by a test rather than trusted to stay in step.

    `bool` is rejected explicitly because `True` is an `int` in Python and
    `diarize: true, speakers: true` is a plausible typo that would otherwise
    read as one speaker and produce a transcript labelled entirely "Speaker 1".
    """
    if value is None or value == "":
        raise ValueError(
            "'speakers' is required when 'diarize' is true — say how many "
            "people are in the recording, e.g. {diarize: true, speakers: 2}. "
            "It cannot be guessed reliably, and a wrong guess relabels the "
            "whole transcript.")
    if isinstance(value, bool) or not isinstance(value, int):
        # A float is refused rather than truncated: `2.0` is harmless and `2.7`
        # is a caller who computed the count and got it wrong, and there is no
        # reading of "2.7 speakers" worth silently turning into 2.
        raise ValueError(
            f"'speakers' must be a whole number of people, not "
            f"{type(value).__name__}")
    if value < 1:
        raise ValueError(f"'speakers' must be at least 1, not {value}")
    if value > MAX_SPEAKERS:
        raise ValueError(
            f"'speakers' must be at most {MAX_SPEAKERS}, not {value}")
    return value


# ------------------------------------------------------------------- the models


def model_paths(download_file):
    """Fetch both ONNX models, returning `(segmentation, embedding)` paths.

    Takes the downloader as an ARGUMENT rather than importing `worker_base`, the
    way `vad.py` does: it keeps this module free of the runner's plumbing, lets
    a test drive it with local files, and `worker_base.download_file` reports
    its own progress so the ~33MB lands on the job row like anything else.

    **Not pre-fetched by Download, unlike the VAD, and that asymmetry is the
    point.** `vad` defaults to TRUE, so a whisper model downloaded on wifi and
    used on a train would quietly lose a feature the user never asked for;
    `diarize` defaults to false, so charging every transcription download 33MB
    for a flag most callers never pass is a cost with no matching benefit. The
    consequence is stated rather than hidden: the first diarized transcription
    on a machine downloads these, and an offline machine's first one fails.
    """
    return (download_file(SEGMENTATION_REPO, SEGMENTATION_FILE,
                          detail="Fetching the speaker segmenter…"),
            download_file(EMBEDDING_REPO, EMBEDDING_FILE,
                          detail="Fetching the speaker embedding model…"))


def diarizer(segmentation_path, embedding_path, speakers):
    """A configured `sherpa_onnx.OfflineSpeakerDiarization` for `speakers` people.

    `num_clusters=speakers` is the whole reason the count is required: the other
    branch of sherpa's clustering is a cosine `threshold`, and that number is
    not one a page author can hold an opinion about (see the module docstring).

    CPU provider explicitly, for `vad.py`'s reason: 33MB of ONNX that runs in
    seconds does not need a second accelerator backend beside the one holding
    the Whisper weights, and onnxruntime's provider-fallback warnings would land
    in the worker log on every transcription.
    """
    import sherpa_onnx

    for path, what in ((segmentation_path, "speaker segmenter"),
                       (embedding_path, "speaker embedding model")):
        if not os.path.isfile(path):
            # Named here because sherpa's own answer to a missing file is a
            # C++ log line and a process abort, which reaches the job row as
            # "the transcription process did not answer".
            raise FileNotFoundError(f"the {what} is missing at {path}")

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=segmentation_path),
            num_threads=1, provider="cpu"),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=embedding_path, num_threads=1, provider="cpu"),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=int(speakers)),
        min_duration_on=MIN_DURATION_ON,
        min_duration_off=MIN_DURATION_OFF)
    if not config.validate():
        # `validate()` returns False and logs the reason; constructing anyway
        # is what aborts the process, so this is the last point at which a
        # misconfiguration can still be a Python exception.
        raise RuntimeError(
            "the speaker diarization models could not be configured — see the "
            "worker log for what sherpa-onnx rejected")
    return sherpa_onnx.OfflineSpeakerDiarization(config)


# --------------------------------------------------------------------- the turns


def speaker_turns(audio, session, sample_rate, should_stop=None):
    """`[(start_s, end_s, speaker_index), …]`, sorted by start time.

    `audio` is the FULL mono float32 waveform — the same array the VAD is
    given, not the regions it produced. Both engines hand over the same thing,
    which is what makes the labels identical across them.

    **`sample_rate` is checked, not trusted.** Each engine decodes at the rate
    its own Whisper front end wants and the models here are 16 kHz exports;
    every turn's `start`/`end` is samples divided by the rate sherpa assumes, so
    a mismatch does not fail — it returns turns whose timestamps are wrong by a
    ratio, which then land on the wrong segments and produce a transcript
    confidently attributed to the wrong people. The two rates agree today; this
    is the assertion that keeps them agreeing.

    `should_stop` is an optional predicate polled once per processed chunk. It
    is the only way a ✕ reaches inside sherpa's C++ loop; returning True aborts
    it, and this function then raises `Cancelled` — which is not a type this
    module can name (it belongs to `worker_base`, which this module deliberately
    does not import), so the caller passes a predicate and gets a
    `DiarizationCancelled` to translate.

    The speaker is an INDEX, not a label. `assign_speakers` is the only thing
    that turns one into a string, so the two engines cannot spell them
    differently, and sorting a list of them is unambiguous where sorting
    "Speaker 10" against "Speaker 2" is not.
    """
    import numpy as np

    expected = int(session.sample_rate)
    if int(sample_rate) != expected:
        raise ValueError(
            f"speaker diarization needs {expected} Hz audio and was handed "
            f"{int(sample_rate)} Hz — the turns would be off by a ratio and "
            "every speaker label with them")

    samples = np.ascontiguousarray(audio, dtype=np.float32)
    cancelled = {"stopped": False}

    def progress(processed, total):
        if should_stop is not None and should_stop():
            cancelled["stopped"] = True
            return 1  # any non-zero value aborts sherpa's loop
        return 0

    result = session.process(samples, callback=progress)
    if cancelled["stopped"]:
        # The aborted result is a prefix of the recording, and a partial
        # diarization would label the first minute and leave the rest None —
        # a transcript that looks diarized and is not.
        raise DiarizationCancelled()
    return [(float(turn.start), float(turn.end), int(turn.speaker))
            for turn in result.sort_by_start_time()]


class DiarizationCancelled(Exception):
    """`should_stop` said so. Translated by the caller into its own cancel."""


# -------------------------------------------------------------------- the join


def label(speaker_index):
    """The public name of a speaker: "Speaker 1", "Speaker 2", …

    One-based because this is read by people and there is no zero-th person in
    a room. Defined once so both engines emit the same string for the same
    index — which is the whole of "same labels" in AI-10c, and is why
    `speaker_turns` hands back an integer.
    """
    return "Speaker %d" % (int(speaker_index) + 1)


def speaker_for(start, end, turns):
    """Which speaker `[start, end)` overlaps MOST, or None if it overlaps none.

    Overlap in TIME, not nearest-turn or midpoint-containment, because a
    Whisper segment is a sentence and a sentence routinely straddles a
    hand-over: "…yeah — no, I think" can begin in one person's turn and end in
    another's, and the person who said most of it is the honest label.

    **Summed PER SPEAKER, not per turn.** One speaker can hold several turns
    inside one segment (a short interjection splits theirs in two), and taking
    the single longest TURN would hand the segment to the interrupter who spoke
    for two seconds over the person who spoke for six across three turns.

    **Ties go to whoever started speaking first** — the speaker whose earliest
    overlapping turn begins earliest, and if even that is equal, the lower
    speaker index. A tie is rare and either answer is defensible; what is not
    defensible is dict iteration order deciding it, because then the same
    recording labels differently on two runs and a page's speaker colours
    shuffle between them. Determinism is the property being bought.

    None — not "Speaker 1" — when nothing overlaps: Whisper heard words where
    the segmenter heard nobody, and inventing a speaker there is the confident
    version of a wrong answer. A zero-length segment lands here too, which is
    the same honest answer for the same reason.
    """
    totals = {}
    earliest = {}
    for turn_start, turn_end, speaker in turns:
        overlap = min(end, turn_end) - max(start, turn_start)
        if overlap <= 0:
            continue
        totals[speaker] = totals.get(speaker, 0.0) + overlap
        earliest[speaker] = min(earliest.get(speaker, turn_start), turn_start)
    if not totals:
        return None
    best = max(totals.values())
    tied = [speaker for speaker, total in totals.items() if total >= best - _TIE_S]
    if len(tied) == 1:
        return tied[0]
    return min(tied, key=lambda speaker: (earliest[speaker], speaker))


def assign_speakers(segments, turns):
    """Put a `speaker` on every segment, and return the labels that landed.

    Mutates in place, because the segments ARE the transcript both engines are
    about to write and copying them would leave two lists to keep in step.

    The returned list is the transcript's legend — the top-level `speakers` in
    the written JSON — and it is derived from what was actually ASSIGNED rather
    than from the turns. A speaker the segmenter heard during a stretch Whisper
    transcribed no words from would otherwise appear in a legend that nothing in
    the transcript refers to, which reads as a bug in the page rendering it.
    Sorted by index, so "Speaker 10" sorts after "Speaker 2" rather than before
    it — the reason `speaker_turns` carries integers this far.
    """
    used = set()
    for segment in segments:
        speaker = speaker_for(segment["start"], segment["end"], turns)
        segment["speaker"] = None if speaker is None else label(speaker)
        if speaker is not None:
            used.add(speaker)
    return [label(speaker) for speaker in sorted(used)]
