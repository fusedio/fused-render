"""Silero VAD over ONNX Runtime: which parts of a recording contain speech.

**Why this file exists at all is PARITY, not performance.** `fused.ai.transcribe`
takes a `vad` flag, and the engines serving that capability must mean the same
thing by it: `faster_whisper` runs a real Silero VAD filter over the waveform
and drops the silence, while the MLX runner could only map the flag onto
mlx-whisper's per-window `no_speech_threshold`. Same public API, same argument,
two behaviours — which is the one thing a second runner for a capability is not
allowed to be (SPEC AI-10c).

**It sits at the runners ROOT**, beside `worker_base.py`, `formats.py`,
`diarize.py` and `partial.py`, and it moved here the moment a SECOND engine
needed it (D319 — the Parakeet runner). It began inside `mlx_whisper/` because
one runner used it and the CT2 engine had faster-whisper's own copy; that was
the right home for exactly as long as there was one caller. Two callers of one
promise is what the root is for: a copy under `parakeet_mlx/` would be two
implementations of "what `vad: true` means", free to drift on the threshold, the
minimum silence and the padding, and neither copy would fail a test — each would
pass its own. Its readers reach it through the same `sys.path` insert that
reaches `worker_base`.

**Its readers are the two MLX engines, not all three.** `faster_whisper/`
neither imports this nor should: faster-whisper ships the same Silero inside
itself and takes `vad_filter=True`, so asking for it there is one argument to a
library that already has the model. This file exists because the MLX engines
have no such library — which is the whole of AI-10f's argument, one engine
further on.

**It owns the PACKING too, not just the detection** (`pack_regions`,
`packed_samples` and the inverses `original_start`/`original_end`, at the foot of
this file). Deciding what the decoder is actually HANDED — one clip per region,
or the speech concatenated into as few clips as fit — is part of what `vad: true`
means, and this module is the one place allowed to define that for both MLX
engines; `mlx_whisper/worker.py` is the only caller today only because Parakeet
has no fixed window to pack into, which is a fact about that engine rather than
a different reading of the flag. The arithmetic is stdlib and numpy, so a
caller that wants it does not need onnxruntime to be installed.

Three constraints shaped it, and each one closed off the obvious route:

* **Not `faster_whisper`'s copy of Silero.** It is right there and it is the
  same model — but importing it would pull `ctranslate2` into an MLX runner's
  venv, which is the whole thing the runner-folder split exists to prevent (a
  Metal-only environment does not get to carry a CPU inference engine because
  one utility module lives inside it).
* **Not the PyTorch Silero.** `torch.hub` would be a multi-GB dependency for a
  2MB model, in the venv whose entire selling point is that it is small.
* **So: the ONNX export, run on `onnxruntime`.** `onnx-community/silero-vad`
  is ungated (the rule `catalog.py` states about gated repos applies to
  everything this app downloads, not only to models a user picks) and the file
  is 2.2MB. Nothing here shells out, and no system ffmpeg is involved — the
  waveform arrives already decoded by the calling `worker.py`'s `av` path.

**Stdlib, numpy and onnxruntime only — no import of `fused_render`**, the same
constraint `formats.py` and `diarize.py` document: every runner reads this on
its own interpreter, with the app's package deliberately off its path.

The model is a STREAMING detector: 512 samples in, one speech probability out,
plus a recurrent state that must be carried to the next window. That is why this
is a loop rather than one array operation, and why `_probabilities` threads
`state` through — passing a fresh state per window silently degrades it to a
frame-energy detector, which is the failure mode that looks like it works.
"""

import os
import sys

# `formats` sits in THIS directory now — the same insert `worker.py` makes,
# repeated because this module is also imported by path on its own (by its
# test, and by anything that wants the region maths without onnxruntime).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import formats  # noqa: E402 - the path insert above is what makes it importable

#: Silero's own frame size at 16 kHz, and not a tunable: the exported graph is
#: built for 512 samples in and one probability out. A different window does not
#: fail, it produces nonsense.
WINDOW = 512

#: The repo and file — read from `formats.COMPONENT_REPOS`, which is the ONE
#: place the ids of repos this app downloads on its own behalf are written down.
#: They used to be literals here, "beside the code that uses them", and the cost
#: of that was a row on the AI Models page the page could not explain: the
#: detector lands in the Hub cache like any model, and the server process cannot
#: import this venv to ask what it is. `formats.py` is importable from both
#: sides, which is the whole reason it exists.
_COMPONENT = "onnx-community/silero-vad"
REPO = _COMPONENT
FILE = formats.COMPONENT_REPOS[_COMPONENT]["file"]

#: Above this a window is speech. Silero's own default, and the same value
#: faster-whisper uses, so the two engines draw the line in the same place.
THRESHOLD = 0.5

#: A run of speech shorter than this is dropped as a click, a breath or a door.
MIN_SPEECH_S = 0.25

#: …and a gap shorter than this does not END a region. Without it every pause
#: between two words becomes a boundary, and the recording is cut into hundreds
#: of fragments — which costs accuracy (each fragment is transcribed with no
#: context) far more than it saves time.
MIN_SILENCE_S = 0.5

#: Kept either side of a region, because a detector tuned to find speech clips
#: its own edges: the onset of the first consonant and the tail of the last
#: vowel sit below the threshold and are exactly the samples a transcript needs.
#: Overlapping regions are merged afterwards, so this can never reorder them.
PAD_S = 0.2

#: The most SPEECH one packed clip may carry, in seconds — see `pack_regions`.
#:
#: **29, not 30, and the missing second is the point.** Whisper's window is 30
#: seconds exactly (`mlx_whisper.audio.N_FRAMES = 3000` at 100 frames a second)
#: and every `transcribe()` call pads its mel to the full window, so a clip that
#: tips a hair over it buys a SECOND, nearly empty encoder pass — the exact cost
#: packing exists to remove, reintroduced at the boundary. A hair is all it
#: takes: region ends are rounded to whole samples by `slice_samples` and each
#: one carries `PAD_S` on both sides, so a run of regions that sums to 30.0 on
#: paper is not guaranteed to sum to 30.0 in float. The headroom is worth more
#: than the ~3% of window it gives up.
BUDGET_S = 29.0


def model_path(download_file):
    """Fetch the ONNX model, returning its local path.

    Takes the downloader as an ARGUMENT rather than importing `worker_base`,
    which keeps this module free of the runner's plumbing and lets a test drive
    it with a local file. `worker_base.download_file` reports its own progress,
    so the 2MB pull appears on the job row like any other.
    """
    return download_file(REPO, FILE, detail="Fetching the speech detector…")


def session(path):
    """An ONNX Runtime session for the detector.

    CPU provider explicitly. The model is 2MB and runs in milliseconds per
    minute of audio; handing it to CoreML would put a second accelerator
    backend beside the one holding the Whisper weights for no measurable gain,
    and `onnxruntime`'s provider fallback warnings would land in the worker log
    every load.
    """
    import onnxruntime

    if not os.path.isfile(path):
        # `FileNotFoundError` rather than a bare `RuntimeError`, because
        # `worker.py` sorts this function's failures into two piles by TYPE: a
        # detector that could not be obtained degrades to transcribing
        # everything, and a bug in this file fails loudly. A missing file after
        # a download is the first kind, and an `OSError` is how it says so.
        raise FileNotFoundError(f"the speech detector is missing at {path}")
    options = onnxruntime.SessionOptions()
    # One thread. This runs while nothing else in the process is doing anything,
    # so the pool would only be threads to create and tear down — and
    # onnxruntime's default is to size it to the machine, which on a laptop
    # means spinning up performance cores for a 2MB model.
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return onnxruntime.InferenceSession(
        path, options, providers=["CPUExecutionProvider"])


def _probabilities(audio, sess, sample_rate):
    """One speech probability per 512-sample window, in order.

    The recurrent state is threaded through deliberately — see the module
    docstring. The trailing partial window is dropped rather than zero-padded:
    32ms of audio cannot change a region boundary that `PAD_S` already widens by
    200ms, and padding it would feed the detector a discontinuity.
    """
    import numpy as np

    state = np.zeros((2, 1, 128), dtype=np.float32)
    rate = np.array(sample_rate, dtype=np.int64)
    probabilities = []
    for start in range(0, len(audio) - WINDOW + 1, WINDOW):
        window = audio[start:start + WINDOW].reshape(1, -1)
        output, state = sess.run(None, {"input": window, "state": state, "sr": rate})
        probabilities.append(float(output.reshape(-1)[0]))
    return probabilities


def speech_regions(audio, sess, sample_rate=16000):
    """`[(start_s, end_s), …]` — where the speech is, in seconds of THIS audio.

    Empty when the recording contains no speech at all. The caller decides what
    that means; this function does not get to invent a region to avoid
    returning nothing (see `worker.py`, which transcribes the whole file in that
    case rather than reporting an empty transcript for a recording it never
    looked at).
    """
    per_window = WINDOW / sample_rate
    probabilities = _probabilities(audio, sess, sample_rate)
    duration = len(audio) / sample_rate

    # PASS ONE — runs of speech, ended by a long enough silence.
    runs = []
    start = None
    quiet = 0
    for index, probability in enumerate(probabilities):
        if probability >= THRESHOLD:
            start = index if start is None else start
            quiet = 0
            continue
        if start is None:
            continue
        quiet += 1
        if quiet * per_window >= MIN_SILENCE_S:
            # `+ 1` because the end is EXCLUSIVE — the boundary after the last
            # speech window, matching the open-ended `len(probabilities)` the
            # tail case appends. Without it every region ended one window
            # (32ms) early: invisible here, since `PAD_S` adds 200ms back, but
            # it would silently become a clipped final syllable for anyone who
            # turned the padding down.
            runs.append((start, index - quiet + 1))
            start = None
            quiet = 0
    if start is not None:
        runs.append((start, len(probabilities)))

    # PASS TWO — drop the too-short, pad the rest, merge what now overlaps.
    regions = []
    for first, last in runs:
        if (last - first) * per_window < MIN_SPEECH_S:
            continue
        begin = max(0.0, first * per_window - PAD_S)
        end = min(duration, last * per_window + PAD_S)
        if regions and begin <= regions[-1][1]:
            # Padding made this touch the previous one. Merging keeps the list
            # ordered and non-overlapping, which everything downstream — the
            # progress mapping and the timestamp remap — relies on.
            #
            # **Unreachable with the constants above, and kept deliberately.**
            # A split needs `MIN_SILENCE_S` (0.5s) of quiet and padding closes
            # `2 * PAD_S` (0.4s), so today two regions can never overlap. That
            # is an accident of two numbers that are tuned independently — turn
            # the padding up to 0.3, or the minimum silence down to 0.4, and
            # this becomes live. Leaving it out would make either edit produce
            # overlapping regions and a transcript whose segments go backwards.
            regions[-1] = (regions[-1][0], end)
        else:
            regions.append((begin, end))
    return regions


def slice_samples(audio, region, sample_rate=16000):
    """The samples for one region. Rounded to whole samples, never resampled."""
    start = max(0, int(region[0] * sample_rate))
    end = min(len(audio), int(region[1] * sample_rate))
    return audio[start:end]


def pack_regions(regions, budget=BUDGET_S):
    """Group consecutive regions into clips of at most `budget` seconds of speech.

    Returns `[[(start_s, end_s), …], …]` — a PARTITION of `regions`, in order,
    every region appearing exactly once. Each inner list is one "pack": the
    clip `packed_samples` builds, and simultaneously the whole of the inverse
    map `original_start`/`original_end` need to undo it. That is why the samples
    are not built here: a caller that wanted every clip's audio up front would
    hold a second copy of the whole waveform (345MB for a 90-minute recording),
    and it only ever needs one clip at a time.

    **Why this exists.** mlx-whisper pads every call's mel to the full 30-second
    window, so a 0.8-second region costs the same encoder pass as a 30-second
    one. Decoding one region per call therefore made `vad: true` a
    PESSIMISATION on the large models: on a 216-second recording that is 92%
    speech (31 regions, min 0.8s, median 5.8s, max 14.0s), `large-v3-turbo`
    took 8.32s for the whole file, 23.30s for the 31 raw regions and 9.31s
    packed. faster-whisper never had this defect — its own `vad_filter` calls
    `collect_chunks`, which concatenates speech to a maximum duration and remaps
    the timestamps afterwards — and since this module exists so both MLX engines
    mean the SAME thing by the flag (AI-10f), the two engines sitting 2.8x apart
    on it was the parity problem, not a missed optimisation.

    **A region longer than the budget passes through ALONE and is never split.**
    Cutting mid-speech loses the words at the cut, and Whisper already chunks a
    long input internally with its own seeking, which is better at it than a
    boundary chosen here. It travels alone rather than with a neighbour because
    the clip is already over the window: adding a neighbour would decode that
    neighbour inside the overflow.
    """
    packs = []
    current = []
    filled = 0.0
    for region in regions:
        span = region[1] - region[0]
        # `>` not `>=`: a clip filled to exactly the budget still fits the
        # window, and rejecting it would pay for the extra pass this avoids.
        # `current and` is what lets an over-budget region through: with nothing
        # to flush it starts its own clip, and the NEXT region then finds
        # `filled` already past the budget and opens another.
        if current and filled + span > budget:
            packs.append(current)
            current = []
            filled = 0.0
        current.append(region)
        filled += span
    if current:
        packs.append(current)
    return packs


def packed_duration(pack):
    """Seconds of SPEECH in one pack — the length of the clip, not of the span
    of recording it was cut out of. What the progress clamp and the ETA's rate
    are denominated in: a clip built from six seconds of recording holding two
    seconds of speech is two seconds of decoding."""
    return sum(end - start for start, end in pack)


def packed_samples(audio, pack, sample_rate=16000):
    """One pack's regions, concatenated — speech with the silence dropped.

    The single-region case returns `slice_samples`' view rather than a
    concatenation of one, because a copy buys nothing and the region can be
    long: a pack of one is what an over-budget region travels as, and what a
    caller with no detector at all asks for.
    """
    if len(pack) == 1:
        return slice_samples(audio, pack[0], sample_rate)
    import numpy as np

    return np.concatenate(
        [slice_samples(audio, region, sample_rate) for region in pack])


def original_start(pack, at):
    """`at` seconds into the packed clip → the START of a segment, in RECORDING
    time.

    A time landing exactly ON a join belongs to the region that STARTS there:
    nothing was said at 5.0 in a clip that was not also said at the next
    region's first moment, and attributing it to the previous region's end would
    put a segment's start behind the silence it was cut out of — a segment that
    begins before the speech it transcribes.
    """
    return _at_original(pack, at, join_ends_region=False)


def original_end(pack, at):
    """`at` seconds into the packed clip → the END of a segment, in RECORDING
    time.

    **The mirror image of `original_start`, and the asymmetry is the point.** A
    time on a join means different things for the two ends of a segment: for a
    start it is the next region's first moment, for an end it is the previous
    region's last one. Mapped with start semantics, a segment lying ENTIRELY
    inside one region and merely ending on the join comes back stretched across
    the silence — clip `3.0-5.0` reported as recording `3.0-30.0`, an end late
    by the whole length of the pause, and text asserted to have been spoken
    during silence that was deliberately removed.

    That is not a float coincidence to be shrugged at: the join IS where the
    pause was, so it is the most natural place for Whisper to end a segment, and
    region ends land on its 0.02s timestamp grid regularly (the detector's own
    `WINDOW / 16000` steps and `2 * PAD_S` are both multiples of it). Downstream
    the stretched span is worse than a late caption — `diarize.speaker_for` sums
    turn overlap across it, so a sentence can be attributed to whoever the
    segmenter heard inside the silence this file discarded.

    One grid step past the join is a DIFFERENT case and still maps into the next
    region: a segment ending at 5.02 genuinely continues into it.
    """
    return _at_original(pack, at, join_ends_region=True)


def _at_original(pack, at, join_ends_region):
    """The shared walk. `join_ends_region` is which side of a join `at` falls on
    when it lands exactly there — the whole of the difference between the two
    public functions, kept in one place so they cannot drift on the clamping.

    The inverse of `packed_samples`, and the reason concatenating is safe at
    all. Without it every timestamp after the first join is early by the length
    of the silence that was dropped — a transcript that looks perfect and sends
    a seeking player to the wrong minute.

    **Called once per ENDPOINT, never once per segment.** A segment can span a
    join (Whisper hears continuous speech, because the silence is not in the
    clip it was given), so its start and its end can fall in different source
    regions; mapping the pair through one offset would stretch or squash it.

    Clamped at both ends. Past the speech, because Whisper times against its
    padded window — the last segment of a two-second clip can end at 29, and a
    hallucination in the padding can start there too — and the clamp is what
    turns such a segment into a zero-length one the caller can recognise and
    drop, rather than text placed inside silence that was cut out. Below zero
    for `slice_samples`' reason: a negative time must not wrap round to
    somewhere else in the recording.
    """
    offset = 0.0
    for start, end in pack:
        span = end - start
        if at < offset + span or (join_ends_region and at <= offset + span):
            # `max(start, …)` is the low clamp. The `or` is the asymmetry: an
            # END on the boundary stops here, at this region's last moment,
            # while a START falls through to the region that begins there.
            return max(start, start + (at - offset))
        offset += span
    return pack[-1][1]
