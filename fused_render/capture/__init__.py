"""Native screen, microphone and still capture — the `fused.capture` bridge.

**Platform-neutral half.** This module owns everything that is not Apple: ids,
output paths, the session registry, the job row a recording appears as in the
download manager, and the watchdog thread that ticks it. The backend
(`_darwin.py`) owns the frames and the file, and is asked for four things —
`probe`, `start_screen`, `start_audio`, `screenshot` — so a second platform is a
second module, not a second design (the same seam
`fused_render/supervisor/_backend.py` uses).

**Why native at all**, when a page can already call `getDisplayMedia`: system
audio (impossible in a browser on macOS), no picker per recording, and — the one
that pays for the rest — the output is a FILE whose path is known before the
recording stops, so `fused.ai.transcribe({path})` is the next line rather than a
blob round-trip through JS. It also survives the page: a recording is a job row,
so navigating away does not end it.

**macOS only, and the floor is not the app's floor.** `SCRecordingOutput` is
macOS 15+, `SCScreenshotManager` 14+, against `LSMinimumSystemVersion` 11.0. So
"unavailable" has to carry a REASON in every case — wrong OS, too old an OS,
permission not granted — and `sources()` answers it without ever showing a
prompt. The prompt belongs to the first real capture, not to a page asking what
is possible (the same rule the GPU probe follows, SPEC §40).
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import time
import uuid

from fused_render import jobs

#: Job ids are server-owned (`jobs.OWNER_SERVER`): the work is this process's,
#: so the manager's ✕ can really stop it — and a page cannot forge a "done" for
#: a recording that is still running.
JOB_PREFIX = jobs.SERVER_ID_PREFIX + "capture:"

#: A recording nobody stops must still end. The page that started it can be
#: closed, and then the only shell-side control is the ✕, which DISCARDS — so
#: the cap is the one ending that keeps the file. Hitting it is a stop.
DEFAULT_MAX_SECONDS = 30 * 60
MAX_MAX_SECONDS = 4 * 60 * 60

#: How often the watchdog ticks the job row and re-reads `cancel_requested`.
TICK_S = 1.5

AUDIO_MODES = ("mic", "system", "both")


class CaptureError(ValueError):
    """A bad request — a typo, a mode that does not exist, an unusable rect."""


class Unsupported(RuntimeError):
    """This machine cannot capture, and the message says why."""


# --------------------------------------------------------------- the backend


def _backend():
    """The one live backend, or `Unsupported` naming what is missing.

    Dispatch on `sys.platform` like the supervisor's `_backend`: exactly one
    backend can ever be live in a process, so this is a module lookup and not an
    interface class standing in front of a single implementation.
    """
    if sys.platform != "darwin":
        raise Unsupported(
            "native capture is macOS-only today — this machine runs "
            f"{sys.platform}"
        )
    from fused_render.capture import _darwin

    return _darwin


def sources() -> dict:
    """What this machine can capture, and what it is waiting for. Never prompts.

    One method rather than a `capabilities()` beside a `devices()`: the answer to
    "can I" and the answer to "of what" are read together by every caller, and a
    payload that carries both cannot describe a machine whose permission and
    device list disagree. `available` is about the OS and the build; `granted` is
    about TCC and moves without this process restarting, which is why they are
    separate booleans rather than one.
    """
    try:
        backend = _backend()
    except Unsupported as e:
        reason = str(e)
        return {
            "video": {"available": False, "granted": False, "reason": reason},
            "audio": {"available": False, "granted": False, "reason": reason},
            "systemAudio": {"available": False, "reason": reason},
            # Shape-identical to the real probe, every key included: a page that
            # reads `sources().screenshot.available` must not throw on the
            # platform where the answer is "no".
            "screenshot": {"available": False, "granted": False,
                           "reason": reason},
            "displays": [],
            "microphones": [],
        }
    return backend.probe()


# --------------------------------------------------------------- the registry


class _Session:
    """One live capture: the backend's handle, its job row, its watchdog."""

    def __init__(self, cid: str, mode: str, path: str, handle, spec: dict):
        self.id = cid
        self.mode = mode
        self.path = path
        self.handle = handle
        self.spec = spec
        self.started_at = time.time()
        self.max_seconds = spec["maxSeconds"]
        self.state = "recording"

    @property
    def job(self) -> str:
        return JOB_PREFIX + self.id

    def public(self) -> dict:
        return {
            "id": self.id,
            "mode": self.mode,
            "path": self.path,
            "state": self.state,
            "seconds": round(max(0.0, time.time() - self.started_at), 2),
            "maxSeconds": self.max_seconds,
            "jobId": self.job,
            "audio": self.spec.get("audio") or False,
        }


_lock = threading.Lock()
_sessions: dict[str, _Session] = {}


def active() -> list[dict]:
    """Every live recording on this machine — the read side of `list()`.

    Live only: a finished recording is a file, and the page that wants it has
    the path from `stop()` (or from the job row's detail). Keeping the corpses
    here would be a second, worse copy of the download manager.
    """
    with _lock:
        return [s.public() for s in _sessions.values() if s.state == "recording"]


# ------------------------------------------------------------------ starting


def _out_dir() -> str:
    """Where recordings land: `<home>/recordings`.

    Beside `<home>/ai/transcripts` (`ai_runtime._transcripts_dir`) and for the
    same reason — the file outlives the tab that asked for it, so it cannot live
    anywhere tied to a page.
    """
    from fused_render.shell.storage import home_dir

    directory = os.path.join(home_dir(), "recordings")
    os.makedirs(directory, exist_ok=True)
    return directory


def _resolve_out(path, base, default_ext: str) -> str:
    """The absolute file to write, from an optional caller `path`.

    A relative `path` resolves beside the CALLING PAGE (`base`), the rule
    `readFile`/`rawUrl`/`transcribe` already follow (RH-1) — "clip.mov" must not
    silently mean "beside wherever the server was launched from". No `path` at
    all lands in `_out_dir()` under a timestamped name.
    """
    if path is None or path == "":
        name = time.strftime("%Y-%m-%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        return os.path.join(_out_dir(), name + default_ext)
    if not isinstance(path, str) or not path.strip():
        raise CaptureError("'path' must be a non-empty string when given")
    out = os.path.expanduser(path.strip())
    if not os.path.isabs(out):
        if not isinstance(base, str) or not os.path.isabs(base):
            raise CaptureError(
                "'path' must be absolute, or relative to a page named by 'base'")
        out = os.path.join(os.path.dirname(base), out)
    out = os.path.abspath(out)
    if os.path.isdir(out):
        raise CaptureError(f"'path' is a directory: {out}")
    parent = os.path.dirname(out)
    if not os.path.isdir(parent):
        raise CaptureError(f"no such directory: {parent}")
    return out


def _max_seconds(value) -> int:
    if value is None or value == "":
        return DEFAULT_MAX_SECONDS
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise CaptureError("'maxSeconds' must be a whole number of seconds")
    if isinstance(value, bool) or seconds <= 0 or seconds > MAX_MAX_SECONDS:
        raise CaptureError(
            f"'maxSeconds' must be between 1 and {MAX_MAX_SECONDS}")
    return seconds


def _audio_mode(value, *, required: bool) -> str | None:
    """`audio` on a screen recording: false/None, or one of AUDIO_MODES.

    Named values rather than a pair of booleans, and refused rather than
    coerced: "microphone" instead of "mic" would otherwise record silence and
    read as the app ignoring the request (the same posture AI-10 takes on
    `task`).
    """
    if value is None or value is False or value == "":
        if required:
            raise CaptureError("'source' must be " + _or_list(AUDIO_MODES))
        return None
    if value is True:
        return "mic"
    if value not in AUDIO_MODES:
        raise CaptureError(
            f"'audio' must be false or {_or_list(AUDIO_MODES)}, not {value!r}")
    return value


def _or_list(values) -> str:
    return ", ".join(repr(v) for v in values[:-1]) + f" or {values[-1]!r}"


def _rect(value):
    if value is None or value == "":
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(n, bool) or not isinstance(n, (int, float))
                   for n in value)):
        raise CaptureError("'rect' must be [x, y, width, height] in points")
    x, y, w, h = (float(n) for n in value)
    if w <= 0 or h <= 0:
        raise CaptureError("'rect' width and height must be positive")
    return (x, y, w, h)


def start(mode: str, body: dict) -> dict:
    """Begin a recording. Returns the record — path included — immediately.

    The path is decided HERE, before a single frame exists, which is what lets a
    caller wire up an `<audio>`/`<video>` (or queue a transcription) without a
    second lookup, and what lets a page that navigated away still find the file.
    Same shape as `/api/ai/transcribe`'s reply, for the same reason.
    """
    if mode not in ("screen", "audio"):
        raise CaptureError(f"mode must be 'screen' or 'audio', not {mode!r}")
    backend = _backend()

    spec = {"maxSeconds": _max_seconds(body.get("maxSeconds"))}
    if mode == "screen":
        spec["audio"] = _audio_mode(body.get("audio"), required=False)
        spec["display"] = body.get("display")
        spec["rect"] = _rect(body.get("rect"))
        spec["cursor"] = bool(body.get("cursor", True))
        spec["device"] = body.get("device")
        out = _resolve_out(body.get("path"), body.get("base"), ".mov")
    else:
        spec["audio"] = _audio_mode(body.get("source", "mic"), required=True)
        if spec["audio"] != "mic":
            raise CaptureError(
                "fused.capture.audio records the microphone; for system audio "
                "record the screen with audio: 'system'")
        # REFUSED, not ignored (the AI-10/D319 posture). Audio-only records
        # through `AVAudioRecorder`, which has no device selection — the API
        # that did deadlocked on a run loop this app cannot provide (see
        # `_darwin.start_audio`). A silently-wrong microphone is a recording the
        # user has to make twice, so the option says where it does work.
        if body.get("device"):
            raise CaptureError(
                "audio-only recording uses the system's current input device, "
                "so 'device' cannot be chosen here — record the screen with "
                "audio: 'mic' to pick a specific microphone, or change the "
                "input in System Settings › Sound "
                "(sources().microphones tells you which is current)")
        spec["device"] = None
        out = _resolve_out(body.get("path"), body.get("base"), ".m4a")

    cid = uuid.uuid4().hex[:12]
    handle = (backend.start_screen(out, spec) if mode == "screen"
              else backend.start_audio(out, spec))
    session = _Session(cid, mode, out, handle, spec)
    with _lock:
        _sessions[cid] = session

    title = body.get("title") or (
        "Screen recording" if mode == "screen" else "Audio recording")
    _report(session, state=jobs.RUNNING, title=str(title)[:120],
            kind="task", unit="s", done=0, total=spec["maxSeconds"],
            cancellable=True,
            detail="Recording — ✕ discards it")
    threading.Thread(target=_watch, args=(session,), daemon=True,
                     name=f"capture-{cid}").start()
    return session.public()


def _report(session: _Session, **fields) -> None:
    """One job tick, best-effort. Reporting must never break the recording."""
    try:
        jobs.upsert({"id": session.job, **fields}, server=True)
    except (jobs.JobError, ValueError):
        pass


def _cancel_requested(session: _Session) -> bool:
    for record in jobs.list_jobs():
        if record["id"] == session.job:
            return bool(record.get("cancel_requested"))
    return False


def _watch(session: _Session) -> None:
    """Tick the row, and enforce the two endings the page never asked for.

    ✕ DISCARDS (the house meaning of cancel, and consistent with every other
    row); the cap STOPS AND KEEPS, because for a recording whose page is gone
    the cap is the only ending that does not destroy the content.
    """
    while True:
        time.sleep(TICK_S)
        with _lock:
            if _sessions.get(session.id) is not session:
                return          # stopped by its owner; that path reports.
        elapsed = time.time() - session.started_at
        if _cancel_requested(session):
            try:
                stop(session.id, discard=True)
            except (CaptureError, Unsupported):
                pass
            return
        if elapsed >= session.max_seconds:
            try:
                stop(session.id)
            except (CaptureError, Unsupported):
                pass
            return
        _report(session, done=round(min(elapsed, session.max_seconds), 1))


# ------------------------------------------------------------------ stopping


def stop(cid: str, *, discard: bool = False) -> dict:
    """End a recording. `discard=True` deletes the file — that is cancel.

    Idempotent-ish by construction: the session is removed from the registry
    under the lock BEFORE the backend is touched, so a ✕ landing at the same
    moment as the page's own `stop()` cannot finalise the same file twice.
    """
    with _lock:
        session = _sessions.pop(cid, None)
    if session is None:
        raise CaptureError(f"no such capture: {cid}")

    error = ""
    try:
        _backend().stop(session.handle)
    except Exception as e:                      # noqa: BLE001 - reported, not raised
        error = f"{e.__class__.__name__}: {e}".strip().rstrip(":")

    if discard:
        try:
            os.remove(session.path)
        except OSError:
            pass
        session.state = "cancelled"
        _report(session, state="cancelled")
        result = session.public()
        result["path"] = None
        result["url"] = None
        return result

    session.state = "error" if error else "stopped"
    result = session.public()
    result.update(_describe(session.path))
    if error:
        _report(session, state="error", message=error)
        result["error"] = error
    else:
        _report(session, state="done",
                detail=os.path.basename(session.path))
    return result


def _describe(path: str) -> dict:
    """The file, as a page needs it: where it is, how to fetch it, how big.

    `url` is the ready-made `/api/fs/raw` address rather than something the
    caller assembles, the same courtesy `fused.ai.image` pays (D-AI-9).
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    ext = os.path.splitext(path)[1].lower()
    mime = {".mov": "video/quicktime", ".mp4": "video/mp4",
            ".m4a": "audio/mp4", ".png": "image/png",
            ".jpg": "image/jpeg"}.get(ext, "application/octet-stream")
    return {"path": path, "url": "/api/fs/raw?path=" + _quote(path),
            "bytes": size, "mime": mime}


def _quote(path: str) -> str:
    from urllib.parse import quote

    return quote(path, safe="")


# --------------------------------------------------------------- the still


def screenshot(body: dict) -> dict:
    """One frame, now. No handle and no job row — it is milliseconds.

    It lives in this namespace rather than at the top level because it shares
    everything that is hard: the TCC grant, the display list, the rect, the
    output-path rule. A root-level `fused.screenshot()` would be a second door
    onto one permission model.
    """
    backend = _backend()
    fmt = body.get("format") or "png"
    if fmt not in ("png", "jpg"):
        raise CaptureError("'format' must be 'png' or 'jpg'")
    spec = {
        "display": body.get("display"),
        "rect": _rect(body.get("rect")),
        "cursor": bool(body.get("cursor", False)),
        "format": fmt,
    }
    out = _resolve_out(body.get("path"), body.get("base"), "." + fmt)
    shot = backend.screenshot(out, spec)
    result = _describe(out)
    result.update(shot)
    return result


# ------------------------------------------------------------------ teardown


def stop_all() -> None:
    """Finalise every live recording — a truncated .mov has no moov atom.

    Registered with `atexit` because the file is the whole product: a server
    that exits mid-recording without this leaves an unplayable file where the
    user has a job row saying they recorded something.
    """
    for cid in list(_sessions):
        try:
            stop(cid)
        except Exception:                        # noqa: BLE001 - exiting anyway
            pass


atexit.register(stop_all)
