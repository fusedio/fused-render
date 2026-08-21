"""Recording fed by the page's own encoder — the Windows and Linux path.

**Not a platform backend on its own.** `_windows.py` and `_linux.py` take the
STILL natively and re-export everything here for the two RECORDING modes, so
there is one implementation of this for both of them.

**Why the page encodes.** On Windows no OS API a non-packaged process can reach
will write a movie with system audio in it: `AppRecordingManager` needs MSIX
identity, `Windows.Graphics.Capture` hands out D3D surfaces and no muxer, Media
Foundation has no screen source, and ffmpeg has no WASAPI input device at all
(its Windows inputs are dshow/gdigrab/vfwcap, and dshow cannot do loopback
without a third-party driver). Chromium already does what a native recorder
would — WGC plus WASAPI loopback on Windows, the PipeWire portal on Linux,
hardware-encoded — so the page runs `MediaRecorder` and this module receives the
chunks it produces and appends them to the file. macOS stays native because
there the browser cannot capture system audio at all, which is the whole
capability (`_darwin.py`).

**What arrives here is already encoded.** One `write()` per timeslice is an
append of an opaque blob — no decode, no re-mux, no frame ever built in Python.
Both containers `MediaRecorder` produces (fragmented mp4, WebM) are playable AS
WRITTEN, which is strictly better than the macOS path: there is no `moov` atom
to lose, so a recording cut short by a page reload or a crash is a valid shorter
file rather than an unplayable one.

**The socket, not the process, owns the recording's life.** A page that reloads
takes its `MediaRecorder` with it, so `detach()` ends the session and KEEPS what
arrived. That is the one behaviour these platforms cannot match on macOS, and
`list()` telling the truth about it — no row saying "recording" over a file
nothing is writing — matters more than pretending otherwise.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time

logger = logging.getLogger(__name__)

#: Containers `MediaRecorder` can produce, as the page names them in `container`.
#: mp4 here is FRAGMENTED mp4 (Chrome 126+ with `codecs=avc1`); webm is the
#: universally supported one and so the default when a caller says nothing.
CONTAINERS = {"webm": ".webm", "mp4": ".mp4"}
DEFAULT_CONTAINER = "webm"

#: A recording whose page never opens its socket is a job row over an empty
#: file. Generous — the share picker is a human decision — but finite.
ATTACH_S = 60.0

#: Chunks stop arriving while the socket stays open: a frozen or backgrounded
#: renderer. Long enough that a 1 s timeslice missing a few is not a failure.
STALL_S = 45.0

#: A ceiling no `maxSeconds` can talk past, because the bytes are the page's to
#: choose: 4 h of 4K at a browser's default bitrate is far under this, and a
#: page in a loop should hit a refusal rather than fill the disk.
MAX_BYTES = 8 * 1024 ** 3


def ext(mode: str, spec: dict) -> str:
    """The extension for what the PAGE is about to produce.

    The container cannot be this module's choice: `MediaRecorder` decides what it
    can encode, and the path has to be decided before the first frame (CP-2), so
    the page states its container in the start body and this maps it. An unknown
    one is refused rather than guessed — a `.webm` holding mp4 bytes is a file
    every other tool misreads, which is the same reason a screenshot has no
    `format` option.
    """
    name = spec.get("container") or DEFAULT_CONTAINER
    if name not in CONTAINERS:
        from fused_render.capture import CaptureError

        raise CaptureError(
            f"'container' must be {' or '.join(repr(k) for k in CONTAINERS)}, "
            f"not {name!r}")
    if mode == "audio" and name == "mp4":
        return ".m4a"
    return CONTAINERS[name]


def refuse(mode: str, spec: dict) -> str | None:
    """What the browser's share picker owns, and what the container forbids.

    Three options are refused rather than ignored (the AI-10/D319 posture):
    `display`, `rect` and `cursor` all describe a capture region, and on this
    path the user chooses the region in the browser's own picker after this call
    returns. Silently ignoring `display: 2` would hand back a recording of the
    wrong monitor with nothing in the reply to suggest why.
    """
    if mode == "screenshot":                                # native, not here
        return None
    if mode == "screen":
        if spec.get("display") not in (None, ""):
            return ("'display' cannot be chosen here — on this platform the "
                    "browser's share picker chooses what is recorded, and it "
                    "opens when the recording starts. fused.capture.screenshot "
                    "does take a 'display'")
        if spec.get("rect"):
            return ("'rect' cannot be chosen here — the browser shares a whole "
                    "screen, window or tab, picked in its own dialog. Crop "
                    "afterwards, or use fused.capture.screenshot, which does "
                    "take a 'rect'")
        if spec.get("cursor") is not None:
            return ("'cursor' cannot be chosen here — whether the pointer is "
                    "recorded is the browser's decision on this platform. "
                    "fused.capture.screenshot does take a 'cursor'")
    out = spec.get("out")
    if out:
        wanted = ext(mode, spec)
        have = os.path.splitext(out)[1].lower()
        # `.mp4` and `.m4a` are one container under two names, so either is a
        # truthful extension for what the mp4 branch writes.
        allowed = ({".mp4", ".m4a"} if wanted in (".mp4", ".m4a")
                   else {wanted})
        if have not in allowed:
            # Name the container that WOULD match the path they asked for —
            # suggesting the one already in force is advice nobody can act on.
            instead = {".webm": "'webm'", ".mp4": "'mp4'",
                       ".m4a": "'mp4'"}.get(have)
            fix = (f", or pass container: {instead}" if instead else "")
            return (f"this recording is written as {wanted} — a 'path' ending "
                    f"{have or 'with no extension'} would name a file holding "
                    f"something else. Ask for a {wanted} path{fix}")
    return None


class _Sink:
    """One recording being fed over a socket: the open file and its counters."""

    def __init__(self, cid: str, mode: str, path: str):
        self.id = cid
        self.mode = mode
        self.path = path
        # A capture id is short and appears in a job row; the socket needs
        # something that is neither guessable nor on display, because attaching
        # to a recording means writing to a file of this server's choosing.
        self.token = secrets.token_urlsafe(24)
        self.transport = "stream"
        self.lock = threading.Lock()
        self.fh = open(path, "wb")
        self.attached = False
        self.done = False
        self.bytes = 0
        self.started_at = time.monotonic()
        self.last_at = self.started_at
        self.error: str | None = None

    # -- writing -------------------------------------------------------------

    def write(self, chunk: bytes) -> None:
        with self.lock:
            if self.done:
                return
            if self.bytes + len(chunk) > MAX_BYTES:
                self.error = (f"the recording passed {MAX_BYTES // 1024 ** 3} "
                              "GB and was stopped")
                self._close()
                return
            self.fh.write(chunk)
            self.bytes += len(chunk)
            self.last_at = time.monotonic()

    def _close(self) -> None:
        """Close the file. Caller holds the lock."""
        if self.done:
            return
        self.done = True
        try:
            self.fh.flush()
            os.fsync(self.fh.fileno())
        except OSError:
            pass
        try:
            self.fh.close()
        except OSError:
            pass

    def close(self) -> None:
        with self.lock:
            self._close()


_lock = threading.Lock()
_sinks: dict[str, _Sink] = {}


# ------------------------------------------------------------------ starting


def _start(out: str, spec: dict, mode: str) -> _Sink:
    """Open the file and register it. Returns WITHOUT waiting for the page.

    Deliberately non-blocking: the reply carries the path and the token, and the
    page opens its socket next. Blocking here would hold a request open across a
    human deciding what to share.
    """
    cid = spec.get("id")
    if not cid:                                          # pragma: no cover
        raise RuntimeError("a streamed recording needs its capture id")
    sink = _Sink(cid, mode, out)
    with _lock:
        _sinks[cid] = sink
    return sink


def start_screen(out: str, spec: dict) -> _Sink:
    return _start(out, spec, "screen")


def start_audio(out: str, spec: dict) -> _Sink:
    return _start(out, spec, "audio")


# ------------------------------------------------------------------ the socket


class AttachError(RuntimeError):
    """The socket may not have this recording, and the message says why."""


def attach(cid: str, token: str | None) -> _Sink:
    """Claim a recording for one socket. Raises rather than half-attaching.

    Refuses a SECOND attach outright: two encoders appending to one file
    interleave two containers into something unplayable, and the honest failure
    is at the door.
    """
    with _lock:
        sink = _sinks.get(cid)
    if sink is None:
        raise AttachError(f"no capture is waiting for a stream: {cid}")
    if not token or not secrets.compare_digest(str(token), sink.token):
        raise AttachError("wrong or missing stream token")
    with sink.lock:
        if sink.done:
            raise AttachError(f"capture {cid} has already finished")
        if sink.attached:
            raise AttachError(f"capture {cid} already has a stream attached")
        sink.attached = True
    return sink


def detach(cid: str) -> None:
    """The socket closed. If the recording is still live, the page went away.

    This is the ending that has no equivalent on macOS: the encoder lived in the
    page. It KEEPS the file — every container `MediaRecorder` writes is playable
    as written — and lets the neutral half report a normal stop, so the row says
    done and `list()` stops showing a recording that is not happening.
    """
    with _lock:
        sink = _sinks.get(cid)
    if sink is None or sink.done:
        return
    from fused_render import capture

    try:
        capture.stop(cid)
    except Exception:                        # noqa: BLE001 - a socket teardown
        logger.warning("could not finalise capture %s after its stream closed",
                       cid, exc_info=True)


# ------------------------------------------------------------------ the seam


def failure(handle: _Sink) -> str | None:
    """Has this recording already died? Read every watchdog tick.

    Two ways it can, and neither raises anywhere: the page never opened its
    socket (a picker the user walked away from), and the socket is open but the
    chunks stopped (a frozen renderer). Both would otherwise tick "Recording"
    to the cap over a file nothing is writing — the case the hook exists for.
    """
    if handle.error:
        return handle.error
    if handle.done:
        return None
    idle = time.monotonic() - handle.last_at
    if not handle.attached:
        if time.monotonic() - handle.started_at > ATTACH_S:
            return ("the page never opened its stream — the share dialog was "
                    f"not answered within {int(ATTACH_S)}s")
        return None
    if idle > STALL_S:
        return (f"no video arrived for {int(idle)}s — the page that was "
                "recording stopped sending")
    return None


def stop(handle: _Sink) -> None:
    """Close the file. Raises only when there is nothing playable in it."""
    empty = False
    with handle.lock:
        empty = handle.bytes == 0
        handle._close()
    with _lock:
        _sinks.pop(handle.id, None)
    if handle.error:
        raise RuntimeError(handle.error)
    if empty:
        raise RuntimeError(
            "no video was ever received for this recording — the page did not "
            "start sending, so the file is empty")
