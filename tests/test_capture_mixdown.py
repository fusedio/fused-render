"""The `audio: "both"` mixdown — the pure half, tested off a Mac.

`capture/_mixdown.py` deliberately imports nothing from Apple so that the part
of the macOS 13–14 recorder with actual ARITHMETIC in it can be tested on every
runner, which is the split D410 already uses for the Windows and Linux stills.
What cannot be tested here is the sample buffers arriving — that lives in
`_darwin_mux` and needs a display, a microphone and an OS two versions behind
this one.

The claim these tests are really defending is the one in the module docstring:
drop/insert does not merely tolerate clock drift, it BOUNDS it. A ring that grew
would be a recording drifting further out of sync every minute, which is what
"some drift is acceptable" must not be allowed to mean.
"""

from __future__ import annotations

import array
import struct
import sys

import pytest

from fused_render.capture import _mixdown
from fused_render.capture._mixdown import (FLOAT, Ring, add_clip_into, peak,
                                           upmix_mono_to_stereo)


def pcm(*values: float) -> bytearray:
    return bytearray(struct.pack(f"<{len(values)}f", *values))


# ------------------------------------------------------------------- the ring


def test_the_ring_hands_back_what_was_pushed_in_order():
    ring = Ring(high_water=1024)
    ring.push(pcm(1.0, 2.0))
    ring.push(pcm(3.0))
    assert ring.take(3 * FLOAT) == pcm(1.0, 2.0, 3.0)
    assert len(ring) == 0
    assert (ring.dropped, ring.padded) == (0, 0)


def test_an_underrun_is_silence_and_is_counted():
    """The microphone is behind — its first buffer has not arrived, or its
    clock is slower. Padding keeps the system stream on time; a shorter buffer
    would shift every sample after it."""
    ring = Ring(high_water=1024)
    ring.push(pcm(1.0))
    out = ring.take(3 * FLOAT)
    assert out == pcm(1.0, 0.0, 0.0)
    assert ring.padded == 2 * FLOAT
    assert ring.dropped == 0


def test_a_backlog_discards_the_oldest_and_is_counted():
    """The microphone is ahead. Dropping the NEWEST would mean the mix is
    permanently listening to the past; dropping the oldest costs one
    discontinuity and puts the two streams back level."""
    ring = Ring(high_water=2 * FLOAT)
    ring.push(pcm(1.0, 2.0))
    ring.push(pcm(3.0, 4.0))
    assert ring.take(2 * FLOAT) == pcm(3.0, 4.0)
    assert ring.dropped == 2 * FLOAT


def test_drift_stays_bounded_over_a_long_recording():
    """THE claim of the design. A microphone running 100 ppm fast for the
    equivalent of an hour must not leave an hour's worth of backlog: the ring
    is capped, so the offset stays inside one high-water mark forever instead
    of growing with the recording."""
    high_water = 200 * FLOAT
    ring = Ring(high_water=high_water)
    block = 100
    fast = block + 1                    # the microphone delivers 1% more
    for _ in range(5_000):
        ring.push(bytes(fast * FLOAT))
        ring.take(block * FLOAT)
        assert len(ring) <= high_water
    assert ring.dropped > 0             # corrections happened
    assert ring.padded == 0             # and never in the wrong direction


def test_a_slow_microphone_only_ever_pads():
    ring = Ring(high_water=200 * FLOAT)
    for _ in range(1_000):
        ring.push(bytes(99 * FLOAT))
        ring.take(100 * FLOAT)
    assert ring.padded > 0
    assert ring.dropped == 0
    assert len(ring) == 0


# ------------------------------------------------------------------ the upmix


def test_mono_upmixes_by_duplicating_the_planar_block():
    """Non-interleaved is [all of channel 0][all of channel 1], so the stereo
    signal IS the block twice — no interleaving, no per-sample work."""
    mono = pcm(1.0, 2.0, 3.0)
    assert upmix_mono_to_stereo(bytes(mono)) == mono + mono


# -------------------------------------------------------------------- the mix


def test_mixing_sums_two_streams():
    dst = pcm(0.1, 0.2, -0.3)
    add_clip_into(dst, pcm(0.2, 0.1, 0.1))
    assert peak(dst) == pytest.approx(0.3, abs=1e-6)
    assert struct.unpack("<3f", bytes(dst)) == pytest.approx((0.3, 0.3, -0.2),
                                                             abs=1e-6)


def test_mixing_clips_rather_than_attenuating():
    """A blanket −3 dB would make every `both` recording quieter than the same
    content recorded either way alone. Clipping touches only the samples the
    encoder was going to clip anyway, so a mix that never peaks comes out
    untouched."""
    dst = pcm(0.9, -0.9, 0.25)
    add_clip_into(dst, pcm(0.9, -0.9, 0.25))
    assert struct.unpack("<3f", bytes(dst)) == pytest.approx((1.0, -1.0, 0.5),
                                                             abs=1e-6)


def test_a_short_source_is_padded_not_a_crash():
    """A microphone buffer can be shorter than the system buffer it is mixed
    into — the ring pads, but nothing may depend on that having happened."""
    dst = pcm(0.5, 0.5)
    add_clip_into(dst, pcm(0.25))
    assert struct.unpack("<2f", bytes(dst)) == pytest.approx((0.75, 0.5),
                                                             abs=1e-6)


def test_silence_mixes_to_the_original():
    dst = pcm(0.4, -0.4)
    add_clip_into(dst, _mixdown.silence(2 * FLOAT))
    assert struct.unpack("<2f", bytes(dst)) == pytest.approx((0.4, -0.4),
                                                             abs=1e-6)


@pytest.mark.skipif(sys.platform != "darwin", reason="Accelerate is a Mac framework")
def test_the_accelerate_path_agrees_with_the_reference_implementation():
    """The pure-Python mixer is not a fallback nobody runs — it is the spec the
    vDSP one has to match. If these ever disagree, the fast path is wrong."""
    assert _mixdown._VDSP is not None, "Accelerate should load on a Mac"
    values = [-1.4, -0.6, -0.5, 0.0, 0.25, 0.5, 0.6, 1.4]
    other = [0.9, -0.6, 0.5, 0.0, 0.25, -0.5, 0.6, -1.4]
    fast = bytearray(struct.pack(f"<{len(values)}f", *values))
    slow = bytearray(fast)
    src = bytearray(struct.pack(f"<{len(other)}f", *other))
    _mixdown._add_clip_vdsp(fast, bytearray(src))
    _mixdown._add_clip_python(slow, bytes(src))
    assert struct.unpack(f"<{len(values)}f", bytes(fast)) == pytest.approx(
        struct.unpack(f"<{len(values)}f", bytes(slow)), abs=1e-6)
    assert peak(fast) <= 1.0


def test_the_reference_implementation_clips_on_every_platform():
    """`_add_clip_python` runs where Accelerate does not, so its clamp is what
    keeps a non-Mac reader of this module honest about the contract."""
    dst = bytearray(struct.pack("<2f", 2.0, -2.0))
    _mixdown._add_clip_python(dst, struct.pack("<2f", 1.0, -1.0))
    assert struct.unpack("<2f", bytes(dst)) == (1.0, -1.0)


def test_the_mix_is_in_place_because_the_bytes_go_straight_back():
    """`add_clip_into` writes into `dst`'s own memory: those bytes are handed
    to `CMBlockBufferReplaceDataBytes` without another copy, which is what lets
    `_darwin_mux` append the sample buffer it was given instead of building
    one."""
    dst = pcm(0.1, 0.1)
    view = memoryview(dst)
    add_clip_into(dst, pcm(0.2, 0.2))
    assert struct.unpack("<2f", bytes(view)) == pytest.approx((0.3, 0.3),
                                                              abs=1e-6)


def test_array_module_round_trip_matches_struct():
    """Guards the reference implementation's own plumbing: `array('f')` is
    float32 on every platform this runs on, which is the assumption the whole
    module is built on."""
    a = array.array("f", [0.5, -0.25])
    assert a.itemsize == FLOAT
    assert struct.unpack("<2f", a.tobytes()) == (0.5, -0.25)
