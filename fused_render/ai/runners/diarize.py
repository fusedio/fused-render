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

**The count is a HINT: given, it is obeyed; omitted, it is estimated** (D318).
sherpa's clustering takes either a cluster count or a cosine distance
threshold, and this module offers both — `speakers: 3` fixes three clusters and
is the path that existed first, while an absent `speakers` clusters by distance
and lets the recording answer. The earlier reading of this file said the
threshold was a number nobody outside a lab could set and therefore REFUSED the
absent case; what that missed is that the app can set it once, for everyone,
which is what every other transcription product does. A wrong estimate is still
a real cost — it is why the count remains the better answer whenever the caller
has one — but a refusal is not the alternative it was taken to be. The value is
still validated in three places (the bridge, the server and each worker),
because a BAD count (`0`, `-1`, `true`, `"2"`) is a typo either way, and the
rule is written down once, here.

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

#: How many CPU threads BOTH ONNX models get. One constant for the two of them,
#: for the reason `MIN_DURATION_ON` is one: two engines read this module and a
#: segmenter configured apart from its embedder is drift no behavioural test
#: would catch. Unlike those two, this value is **not** sherpa's default and is
#: not restating anything upstream chose — it is measured here.
#:
#: It was `1`, under a comment that thought it was restating a library default.
#: The segmentation pass is the dominant cost of a diarized transcription, not
#: the footnote the CPU-provider note below used to call it: on a 216-second
#: recording on a 10-core Apple Silicon machine it ran in 26.64s on one thread,
#: 14.80s on two and 11.55s on four. Embedding and clustering together measured
#: ~0s, so this number is essentially the whole of the phase's wall clock.
#:
#: **The output was identical at 1, 2, 4 and 8 threads** — the same 48 turns and
#: the same 6 speakers — which is the evidence that made this safe to change, and
#: the limit of that evidence is one machine and one recording. It is not a
#: guarantee: ONNX Runtime's intra-op parallelism can in principle reorder
#: floating-point reductions, which would perturb the posteriors and so the turn
#: boundaries, and this value is host-dependent by design. `diarizer` states what
#: is and is not promised as a result.
#:
#: **The cap of 4 is load-bearing, and this is the reason it is not a magic
#: number:** eight threads measured 16.79s — SLOWER than four's 11.55s — on that
#: same 10-core part. Every current Apple Silicon design is performance cores
#: plus efficiency cores, so threads past the P-core count land on the slow ones
#: and the segmentation pass ends up waiting for them. `os.cpu_count()` uncapped
#: is therefore a pessimisation on every machine this runner ships to, in the
#: same way `1` was in the other direction. `or 1` because `os.cpu_count()` is
#: documented to be able to return None, and `num_threads=None` is a C++ config
#: field this process would abort on rather than raise about.
NUM_THREADS = min(4, os.cpu_count() or 1)

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

#: The cosine distance at which two voices stop being the same person, used
#: when the caller did not say how many there are.
#:
#: **sherpa-onnx's own default**, restated here for the reason `MIN_DURATION_ON`
#: is: the number a transcript's speaker labels come out of must not be
#: whatever the installed wheel happens to default to this month, and the two
#: engines must not be able to pick up different ones. Larger merges more —
#: two people become one — and smaller splits one person across a cough or a
#: change of microphone distance. 0.5 is where sherpa's own examples sit for the
#: WeSpeaker embeddings this module uses, which is the pairing the number was
#: chosen against; there is no measurement of our own behind it, and that is
#: exactly why a caller who KNOWS the count should still pass it.
CLUSTER_THRESHOLD = 0.5


# ------------------------------------------------------------------ the argument


def speakers_or_raise(value):
    """`speakers` as an int, `None` when it is absent, or `ValueError`.

    The rule in one place, called from the server (turned into a 400) and from
    each worker (a `ValueError` the supervisor reports). `runtime.js` states the
    same rule a fourth time in JavaScript, so the caller fails before a job row
    exists — three implementations of one sentence, which is why the sentence is
    pinned by a test rather than trusted to stay in step.

    **None is an ANSWER, not a failure** (D318): it means "estimate it", and
    `diarizer` turns it into threshold clustering. An empty string answers the
    same way, because that is what an untouched number input sends and a
    documented default reachable only by deleting the key is not a default.

    A bad EXPLICIT value is still refused, and that half is unchanged: `0`,
    `-1`, `2.7` and `"2"` are typos whether or not the argument is optional,
    and reading them as "estimate" would turn a caller's mistake into a
    silently different transcript.

    `bool` is rejected explicitly because `True` is an `int` in Python and
    `diarize: true, speakers: true` is a plausible typo that would otherwise
    read as one speaker and produce a transcript labelled entirely "Speaker 1".
    """
    if value is None or value == "":
        return None
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
    way `vad.py` does: it keeps this module free of the runner's plumbing and
    lets a test drive it with local files. It also carries the caller's job row,
    which matters here more than it does for the detector — this fetch happens
    inside a TRANSCRIPTION, so the caller passes a downloader already bound to
    the row the user is watching rather than to the model's own load row.

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
    """A configured `sherpa_onnx.OfflineSpeakerDiarization`.

    `speakers` is a count, or **None to estimate it** (D318), and that single
    argument chooses between the two branches of sherpa's clustering:

    * a count fixes `num_clusters`, and the recording is cut into exactly that
      many voices whether or not that is how many spoke;
    * None leaves `num_clusters` negative and hands over `CLUSTER_THRESHOLD`
      instead, so voices merge by cosine distance and the count falls out.

    The fixed branch is spelled exactly as it was before the other one existed,
    with no `threshold` passed: a caller who gives the count must not have their
    clustering quietly re-tuned by a feature they did not ask for, and the surest
    way to promise that is to leave its call untouched.

    **That promise is about the CALL, not about the bytes**, and the distinction
    is not pedantry — it used to read "the same transcript they got yesterday,
    byte for byte", which is more than this file can honestly claim now that
    `NUM_THREADS` derives from `os.cpu_count()`. ONNX Runtime's intra-op
    parallelism can in principle change the order of floating-point reductions,
    and a different order perturbs the segmentation posteriors and therefore the
    turn boundaries. What was actually established is narrower than a guarantee
    and is worth writing down as such: on ONE machine and ONE 216-second
    recording, 1, 2, 4 and 8 threads produced identical output — the same 48
    turns and the same 6 speakers. That is real evidence that this model's
    reductions are stable under threading, and it is not proof that they must be
    on every part and every recording. A transcript that differs across machines
    by a few milliseconds of turn boundary is the accepted cost of the phase
    taking 11.5s instead of 26.6s (see `NUM_THREADS`); what is NOT accepted is
    the label set changing, which is what the count-fixing branch above pins.

    CPU provider explicitly, and NOT because this is cheap — it is not. The
    segmentation pass is the dominant cost of a diarized transcription (26.6s of
    a 216-second recording on one thread; see `NUM_THREADS`), and the sentence
    that used to sit here — "33MB of ONNX that runs in seconds" — asserted a
    timing this phase never had. What survives that correction is the rest of
    `vad.py`'s reason, which does not depend on the size of the model: a second
    accelerator backend beside the one holding the Whisper weights buys nothing
    measured (CoreML measured SLOWER than CPU for ASR — sherpa-onnx#2910), and
    onnxruntime's provider-fallback warnings would land in the worker log on
    every transcription. The answer to the cost is `NUM_THREADS`, not a
    different provider.
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
            num_threads=NUM_THREADS, provider="cpu"),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=embedding_path, num_threads=NUM_THREADS, provider="cpu"),
        clustering=(
            sherpa_onnx.FastClusteringConfig(num_clusters=int(speakers))
            if speakers is not None else
            # -1 rather than 0, which sherpa also reads as "not fixed": it is
            # the value sherpa's own default carries, so a config printed into
            # the worker log looks like the library's rather than like a
            # sentinel this app invented.
            sherpa_onnx.FastClusteringConfig(num_clusters=-1,
                                             threshold=CLUSTER_THRESHOLD)),
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


def speaker_count(turns):
    """How many distinct people the clustering settled on.

    What an ESTIMATING run reports back to its caller — the resolved count that
    a fixed run already knows because it supplied it. Derived from the turns
    rather than asked of sherpa, because the turns are the only thing either
    worker keeps hold of and they are what the labels come from, so this number
    and the transcript's legend cannot describe different recordings.

    It counts the SEGMENTER's answer, not the transcript's: a person the
    segmenter heard during a stretch Whisper found no words in is counted here
    and is absent from the legend (`assign_speakers` explains why the legend is
    the narrower list). Both are true and they answer different questions —
    "how many people are in this recording" and "how many are quoted in this
    transcript".
    """
    return len({int(speaker) for _, _, speaker in turns})


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
