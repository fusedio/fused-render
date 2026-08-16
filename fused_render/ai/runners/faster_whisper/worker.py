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
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded model. One per process.
_loaded = {}


# --------------------------------------------------------------- model loading


def download(model_id):
    """A Whisper repo is an ordinary multi-file snapshot — nothing clever."""
    return worker_base.download_snapshot(model_id)


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
    """
    import ctranslate2

    try:
        cuda = ctranslate2.get_cuda_device_count() > 0
    except (AttributeError, RuntimeError):
        cuda = False
    return ("cuda", "float16") if cuda else ("cpu", "int8")


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


def load(model_id, fetched):
    # The format check comes FIRST, before either import. A repo in the wrong
    # format is a fact about the download, not about this environment, and
    # ordering the import ahead of it would replace the explanation below with
    # whichever ImportError happened to come first.
    if not os.path.isfile(os.path.join(fetched, _CT2_WEIGHTS)):
        # The same trap text generation has with GGUF and AWQ repos: the AI
        # Models page offers Load on anything whose task label maps to a
        # capability, and the format is not in the label. Naming the fix here is
        # the difference between a user changing one string and a web search.
        raise RuntimeError(
            f"{model_id} has no {_CT2_WEIGHTS} — this looks like a "
            "transformers-format Whisper repo, and this runner loads "
            "CTranslate2 conversions. Try Systran/faster-whisper-large-v3 or "
            "deepdml/faster-whisper-large-v3-turbo-ct2.")

    from faster_whisper import WhisperModel

    device, compute_type = _placement()
    _loaded["model"] = WhisperModel(fetched, device=device, compute_type=compute_type)
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


def _call_with_ticks(call, job, row, detail):
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
        except BaseException:
            # Handing the thread over BEFORE unwinding, so the next request
            # waits for it. Doing this only on the way out of a cancel is what
            # keeps the normal path free of it.
            _orphan["thread"] = thread
            raise
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
    segmentation, embedding = diarize.model_paths(worker_base.download_file)
    worker_base.report(job=job, **row, state="running", done=None, total=None,
                       detail="Finding speakers…")
    session = diarize.diarizer(segmentation, embedding, speakers)
    return _call_with_ticks(
        lambda: diarize.speaker_turns(audio, session, sample_rate),
        job, row, "Finding speakers…")


def generate(body):
    """Transcribe one file. Returns `{path, output, segments, language, …}`."""
    model = _loaded.get("model")
    if model is None:
        raise RuntimeError("no model is loaded")

    source = str(body.get("path") or "")
    out = str(body.get("out") or "")
    out_text = str(body.get("outText") or "")
    if not source:
        raise ValueError("'path' must be the audio file to transcribe")
    if not out or not out_text:
        raise ValueError("'out' and 'outText' must be where to write the transcript")

    task = str(body.get("task") or "transcribe")
    # None, not "": faster-whisper reads a falsy language as "detect it", and an
    # empty string would be passed through as a language code that matches none.
    language = str(body.get("language") or "") or None
    initial_prompt = str(body.get("initialPrompt") or "") or None
    # `is None`, not `get("vad", True)`: a JSON null is "not specified", and a
    # default reached only by an absent KEY inverts for the caller that spreads
    # an options object carrying an unset one.
    vad = True if body.get("vad") is None else bool(body.get("vad"))
    # Defaults FALSE, so every existing caller's output is byte-identical: no
    # `speaker` on a segment, no `speakers` in the JSON, no 33MB download and
    # no separate decode.
    diarizing = bool(body.get("diarize"))
    # Validated HERE too, by the shared rule. The bridge and the server both
    # check it first, but neither is the only door into this process.
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
    # PHASE ZERO, and only when diarizing: the samples, which this runner
    # otherwise never holds (see `_waveform`). `audio` then goes to
    # `transcribe()` in place of the path, so the file is decoded once whether
    # or not speakers were asked for.
    audio = _waveform(source, model, job, row) if diarizing else None
    # …and who was speaking, over all of it, before a word is transcribed. A
    # fast pre-pass with its own `detail` line rather than a share of the
    # transcript's bar — see `_speaker_turns`.
    turns = (_speaker_turns(audio, _model_sample_rate(model), speakers, job, row)
             if diarizing else None)
    # TWO phases, and only the second one is lazy. `transcribe` returns a
    # generator that decodes segment by segment as it is consumed — but it
    # decodes the whole file and runs the VAD before handing that generator
    # over, so this call blocks for minutes on a long recording. Ticking
    # through it is what keeps the ✕ live in that window (see `_call_with_ticks`).
    stream, info = _call_with_ticks(
        lambda: model.transcribe(
            source if audio is None else audio,
            task=task, language=language, initial_prompt=initial_prompt,
            vad_filter=vad),
        job, row, "Decoding audio…")
    # `info.duration` is the whole AUDIO; a segment's `end` is where SPEECH
    # ended. With the VAD on (the default) those differ by however much silence
    # the recording trails off with — so the bar legitimately stops short of its
    # total on a file that ends quietly, and that is not an off-by-one to fix.
    # Making them agree would mean either reporting against speech-only
    # duration, which is not knowable until the end, or disabling the VAD, which
    # costs the wall-clock saving it exists for.
    total = round(float(getattr(info, "duration", 0) or 0), 2) or None
    # The ETA's clock starts HERE, not at `started`. The eager phase above
    # produced no segments, so charging its seconds to the first one makes the
    # rate read as wildly slower than it is: a 60-second decode and a first
    # segment ending at 5s of audio says 12 wall-seconds per audio-second, and
    # a 90-minute file that will take ~18 minutes announces "~1079 min left".
    # `started` still measures the whole job, which is what `seconds` reports.
    transcribing_since = time.time()

    segments = []
    for segment in stream:
        segments.append({
            "start": round(float(segment.start), 2),
            "end": round(float(segment.end), 2),
            "text": segment.text.strip(),
        })
        done = segments[-1]["end"]
        elapsed = time.time() - transcribing_since
        try:
            # `report_or_cancel`, not `report`: this loop is the only place a
            # stop can be honoured, and the reply to this tick is how the ✕
            # gets here.
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
            # below — an hour of decoding discarded at 99%, with the transcript
            # complete in memory and nothing on disk to show for it.
            #
            # Asking the generator for one more segment is what tells the two
            # apart, and it is only ever paid for on the cancel path. If one
            # comes back it is dropped, which costs nothing: we are stopping.
            # It does cost the DECODE of that segment, so a ✕ on a long segment
            # is honoured seconds later than it was pressed — the price of not
            # throwing away a finished transcript, and paid only once.
            #
            # **A failure in the probe must not become the outcome.** Decoding
            # the tail of a file is exactly where a container or codec error
            # surfaces, and an exception raised here would REPLACE the
            # `Cancelled` in flight: `_single` would answer `{"ok": false}` and
            # the row would end in `error`, telling the user the transcription
            # they cancelled had failed. Unknown means "assume there is more",
            # which re-raises the cancel — the honest answer to a ✕.
            try:
                more = next(stream, None) is not None
            except BaseException:  # noqa: BLE001 - the cancel is the outcome, not this
                more = True
            if more:
                raise
            break

    # The join, through the same shared function the MLX runner calls on the
    # same two lists — which is what makes "identical labels" structural rather
    # than a thing to keep testing (AI-10c).
    speaker_list = diarize.assign_speakers(segments, turns) if turns is not None else None

    text = " ".join(s["text"] for s in segments).strip()
    result = {
        "path": source,
        "output": out,
        "outputText": out_text,
        "model": body.get("model") or "",
        "task": task,
        "language": getattr(info, "language", None),
        "duration": total,
        "seconds": round(time.time() - started, 2),
        "segments": segments,
    }
    if speaker_list is not None:
        # ADDITIVE, and only when asked: a run without `diarize` writes exactly
        # the bytes it always did, key for key. The list is the transcript's
        # legend — the labels that actually landed on a segment.
        result["speakers"] = speaker_list
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
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)
