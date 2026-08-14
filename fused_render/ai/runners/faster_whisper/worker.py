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

**Cancelling works through the job row**, as it does for images: `transcribe()`
hands back a GENERATOR and the decoding happens as it is consumed, so the
per-segment loop is a real interruption point — and the reply to the progress
tick we were sending anyway is how the manager's ✕ reaches a process that is
otherwise looking at nothing else.

**Nothing here shells out to ffmpeg.** faster-whisper decodes through PyAV,
whose wheels carry the ffmpeg libraries — see this folder's `pyproject.toml`.
A `subprocess` call to a binary the app does not ship would work on the machine
this was written on and fail on a user's.
"""

import json
import os
import sys
import time

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
_CT2_WEIGHTS = "model.bin"


def load(model_id, fetched):
    from faster_whisper import WhisperModel

    device, compute_type = _placement()
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
    _loaded["model"] = WhisperModel(fetched, device=device, compute_type=compute_type)
    _loaded["device"] = device


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
    """m:ss — what a progress line about a recording should say."""
    seconds = int(seconds or 0)
    return "%d:%02d" % (seconds // 60, seconds % 60)


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
    vad = bool(body.get("vad", True))
    job = body.get("job") or None

    started = time.time()
    worker_base.report(job=job, state="running", kind="task", unit="s",
                       done=0, total=None, detail="Decoding audio…")
    # Lazy: `transcribe` returns a GENERATOR and does the work as it is
    # consumed, which is what makes the loop below both the progress and the
    # cancellation point. `info` is available immediately, so the total is known
    # before the first segment.
    stream, info = model.transcribe(
        source, task=task, language=language, initial_prompt=initial_prompt,
        vad_filter=vad)
    total = round(float(getattr(info, "duration", 0) or 0), 2) or None

    segments = []
    for segment in stream:
        segments.append({
            "start": round(float(segment.start), 2),
            "end": round(float(segment.end), 2),
            "text": segment.text.strip(),
        })
        done = segments[-1]["end"]
        elapsed = time.time() - started
        # `report_or_cancel`, not `report`: this loop is the only place a stop
        # can be honoured, and the reply to this tick is how the ✕ gets here.
        worker_base.report_or_cancel(
            job=job, kind="task", unit="s", done=done, total=total,
            detail="Transcribing — %s of %s%s" % (
                _clock(done), _clock(total) if total else "?",
                _eta(total - done if total else None, elapsed, done)))
        if worker_base.CANCEL.is_set():
            raise worker_base.Cancelled()

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
