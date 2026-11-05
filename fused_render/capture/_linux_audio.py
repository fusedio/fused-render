"""Native Linux audio, over PipeWire's pulse-compatible server: `pasimple`.

**Why `pasimple` and not the others considered.** PyAV bundles only an ALSA
input device (`-f alsa`), which is a dead end under PipeWire's `pipewire-pulse`
session — the thing actually running on this machine — and its wheel is
~130 MB for a capability this needs none of the rest of. `sounddevice`'s wheel
links `libportaudio`, which the common manylinux wheels do not ship, so it is a
"works on my machine" dependency rather than a portable one. `pasimple` is a
ctypes binding over `libpulse-simple.so.0` — no build step, no bundled native
lib, and `libpulse-simple` is what `pactl`/every desktop already has, because
`pipewire-pulse` is a drop-in pulseaudio server.

**One blocking `PA_STREAM_RECORD` stream per source, each pulled on its own
daemon thread.** `PaSimple.read(n)` blocks until `n` bytes exist, so the loop
is the simplest Python has: read a ~20 ms chunk, do something with it, repeat.
`"mic"` and `"system"` are one stream each (`device_name=None` is the default
source; `"@DEFAULT_MONITOR@"` is pulse's own alias for the default sink's
monitor — no need to resolve its name first). `"both"` runs two streams and
mixes them: the monitor is the MASTER clock (it drives when a mixed sample is
written), the microphone's bytes queue in `_mixdown.Ring` and are pulled off it
sample-for-sample as the monitor produces frames — the same drift-bounded
scheme `_darwin_mux` uses for two independently-clocked sources, imported
rather than re-derived.

**Output is a plain mono 48 kHz int16 WAV**, not an encoded container: the main
venv has no encoder (`fused.ai.transcribe` shells out to ffmpeg in its own
runner, not this process), and `wave` is stdlib. Every chunk is converted from
the stream's float32 and written immediately, so the file exists and grows
from the first chunk rather than appearing only at `stop()` — though, as with
any WAV being appended to, its header undercounts frames until `stop()`
patches it on close.

**Pause keeps the streams open and draining.** Closing a `PaSimple` stream on
pause and reopening it on resume would both lose whatever PipeWire queued in
between and risk the default source/monitor changing underneath a paused
recording; instead the puller threads keep reading (so PipeWire is never left
blocked on a reader) and just stop writing to the WAV while `paused` is set.

**Each puller thread owns closing its own stream; nothing else ever frees a
stream a read may be inside.** `libpulse-simple` aborts the whole process when
`pa_simple_free` runs under a blocked `pa_simple_read` (a mutex freed while the
reader holds it, or the reader waking on a freed stream). A read can block
forever — a Bluetooth headset whose transport failed delivers nothing and
never errors — so `stop` sets the stop flag, waits a bounded time, and closes
only streams whose reader thread has exited or never started. A reader still
stuck is left running with its stream (a logged leak, cheaper than a dead
server) and closes the stream itself if its read ever returns; the WAV is
closed regardless and `Handle.write` is a no-op once it is.

**A stalled stream is reported, not waited on.** Every read stamps its stream's
last-chunk time (paused or not — pause keeps reading); `failure()` flags a
stream that has delivered nothing for `STALL_AFTER_S`; until its first chunk
arrives the allowance is the longer `FIRST_CHUNK_GRACE_S`, since opening a
Bluetooth headset's microphone switches its profile before anything flows.

**Permission is not a concept here.** Unlike the macOS tap, there is no grant
a user can deny — if PipeWire is reachable at all, `pasimple` gets real
samples. What IS silent for an unrelated reason is a muted or idle output: the
monitor stream delivers exact zeros for that too, so `warning()` reports it the
same way the macOS backends do, as a warning on an otherwise valid file.
"""

from __future__ import annotations

import array
import ctypes.util
import logging
import os
import socket
import threading
import time
import wave

from fused_render.capture import _mixdown, _pause

logger = logging.getLogger(__name__)

#: pulse's own alias for "whatever the default sink's monitor is right now" —
#: resolving an actual monitor name first would be one more thing to get wrong
#: when the default output changes between calls.
MONITOR_DEVICE = "@DEFAULT_MONITOR@"

RATE = 48000
CHANNELS = 1

#: ~20 ms per pull — small enough that pause/stop react promptly, large enough
#: that the read() loop is not pure syscall overhead.
CHUNK_MS = 20
CHUNK_SAMPLES = RATE * CHUNK_MS // 1000
CHUNK_BYTES = CHUNK_SAMPLES * 4                       # float32 mono

#: How long "both" lets the microphone drift from the monitor before dropping
#: or padding — one second, generous because a desktop's two clocks are close.
RING_HIGH_WATER = RATE * 4 * 1

#: A stream that delivers no chunk for this long is reported as stalled by
#: `failure()`.
STALL_AFTER_S = 3.0

#: The same, until a stream's first chunk: a Bluetooth headset's A2DP -> HFP
#: profile switch can take several seconds to open the microphone.
FIRST_CHUNK_GRACE_S = 10.0

#: How long `_teardown` waits for each reader to exit before leaving it.
_JOIN_TIMEOUT_S = 2.0

#: A recording shorter than this has not been given long enough to hear
#: anything, so `warning()` never flags it.
_WARN_AFTER_S = 1.0

#: An RMS above this on the monitor counts as "heard something" — the same
#: figure `_darwin_mux`'s peak check uses, so both platforms draw the line at
#: the same loudness.
_HEAR_RMS = 1e-4

#: How long `available()`'s reachability check waits for `connect()` before
#: giving up — long enough for a live local socket to accept instantly (it is
#: not doing any work yet), short enough that a dead path does not make a
#: probe feel stuck.
_REACHABLE_TIMEOUT_S = 0.2


# ------------------------------------------------------------------- the probe


def _pasimple():
    try:
        import pasimple

        return pasimple
    except ImportError:
        return None


def _lib_loads() -> bool:
    name = ctypes.util.find_library("pulse-simple") or "libpulse-simple.so.0"
    try:
        ctypes.CDLL(name)
        return True
    except OSError:
        return False


def _local_unix_path(entry: str) -> str | None:
    """The socket path when a single `$PULSE_SERVER` entry names a local unix
    socket, else None. Per libpulse's server-string rules, an entry may carry
    an optional `{machine-id}` prefix (stripped here, unused — this module
    never needs to tell sessions apart by it); what is left names a local
    socket only as `unix:/path` or a bare absolute `/path`. Anything else
    (`tcp:`, `tcp4:`, `tcp6:`, a hostname) is remote."""
    if entry.startswith("{"):
        end = entry.find("}")
        if end != -1:
            entry = entry[end + 1:]
    if entry.startswith("unix:"):
        return entry[len("unix:"):]
    if entry.startswith("/"):
        return entry
    return None


def _first_local_entry() -> str | None:
    """`$PULSE_SERVER` may be a whitespace-separated list of entries, tried
    in order by libpulse itself; this returns the path of the first one that
    names a local unix socket (see `_local_unix_path`), skipping remote
    entries ahead of it rather than giving up at the first one. None when
    `$PULSE_SERVER` is unset or every entry is remote."""
    pulse_server = os.environ.get("PULSE_SERVER")
    if not pulse_server:
        return None
    for entry in pulse_server.split():
        path = _local_unix_path(entry)
        if path is not None:
            return path
    return None


def _remote_server() -> bool:
    """True when `$PULSE_SERVER` is set but none of its entries name a local
    unix socket (see `_first_local_entry`) — this module only speaks to a
    local PipeWire/pulse session, so a server string with only remote
    entries (`tcp:host:port`, most commonly) is unsupported regardless of
    whether it would actually answer."""
    pulse_server = os.environ.get("PULSE_SERVER")
    return bool(pulse_server) and _first_local_entry() is None


def _socket_path() -> str | None:
    """Where a local session's socket should be: the first local unix-socket
    entry in `$PULSE_SERVER` when one is set (see `_first_local_entry`), else
    the default `$XDG_RUNTIME_DIR/pulse/native`. None when `$PULSE_SERVER` is
    set but names no local entry (that is `_remote_server()`'s call, not this
    one's) and `$XDG_RUNTIME_DIR` is unset."""
    pulse_server = os.environ.get("PULSE_SERVER")
    if pulse_server:
        return _first_local_entry()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(runtime, "pulse", "native") if runtime else None


def _reachable(path: str) -> bool:
    """A cheap, non-prompting proof that something is actually listening —
    an existing path (or a set `$PULSE_SERVER`) is not proof of that: a
    socket left behind by a session that crashed looks identical to a live
    one until something connects to it. `connect()` and an immediate
    `close()` is the whole check: no pulse frame is ever sent, so this can
    never be mistaken for starting a stream (CP-7)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_REACHABLE_TIMEOUT_S)
            sock.connect(path)
        return True
    except OSError:
        return False


def available() -> bool:
    """Cheap and honest: no pulse stream opened, nobody prompted (there is
    nothing to prompt for — see the header). "Cheap" still means a real
    reachability check, not just a path or an env var existing — see
    `_reachable`."""
    if _pasimple() is None or not _lib_loads() or _remote_server():
        return False
    path = _socket_path()
    return bool(path) and _reachable(path)


def unavailable_reason() -> str:
    """Why `available()` said no, most actionable cause first."""
    if _pasimple() is None:
        return ("audio recording needs pasimple — pip install "
                "'fused-render[linux-desktop]' (the packaged app ships it)")
    if not _lib_loads():
        return ("audio recording needs libpulse-simple.so.0 — install the "
                "pulseaudio or pipewire-pulse client libraries")
    if _remote_server():
        return ("audio recording only supports a local PipeWire or "
                "PulseAudio session — $PULSE_SERVER names a remote one")
    path = _socket_path()
    if not path or not _reachable(path):
        return ("audio recording needs a PipeWire (or PulseAudio) session — "
                "none is reachable")
    return "audio recording is unavailable"                # pragma: no cover


# ------------------------------------------------------------------- the handle


class _Reader:
    """One open stream, the label it reports under, and the thread that reads
    it (None until launched, and for a stream whose launch never happened)."""

    def __init__(self, label: str, stream):
        self.label = label
        self.stream = stream
        self.thread: threading.Thread | None = None


class Handle:
    """A recording in progress: the open streams, their puller threads, the WAV."""

    def __init__(self, path: str, source: str):
        self.path = path
        self.audio_source = source                    # "mic" | "system" | "both"
        self.error: str | None = None
        self.heard_system = False
        self.started_at = time.monotonic()
        self.paused = False
        self.last_level = 0.0
        self.last_chunk_at: dict[str, float] = {}     # stream label -> monotonic
        self._lock = threading.Lock()                 # guards error and the wav
        self._ring_lock = threading.Lock()             # guards the mic ring
        self._stop = threading.Event()
        self._wav: wave.Wave_write | None = None
        self.delivered: set[str] = set()              # labels with a first chunk
        self._readers: list[_Reader] = []
        self._ring: _mixdown.Ring | None = None

    def note_error(self, message: str) -> None:
        with self._lock:
            if self.error is None:
                self.error = message
        self._stop.set()

    def write(self, pcm16: bytes) -> None:
        if self.paused or not pcm16:
            return
        with self._lock:
            if self._wav is not None:
                self._wav.writeframes(pcm16)

    def beat(self, label: str) -> None:
        self.last_chunk_at[label] = time.monotonic()
        self.delivered.add(label)

    def add_reader(self, label: str, stream) -> _Reader:
        """Record a stream the moment it opens (so teardown finds it even if a
        later open raises) and start its stall clock."""
        reader = _Reader(label, stream)
        self._readers.append(reader)
        self.last_chunk_at[label] = time.monotonic()
        return reader

    def stalled(self) -> str | None:
        """The label of a stream that has delivered nothing for
        `STALL_AFTER_S` (`FIRST_CHUNK_GRACE_S` before its first chunk), else
        None."""
        now = time.monotonic()
        for label, at in list(self.last_chunk_at.items()):
            limit = (STALL_AFTER_S if label in self.delivered
                     else FIRST_CHUNK_GRACE_S)
            if now - at > limit:
                return label
        return None

    def meter(self, floats: bytes) -> None:
        try:
            self.last_level = _pause.amp_to_level(_pause.rms_f32(floats))
        except Exception:                             # noqa: BLE001 - a meter
            pass


def _float_to_int16(data: bytes) -> bytes:
    """float32 PCM -> int16 PCM, clamped to [-1, 1]."""
    count = len(data) // 4
    if count <= 0:
        return b""
    floats = array.array("f")
    floats.frombytes(data[:count * 4])
    out = array.array("h", bytes(2 * count))
    for i, v in enumerate(floats):
        v = -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)
        out[i] = int(v * 32767)
    return out.tobytes()


def _open_stream(pasimple_mod, device_name: str | None, label: str):
    # `fragsize` defaults to "about 2s" (pasimple's own words) when left at
    # -1, which would make every `read(CHUNK_BYTES)` block for ~2s instead of
    # ~20ms — pin it to the chunk size so pulls arrive at the pace the pause
    # flag and the level meter expect.
    return pasimple_mod.PaSimple(
        pasimple_mod.PA_STREAM_RECORD, pasimple_mod.PA_SAMPLE_FLOAT32LE,
        channels=CHANNELS, rate=RATE, app_name="Fused", stream_name=label,
        device_name=device_name, fragsize=CHUNK_BYTES)


def _close_stream(stream) -> None:
    try:
        stream.close()
    except Exception:                                   # noqa: BLE001
        logger.warning("closing an audio stream failed", exc_info=True)


def _pull_single(handle: Handle, label: str, stream, is_monitor: bool) -> None:
    """One source, written straight to the WAV."""
    try:
        while not handle._stop.is_set():
            data = stream.read(CHUNK_BYTES)
            handle.beat(label)
            if is_monitor and not handle.heard_system:
                if _pause.rms_f32(data) > _HEAR_RMS:
                    handle.heard_system = True
            handle.meter(data)
            handle.write(_float_to_int16(data))
    except Exception as exc:                            # noqa: BLE001
        if not handle._stop.is_set():
            handle.note_error(
                f"the {label} audio stream ended unexpectedly: {exc} "
                "(PipeWire may have gone away)")
    finally:
        _close_stream(stream)


def _pull_mic_into_ring(handle: Handle, label: str, stream) -> None:
    """`"both"`'s microphone half: queues bytes for the monitor thread to mix."""
    try:
        while not handle._stop.is_set():
            data = stream.read(CHUNK_BYTES)
            handle.beat(label)
            with handle._ring_lock:
                handle._ring.push(data)
    except Exception as exc:                            # noqa: BLE001
        if not handle._stop.is_set():
            handle.note_error(
                f"the {label} audio stream ended unexpectedly: {exc} "
                "(PipeWire may have gone away)")
    finally:
        _close_stream(stream)


def _pull_monitor_master(handle: Handle, label: str, stream) -> None:
    """`"both"`'s master clock: one monitor chunk pulls the matching mic bytes
    out of the ring and mixes them in place."""
    try:
        while not handle._stop.is_set():
            tap = bytearray(stream.read(CHUNK_BYTES))
            handle.beat(label)
            if not handle.heard_system and _pause.rms_f32(tap) > _HEAR_RMS:
                handle.heard_system = True
            with handle._ring_lock:
                mic = handle._ring.take(len(tap))
            _mixdown.add_clip_into(tap, bytes(mic))
            handle.meter(tap)
            handle.write(_float_to_int16(bytes(tap)))
    except Exception as exc:                            # noqa: BLE001
        if not handle._stop.is_set():
            handle.note_error(
                f"the {label} audio stream ended unexpectedly: {exc} "
                "(PipeWire may have gone away)")
    finally:
        _close_stream(stream)


# ------------------------------------------------------------------- start/stop


def start(out: str, spec: dict) -> Handle:
    """Record `spec["audio"]` ("mic", "system" or "both") to `out` (mono WAV)."""
    source = spec.get("audio") or "mic"
    handle = Handle(out, source)
    try:
        _start(handle, out, source)
    except BaseException:
        _teardown(handle, raise_errors=False)
        raise
    return handle


def _start(handle: Handle, out: str, source: str) -> None:
    pasimple_mod = _pasimple()
    if pasimple_mod is None:
        raise RuntimeError(unavailable_reason())

    handle._wav = wave.open(out, "wb")
    handle._wav.setnchannels(CHANNELS)
    handle._wav.setsampwidth(2)
    handle._wav.setframerate(RATE)

    if source == "mic":
        mic = _open_reader(handle, pasimple_mod, "microphone", None)
        _launch(handle, mic, "mic", _pull_single, False)
    elif source == "system":
        system = _open_reader(handle, pasimple_mod, "system", MONITOR_DEVICE)
        _launch(handle, system, "system", _pull_single, True)
    else:
        handle._ring = _mixdown.Ring(RING_HIGH_WATER)
        # Both are opened before either thread starts, and each is recorded as
        # it opens — so a monitor open that raises still finds the mic reader
        # on the handle and `_teardown` closes its stream rather than leaking.
        mic = _open_reader(handle, pasimple_mod, "microphone", None)
        system = _open_reader(handle, pasimple_mod, "system", MONITOR_DEVICE)
        _launch(handle, mic, "mic", _pull_mic_into_ring)
        _launch(handle, system, "system", _pull_monitor_master)


_STREAM_NAMES = {"microphone": "Fused microphone", "system": "Fused system audio"}


def _open_reader(handle: Handle, pasimple_mod, label: str,
                 device_name: str | None) -> _Reader:
    return handle.add_reader(
        label, _open_stream(pasimple_mod, device_name, _STREAM_NAMES[label]))


def _launch(handle: Handle, reader: _Reader, thread_suffix: str, target,
            *extra) -> None:
    reader.thread = threading.Thread(
        target=target, args=(handle, reader.label, reader.stream, *extra),
        daemon=True, name=f"fused-capture-{thread_suffix}")
    reader.thread.start()


def _teardown(handle: Handle, raise_errors: bool = True) -> None:
    """Stop every puller, close the streams no reader owns, finalise the WAV.

    A stream with a live reader thread is never closed here — see the module
    header: the reader closes it itself if its read ever returns. Every step
    runs even if an earlier one raises, so a failed stop still closes every
    stream no reader owns and finalises the WAV."""
    first: BaseException | None = None
    handle._stop.set()

    def step(fn, *args):
        nonlocal first
        try:
            fn(*args)
        except BaseException as exc:                    # noqa: BLE001
            first = first or exc

    deadline = time.monotonic() + _JOIN_TIMEOUT_S
    for reader in handle._readers:
        if reader.thread is not None and reader.thread.ident is not None:
            step(reader.thread.join, max(0.0, deadline - time.monotonic()))
    # A reader with no thread, or whose thread never ran (an open that failed
    # mid-`_start`), has nothing to close its stream.
    for reader in handle._readers:
        thread = reader.thread
        if thread is not None and thread.ident is not None:
            if thread.is_alive():
                logger.warning(
                    "audio reader %s is stuck in a read; leaving its stream "
                    "open rather than freeing it under the read", thread.name)
            continue
        step(_close_stream, reader.stream)
    with handle._lock:
        wav, handle._wav = handle._wav, None
    if wav is not None:
        step(wav.close)
    if first is not None and raise_errors:
        raise first


def stop(handle: Handle) -> None:
    """End the capture; the WAV is finalised when this returns."""
    _teardown(handle)
    if handle.error:
        raise RuntimeError(handle.error)


def pause(handle: Handle) -> None:
    """Stop writing; the streams keep draining so PipeWire is never blocked."""
    handle.paused = True
    handle.last_level = 0.0


def resume(handle: Handle) -> None:
    handle.paused = False


def can_pause(handle: Handle) -> bool:
    return True


def level(handle: Handle) -> float:
    """0..1 loudness of the most recent chunk."""
    return 0.0 if handle.paused else handle.last_level


def failure(handle: Handle) -> str | None:
    """The error this recording has ALREADY died of: a puller thread's, or a
    stream that has stopped delivering chunks."""
    if handle.error is None:
        label = handle.stalled()
        if label is not None:
            handle.note_error(
                f"the {label} audio device stopped delivering audio (it may "
                "have disconnected or switched profile)")
    return handle.error


def warning(handle: Handle) -> str | None:
    """A sentence when a system/both recording that ran long enough heard
    nothing on the monitor. A warning, not a failure: the file is valid either
    way, and a muted or idle output is indistinguishable from here. Never
    raises; paused time still counts toward the heard-nothing timer, the same
    documented limitation the macOS tap has."""
    if handle.audio_source not in ("system", "both"):
        return None
    if handle.heard_system:
        return None
    if time.monotonic() - handle.started_at < _WARN_AFTER_S:
        return None
    return ("no system audio was captured — nothing was playing, or the "
            "output is muted")
