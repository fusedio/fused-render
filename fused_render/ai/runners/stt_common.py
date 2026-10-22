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


def prefetch_detector(base):
    """Fetch the 2MB speech detector beside a model's download. Best-effort.

    Called from both runners' `download()` (MLX always; faster-whisper for a
    Parakeet repo, whose cuts depend on it — CTranslate2 Whisper ships its own
    Silero). Fetched HERE rather than lazily on the first transcription because
    a "Download" that leaves a cache which cannot work offline has not done what
    the button said: `vad` defaults to true, so a user who downloads on wifi and
    transcribes on a train would otherwise find the feature quietly degraded.

    Deliberately best-effort: the detector is an optimisation over a
    transcription that works without it (see `speech_regions`), so a Hub hiccup
    over 2MB must not fail a model download that already succeeded.

    **Best-effort stops at the ✕.** `base.Cancelled` is an ordinary `Exception`
    and the one that is not a Hub hiccup: absorbed, the row would finish
    "successfully" for a user who pressed stop. Re-raised first, and before the
    broad catch, because order is the whole of the distinction.
    """
    try:
        import vad as vad_module

        vad_module.model_path(base.download_file)
    except base.Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - see the docstring
        print(f"could not pre-fetch the speech detector: "
              f"{error.__class__.__name__}: {error}", file=sys.stderr, flush=True)


def speech_regions(base, cache, audio, total, job, row):
    """Where the speech is, or `None` when the whole file should be decoded.

    Returns a list of `(start, end)` in seconds. `None` — not an empty list —
    means "do not filter", and the two are deliberately different answers:
    an empty list is the detector saying there is no speech in this recording,
    while None is this function saying it could not ask.

    **A detector that cannot be fetched degrades to no filtering rather than
    failing the transcription.** The trade is stated rather than assumed: the
    VAD is an optimisation and a small quality nudge, so without Silero a user
    gets a correct transcript that took longer, which is a much better outcome
    than a feature that stops working offline. What stands underneath differs by
    engine, and the difference is a cost: mlx-whisper still has its own
    per-window `no_speech_threshold`, but **Parakeet has no no-speech gate at
    all** (a transducer decodes whatever it is handed), so for it the degrade
    means fixed-offset time slices (`faster_whisper/worker._time_slices`) and
    silence reaching the decoder uncut. It is NOT silent: the reason goes to the
    row the user is watching and to the worker log, because a `vad: true` that
    quietly did nothing is exactly the kind of difference between two engines
    this runner exists to eliminate.

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


def words_in_original_time(pack, spans, at, until):
    """One segment's words, remapped into RECORDING time and clamped into it.

    `spans` is `[(start, end, text), …]` in seconds of the packed CLIP, whatever
    engine timed them (mlx-whisper's word dicts, a transducer's token groups);
    returns `[{"start", "end", "word"}, …]`, the shape both engines publish.

    Each word travels the packing's one inverse (`vad.original_word_span`), NOT
    the segment's endpoint pair: a segment may straddle a join (real speech on
    both sides of the silence that was cut), a word may not, so mapping a word's
    two ends independently would stretch any token that strictly contains a join
    across the whole dropped pause (AI-10f).

    **Clamped into the segment, never dropped** — the opposite of what a
    segment that inverts gets, and deliberately. A dropped SEGMENT loses text
    that was never spoken in the recording's timeline. A dropped WORD loses text
    that IS in the transcript, breaking the one invariant a caller builds on:
    that the words of a segment are that segment's text, in order, complete. So a
    word that inverts after mapping (timed into Whisper's 30-second padding, say)
    collapses to an INSTANT at the nearest bound rather than vanishing; a
    zero-length word renders as un-highlightable, which is the honest rendering of
    "this word is here and its span is not known". Only a word with NO TEXT is
    skipped: nothing downstream can render it.

    `probability` is not carried: it is an engine-tell (AI-10c), and the
    leading space on `word` is kept because it is how the text reconstructs by
    concatenation.
    """
    import vad as vad_module

    out = []
    for w_start, w_end, text in spans:
        if not text:
            continue
        start, end = vad_module.original_word_span(pack, float(w_start or 0.0),
                                                   float(w_end or 0.0))
        # Into the SEGMENT's bounds, not the pack's: a word outside its own
        # segment would highlight text belonging to a neighbouring line.
        start = min(max(start, at), until)
        end = min(max(end, at), until)
        out.append({
            "start": round(start, 2),
            # `max`, so rounding cannot invert a word whose two ends round
            # across each other — 2dp is the transcript's precision everywhere.
            "end": round(max(end, start), 2),
            "word": text,
        })
    return out
