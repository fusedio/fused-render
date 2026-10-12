"""Pause/resume arithmetic for audio recordings, with nothing from Apple.

Kept free of pyobjc so the parts with real arithmetic in them can be tested on
every runner (the split `_mixdown.py` already uses):

- `PauseClock` — recording time that excludes paused time, so neither the
  seconds a page sees nor the `maxSeconds` cap count a pause.
- `PtsShifter` — the ScreenCaptureKit mux route (macOS 13.0-14.1) writes the
  stream's own timestamps; dropping buffers while paused leaves a hole, so every
  kept buffer after a resume is moved back by the gap. The gap is measured from
  the stream's timestamps, not the wall clock, so there is neither silence nor
  overlap at the seam.
- `rms_f32` / `db_to_level` / `amp_to_level` — the level the pill's meter shows.
"""

from __future__ import annotations

import array
import math
import time
from typing import Callable, Optional


class PauseClock:
    """Accounts for time spent paused. Pause/resume are idempotent."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self.paused_total = 0.0
        self.paused_since: Optional[float] = None

    @property
    def paused(self) -> bool:
        return self.paused_since is not None

    def pause(self) -> None:
        if self.paused_since is None:
            self.paused_since = self._clock()

    def resume(self) -> None:
        if self.paused_since is not None:
            self.paused_total += self._clock() - self.paused_since
            self.paused_since = None

    def paused_for(self) -> float:
        """How long the current pause has lasted (0.0 when not paused)."""
        if self.paused_since is None:
            return 0.0
        return self._clock() - self.paused_since

    def elapsed(self, started_at: float) -> float:
        """Recorded seconds: wall time since `started_at` minus paused time."""
        return (self._clock() - started_at - self.paused_total
                - self.paused_for())


class PtsShifter:
    """Closes the hole a pause leaves in a stream of timestamped buffers."""

    def __init__(self) -> None:
        self.paused = False
        self.offset = 0.0
        self.last_end: Optional[float] = None
        self._resume_pending = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            self._resume_pending = True

    def shift(self, pts: float, duration: float) -> Optional[float]:
        """The new presentation time for a buffer, or None to drop it."""
        if self.paused:
            return None
        if self._resume_pending:
            if self.last_end is not None:
                self.offset += max(0.0, pts - self.last_end)
            self._resume_pending = False
        self.last_end = pts + duration
        return pts - self.offset


def rms_f32(data) -> float:
    """RMS of little-endian float32 PCM bytes, clamped to 1.0."""
    usable = len(data) - len(data) % 4
    if usable <= 0:
        return 0.0
    samples = array.array("f")
    samples.frombytes(bytes(data[:usable]))
    if not samples:
        return 0.0
    total = math.fsum(x * x for x in samples)
    return min(1.0, math.sqrt(total / len(samples)))


def db_to_level(db: float, floor: float = -50.0) -> float:
    """Decibels (<= 0) to a 0..1 meter level; `floor` and below is 0."""
    return max(0.0, min(1.0, (db - floor) / -floor))


def amp_to_level(rms: float) -> float:
    """Linear amplitude to a 0..1 meter level, on the same -50 dB scale."""
    return db_to_level(20.0 * math.log10(max(rms, 1e-6)))
