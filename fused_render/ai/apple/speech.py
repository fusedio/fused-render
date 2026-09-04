"""`afm-speech`: one transcription through the Apple helper, run as a job.

The route (`routers/ai_runtime.py::api_ai_transcribe`) validates, decides the
output paths and opens the row; this module does the run on a thread and
writes EXACTLY the files the Whisper workers write — the same `.json` keys,
the same `.txt`, the same `.partial.jsonl` through `runners/partial.py` —
because `runtime.js`'s `done()` and `templates/shared/fused_ai.py` read those
files, not this module's reply. A page must not be able to tell which engine
ran (AI-10c).

Borrowed from the supervisor rather than reimplemented: the row payload
(`transcribe_row_fields`), the reporter (`_report`), the ✕ (`_cancel_
requested`), and the error wording (`_failure_text`). Not borrowed: the
worker table — see `host.py`'s docstring for why this tier is not a Runner.
"""

from __future__ import annotations

import json
import os
import threading
import time

from fused_render.ai import supervisor
from fused_render.ai.apple import host
from fused_render.ai.runners import partial

MODEL = "afm-speech"

#: The tier's own vocabulary for a wait the row can show. Same register as
#: `supervisor._QUEUED_DETAIL`.
_INSTALLING_DETAIL = "Downloading Apple's speech model for {locale}…"


def locale_for(language: str | None, availability: host.Availability) -> tuple[str | None, str | None]:
    """The BCP-47 locale a request's `language` means here, or a refusal.

    `language` on the transcribe verb is Whisper's vocabulary — an ISO 639
    code like `"en"`, or absent for auto-detect. SpeechTranscriber wants a
    locale at init and detects nothing, so: a full tag passes through when
    Apple supports it; a bare language picks the system region's variant
    when that exists, else an installed variant, else the first supported
    one; nothing at all means the system locale. Returns `(locale, None)` or
    `(None, why)`.
    """
    supported = list(availability.speech_locales)
    installed = set(availability.installed_locales)
    default = availability.default_locale or (supported[0] if supported else "en-US")
    if not language:
        # The system locale may itself be one Apple has no model for (en-IN
        # is; plenty are not). Fall back by language the same way a bare
        # code does rather than hand the helper a tag it will refuse.
        if default in supported or not supported:
            return default, None
        language = default.split("-")[0]
    wanted = language.replace("_", "-")
    lowered = {tag.lower(): tag for tag in supported}
    if wanted.lower() in lowered:
        return lowered[wanted.lower()], None
    if "-" in wanted:
        return None, (f"Apple's speech model has no {wanted!r} — supported: "
                      + ", ".join(sorted(supported)))
    lang = wanted.lower()
    candidates = [tag for tag in supported if tag.split("-")[0].lower() == lang]
    if not candidates:
        return None, (f"Apple's speech model has no {wanted!r} — supported: "
                      + ", ".join(sorted(supported)))
    region = default.split("-")[-1] if "-" in default else ""
    for tag in candidates:
        if region and tag.split("-")[-1].lower() == region.lower():
            return tag, None
    # No regional match: the conventional variant for the language before an
    # arbitrary installed one — Apple's `installedLocales` comes back in no
    # meaningful order (en-ZA led it here), and "en" landing on South African
    # English because it was first in a set is not a choice anyone made.
    conventional = {"en": "en-US", "es": "es-ES", "fr": "fr-FR", "de": "de-DE", "it": "it-IT",
                    "pt": "pt-BR", "zh": "zh-CN", "ja": "ja-JP", "ko": "ko-KR", "yue": "yue-CN"}
    preferred = conventional.get(lang)
    if preferred and preferred in candidates:
        return preferred, None
    for tag in sorted(candidates):
        if tag in installed:
            return tag, None
    return sorted(candidates)[0], None


def start(request: dict, job: str) -> None:
    """Open `job` and transcribe on a thread. Mirrors `supervisor.start_transcribe`."""
    title = (os.path.basename(str(request.get("path") or "")) or MODEL)[:80]
    fields = supervisor.transcribe_row_fields(title, MODEL)
    supervisor._report(job, **fields, state="running", done=None, total=None,
                       detail="Preparing…")

    def run() -> None:
        try:
            result = _run(request, job, fields)
        except BaseException as e:  # noqa: BLE001 - top of a thread; see supervisor._bring_up
            message = supervisor._failure_text(e) if not isinstance(e, host.AppleError) else str(e)
            if message == "cancelled":
                supervisor._report(job, **fields, state="cancelled")
            else:
                supervisor._report(job, **fields, state="error", message=message)
            return
        duration = result.get("duration")
        supervisor._report(job, **fields, state="done", done=duration, total=duration,
                           detail=f"Saved {os.path.basename(result.get('output') or 'transcript')}")

    threading.Thread(target=run, name="ai-apple-transcribe", daemon=True).start()


#: Containers AVFoundation opens on macOS 26. Anything else — WebM and Ogg
#: first among them, which is what Chrome's MediaRecorder produces — is
#: rewritten to WAV first (`_decoded_copy`). Lower-case, with the dot.
AVFOUNDATION_EXTENSIONS = frozenset({
    ".m4a", ".mp4", ".mov", ".aac", ".mp3", ".wav", ".wave", ".aif", ".aiff", ".aifc",
    ".caf", ".flac", ".m4v", ".3gp", ".amr", ".ac3", ".ec3", ".snd", ".au",
})
#: The one rate Whisper's front end and Apple's model both accept happily.
SAMPLE_RATE = 16000


def _decoded_copy(source: str, workdir: str) -> str:
    """`source` as a 16 kHz mono 16-bit WAV beside the transcript, for a
    container AVFoundation cannot open.

    PyAV in THIS process — the same decode `mlx_whisper/worker.py::_decode_audio`
    does inside its venv, with the same three rules (planar float, mono
    mixdown, resampler FLUSHED so the last frame is not lost), then written as
    int16 for the helper. No system ffmpeg is ever spawned (the app's policy —
    see the Whisper folders' pyproject.toml). A missing `av` is a build problem
    and is named as one.
    """
    try:
        import av
        import numpy as np
    except ImportError as e:  # pragma: no cover - a build without the [bundled] extra
        raise host.AppleError(
            "unavailable",
            f"{os.path.basename(source)} is not a container Apple's speech model reads "
            "(mp4/m4a/mov/wav/aiff/mp3/flac), and this build has no decoder to convert "
            f"it (PyAV missing: {e})") from e
    import wave

    chunks = []
    with av.open(source) as container:
        streams = container.streams.audio
        if not streams:
            raise host.AppleError("bad_request",
                                  f"{os.path.basename(source)} has no audio track to transcribe")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(streams[0]):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise host.AppleError("bad_request", f"{os.path.basename(source)} decoded to no audio")
    pcm = np.concatenate(chunks).astype(np.int16)
    target = os.path.join(workdir, os.path.splitext(os.path.basename(source))[0][:60] + ".decoded.wav")
    with wave.open(target, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return target


def _run(request: dict, job: str, fields: dict) -> dict:
    source = request["path"]
    out, out_text = request["out"], request["outText"]
    locale = request["locale"]
    words = bool(request.get("words"))
    started = time.time()
    segments: list[dict] = []
    duration = None
    reported_locale = locale

    def tick(**over) -> None:
        supervisor._report(job, **fields, **over)

    tick(state="running", done=0, total=None, detail="Decoding audio…")
    # What the helper is HANDED. The transcript's `path` stays the caller's
    # file — a decoded copy is a private intermediate, removed at the end.
    # Two doors into the decode: an extension AVFoundation is known not to
    # open goes straight there; a native-looking one that AVFoundation still
    # refuses ("Cannot Open" — a mislabelled file, an odd codec) is retried
    # through it ONCE rather than failing on a container PyAV reads fine.
    decoded: str | None = None
    try:
        if os.path.splitext(source)[1].lower() not in AVFOUNDATION_EXTENSIONS:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            decoded = _decoded_copy(source, os.path.dirname(out))
        try:
            return _run_helper(request, job, fields, decoded or source, started, tick)
        except host.AppleError as e:
            if decoded or e.type != "ai_error" or not _looks_undecodable(str(e)):
                raise
            tick(state="running", done=0, total=None, detail="Re-decoding audio…")
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            decoded = _decoded_copy(source, os.path.dirname(out))
            return _run_helper(request, job, fields, decoded, started, tick)
    finally:
        if decoded:
            try:
                os.remove(decoded)
            except OSError:
                pass


def _looks_undecodable(message: str) -> bool:
    """AVFoundation's own words for "I cannot read this file" — the only
    failures worth a second pass through PyAV."""
    lowered = message.lower()
    return any(marker in lowered for marker in
               ("cannot open", "no audio track", "could not be decoded", "cannot decode",
                "unsupported", "not supported"))


def _run_helper(request: dict, job: str, fields: dict, fed: str, started: float, tick) -> dict:
    """One pass through the helper over `fed`, writing the transcript files."""
    source, out, out_text = request["path"], request["out"], request["outText"]
    locale, words = request["locale"], bool(request.get("words"))
    segments: list[dict] = []
    duration = None
    reported_locale = locale
    with partial.sink(request.get("outPartial")) as progressive:
        # The first frame may be the locale's model downloading, so the bound
        # is the transcription ceiling, not text's; after it the child's own
        # exit is the bound, and this row is what a caller watches.
        stream = host.frames("speech", {"path": fed, "locale": locale, "words": words},
                             first_timeout=supervisor.TRANSCRIBE_TIMEOUT_S)
        for frame in stream:
            if supervisor._cancel_requested(job):
                # Closing the generator cancels the helper's request (its
                # `finally`); the partial file stays as the salvage, as it
                # does for a cancelled Whisper run.
                stream.close()
                raise supervisor.SupervisorError("cancelled")
            kind = frame.get("type")
            if kind == "assets":
                tick(state="running", done=None, total=None,
                     detail=_INSTALLING_DETAIL.format(locale=locale))
            elif kind == "segment":
                segment = {"start": frame.get("start"), "end": frame.get("end"),
                           "text": str(frame.get("text") or "").strip()}
                if frame.get("words") is not None:
                    segment["words"] = frame["words"]
                segments.append(segment)
                progressive.add(segment)
                tick(state="running", done=segment["end"], total=None, detail="Transcribing…")
            elif kind == "done":
                if frame.get("cancelled"):
                    raise supervisor.SupervisorError("cancelled")
                if not frame.get("ok"):
                    error = frame.get("error") or {}
                    raise host.AppleError(str(error.get("type") or "ai_error"),
                                          str(error.get("message") or "the transcription failed"))
                duration = frame.get("duration")
                reported_locale = frame.get("locale") or locale
        # Same discipline as the workers: the finished `.json` replaces the
        # partial, so a reader never sees both.
        text = " ".join(s["text"] for s in segments).strip()
        payload = {
            "path": source,
            "output": out,
            "outputText": out_text,
            "model": MODEL,
            "task": "transcribe",
            # ISO 639 like the Whisper workers write (`"en"`), so a page that
            # switches on `language` reads one vocabulary; the full locale
            # travels in `providerMetadata.apple.locale` via the route's reply.
            "language": reported_locale.split("-")[0] if reported_locale else None,
            "locale": reported_locale,
            "duration": duration,
            "seconds": round(time.time() - started, 2),
            "segments": segments,
        }
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({**payload, "text": text}, handle, ensure_ascii=False, indent=1)
        with open(out_text, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        # The sink's own `__exit__` discards the partial on a clean exit and
        # keeps it as salvage on an error or a cancel — the workers' rule.
    return {**payload, "segments": len(segments)}
