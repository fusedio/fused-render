"""Speech to text on MLX: one resident model, four routes (SPEC §40, AI-10c).

The Apple Silicon half of a capability `faster_whisper/` already serves
everywhere. Same contract, same result dict, same two output files, same job
row — a page cannot tell which of the two transcribed for it, and that is the
point: `fused.ai.transcribe()` is about audio, not about backends. Read
`faster_whisper/worker.py` alongside this; where the two agree, the reason is
written down there and not repeated here.

Four things are genuinely different, and all four follow from `mlx_whisper`
being a library rather than a streaming decoder:

* **This process decodes the audio itself, with `av`.** `mlx_whisper.transcribe`
  accepts a path, and on that path it calls openai-whisper's `load_audio()`,
  which SPAWNS `ffmpeg`. This app bundles rclone, not ffmpeg (see this folder's
  `pyproject.toml`), so that path is closed: `_decode_audio` turns the file into
  16 kHz mono float32 through PyAV's in-process ffmpeg libraries and the
  waveform is what `transcribe()` is given. The library takes an ndarray on the
  same argument, which is what makes this a one-line difference rather than a
  fork.
* **`vad: true` runs a real speech detector**, as it does on the CT2 runner —
  Silero over ONNX Runtime (`vad.py`), on the waveform `av` already produced.
  The silence is dropped, the remaining speech is CONCATENATED into clips of up
  to `vad.BUDGET_S` seconds (one `transcribe()` call each, because the library
  pads every call to a 30-second window — a call per region made `vad: true`
  slower than no VAD at all on the large models), and every timestamp is mapped
  back to original-recording time through `vad.original_start`/`original_end` (a
  time on a join is the start of the next region or the end of the previous one,
  never both). The cut is always at a boundary the detector found in silence,
  never at a fixed offset.
* **`transcribe()` is ONE blocking call, not a generator.** faster-whisper hands
  back a stream and progress falls out of consuming it. Here the whole
  transcript arrives at once, so the per-segment tick that carries progress AND
  the ✕ does not exist. Both are rebuilt from the outside: the call runs on a
  thread and this one ticks (`_call_with_ticks`, the same shape and the same
  reason as the whisper runner's), and the position it reports comes from the
  library's own frame counter (`_watch_progress`).
* **Progress stays SECONDS OF AUDIO** (SPEC AI-10a, and `runtime.js` promises it
  to pages). See `_watch_progress` for how, and for what it costs.
* **The model is not "loaded" by a call of ours.** `mlx_whisper` keeps its model
  in a module-level holder keyed by path, so `load()` primes that holder rather
  than storing a handle here — otherwise the first transcription would load the
  weights a second time, inside the request, having already reported ready.

**A cancel is honoured, and better than the CT2 runner manages.** The ✕ arrives
as the reply to a tick, exactly as it does there — but here the tick thread can
also poke the decode: `_STOP` is checked inside the progress hook, so the
abandoned `transcribe()` raises at the next window instead of running the file
to its end. The orphan machinery below is kept anyway, because "the next window"
is up to 30 seconds of audio away and the phase before it (`av` decoding) has no
hook at all.
"""

import http.client
import importlib
import json
import math
import os
import sys
import threading
import time

# The base sits one directory up, in `runners/` — see mlx_text/worker.py — and
# so, since D319, does every module two engines share.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diarize  # noqa: E402 - the SHARED speaker labelling; see runners/diarize.py
import formats  # noqa: E402 - the shared format checks; see formats.py
import partial  # noqa: E402 - the SHARED progressive transcript; see runners/partial.py
import worker_base  # noqa: E402 - the path insert above is what makes it importable

# `vad` is imported where it is USED rather than here, and stays that way: it is
# reached through the same insert (it moved to `runners/` in D319, when the
# Parakeet engine became its second caller), but every reader of it is inside a
# function so that a test can stand it in and so that a machine with no
# detector never pays for the import.

#: The loaded model's snapshot PATH, not the model. See the module docstring:
#: `mlx_whisper` owns the object, in `transcribe.ModelHolder`, and a second
#: reference here would only be a way for the two to disagree about which model
#: is resident.
_loaded = {}

#: The one MLX stream every thread in this process works on. See `_pin_stream`.
_STREAM = {"stream": None}
_STREAM_LOCK = threading.Lock()


def _pin_stream():
    """Put this thread's MLX work on the process's ONE shared stream.

    **MLX streams are per-THREAD from mlx 0.32 on, and this worker is threaded.**
    The weights are primed on the bring-up thread (`worker_base.serve` runs
    `load` on one), each request arrives on a `ThreadingTCPServer` thread, and
    each decode runs on a fresh thread of its own (`_call_with_ticks`). Under
    0.32 a thread that touches MLX gets its own default stream — and an
    UNEVALUATED array is a graph pinned to the stream it was built on, so
    forcing it from another thread does not raise a Python exception. It throws
    `std::runtime_error("There is no Stream(gpu, 1) in current thread")` out of
    `metal::get_command_encoder`, which is an UNCAUGHT C++ exception and aborts
    the process.

    **The exact leak, because "loaded on another thread" alone is not it.** An
    array that has been EVALUATED travels between threads perfectly well —
    measured, in every direction, including from a thread that has since exited.
    `mlx_whisper.load_model` ends on `mx.eval(model.parameters())`, so the
    weights are fine. What it misses is that `nn.Module.valid_parameter_filter`
    **skips keys beginning with an underscore**, and this model derives two
    tensors at construction and stores them under exactly such names:
    `TextEncoder._positional_embedding` (a `sinusoids(...)` graph) and
    `TextDecoder._mask` (a causal mask). `parameters()` does not report them,
    `mx.eval` therefore never forces them, and they reach the decode thread as
    live graphs owned by the loading thread. The first thing `transcribe()` does
    there is `detect_language`, whose encoder pass adds `_positional_embedding`
    — so every MLX Whisper transcription died on its first decode, on every
    model, leaving only a `libc++abi:` line in the worker log and "the
    transcription process did not answer: Remote end closed connection without
    response" on the job row.

    Confirmed by removing only that one difference: pre-evaluating those two
    tensors on the loading thread, with no stream pinning at all, transcribes
    cleanly; without it the same script aborts. Pinning is still the fix worth
    shipping — it holds for whatever the NEXT version of this library leaves
    lazy, where a list of two attribute names would not.

    The invariant this protects, and the one to test any sibling runner against:
    **nothing lazy may survive the loading thread.** `mflux_image/worker.py` used
    to document why it satisfied that already and needed no pin; it does not, and
    now pins too. Read its `load` before trusting a "same shape, but fine"
    argument here — it also records the part this function does NOT cover: the
    default stream is per (thread, DEVICE), so `make(mx.default_device())` below
    pins the GPU only and leaves this thread's CPU default alone.

    `new_thread_unsafe_stream` is mlx's own answer: a stream not owned by the
    thread that made it. "Unsafe" means it must not be driven by two threads AT
    ONCE, which this worker already guarantees — the supervisor serializes
    transcriptions (`_TRANSCRIBE_LOCK`) and an abandoned decode is waited for
    (`_await_orphan`) before the next one starts.

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
    """The Whisper snapshot, and the 2MB speech detector beside it.

    The detector is fetched HERE rather than lazily on the first transcription,
    for the reason `diffusers_image/worker.py` gives about its GGUF recipe: a
    "Download" that leaves a cache which cannot work offline has not done the
    thing the button said it would. `vad` defaults to true, so a user who
    downloads a model on wifi and transcribes on a train would otherwise find
    the feature quietly degraded at exactly the wrong moment.

    Best-effort, and deliberately so: the detector is an optimisation over a
    transcription that works without it (see `_speech_regions`), so a Hub
    hiccup while fetching 2MB must not fail an 8GB model download that has
    already succeeded.

    **Best-effort stops at the ✕.** `Cancelled` is now something a fetch really
    raises — the tick carries it back (`worker_base.fetch_with_progress`), where
    it used to be latent — and it is the one exception here that is not a Hub
    hiccup. Absorbed, it would print "could not pre-fetch the speech detector:
    Cancelled" and report the download DONE, so a user who pressed stop would
    watch the row finish successfully. Re-raised first, and before the broad
    catch, because `Cancelled` is an ordinary `Exception` and order is the whole
    of the distinction.
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


def _snapshot_config(fetched) -> dict:
    """The snapshot's `config.json`, or {} — the disambiguator the format check
    needs: mlx-community's newer re-uploads call their weights
    `model.safetensors`, the name every transformers repo also has, and the
    native `n_mels`/`n_audio_ctx` config beside it is what says whisper."""
    try:
        with open(os.path.join(fetched, "config.json"), encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def load(model_id, fetched):
    """`fetched` is what `download` returned — the snapshot directory."""
    # The format check comes FIRST, before the import, exactly as it does in the
    # CT2 runner: a repo in the wrong format is a fact about the download and
    # not about this environment, and importing first would replace the
    # explanation below with whichever ImportError happened to come first.
    #
    # Checked by `formats.is_mlx_whisper_snapshot` rather than by catching the
    # loader's error, for the reason `faster_whisper/worker.py` gives about
    # `model.bin`: what that error says is "No such file or directory:
    # '…/weights.npz'", and a user reading it has no way to know their repo was
    # the wrong FORMAT rather than a broken download. From `formats` for the
    # reason the CT2 runner's own constant gives: the AI Models page reads the
    # same predicate, and its engine tag must not promise a load this function
    # refuses.
    #
    # **There are now THREE incompatible Whisper formats in this app** — CT2
    # (`model.bin`), MLX (here), and transformers — and the AI Models page
    # offers Load on anything whose task label says "speech recognition",
    # because the format is not in the label. This message is the only thing
    # between a user and a search engine, so it names the format they have, the
    # format this runner needs, and a repo that works.
    names = set(os.listdir(fetched))
    if not formats.is_mlx_whisper_snapshot(names, _snapshot_config(fetched)):
        raise RuntimeError(
            f"{model_id} is not an MLX conversion of Whisper — this runner "
            f"needs {formats.MLX_WHISPER_WEIGHTS[0]}, "
            f"{formats.MLX_WHISPER_WEIGHTS[1]}, or model.safetensors beside "
            "whisper's own config.json, and this repo is in another format "
            "(CTranslate2 repos carry model.bin; transformers repos carry "
            "model.safetensors with a transformers config). Try "
            "mlx-community/whisper-large-v3-turbo or "
            "mlx-community/whisper-medium-mlx.")

    # The RELEASED library (0.4.x) looks for `weights.safetensors` then
    # `weights.npz` — `model.safetensors` support exists only on mlx-examples
    # main, unreleased. Without this link, a repo the format check just accepted
    # dies inside `mx.load` on the missing `.npz` path with "[load_npz] Input
    # must be a zip file…", which explains nothing. One relative symlink in the
    # snapshot makes the accepted layout the one the library already reads;
    # drop it when the pin moves past a release that reads `model.safetensors`
    # itself.
    if (formats.MLX_WHISPER_SHARED_WEIGHTS in names
            and not any(name in names for name in formats.MLX_WHISPER_WEIGHTS)):
        os.symlink(formats.MLX_WHISPER_SHARED_WEIGHTS,
                   os.path.join(fetched, "weights.safetensors"))

    module = _transcribe_module()
    import mlx.core as mx

    # BEFORE the weights exist, because an array remembers the stream it was
    # made on and this thread is not the one that will decode. See `_pin_stream`.
    _pin_stream()

    # Primed rather than held: `transcribe()` looks its model up in this holder
    # by PATH, so priming it is what makes the weights resident now instead of
    # inside the first request. `float16` is not a choice made here — it is the
    # dtype `transcribe()` derives from its own `fp16` decode option, whose
    # default is True, and this file never passes that option. A mismatch would
    # not fail, it would silently load the model twice (once per dtype) and cost
    # the memory of both.
    module.ModelHolder.get_model(fetched, mx.float16)
    _loaded["path"] = fetched
    # See `worker_base.STATE["device"]`. MLX is Metal or nothing, so unlike the
    # CT2 runner there is nothing to detect — but the page shows this field to
    # explain a speed, and "mps" beside a Mac transcribing in seconds is the
    # answer to "why is this so much faster than it was".
    worker_base.set_state(device="mps")


def _transcribe_module():
    """`mlx_whisper.transcribe` the MODULE, never the function of the same name.

    `mlx_whisper/__init__.py` does `from .transcribe import transcribe`, so the
    package attribute `mlx_whisper.transcribe` is the FUNCTION and shadows its
    own module. `import mlx_whisper.transcribe as t` therefore binds the
    function, and `t.ModelHolder` is an AttributeError on a function object —
    which is exactly the confusing failure this helper exists to prevent.
    `import_module` returns the sys.modules entry, which is the module.
    """
    return importlib.import_module("mlx_whisper.transcribe")


def memory():
    """What MLX itself says it is holding, in bytes — never RSS alone.

    The same reason `mlx_text/worker.py` gives: MLX memory-maps its weights and
    its arrays are lazy, so RSS right after a load reports the interpreter and
    not the model. Metal's unified memory makes it worse here rather than
    better — the buffers are real and RSS still cannot see them, so the AI
    Models page would report a resident Whisper model as costing nothing.
    `worker_base` takes the larger of this and its own RSS reading, so a wrong
    answer in either direction is corrected by the other.

    `get_active_memory` moved out of `mlx.core.metal` into `mlx.core` and the
    old spelling is deprecated, so both are tried — a version skew should cost
    the better number, not raise inside `/health`.
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


#: What Whisper's mel front end expects, and the only rate this runner produces.
#: Not a preference — `log_mel_spectrogram` assumes it, so resampling elsewhere
#: would transcribe a chipmunk.
SAMPLE_RATE = 16000


def _decode_audio(path):
    """The file as 16 kHz mono float32, decoded IN THIS PROCESS.

    The whole reason this function exists: `mlx_whisper.transcribe(path)` would
    do this for us by spawning `ffmpeg`, which this app does not ship (see the
    folder's `pyproject.toml`). PyAV's wheels carry the ffmpeg libraries, so the
    same decode happens with no binary to find and no `subprocess` call — the
    property the runner-folder design exists to preserve.

    Three details, each of which produces silent nonsense rather than an error
    when it is wrong:

    * **`fltp` planar float**, because that is what Whisper's front end wants
      and an int16 buffer read as float is white noise at full scale.
    * **`layout="mono"`**, so a stereo interview mixes down rather than arriving
      as two interleaved channels — which reads as speech at double speed.
    * **the resampler is FLUSHED** (`resample(None)`). It buffers, so the tail
      of the recording — up to a frame of it — is still inside the filter when
      the container runs out. Dropping it truncates every transcript by a
      fraction of a second, which is invisible until it eats a word.

    Whole-file rather than streamed, deliberately: `transcribe()` wants the
    entire waveform anyway (it builds one mel spectrogram over all of it), so
    streaming would buy nothing and cost the ability to state the duration up
    front. Sixteen bits of float per sample is 64 kB per second of audio — a
    90-minute recording is ~350 MB, which is real but is a fraction of the
    model beside it.
    """
    import av
    import numpy as np

    chunks = []
    with av.open(path) as container:
        streams = container.streams.audio
        if not streams:
            # A video with no audio track, or a file that is not media at all.
            # Named here because PyAV's own answer is an IndexError, which
            # reaches the job row as "list index out of range".
            raise RuntimeError(f"{os.path.basename(path)} has no audio track to transcribe")
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(streams[0]):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise RuntimeError(f"{os.path.basename(path)} decoded to no audio")
    return np.concatenate(chunks).astype(np.float32)


# ------------------------------------------------------------------- reporting
#
# `_eta` and `_clock` are copied from `faster_whisper/worker.py` rather than
# shared. There is nowhere to share them TO: each runner runs on an interpreter
# built from its own folder, and the only module both can import is
# `worker_base`, which is the supervisor's contract and not a place for one
# capability's formatting. The copy is deliberate and the two must agree —
# `tests/test_ai_mlx_whisper_worker.py` pins the same cases the CT2 runner's
# tests pin, for the same reason: this clock is read one line under the job
# manager's own, and two formatters encoding one rule have to encode all of it.


def _eta(remaining_audio, elapsed, done_audio):
    """Wall-clock left, from how much AUDIO is left and how fast it is going."""
    if not done_audio or remaining_audio is None or remaining_audio <= 0:
        return ""
    remaining = remaining_audio * (elapsed / done_audio)
    if remaining < 60:
        return " · ~%ds left" % round(remaining)
    return " · ~%.1f min left" % (remaining / 60)


def _clock(seconds):
    """m:ss, or h:mm:ss once there are hours — `faster_whisper/worker.py`'s.

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
# The whisper runner's `_call_with_ticks` / `_orphan` / `_ORPHAN_WAIT_S`, with
# the reasoning it documents. Copied for the reason `_clock` is: two
# interpreters, no shared module below `worker_base`.

#: How often a blocking phase ticks. Module-level so a test can shorten it.
_TICK_S = 1.0

#: The work thread a cancel walked away from, if one is still running.
#:
#: A cancel unwinds the HANDLER, not the work: `worker_base._single` catches
#: `Cancelled`, replies, and leaves its `with GENERATE_LOCK` block while this
#: thread may still be inside `transcribe()` holding the mel buffer and the
#: model. A user who presses ✕ and immediately retries would then have two
#: decodes running on one process and one model — the exact thing that lock
#: exists to prevent. The lock belongs to the base and is not ours to hold
#: longer, so the abandoned thread is remembered and waited for here instead.
_orphan: "dict[str, threading.Thread | None]" = {"thread": None}

#: Asks the abandoned work to stop, and is the reason the wait below is usually
#: instant. The progress hook checks it once per decoded window, so a cancelled
#: `transcribe()` raises out of the library rather than running the recording to
#: its end — which the CT2 runner cannot do at all, because its eager phase has
#: no hook inside it. It is a REQUEST and not a guarantee: nothing checks it
#: during the `av` decode, and a window is up to 30 seconds of audio long.
_STOP = threading.Event()

#: How long a new transcription will wait for abandoned work to finish before
#: refusing instead. See the CT2 runner for the argument; in short, a wedge
#: inside PyAV or the decoder must not block every later transcription for the
#: life of the process, and a hang with a spinner is the worst failure
#: available.
_ORPHAN_WAIT_S = 30.0


def _await_orphan(job, row):
    """Let work abandoned by an earlier cancel finish before starting more.

    Ticks while it waits, for the same reason the work itself does: this is time
    the user is watching. Bounded by `_ORPHAN_WAIT_S`; raises rather than
    starting a second decode on the same model, which is what the wait exists to
    prevent. The orphan is KEPT on the deadline — it may still finish, and the
    next request then sails through.
    """
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

    `progress()` is asked, on every tick, what to say: it returns
    `(done, total, detail)`. That indirection is the whole difference from the
    CT2 runner's version of this function — there the ticked phase is a decode
    nothing is known about, so its ticks carry no numbers; here the same
    mechanism carries real seconds-of-audio progress during transcription (see
    `_watch_progress`), which is what keeps `onProgress` meaning what
    `runtime.js` says it means.

    Same shape as `worker_base.fetch_with_progress`, and for the same reason:
    the poll IS the progress and the cancellation point. `report_or_cancel` is
    what carries a ✕ back — a plain `report` cannot.

    **A ✕ that lands while the work is FINISHING does not discard it.** `cancelled`
    is the caller's flag, set to `{"late": True}` when the ✕ arrived but `call()`
    had already returned; the value is handed back normally and the caller
    decides what a cancel means at that point. Without it there is a window of up
    to `worker_base.JOB_TIMEOUT_S` (3s) — the round trip of the very tick that
    carries the ✕ — in which a completed transcription is thrown away: liveness
    was read BEFORE the report, so a `transcribe()` that returned during it still
    took the cancel branch, orphaned a thread that had already finished, and
    raised with `result["value"]` in hand and nothing written to disk. That is
    the failure `faster_whisper/worker.py` documents as "an hour of decoding
    discarded at 99%", reached by a different route: there the race is a cancel
    on the last SEGMENT, here it is a cancel during the last REPORT.

    Only a value is salvaged. A call that finished by RAISING has nothing worth
    keeping, so the cancel stands and is the better answer — the user asked to
    stop, and reporting the failure of work they abandoned sends them looking
    for a fault that does not matter.
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
            # re-read HERE rather than trusted from before the call, because the
            # call is where the time went — see the docstring's window.
            if _finished(thread, result):
                if cancelled is not None:
                    cancelled["late"] = True
                break
            # Handing the thread over BEFORE unwinding, so the next request
            # waits for it — and asking it to stop on the way past, which is
            # what usually makes that wait instant.
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
    from `is_alive()` a moment before `run()`'s assignment is visible on this
    one, and `result` can hold an `error` from a call that finished with nothing
    to salvage. Asking for a VALUE from a thread that has stopped is the only
    state in which a cancel has arrived too late to be worth honouring.
    """
    return not thread.is_alive() and "value" in result


# ------------------------------------------------------------------- progress


class _Ticker:
    """A stand-in for the `tqdm` bar `mlx_whisper.transcribe` builds internally.

    It counts FRAMES of audio at 100 per second — `total` is the recording and
    `update(n)` advances by the window just decoded — so this is not a
    reconstruction of progress, it is the library's own measurement of where it
    has got to, borrowed. See `_watch_progress` for why it is borrowed rather
    than asked for.

    `__enter__`/`__exit__` because the bar is used as a context manager, and
    `update` because that is the only method called on it. Everything else a
    real tqdm has is deliberately absent: if a future mlx-whisper calls
    something more, the AttributeError is a loud failure in a covered path
    rather than a wrong number in a quiet one.
    """

    def __init__(self, position, total=None, **kwargs):
        self._position = position
        self._position["total_frames"] = total or 0
        self._position["frames"] = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, n):
        self._position["frames"] = self._position.get("frames", 0) + (n or 0)
        # The one place a cancel can reach INSIDE the library. Raising here
        # unwinds `transcribe()` at a window boundary instead of letting an
        # abandoned decode run a 90-minute recording to its end while the next
        # request waits on `_await_orphan`.
        if _STOP.is_set():
            raise worker_base.Cancelled()


#: Frames per second in mlx-whisper's own units (`audio.FRAMES_PER_SECOND`),
#: which is the hop length divided into the sample rate: 16000 / 160 = 100.
#: Hard-coded rather than imported because it is a property of Whisper's mel
#: front end — the same constant openai-whisper, faster-whisper and this all
#: use — and importing it would make the progress path depend on a private
#: module layout that the shim below already has to be careful about.
_FRAMES_PER_SECOND = 100


class _watch_progress:
    """Borrow mlx-whisper's internal frame counter for the duration of a call.

    **The problem this solves is a PUBLIC one.** SPEC AI-10a and the
    `fused.ai.transcribe` doc comment in `runtime.js` both promise that
    `onProgress` gives `done`/`total` in seconds of audio. faster-whisper
    delivers that for free — its generator yields segments and the loop reports
    each one's end timestamp. `mlx_whisper.transcribe()` is one blocking call
    that returns everything at once, so the honest reading of it is 0% for the
    length of the recording and then 100%. A page written against the documented
    contract would draw a frozen bar for eighteen minutes, which is the failure
    mode this app treats as worse than no bar at all.

    Two ways to keep the promise, and this is the cheaper one:

    * **Chunk the audio here** and call `transcribe()` per chunk. Rejected as a
      way of getting PROGRESS: the library's own window seeking is what makes
      Whisper accurate across a boundary (it re-seeks to the last timestamp
      rather than cutting at a fixed offset, and conditions each window on the
      previous text), so chunking at arbitrary offsets trades transcript
      quality for a progress bar. (The VAD path does cut the audio — but only
      at boundaries the detector found IN SILENCE, where there is no sentence
      to cut through. Different cut, different trade; see `_transcribe_regions`.)
    * **Read the counter the library already keeps.** `transcribe()` runs its
      window loop inside `tqdm.tqdm(total=content_frames, unit="frames")` and
      calls `update()` with each window it finishes. That is exactly the number
      wanted, already in the right unit, measured by the code doing the work.

    So the module's `tqdm` binding is swapped for `_Ticker` for the duration of
    the call and restored afterwards. **This reaches into another package's
    module globals, and that is a real cost** — it is not part of mlx-whisper's
    API and a future version may not have it. Hence the guard: if the attribute
    is not there, the transcription runs exactly as before and the ticks carry
    no numbers instead of wrong ones, which `generate` reports honestly. The
    swap is also why `verbose=False` is passed: the bar is constructed only when
    verbose is False (`disable=verbose is not False`), and with `verbose=None`
    — the library's default — there would be nothing to borrow.

    What it costs in resolution: the counter advances once per decoded window,
    which is up to 30 seconds of audio, where faster-whisper reports every
    segment (a few seconds). Coarser, same unit, same meaning. A recording whose
    windows contain no speech is skipped without an update, so the bar can sit
    still through a long silence and then jump — it is behind, never ahead.
    """

    def __init__(self, position):
        self._position = position
        self._module = None
        self._saved = None

    def __enter__(self):
        module = _transcribe_module()
        if getattr(module, "tqdm", None) is None:
            # No hook: `available` stays False and the caller ticks without
            # numbers. Deliberately not an error — a transcription that works
            # with a coarse bar is better than one that refuses to start.
            self._position["available"] = False
            return self
        self._module = module
        self._saved = module.tqdm
        position = self._position
        position["available"] = True

        class _Tqdm:
            """The `tqdm` MODULE's shape, not the class's: the library calls
            `tqdm.tqdm(...)`, so what is swapped in has to answer to that."""

            @staticmethod
            def tqdm(*args, **kwargs):
                return _Ticker(position, **kwargs)

        module.tqdm = _Tqdm
        return self

    def __exit__(self, *exc):
        # Restored unconditionally, including on the cancel path: the module is
        # process-global and a `_Ticker` left behind would be handed to a decode
        # whose progress slot belongs to a request that is over.
        if self._module is not None:
            self._module.tqdm = self._saved
        return False


def _clip_seconds(position, speech):
    """How far into the CLIP the decoder has got, in seconds, or None.

    Seconds of the clip rather than of the recording, because that is what the
    borrowed counter actually measures: it counts mel frames of whatever
    waveform the library was handed, which once the silence is dropped and the
    regions are packed is neither the recording nor any contiguous part of it.
    Turning this into a position in the recording is `_original_end`'s job, and
    the two are separate because the ETA needs THIS number — its rate is
    `elapsed / speech decoded`, and a remapped position is a different currency
    (see `_transcribe_regions`).

    `speech` clamps it: mlx-whisper's bar counts the frames of a PADDED clip, so
    the last window of a two-second clip reports thirty. Unclamped, a bar would
    overshoot the clip and, through the remap, walk into silence that was cut
    out — a bar that can exceed its own total.

    None rather than 0 when there is no counter to read: a `done` of 0 is a
    claim that nothing has been transcribed, and `worker_base`/the job manager
    render an absent number as indeterminate, which is the truth here.
    """
    if not position.get("available"):
        return None
    return min(position.get("frames", 0) / _FRAMES_PER_SECOND, speech)


# ------------------------------------------------------------------------- VAD


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
_FETCH_FAILED = (OSError, http.client.HTTPException, ImportError)


def _onnx_failures():
    """Every exception ONNX Runtime's C++ layer can raise loading a model.

    Named as "the module that contains exactly these and nothing else" rather
    than as a list, because there is nothing else to name: onnxruntime registers
    each of its fifteen error types from pybind with `Exception` as the base, so
    there is no `OrtError` to catch and no shared ancestor short of `Exception`
    itself. Enumerating `Fail`, `InvalidProtobuf`, `NoSuchFile`, … here would
    quietly stop covering a runtime that adds a sixteenth.

    `()` when onnxruntime is absent, which needs no special case: the
    `ImportError` that then comes out of `vad.session` is degradable on its own
    account (see `_FETCH_FAILED`).
    """
    try:
        from onnxruntime.capi import onnxruntime_pybind11_state as ort_errors
    except ImportError:
        return ()
    return tuple(value for value in vars(ort_errors).values()
                 if isinstance(value, type) and issubclass(value, Exception))


def _speech_regions(audio, total, job, row):
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
    which is how they get found. `worker_base.Cancelled` is also an `Exception`
    and is excluded by the same narrowing — which is no longer a precaution: the
    fetch below ticks through `report_or_cancel` now that it reports into a
    transcription row with a live ✕, so a ✕ pressed while the 2MB detector is
    downloading raises HERE, and a wider catch would answer it by transcribing
    the whole file for the user who asked to stop.
    """
    import vad as vad_module

    sess = _loaded.get("vad")
    if sess is None:
        try:
            # Bound to THIS row, for the reason `_speaker_turns` gives. `download`
            # pre-fetches the detector so this is normally a cache hit, but a
            # machine whose whisper download predates AI-10f reaches the network
            # here — and used to reopen the model's finished load row to say so.
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
    return vad_module.speech_regions(audio, sess)


# ----------------------------------------------------------------- diarization


def _speaker_turns(audio, speakers, job, row):
    """Who spoke when, over the WHOLE waveform — `[(start, end, index), …]`.

    **Independent of the VAD, and before it.** The segmenter finds its own
    silence and is better at it than a threshold over Silero probabilities, and
    handing it VAD regions would mean clustering voices across cuts made for a
    different purpose. Turns come back in original-recording time, which is the
    same clock `_transcribe_regions` maps every segment into — so the join in
    `generate` is a join, not a second remap.

    **A failure here FAILS the transcription**, which is the opposite of what
    `_speech_regions` does two functions down, and the difference is what the
    caller asked for. `vad` is an optimisation the user did not request and a
    transcript without it is still the transcript they wanted; `diarize: true`
    is a request for speaker labels, and quietly returning an unlabelled
    transcript would answer a different question while looking like success.
    So there is no degradation path: no `try`, no fallback, and the reason
    reaches the job row as an error.

    Ticked through `_call_with_ticks` so the row stays alive and the ✕ stays
    answerable, and `_STOP` is threaded into sherpa's own progress callback so
    an abandoned diarization stops at the next chunk rather than running the
    recording out — the same reach-inside `_Ticker` gives the decode.

    **`done`/`total` stay None for the whole phase.** They are SECONDS OF AUDIO
    (SPEC AI-10a, promised to pages by `runtime.js`) and they denominate the
    TRANSCRIPT; a pre-pass that filled them would either inflate `total` past
    the recording or run the bar to 100% before a word was transcribed. The row
    already has the field for this — `detail` — and an indeterminate bar under
    its own sentence is what the audio decode above already does.
    """
    # Bound to THIS request's row: the fetch happens inside a transcription, so
    # an unbound `download_file` would tick into `JOB_ID` — the model's own
    # finished load row — while the row the user is watching said nothing.
    def fetch(repo_id, filename, detail=None):
        return worker_base.download_file(repo_id, filename, detail=detail,
                                         job=job, row=row)

    segmentation, embedding = diarize.model_paths(fetch)
    worker_base.report(job=job, **row, state="running", done=None, total=None,
                       detail="Finding speakers…")
    session = diarize.diarizer(segmentation, embedding, speakers)

    def run():
        return diarize.speaker_turns(audio, session, SAMPLE_RATE,
                                     should_stop=_STOP.is_set)

    try:
        return _call_with_ticks(run, job, row,
                                lambda: (None, None, "Finding speakers…"))
    except diarize.DiarizationCancelled:
        # `_STOP` was set by the tick loop on its way out of a ✕, so the cancel
        # is already in flight on this thread's behalf; this is the abort
        # arriving from inside sherpa a moment later. Translated rather than
        # propagated, because `diarize.py` cannot name `worker_base.Cancelled`
        # (it is imported by the server too) and the supervisor only knows the
        # one type.
        raise worker_base.Cancelled()


def _packs_to_decode(regions, duration):
    """The clips to transcribe, each as a LIST of `(start, end)` regions in
    original-recording time whose speech travels in one `transcribe()` call.

    `duration` is the NUMBER of seconds decoded, never the row's `total` — the
    two differ for a recording so short it rounds to zero, where `total` is None
    (indeterminate, which is what the row should show) and this arithmetic needs
    0.0. The wording is `parakeet_mlx`'s because the split is: handing the None
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


def _decode_clip(module, clip, fetched, task, language, initial_prompt):
    """One `transcribe()` call, on the thread `_call_with_ticks` gave it.

    A named function rather than the lambda it replaced, because the pin has to
    happen ON THIS THREAD — every decode runs on a thread of its own, and the
    weights it is about to touch were primed on another. See `_pin_stream`.
    """
    _pin_stream()
    return module.transcribe(clip, path_or_hf_repo=fetched, task=task,
                             language=language, initial_prompt=initial_prompt,
                             verbose=False)


def _transcribe_regions(audio, packs, fetched, task, language, initial_prompt,
                        job, row, total, duration, transcribing_since,
                        progressive=None):
    """Transcribe each clip and return `(segments, language)` in ORIGINAL time.

    `packs` is `_packs_to_decode`'s list: one entry per `transcribe()` call,
    each a list of regions whose speech that call is handed CONCATENATED.

    **`total` and `duration` are the same seconds in two currencies**, and the
    split is `parakeet_mlx/worker.py`'s, ported rather than reinvented: `total`
    is what the ROW carries and is None for a recording too short to round to a
    tenth of a second, because an indeterminate bar is the honest rendering of
    "no length worth showing"; `duration` is the arithmetic, and is 0.0 there.
    Only the reporting may see the None.

    `progressive` is the partial-transcript sink (`runners/partial.py`), fed
    from the one place in this file where a segment is finished AND already
    remapped into original-recording time — the only point at which a line is
    safe to publish, since a page seeking a player off a clip-relative
    timestamp would land in the wrong minute. Omitted, a no-op stands in.

    **The cut is at a VAD boundary, never at a fixed offset**, and that is what
    makes this loop acceptable where the chunking `_watch_progress` rejects is
    not. Whisper's accuracy across a boundary comes from re-seeking to the last
    timestamp and conditioning each window on the previous text; cutting mid
    sentence throws both away. A VAD boundary is by construction a stretch of
    silence at least `MIN_SILENCE_S` long, which is where a sentence has already
    ended.

    **How MANY of those cuts share a call is a cost question, and the answer is
    "as many as fit"** (`vad.pack_regions`). The library pads every call's mel to
    its full 30-second window, so a call per region billed 30 seconds of encoder
    for a 0.8-second region: `vad: true` cost `large-v3-turbo` 23.30s on a
    216-second recording that decodes whole in 8.32s, and 9.31s once packed. The
    silence still never reaches the decoder — the clip is the regions
    concatenated, not one span from the first to the last, which is the
    difference between dropping the silence and merely relabelling it.

    **What is still lost is conditioning ACROSS a CALL.** `condition_on_previous_
    text` operates inside one `transcribe()` call, so the first region of a clip
    starts with no memory of the last region of the one before it — a proper noun
    established early can be spelled differently later. Packing shrinks that loss
    rather than removing it: regions inside one clip now do condition on each
    other (it is one continuous waveform to the decoder), which is a side effect
    of the packing and not its reason. Carrying the previous clip's tail in as
    `initial_prompt` was considered and NOT done: it invites the model to
    continue a sentence that finished before the silence, which is the known way
    to trigger a repetition loop, and it would make each clip's output depend
    on the previous clip's errors. A caller's own `initial_prompt` goes to
    EVERY clip instead, since it is context about the recording as a whole
    (names, jargon) rather than about a position in it.

    **The language is detected once and then pinned.** With `language=None` each
    call would detect independently, so a quiet region could come back as a
    different language and the transcript would change tongue halfway down. The
    first region's answer is used for the rest, which is also faster: detection
    is a decode of its own.
    """
    module = _transcribe_module()
    segments = []
    detected = language
    if progressive is None:
        progressive = partial.sink(None)

    #: The ETA's own currency: seconds of audio there actually are to DECODE,
    #: which once silence is dropped is not the length of the recording.
    #:
    #: `done`/`total` stay denominated in the original recording — that is the
    #: public contract (AI-10a), and that they drift from the decoder's own
    #: units is the same accepted trade `faster_whisper/worker.py` documents.
    #: The ETA cannot live with it, because it does not report a position, it
    #: divides: `elapsed / done_audio` is a RATE, and feeding it the remapped
    #: position mixes two currencies in one fraction. A 60-second speech region
    #: at the end of an hour-long file made the first tick read
    #: `60 * (0.5 / 3540)` — "~0s left" for the entire decode — and the same
    #: region at the START of that file ends on `3540 * (6 / 60)`, promising
    #: six minutes on a job that is about to finish.
    speech_total = sum(_speech_seconds(pack) for pack in packs)
    #: Seconds of speech finished by earlier clips. The current clip's own
    #: contribution comes off the live counter, below.
    decoded = 0.0

    for index, pack in enumerate(packs):
        last = index == len(packs) - 1
        speech = _speech_seconds(pack)
        # `duration`, not `total`: this is arithmetic on the waveform, and it has
        # to match what `_packs_to_decode` was handed or the whole-file clip stops
        # being recognised as one and gets needlessly copied.
        clip = audio if pack == [(0.0, duration)] else _clip_samples(audio, pack)
        if len(clip) < SAMPLE_RATE // 10:
            # Under a tenth of a second. Whisper pads anything shorter than its
            # 30s window anyway, so this is all padding and no signal — and a
            # clip of a few hundred samples is where the decoder hallucinates a
            # sentence out of nothing.
            continue
        position = {}

        def progress(regions=pack, span=speech, before=decoded):
            at = _clip_seconds(position, span)
            if at is None:
                # No counter to read (see `_watch_progress`). Honest
                # indeterminate ticking rather than an invented percentage —
                # the tick is here to be answered, not to move a bar.
                return None, None, "Transcribing…"
            # Two currencies, deliberately: `done` is REPORTED, so it is mapped
            # back onto the recording's clock (AI-10a); `speech_done` is
            # DIVIDED, so it stays in seconds actually decoded. `at` is this
            # clip's share, already clamped to its speech, and `before` is what
            # earlier clips contributed. All three are default arguments because
            # the closure outlives the loop iteration that made it, and a late
            # tick reading the loop variables would price this clip against a
            # later clip's progress.
            # `_original_end`, not `_original_start`: the counter reports how far
            # decoding HAS got, so a position sitting exactly on a join is the end
            # of the region just finished, not the start of one not begun. The bar
            # is then behind rather than ahead, which is the rule `_watch_progress`
            # already states about its coarse updates.
            done = round(_original_end(regions, at), 2)
            elapsed = time.time() - transcribing_since
            speech_done = before + at
            return done, total, "Transcribing — %s of %s%s" % (
                _clock(done), _clock(total) if total else "?",
                _eta(speech_total - speech_done if speech_total else None,
                     elapsed, speech_done))

        # A ✕ landing while a call RETURNS is not honoured for THAT call — its
        # transcript is complete and that decoding is spent, exactly as
        # `faster_whisper/worker.py` guards on its last segment. Whether it is
        # honoured for the RUN depends on what is left: see below.
        late_cancel = {}
        with _watch_progress(position):
            result = _call_with_ticks(
                lambda: _decode_clip(module, clip, fetched, task, detected,
                                     initial_prompt),
                job, row, progress, cancelled=late_cancel)

        # This clip is decoded, whatever its counter last said: the borrowed bar
        # counts mel frames of a PADDED window, so trusting its final value
        # would drift the rate a little further from the truth on every clip.
        decoded += speech
        detected = detected or result.get("language")
        for segment in (result.get("segments") or []):
            # The library's segments carry tokens, logprobs and temperatures
            # too; only these three are published, because these three are the
            # CT2 runner's shape and a page must not have to know which one ran.
            #
            # `_original_start`/`_original_end` are the remap: timestamps come
            # back relative to the CLIP — which is this pack's regions
            # concatenated, silence removed — and every consumer, the .json file,
            # a caption track, a page seeking a player, reads them as positions
            # in the FILE. Getting this wrong
            # is silent: a transcript that looks perfect and whose every
            # timestamp after the first join is early by the length of the
            # silence that was dropped.
            #
            # EACH END is mapped on its own, and by its OWN flavour of the
            # mapping, because a segment can span a join: the decoder hears
            # continuous speech across it (the silence is not in the clip it was
            # given), so a start and an end can fall in different source regions,
            # and one shared offset would place both in whichever region was
            # picked. A time landing exactly ON a join is the case the two
            # flavours exist for — see `_original_end`. Both clamp to the pack's
            # last moment, because Whisper times against a padded 30s window: the
            # last segment of a two-second clip can end at 29, and a
            # hallucination in the padding can START there too. Unclamped, either
            # one places speech inside the silence that was removed and reorders
            # it against the next clip.
            #
            # **A segment that DOES span a join keeps its real interval, and that
            # is deliberate**: the words genuinely sit either side of the pause,
            # and a page seeking a player wants where they are in the recording.
            # What must not treat the interval as solid is the SPEAKER scoring —
            # diarization ran on the full waveform and routinely has turns inside
            # the gap — which is why `generate` hands `assign_speakers` the region
            # list as a mask (D358).
            at = _original_start(pack, float(segment.get("start") or 0.0))
            until = _original_end(pack, float(segment.get("end") or 0.0))
            if at >= until:
                # Clamping alone is not enough for a segment that begins past
                # the clip: end-only clamping emitted `{start: 15.0, end: 12.0}`
                # — a segment running BACKWARDS — and clamping both would flatten
                # it to zero length at the boundary, which is text asserted to
                # have been spoken during silence that was cut out. Nothing was
                # said there, so the honest transcript omits it. Real speech that
                # merely overruns its clip still survives, clamped: it starts
                # inside, so `at < until`. The mapping is monotonic, so this can
                # only ever fire on a segment past the end — never on one that
                # merely crossed a join.
                continue
            segments.append({
                "start": round(at, 2),
                "end": round(until, 2),
                "text": str(segment.get("text") or "").strip(),
            })
            # Published the instant it is final. This runner reports progress
            # once per decoded 30s WINDOW rather than per segment, so without
            # this a page's bar jumps and its transcript stays empty until the
            # end; with it, the words arrive in the same jumps the bar does.
            progressive.add(segments[-1])
        if late_cancel.get("late"):
            # The rule the CT2 runner states: a cancel is worth honouring
            # exactly while there is work left to stop. On the LAST clip there
            # is none — the transcript is finished, and discarding it would be
            # "an hour of decoding thrown away at 99%". On any earlier clip
            # there are minutes of decoding still to come, so the ✕ stands, and
            # it must: keeping the first region of five and writing it out
            # would present a fifth of a transcript as a whole one, which is
            # the worse of the two failures by a distance.
            if not last:
                raise worker_base.Cancelled()
            break

    return segments, detected


def _clip_samples(audio, pack):
    """One clip's samples: this pack's regions, CONCATENATED.

    Only ever called for a filtered pack — the unfiltered path passes the whole
    waveform through untouched — which is why importing the detector's module
    here costs nothing on a machine that has no detector (`vad.py`'s heavy
    import is inside `vad.session`, not at its module scope)."""
    import vad as vad_module

    return vad_module.packed_samples(audio, pack, SAMPLE_RATE)


def _speech_seconds(pack):
    """Seconds of SPEECH in one pack — the length of the clip that will be
    decoded, not of the stretch of recording it was cut out of."""
    import vad as vad_module

    return vad_module.packed_duration(pack)


def _original_start(pack, at):
    """`at` seconds into a packed clip → a segment's START, in RECORDING time."""
    import vad as vad_module

    return vad_module.original_start(pack, at)


def _original_end(pack, at):
    """`at` seconds into a packed clip → a segment's END, in RECORDING time.

    Two functions, not one with a flag, because the two are asked DIFFERENT
    questions about a time landing exactly on a join: a start belongs to the
    region that begins there, an end to the region that ends there. One shared
    mapping used for both stretched any segment that merely ended on a join
    across the whole dropped pause (`3.0-5.0` → `3.0-30.0`), which is text
    claimed to have been spoken during silence this runner removed — and which
    `diarize.speaker_for` would then label from the turns inside that silence.
    `vad.py` carries the reasoning and the grid arithmetic.

    Both live in `vad.py` rather than here for AI-10f's reason: the packing and
    its inverse are one decision, they have to be read together to be checked,
    and the module that defines what `vad: true` means is the place both live.
    Called per tick and per segment endpoint, so it is deliberately arithmetic
    over a short list and nothing else."""
    import vad as vad_module

    return vad_module.original_end(pack, at)


# --------------------------------------------------------------- transcription


def generate(body):
    """Transcribe one file. Returns `{path, output, segments, language, …}`.

    Byte-for-byte the CT2 runner's result dict and the same two files on disk —
    a page must not be able to tell which runner served it (SPEC AI-10c).
    """
    fetched = _loaded.get("path")
    if fetched is None:
        raise RuntimeError("no model is loaded")

    source = str(body.get("path") or "")
    out = str(body.get("out") or "")
    out_text = str(body.get("outText") or "")
    # Where each segment lands AS it is decoded. Passed rather than derived, for
    # the reason `outText` is: the server owns where user files go. Absent — an
    # older request, or a caller that wants none — it is a no-op sink and this
    # function runs exactly as it did before the feature existed.
    out_partial = str(body.get("outPartial") or "") or None
    if not source:
        raise ValueError("'path' must be the audio file to transcribe")
    if not out or not out_text:
        raise ValueError("'out' and 'outText' must be where to write the transcript")

    task = str(body.get("task") or "transcribe")
    # None, not "": a falsy language means "detect it", and an empty string
    # would be passed through as a language code that matches none.
    language = str(body.get("language") or "") or None
    initial_prompt = str(body.get("initialPrompt") or "") or None
    # `is None`, not `get("vad", True)`: a JSON null is "not specified", and a
    # default reached only by an absent KEY inverts for the caller that spreads
    # an options object carrying an unset one. (The bug the CT2 runner shipped.)
    vad = True if body.get("vad") is None else bool(body.get("vad"))
    # Defaults FALSE, so every existing caller's output is byte-identical: no
    # `speaker` on a segment, no `speakers` in the JSON, no 33MB download.
    diarizing = bool(body.get("diarize"))
    # Validated HERE, before the decode, and by the shared rule — the bridge
    # and the server both check it first, but neither is the only door into
    # this process, and a `speakers` refused after ninety seconds of `av` is a
    # refusal the user paid for.
    #
    # **None is a legitimate answer** (D318): it means the caller did not say
    # how many people are in the recording, and `diarize.diarizer` clusters by
    # distance instead of by count. Only a bad EXPLICIT value raises here.
    speakers = diarize.speakers_or_raise(body.get("speakers")) if diarizing else None
    job = body.get("job") or None
    # The row's IDENTITY, carried on every tick — see
    # `supervisor.transcribe_row_fields`. A tick missing `title` is refused
    # outright and one missing `cancellable`/`unit` rebuilds a row that looks
    # operable and is not.
    row = body.get("row") or {}

    started = time.time()
    worker_base.report(job=job, **row, state="running",
                       done=0, total=None, detail="Decoding audio…")
    _await_orphan(job, row)
    _STOP.clear()

    # PHASE ONE — the file becomes a waveform, in this process. Ticked because
    # it is not free: a 90-minute recording is a real decode, and it is the one
    # phase with no progress hook inside it, so its ticks carry no numbers and
    # exist to keep the row alive and the ✕ answerable.
    decode_cancel = {}
    audio = _call_with_ticks(lambda: _decode_audio(source), job, row,
                             lambda: (None, None, "Decoding audio…"),
                             cancelled=decode_cancel)
    if decode_cancel.get("late"):
        # The ✕ landed as the DECODE finished, so nothing was lost by letting it
        # complete — but the transcription has not started, and that is all of
        # the work. A cancel is worth honouring exactly while there is work left
        # to stop (the CT2 runner's rule), and here there is essentially all of
        # it, so the salvage is refused rather than turned into a run the user
        # asked not to have.
        raise worker_base.Cancelled()
    # The duration is OURS here, not the decoder's: we hold the samples, so it
    # is exact rather than a container's declared length. It is also available
    # before the model sees a thing, which is what lets the very first
    # transcribing tick carry a `total`.
    #
    # TWO names for it, ported from `parakeet_mlx/worker.py` (which fixed this
    # first, and whose comment says the same thing) rather than solved a second
    # way, because the difference is a real file: a clip of a few dozen samples
    # rounds to 0.0, and `total` is what the ROW carries — where 0 would be a bar
    # claiming a length nobody can see move, so None (indeterminate) is the
    # honest value. `duration` is the arithmetic, and it stays a number: handing
    # the None on made the only pack `[(0.0, None)]` and the speech sum over the
    # packs raised a TypeError, turning a file the decode loop skips in one line
    # into a traceback on the job row.
    duration = round(len(audio) / SAMPLE_RATE, 2)
    total = duration or None

    # PHASE ONE-AND-A-HALF — who is speaking, over the whole waveform. Before
    # the VAD and independent of it (see `_speaker_turns`). It gets its own
    # `detail` line rather than a share of the transcript's bar because it
    # produces no transcript, NOT because it is quick — it is not: measured at
    # 11.5s on a 216-second recording the decode itself finishes in 9s (see
    # `diarize.NUM_THREADS`), so on a diarized run this is a phase the user
    # genuinely waits on, which is exactly why it must say something.
    turns = _speaker_turns(audio, speakers, job, row) if diarizing else None

    # The ETA's clock starts HERE, not at `started`: the audio decode produced
    # no transcript, so charging its seconds to the first window makes the rate
    # read as wildly slower than it is. **Below the diarization for the same
    # reason, not by accident.** `_transcribe_regions` divides `elapsed` by
    # seconds of speech decoded, so every second charged to this clock before a
    # word exists inflates the whole first minute of ETAs — and on a 90-minute
    # recording the pre-pass is minutes. Started above it, the first tick priced
    # the transcript at the diarization's wall time plus its own, which is the
    # exact failure this variable exists to prevent, reintroduced one phase
    # later. `faster_whisper/worker.py` starts its clock after both pre-passes.
    transcribing_since = time.time()

    # PHASE TWO — find the speech. Skipped entirely when the caller said not to,
    # which is what `vad: false` now means: no Silero, and mlx-whisper's own
    # per-window `no_speech_threshold` left at its default. That is closer to
    # faster-whisper's `vad_filter=False` than the previous mapping (which
    # disabled the threshold as a stand-in for a filter this runner did not
    # have) — the two engines now mean the same thing by the same flag.
    regions = _speech_regions(audio, total, job, row) if vad else None
    # One entry per `transcribe()` call, each carrying the regions whose speech
    # rides in it: the library pads every call to a 30-second window, so a call
    # per region is what made `vad: true` cost more than it saved.
    packs = _packs_to_decode(regions, duration)

    # Everything from here to the final write happens inside the sink, because
    # its EXIT is the lifecycle: reaching the end means the real output landed
    # and the partial file is duplicate bytes; a `Cancelled` means the user does
    # not want this transcript at all; anything else LEAVES the file, which is
    # the only salvage from a run that died halfway. See `runners/partial.py`.
    # `spans` for the reason `assign_speakers` gets it below: the sink labels each
    # line as it lands, and a live label scored over a dropped pause is a speaker
    # the page has to un-render when the final file disagrees.
    with partial.sink(out_partial, turns=turns, spans=regions or None,
                      cancelled=(worker_base.Cancelled,)) as progressive:
        # PHASE THREE — transcribe each clip, ticked from here and watched from
        # inside, with every timestamp mapped back to original-recording time.
        segments, language = _transcribe_regions(
            audio, packs, fetched, task, language, initial_prompt,
            job, row, total, duration, transcribing_since,
            progressive=progressive)

        # PHASE FOUR — the join. Both engines call the SAME function on the same
        # two lists, which is what makes "identical labels" structural rather
        # than a thing to keep testing (AI-10c).
        #
        # `spans` is the one thing this engine adds, and it is a SCORING mask
        # rather than a different join: packing lets a segment straddle a dropped
        # pause (see `_transcribe_regions`), the diarizer saw the whole waveform
        # including that pause, and unmasked the label went to whoever spoke in
        # the silence instead of to whoever said the words. `regions or None`
        # because an EMPTY region list is the detector finding no speech at all —
        # the whole file is then decoded, nothing is dropped, and a mask of no
        # intervals would score every segment against nobody and unlabel the
        # entire transcript. None (the other engines' value) is the honest mask
        # for "no silence was removed".
        speaker_list = (diarize.assign_speakers(segments, turns,
                                                spans=regions or None)
                        if turns is not None else None)

        text = " ".join(s["text"] for s in segments).strip()
        payload = {
            "path": source,
            "output": out,
            "outputText": out_text,
            "model": body.get("model") or "",
            "task": task,
            "language": language,
            "duration": total,
            "seconds": round(time.time() - started, 2),
            "segments": segments,
        }
        if speaker_list is not None:
            # ADDITIVE, and only when asked: a run without `diarize` writes
            # exactly the bytes it always did, key for key. The list is the
            # transcript's legend — the labels that actually landed on a
            # segment — so a page can build a colour map without walking
            # thousands of segments first.
            payload["speakers"] = speaker_list
            if speakers is None:
                # …and additive AGAIN, for the same reason one layer in: a run
                # that GAVE the count writes exactly the bytes it wrote before
                # estimation existed, because it already knows this number.
                # An estimating run is the only one that learned something,
                # and this is where it says what (D318). It is the SEGMENTER's
                # count, which can exceed the legend — see `speaker_count`.
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
