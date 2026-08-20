"""Speech to text on Parakeet-TDT: the third engine for one capability (D319).

NVIDIA's Parakeet, a TRANSDUCER rather than an encoder-decoder, through
`parakeet-mlx` on Apple Silicon. Registered BELOW `mlx_whisper/`, so a Mac
still transcribes on Whisper unless the user picks this on the Engines tab
(D302). Read `mlx_whisper/worker.py` alongside this: the audio decode, the
ticking, the orphan machinery, the VAD wiring and the diarization pre-pass are
that file's, reasoning included, and are not re-argued here. What follows is
only what is genuinely different.

* **The library will not take a waveform, so its loader is BORROWED.**
  `mlx_whisper.transcribe()` accepts an ndarray on the same argument as a path;
  `parakeet_mlx`'s `transcribe()` takes a path and nothing else, and the first
  thing it does with one is `load_audio`, which SPAWNS ffmpeg — a binary this
  app does not ship (see this folder's `pyproject.toml`). Reimplementing
  `transcribe()` around `get_logmel` + `generate` was the alternative and it
  costs the library's chunking and its overlap token merge, which is most of
  what makes a long recording come out right. So `_borrowed_audio` swaps the
  module's `load_audio` binding for the duration of one call and hands over the
  waveform `av` already produced. It is the same reach-into-another-package's-
  globals `mlx_whisper/worker.py`'s `_watch_progress` makes, with the same
  guard: if the binding is not there, this runner says so rather than
  transcribing something unexpected.
* **Progress comes from the library's chunk callback.** `transcribe()` calls
  `chunk_callback(end_samples, total_samples)` BEFORE it decodes each chunk, so
  the position reported is that chunk's START — everything already decoded, and
  never a second of audio that has not been. Seconds of audio (SPEC AI-10a),
  like both whisper runners.
* **Three options are REFUSED rather than ignored.** `task: "translate"` (this
  model transcribes only), `language` (it has no such argument — v3 detects
  among its 25 languages and cannot be pinned) and `initialPrompt` (a
  transducer has no text conditioning). Each raises naming this engine and what
  to do instead. Accepting them silently is the failure mode this app treats as
  worst: a caller asking for English output and getting French, with nothing
  saying which engine decided.
* **Parakeet does not hallucinate on silence**, so `vad` here is a wall-clock
  saving rather than a correctness fix — but it is the SAME Silero and the same
  regions as the other two engines (`runners/vad.py`), because "vad means one
  thing" (AI-10f) is a promise about the argument, not about why an engine
  wants it.

Everything a page sees is the other two runners' shape: the same result dict,
the same two files, the same partial transcript, the same speaker labels
through the same `runners/diarize.py`.
"""

import http.client
import json
import math
import os
import sys
import threading
import time

# The base and every module more than one engine shares sit one directory up,
# in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diarize  # noqa: E402 - the SHARED speaker labelling; see runners/diarize.py
import engine_options  # noqa: E402 - what this engine cannot do, refused in ONE place
import formats  # noqa: E402 - the shared format checks; see formats.py
import partial  # noqa: E402 - the SHARED progressive transcript; see runners/partial.py
import worker_base  # noqa: E402 - the path insert above is what makes it importable

# `vad` is imported where it is USED, like the MLX whisper runner does it: a
# test can stand it in, and a machine with no detector never pays the import.

#: The resident model, its sample rate, and the VAD session once one exists.
#: Unlike `mlx_whisper`, the library here has no module-level holder of its own
#: — `from_pretrained` returns an object and owns nothing — so this IS where the
#: model lives.
_loaded = {}

#: The one MLX stream every thread in this process works on. See `_pin_stream`.
_STREAM = {"stream": None}
_STREAM_LOCK = threading.Lock()


def _pin_stream():
    """Put this thread's MLX work on the process's ONE shared stream.

    `mlx_whisper/worker.py` documents the whole failure and it applies here
    unchanged: from mlx 0.32 a default stream belongs to the THREAD that made
    it, an UNEVALUATED array is a graph pinned to its stream, and forcing one
    from another thread throws an uncaught C++ exception that aborts the
    process rather than raising in Python. This worker loads on one thread and
    decodes on another (`_call_with_ticks`), which is exactly that shape.

    **And this runner really does leave lazy arrays behind.**
    `parakeet_mlx.from_pretrained` ends by casting every weight
    (`v.astype(dtype)`) into `model.update(...)` and never calls `mx.eval`, so
    without the pin the entire model reaches the decode thread as live graphs
    owned by the loader. `load()` evaluates them as well — belt and braces, and
    the cheaper of the two to lose — but the pin is what holds for whatever the
    next version of this library leaves lazy.

    A no-op on an mlx too old to have the call, which is the right answer:
    streams were process-wide there and there was nothing to pin.
    """
    import mlx.core as mx

    make = getattr(mx, "new_thread_unsafe_stream", None)
    pin = getattr(mx, "set_default_stream", None)
    if make is None or pin is None:
        return None
    with _STREAM_LOCK:
        stream = _STREAM["stream"]
        if stream is None:
            stream = _STREAM["stream"] = make(mx.default_device())
    pin(stream)
    return stream


# --------------------------------------------------------------- model loading


def download(model_id):
    """The Parakeet snapshot, and the 2MB speech detector beside it.

    Both halves for `mlx_whisper/worker.py`'s reasons: a "Download" that leaves
    a cache which cannot work offline has not done what the button said, and
    `vad` defaults to true. Best-effort, because a transcription works without
    the detector — but NOT past a ✕, which is a real exception here and would
    otherwise report a cancelled download as finished.
    """
    snapshot = worker_base.download_snapshot(model_id)
    try:
        import vad as vad_module

        vad_module.model_path(worker_base.download_file)
    except worker_base.Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - see the docstring
        print(f"could not pre-fetch the speech detector: "
              f"{error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
    return snapshot


def load(model_id, fetched):
    """`fetched` is what `download` returned — the snapshot directory.

    The format check comes FIRST, before the import, as it does in both whisper
    runners: a repo in the wrong format is a fact about the download rather
    than about this environment, and importing first would replace the sentence
    below with whichever ImportError happened to come first.

    **There are four incompatible speech formats in this app now** — CT2's
    `model.bin`, MLX Whisper's `weights.npz`, transformers' safetensors, and
    this — and the AI Models page offers Load on anything whose task label says
    "speech recognition", because the format is not in the label. A Whisper
    repo arriving here carries `model.safetensors` too, so the FILE cannot be
    the check: `formats.is_parakeet_checkpoint` reads the NeMo class out of
    `config.json`, which is the same evidence the page's engine tag uses.
    """
    config_path = os.path.join(fetched, "config.json")
    config = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, ValueError):
            # A config that will not parse is a broken download, and the
            # message below already says what a good one looks like. Not
            # re-raised as a JSON error, which would send the reader looking at
            # their disk rather than at their repo.
            config = {}
    if not (formats.is_parakeet_checkpoint(config)
            and os.path.isfile(os.path.join(fetched, formats.PARAKEET_WEIGHTS))):
        raise RuntimeError(
            f"{model_id} is not a Parakeet export — this runner loads NeMo ASR "
            f"models converted for MLX, which carry a config.json naming a "
            f"{formats.NEMO_ASR_TARGET}… class beside "
            f"{formats.PARAKEET_WEIGHTS} (a Whisper repo carries the same file "
            "and a different config). Try mlx-community/parakeet-tdt-0.6b-v3, "
            "or switch the engine on the AI Models page to transcribe with "
            "Whisper.")

    import parakeet_mlx

    # BEFORE the weights exist, because an array remembers the stream it was
    # made on and this thread is not the one that will decode.
    _pin_stream()
    model = parakeet_mlx.from_pretrained(fetched)

    # `from_pretrained` casts every parameter and evaluates none of them, so
    # without this the first decode touches a graph built on THIS thread. The
    # pin above already covers it; this makes the resident cost real now rather
    # than inside the first request, which is what "ready" is supposed to mean.
    import mlx.core as mx

    mx.eval(model.parameters())

    rate = int(model.preprocessor_config.sample_rate)
    if rate != SUPPORTED_RATE:
        # Refused HERE, and nothing is stored: everything downstream of the
        # decode is 16 kHz and none of it can say so for itself in a sentence
        # anybody can act on. Silero's exported graph takes 512-sample windows
        # at that rate and nothing else, and it is reached OUTSIDE
        # `_speech_regions`'s degradation guard, so an 8 kHz model would abort
        # a transcription from inside ONNX Runtime; with `vad: false` it would
        # instead produce diarization turns wrong by a ratio, which is a
        # transcript confidently attributed to the wrong people.
        raise RuntimeError(
            f"{model_id} wants {rate} Hz audio and this runner's speech "
            f"detection and speaker labelling are {SUPPORTED_RATE} Hz only. "
            "Every Parakeet model published for MLX today is 16 kHz — try "
            "mlx-community/parakeet-tdt-0.6b-v3.")

    _loaded["model"] = model
    _loaded["rate"] = rate
    # See `worker_base.STATE["device"]`. MLX is Metal or nothing.
    worker_base.set_state(device="mps")


def memory():
    """What MLX itself says it is holding, in bytes — never RSS alone.

    `mlx_whisper/worker.py`'s function and its reasoning: MLX memory-maps its
    weights and its arrays are lazy, so RSS right after a load reports the
    interpreter and not the model. `worker_base` takes the larger of this and
    its own reading, so a wrong answer either way is corrected by the other.
    """
    import mlx.core as mx

    for probe in (getattr(mx, "get_active_memory", None),
                  getattr(getattr(mx, "metal", None), "get_active_memory", None)):
        if probe is None:
            continue
        value = probe()
        if isinstance(value, int) and value > 0:
            return value
    return None


# ------------------------------------------------------------- audio, no ffmpeg


#: The only rate this runner can serve, checked at LOAD.
#:
#: Not Parakeet's constraint — its mel front end reads whatever its config
#: says — but everything AROUND it: `runners/vad.py` runs Silero's export,
#: which takes 512-sample windows at 16 kHz and nothing else, and
#: `runners/diarize.py` refuses turns denominated in any other rate because
#: its two sherpa models are 16 kHz exports. Every Parakeet model published
#: for MLX today says 16 kHz, so this is a guard rather than a limitation.
SUPPORTED_RATE = 16000


def _sample_rate():
    """The rate the LOADED model wants, read rather than assumed.

    `_loaded["rate"]`, not a `.get(...) or 16000`: the only state in which a
    fallback could fire is one where nothing is known about the model, and
    guessing there would resample the recording to a rate nobody checked —
    the chipmunk transcript this number exists to prevent, produced silently.
    A `KeyError` is the honest answer, and `load` is what makes it impossible.
    """
    return int(_loaded["rate"])


def _decode_audio(path, rate):
    """The file as mono float32 at `rate`, decoded IN THIS PROCESS.

    `mlx_whisper/worker.py`'s function, with the rate as an argument. Its three
    details all produce silent nonsense rather than an error when wrong, and
    are documented there: `fltp` planar float (an int16 buffer read as float is
    white noise), `layout="mono"` (a stereo interview otherwise arrives
    interleaved and reads as double-speed speech), and the resampler FLUSH,
    without which the tail of every recording is left inside the filter.
    """
    import av
    import numpy as np

    chunks = []
    with av.open(path) as container:
        streams = container.streams.audio
        if not streams:
            raise RuntimeError(f"{os.path.basename(path)} has no audio track to transcribe")
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=rate)
        for frame in container.decode(streams[0]):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise RuntimeError(f"{os.path.basename(path)} decoded to no audio")
    # `dtype=` on the concatenate rather than `.astype` after it. The frames
    # are already float32 (the resampler was asked for `fltp`), so the second
    # form allocates the whole waveform TWICE and frees one copy — 345MB of it
    # on a 90-minute recording, at the moment the model is also resident.
    return np.concatenate(chunks, dtype=np.float32)


# ------------------------------------------------------------------- reporting
#
# `_eta` and `_clock` are the whisper runners', copied for the reason they
# document: each runner runs on an interpreter built from its own folder, and
# the only module all of them can import is `worker_base`, which is the
# supervisor's contract and not a place for one capability's formatting. The
# three must agree — this clock is read one line under the job manager's own.


def _eta(remaining_audio, elapsed, done_audio):
    """Wall-clock left, from how much AUDIO is left and how fast it is going."""
    if not done_audio or remaining_audio is None or remaining_audio <= 0:
        return ""
    remaining = remaining_audio * (elapsed / done_audio)
    if remaining < 60:
        return " · ~%ds left" % round(remaining)
    return " · ~%.1f min left" % (remaining / 60)


def _clock(seconds):
    """m:ss, or h:mm:ss once there are hours.

    `floor(x + 0.5)` rather than `round`, because Python's `round` is BANKER'S
    rounding and JavaScript's `Math.round` is not, and this string sits one line
    below the manager's own rendering of the same pair.
    """
    seconds = math.floor((seconds or 0) + 0.5)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


# ------------------------------------------------------------------- ticking
#
# The MLX whisper runner's `_await_orphan` / `_call_with_ticks` / `_finished`,
# with the reasoning it documents. Copied for `_clock`'s reason: separate
# interpreters, no shared module below `worker_base`.

#: How often a blocking phase ticks. Module-level so a test can shorten it.
_TICK_S = 1.0

#: The work thread a cancel walked away from, if one is still running. A cancel
#: unwinds the HANDLER, not the work, and two decodes on one model is what
#: `GENERATE_LOCK` exists to prevent — that lock is the base's and is not ours
#: to hold longer, so the abandoned thread is remembered and waited for here.
_orphan: "dict[str, threading.Thread | None]" = {"thread": None}

#: Asks the abandoned work to stop. Checked in the chunk callback, which is the
#: one place a ✕ can reach inside the library — a cancelled `transcribe()`
#: therefore raises at the next chunk instead of running a 90-minute recording
#: to its end. A REQUEST, not a guarantee: nothing checks it during the `av`
#: decode, and a chunk is up to `_CHUNK_S` of audio long.
_STOP = threading.Event()

#: How long a new transcription waits for abandoned work before refusing. A
#: wedge inside PyAV or the decoder must not block every later transcription
#: for the life of the process, and a hang with a spinner is the worst failure
#: available.
_ORPHAN_WAIT_S = 30.0


def _await_orphan(job, row):
    """Let work abandoned by an earlier cancel finish before starting more."""
    thread = _orphan.get("thread")
    if thread is None or not thread.is_alive():
        _orphan["thread"] = None
        return
    deadline = time.time() + _ORPHAN_WAIT_S
    while thread.is_alive():
        thread.join(timeout=_TICK_S)
        if not thread.is_alive():
            break
        if time.time() >= deadline:
            raise RuntimeError(
                "a previous transcription that was cancelled is still stopping "
                f"(over {int(_ORPHAN_WAIT_S)}s). Try again in a moment; if it "
                "never clears, unload the model from the AI Models page.")
        worker_base.report_or_cancel(
            job=job, **row, state="running", done=None, total=None,
            detail="Finishing a cancelled decode…")
        if worker_base.CANCEL.is_set():
            raise worker_base.Cancelled()
    _orphan["thread"] = None


def _call_with_ticks(call, job, row, progress, cancelled=None):
    """Run `call()` on a thread, ticking once a second until it returns.

    `progress()` is asked on every tick what to say, returning
    `(done, total, detail)`. `report_or_cancel` is what carries a ✕ back — a
    plain `report` cannot.

    **A ✕ that lands while the work is FINISHING does not discard it.**
    `cancelled` is the caller's flag, set to `{"late": True}` when the ✕
    arrived but `call()` had already returned; the value is handed back
    normally and the caller decides what a cancel means at that point. Only a
    VALUE is salvaged — a call that finished by raising has nothing worth
    keeping, and reporting the failure of work the user abandoned sends them
    looking for a fault that does not matter.
    """
    result = {}

    def run():
        try:
            result["value"] = call()
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            result["error"] = e

    thread = threading.Thread(target=run, name="decode", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(timeout=_TICK_S)
        if not thread.is_alive():
            break
        done, total, detail = progress()
        try:
            worker_base.report_or_cancel(job=job, **row, state="running",
                                         done=done, total=total, detail=detail)
            if not worker_base.CANCEL.is_set():
                continue
        except BaseException:
            # The report itself carried the ✕ back (or failed). Liveness is
            # re-read HERE rather than trusted from before the call, because
            # the call is where the time went.
            if _finished(thread, result):
                if cancelled is not None:
                    cancelled["late"] = True
                break
            _STOP.set()
            _orphan["thread"] = thread
            raise
        if _finished(thread, result):
            if cancelled is not None:
                cancelled["late"] = True
            break
        _STOP.set()
        _orphan["thread"] = thread
        raise worker_base.Cancelled()
    if "error" in result:
        raise result["error"]
    return result["value"]


def _finished(thread, result):
    """Has the work already produced a value? — the last-second cancel guard.

    Both halves are needed and neither implies the other: a thread can be gone
    from `is_alive()` a moment before `run()`'s assignment is visible here, and
    `result` can hold an `error` from a call with nothing to salvage.
    """
    return not thread.is_alive() and "value" in result


# ------------------------------------------------------------------------- VAD


#: What "the detector could not be obtained" is allowed to arrive as — the MLX
#: whisper runner's tuple and its reasoning. `OSError` covers almost the whole
#: fetch (sockets, DNS, TLS, timeouts, a full disk, and every huggingface_hub
#: failure worth degrading on); `http.client.HTTPException` is the one shape
#: that is not an `OSError`; `ImportError` is here because onnxruntime is
#: imported inside `vad.session` and a venv without it is the same outcome for
#: the user. Deliberately NOT `ValueError`: hf raises `HFValidationError` for a
#: repo id that is not one, and that id is a constant in `vad.py`.
_FETCH_FAILED = (OSError, http.client.HTTPException, ImportError)


def _onnx_failures():
    """Every exception ONNX Runtime's C++ layer can raise loading a model.

    Named as "the module that contains exactly these" rather than as a list,
    because onnxruntime registers each of its error types from pybind with
    `Exception` as the base — there is no `OrtError` and no shared ancestor
    short of `Exception` itself, so an enumeration would quietly stop covering
    a runtime that adds one.
    """
    try:
        from onnxruntime.capi import onnxruntime_pybind11_state as ort_errors
    except ImportError:
        return ()
    return tuple(value for value in vars(ort_errors).values()
                 if isinstance(value, type) and issubclass(value, Exception))


def _speech_regions(audio, rate, total, job, row):
    """Where the speech is, or `None` when the whole file should be decoded.

    The MLX whisper runner's function, and the same trade: a detector that
    cannot be FETCHED degrades to no filtering, loudly (stderr and the row);
    anything else propagates, because a `TypeError` out of `vad.py` is a bug in
    this repository and absorbing it would reach the user as "Speech detection
    unavailable" — a sentence that reads like a flaky network and sends nobody
    to the defect. Only obtaining the detector sits inside the `try`.

    **On this engine the VAD is a saving, not a correction.** Parakeet does not
    hallucinate text over silence the way Whisper can, so skipping the quiet
    parts buys wall-clock rather than accuracy. It is the same detector and the
    same regions all the same: `vad: true` is one promise across the engines
    (AI-10f), and an engine that meant something else by it would be the one
    thing a third runner is not allowed to be.
    """
    import vad as vad_module

    sess = _loaded.get("vad")
    if sess is None:
        try:
            # Bound to THIS row: the fetch happens inside a transcription, so an
            # unbound `download_file` would tick into `JOB_ID` — the model's own
            # finished load row — while the row the user is watching said nothing.
            def fetch(repo_id, filename, detail=None):
                return worker_base.download_file(repo_id, filename, detail=detail,
                                                 job=job, row=row)

            sess = vad_module.session(vad_module.model_path(fetch))
        except _FETCH_FAILED + _onnx_failures() as error:
            print(f"speech detection unavailable, transcribing the whole file: "
                  f"{error.__class__.__name__}: {error}", file=sys.stderr,
                  flush=True)
            worker_base.report(
                job=job, **row, state="running", done=0, total=total,
                detail="Speech detection unavailable — transcribing everything…")
            return None
        _loaded["vad"] = sess
    worker_base.report(job=job, **row, state="running", done=0, total=total,
                       detail="Finding speech…")
    return vad_module.speech_regions(audio, sess, sample_rate=rate)


# ----------------------------------------------------------------- diarization


def _speaker_turns(audio, rate, speakers, job, row):
    """Who spoke when, over the WHOLE waveform — `[(start, end, index), …]`.

    The MLX whisper runner's function through the same shared module, and the
    two are meant to read side by side: the models, the labels and the join all
    live in `runners/diarize.py`, so what differs here is only this engine's
    ticking. `speakers` may be None, which means estimate the count (D318).

    **A failure here FAILS the transcription**, unlike the VAD's degradation:
    `diarize: true` is a request for speaker labels, and quietly returning an
    unlabelled transcript answers a different question while looking like
    success.

    **`done`/`total` stay None for the whole phase.** They are seconds of audio
    of the TRANSCRIPT (SPEC AI-10a), and a pre-pass that filled them would run
    the bar to 100% before a word was decoded. `detail` is the field the row
    has for a stage.
    """
    def fetch(repo_id, filename, detail=None):
        return worker_base.download_file(repo_id, filename, detail=detail,
                                         job=job, row=row)

    segmentation, embedding = diarize.model_paths(fetch)
    worker_base.report(job=job, **row, state="running", done=None, total=None,
                       detail="Finding speakers…")
    session = diarize.diarizer(segmentation, embedding, speakers)

    def run():
        return diarize.speaker_turns(audio, session, rate,
                                     should_stop=_STOP.is_set)

    try:
        return _call_with_ticks(run, job, row,
                                lambda: (None, None, "Finding speakers…"))
    except diarize.DiarizationCancelled:
        # `diarize.py` cannot name `worker_base.Cancelled` (the server imports
        # it too), so it raises its own and the caller translates.
        raise worker_base.Cancelled()


# --------------------------------------------------------------- transcription


#: How much audio one `generate` pass sees, and how much of it the next pass
#: repeats. `parakeet-mlx` chunks a long recording itself and merges the
#: overlapping tokens (`merge_longest_contiguous`, falling back to a longest-
#: common-subsequence alignment), which is the machinery `_borrowed_audio`
#: exists to keep rather than reimplement.
#:
#: Half the library's own default of 120s, for the progress: the chunk callback
#: is the only position this engine reports, so 120s chunks mean a bar that
#: moves twice a minute at best. 60s is still an order of magnitude longer than
#: a sentence, and the 15s overlap is the library's own — the merge needs
#: enough repeated audio to align two decodes on, and shortening it is how
#: words go missing at a seam.
_CHUNK_S = 60.0
_OVERLAP_S = 15.0


class _borrowed_audio:
    """Hand `parakeet_mlx.transcribe()` a waveform where it wants a path.

    The library's `transcribe(path)` calls `load_audio`, which shells out to
    ffmpeg — not shipped here (this folder's `pyproject.toml` says why) — so
    the module's binding is swapped for the duration of one call and the
    samples `av` already produced are returned instead. The path still travels
    through the call and is simply not read.

    **This reaches into another package's module globals, and that is a real
    cost**, the same one `mlx_whisper/worker.py`'s `_watch_progress` pays: it
    is not part of parakeet-mlx's API and a future version may load its audio
    somewhere else. Hence the check — a missing binding RAISES rather than
    letting the call fall through to ffmpeg, because the failure that would
    produce is "FFmpeg is not installed" from inside a library, on a machine
    where that is not the user's problem to fix.
    """

    def __init__(self, module, samples):
        self._module = module
        self._samples = samples
        self._saved = None

    def __enter__(self):
        if getattr(self._module, "load_audio", None) is None:
            raise RuntimeError(
                "this build of parakeet-mlx does not load its audio through "
                "`load_audio`, which is where this runner hands over the "
                "waveform it decoded — it would shell out to ffmpeg instead. "
                "Pin parakeet-mlx to a version that has it, or transcribe with "
                "Whisper from the AI Models page.")
        import mlx.core as mx

        self._saved = self._module.load_audio
        samples = mx.array(self._samples)
        self._module.load_audio = lambda *args, **kwargs: samples
        return self

    def __exit__(self, *exc):
        # Restored unconditionally, including on the cancel path: the module is
        # process-global, and a waveform left behind belongs to a request that
        # is over.
        #
        # **It is restored on the DECODE thread, which a cancel has already
        # walked away from**, so for a moment after `generate` raises the swap
        # is still in place. That is safe for exactly one reason and it is
        # worth naming: nothing else can decode in that window. The supervisor
        # serializes transcriptions, `_await_orphan` waits for this thread
        # before the next one starts, and past its deadline it REFUSES rather
        # than proceeding — so no request ever meets another's waveform.
        self._module.load_audio = self._saved
        return False


def _parakeet_module():
    """`parakeet_mlx.parakeet` — the module `transcribe()` reads `load_audio`
    from, which is where the binding has to be swapped. Not `parakeet_mlx`
    itself and not `parakeet_mlx.audio`: the name is bound into the defining
    module's globals by `from … import load_audio`, and patching either of the
    other two would leave the real one in place and change nothing."""
    import importlib

    return importlib.import_module("parakeet_mlx.parakeet")


def _decode_clip(model, module, clip, rate, position):
    """One `transcribe()` call, on the thread `_call_with_ticks` gave it.

    A named function rather than a lambda because the pin has to happen ON THIS
    THREAD — every decode runs on a thread of its own and the weights were
    primed on another. See `_pin_stream`.

    `chunk_callback` is both the progress hook and the cancel hook. It fires
    BEFORE each chunk is decoded, so what it reports is that chunk's START:
    everything before it is finished, which is a position the bar can never be
    ahead of. A recording shorter than `_CHUNK_S` never chunks and therefore
    never calls back, and the ticks then carry no numbers rather than an
    invented percentage — the same honest answer the MLX whisper runner gives
    when it has no counter to borrow.
    """
    _pin_stream()

    def chunk_callback(done_samples, total_samples):
        if _STOP.is_set():
            # The one place a ✕ reaches INSIDE the library. Raising here
            # unwinds `transcribe()` at a chunk boundary instead of letting an
            # abandoned decode run the recording out while the next request
            # waits on `_await_orphan`.
            raise worker_base.Cancelled()
        position["available"] = True
        position["seconds"] = max(0.0, float(done_samples) / rate - _CHUNK_S)

    with _borrowed_audio(module, clip):
        return model.transcribe("<in-memory>", chunk_duration=_CHUNK_S,
                                overlap_duration=_OVERLAP_S,
                                chunk_callback=chunk_callback)


def _regions_to_decode(audio, regions, duration):
    """The clips to transcribe, as `(start, end)` in original-recording time.

    `duration` is the NUMBER of seconds decoded, never the row's `total` — the
    two differ for a recording so short it rounds to zero, where `total` is
    None (indeterminate, which is what the row should show) and this arithmetic
    needs 0.0. Handing the None on made the only clip `(0.0, None)`, and the
    ETA's `sum` over the clips then raised a TypeError out of a file the loop
    below would have skipped in one line.

    One region spanning everything is what "no VAD" looks like, and that is the
    point of the shape: the filtered and unfiltered paths are ONE loop, so the
    timestamp remap, the progress mapping and the cancel behaviour cannot drift
    between them. `vad: false` is not a different code path, it is a region
    list of length one.

    A recording the detector finds NO speech in also decodes whole: reporting
    an empty transcript for a file nobody looked at would be the confident
    version of a wrong answer.

    **One region per clip, and this engine deliberately does NOT pack them the
    way `mlx_whisper` does** (`vad.pack_regions`, which sits in the shared module
    and is available here for the asking). Packing pays for itself over there
    because Whisper is a fixed-window encoder: mlx-whisper pads every call's mel
    to 30 seconds, so a 0.8-second region costs a full window and thirty-one
    regions cost more than the whole file. Parakeet is a transducer with no such
    window — `parakeet_mlx.parakeet` only chunks at all above
    `chunk_duration = 60.0` — so its cost is proportional to the audio it is
    given, and one call per region costs the same as one call for the lot. What
    packing WOULD add here is a second timestamp mapping to keep correct for no
    measured gain, which is why the two engines differ on this and agree on
    everything the flag actually promises: both drop the silence, both cut only
    at boundaries the detector found, both report original-recording time
    (AI-10f). "Same meaning" is not "same batching".
    """
    if not regions:
        return [(0.0, duration)]
    return list(regions)


def _slice(audio, start, end, rate):
    """One clip's samples, through the shared detector's own slicing so the
    arithmetic that produced the region and the arithmetic that cuts it cannot
    disagree by a sample."""
    import vad as vad_module

    return vad_module.slice_samples(audio, (start, end), rate)


def _seconds_done(position, offset=0.0, limit=None):
    """Where the decoder has got to, in seconds of the ORIGINAL recording.

    `offset` is where the clip starts in that recording and `limit` where it
    ends — both exist because of the VAD: once silence is dropped the callback
    denominates seconds of SPEECH, and reporting that against a `total` of the
    whole recording would put two units in one bar.

    None rather than 0 when there is nothing to read: a `done` of 0 claims
    nothing has been transcribed, and the job manager renders an absent number
    as indeterminate, which is the truth.
    """
    if not position.get("available"):
        return None
    done = offset + float(position.get("seconds", 0.0))
    if limit is not None:
        done = min(done, limit)
    return round(done, 2)


def _transcribe_regions(audio, clips, rate, job, row, total, duration,
                        transcribing_since, progressive=None):
    """Transcribe each clip and return the segments in ORIGINAL time.

    **`total` and `duration` are the same seconds in two currencies**, and the
    split is not tidiness: `total` is what the ROW carries and is None for a
    recording too short to round to a tenth of a second, because an
    indeterminate bar is the honest rendering of "no length worth showing";
    `duration` is the arithmetic, and is 0.0 there. Only the reporting may see
    the None.

    `progressive` is the partial-transcript sink (`runners/partial.py`), fed
    from the one place where a segment is finished AND already remapped into
    original-recording time — the only point at which a line is safe to
    publish, since a page seeking a player off a clip-relative timestamp would
    land in the wrong minute.

    A SENTENCE is this engine's segment. Parakeet emits tokens with
    timestamps and the library groups them into `AlignedSentence`s on
    punctuation and silence, which is the closest thing it has to Whisper's
    segment and is what makes one transcript shape serve all three engines. The
    tokens themselves are dropped, exactly as the whisper runners drop their
    logprobs and temperatures: a page must not be able to tell which engine ran.
    """
    module = _parakeet_module()
    model = _loaded["model"]
    segments = []
    if progressive is None:
        progressive = partial.sink(None)

    #: The ETA's own currency: seconds of audio there are to DECODE, which once
    #: silence is dropped is not the length of the recording. `done`/`total`
    #: stay denominated in the original recording (AI-10a) — the ETA cannot,
    #: because it divides, and mixing the two currencies in one fraction is how
    #: a job about to finish announces six minutes left.
    speech_total = sum(clip_end - clip_start for clip_start, clip_end in clips)
    decoded = 0.0

    for index, (start, end) in enumerate(clips):
        last = index == len(clips) - 1
        clip = (audio if (start, end) == (0.0, duration)
                else _slice(audio, start, end, rate))
        if len(clip) < rate // 10:
            # Under a tenth of a second: all padding and no signal, and a clip
            # of a few hundred samples is where a decoder invents a sentence.
            continue
        position = {}

        def progress(offset=start, limit=end, before=decoded):
            done = _seconds_done(position, offset=offset, limit=limit)
            if done is None:
                return None, None, "Transcribing…"
            elapsed = time.time() - transcribing_since
            # `before` and the bounds are default arguments for the reason the
            # MLX runner's are: the closure outlives the loop iteration that
            # made it, and a late tick reading the loop variable would price
            # this clip against a later clip's progress.
            speech_done = before + (done - offset)
            return done, total, "Transcribing — %s of %s%s" % (
                _clock(done), _clock(total) if total else "?",
                _eta(speech_total - speech_done if speech_total else None,
                     elapsed, speech_done))

        late_cancel = {}
        result = _call_with_ticks(
            lambda: _decode_clip(model, module, clip, rate, position),
            job, row, progress, cancelled=late_cancel)

        # This clip is decoded, whatever the callback last said: it reports the
        # START of a chunk, so trusting its final value would leave the rate a
        # chunk short on every region.
        decoded += end - start
        for sentence in (getattr(result, "sentences", None) or []):
            # `+ start` is the remap: timestamps come back relative to the CLIP
            # and every consumer reads them as positions in the FILE. Getting
            # it wrong is silent — a transcript that looks perfect and whose
            # every timestamp after the first gap is early. BOTH ends are
            # clamped to the region, because a token at a chunk seam can land
            # fractionally past it.
            at = min(float(getattr(sentence, "start", 0.0) or 0.0) + start, end)
            until = min(float(getattr(sentence, "end", 0.0) or 0.0) + start, end)
            if at >= until:
                # A sentence that begins past the clip. Clamping both ends
                # would flatten it onto the boundary, which is text asserted to
                # have been spoken during silence that was cut out; nothing was
                # said there, so the honest transcript omits it.
                continue
            text = str(getattr(sentence, "text", "") or "").strip()
            if not text:
                continue
            segments.append({
                "start": round(at, 2),
                "end": round(until, 2),
                "text": text,
            })
            # Published the instant it is final, so a page's transcript fills
            # in the same steps its bar moves in.
            progressive.add(segments[-1])
        if late_cancel.get("late"):
            # A cancel is worth honouring exactly while there is work left to
            # stop. On the LAST clip there is none and discarding the
            # transcript would be an hour of decoding thrown away at 99%; on an
            # earlier one there are minutes still to come, and writing what
            # exists would present a fifth of a transcript as a whole one.
            if not last:
                raise worker_base.Cancelled()
            break

    return segments


#: This runner's own code, as `runners/engine_options.py` and the registry
#: spell it. A literal because a worker cannot import the registry (its venv
#: has no `fused_render`), and `tests/test_ai_engine_options.py` pins that
#: every code that table names is a registered runner.
RUNNER_CODE = "parakeet-mlx"


def generate(body):
    """Transcribe one file. Returns `{path, output, segments, language, …}`.

    The whisper runners' result dict and the same two files on disk — a page
    must not be able to tell which runner served it (SPEC AI-10c), which is
    why the differences this engine really has are REFUSALS rather than quiet
    substitutions.
    """
    model = _loaded.get("model")
    if model is None:
        raise RuntimeError("no model is loaded")
    rate = _sample_rate()

    source = str(body.get("path") or "")
    out = str(body.get("out") or "")
    out_text = str(body.get("outText") or "")
    # Where each segment lands AS it is decoded. Passed rather than derived:
    # the server owns where user files go. Absent, it is a no-op sink.
    out_partial = str(body.get("outPartial") or "") or None
    if not source:
        raise ValueError("'path' must be the audio file to transcribe")
    if not out or not out_text:
        raise ValueError("'out' and 'outText' must be where to write the transcript")

    task = str(body.get("task") or engine_options.TRANSCRIBE)
    # The BACKSTOP, not the first refusal: the endpoint asks the same question
    # of the same module before a job row exists, so an ordinary caller is told
    # instantly rather than after a multi-gigabyte download and a load. This
    # copy is here because neither the bridge nor the endpoint is the only door
    # into this process — the same reason `speakers_or_raise` is called twice.
    # `words` is absent from this call ON PURPOSE: it is the one option answered
    # best-effort rather than refused (see `engine_options.words_available`).
    # This engine emits no `words` key, a caller reads that from the segment, and
    # nothing here has to know the flag exists. Parakeet's own per-token times
    # could supply it — see `_sentences`, which already drops them — but a token
    # here is a SUBWORD, so words would have to be rebuilt by grouping on a
    # leading space, and the times sit on an 80ms grid rather than whisper's
    # 10ms. `engine_options.WORDS_RUNNERS` carries the full comparison.
    engine_options.unsupported_or_raise(
        RUNNER_CODE, task=task, language=str(body.get("language") or ""),
        initial_prompt=str(body.get("initialPrompt") or ""))

    # `is None`, not `get("vad", True)`: a JSON null is "not specified", and a
    # default reached only by an absent KEY inverts for the caller that spreads
    # an options object carrying an unset one.
    vad = True if body.get("vad") is None else bool(body.get("vad"))
    # Defaults FALSE, so every existing caller's output is byte-identical.
    diarizing = bool(body.get("diarize"))
    # Validated HERE by the shared rule — the bridge and the server both check
    # it first, but neither is the only door into this process. None means the
    # count was not given and the clustering estimates it (D318).
    speakers = diarize.speakers_or_raise(body.get("speakers")) if diarizing else None
    job = body.get("job") or None
    # The row's IDENTITY, carried on every tick — a tick missing `title` is
    # refused outright and one missing `cancellable`/`unit` rebuilds a row that
    # looks operable and is not.
    row = body.get("row") or {}

    started = time.time()
    worker_base.report(job=job, **row, state="running",
                       done=0, total=None, detail="Decoding audio…")
    _await_orphan(job, row)
    _STOP.clear()

    # PHASE ONE — the file becomes a waveform, in this process. Ticked because
    # it is not free and it is the one phase with no progress hook inside it.
    decode_cancel = {}
    audio = _call_with_ticks(lambda: _decode_audio(source, rate), job, row,
                             lambda: (None, None, "Decoding audio…"),
                             cancelled=decode_cancel)
    if decode_cancel.get("late"):
        # The ✕ landed as the decode finished, so nothing was lost by letting
        # it complete — but the transcription has not started, and that is all
        # of the work.
        raise worker_base.Cancelled()
    # The duration is OURS: we hold the samples, so it is exact rather than a
    # container's declared length, and it exists before the model sees a thing.
    #
    # TWO names for it, and the difference is a real file: a clip of a few dozen
    # samples rounds to 0.0, and `total` is what the ROW carries — where 0 would
    # be a bar claiming a length nobody can see move, so None (indeterminate) is
    # the honest value. `duration` is the arithmetic, and it stays a number:
    # handing the None to `_regions_to_decode` made the only clip `(0.0, None)`
    # and the ETA's `sum` over the clips raised a TypeError, turning a file the
    # decode loop skips in one line into a traceback on the job row.
    duration = round(len(audio) / rate, 2)
    total = duration or None

    # PHASE ONE-AND-A-HALF — who is speaking, over the whole waveform. Before
    # the VAD and independent of it, and a fast pre-pass, which is why it gets
    # its own `detail` line rather than a share of the transcript's bar.
    turns = _speaker_turns(audio, rate, speakers, job, row) if diarizing else None

    # The ETA's clock starts HERE, below the diarization and for the same
    # reason it is below the audio decode: `_transcribe_regions` divides
    # `elapsed` by seconds of speech decoded, so every second charged to it
    # before a word exists inflates the whole first minute of ETAs.
    transcribing_since = time.time()

    # PHASE TWO — find the speech, unless the caller said not to.
    regions = _speech_regions(audio, rate, total, job, row) if vad else None
    clips = _regions_to_decode(audio, regions, duration)

    # Everything from here to the final write happens inside the sink, because
    # its EXIT is the lifecycle: reaching the end means the real output landed
    # and the partial file is duplicate bytes; a `Cancelled` means the user
    # does not want this transcript at all; anything else LEAVES the file,
    # which is the only salvage from a run that died halfway.
    with partial.sink(out_partial, turns=turns,
                      cancelled=(worker_base.Cancelled,)) as progressive:
        # PHASE THREE — transcribe each clip, with every timestamp mapped back
        # into original-recording time.
        segments = _transcribe_regions(audio, clips, rate, job, row, total,
                                       duration, transcribing_since,
                                       progressive=progressive)

        # PHASE FOUR — the join, through the SAME function the whisper runners
        # call on the same two lists, which is what makes "identical labels"
        # structural rather than a thing to keep testing (AI-10c).
        speaker_list = (diarize.assign_speakers(segments, turns)
                        if turns is not None else None)

        text = " ".join(s["text"] for s in segments).strip()
        payload = {
            "path": source,
            "output": out,
            "outputText": out_text,
            "model": body.get("model") or "",
            "task": task,
            # None, always: Parakeet reports no language. The key is kept
            # because every consumer of a transcript reads this shape, and a
            # null is the honest value — inventing "en" would be a claim the
            # model never made about a multilingual recording.
            "language": None,
            "duration": total,
            "seconds": round(time.time() - started, 2),
            "segments": segments,
        }
        if speaker_list is not None:
            # ADDITIVE, and only when asked: a run without `diarize` writes
            # exactly the bytes it always did. The list is the transcript's
            # legend — the labels that actually landed on a segment.
            payload["speakers"] = speaker_list
            if speakers is None:
                # …and the count only when it was ESTIMATED, by the rule the
                # whisper runners follow (D318): a caller who supplied it
                # already knows.
                payload["estimatedSpeakers"] = diarize.speaker_count(turns)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({**payload, "text": text}, handle, ensure_ascii=False, indent=1)
        with open(out_text, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    # The segments stay out of the REPLY: a 90-minute recording is thousands of
    # them, and the caller was handed the path to the file that holds them
    # before this ever started.
    return {**payload, "segments": len(segments)}


if __name__ == "__main__":
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)
