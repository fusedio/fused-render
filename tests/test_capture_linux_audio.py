"""Native Linux audio (`capture/_linux_audio.py`) — tested off Linux too, by
standing in for `pasimple` with a fake that produces deterministic float32
chunks instead of opening a real PipeWire stream.
"""

from __future__ import annotations

import array
import math
import socket
import time
import wave

import pytest

from fused_render.capture import _linux, _linux_audio

#: AF_UNIX exists on Linux and macOS (and modern Windows), but a test that
#: needs a real listening socket should skip cleanly rather than fail on a
#: platform where it is simply absent.
_HAS_AF_UNIX = hasattr(socket, "AF_UNIX")
_needs_af_unix = pytest.mark.skipif(not _HAS_AF_UNIX, reason="no AF_UNIX on this platform")

RATE = _linux_audio.RATE
CHUNK_SAMPLES = _linux_audio.CHUNK_SAMPLES


# --------------------------------------------------------------------- the fake


class FakeStream:
    """One `PaSimple` stream: `gen(chunk_index, num_bytes) -> bytes` on every
    `read`, paced by a short real sleep so a test's wall-clock budget bounds
    how many chunks a busy puller thread can pull."""

    def __init__(self, gen, pace: float = 0.001):
        self._gen = gen
        self._pace = pace
        self._idx = 0
        self.closed = False

    def read(self, num_bytes: int) -> bytes:
        if self._pace:
            time.sleep(self._pace)
        data = self._gen(self._idx, num_bytes)
        self._idx += 1
        return data

    def close(self) -> None:
        self.closed = True


def silence(idx: int, num_bytes: int) -> bytes:
    return bytes(num_bytes)


def sine(freq: float = 440.0, amp: float = 0.3, pace: float = 0.001):
    def gen(idx: int, num_bytes: int) -> bytes:
        n = num_bytes // 4
        start = idx * n
        samples = [amp * math.sin(2 * math.pi * freq * (start + i) / RATE)
                   for i in range(n)]
        return array.array("f", samples).tobytes()
    return gen


def failing_after(base_gen, fail_at: int, message: str = "PipeWire went away"):
    def gen(idx: int, num_bytes: int) -> bytes:
        if idx >= fail_at:
            raise RuntimeError(message)
        return base_gen(idx, num_bytes)
    return gen


def fake_pasimple(generators: dict, pace: float = 0.001):
    """`generators`: `{device_name: gen}` — `None` is the default source,
    `_linux_audio.MONITOR_DEVICE` is the default sink's monitor."""

    class _PaSimple:
        def __init__(self, direction, fmt, channels, rate, app_name="python",
                     stream_name=None, server_name=None, device_name=None,
                     maxlength=-1, tlength=-1, prebuf=-1, minreq=-1, fragsize=-1):
            self._stream = FakeStream(generators[device_name], pace=pace)

        def read(self, num_bytes):
            return self._stream.read(num_bytes)

        def close(self):
            self._stream.close()

    class _Module:
        PA_STREAM_RECORD = 1
        PA_STREAM_PLAYBACK = 2
        PA_SAMPLE_FLOAT32LE = 3
        PaSimple = _PaSimple

    return _Module()


def _wav_frames(path) -> tuple[int, int, bytes]:
    with wave.open(str(path), "rb") as handle:
        return handle.getnchannels(), handle.getframerate(), handle.readframes(handle.getnframes())


def _rms(pcm16: bytes) -> float:
    if not pcm16:
        return 0.0
    ints = array.array("h")
    ints.frombytes(pcm16)
    return (sum(v * v for v in ints) / len(ints)) ** 0.5


# ----------------------------------------------------------------------- probe


@_needs_af_unix
def test_available_true_when_pasimple_lib_and_a_real_socket_are_reachable(
        monkeypatch, tmp_path):
    # A real listening unix socket at the default
    # `$XDG_RUNTIME_DIR/pulse/native` path, not just a path that exists —
    # `available()` must actually connect to it, not merely stat it.
    monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({}))
    monkeypatch.setattr(_linux_audio, "_lib_loads", lambda: True)
    monkeypatch.delenv("PULSE_SERVER", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    pulse_dir = tmp_path / "pulse"
    pulse_dir.mkdir()
    sock_path = str(pulse_dir / "native")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    try:
        assert _linux_audio.available() is True
    finally:
        server.close()


def test_unavailable_without_pasimple(monkeypatch):
    monkeypatch.setattr(_linux_audio, "_pasimple", lambda: None)
    assert _linux_audio.available() is False
    assert "pasimple" in _linux_audio.unavailable_reason()


@_needs_af_unix
def test_unavailable_when_the_socket_path_has_nothing_listening(monkeypatch, tmp_path):
    # A dead path: exists on disk as a concept (the directory does), but
    # nothing bound a socket there — the old `os.path.exists` check would have
    # called this available; `available()` must actually try to connect.
    monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({}))
    monkeypatch.setattr(_linux_audio, "_lib_loads", lambda: True)
    monkeypatch.delenv("PULSE_SERVER", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "pulse").mkdir()
    assert _linux_audio.available() is False
    assert "PipeWire" in _linux_audio.unavailable_reason()


@_needs_af_unix
def test_unavailable_when_pulse_server_names_a_remote_host(monkeypatch, tmp_path):
    # `tcp:` (or anything else non-`unix:`) is a remote server — this module
    # only supports a local PipeWire/pulse session, so this is unsupported
    # without even trying to reach it.
    monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({}))
    monkeypatch.setattr(_linux_audio, "_lib_loads", lambda: True)
    monkeypatch.setenv("PULSE_SERVER", "tcp:example.org:4713")
    assert _linux_audio.available() is False
    assert "local" in _linux_audio.unavailable_reason()


@_needs_af_unix
def test_available_true_with_pulse_server_naming_a_reachable_unix_socket(
        monkeypatch, tmp_path):
    sock_path = str(tmp_path / "native")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    try:
        monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({}))
        monkeypatch.setattr(_linux_audio, "_lib_loads", lambda: True)
        monkeypatch.setenv("PULSE_SERVER", "unix:" + sock_path)
        assert _linux_audio.available() is True
    finally:
        server.close()


@_needs_af_unix
def test_available_true_with_pulse_server_naming_a_bare_absolute_path(
        monkeypatch, tmp_path):
    # libpulse treats a bare absolute path (no `unix:` prefix) as a local
    # unix socket too — not just the `unix:` form.
    sock_path = str(tmp_path / "native")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    try:
        monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({}))
        monkeypatch.setattr(_linux_audio, "_lib_loads", lambda: True)
        monkeypatch.setenv("PULSE_SERVER", sock_path)
        assert _linux_audio.available() is True
    finally:
        server.close()


@_needs_af_unix
def test_available_true_with_pulse_server_machine_id_prefixed_unix_path(
        monkeypatch, tmp_path):
    # An entry may carry an optional `{machine-id}` prefix ahead of the
    # `unix:/path` form — it must be stripped, not treated as part of the
    # path or as making the entry unrecognized (and so "remote").
    sock_path = str(tmp_path / "native")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    try:
        monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({}))
        monkeypatch.setattr(_linux_audio, "_lib_loads", lambda: True)
        monkeypatch.setenv("PULSE_SERVER", "{deadbeef}unix:" + sock_path)
        assert _linux_audio.available() is True
    finally:
        server.close()


@_needs_af_unix
def test_available_true_with_pulse_server_list_tcp_first_then_local_path(
        monkeypatch, tmp_path):
    # `$PULSE_SERVER` may be a whitespace-separated list of entries tried in
    # order; a remote entry ahead of a local one must not make the whole
    # thing "remote" — the first local entry anywhere in the list wins.
    sock_path = str(tmp_path / "native")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    try:
        monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({}))
        monkeypatch.setattr(_linux_audio, "_lib_loads", lambda: True)
        monkeypatch.setenv(
            "PULSE_SERVER", "tcp:example.org:4713 unix:" + sock_path)
        assert _linux_audio.available() is True
    finally:
        server.close()


@_needs_af_unix
def test_unavailable_when_pulse_server_names_only_tcp_entries(monkeypatch):
    # A server string with no local entry at all is still correctly reported
    # as remote/unsupported, not probed.
    monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({}))
    monkeypatch.setattr(_linux_audio, "_lib_loads", lambda: True)
    monkeypatch.setenv(
        "PULSE_SERVER", "tcp:example.org:4713 tcp6:[::1]:4713")
    assert _linux_audio.available() is False
    assert "local" in _linux_audio.unavailable_reason()


def test_probe_reports_audio_native_truthfully(monkeypatch):
    monkeypatch.setattr(_linux_audio, "available", lambda: True)
    probe = _linux.probe()
    assert probe["audioNative"] is True
    assert probe["audio"]["available"] is True
    assert probe["systemAudio"]["available"] is True

    monkeypatch.setattr(_linux_audio, "available", lambda: False)
    monkeypatch.setattr(_linux_audio, "unavailable_reason",
                        lambda: "audio recording needs a PipeWire (or PulseAudio) "
                                "session — none is reachable")
    probe = _linux.probe()
    assert probe["audioNative"] is False
    # The mic still has the `_sink` browser fallback, so `audio` stays
    # truthful here — the browser merge refines it once `audioNative` is
    # false. `systemAudio` has no such fallback, so it reports the truth.
    assert probe["audio"]["available"] is True
    assert probe["audio"]["reason"] is None
    assert probe["systemAudio"]["available"] is False
    assert "PipeWire" in probe["systemAudio"]["reason"]


def test_refuse_no_longer_blocks_system_both_when_available(monkeypatch):
    monkeypatch.setattr(_linux_audio, "available", lambda: True)
    assert _linux.refuse("audio", {"audio": "system"}) is None
    assert _linux.refuse("audio", {"audio": "both"}) is None
    assert _linux.refuse("audio", {"audio": "mic"}) is None


def test_refuse_blocks_system_both_when_unavailable(monkeypatch):
    monkeypatch.setattr(_linux_audio, "available", lambda: False)
    monkeypatch.setattr(_linux_audio, "unavailable_reason", lambda: "no pipewire here")
    assert _linux.refuse("audio", {"audio": "system"}) == "no pipewire here"
    assert _linux.refuse("audio", {"audio": "both"}) == "no pipewire here"
    # mic still works via the `_sink` fallback, so it is never refused here.
    assert _linux.refuse("audio", {"audio": "mic"}) is None


def test_refuse_blocks_a_chosen_microphone_when_native(monkeypatch):
    monkeypatch.setattr(_linux_audio, "available", lambda: True)
    why = _linux.refuse("audio", {"audio": "mic", "device": "usb-1"})
    assert why is not None
    assert "microphone" in why
    assert "system sound settings" in why
    # "system"/"both" refuse on the unavailability check first, but a device
    # on them is just as unsupported once native capture is reachable.
    assert _linux.refuse("audio", {"audio": "both", "device": "usb-1"}) is not None
    # No device named: nothing to refuse.
    assert _linux.refuse("audio", {"audio": "mic"}) is None
    assert _linux.refuse("audio", {"audio": "mic", "device": None}) is None


def test_refuse_blocks_a_wrong_extension_path_when_native(monkeypatch):
    monkeypatch.setattr(_linux_audio, "available", lambda: True)
    why = _linux.refuse("audio", {"audio": "mic", "out": "/tmp/x.mp3"})
    assert why is not None
    assert ".wav" in why
    assert _linux.refuse("audio", {"audio": "mic", "out": "/tmp/x.wav"}) is None
    # No path given: nothing to check.
    assert _linux.refuse("audio", {"audio": "mic"}) is None


def test_refuse_ignores_device_when_falling_back_to_sink(monkeypatch):
    # Unavailable native capture means "mic" runs through `_sink`, which does
    # honour a chosen `device` (the browser's own picker) — so this refusal
    # must not fire in that case.
    monkeypatch.setattr(_linux_audio, "available", lambda: False)
    monkeypatch.setattr(_linux_audio, "unavailable_reason", lambda: "no pipewire here")
    assert _linux.refuse("audio", {"audio": "mic", "device": "usb-1"}) is None


def test_refuse_checks_extension_when_falling_back_to_sink(monkeypatch):
    # Unavailable native capture means "mic" runs through `_sink`, which writes
    # whatever container the page's MediaRecorder picked (`.webm` by default) —
    # a `path` with a mismatched extension must still be refused there, the
    # same as the native path refuses one against `.wav`.
    monkeypatch.setattr(_linux_audio, "available", lambda: False)
    monkeypatch.setattr(_linux_audio, "unavailable_reason", lambda: "no pipewire here")
    why = _linux.refuse("audio", {"audio": "mic", "out": "/tmp/x.mp3"})
    assert why is not None
    assert ".webm" in why
    assert _linux.refuse("audio", {"audio": "mic", "out": "/tmp/x.webm"}) is None


def test_ext_refuse_start_audio_agree_despite_availability_flip(monkeypatch):
    """`ext()`, `refuse()` and `start_audio()` must not each probe
    `_linux_audio.available()` on their own: if the PipeWire/pulse socket
    appeared or vanished between those three calls, probing independently
    would let the extension, the refusal and the backend disagree about
    whether this recording is native. They are threaded the same `spec` dict
    (as `__init__.start` does) and must all go with whatever the FIRST probe
    said, even though `available()` here flips on every call after that.
    """
    flips = iter([True, False, True, False, True, False])
    monkeypatch.setattr(_linux_audio, "available", lambda: next(flips))
    monkeypatch.setattr(_linux_audio, "unavailable_reason", lambda: "no pipewire here")
    sentinel = object()
    monkeypatch.setattr(_linux_audio, "start", lambda out, spec: sentinel)

    # First probe says "available" -> every later call must agree, even
    # though the mock would answer False, True, False... from here on.
    spec = {"audio": "both"}
    assert _linux.ext("audio", spec) == ".wav"
    assert _linux.refuse("audio", spec) is None
    assert _linux.start_audio("/tmp/whatever.wav", spec) is sentinel

    # A fresh spec re-probes once (a new recording), and this time the first
    # probe says "unavailable" -> ext, refuse and start_audio all agree on
    # THAT, again regardless of what later calls to available() would say.
    spec2 = {"audio": "both"}
    assert _linux.ext("audio", spec2) != ".wav"
    assert _linux.refuse("audio", spec2) == "no pipewire here"


# ------------------------------------------------------------------- recording


def test_mic_produces_a_valid_wav(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "_pasimple",
                        lambda: fake_pasimple({None: silence}))
    out = tmp_path / "mic.wav"
    handle = _linux_audio.start(str(out), {"audio": "mic"})
    time.sleep(0.05)
    _linux_audio.stop(handle)

    channels, rate, pcm = _wav_frames(out)
    assert channels == 1
    assert rate == RATE
    assert len(pcm) > 0


def test_system_produces_a_valid_wav_with_sine(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "_pasimple",
                        lambda: fake_pasimple({_linux_audio.MONITOR_DEVICE: sine()}))
    out = tmp_path / "system.wav"
    handle = _linux_audio.start(str(out), {"audio": "system"})
    time.sleep(0.05)
    _linux_audio.stop(handle)

    channels, rate, pcm = _wav_frames(out)
    assert channels == 1
    assert rate == RATE
    assert _rms(pcm) > 1000
    assert handle.heard_system is True


def test_both_mixes_mic_into_the_monitor_master_clock(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({
        None: sine(freq=220.0, amp=0.2),
        _linux_audio.MONITOR_DEVICE: silence,
    }))
    out = tmp_path / "both.wav"
    handle = _linux_audio.start(str(out), {"audio": "both"})
    time.sleep(0.05)
    _linux_audio.stop(handle)

    channels, rate, pcm = _wav_frames(out)
    assert channels == 1
    assert rate == RATE
    # the monitor is silent, but the mic's tone still comes through the mix.
    assert _rms(pcm) > 1000
    # and because the monitor itself never had anything on it, that is flagged.
    assert handle.heard_system is False


def test_both_closes_the_mic_stream_when_the_monitor_open_raises(monkeypatch, tmp_path):
    """A fake `PaSimple` whose MONITOR open raises: the mic stream was
    already open at that point, so `_start`'s `BaseException` handler must
    find it in `handle._streams` and close it — not leak it to PipeWire."""
    opened: list = []

    class _RaisingPaSimple:
        def __init__(self, direction, fmt, channels, rate, app_name="python",
                     stream_name=None, server_name=None, device_name=None,
                     maxlength=-1, tlength=-1, prebuf=-1, minreq=-1, fragsize=-1):
            if device_name == _linux_audio.MONITOR_DEVICE:
                raise RuntimeError("the monitor source is gone")
            self._stream = FakeStream(silence)
            opened.append(self)

        def read(self, num_bytes):
            return self._stream.read(num_bytes)

        def close(self):
            self._stream.close()

    class _Module:
        PA_STREAM_RECORD = 1
        PA_SAMPLE_FLOAT32LE = 3
        PaSimple = _RaisingPaSimple

    monkeypatch.setattr(_linux_audio, "_pasimple", lambda: _Module())
    out = tmp_path / "both.wav"
    with pytest.raises(RuntimeError, match="monitor source is gone"):
        _linux_audio.start(str(out), {"audio": "both"})

    assert len(opened) == 1
    assert opened[0]._stream.closed is True


def test_pause_drops_samples(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "_pasimple",
                        lambda: fake_pasimple({None: sine()}))

    unpaused_out = tmp_path / "unpaused.wav"
    h1 = _linux_audio.start(str(unpaused_out), {"audio": "mic"})
    time.sleep(0.05)
    _linux_audio.stop(h1)
    _, _, unpaused_pcm = _wav_frames(unpaused_out)

    paused_out = tmp_path / "paused.wav"
    h2 = _linux_audio.start(str(paused_out), {"audio": "mic"})
    _linux_audio.pause(h2)
    assert h2.last_level == 0.0
    time.sleep(0.05)
    _linux_audio.resume(h2)
    _linux_audio.stop(h2)
    _, _, paused_pcm = _wav_frames(paused_out)

    assert len(paused_pcm) < len(unpaused_pcm) / 2


def test_warning_on_silent_monitor(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "_WARN_AFTER_S", 0.01)
    monkeypatch.setattr(_linux_audio, "_pasimple",
                        lambda: fake_pasimple({_linux_audio.MONITOR_DEVICE: silence}))
    out = tmp_path / "quiet.wav"
    handle = _linux_audio.start(str(out), {"audio": "system"})
    time.sleep(0.05)
    _linux_audio.stop(handle)

    warning = _linux_audio.warning(handle)
    assert warning is not None
    assert "no system audio" in warning


def test_no_warning_when_audio_present(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "_WARN_AFTER_S", 0.01)
    monkeypatch.setattr(_linux_audio, "_pasimple",
                        lambda: fake_pasimple({_linux_audio.MONITOR_DEVICE: sine()}))
    out = tmp_path / "loud.wav"
    handle = _linux_audio.start(str(out), {"audio": "system"})
    time.sleep(0.05)
    _linux_audio.stop(handle)

    assert _linux_audio.warning(handle) is None


def test_warning_never_applies_to_mic_only(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "_WARN_AFTER_S", 0.01)
    monkeypatch.setattr(_linux_audio, "_pasimple",
                        lambda: fake_pasimple({None: silence}))
    out = tmp_path / "mic_silent.wav"
    handle = _linux_audio.start(str(out), {"audio": "mic"})
    time.sleep(0.05)
    _linux_audio.stop(handle)

    assert _linux_audio.warning(handle) is None


def test_failure_surfaces_when_a_puller_thread_dies(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "_pasimple", lambda: fake_pasimple({
        None: failing_after(silence, fail_at=2),
    }))
    out = tmp_path / "dies.wav"
    handle = _linux_audio.start(str(out), {"audio": "mic"})
    time.sleep(0.05)

    failure = _linux_audio.failure(handle)
    assert failure is not None
    assert "unexpectedly" in failure

    with pytest.raises(RuntimeError):
        _linux_audio.stop(handle)


def test_can_pause_is_always_true_for_native_handles(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "_pasimple",
                        lambda: fake_pasimple({None: silence}))
    out = tmp_path / "pausable.wav"
    handle = _linux_audio.start(str(out), {"audio": "mic"})
    try:
        assert _linux_audio.can_pause(handle) is True
        assert _linux.can_pause(handle) is True
    finally:
        _linux_audio.stop(handle)


# --------------------------------------------------------------- `_linux` wiring


def test_linux_ext_is_wav_for_native_audio(monkeypatch):
    monkeypatch.setattr(_linux_audio, "available", lambda: True)
    assert _linux.ext("audio", {"audio": "mic"}) == ".wav"


def test_linux_start_audio_delegates_to_linux_audio_when_available(monkeypatch, tmp_path):
    monkeypatch.setattr(_linux_audio, "available", lambda: True)
    monkeypatch.setattr(_linux_audio, "_pasimple",
                        lambda: fake_pasimple({None: silence}))
    out = tmp_path / "wired.wav"
    handle = _linux.start_audio(str(out), {"audio": "mic"})
    try:
        assert isinstance(handle, _linux_audio.Handle)
        assert _linux.failure(handle) is None
        assert _linux.level(handle) >= 0.0
    finally:
        _linux.stop(handle)
