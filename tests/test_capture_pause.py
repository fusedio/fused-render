"""Pause/resume for audio recordings — the pure half, tested off a Mac.

`capture/_pause.py` imports nothing from Apple: the clock that excludes paused
time, the timestamp shifter that closes the hole a pause leaves in a muxed
stream, and the level maths behind the recording pill's meter.
"""

from __future__ import annotations

import array
import math
import struct
import sys

import pytest

from fused_render.capture import _pause
from fused_render.capture._pause import (PauseClock, PtsShifter, amp_to_level,
                                         db_to_level, rms_f32)


class Now:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def f32(*v):
    return struct.pack(f"<{len(v)}f", *v)


# ------------------------------------------------------------------ the clock


def test_the_clock_counts_wall_time_when_never_paused():
    now = Now()
    clock = PauseClock(now)
    now.t += 5
    assert clock.elapsed(1000.0) == pytest.approx(5)
    assert not clock.paused


def test_the_clock_freezes_while_paused_and_excludes_it_after():
    now = Now()
    clock = PauseClock(now)
    now.t += 3
    clock.pause()
    now.t += 100
    assert clock.paused
    assert clock.elapsed(1000.0) == pytest.approx(3)
    clock.resume()
    assert clock.elapsed(1000.0) == pytest.approx(3)
    now.t += 2
    assert clock.elapsed(1000.0) == pytest.approx(5)


def test_the_clock_pause_and_resume_are_idempotent():
    now = Now()
    clock = PauseClock(now)
    clock.resume()                       # not paused: nothing happens
    now.t += 1
    clock.pause()
    now.t += 10
    clock.pause()                        # does not restart the pause
    now.t += 10
    clock.resume()
    assert clock.paused_total == pytest.approx(20)
    assert clock.elapsed(1000.0) == pytest.approx(1)


def test_the_clock_accumulates_several_pauses():
    now = Now()
    clock = PauseClock(now)
    for _ in range(3):
        now.t += 1
        clock.pause()
        now.t += 4
        clock.resume()
    assert clock.elapsed(1000.0) == pytest.approx(3)


# ----------------------------------------------------------------- the shifter


def test_the_shifter_is_the_identity_until_paused():
    s = PtsShifter()
    assert s.shift(1.0, 0.5) == pytest.approx(1.0)
    assert s.shift(1.5, 0.5) == pytest.approx(1.5)
    assert s.offset == 0.0


def test_the_shifter_drops_buffers_while_paused():
    s = PtsShifter()
    s.shift(1.0, 0.5)
    s.pause()
    assert s.shift(1.5, 0.5) is None
    assert s.shift(2.0, 0.5) is None


def test_the_shifter_closes_the_gap_after_resume():
    s = PtsShifter()
    s.shift(1.0, 0.5)                    # ends at 1.5
    s.pause()
    s.shift(1.5, 0.5)
    s.resume()
    out = s.shift(10.0, 0.5)             # 8.5 s of wall clock passed
    assert out == pytest.approx(1.5)     # butts up against the previous end
    assert s.offset == pytest.approx(8.5)
    assert s.shift(10.5, 0.5) == pytest.approx(2.0)


def test_the_shifter_accumulates_a_second_cycle():
    s = PtsShifter()
    s.shift(0.0, 1.0)
    s.pause()
    s.resume()
    assert s.shift(5.0, 1.0) == pytest.approx(1.0)   # offset 4
    s.pause()
    s.resume()
    assert s.shift(20.0, 1.0) == pytest.approx(2.0)  # offset 4 + 14 = 18
    assert s.offset == pytest.approx(18.0)


def test_resume_before_any_kept_buffer_adds_no_offset():
    s = PtsShifter()
    s.pause()
    s.resume()
    assert s.shift(7.0, 0.5) == pytest.approx(7.0)
    assert s.offset == 0.0


def test_a_negative_gap_is_clamped():
    s = PtsShifter()
    s.shift(5.0, 1.0)                    # ends at 6.0
    s.pause()
    s.resume()
    assert s.shift(5.5, 1.0) == pytest.approx(5.5)   # overlap: no negative offset
    assert s.offset == 0.0


# ----------------------------------------------------------------- level maths


def test_rms_of_a_constant_signal():
    assert rms_f32(f32(0.5, 0.5, -0.5, -0.5)) == pytest.approx(0.5)


def test_rms_edge_cases():
    assert rms_f32(b"") == 0.0
    assert rms_f32(b"\x00\x00") == 0.0                      # shorter than a sample
    assert rms_f32(f32(0.5) + b"\x01\x02") == pytest.approx(0.5)   # odd tail
    assert rms_f32(f32(4.0, 4.0)) == 1.0                    # clamped
    assert rms_f32(bytearray(f32(0.5))) == pytest.approx(0.5)
    assert rms_f32(memoryview(f32(0.5))) == pytest.approx(0.5)


def test_db_to_level():
    assert db_to_level(-160.0) == 0.0
    assert db_to_level(0.0) == 1.0
    assert db_to_level(10.0) == 1.0
    assert db_to_level(-25.0) == pytest.approx(0.5)


def test_amp_to_level_is_monotonic_and_bounded():
    amps = [0.0, 1e-4, 1e-3, 1e-2, 0.1, 0.5, 1.0]
    levels = [amp_to_level(a) for a in amps]
    assert levels == sorted(levels)
    assert levels[0] == 0.0 and levels[-1] == 1.0
    assert amp_to_level(10 ** (-25 / 20)) == pytest.approx(0.5)
