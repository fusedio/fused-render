"""macOS 14.2+ system audio, audio-only: a Core Audio process tap.

**Why this module exists.** Until now system audio was a property of a SCREEN
recording (`SCStreamConfiguration.capturesAudio`), which means a display stream,
the Screen Recording permission and a purple "recording your screen" indicator
for a page that only wants the other side of a call. macOS 14.2 added the API
that was missing: `CATapDescription` + `AudioHardwareCreateProcessTap` tap the
system's output mix directly. No ScreenCaptureKit, no display, and the
permission is the narrower "System Audio Recording Only" one
(`NSAudioCaptureUsageDescription`). Below 14.2 `_darwin.start_audio` falls back
to `_darwin_mux`'s audio-only stream (Screen Recording permission, 13.0+).

**How it is wired.** A tap is not a device; it only produces audio inside an
aggregate device that lists it. So: tap -> PRIVATE aggregate device -> one
IOProc -> `AVAudioFile` (AAC .m4a). For `source: "both"` the microphone is added
to the SAME aggregate as a sub-device, so mic and system audio arrive in one
callback on one clock and the mix is an add — no ring buffer, no drift
correction, none of `_mixdown`'s machinery beyond its add/clip helpers.

**Things measured on macOS 27, each of which cost a wrong turn:**

  * The `kAudio...Key` constants are `bytes` in pyobjc. Put in the aggregate
    dict as they are, `AudioHardwareCreateAggregateDevice` SEGFAULTS the
    interpreter; they must be `str`. (`_k`)
  * `kAudioAggregateDeviceTapAutoStartKey: True` makes the aggregate deliver
    NOTHING until something is audibly playing — a silent Mac produced a
    zero-length file. `False` gives a steady callback from `AudioDeviceStart`
    on, silence included. (The key's name suggests the opposite; the behaviour
    was measured, not read.) A tap-only aggregate needs no clock sub-device.
  * The IOProc block receives five arguments, the last of which is declared
    `inout`; pyobjc therefore expects the block to RETURN it, and a block that
    returns None kills the process with an uncaught NSException. Every exit
    path returns the last argument, and nothing is allowed to raise.
  * pyobjc returns the `AudioDeviceIOProcID` as an opaque `PyObjCPointer`, and
    its own `AudioDeviceStart`/`Stop`/`DestroyIOProcID` refuse it ("undefined
    argument"). Those three go through ctypes with `pointerAsInteger`; so does
    the one property read (`kAudioTapPropertyFormat`), whose out-parameter
    pyobjc cannot express.
  * The tap's format is interleaved float32, so the `AVAudioFile` must be
    opened with an INTERLEAVED processing format. A deinterleaved file against
    an interleaved buffer fails every write with -50.
  * `AVAudioPCMBuffer.initWithPCMFormat_bufferListNoCopy_deallocator_` accepts
    the IOProc's `AudioBufferList` as pyobjc hands it over, and the list is
    indexable with `mData` as a writable `memoryview`, which is all the "both"
    mix needs. The buffer wraps memory that is only valid inside the callback;
    `writeFromBuffer_error_` is synchronous, so that is enough.

**Permission denial is not detected.** A denied "System Audio Recording Only"
grant does not fail anywhere: the tap just delivers zeros. There is no public
preflight either (`probe` reports `granted: None`). What IS cheap is noticing
that no sample was ever non-zero, and `warning()` reports exactly that after
the fact — as a WARNING, never an error, because an idle Mac produces exact
zeros too and the two cannot be told apart from here.

**No run loop, anywhere**: start and stop are two HTTP requests on two uvicorn
threads. Core Audio calls the IOProc on its own dispatch queue (the shared
ctypes one from `_darwin_mux`), so nothing here needs one.
"""

from __future__ import annotations

import ctypes
import threading
import time
import uuid

import AVFoundation as AVF
import CoreAudio as CA
import Foundation
import objc

from fused_render.capture import _darwin_mux
from fused_render.capture._darwin import _AAC, _QUALITY_HIGH
from fused_render.capture._mixdown import add_clip_into, upmix_mono_to_stereo

#: `AudioHardwareCreateProcessTap` is macOS 14.2.
TAP_MIN = (14, 2)

#: `kAudioFormatFlagIsNonInterleaved`.
_NON_INTERLEAVED = 0x20

#: A recording shorter than this has not been given long enough to hear anything,
#: so it is never warned about.
_WARN_AFTER_S = 1.0

_QUEUE = _darwin_mux._serial_queue(b"io.fused.capture.tap")

_core = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_core.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
for _name in ("AudioDeviceStart", "AudioDeviceStop", "AudioDeviceDestroyIOProcID"):
    getattr(_core, _name).argtypes = [ctypes.c_uint32, ctypes.c_void_p]


class _Asbd(ctypes.Structure):
    _fields_ = [("rate", ctypes.c_double)] + [
        (n, ctypes.c_uint32) for n in
        ("format", "flags", "bytes_per_packet", "frames_per_packet",
         "bytes_per_frame", "channels", "bits", "reserved")]


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32)]


def _k(key) -> str:
    """A pyobjc `kAudio...Key` as the `str` the aggregate dict needs (see header)."""
    return key.decode() if isinstance(key, bytes) else key


def _ok(status: int, what: str) -> None:
    if status != 0:
        raise RuntimeError(f"{what} failed (OSStatus {status})")


class TapHandle:
    """A recording in progress, with everything that must outlive `start`."""

    def __init__(self, path: str, both: bool):
        self.path = path
        self.both = both
        self.error: str | None = None
        self.heard_system = False
        self.started_at = 0.0
        self.tap = 0
        self.aggregate = 0
        self.proc = None
        self.block = None
        self.file = None
        self.fmt = None
        self._lock = threading.Lock()
        self._closing = False

    def note_error(self, message: str) -> None:
        with self._lock:
            if self.error is None:
                self.error = message

    def write(self, buf) -> None:
        with self._lock:
            if self._closing or self.error is not None:
                return
            ok, error = self.file.writeFromBuffer_error_(buf, None)
            if not ok:
                self.error = f"writing the audio failed: {error}"

    def on_audio(self, data) -> None:
        """One IOProc callback. `data` is the input `AudioBufferList`."""
        if not self.both:
            if not self.heard_system:
                self.heard_system = any(data[0].mData.cast("f"))
            buf = AVF.AVAudioPCMBuffer.alloc() \
                .initWithPCMFormat_bufferListNoCopy_deallocator_(self.fmt, data, None)
            if buf is None:
                raise RuntimeError("the tap's buffers do not match its format")
            self.write(buf)
            return
        # The aggregate's input is the microphone's buffer(s) first and the
        # tap's last (measured); one clock, so both hold the same frame count.
        mic, tap = data[0], data[len(data) - 1]
        tap_bytes = bytearray(tap.mData)
        if not self.heard_system:
            self.heard_system = any(memoryview(tap_bytes).cast("f"))
        frames = len(tap_bytes) // 8
        mic_bytes = bytes(mic.mData)
        channels = max(1, int(mic.mNumberChannels))
        if channels == 1:
            mic_bytes = bytes(upmix_mono_to_stereo(mic_bytes))
        elif channels > 2:
            mic_bytes = _first_two(mic_bytes, channels)
        mic_bytes = mic_bytes[:frames * 8]
        mix = bytearray(tap_bytes)
        add_clip_into(mix, mic_bytes)
        buf = AVF.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
            self.fmt, frames)
        buf.setFrameLength_(frames)
        # Through the buffer's own `AudioBufferList`: its `mData` is a plain
        # byte memoryview. (`floatChannelData()[0].as_buffer(n)` is the obvious
        # spelling and does not work — the pointer has no declared length, the
        # assignment fails on a format mismatch and reading it segfaults.)
        buf.mutableAudioBufferList()[0].mData[:len(mix)] = bytes(mix)
        self.write(buf)


def _first_two(data: bytes, channels: int) -> bytes:
    """Interleaved float32 with `channels` channels -> its first two."""
    import array

    samples = array.array("f")
    samples.frombytes(data)
    out = array.array("f")
    for i in range(0, len(samples) - channels + 1, channels):
        out.extend(samples[i:i + 2])
    return out.tobytes()


def _tap_format(tap: int) -> _Asbd:
    asbd = _Asbd()
    address = _Address(CA.kAudioTapPropertyFormat,
                       CA.kAudioObjectPropertyScopeGlobal,
                       CA.kAudioObjectPropertyElementMain)
    size = ctypes.c_uint32(ctypes.sizeof(asbd))
    _ok(_core.AudioObjectGetPropertyData(
        tap, ctypes.byref(address), 0, None, ctypes.byref(size),
        ctypes.byref(asbd)), "reading the tap's format")
    return asbd


def _mic_uid(spec: dict) -> str:
    """The microphone's Core Audio UID — which is an `AVCaptureDevice.uniqueID`."""
    wanted = spec.get("device")
    if wanted:
        if AVF.AVCaptureDevice.deviceWithUniqueID_(str(wanted)) is None:
            from fused_render.capture import CaptureError

            raise CaptureError(
                f"no such microphone: {wanted!r} "
                "(sources().microphones lists the ones this Mac has)")
        return str(wanted)
    device = AVF.AVCaptureDevice.defaultDeviceWithMediaType_(AVF.AVMediaTypeAudio)
    if device is None:
        raise RuntimeError("this Mac reports no microphone")
    return str(device.uniqueID())


def start(out: str, spec: dict) -> TapHandle:
    """Record the system's output mix (and, for "both", the microphone) to `out`."""
    both = spec.get("audio") == "both"
    handle = TapHandle(out, both)
    try:
        _start(handle, out, spec)
    except BaseException:
        # A cleanup failure here must not mask the error that got us here
        # (say, "no such microphone").
        _teardown(handle, raise_errors=False)
        raise
    return handle


def _start(handle: TapHandle, out: str, spec: dict) -> None:
    desc = CA.CATapDescription.alloc().initStereoGlobalTapButExcludeProcesses_([])
    desc.setPrivate_(True)
    desc.setMuteBehavior_(CA.CATapUnmuted)
    desc.setName_("Fused Render system audio")
    status, handle.tap = CA.AudioHardwareCreateProcessTap(desc, None)
    _ok(status, "creating the system audio tap")
    handle.tap = int(handle.tap)

    aggregate = {
        _k(CA.kAudioAggregateDeviceUIDKey): str(uuid.uuid4()),
        _k(CA.kAudioAggregateDeviceNameKey): "Fused Render capture",
        _k(CA.kAudioAggregateDeviceIsPrivateKey): True,
        # False, on purpose and against the key's name — see the header.
        _k(CA.kAudioAggregateDeviceTapAutoStartKey): False,
        _k(CA.kAudioAggregateDeviceTapListKey): [{
            _k(CA.kAudioSubTapUIDKey): str(desc.UUID().UUIDString()),
            _k(CA.kAudioSubTapDriftCompensationKey): True}],
    }
    if handle.both:
        mic = _mic_uid(spec)
        aggregate[_k(CA.kAudioAggregateDeviceSubDeviceListKey)] = [
            {_k(CA.kAudioSubDeviceUIDKey): mic}]
        aggregate[_k(CA.kAudioAggregateDeviceMainSubDeviceKey)] = mic
    status, handle.aggregate = CA.AudioHardwareCreateAggregateDevice(aggregate, None)
    _ok(status, "creating the capture device")
    handle.aggregate = int(handle.aggregate)

    asbd = _tap_format(handle.tap)
    interleaved = not (asbd.flags & _NON_INTERLEAVED)
    if handle.both and not interleaved:
        raise RuntimeError("the system audio tap is not interleaved, which "
                           "mixing in the microphone requires")
    channels = int(asbd.channels) or 2
    handle.fmt = AVF.AVAudioFormat.alloc() \
        .initWithCommonFormat_sampleRate_channels_interleaved_(
            1, float(asbd.rate), channels, interleaved)      # 1 = PCMFormatFloat32
    if handle.fmt is None:
        raise RuntimeError("the tap reports a format Core Audio cannot describe")
    settings = {
        AVF.AVFormatIDKey: _AAC,
        AVF.AVSampleRateKey: float(asbd.rate),
        AVF.AVNumberOfChannelsKey: 2,
        AVF.AVEncoderAudioQualityKey: _QUALITY_HIGH,
    }
    handle.file, error = AVF.AVAudioFile.alloc() \
        .initForWriting_settings_commonFormat_interleaved_error_(
            Foundation.NSURL.fileURLWithPath_(out), settings, 1, interleaved, None)
    if handle.file is None:
        raise RuntimeError(f"could not open {out} for writing: {error}")

    def block(now, in_data, in_time, out_data, out_time):
        # Returns the last argument on EVERY path: it is `inout`, and a block that
        # returns None (or raises) takes the whole process down (see the header).
        try:
            handle.on_audio(in_data)
        except Exception as exc:                         # pragma: no cover
            handle.note_error(f"the system audio callback failed: {exc}")
        return out_time

    handle.block = block
    status, proc = CA.AudioDeviceCreateIOProcIDWithBlock(
        None, handle.aggregate, _QUEUE, block)
    _ok(status, "attaching the audio callback")
    handle.proc = ctypes.c_void_p(proc.pointerAsInteger)
    handle.started_at = time.monotonic()
    _ok(_core.AudioDeviceStart(handle.aggregate, handle.proc),
        "starting the capture device")


def _teardown(handle: TapHandle, raise_errors: bool = True) -> None:
    """Undo whatever `start` got through. Every step runs even if one raises, so
    a failed stop never leaks a tap or an aggregate device (they outlive the
    process otherwise)."""
    first: BaseException | None = None

    def step(fn, *args):
        nonlocal first
        try:
            fn(*args)
        except BaseException as exc:                     # noqa: BLE001
            first = first or exc

    if handle.proc is not None and handle.aggregate:
        step(_core.AudioDeviceStop, handle.aggregate, handle.proc)
        step(_core.AudioDeviceDestroyIOProcID, handle.aggregate, handle.proc)
        handle.proc = None
    step(_close_file, handle)
    if handle.aggregate:
        step(CA.AudioHardwareDestroyAggregateDevice, handle.aggregate)
        handle.aggregate = 0
    if handle.tap:
        step(CA.AudioHardwareDestroyProcessTap, handle.tap)
        handle.tap = 0
    if first is not None and raise_errors:
        raise first


def _close_file(handle: TapHandle) -> None:
    with handle._lock:
        handle._closing = True
        file, handle.file = handle.file, None
    if file is None:
        return
    # `close` is macOS 15; before it the file finalises when the last reference
    # goes, which `file = None` and this frame ending is.
    if file.respondsToSelector_(b"close"):
        file.close()


def stop(handle: TapHandle) -> None:
    """End the capture; the .m4a is finalised when this returns."""
    _teardown(handle)
    if handle.error:
        raise RuntimeError(handle.error)


def failure(handle: TapHandle) -> str | None:
    """The callback or write error this recording has ALREADY died of."""
    return handle.error


def warning(handle: TapHandle) -> str | None:
    """A sentence when a recording that ran long enough heard no system audio.

    A warning, not a failure: see the header. Reads state; changes none."""
    if handle.heard_system or not handle.started_at:
        return None
    if time.monotonic() - handle.started_at < _WARN_AFTER_S:
        return None
    return ("no system audio was captured — either nothing was playing, or this "
            "app has not been granted System Audio Recording (System Settings › "
            "Privacy & Security › Screen & System Audio Recording)")
