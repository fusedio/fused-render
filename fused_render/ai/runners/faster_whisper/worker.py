"""Speech to text on faster-whisper: one resident model, four routes (SPEC §40).

The third runner, and the same shape as the other two: the HTTP contract, the
download reporting and the state machine are `worker_base`'s, and what lives
here is only what is true of Whisper in particular.

Three things make it different from both, and all three follow from the input
being a RECORDING rather than a prompt:

* **The input is a path, and this process opens it itself.** The worker runs on
  the same machine as the server, so there is nothing to upload and nothing to
  base64 — `body["path"]` is an absolute path the server already checked exists.
* **Progress is SECONDS OF AUDIO**, not tokens and not steps. That is the unit
  the user is thinking in ("it's got through 12 minutes of the 90"), and
  `info.duration` gives the total before the first segment is decoded.
* **The transcript is written by this process to paths the SERVER chose**
  (`body["out"]`, `body["outText"]`). The server owns where user files go; this
  process owns the words. It never invents a location, which is also why the
  `.txt` sibling is passed rather than derived from the `.json`.

**Cancelling works through the job row**, as it does for images — the reply to
the progress tick we were sending anyway is how the manager's ✕ reaches a
process that is otherwise looking at nothing else. It takes TWO mechanisms here,
because the run has two phases and only the second one looks like one:
`transcribe()` hands back a generator that decodes as it is consumed (so the
per-segment loop is a real interruption point), but before it returns it has
already decoded the whole file and run the VAD over it. That eager phase is
minutes on a long recording, so it is ticked from a thread — see
`_call_with_ticks`, which exists because the first cut left exactly that window
silent and uncancellable.

**Two model formats, one runner (D1423).** A snapshot with `model.bin` is a
CTranslate2 Whisper conversion and goes through `WhisperModel`, as it always
did. A snapshot with the sherpa-onnx transducer files (`formats.
SHERPA_TRANSDUCER_FILES`) is NVIDIA Parakeet TDT 0.6B v3, run through the
sherpa-onnx this folder already depends on for diarization — so there is no
third venv and no third runner. `load()` records which one it built in
`_loaded["kind"]` and `generate()` dispatches on it. The two are indistinguishable
to a page (AI-10c): same reply, same files, same `words` shape. Parakeet has no
built-in VAD and sherpa's offline transducer takes one utterance, so that path
cuts the recording into packs with the SAME detector and packing the MLX runner
uses (`stt_common`, `vad.py`) and decodes them one at a time.

**Nothing here shells out to ffmpeg.** faster-whisper decodes through PyAV,
whose wheels carry the ffmpeg libraries — see this folder's `pyproject.toml`.
A `subprocess` call to a binary the app does not ship would work on the machine
this was written on and fail on a user's.
"""

import json
import math
import os
import sys
import threading
import time

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diarize  # noqa: E402 - the SHARED speaker labelling; see runners/diarize.py
import formats  # noqa: E402 - the shared format checks; see formats.py
import partial  # noqa: E402 - the SHARED progressive transcript; see runners/partial.py
import stt_common  # noqa: E402 - the VAD policy both STT runners share
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded model. One per process.
_loaded = {}


# --------------------------------------------------------------- model loading


def download(model_id):
    """This runner's two closed file sets in one allow list: faster-whisper's own
    (`formats.CT2_FILES` — `model.bin` and the JSON configs) and the sherpa-onnx
    transducer's (`formats.SHERPA_TRANSDUCER_FILES`). An allow list; never an
    ignore of `*.bin`. A repo carries one format or the other, so the union
    fetches exactly what the format it is needs — and a Parakeet repo's
    `test_wavs/` stays out.

    **A Parakeet repo also pre-fetches the speech detector**, as the MLX runner's
    download does: its cuts depend on Silero, so a download that leaves it behind
    degrades the first offline run to fixed slices. Whisper does not need it —
    faster-whisper ships its own."""
    snapshot = worker_base.download_snapshot(
        model_id,
        allow_patterns=list(formats.CT2_FILES + formats.SHERPA_TRANSDUCER_FILES))
    if not os.path.isfile(os.path.join(snapshot, _CT2_WEIGHTS)):
        stt_common.prefetch_detector(worker_base)
    return snapshot


def _placement():
    """Device and compute type, the way the image runner's `_place()` picks one.

    CUDA when there is one, CPU otherwise — CTranslate2 has no Metal backend, so
    an Apple Silicon machine transcribes on its CPU cores and is still several
    times faster than real time.

    **`int8` on CPU is a real quality trade, and a deliberate one.** It is
    roughly twice as fast as float32 and a quarter of the memory, at a small
    cost in word error rate — which for a 90-minute recording is the difference
    between a wait and an afternoon. A caller who wants the accuracy back has
    the larger model as the lever, and that is the better one to reach for.

    **CUDA takes the same trade, not the opposite one — `int8_float16`, not
    plain `float16`.** This runner is not the only thing that can be resident
    on the GPU at once (see `torch_image._place`'s size-aware placement and
    `os_footprint_bytes()`), so a smaller footprint here is VRAM handed back to
    whatever else is loaded, for the same small WER cost CTranslate2's own
    quantization docs attribute to int8 generally. faster-whisper's own
    published benchmark (its README, large-v2 on an RTX 3070 Ti) has `int8` at
    2926MB against `float16`'s 4525MB — about a third less VRAM — while being
    slightly FASTER, not slower (59s vs 1:03). `int8_float16` over bare `int8`
    is what keeps that speed: it quantizes the weights but still computes the
    non-quantized layers in float16, rather than in whatever precision the
    checkpoint was converted at (float32, in practice) — CPUs have no native
    float16 compute to fall back to, which is why `int8` alone is CPU's
    version of the same choice. And it degrades safely on hardware that can't
    do it efficiently: CTranslate2 detects an unsupported combination itself
    and converts the weights to one the device supports, logging that it did,
    rather than failing to load.
    """
    import ctranslate2

    try:
        cuda = ctranslate2.get_cuda_device_count() > 0
    except (AttributeError, RuntimeError):
        cuda = False
    return ("cuda", "int8_float16") if cuda else ("cpu", "int8")


#: What a CTranslate2 conversion always has and a transformers checkpoint never
#: does. Checked by NAME rather than by catching the loader's error alone,
#: because that error is a bare "Unable to open file 'model.bin'" and the user
#: who reads it has no way to know that their repo was the wrong FORMAT rather
#: than a broken download.
#:
#: From `formats` rather than spelled here, because the AI Models page now
#: reads the same fact to decide which engine tag a cached repo gets — and a
#: card promising a load this function then refuses is the one way that tag can
#: be worse than no tag.
_CT2_WEIGHTS = formats.CT2_WEIGHTS


def _load_sherpa(fetched):
    """Parakeet TDT as a sherpa-onnx offline transducer, on the CPU.

    `model_type="nemo_transducer"` is not optional: the default `transducer`
    loads the same graphs and decodes garbage. **CPU only, and that is the
    design, not a gap** — the int8 graphs are the CPU build, ONNX Runtime's CPU
    provider is the one every platform this runner ships on has, and at several
    times faster than Whisper turbo on a CPU there is nothing to buy by adding a
    GPU provider that two of the three platforms lack. `num_threads` is the
    shared diarization finding (`diarize.NUM_THREADS`): threads past the
    performance cores make an ONNX model SLOWER on a P+E part, and this is the
    same ONNX Runtime underneath.

    `feature_dim=80` is sherpa's own default, spelled out. (Measured with the
    gate on the real export: the NeMo reader takes the mel size from the
    model's metadata, so the value does not change the result.)
    """
    import sherpa_onnx

    def at(name):
        return os.path.join(fetched, name)

    _loaded["model"] = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=at("encoder.int8.onnx"), decoder=at("decoder.int8.onnx"),
        joiner=at("joiner.int8.onnx"), tokens=at("tokens.txt"),
        num_threads=diarize.NUM_THREADS, sample_rate=SAMPLE_RATE, feature_dim=80,
        model_type="nemo_transducer", provider="cpu")
    _loaded["kind"] = "sherpa"
    _loaded["device"] = "cpu"
    worker_base.set_state(device="cpu")


def load(model_id, fetched):
    # The format check comes FIRST, before either import. A repo in the wrong
    # format is a fact about the download, not about this environment, and
    # ordering the import ahead of it would replace the explanation below with
    # whichever ImportError happened to come first.
    if not os.path.isfile(os.path.join(fetched, _CT2_WEIGHTS)):
        if formats.is_sherpa_transducer_snapshot(
                {name for name in formats.SHERPA_TRANSDUCER_FILES
                 if os.path.isfile(os.path.join(fetched, name))}):
            return _load_sherpa(fetched)
        # The same trap text generation has with GGUF and AWQ repos: the AI
        # Models page offers Load on anything whose task label maps to a
        # capability, and the format is not in the label. Naming the fix here is
        # the difference between a user changing one string and a web search.
        raise RuntimeError(
            f"{model_id} has no {_CT2_WEIGHTS} and is not a sherpa-onnx "
            "transducer export either — this looks like a transformers-format "
            "Whisper repo, and this runner loads CTranslate2 conversions "
            "(Whisper) and sherpa-onnx int8 exports (Parakeet). Try "
            # Named models a refusal points at, and they must be models the
            # CATALOG actually offers — a refusal naming a repo the page no
            # longer suggests sends the user to a Hub search, which is the whole
            # thing this string exists to prevent (the rule was first written
            # for `torch_text._TRY_INSTEAD`, removed with that runner) — update
            # these three if SUGGESTIONS["faster-whisper"] moves.
            "deepdml/faster-whisper-large-v3-turbo-ct2 or "
            "Systran/faster-whisper-small for Whisper, or "
            "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8 for Parakeet.")

    from faster_whisper import WhisperModel

    device, compute_type = _placement()
    # **No `cpu_threads`, and that is now measured rather than merely unexamined.**
    # This runner is the one place where CPU is the ONLY path (CTranslate2 has no
    # Metal backend), so it is where `diarize.NUM_THREADS`' finding — threads past
    # the performance cores make a CPU model SLOWER on a P+E part — was most
    # likely to repeat. It does not, because CT2's default already IS the value
    # that finding lands on: `cpu_threads=0` means "CT2's own default", CT2
    # documents `intra_threads` as "0 to use a default value" and faster-whisper
    # documents that value as 4 — and a default run sits at ~350-390% CPU on a
    # 10-core machine rather than at ~800%, which is the same thing observed from
    # outside.
    #
    # Measured on that machine: 216-second recording, `int8` on CPU exactly as
    # `_placement` picks it, one process per setting, best / median of 2-3 passes,
    # and every setting returned the same segment count.
    #
    #     model=tiny.en   default 21.1s    1 thr 19.5s   4 thr 18.1s   8 thr 61.3s
    #     model=small     default 94.4s    2 thr 71.9s   4 thr 86.8s   8 thr 146.6s
    #     (small medians)         102.2s          92.5s        88.6s         182.8s
    #
    # So an explicit 4 would restate the default, and every alternative at or
    # below it lands inside the run-to-run spread — while 8, which is what a naive
    # "use the machine" rule (`os.cpu_count()`) would reach for, is 3.4x slower on
    # tiny.en and 1.7x on small. **The suggestive 2-thread figure was deliberately
    # NOT landed**: one best-of-2 on a busy machine cannot separate it from noise
    # (its own median is slower than 4's), and a hardcoded 2 would be
    # host-dependent in the opposite direction from `NUM_THREADS` — wrong on a
    # 16-performance-core part in exactly the way `1` was wrong for diarization.
    # If this is ever revisited, it needs a quiet machine and several parts, not a
    # bigger number.
    _loaded["model"] = WhisperModel(fetched, device=device, compute_type=compute_type)
    _loaded["kind"] = "ct2"
    _loaded["device"] = device
    # See `worker_base.STATE["device"]`. CTranslate2 has no Metal backend, so an
    # Apple Silicon machine transcribes on its CPU cores — which is fine, and is
    # exactly the sort of thing a user should be able to read rather than deduce.
    worker_base.set_state(device=device)


def memory():
    """RSS, and that is honest here.

    Unlike MLX (lazy mmap'd arrays, AI-8a) and unlike diffusers (a GPU
    allocator's pool that RSS cannot see), CTranslate2 on CPU holds its weights
    in ordinary process memory. `worker_base` takes the larger of this and its
    own RSS reading, so there is nothing to correct for.
    """
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:  # noqa: BLE001 - a memory probe must never break /health
        return None


# --------------------------------------------------------------- transcription


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

    The hours field is not optional politeness: without it a 90-minute file
    reads "90:00" in this detail line while the manager renders the very same
    pair as "1:30:00" one line above (`jobAmount`), so one row states the same
    number two ways. Same rule as that formatter, for the same reason "720 /
    5400" was wrong there.

    **Rounds, because that formatter rounds.** Truncating disagreed with it on
    any fractional segment end — 89.6s read "1:30" in the manager and "1:29"
    one line below — and segment ends are fractional essentially always, so the
    mismatch was the common case rather than an edge one. Two formatters
    encoding one rule have to encode all of it.

    `floor(x + 0.5)` rather than `round`, because Python's `round` is BANKER'S
    rounding and JavaScript's `Math.round` is not: they disagree on an exact
    half — `round(88.5)` is 88, `Math.round(88.5)` is 89 — and segment ends
    arrive rounded to two decimals, so an exact half is reachable rather than
    theoretical. Reaching for `round` here would have fixed the common case and
    left the same bug behind on the boundary.
    """
    seconds = math.floor((seconds or 0) + 0.5)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


#: How often the eager phase ticks. Module-level so a test can shorten it.
_TICK_S = 1.0

#: The decode thread a cancel walked away from, if one is still running.
#:
#: A cancel unwinds the HANDLER, not the work: `worker_base._single` catches
#: `Cancelled`, replies, and leaves its `with GENERATE_LOCK` block while this
#: thread is still inside `model.transcribe()` holding the whole mel buffer. A
#: user who presses ✕ and immediately retries would then have two decodes
#: running on one process and one `WhisperModel` — the exact thing that lock
#: exists to prevent. The lock belongs to the base and is not ours to hold
#: longer, so the abandoned thread is remembered and waited for here instead.
#:
#: Annotated because the initialiser alone types the slot as None-only, and the
#: only thing ever assigned to it is a Thread.
_orphan: "dict[str, threading.Thread | None]" = {"thread": None}


#: How long a new transcription will wait for an abandoned decode to finish
#: before refusing instead.
#:
#: There has to be a deadline, and "the file it was reading" is NOT one. A wedge
#: inside PyAV or the VAD — a malformed container is exactly where a decoder
#: hangs — means `model.transcribe()` never returns, and an unbounded join then
#: blocks every later transcription on "Finishing a cancelled decode…" for as
#: long as the process lives, with unloading the model the only way out. A hang
#: with a spinner is the worst failure available: nothing says what is wrong and
#: nothing can be done from the page.
#:
#: Short rather than generous, because the ordinary case wants it short too:
#: cancelling five seconds into a 90-minute file leaves an eager decode of that
#: whole file running, and the NEXT recording must not sit through a decode of
#: the one the user already abandoned. So a caller waits briefly, and otherwise
#: gets an error naming the situation and what to do about it — which is
#: recoverable in a way a hang is not, and self-healing whenever the orphan
#: really does finish.
_ORPHAN_WAIT_S = 30.0


def _await_orphan(job, row):
    """Let a decode abandoned by an earlier cancel finish before starting another.

    Ticks while it waits, for the same reason the decode itself does: this is
    time the user is watching, and a row that says nothing during it goes stale.

    **Bounded by `_ORPHAN_WAIT_S`, not by the file.** An earlier version of this
    docstring claimed the file bounded it; that is only true of a decoder that
    returns, and the case worth defending against is the one that does not.
    Raises `RuntimeError` on the deadline rather than starting a second decode
    on the same model, which is the thing this wait exists to prevent.
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
            # The orphan is KEPT, deliberately: it may still finish, and the
            # next request will then sail through. Refusing is the honest answer
            # meanwhile — the alternative is either a second decode on one
            # `WhisperModel` or a wait with no end.
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


def _call_with_ticks(call, job, row, detail, late=None):
    """Run `call()` on a thread, ticking once a second until it returns.

    **`model.transcribe()` is not the lazy call it looks like.** It hands back a
    generator, which reads as "nothing has happened yet" — but before it
    returns, faster-whisper decodes the ENTIRE file through PyAV and, with the
    VAD on (this runner's default), runs silero over all of it. On a 90-minute
    recording that is tens of seconds to minutes, and the first cut spent them
    behind a single plain `report`: the row sat at 0 with only the heartbeat
    repeating it, and — the part that made this a bug rather than a cosmetic
    gap — a ✕ pressed in that window was not honoured until the first segment
    landed, because a plain `report` cannot carry a cancel back.

    Same shape as `worker_base.fetch_with_progress`, and for the same reason:
    the poll IS the progress and the cancellation point. A `Cancelled` raised
    here leaves the decode running on a daemon thread that nobody waits for —
    it finishes into a result that is discarded, which is the same trade the
    download path already makes and is bounded by the file.

    **`late` is the caller saying "a ✕ on THIS call is too late to honour".** The
    last Parakeet pack is the case: its decode is the only work left, so a ✕
    landing on it must not discard a transcript that is one call from finished
    (the rule `_consume_ct2` and MLX's `late_cancel` state). With a dict passed,
    a cancel is recorded as `late["late"] = True` and the call is WAITED for —
    silently, since every further tick would answer the same ✕ — then its value
    is returned. Bounded by one pack (`vad.BUDGET_S`) now that no pack is longer.
    A call that finished by raising keeps its error: there is nothing to salvage.
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
        # No `done`/`total`: nothing is known about the audio until `info`
        # arrives, and an invented percentage is what makes live work read as
        # frozen. The tick is here to be answered, not to move a bar.
        try:
            worker_base.report_or_cancel(job=job, **row, state="running",
                                         done=None, total=None, detail=detail)
            if not worker_base.CANCEL.is_set():
                continue
        except worker_base.Cancelled:
            if late is not None:
                late["late"] = True
                thread.join()
                break
            _orphan["thread"] = thread
            raise
        except BaseException:
            # Handing the thread over BEFORE unwinding, so the next request
            # waits for it. Doing this only on the way out of a cancel is what
            # keeps the normal path free of it.
            _orphan["thread"] = thread
            raise
        if late is not None:
            late["late"] = True
            thread.join()
            break
        _orphan["thread"] = thread
        raise worker_base.Cancelled()
    if "error" in result:
        raise result["error"]
    return result["value"]


# ----------------------------------------------------------------- diarization


#: What `diarize.py` and Whisper both work in. Not a preference — Whisper's mel
#: front end assumes it and the diarization models are 16 kHz exports.
#:
#: Only the FALLBACK, though: `_waveform` asks the loaded model instead, because
#: `transcribe()` re-decodes a path at `self.feature_extractor.sampling_rate`
#: and does NOT expose that as an argument. Handing it an array decoded at a
#: different rate would transcribe a chipmunk with no error anywhere.
SAMPLE_RATE = 16000


def _model_sample_rate(model):
    """The rate `transcribe()` would have decoded this file at itself."""
    rate = getattr(getattr(model, "feature_extractor", None), "sampling_rate", None)
    return int(rate) if rate else SAMPLE_RATE


def _waveform(source, model, job, row):
    """The recording as 16 kHz mono float32 — the samples diarization needs.

    **This runner normally never holds one, and that is the whole problem this
    function solves.** `model.transcribe(path)` hands faster-whisper a PATH and
    it decodes internally; the samples exist inside the library and are never
    ours. Diarization is not a whisper feature and cannot be reached through
    that call, so `diarize: true` needs a waveform this process can pass to
    sherpa-onnx.

    Three ways to get one were available and two are worse:

    * **Decode a second time with `av` directly.** It works — it is what
      `mlx_whisper/worker.py` does — but it would decode a 90-minute recording
      TWICE (once here, once inside faster-whisper) and it would name `av` as
      this folder's own dependency in order to reimplement a function
      faster-whisper already exports.
    * **Give this engine a different diarization** — a cheaper one, or none.
      Refused outright: `diarize: true` must mean the same thing on both
      engines the way `vad: true` does (AI-10c), and "the CT2 runner labels
      worse" is exactly the two-behaviours-one-argument failure the runner
      split is not allowed to produce.
    * **Use faster-whisper's OWN decoder and hand the array back to it.**
      `faster_whisper.audio.decode_audio` is the function `transcribe()` calls
      on a path, it produces exactly this array through PyAV, and `transcribe()`
      accepts an ndarray on the same argument. One decode, one library, no new
      dependency and no second implementation of "what 16 kHz mono means".

    So the array is decoded here and passed to `transcribe()` in place of the
    path — which also makes the diarized and undiarized paths differ by an
    argument rather than by a pipeline. **Only when diarizing**: an ordinary
    transcription still hands over the path, so nothing about the common case
    changes shape.

    Ticked, because it is not free: this is the same minutes-long decode of a
    long recording that `_call_with_ticks` exists for, and it is now happening
    where the row would otherwise sit silent.
    """
    from faster_whisper.audio import decode_audio

    rate = _model_sample_rate(model)
    return _call_with_ticks(
        lambda: decode_audio(source, sampling_rate=rate),
        job, row, "Decoding audio…")


def _speaker_turns(audio, sample_rate, speakers, job, row):
    """Who spoke when, over the WHOLE waveform — `[(start, end, index), …]`.

    The MLX runner's function of the same name, doing the same thing through
    the same shared module, and the two are meant to be readable side by side:
    the models, the labels and the join all live in `runners/diarize.py`, so
    what differs here is only this engine's ticking.

    **A failure FAILS the transcription**, unlike the VAD's degradation. The
    caller asked for speaker labels; an unlabelled transcript answers a
    different question while looking like success.

    **`done`/`total` stay None.** They are SECONDS OF AUDIO denominating the
    TRANSCRIPT (SPEC AI-10a), and a pre-pass that borrowed them would run the
    bar to 100% before a word was decoded. `detail` is the field the row
    already has for a stage, and an indeterminate bar under its own sentence is
    what the decode above already does.

    No `should_stop` predicate, unlike the MLX runner: this worker has no
    `_STOP` flag to offer — a cancel here abandons the thread and lets it
    finish, which is the same trade `_call_with_ticks` documents for the decode.
    """
    # Bound to THIS request's row — see the MLX runner's identical closure: the
    # fetch happens inside a transcription, so an unbound `download_file` would
    # tick into `JOB_ID`, the model's own finished load row.
    def fetch(repo_id, filename, detail=None):
        return worker_base.download_file(repo_id, filename, detail=detail,
                                         job=job, row=row)

    segmentation, embedding = diarize.model_paths(fetch)
    worker_base.report(job=job, **row, state="running", done=None, total=None,
                       detail="Finding speakers…")
    session = diarize.diarizer(segmentation, embedding, speakers)
    return _call_with_ticks(
        lambda: diarize.speaker_turns(audio, session, sample_rate),
        job, row, "Finding speakers…")


def _ct2_words(segment):
    """faster-whisper's `segment.words` in the shape both engines publish.

    The library's DTW alignment, mapped to `{start, end, word}` and nothing
    else: `probability` is an engine-tell (AI-10c) — a transducer has no such
    number, so publishing it would make the reply depend on which engine ran.
    `word` keeps the library's LEADING SPACE, which is how the text reconstructs
    by concatenation (the MLX runner and the Parakeet path keep it too). A
    segment the library returned no `words` for — it can, on a very short one —
    gets an empty list rather than a missing key, so a caller that asked for
    words reads "none were timed" rather than "this engine declined".
    """
    return [{"start": round(float(w.start), 2), "end": round(float(w.end), 2),
             "word": w.word}
            for w in (getattr(segment, "words", None) or [])]


def _consume_ct2(stream, progressive, job, row, total, transcribing_since, words):
    """The CTranslate2 decode loop: one tick per segment, the last one safe.

    Everything about cancelling lives in the comments below and moved here
    unchanged when the Parakeet path was added (D1423).
    """
    segments = []
    for segment in stream:
        segments.append({
            "start": round(float(segment.start), 2),
            "end": round(float(segment.end), 2),
            "text": segment.text.strip(),
        })
        if words:
            # ADDITIVE and only when asked, the rule `diarize` follows: a run
            # without the flag writes exactly the bytes it always did. Placed
            # after `text`, so an existing transcript's key order is untouched.
            segments[-1]["words"] = _ct2_words(segment)
        # Appended BEFORE the tick, so a page tailing the file already has
        # the segment by the time the row says the bar moved past it.
        progressive.add(segments[-1])
        done = segments[-1]["end"]
        elapsed = time.time() - transcribing_since
        try:
            # `report_or_cancel`, not `report`: this loop is the only place
            # a stop can be honoured, and the reply to this tick is how the
            # ✕ gets here.
            worker_base.report_or_cancel(
                job=job, **row, state="running", done=done, total=total,
                detail="Transcribing — %s of %s%s" % (
                    _clock(done), _clock(total) if total else "?",
                    _eta(total - done if total else None, elapsed, done)))
            if not worker_base.CANCEL.is_set():
                continue
            raise worker_base.Cancelled()
        except worker_base.Cancelled:
            # **A cancel is only worth honouring while there is work left to
            # stop.** This tick fires AFTER its segment is appended, so a ✕
            # landing on the last one used to raise straight past the write
            # below — an hour of decoding discarded at 99%, with the
            # transcript complete in memory and nothing on disk to show.
            #
            # Asking the generator for one more segment is what tells the
            # two apart, and it is only ever paid for on the cancel path. If
            # one comes back it is dropped, which costs nothing: we are
            # stopping. It does cost the DECODE of that segment, so a ✕ on a
            # long segment is honoured seconds later than it was pressed —
            # the price of not throwing away a finished transcript, and paid
            # only once.
            #
            # **A failure in the probe must not become the outcome.**
            # Decoding the tail of a file is exactly where a container or
            # codec error surfaces, and an exception raised here would
            # REPLACE the `Cancelled` in flight: `_single` would answer
            # `{"ok": false}` and the row would end in `error`, telling the
            # user the transcription they cancelled had failed. Unknown
            # means "assume there is more", which re-raises the cancel — the
            # honest answer to a ✕.
            try:
                more = next(stream, None) is not None
            except BaseException:  # noqa: BLE001 - the cancel is the outcome
                more = True
            if more:
                raise
            break

    return segments


# -------------------------------------------------------------------- Parakeet

#: Characters that end a sentence in the languages Parakeet v3 covers.
_SENTENCE_END = ".?!\u2026"
#: …and what may trail them without moving the end: a closing quote or bracket.
_CLOSERS = "\"'\u201d\u2019)]\u00bb"


def _sherpa_words(tokens, stamps):
    """Group sherpa's tokens into words: `[(start, text), …]`, clip time.

    A token that begins a word carries a LEADING SPACE — the SentencePiece `▁`
    boundary, which sherpa 1.13.8 hands back already decoded to a plain space
    (measured; `▁` is also accepted, in case an export or a later release does
    not decode it). A token without one — a word piece or a punctuation mark —
    continues the word before it, so "sk" joins " A" and "?" joins " what". The
    first token starts a word whatever it looks like.

    Each word's text keeps its leading space, like the other two engines'
    (`_ct2_words`).
    """
    words = []
    for token, at in zip(tokens, stamps):
        token = str(token).replace("\u2581", " ")
        if not words or token[:1] == " ":
            words.append([float(at), token if token[:1] == " " else " " + token])
        else:
            words[-1][1] += token
    return [(at, text) for at, text in words]


def _sherpa_segments(result, pack_seconds):
    """One pack's result as sentences: `[(start, end, text, words), …]` in CLIP
    time, `words` being `[(start, end, text), …]`.

    Parakeet v3 emits punctuation, and a sentence is what a transcript line
    should be, so segments split after a word ending in `. ? ! …` (a closing
    quote or bracket may trail it). The text is rebuilt from the tokens so it
    agrees with the words by construction.

    **Ends are the next thing's start**, as a transducer gives no ends: a word
    ends where the next word begins, a sentence where the next sentence begins,
    and the last of each where the pack does. The pack's own end is the honest
    bound — there is no later timestamp to prefer. All ends are clamped to
    `[start, pack_seconds]`, so the mapping back to the recording starts from
    monotonic input.

    **No usable timestamps is one segment over the whole pack**, with the text
    and no words: `tokens` and `timestamps` that disagree in length mean the
    export or the library changed under us, and a transcript with coarse times
    is a better answer than a failed run or a silently shifted one.
    """
    text = str(getattr(result, "text", "") or "").strip()
    tokens = list(getattr(result, "tokens", None) or [])
    stamps = list(getattr(result, "timestamps", None) or [])
    if not tokens or len(tokens) != len(stamps):
        return [(0.0, pack_seconds, text, [])] if text else []

    words = [(min(max(at, 0.0), pack_seconds), word)
             for at, word in _sherpa_words(tokens, stamps)]
    sentences, current = [], []
    for item in words:
        current.append(item)
        if item[1].rstrip().rstrip(_CLOSERS).endswith(tuple(_SENTENCE_END)):
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)

    out = []
    for index, sentence in enumerate(sentences):
        start = sentence[0][0]
        end = (sentences[index + 1][0][0] if index + 1 < len(sentences)
               else pack_seconds)
        end = max(end, start)
        spans = []
        for position, (at, word) in enumerate(sentence):
            until = (sentence[position + 1][0] if position + 1 < len(sentence)
                     else end)
            spans.append((at, max(until, at), word))
        out.append((start, end, "".join(w for _, _, w in spans).strip(), spans))
    return out


def _cut(start, end):
    """`[start, end)` as consecutive single-region packs of at most
    `vad.BUDGET_S` seconds. The first starts at `start` and the last ends at
    `end` exactly, so a caller's edges survive and only the inside is cut."""
    import vad as vad_module

    packs, at = [], start
    while at < end:
        stop = min(at + vad_module.BUDGET_S, end)
        packs.append([(at, stop)])
        at = stop
    return packs


def _time_slices(duration):
    """A recording as consecutive packs of at most `vad.BUDGET_S` seconds.

    What "no speech regions" becomes on this path. The MLX runner can hand a
    whole file to one call because Whisper windows internally; sherpa's offline
    transducer takes ONE utterance and its memory grows with it, so a two-hour
    recording must not arrive as one. The cut is at a fixed offset rather than a
    pause — which can split a word — and that is the cost of having no detector
    (`vad: false`, or one that could not be fetched); with the detector the cuts
    are at silence (`vad.pack_regions`).
    """
    return _cut(0.0, duration)


def _bounded_packs(packs):
    """`vad.pack_regions`' packs with no pack over the budget.

    `pack_regions` lets a region longer than the budget through ALONE, because
    Whisper windows internally and a cut chosen there would be worse than its
    own seeking. Sherpa has no such seeking and its memory grows with the
    utterance, so a minutes-long unbroken region (a lecture, a monologue) is cut
    here at fixed offsets from the region's own start. The region's edges are the
    detector's; only the inside is a blind cut, which can split a word — the same
    cost `_time_slices` documents, paid only where the detector found no pause.
    """
    import vad as vad_module

    out = []
    for pack in packs:
        if vad_module.packed_duration(pack) <= vad_module.BUDGET_S:
            out.append(pack)
            continue
        # Over-budget packs hold one region by construction; a general pack is
        # handled anyway by cutting each of its regions on its own.
        for start, end in pack:
            out.extend(_cut(start, end))
    return out


def _decode_pack(recognizer, clip):
    """One sherpa decode, on the thread `_call_with_ticks` gave it."""
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, clip)
    recognizer.decode_stream(stream)
    return stream.result


def _speech_regions(audio, total, job, row):
    """Where the speech is, or `None` — `stt_common.speech_regions` bound to this
    worker's base and loaded dict. A module function so a test can pin what the
    detector finds, as the MLX runner's tests do."""
    return stt_common.speech_regions(worker_base, _loaded, audio, total, job, row)


def _consume_sherpa(recognizer, audio, packs, progressive, job, row, total,
                    duration, words, transcribing_since):
    """Decode each pack and return `(segments)` in RECORDING time.

    One tick per pack, after its segments are in the partial file — the CT2
    loop's order, so a page tailing the file has a line before the bar moves
    past it. `done`/`total` are seconds of the RECORDING (AI-10a); the ETA's
    rate is seconds of SPEECH decoded per wall second, the MLX runner's rule
    and for its reason (a position in the recording is not a measure of work
    once silence is dropped).

    **Every endpoint travels `vad.py`'s inverse on its own**, a segment's start
    and end through `original_start`/`original_end` and a word through
    `original_word_span` (never stretched across a join), because the clip the
    recognizer heard had the silence removed. Words are then clamped into their
    segment and never dropped — the invariant a caller builds on is that a
    segment's words are its text, in order, complete.
    """
    import vad as vad_module

    segments = []
    speech_total = sum(vad_module.packed_duration(pack) for pack in packs)
    decoded = 0.0
    for index, pack in enumerate(packs):
        last = index == len(packs) - 1
        speech = vad_module.packed_duration(pack)
        clip = (audio if pack == [(0.0, duration)]
                else vad_module.packed_samples(audio, pack, SAMPLE_RATE))
        if len(clip) < SAMPLE_RATE // 10:
            # Under a tenth of a second: nothing to hear, and a few hundred
            # samples is where a decoder invents a word.
            continue
        # The last pack's decode is the only work left: a ✕ on it is late (see
        # `_call_with_ticks`), the rule the post-tick `except` below states too.
        result = _call_with_ticks(lambda: _decode_pack(recognizer, clip),
                                  job, row, "Transcribing…",
                                  late={} if last else None)
        decoded += speech
        for start, end, text, spans in _sherpa_segments(result, len(clip) / SAMPLE_RATE):
            at = vad_module.original_start(pack, start)
            until = max(vad_module.original_end(pack, end), at)
            line = {"start": round(at, 2), "end": round(until, 2), "text": text}
            if words and spans:
                line["words"] = []
                for w_start, w_end, word in spans:
                    w_at, w_until = vad_module.original_word_span(pack, w_start, w_end)
                    w_at = min(max(w_at, at), until)
                    w_until = min(max(w_until, w_at), until)
                    line["words"].append({"start": round(w_at, 2),
                                          "end": round(max(w_until, w_at), 2),
                                          "word": word})
            segments.append(line)
            progressive.add(line)
        done = round(min(pack[-1][1], duration), 2)
        elapsed = time.time() - transcribing_since
        try:
            worker_base.report_or_cancel(
                job=job, **row, state="running", done=done, total=total,
                detail="Transcribing — %s of %s%s" % (
                    _clock(done), _clock(total) if total else "?",
                    _eta(speech_total - decoded if speech_total else None,
                         elapsed, decoded)))
            if not worker_base.CANCEL.is_set():
                continue
            raise worker_base.Cancelled()
        except worker_base.Cancelled:
            # A cancel is only worth honouring while there is work left to stop:
            # on the LAST pack the transcript is finished, and discarding it
            # would be an hour of decoding thrown away at 99% (the CT2 loop's
            # rule, and the MLX runner's).
            if not last:
                raise
            break
    return segments


def generate(body):
    """Transcribe one file. Returns `{path, output, segments, language, …}`."""
    model = _loaded.get("model")
    if model is None:
        raise RuntimeError("no model is loaded")
    # A model loaded before `kind` existed, or by a test, is CTranslate2.
    sherpa = _loaded.get("kind") == "sherpa"

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
    if sherpa and task != "transcribe":
        # Refused HERE rather than in `engine_options.UNSUPPORTED`: that table is
        # per runner CODE, and this runner's code also serves Whisper, which does
        # translate. What cannot is this MODEL, so the refusal lives where the
        # model does — before a row is touched or a byte of audio is decoded.
        raise ValueError(
            "Parakeet transcribes only; 'translate' needs a Whisper model such "
            "as deepdml/faster-whisper-large-v3-turbo-ct2")
    # `words` (D392) is honoured on BOTH formats now: faster-whisper's own DTW
    # alignment on the CTranslate2 path (`word_timestamps=True`, mapped by
    # `_ct2_words`) and the transducer's token timestamps on the Parakeet one.
    # Only for `transcribe`: `translate` timings are the English text's against
    # the foreign audio, which a caller cannot use — the MLX runner's rule too.
    # Still the one option answered best-effort (see `engine_options`): a caller
    # reads on the segment whether it was honoured.
    words = bool(body.get("words")) and task == "transcribe"
    # None, not "": faster-whisper reads a falsy language as "detect it", and an
    # empty string would be passed through as a language code that matches none.
    language = str(body.get("language") or "") or None
    initial_prompt = str(body.get("initialPrompt") or "") or None
    if sherpa and (language or "").lower() == "auto":
        # The one spelling of "detect it" a caller can send: Parakeet always does.
        language = None
    if sherpa and language:
        # Refused for `translate`'s reason, and before a row is touched: Parakeet
        # v3 picks its own language among the 25 it was trained on, so pinning
        # one would be accepted and ignored — a page that asked for German and
        # got a guess has nothing on the reply to tell it.
        raise ValueError(
            "Parakeet detects its own language and cannot be pinned to one; "
            "'language' needs a Whisper model such as "
            "deepdml/faster-whisper-large-v3-turbo-ct2")
    if sherpa and initial_prompt:
        raise ValueError(
            "Parakeet is a transducer with no text to condition on, so "
            "'initialPrompt' is not supported; it needs a Whisper model such as "
            "deepdml/faster-whisper-large-v3-turbo-ct2")
    # `is None`, not `get("vad", True)`: a JSON null is "not specified", and a
    # default reached only by an absent KEY inverts for the caller that spreads
    # an options object carrying an unset one.
    vad = True if body.get("vad") is None else bool(body.get("vad"))
    # Defaults FALSE, so every existing caller's output is byte-identical: no
    # `speaker` on a segment, no `speakers` in the JSON, no 33MB download and
    # no separate decode.
    diarizing = bool(body.get("diarize"))
    # Validated HERE too, by the shared rule. The bridge and the server both
    # check it first, but neither is the only door into this process. None
    # means the caller did not say, and the clustering estimates it (D318).
    speakers = diarize.speakers_or_raise(body.get("speakers")) if diarizing else None
    job = body.get("job") or None
    # The row's IDENTITY, decided by the server and carried on every tick this
    # process sends. Not decoration: the job manager can evict any row under
    # capacity pressure and rebuild it from the next report, so a tick missing
    # `title` is refused outright (`upsert` will not create a row without one)
    # and one missing `cancellable`/`unit` rebuilds a row that looks operable
    # and is not. A transcription queue is exactly what pushes the row count
    # past the cap, so this is the designed usage, not an edge.
    row = body.get("row") or {}

    started = time.time()
    worker_base.report(job=job, **row, state="running",
                       done=0, total=None, detail="Decoding audio…")
    _await_orphan(job, row)
    # PHASE ZERO: the samples. Only when diarizing on the CTranslate2 path,
    # which otherwise never holds them (see `_waveform`) — `audio` then goes to
    # `transcribe()` in place of the path, so the file is decoded once whether or
    # not speakers were asked for. ALWAYS on the Parakeet path: sherpa takes
    # samples, never a path, and the VAD and the diarization read the same array.
    audio = _waveform(source, model, job, row) if (diarizing or sherpa) else None
    # …and who was speaking, over all of it, before a word is transcribed. A
    # fast pre-pass with its own `detail` line rather than a share of the
    # transcript's bar — see `_speaker_turns`.
    turns = (_speaker_turns(audio, _model_sample_rate(model), speakers, job, row)
             if diarizing else None)
    regions = None
    if sherpa:
        duration = round(len(audio) / SAMPLE_RATE, 2)
        total = duration or None
        # Independent of the diarization and after it, as on the MLX runner: the
        # segmenter finds its own silence and is better at it than a threshold
        # over Silero probabilities. Ticked from a thread, because Silero over a
        # long recording is minutes in which a ✕ must still land.
        if vad:
            regions = _call_with_ticks(
                lambda: _speech_regions(audio, total, job, row),
                job, row, "Finding speech…")
        # Branched on what the detector RETURNED, not on the shape of the packs:
        # a region that happens to span the whole file is its answer, and the
        # sink below is scoring speakers against exactly that list.
        packs = (_bounded_packs(stt_common.packs_to_decode(regions, duration))
                 if regions else _time_slices(duration))
        stream = None
        # Parakeet auto-detects and sherpa reports no language, so the reply
        # carries none rather than inventing a code a page would then trust (a
        # pinned `language` was refused above, so there is nothing to echo).
        detected = None
    else:
        # TWO phases, and only the second one is lazy. `transcribe` returns a
        # generator that decodes segment by segment as it is consumed — but it
        # decodes the whole file and runs the VAD before handing that generator
        # over, so this call blocks for minutes on a long recording. Ticking
        # through it is what keeps the ✕ live in that window (see `_call_with_ticks`).
        stream, info = _call_with_ticks(
            lambda: model.transcribe(
                source if audio is None else audio,
                task=task, language=language, initial_prompt=initial_prompt,
                vad_filter=vad, **({"word_timestamps": True} if words else {})),
            job, row, "Decoding audio…")
        # `info.duration` is the whole AUDIO; a segment's `end` is where SPEECH
        # ended. With the VAD on (the default) those differ by however much
        # silence the recording trails off with — so the bar legitimately stops
        # short of its total on a file that ends quietly, and that is not an
        # off-by-one to fix. Making them agree would mean either reporting
        # against speech-only duration, which is not knowable until the end, or
        # disabling the VAD, which costs the wall-clock saving it exists for.
        total = round(float(getattr(info, "duration", 0) or 0), 2) or None
        detected = getattr(info, "language", None)
    # The ETA's clock starts HERE, not at `started`. The eager phase above
    # produced no segments, so charging its seconds to the first one makes the
    # rate read as wildly slower than it is: a 60-second decode and a first
    # segment ending at 5s of audio says 12 wall-seconds per audio-second, and
    # a 90-minute file that will take ~18 minutes announces "~1079 min left".
    # `started` still measures the whole job, which is what `seconds` reports.
    transcribing_since = time.time()

    # Everything from here to the final write happens inside the sink, because
    # its EXIT is the lifecycle: reaching the end means the real output landed
    # and the partial file is duplicate bytes; a `Cancelled` means the user does
    # not want this transcript at all; anything else LEAVES the file, which is
    # the only salvage from a run that died halfway. See `runners/partial.py`.
    # `spans` (the detector's regions, when one ran) is the scoring mask the MLX
    # runner passes for the same reason: the sink labels each line as it lands,
    # and a live label scored over a dropped pause is a speaker the page would
    # have to un-render when the final file disagrees (D366).
    with partial.sink(out_partial, turns=turns, spans=regions or None,
                      cancelled=(worker_base.Cancelled,)) as progressive:
        if sherpa:
            segments = _consume_sherpa(model, audio, packs, progressive, job, row,
                                       total, duration, words, transcribing_since)
        else:
            segments = _consume_ct2(stream, progressive, job, row, total,
                                    transcribing_since, words)

        # The join, through the same shared function the MLX runner calls on the
        # same two lists — which is what makes "identical labels" structural
        # rather than a thing to keep testing (AI-10c).
        speaker_list = (diarize.assign_speakers(segments, turns,
                                                spans=regions or None)
                        if turns is not None else None)

        text = " ".join(s["text"] for s in segments).strip()
        result = {
            "path": source,
            "output": out,
            "outputText": out_text,
            "model": body.get("model") or "",
            "task": task,
            "language": detected,
            "duration": total,
            "seconds": round(time.time() - started, 2),
            "segments": segments,
        }
        if speaker_list is not None:
            # ADDITIVE, and only when asked: a run without `diarize` writes
            # exactly the bytes it always did, key for key. The list is the
            # transcript's legend — the labels that landed on a segment.
            result["speakers"] = speaker_list
            if speakers is None:
                # The MLX runner's key, by the same rule and with the same
                # meaning (D318): present only on a run that ESTIMATED the
                # count, because a run that was told it already knows. A page
                # must not be able to tell which engine served it (AI-10c), so
                # this one is written on both or on neither.
                result["estimatedSpeakers"] = diarize.speaker_count(turns)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({**result, "text": text}, handle, ensure_ascii=False, indent=1)
        with open(out_text, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    # The segments stay out of the REPLY: a 90-minute recording is thousands of
    # them, and the caller was handed the path to the file that holds them
    # before this ever started. The supervisor only needs to know it landed.
    return {**result, "segments": len(segments)}


if __name__ == "__main__":
    # No `release=` (see `worker_base._release`'s docstring): the CTranslate2
    # backend underneath faster-whisper exposes no cache-release call at all —
    # there is nothing here in the shape of `mx.clear_cache()`/`torch.*.
    # empty_cache()` to hand the idle timer to reach for.
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)
