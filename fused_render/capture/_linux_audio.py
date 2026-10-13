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
        self._lock = threading.Lock()                 # guards error and the wav
        self._ring_lock = threading.Lock()             # guards the mic ring
        self._stop = threading.Event()
        self._wav: wave.Wave_write | None = None
        self._streams: list = []
        self._threads: list[threading.Thread] = []
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


def _pull_single(handle: Handle, stream, is_monitor: bool, label: str) -> None:
    """One source, written straight to the WAV."""
    try:
        while not handle._stop.is_set():
            data = stream.read(CHUNK_BYTES)
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


def _pull_mic_into_ring(handle: Handle, stream) -> None:
    """`"both"`'s microphone half: queues bytes for the monitor thread to mix."""
    try:
        while not handle._stop.is_set():
            data = stream.read(CHUNK_BYTES)
            with handle._ring_lock:
                handle._ring.push(data)
    except Exception as exc:                            # noqa: BLE001
        if not handle._stop.is_set():
            handle.note_error(
                f"the microphone audio stream ended unexpectedly: {exc} "
                "(PipeWire may have gone away)")


def _pull_monitor_master(handle: Handle, stream) -> None:
    """`"both"`'s master clock: one monitor chunk pulls the matching mic bytes
    out of the ring and mixes them in place."""
    try:
        while not handle._stop.is_set():
            tap = bytearray(stream.read(CHUNK_BYTES))
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
                f"the system audio stream ended unexpectedly: {exc} "
                "(PipeWire may have gone away)")


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
        stream = _open_stream(pasimple_mod, None, "Fused microphone")
        handle._streams.append(stream)
        thread = threading.Thread(
            target=_pull_single, args=(handle, stream, False, "microphone"),
            daemon=True, name="fused-capture-mic")
        handle._threads.append(thread)
        thread.start()
    elif source == "system":
        stream = _open_stream(pasimple_mod, MONITOR_DEVICE, "Fused system audio")
        handle._streams.append(stream)
        thread = threading.Thread(
            target=_pull_single, args=(handle, stream, True, "system"),
            daemon=True, name="fused-capture-system")
        handle._threads.append(thread)
        thread.start()
    else:
        handle._ring = _mixdown.Ring(RING_HIGH_WATER)
        # Appended right after each open, not batched after both succeed — so
        # a monitor open that raises still finds the mic stream in
        # `handle._streams` and `_teardown` closes it rather than leaking it.
        mic = _open_stream(pasimple_mod, None, "Fused microphone")
        handle._streams.append(mic)
        monitor = _open_stream(pasimple_mod, MONITOR_DEVICE, "Fused system audio")
        handle._streams.append(monitor)
        mic_thread = threading.Thread(
            target=_pull_mic_into_ring, args=(handle, mic),
            daemon=True, name="fused-capture-mic")
        monitor_thread = threading.Thread(
            target=_pull_monitor_master, args=(handle, monitor),
            daemon=True, name="fused-capture-system")
        handle._threads.extend([mic_thread, monitor_thread])
        mic_thread.start()
        monitor_thread.start()


def _teardown(handle: Handle, raise_errors: bool = True) -> None:
    """Stop every puller, close every stream, finalise the WAV header.

    Every step runs even if an earlier one raises, so a failed stop never
    leaves a stream attached to PipeWire with nothing reading it."""
    first: BaseException | None = None
    handle._stop.set()

    def step(fn, *args):
        nonlocal first
        try:
            fn(*args)
        except BaseException as exc:                    # noqa: BLE001
            first = first or exc

    for thread in handle._threads:
        step(thread.join, 2.0)
    for stream in handle._streams:
        step(stream.close)
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
    """The puller-thread error this recording has ALREADY died of."""
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
