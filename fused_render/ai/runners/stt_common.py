"""What the two speech-to-text runners share beyond the HTTP base (SPEC AI-10).

`mlx_whisper/worker.py` and `faster_whisper/worker.py` both reach `vad: true`
through Silero over onnxruntime (`runners/vad.py`) and both decode the resulting
packs one call at a time. The obtaining-the-detector policy and the "what is a
pack, and what does no VAD look like" rule are the same decision on both, so they
live once here and neither worker forks them. (Named `stt_common`, beside
`speech_common`, which is the TTS runners' and shares nothing with this.)

Stdlib-only at import time, like `vad.py`: the heavy imports happen inside
`vad.session`.
"""

import http.client
import sys


#: What "the detector could not be obtained" is allowed to arrive as.
#:
#: `OSError` is almost all of the FETCH: a socket that never connected, a DNS
#: name that did not resolve, a TLS handshake, a timeout and a full disk all
#: arrive as one — and so does every huggingface_hub failure worth degrading on,
#: because `HfHubHTTPError` is declared `(HTTPError, OSError)`, the offline guard
#: derives from `ConnectionError` and the local-cache miss from
#: `FileNotFoundError` (which is also what `vad.session` raises when the download
#: left no file). `http.client.HTTPException` is the one shape that is NOT an
#: `OSError`: a response malformed rather than merely unhappy.
#:
#: `ImportError` is here because onnxruntime is imported inside `vad.session`,
#: and a venv without it is the same outcome for the user as a detector that
#: would not download: this runner transcribes perfectly well without one, and
#: onnxruntime is the dependency in this folder most likely to have no wheel for
#: some future interpreter. It is still not silent — the reason goes to stderr
#: and to the row — and it costs absorbing a misspelt import inside `session`,
#: a four-line function whose only import is a constant.
#:
#: Deliberately NOT `ValueError`: hf raises `HFValidationError(ValueError)` for a
#: repo id that is not a repo id, and the id is a constant in `vad.py` — so that
#: one is a typo in this codebase, not a bad day on the network.
FETCH_FAILED = (OSError, http.client.HTTPException, ImportError)


def onnx_failures():
    """Every exception ONNX Runtime's C++ layer can raise loading a model.

    Named as "the module that contains exactly these and nothing else" rather
    than as a list, because there is nothing else to name: onnxruntime registers
    each of its fifteen error types from pybind with `Exception` as the base, so
    there is no `OrtError` to catch and no shared ancestor short of `Exception`
    itself. Enumerating `Fail`, `InvalidProtobuf`, `NoSuchFile`, … here would
    quietly stop covering a runtime that adds a sixteenth.

    `()` when onnxruntime is absent, which needs no special case: the
    `ImportError` that then comes out of `vad.session` is degradable on its own
    account (see `FETCH_FAILED`).
    """
    try:
        from onnxruntime.capi import onnxruntime_pybind11_state as ort_errors
    except ImportError:
        return ()
    return tuple(value for value in vars(ort_errors).values()
                 if isinstance(value, type) and issubclass(value, Exception))


def speech_regions(base, cache, audio, total, job, row):
    """Where the speech is, or `None` when the whole file should be decoded.

    Returns a list of `(start, end)` in seconds. `None` — not an empty list —
    means "do not filter", and the two are deliberately different answers:
    an empty list is the detector saying there is no speech in this recording,
    while None is this function saying it could not ask.

    **A detector that cannot be fetched degrades to no filtering rather than
    failing the transcription.** The trade is stated rather than assumed: the
    VAD is an optimisation and a small quality nudge, and mlx-whisper still has
    its own per-window `no_speech_threshold` underneath — so without Silero a
    user gets a correct transcript that took longer, which is a much better
    outcome than a feature that stops working offline. It is NOT silent: the
    reason goes to the row the user is watching and to the worker log, because
    a `vad: true` that quietly did nothing is exactly the kind of difference
    between two engines this runner exists to eliminate.

    **"Could not be fetched" is the whole of what degrades**, and the shape of
    this function is what keeps it that way. Only OBTAINING the detector — the
    download and the session that loads the file — sits inside the `try`; the
    detection itself runs after it. A `TypeError` out of `vad.py`, a tensor
    reshaped wrong, a state threaded to the wrong argument: those are bugs in
    this repository, and absorbed here each of them would reach the user as
    "Speech detection unavailable", a sentence that reads like a flaky network
    and sends nobody to the defect. They propagate and fail the transcription,
    which is how they get found. `base.Cancelled` is also an `Exception`
    and is excluded by the same narrowing — which is no longer a precaution: the
    fetch below ticks through `report_or_cancel` now that it reports into a
    transcription row with a live ✕, so a ✕ pressed while the 2MB detector is
    downloading raises HERE, and a wider catch would answer it by transcribing
    the whole file for the user who asked to stop.

    `base` is the calling worker's `worker_base` and `cache` its `_loaded` dict:
    both are passed rather than imported, because each worker (and each test)
    holds its own, and a shared module that imported them would bind whichever
    was first.
    """
    import vad as vad_module

    sess = cache.get("vad")
    if sess is None:
        try:
            # Bound to THIS row, for the reason `_speaker_turns` gives. `download`
            # pre-fetches the detector so this is normally a cache hit, but a
            # machine whose whisper download predates AI-10f reaches the network
            # here — and used to reopen the model's finished load row to say so.
            def fetch(repo_id, filename, detail=None):
                return base.download_file(repo_id, filename, detail=detail,
                                                 job=job, row=row)

            sess = vad_module.session(vad_module.model_path(fetch))
        except FETCH_FAILED + onnx_failures() as error:
            print(f"speech detection unavailable, transcribing the whole file: "
                  f"{error.__class__.__name__}: {error}", file=sys.stderr,
                  flush=True)
            base.report(
                job=job, **row, state="running", done=0, total=total,
                detail="Speech detection unavailable — transcribing everything…")
            return None
        cache["vad"] = sess
    base.report(job=job, **row, state="running", done=0, total=total,
                       detail="Finding speech…")
    return vad_module.speech_regions(audio, sess)


def packs_to_decode(regions, duration):
    """The clips to transcribe, each as a LIST of `(start, end)` regions in
    original-recording time whose speech travels in one `transcribe()` call.

    `duration` is the NUMBER of seconds decoded, never the row's `total` — the
    two differ for a recording so short it rounds to zero, where `total` is None
    (indeterminate, which is what the row should show) and this arithmetic needs
    0.0. The wording came from the withdrawn `parakeet_mlx` runner (D406)
    because the split is: handing the None
    on made the only pack `[(0.0, None)]`, and the speech sum over the packs then
    raised a TypeError out of a file the loop below would have skipped in one
    line.

    A list per clip rather than a single span, because the clip handed to the
    decoder is those regions CONCATENATED — the silence between them is dropped,
    which is the whole reason `vad: true` exists (Whisper hallucinates in
    silence) — and the list is what `vad.original_start`/`original_end` invert to
    put every timestamp back on the recording's clock. The packing rule and its
    inverse live in `vad.py`, beside the detector, because how many regions share
    a call is part of what `vad: true` MEANS and both MLX engines read that from
    one place (AI-10f).

    One pack of one region spanning everything is what "no VAD" looks like, and
    that is the point of the shape: the filtered and unfiltered paths are ONE
    loop, so the timestamp remap, the progress mapping and the cancel behaviour
    cannot drift between them. `vad: false` is not a different code path, it is
    a pack list of length one.

    A recording the detector finds NO speech in also decodes whole. Reporting
    an empty transcript for a file nobody looked at would be the confident
    version of a wrong answer — the detector is tuned for speech, not for
    whispering, singing or a bad microphone, and Whisper's own no-speech
    handling is the better final word.
    """
    if not regions:
        return [[(0.0, duration)]]
    import vad as vad_module

    return vad_module.pack_regions(list(regions))
