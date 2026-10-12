"""Live macOS audio-only capture smoke — opt in with FUSED_CAPTURE_LIVE=1.

Drives `_darwin.start_audio` / `stop` against the real Core Audio / ScreenCaptureKit
stack, so it needs a Mac and the permissions the route asks for (System Audio
Recording for the tap, Screen Recording for the 13.0-14.1 fallback, Microphone
for "both"). It must stay out of CI, where there is no audio hardware and a
permission prompt would hang the run.

A SILENT file passes: that is the mechanics (a playable .m4a of the right length
came out). Whether it was audible is reported, not asserted — the tap cannot
tell a denied permission from an idle Mac — via `heard_system` and the peak.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("FUSED_CAPTURE_LIVE") != "1",
    reason="live macOS capture: set FUSED_CAPTURE_LIVE=1 on a Mac")


def _peak(path: str) -> float:
    """Peak level 0..1, decoded by `afconvert` to 16-bit WAV.

    Not read through pyobjc (`AVAudioFile` + `floatChannelData`): that pointer
    array has no declared length, and indexing it segfaults the interpreter.
    """
    import array
    import subprocess
    import wave

    wav = path + ".wav"
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16", path, wav],
                   check=True, capture_output=True)
    with wave.open(wav) as w:
        samples = array.array("h", w.readframes(w.getnframes()))
    return max((abs(x) for x in samples), default=0) / 32768.0


def _duration(path: str) -> float:
    import AVFoundation as AVF
    import CoreMedia as CM
    import Foundation

    asset = AVF.AVURLAsset.URLAssetWithURL_options_(
        Foundation.NSURL.fileURLWithPath_(path), None)
    return float(CM.CMTimeGetSeconds(asset.duration()))


@pytest.mark.parametrize("forced", [False, True], ids=["tap", "sck"])
@pytest.mark.parametrize("source", ["system", "both"])
def test_live_audio_only(tmp_path, monkeypatch, source, forced):
    from fused_render.capture import _darwin

    if forced:
        monkeypatch.setenv(_darwin.FORCE_SCK_AUDIO, "1")
    else:
        monkeypatch.delenv(_darwin.FORCE_SCK_AUDIO, raising=False)
        if _darwin._os_version() < (14, 2):
            pytest.skip("no process taps below macOS 14.2")
    out = str(tmp_path / "live.m4a")
    handle = _darwin.start_audio(out, {"audio": source})
    time.sleep(2.2)
    heard = getattr(handle, "heard_system", None)
    _darwin.stop(handle)
    assert _darwin.failure(handle) is None
    assert os.path.getsize(out) > 0
    duration = _duration(out)
    peak = _peak(out)
    print(f"LIVE {source} {'sck' if forced else 'tap'}: duration={duration:.2f}s "
          f"peak={peak:.4f} heard_system={heard}")
    assert duration > 1.0
