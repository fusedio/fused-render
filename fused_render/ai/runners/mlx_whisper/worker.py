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
  The silence is dropped, each speech region is transcribed on its own, and
  every timestamp is mapped back to original-recording time. The cut is always
  at a boundary the detector found in silence, never at a fixed offset.
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

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# …and THIS folder, for `vad`. Python already puts a script's own directory on
# the path, so in production this line changes nothing — but the supervisor is
# not the only thing that loads this file: the tests import it by path, where
# sys.path[0] is whatever the test runner's is, and a sibling module that
# resolves only under one of the two loaders is a test suite exercising a
# different import than the one that ships.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded model's snapshot PATH, not the model. See the module docstring:
#: `mlx_whisper` owns the object, in `transcribe.ModelHolder`, and a second
#: reference here would only be a way for the two to disagree about which model
#: is resident.
_loaded = {}


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
    """
    snapshot = worker_base.download_snapshot(model_id)
    try:
        import vad as vad_module

        vad_module.model_path(worker_base.download_file)
    except Exception as error:  # noqa: BLE001 - see the docstring
        print(f"could not pre-fetch the speech detector: "
              f"{error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
    return snapshot


#: What an MLX conversion always has and neither of the other two Whisper
#: formats does. `.npz` is what `mlx-community` publishes today and
#: `.safetensors` is what `mlx_whisper.load_models` prefers when it is there, so
#: both are accepted — a repo with either is loadable, and requiring the older
#: spelling would refuse a re-upload that works.
#:
#: Checked by NAME rather than by catching the loader's error, for the reason
#: `faster_whisper/worker.py` gives about `model.bin`: what that error says is
#: "No such file or directory: '…/weights.npz'", and a user reading it has no
#: way to know their repo was the wrong FORMAT rather than a broken download.
_MLX_WEIGHTS = ("weights.npz", "weights.safetensors")


def load(model_id, fetched):
    """`fetched` is what `download` returned — the snapshot directory."""
    # The format check comes FIRST, before the import, exactly as it does in the
    # CT2 runner: a repo in the wrong format is a fact about the download and
    # not about this environment, and importing first would replace the
    # explanation below with whichever ImportError happened to come first.
    #
    # **There are now THREE incompatible Whisper formats in this app** — CT2
    # (`model.bin`), MLX (here), and transformers (`model.safetensors`) — and
    # the AI Models page offers Load on anything whose task label says "speech
    # recognition", because the format is not in the label. This message is the
    # only thing between a user and a search engine, so it names the format they
    # have, the format this runner needs, and a repo that works.
    if not any(os.path.isfile(os.path.join(fetched, name)) for name in _MLX_WEIGHTS):
        raise RuntimeError(
            f"{model_id} has no {_MLX_WEIGHTS[0]} — this runner loads MLX "
            "conversions of Whisper, and this repo is in another format "
            "(CTranslate2 repos carry model.bin, transformers repos carry "
            "model.safetensors). Try mlx-community/whisper-large-v3-turbo or "
            "mlx-community/whisper-medium-mlx.")

    module = _transcribe_module()
    import mlx.core as mx

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


def _seconds_done(position, offset=0.0, limit=None):
    """Where the decoder has got to, in **seconds of the ORIGINAL recording**.

    `offset` is where the clip being transcribed starts in that recording, and
    `limit` is where it ends. Both exist because of the VAD: once silence is
    dropped, the borrowed counter denominates seconds of SPEECH, and reporting
    that against a `total` of the whole recording would be two different units
    in one progress bar — a 90-minute file with 30 minutes of silence would
    finish at 60/90 and stop. Mapping the counter back through the region is
    what keeps `done`/`total` meaning what SPEC AI-10a says they mean.

    `limit` clamps rather than trusting the counter: mlx-whisper's bar counts
    the mel frames of a padded clip, so the last window of a region can report
    a fraction of a second past its end, and a bar that overshoots into the
    silence it skipped is a bar that can exceed its own total.

    None rather than 0 when there is no counter to read: a `done` of 0 is a
    claim that nothing has been transcribed, and `worker_base`/the job manager
    render an absent number as indeterminate, which is the truth here.
    """
    if not position.get("available"):
        return None
    done = offset + position.get("frames", 0) / _FRAMES_PER_SECOND
    if limit is not None:
        done = min(done, limit)
    return round(done, 2)


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
    and is excluded by the same narrowing — no download reports through
    `report_or_cancel` today, so a swallowed ✕ is latent rather than live, but a
    catch that COULD swallow one is not a catch worth leaving in place.
    """
    import vad as vad_module

    sess = _loaded.get("vad")
    if sess is None:
        try:
            sess = vad_module.session(
                vad_module.model_path(worker_base.download_file))
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


def _regions_to_decode(audio, regions, total):
    """The clips to transcribe, as `(start, end)` in original-recording time.

    One region spanning everything is what "no VAD" looks like, and that is the
    point of the shape: the filtered and unfiltered paths are ONE loop, so the
    timestamp remap, the progress mapping and the cancel behaviour cannot drift
    between them. `vad: false` is not a different code path, it is a region
    list of length one.

    A recording the detector finds NO speech in also decodes whole. Reporting
    an empty transcript for a file nobody looked at would be the confident
    version of a wrong answer — the detector is tuned for speech, not for
    whispering, singing or a bad microphone, and Whisper's own no-speech
    handling is the better final word.
    """
    if not regions:
        return [(0.0, total)]
    return list(regions)


def _transcribe_regions(audio, clips, fetched, task, language, initial_prompt,
                        job, row, total, transcribing_since):
    """Transcribe each clip and return `(segments, language)` in ORIGINAL time.

    **The cut is at a VAD boundary, never at a fixed offset**, and that is what
    makes this loop acceptable where the chunking `_watch_progress` rejects is
    not. Whisper's accuracy across a boundary comes from re-seeking to the last
    timestamp and conditioning each window on the previous text; cutting mid
    sentence throws both away. A VAD boundary is by construction a stretch of
    silence at least `MIN_SILENCE_S` long, which is where a sentence has already
    ended.

    **What is still lost is conditioning ACROSS the gap.** `condition_on_
    previous_text` operates inside one `transcribe()` call, so region two starts
    with no memory of region one — a proper noun established before a pause can
    be spelled differently after it. Carrying the previous region's tail in as
    `initial_prompt` was considered and NOT done: it invites the model to
    continue a sentence that finished before the silence, which is the known way
    to trigger a repetition loop, and it would make each region's output depend
    on the previous region's errors. A caller's own `initial_prompt` goes to
    EVERY region instead, since it is context about the recording as a whole
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

    for index, (start, end) in enumerate(clips):
        last = index == len(clips) - 1
        clip = audio if (start, end) == (0.0, total) else _slice(audio, start, end)
        if len(clip) < SAMPLE_RATE // 10:
            # Under a tenth of a second. Whisper pads anything shorter than its
            # 30s window anyway, so this is all padding and no signal — and a
            # clip of a few hundred samples is where the decoder hallucinates a
            # sentence out of nothing.
            continue
        position = {}

        def progress(offset=start, limit=end):
            done = _seconds_done(position, offset=offset, limit=limit)
            if done is None:
                # No counter to read (see `_watch_progress`). Honest
                # indeterminate ticking rather than an invented percentage —
                # the tick is here to be answered, not to move a bar.
                return None, None, "Transcribing…"
            elapsed = time.time() - transcribing_since
            return done, total, "Transcribing — %s of %s%s" % (
                _clock(done), _clock(total) if total else "?",
                _eta(total - done if total else None, elapsed, done))

        # A ✕ landing while a call RETURNS is not honoured for THAT call — its
        # transcript is complete and that decoding is spent, exactly as
        # `faster_whisper/worker.py` guards on its last segment. Whether it is
        # honoured for the RUN depends on what is left: see below.
        late_cancel = {}
        with _watch_progress(position):
            result = _call_with_ticks(
                lambda: module.transcribe(
                    clip, path_or_hf_repo=fetched, task=task, language=detected,
                    initial_prompt=initial_prompt, verbose=False),
                job, row, progress, cancelled=late_cancel)

        detected = detected or result.get("language")
        for segment in (result.get("segments") or []):
            # The library's segments carry tokens, logprobs and temperatures
            # too; only these three are published, because these three are the
            # CT2 runner's shape and a page must not have to know which one ran.
            #
            # `+ start` is the remap: timestamps come back relative to the CLIP,
            # and every consumer — the .json file, a caption track, a page
            # seeking a player — reads them as positions in the FILE. Getting
            # this wrong is silent: a transcript that looks perfect and whose
            # every timestamp after the first gap is early.
            # BOTH ends are clamped to the region, because Whisper times against
            # a padded 30s window: the last segment of a two-second clip can end
            # at 29, and a hallucination in the padding can START there too.
            # Unclamped, either one places speech inside the silence that was
            # removed and reorders it against the next region.
            at = min(float(segment.get("start") or 0.0) + start, end)
            until = min(float(segment.get("end") or 0.0) + start, end)
            if at >= until:
                # Clamping alone is not enough for a segment that begins past
                # the clip: end-only clamping emitted `{start: 15.0, end: 12.0}`
                # — a segment running BACKWARDS — and clamping both would flatten
                # it to zero length at the boundary, which is text asserted to
                # have been spoken during silence that was cut out. Nothing was
                # said there, so the honest transcript omits it. Real speech that
                # merely overruns its region still survives, clamped: it starts
                # inside, so `at < until`.
                continue
            segments.append({
                "start": round(at, 2),
                "end": round(until, 2),
                "text": str(segment.get("text") or "").strip(),
            })
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


def _slice(audio, start, end):
    """One clip's samples. Only ever called for a REGION — the unfiltered path
    passes the whole waveform through untouched — which is why importing the
    detector's module here costs nothing on a machine that has no detector."""
    import vad as vad_module

    return vad_module.slice_samples(audio, (start, end), SAMPLE_RATE)


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
    total = round(len(audio) / SAMPLE_RATE, 2) or None
    # The ETA's clock starts HERE, not at `started`: the audio decode produced
    # no transcript, so charging its seconds to the first window makes the rate
    # read as wildly slower than it is.
    transcribing_since = time.time()

    # PHASE TWO — find the speech. Skipped entirely when the caller said not to,
    # which is what `vad: false` now means: no Silero, and mlx-whisper's own
    # per-window `no_speech_threshold` left at its default. That is closer to
    # faster-whisper's `vad_filter=False` than the previous mapping (which
    # disabled the threshold as a stand-in for a filter this runner did not
    # have) — the two engines now mean the same thing by the same flag.
    regions = _speech_regions(audio, total, job, row) if vad else None
    clips = _regions_to_decode(audio, regions, total)

    # PHASE THREE — transcribe each clip, ticked from here and watched from
    # inside, with every timestamp mapped back to original-recording time.
    segments, language = _transcribe_regions(
        audio, clips, fetched, task, language, initial_prompt,
        job, row, total, transcribing_since)

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
