"""Silero VAD over ONNX Runtime: which parts of a recording contain speech.

**Why this file exists at all is PARITY, not performance.** `fused.ai.transcribe`
takes a `vad` flag, and until now the two whisper engines meant different things
by it: `faster_whisper` runs a real Silero VAD filter over the waveform and
drops the silence, while this runner could only map the flag onto
mlx-whisper's per-window `no_speech_threshold`. Same public API, same argument,
two behaviours — which is the one thing a second runner for a capability is not
allowed to be (SPEC AI-10c).

Three constraints shaped it, and each one closed off the obvious route:

* **Not `faster_whisper`'s copy of Silero.** It is right there and it is the
  same model — but importing it would pull `ctranslate2` into the MLX runner's
  venv, which is the whole thing the runner-folder split exists to prevent (a
  Metal-only environment does not get to carry a CPU inference engine because
  one utility module lives inside it).
* **Not the PyTorch Silero.** `torch.hub` would be a multi-GB dependency for a
  2MB model, in the venv whose entire selling point is that it is small.
* **So: the ONNX export, run on `onnxruntime`.** `onnx-community/silero-vad`
  is ungated (the rule `catalog.py` states about gated repos applies to
  everything this app downloads, not only to models a user picks) and the file
  is 2.2MB. Nothing here shells out, and no system ffmpeg is involved — the
  waveform arrives already decoded by `worker.py`'s `av` path.

The model is a STREAMING detector: 512 samples in, one speech probability out,
plus a recurrent state that must be carried to the next window. That is why this
is a loop rather than one array operation, and why `_probabilities` threads
`state` through — passing a fresh state per window silently degrades it to a
frame-energy detector, which is the failure mode that looks like it works.
"""

import os

#: Silero's own frame size at 16 kHz, and not a tunable: the exported graph is
#: built for 512 samples in and one probability out. A different window does not
#: fail, it produces nonsense.
WINDOW = 512

#: The repo and file. Named here rather than in `worker.py` so the one thing a
#: reader wants to check — what gets downloaded onto their machine — is beside
#: the code that uses it.
REPO = "onnx-community/silero-vad"
FILE = "onnx/model.onnx"

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
