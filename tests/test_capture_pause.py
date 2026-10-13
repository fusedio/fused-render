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
                                         db_to_level, rms_f32, rms_pcm)


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


# --------------------------------------------------- the macOS backends (Mac)

_darwin_only = pytest.mark.skipif(sys.platform != "darwin",
                                  reason="the macOS backend")


@_darwin_only
def test_a_paused_tap_discards_its_callbacks_and_resumes():
    from fused_render.capture import _darwin_tap

    handle = _darwin_tap.TapHandle("/tmp/x.m4a", False)
    wrote = []
    handle.write = wrote.append

    class Buf:
        mData = memoryview(f32(0.5, 0.5))

    _darwin_tap.pause(handle)
    handle.on_audio([Buf()])                       # returns before touching AVF
    assert wrote == [] and not handle.heard_system
    assert _darwin_tap.level(handle) == 0.0
    _darwin_tap.resume(handle)
    assert handle.paused is False


@_darwin_only
def test_the_tap_both_mix_upmixes_a_mono_mic_interleaved(monkeypatch):
    """The tap's buffer is interleaved stereo, so a mono mic must be duplicated
    per frame (L=R), not block-wise as the planar mux path does."""
    from fused_render.capture import _darwin_tap

    class FakeBuf:
        def __init__(self, nbytes):
            self.raw = bytearray(nbytes)

        def setFrameLength_(self, n):
            pass

        def mutableAudioBufferList(self):
            raw = self.raw

            class Slot:
                mData = memoryview(raw)

            return [Slot()]

    class FakePCM:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithPCMFormat_frameCapacity_(self, fmt, frames):
            self.buf = FakeBuf(frames * 8)
            made.append(self.buf)
            return self.buf

    class FakeAVF:
        AVAudioPCMBuffer = FakePCM

    made = []
    monkeypatch.setattr(_darwin_tap, "AVF", FakeAVF)
    handle = _darwin_tap.TapHandle("/tmp/x.m4a", True)
    wrote = []
    handle.write = wrote.append

    class Mic:
        mNumberChannels = 1
        mData = memoryview(f32(0.1, 0.2, 0.3))

    class Tap:
        mNumberChannels = 2
        mData = memoryview(f32(0.5, -0.5, 0.25, -0.25, 0.0, 0.0))

    handle.on_audio([Mic(), Tap()])
    got = array.array("f")
    got.frombytes(bytes(made[0].raw))
    assert list(got) == pytest.approx(
        [0.6, -0.4, 0.45, -0.05, 0.3, 0.3])
    assert wrote == [made[0]]


@_darwin_only
def test_the_tap_meter_only_runs_while_somebody_polls():
    from fused_render.capture import _darwin_tap

    handle = _darwin_tap.TapHandle("/tmp/x.m4a", False)
    handle._meter(f32(0.5, 0.5))
    assert handle.last_level == 0.0                # never polled: no work
    assert _darwin_tap.level(handle) == 0.0        # this poll arms the meter
    handle._meter(f32(0.5, 0.5))
    assert _darwin_tap.level(handle) == pytest.approx(amp_to_level(0.5))
    _darwin_tap.pause(handle)
    assert _darwin_tap.level(handle) == 0.0


@_darwin_only
def test_darwin_can_pause_dispatches_by_handle_kind():
    from fused_render.capture import _darwin, _darwin_mux, _darwin_tap

    assert _darwin.can_pause(_darwin._AudioHandle(object(), "/tmp/x.m4a"))
    assert _darwin.can_pause(_darwin_tap.TapHandle("/tmp/x.m4a", False))
    assert _darwin.can_pause(
        _darwin_mux.MuxHandle("/tmp/x.m4a", "system", audio_only=True))
    assert not _darwin.can_pause(_darwin_mux.MuxHandle("/tmp/x.mp4", "system"))
    assert not _darwin.can_pause(object())
    with pytest.raises(RuntimeError, match="screen recordings"):
        _darwin.pause(_darwin_mux.MuxHandle("/tmp/x.mp4", "system"))
    with pytest.raises(RuntimeError, match="screen recordings"):
        _darwin.resume(object())


@_darwin_only
def test_darwin_pause_resume_level_on_the_microphone_recorder():
    from fused_render.capture import _darwin

    class Recorder:
        def __init__(self):
            self.calls = []
            self.record_ok = True
            self.power = -25.0

        def pause(self):
            self.calls.append("pause")

        def record(self):
            self.calls.append("record")
            return self.record_ok

        def updateMeters(self):
            self.calls.append("meters")

        def averagePowerForChannel_(self, ch):
            return self.power

    rec = Recorder()
    handle = _darwin._AudioHandle(rec, "/tmp/x.m4a")
    assert _darwin.level(handle) == pytest.approx(0.5)
    _darwin.pause(handle)
    assert handle.paused and rec.calls[-1] == "pause"
    assert _darwin.level(handle) == 0.0
    rec.record_ok = False
    with pytest.raises(RuntimeError, match="did not resume"):
        _darwin.resume(handle)
    assert handle.paused                          # still paused: it did not resume
    rec.record_ok = True
    _darwin.resume(handle)
    assert not handle.paused

    def boom(ch):
        raise RuntimeError("no meters")

    rec.averagePowerForChannel_ = boom
    assert _darwin.level(handle) == 0.0           # never raises


@_darwin_only
def test_darwin_dispatch_reaches_the_tap_and_mux_hooks(monkeypatch):
    from fused_render.capture import _darwin, _darwin_mux, _darwin_tap

    tap = _darwin_tap.TapHandle("/tmp/x.m4a", False)
    _darwin.pause(tap)
    assert tap.paused
    _darwin.resume(tap)
    assert not tap.paused
    mux = _darwin_mux.MuxHandle("/tmp/x.m4a", "both", audio_only=True)
    mux.ring.push(b"\x00" * 64)
    _darwin.pause(mux)
    assert mux.shifter.paused and len(mux.ring) == 0       # stale mic dropped
    assert _darwin.level(mux) == 0.0
    _darwin.resume(mux)
    assert not mux.shifter.paused


@_darwin_only
def test_a_paused_mux_discards_audio_before_the_writer():
    from fused_render.capture import _darwin_mux

    mux = _darwin_mux.MuxHandle("/tmp/x.m4a", "system", audio_only=True)
    appended = []

    class Input:
        def isReadyForMoreMediaData(self):
            return True

        def appendSampleBuffer_(self, sbuf):
            appended.append(sbuf)
            return True

    mux.audio_input = Input()
    mux.shifter.pause()
    mux._append(mux.audio_input, object(), video=False)   # would blow up if reached
    mux.on_system_audio(object())                          # returns before anything
    mux.on_mic(object())
    assert appended == [] and not mux._started


def _real_audio_buffer(start_s: float, frames: int = 256, rate: int = 44100):
    """A genuine CMSampleBuffer of mono float32 silence at `start_s`.

    Built through CoreMedia (not faked) because the thing being proved is how
    pyobjc marshals the timing-info array in `_shifted`."""
    import CoreMedia as CM
    from CoreAudio import AudioStreamBasicDescription

    asbd = AudioStreamBasicDescription(float(rate), 0x6C70636D, 0x29, 4, 1, 4, 1,
                                       32, 0)
    status, fmt = CM.CMAudioFormatDescriptionCreate(None, asbd, 0, None, 0, None,
                                                    None, None)
    assert status == 0
    size = frames * 4
    status, block = CM.CMBlockBufferCreateWithMemoryBlock(
        None, None, size, None, None, 0, size,
        CM.kCMBlockBufferAssureMemoryNowFlag, None)
    assert status == 0
    CM.CMBlockBufferReplaceDataBytes(bytes(size), block, 0, size)
    pts = CM.CMTimeMake(int(start_s * rate), rate)
    status, sbuf = CM.CMAudioSampleBufferCreateReadyWithPacketDescriptions(
        None, block, fmt, frames, pts, None, None)
    assert status == 0
    return sbuf


@_darwin_only
def test_a_resumed_mux_retimes_real_buffers_back_over_the_gap():
    import CoreMedia as CM
    from fused_render.capture import _darwin_mux

    mux = _darwin_mux.MuxHandle("/tmp/x.m4a", "system", audio_only=True)
    first = _real_audio_buffer(100.0)
    assert mux._shifted(first) is first              # nothing paused: untouched
    end = 100.0 + 256 / 44100
    mux.shifter.pause()
    assert mux._shifted(_real_audio_buffer(101.0)) is None
    mux.shifter.resume()
    later = _real_audio_buffer(110.0)
    moved = mux._shifted(later)
    assert moved is not None and moved is not later
    got = CM.CMTimeGetSeconds(CM.CMSampleBufferGetPresentationTimeStamp(moved))
    assert got == pytest.approx(end, abs=1e-3)       # butts against the last end
    again = mux._shifted(_real_audio_buffer(110.0 + 256 / 44100))
    got = CM.CMTimeGetSeconds(CM.CMSampleBufferGetPresentationTimeStamp(again))
    assert got == pytest.approx(end + 256 / 44100, abs=1e-3)
    assert mux.error is None


def test_rms_pcm_silence_and_unsupported():
    for bits, flt in ((32, True), (16, False), (24, False), (32, False)):
        assert rms_pcm(bytes(24), bits, flt) == 0.0
    assert rms_pcm(b"", 16, False) == 0.0
    assert rms_pcm(b"\x01\x02\x03\x04", 8, False) == 0.0     # unsupported width
    assert rms_pcm(b"\x01" * 16, 64, True) == 0.0              # float64 is not handled
    assert rms_pcm(b"\x01" * 16, 24, True) == 0.0


def test_rms_pcm_full_scale_each_format():
    assert rms_pcm(struct.pack("<4f", 1, -1, 1, -1), 32, True) == pytest.approx(1.0)
    assert rms_pcm(struct.pack("<4h", 32767, -32768, 32767, -32768), 16, False) == pytest.approx(1.0, abs=1e-4)
    assert rms_pcm(struct.pack("<2h", 16384, -16384), 16, False) == pytest.approx(0.5)
    full24 = (0x7FFFFF).to_bytes(3, "little") + (-0x800000 & 0xFFFFFF).to_bytes(3, "little")
    assert rms_pcm(full24, 24, False) == pytest.approx(1.0, abs=1e-6)
    assert rms_pcm(struct.pack("<2i", 2**30, -2**30), 32, False) == pytest.approx(0.5)


def test_rms_pcm_odd_trailing_bytes_are_dropped():
    whole = struct.pack("<2h", 16384, -16384)
    assert rms_pcm(whole + b"\x7f", 16, False) == pytest.approx(0.5)
    assert rms_pcm(b"\x7f", 16, False) == 0.0
    assert rms_pcm(b"\x00\x00\x40\x01", 24, False) == pytest.approx(0.5)   # one 3-byte sample + 1 stray


@_darwin_only
def test_a_mic_buffer_racing_a_pause_is_not_pushed_into_the_ring(monkeypatch):
    """`on_mic` checks `paused` before the ring lock; a `pause()` landing in
    between must not leave a stale buffer for the first system buffer after
    the resume."""
    from fused_render.capture import _darwin_mux

    mux = _darwin_mux.MuxHandle("/tmp/x.m4a", "both", audio_only=True)
    monkeypatch.setattr(_darwin_mux.CM, "CMSampleBufferIsValid", lambda s: True)

    def racing_pcm(sbuf):
        mux.shifter.pause()                # the pause lands mid-callback
        return None, bytearray(16), 2

    monkeypatch.setattr(_darwin_mux, "_pcm", racing_pcm)
    mux.on_mic(object())
    assert len(mux.ring) == 0
