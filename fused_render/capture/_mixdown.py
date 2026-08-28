"""Mixing two live PCM streams into one track, and the drift that forces.

**Only `audio: "both"` on macOS 13–14 needs this.** On 15 `SCRecordingOutput`
takes system audio and the microphone as two stream outputs and mixes them
itself — that mixing is the part of it that does not exist below 15, and this
module is the replacement. `"system"` and `"mic"` alone never come through here:
each has a single source whose buffers go straight to the writer.

**Two sources, two clocks, and no resampler.** System audio rides the output
device's clock and the microphone rides the input device's; a nominal 48 kHz
input really runs at 48000-and-a-bit, so the two produce samples at slightly
different rates forever. The expensive fix is a resampler locked to a master
clock (what a DAW ships). What is here instead is drop/insert against a ring:
the system stream is the master, the microphone is buffered, and each system
buffer pulls the matching number of bytes — padding with silence on an underrun
and discarding the oldest bytes when the ring runs long.

The property that buys is worth stating exactly, because it is better than
"some drift is acceptable" suggests: **the offset does not accumulate.** A
correction happens whenever the ring drifts past its high-water mark, so the
error stays bounded by one buffer rather than growing with the recording. What
is actually being accepted is a rare, faint discontinuity — at a typical ~100
ppm device offset, one correction every few minutes — and not a recording that
is a second out of sync by the end. No crossfade smooths those; that is the
deliberate cheap-and-good-enough line for a path two OS versions will ever run.

**Layout does not matter to the arithmetic and that is load-bearing.** Mixing is
an elementwise add over the whole byte range, so interleaved and planar both
come out right as long as BOTH sources agree — which is why `_darwin_mux`
forces one format on this path and upmixes a mono microphone rather than
letting two layouts meet here.

`add_clip_into` goes through Accelerate's vDSP on macOS because numpy is
`[bundled]` and this backend is core (`pyproject.toml` — a capability that works
only on installs that happen to have numpy is the mistake that file's comments
keep naming), and Accelerate is a system framework that costs nothing to reach
through ctypes. The pure-Python fallback beside it is the reference the fast
path has to match: `tests/test_capture_mixdown.py` asserts the two agree, which
is also what lets every function here be tested off a Mac.
"""

from __future__ import annotations

import array
import ctypes
import ctypes.util
import struct
import sys

#: Bytes per float32 sample. Every stream on this path is float32 PCM by the
#: time it arrives — `_darwin_mux` asks both sources for exactly that.
FLOAT = 4


class Ring:
    """The microphone's bytes, waiting for the system buffer that will mix them.

    Bounded in both directions, which is the whole drift story: `push` discards
    the OLDEST bytes once the backlog passes `high_water` (the microphone is
    running fast, or a system buffer was dropped), and `take` pads with silence
    when there are not enough (the microphone is running slow, or has not
    delivered its first buffer yet). Neither can grow without limit, so a
    four-hour recording holds no more memory than a four-second one.

    Counters, not silence: `dropped` and `padded` record how often each
    correction fired, because a rate that climbs is the only visible symptom of
    a format mismatch this module cannot detect on its own. They are read by
    the tests and by a debugger — NOT by the job row, which is deliberate for
    now: a `both` recording's correction count is diagnostic, and the row
    already carries the number that matters to a user (`dropped_frames`).
    """

    def __init__(self, high_water: int):
        if high_water <= 0:                              # pragma: no cover
            raise ValueError("high_water must be positive")
        self._buf = bytearray()
        self._high = high_water
        self.dropped = 0
        self.padded = 0

    def __len__(self) -> int:
        return len(self._buf)

    def push(self, data: bytes) -> None:
        self._buf += data
        excess = len(self._buf) - self._high
        if excess > 0:
            del self._buf[:excess]
            self.dropped += excess

    def take(self, count: int) -> bytearray:
        """`count` bytes, padded with silence if the microphone is behind."""
        have = min(count, len(self._buf))
        out = bytearray(self._buf[:have])
        del self._buf[:have]
        if have < count:
            self.padded += count - have
            out += bytes(count - have)
        return out


def upmix_mono_to_stereo(data: bytes) -> bytearray:
    """One planar mono channel as two identical planar channels.

    Planar is why this is a concatenation and not an interleave: a non-
    interleaved buffer is [all of channel 0][all of channel 1], so duplicating
    the block IS the stereo signal. A built-in microphone is mono on most Macs
    while ScreenCaptureKit's system audio is stereo, and an elementwise add
    needs both sides the same length — see the module docstring.
    """
    out = bytearray(data)
    out += data
    return out


def _load_vdsp():
    """Accelerate's `vDSP_vadd`/`vDSP_vclip`, or None off a Mac."""
    if sys.platform != "darwin":                         # pragma: no cover
        return None
    name = ctypes.util.find_library("Accelerate")
    if not name:                                         # pragma: no cover
        return None
    try:
        lib = ctypes.CDLL(name)
    except OSError:                                      # pragma: no cover
        return None
    stride, count = ctypes.c_long, ctypes.c_ulong
    ptr = ctypes.POINTER(ctypes.c_float)
    lib.vDSP_vadd.argtypes = [ptr, stride, ptr, stride, ptr, stride, count]
    lib.vDSP_vadd.restype = None
    lib.vDSP_vclip.argtypes = [ptr, stride, ptr, ptr, ptr, stride, count]
    lib.vDSP_vclip.restype = None
    return lib


_VDSP = _load_vdsp()


def _add_clip_python(dst: bytearray, src: bytes) -> None:
    """The reference implementation, and what runs when Accelerate is absent."""
    count = len(dst) // FLOAT
    a = array.array("f")
    a.frombytes(bytes(dst[:count * FLOAT]))
    b = array.array("f")
    b.frombytes(bytes(src[:count * FLOAT]))
    for i in range(count):
        total = a[i] + b[i]
        a[i] = -1.0 if total < -1.0 else (1.0 if total > 1.0 else total)
    dst[:count * FLOAT] = a.tobytes()


def _add_clip_vdsp(dst: bytearray, src: bytearray) -> None:
    count = len(dst) // FLOAT
    floats = ctypes.c_float * count
    # `from_buffer` and not `from_buffer_copy`: both sides are writable
    # bytearrays precisely so the add lands in `dst`'s own memory, which is the
    # memory handed straight back to `CMBlockBufferReplaceDataBytes`.
    a = floats.from_buffer(dst)
    b = floats.from_buffer(src)
    one = ctypes.c_long(1)
    total = ctypes.c_ulong(count)
    _VDSP.vDSP_vadd(a, one, b, one, a, one, total)
    # Clip rather than attenuate. A fixed −3 dB on the sum would make every
    # `both` recording quieter than the same content recorded either way alone;
    # clipping only touches samples that were going to be clipped by the encoder
    # regardless, so a mix that never peaks is bit-identical to an untouched one.
    low = ctypes.c_float(-1.0)
    high = ctypes.c_float(1.0)
    _VDSP.vDSP_vclip(a, one, ctypes.byref(low), ctypes.byref(high), a, one, total)


def add_clip_into(dst: bytearray, src: bytes) -> None:
    """`dst += src`, sample-wise, clamped to [-1, 1]. In place, float32 PCM.

    In place because `dst` is the system-audio buffer's own bytes on their way
    back into the `CMBlockBuffer` they came from: mixing there means the
    recorder appends the sample buffer it was handed, and never has to build a
    `CMSampleBuffer` — which is the single fiddliest thing this backend would
    otherwise have to do through pyobjc.
    """
    if len(src) < len(dst):                              # pragma: no cover
        src = bytearray(src) + bytes(len(dst) - len(src))
    if _VDSP is None:                                    # pragma: no cover
        _add_clip_python(dst, src)
        return
    _add_clip_vdsp(dst, src if isinstance(src, bytearray) else bytearray(src))


def silence(count: int) -> bytes:
    """`count` bytes of float32 silence — which is zero bytes, on every Mac."""
    return bytes(count)


def peak(data: bytes) -> float:
    """Loudest absolute sample, for tests and for nothing else."""
    count = len(data) // FLOAT
    if not count:
        return 0.0
    return max(abs(v) for v in struct.unpack(f"<{count}f", bytes(data[:count * FLOAT])))
