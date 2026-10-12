"""macOS 13–14 screen recording: the muxer that `SCRecordingOutput` would be.

**Why this module exists.** `SCRecordingOutput` — the API that writes the .mov
for `_darwin.py` without a single sample buffer passing through Python — is
macOS 15. Below it `SCStream` still delivers frames and system audio (12.3 and
13.0 respectively), so what is missing is not the capture but the WRITER. This
module supplies one: an `AVAssetWriter` with a video input and an audio input,
fed from an `SCStreamOutput` delegate.

**What that costs, stated plainly, because it reverses the property D409 was
proud of.** Sample buffers DO pass through Python here — one callback per video
frame and per audio packet. They are not touched: the delegate checks a status
attachment and calls `appendSampleBuffer_`, so no pixels are read and the CPU
cost is a bridge crossing, not an encode. The exception is `audio: "both"`,
which reads and rewrites the audio bytes (`_mixdown`), because two sources have
to become one track and below 15 nothing else will do it.

The real risk is not throughput but LATENCY: every callback needs the GIL, and
`SCStream` drops buffers whose handler is slow. User Python and AI inference
both run out of process (`_child.py`, `ai/supervisor.py`), so the in-process
competition is uvicorn's I/O and the index scanner's `os.walk` bursts. Against
that: `queueDepth` is raised to its maximum, and every drop is COUNTED and
surfaced rather than lost quietly — `dropped_frames` on the handle is what a
reader should look at before believing a recording is fine.

**Three audio modes are three different data paths**, and only one of them is
hard:

  * `"system"` — ScreenCaptureKit's audio buffers go straight to the writer.
  * `"mic"` — an `AVCaptureSession` + `AVCaptureAudioDataOutput` delivers
    `CMSampleBuffer`s that go straight to the writer too, with no byte surgery.
    The FORMAT is still requested rather than taken as it comes (`_start_mic`),
    because most built-in microphones are mono and the writer's input is
    stereo. `device` selection works here exactly as `microphoneCaptureDeviceID`
    works on 15.
  * `"both"` — the only path that mixes, and the only one that forces a format
    (48 kHz float32 on both sides) so that mixing is an elementwise add. See
    `_mixdown` for the ring and the drift it corrects.

**`AVCaptureAudioDataOutput`, never `AVCaptureAudioFileOutput`.** D409 records
in detail how the FILE output hung every `stop()` forever: `stopRecording`
marshals with `performSelector:onThread:waitUntilDone:YES` and this app has no
run loop on the thread it wants. A DATA output has no such call — it delivers to
a dispatch queue and is stopped with `AVCaptureSession.stopRunning`, which
blocks briefly but marshals nowhere. That is the reason the microphone can come
back here at all.

**No new dependency for the dispatch queues.** `SCStream` and
`AVCaptureAudioDataOutput` both want a serial `dispatch_queue_t` and pyobjc's
libdispatch bindings are not installed (and would be a core dep for two calls).
`dispatch_queue_create` through ctypes on libSystem returns a pointer that
`objc.objc_object` wraps into an `OS_dispatch_queue_serial`, which is what both
selectors take — the same ctypes-for-one-platform-call posture `_windows.py`
already uses. The queues are created ONCE at module scope and never released:
the ownership semantics of a ctypes-made object behind a pyobjc proxy are not
worth discovering per recording.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time

import objc
import AVFoundation as AVF
import CoreMedia as CM
import Foundation
import ScreenCaptureKit as SCK

from fused_render.capture import _pause
from fused_render.capture._darwin import _AAC, _Wait
from fused_render.capture._mixdown import Ring, add_clip_into, upmix_mono_to_stereo

#: `kAudioFormatLinearPCM` — a FourCC ('lpcm'), which pyobjc does not name.
_LPCM = 1819304813

#: `SCStreamOutputType`. Screen is 0 and audio is 1; microphone (2) is macOS 15
#: and is exactly what this module exists to work around.
_TYPE_SCREEN = 0
_TYPE_AUDIO = 1

#: The one audio format both sources are forced to on the `"both"` path, so the
#: mix is an add. 48 kHz is ScreenCaptureKit's own rate — moving IT would mean
#: resampling the master, which is the work this design skips.
_RATE = 48000.0
_CHANNELS = 2
_SYSTEM_BYTES_PER_FRAME = _CHANNELS * 4

#: How much microphone audio may back up before the oldest is discarded — 200 ms.
#: Small enough that a correction is a click rather than a repeated phrase, and
#: large enough to absorb ordinary jitter between two dispatch queues.
_RING_MS = 200

#: `SCStreamConfiguration.queueDepth`'s documented maximum. Raised from the
#: default 3 because every frame here waits on the GIL — see the header.
_QUEUE_DEPTH = 8

#: How long `stop()` waits for `finishWriting` — the call that appends the
#: `moov` atom, without which the .mov does not play.
FINISH_S = 30.0


def _serial_queue(label: bytes):
    """A serial `dispatch_queue_t` as something pyobjc will pass along."""
    lib = ctypes.CDLL(ctypes.util.find_library("System"))
    lib.dispatch_queue_create.restype = ctypes.c_void_p
    lib.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    handle = lib.dispatch_queue_create(label, None)
    if not handle:                                       # pragma: no cover
        raise RuntimeError("could not create a dispatch queue")
    return objc.objc_object(c_void_p=ctypes.c_void_p(handle))


# Created once for the life of the process, deliberately (see the header).
# Three rather than one so a slow video append cannot delay an audio packet —
# the writer is guarded by a lock, the queues are not a serialisation point.
_SCREEN_Q = _serial_queue(b"io.fused.capture.mux.screen")
_AUDIO_Q = _serial_queue(b"io.fused.capture.mux.audio")
_MIC_Q = _serial_queue(b"io.fused.capture.mux.mic")


# --------------------------------------------------------------- sample buffers


def _is_complete(sbuf) -> bool:
    """Whether this screen frame actually carries an image.

    ScreenCaptureKit emits sample buffers for frames it did NOT redraw — an
    idle screen produces a steady trickle of them — and their status says so
    while their pixel buffer is empty. Appending one is the difference between
    a recording and a one-frame movie, and nothing in the API makes it obvious;
    it is the single most common way this whole approach is got wrong.
    """
    attachments = CM.CMSampleBufferGetSampleAttachmentsArray(sbuf, False)
    if not attachments:
        # No attachments at all is not a failure — treat it as usable, since
        # the alternative is silently discarding every frame on a future OS
        # that stops setting them.
        return True
    status = attachments[0].get(SCK.SCStreamFrameInfoStatus)
    if status is None:
        return True
    return int(status) == SCK.SCFrameStatusComplete


def _pcm(sbuf):
    """`(block buffer, bytes, frames)` for an audio sample buffer.

    The bytes are a COPY: `CMBlockBufferGetDataPointer` is unusable from pyobjc
    (it marshals the memory into a tuple of ints rather than handing back a
    writable view), so the round trip on the mixing path is
    `CMBlockBufferCopyDataBytes` out and `CMBlockBufferReplaceDataBytes` back.
    """
    block = CM.CMSampleBufferGetDataBuffer(sbuf)
    if block is None:
        return None, None, 0
    length = CM.CMBlockBufferGetDataLength(block)
    if not length:
        return None, None, 0
    status, data = CM.CMBlockBufferCopyDataBytes(block, 0, length, None)
    if status != 0 or data is None:                      # pragma: no cover
        return None, None, 0
    frames = int(CM.CMSampleBufferGetNumSamples(sbuf)) or 0
    return block, bytearray(data), frames


#: A system buffer whose samples all stay under this counts as silence. SCK
#: hands float32 PCM (the same assumption the mixing path makes).
_HEARD_PEAK = 1e-4
#: How many buffers are inspected before giving up on hearing anything — the
#: check copies bytes inside the callback, so it stops at the first loud one.
_PEEK_BUFFERS = 200
#: Shorter than this a "nothing heard" says nothing, so no warning.
_WARN_AFTER_S = 1.0


def _peak_f32(data) -> float:
    """The largest absolute sample in little-endian float32 PCM bytes."""
    import array

    usable = len(data) - len(data) % 4
    if usable <= 0:
        return 0.0
    samples = array.array("f")
    samples.frombytes(bytes(data[:usable]))
    return max((abs(x) for x in samples), default=0.0)


# -------------------------------------------------------------------- delegates


class _ScreenOutput(Foundation.NSObject,
                    protocols=[objc.protocolNamed("SCStreamOutput")]):
    """`SCStreamOutput` — every frame and every system-audio packet lands here."""

    def initWithRecorder_(self, recorder):
        self = objc.super(_ScreenOutput, self).init()
        if self is None:                                 # pragma: no cover
            return None
        self._recorder = recorder
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, kind):
        try:
            if kind == _TYPE_SCREEN:
                self._recorder.on_video(sbuf)
            elif kind == _TYPE_AUDIO:
                self._recorder.on_system_audio(sbuf)
        except Exception as exc:                         # pragma: no cover
            # A raise here unwinds into a dispatch queue where nothing catches
            # it. Record it and let the watchdog end the recording instead.
            self._recorder.note_error(f"the recorder failed: {exc}")


class _StreamWatch(Foundation.NSObject,
                   protocols=[objc.protocolNamed("SCStreamDelegate")]):
    """The only thing that reports a stream dying mid-recording on this path.

    On macOS 15 `SCRecordingOutput`'s delegate carries that news and
    `_darwin.failure` reads it. There is no recording output here, so without
    this a display that went away or a revoked permission would tick "Recording"
    to the cap over a file nothing is writing — the exact failure D409 added the
    per-tick `failure(handle)` hook to prevent.
    """

    def initWithRecorder_(self, recorder):
        self = objc.super(_StreamWatch, self).init()
        if self is None:                                 # pragma: no cover
            return None
        self._recorder = recorder
        return self

    def stream_didStopWithError_(self, stream, error):
        self._recorder.note_error(
            str(error.localizedDescription()) if error is not None
            else "the capture stream stopped")


class _MicOutput(Foundation.NSObject, protocols=[
        objc.protocolNamed("AVCaptureAudioDataOutputSampleBufferDelegate")]):
    """The microphone, on `"mic"` and on `"both"`."""

    def initWithRecorder_(self, recorder):
        self = objc.super(_MicOutput, self).init()
        if self is None:                                 # pragma: no cover
            return None
        self._recorder = recorder
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(
            self, output, sbuf, connection):
        try:
            self._recorder.on_mic(sbuf)
        except Exception as exc:                         # pragma: no cover
            self._recorder.note_error(f"the microphone failed: {exc}")


# -------------------------------------------------------------------- recorder


class MuxHandle:
    """A recording in progress, and everything that must outlive `start`.

    Strong references throughout, for the reason `_darwin.start_screen` already
    documents about its delegate: pyobjc holds none for the ObjC side, and a
    collected delegate takes the callbacks — and so the recording — with it.
    """

    def __init__(self, path: str, audio: str | None, *, audio_only: bool = False):
        self.path = path
        self.audio = audio
        # No video track at all (the 13.0-14.1 route for audio-only system
        # audio). The stream still has to deliver a display, so frames keep
        # arriving; they are ignored, and the writer's session starts on the
        # first AUDIO buffer instead.
        self.audio_only = audio_only
        self.mixing = audio == "both"
        self.error: str | None = None
        self.dropped_frames = 0
        self.dropped_audio = 0
        # For `warning`: how many system buffers arrived, and whether any of the
        # first few carried sound. `started_at` is set once the stream runs.
        self.system_buffers = 0
        self.system_heard = False
        self.started_at = 0.0
        # Pause (audio-only recordings): while paused the callbacks discard
        # what arrives and the stream keeps running; after a resume every kept
        # audio buffer is retimed back by the gap, so the file has no hole.
        self.shifter = _pause.PtsShifter()
        # The pill's meter: computed only while somebody polled in the last 2 s.
        self.last_level = 0.0
        self.level_polled_at = 0.0

        self.stream = None
        self.session = None
        self.output = None
        self.watch = None
        self.mic_delegate = None
        self.mic_output = None
        self.writer = None
        self.video_input = None
        self.audio_input = None

        self._lock = threading.Lock()
        self._ring_lock = threading.Lock()
        self._closing = False
        self._started = False
        self.ring = Ring(int(_RATE * (_RING_MS / 1000.0)) * _SYSTEM_BYTES_PER_FRAME)

    # ------------------------------------------------------------ bookkeeping

    def note_error(self, message: str) -> None:
        with self._lock:
            if self.error is None:
                self.error = message

    def _append(self, target, sbuf, *, video: bool) -> None:
        """The one place a sample buffer reaches the writer.

        `startSessionAtSourceTime_` rides the FIRST VIDEO frame and audio that
        arrives before it is discarded: the session's start time defines the
        movie's zero, and a buffer stamped before it either fails to append or
        lands as a leading offset that desynchronises everything after.
        """
        with self._lock:
            if self._closing or self.error is not None:
                return
            if not video and self.shifter.paused:
                return
            if not self._started:
                if video == self.audio_only:
                    return
                self.writer.startSessionAtSourceTime_(
                    CM.CMSampleBufferGetPresentationTimeStamp(sbuf))
                self._started = True
            if not video:
                sbuf = self._shifted(sbuf)
                if sbuf is None:
                    return
            if not target.isReadyForMoreMediaData():
                # Backpressure, not an error — the encoder is behind. Counted
                # so it is visible; see the header on why silence is the wrong
                # answer here.
                if video:
                    self.dropped_frames += 1
                else:
                    self.dropped_audio += 1
                return
            if not target.appendSampleBuffer_(sbuf):
                self.error = self._writer_error() or "the writer rejected a sample"

    def _shifted(self, sbuf):
        """The buffer to append after the pause offset: the buffer itself while
        nothing was paused, a retimed copy afterwards, None to drop it.

        Called under `self._lock`. A failed retime is recorded as the
        recording's error rather than appended at a stale timestamp, which
        would put the audio out of place."""
        pts = CM.CMSampleBufferGetPresentationTimeStamp(sbuf)
        dur = CM.CMSampleBufferGetDuration(sbuf)
        seconds = CM.CMTimeGetSeconds(pts)
        length = CM.CMTimeGetSeconds(dur)
        if length != length or length < 0:               # invalid duration
            length = 0.0
        if self.shifter.shift(seconds, length) is None:
            return None
        if self.shifter.offset == 0.0:
            return sbuf
        status, infos, count = CM.CMSampleBufferGetSampleTimingInfoArray(
            sbuf, 0, None, None)
        if count:
            status, infos, count = CM.CMSampleBufferGetSampleTimingInfoArray(
                sbuf, count, None, None)
        if status != 0 or not count:
            self.error = f"could not retime audio after a pause (OSStatus {status})"
            return None
        offset = CM.CMTimeMakeWithSeconds(self.shifter.offset, pts.timescale)
        shifted = []
        for info in infos:
            info.presentationTimeStamp = CM.CMTimeSubtract(
                info.presentationTimeStamp, offset)
            if info.decodeTimeStamp.flags & 1:           # kCMTimeFlags_Valid
                info.decodeTimeStamp = CM.CMTimeSubtract(
                    info.decodeTimeStamp, offset)
            shifted.append(info)
        status, new = CM.CMSampleBufferCreateCopyWithNewTiming(
            None, sbuf, count, shifted, None)
        if status != 0 or new is None:
            self.error = f"could not retime audio after a pause (OSStatus {status})"
            return None
        return new

    def _writer_error(self) -> str | None:
        error = self.writer.error() if self.writer is not None else None
        if error is None:
            return None
        return str(error.localizedDescription())

    # --------------------------------------------------------------- callbacks

    def on_video(self, sbuf) -> None:
        if (self.audio_only or not CM.CMSampleBufferIsValid(sbuf)
                or not _is_complete(sbuf)):
            return
        self._append(self.video_input, sbuf, video=True)

    def _note_system(self, sbuf, data=None) -> None:
        """Count the buffer and, until one is heard, look at its samples.

        Never raises: a parse failure must not cost the recording, so it is
        treated as heard (unknown means no warning)."""
        self.system_buffers += 1
        if self.system_heard or self.system_buffers > _PEEK_BUFFERS:
            return
        try:
            if data is None:
                _block, data, _frames = _pcm(sbuf)
            if data is not None and _peak_f32(data) > _HEARD_PEAK:
                self.system_heard = True
        except Exception:                                # noqa: BLE001
            self.system_heard = True

    def _meter(self, data) -> None:
        if time.monotonic() - self.level_polled_at >= 2.0:
            return
        try:
            if len(data) % 4 == 0:
                self.last_level = _pause.amp_to_level(_pause.rms_f32(data))
        except Exception:                                # noqa: BLE001
            pass

    def _meter_sbuf(self, sbuf) -> None:
        if time.monotonic() - self.level_polled_at >= 2.0:
            return
        try:
            _block, data, _frames = _pcm(sbuf)
            if data is not None:
                self._meter(data)
        except Exception:                                # noqa: BLE001
            pass

    def on_system_audio(self, sbuf) -> None:
        if self.shifter.paused or not CM.CMSampleBufferIsValid(sbuf):
            return
        if not self.mixing:
            self._note_system(sbuf)
            self._meter_sbuf(sbuf)
            self._append(self.audio_input, sbuf, video=False)
            return
        block, data, _frames = _pcm(sbuf)
        if data is None:
            return
        self._note_system(sbuf, data)
        with self._ring_lock:
            mic = self.ring.take(len(data))
        add_clip_into(data, mic)
        self._meter(data)
        # Mutate BEFORE appending, never after: the writer retains the buffer
        # and encodes off it asynchronously, so a rewrite that lands later is a
        # race against the encoder rather than an edit.
        if CM.CMBlockBufferReplaceDataBytes(bytes(data), block, 0, len(data)) != 0:
            return                                       # pragma: no cover
        self._append(self.audio_input, sbuf, video=False)

    def on_mic(self, sbuf) -> None:
        if self.shifter.paused or not CM.CMSampleBufferIsValid(sbuf):
            return
        if not self.mixing:
            # Straight through, in whatever format the device gave — the writer
            # converts. No ring, no rewrite, no format to agree on.
            self._meter_sbuf(sbuf)
            self._append(self.audio_input, sbuf, video=False)
            return
        _block, data, frames = _pcm(sbuf)
        if data is None or not frames:
            return
        # The device may have ignored the stereo request — most built-in
        # microphones are mono. Detected from the buffer itself rather than
        # from an `AudioStreamBasicDescription`, because bytes-per-frame is the
        # only fact the add actually needs.
        if len(data) // frames < _SYSTEM_BYTES_PER_FRAME:
            data = upmix_mono_to_stereo(bytes(data))
        with self._ring_lock:
            self.ring.push(bytes(data))

    # ----------------------------------------------------------- pause/resume

    def pause(self) -> None:
        self.shifter.pause()
        self.last_level = 0.0
        # Mic samples that arrived before the pause would otherwise be mixed
        # into the first system buffer after the resume, seconds out of place.
        with self._ring_lock:
            self.ring.clear()

    def resume(self) -> None:
        self.shifter.resume()

    # ------------------------------------------------------------------- close

    def finish(self) -> None:
        """Mark both inputs done and wait for the `moov` atom to land."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
            started = self._started
        for target in (self.video_input, self.audio_input):
            if target is not None:
                target.markAsFinished()
        if not started:
            # Nothing was ever appended, so there is no session and
            # `finishWriting` would produce an unreadable file. Say so instead.
            self.writer.cancelWriting()
            if self.audio_only:
                raise RuntimeError(
                    "the recording captured no audio — Screen Recording "
                    "permission may have been revoked while it ran")
            raise RuntimeError(
                "the recording captured no frames — the display may have gone "
                "away, or Screen Recording permission was revoked while it ran")
        wait = _Wait("finishing the recording")
        self.writer.finishWritingWithCompletionHandler_(lambda: wait.done(None))
        if not wait.event.wait(FINISH_S):                # pragma: no cover
            raise RuntimeError(
                "the recording did not finish writing — the file may be "
                "incomplete: " + self.path)
        if self.writer.status() == AVF.AVAssetWriterStatusFailed:
            raise RuntimeError("the recording failed: "
                               + (self._writer_error() or "unknown error"))


# ----------------------------------------------------------------------- start


def _video_settings(width: int, height: int) -> dict:
    settings = {
        AVF.AVVideoCodecKey: AVF.AVVideoCodecTypeH264,
        AVF.AVVideoWidthKey: int(width),
        AVF.AVVideoHeightKey: int(height),
    }
    # A screen at native Retina resolution is far past what the default h264
    # bitrate is tuned for, and text is the first thing that goes soft. Both
    # keys are looked up rather than named so a missing one costs the bitrate
    # and not the recording.
    rate_key = getattr(AVF, "AVVideoAverageBitRateKey", None)
    props_key = getattr(AVF, "AVVideoCompressionPropertiesKey", None)
    if rate_key and props_key:
        bitrate = min(40_000_000, max(4_000_000, int(width) * int(height) * 3))
        settings[props_key] = {rate_key: bitrate}
    return settings


def _audio_settings(channels: int) -> dict:
    return {
        AVF.AVFormatIDKey: _AAC,
        AVF.AVSampleRateKey: _RATE,
        AVF.AVNumberOfChannelsKey: int(channels),
        AVF.AVEncoderAudioQualityKey: 96,                # AVAudioQualityHigh
    }


def _mic_device(spec: dict):
    wanted = spec.get("device")
    if wanted:
        device = AVF.AVCaptureDevice.deviceWithUniqueID_(str(wanted))
        if device is None:
            from fused_render.capture import CaptureError

            raise CaptureError(
                f"no such microphone: {wanted!r} "
                "(sources().microphones lists the ones this Mac has)")
        return device
    device = AVF.AVCaptureDevice.defaultDeviceWithMediaType_(AVF.AVMediaTypeAudio)
    if device is None:
        raise RuntimeError("this Mac reports no microphone")
    return device


def _start_mic(handle: MuxHandle, spec: dict) -> None:
    """An `AVCaptureSession` feeding `on_mic`. See the header on why DATA out."""
    device = _mic_device(spec)
    device_input, error = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(
        device, None)
    if device_input is None:
        raise RuntimeError(f"could not open the microphone: {error}")

    session = AVF.AVCaptureSession.alloc().init()
    session.beginConfiguration()
    if not session.canAddInput_(device_input):
        session.commitConfiguration()
        raise RuntimeError(
            "the microphone could not be added to the capture session — "
            "Microphone permission may not have been granted to this app "
            "(System Settings › Privacy & Security › Microphone)")
    session.addInput_(device_input)

    output = AVF.AVCaptureAudioDataOutput.alloc().init()
    # Asked for on BOTH microphone paths, not only the mixing one. `"both"`
    # needs it because the add requires one agreed format — but `"mic"` needs
    # it too, and for a reason that is easy to miss: most built-in microphones
    # are MONO, the writer's audio input is stereo, and a channel-count
    # mismatch is something `appendSampleBuffer_` can refuse on every buffer.
    # `AVCaptureAudioDataOutput` does the conversion itself, so asking here
    # makes the writer's input format deterministic instead of a property of
    # whichever Mac is running.
    settings = {
        AVF.AVFormatIDKey: _LPCM,
        AVF.AVSampleRateKey: _RATE,
        AVF.AVNumberOfChannelsKey: _CHANNELS,
        AVF.AVLinearPCMBitDepthKey: 32,
        AVF.AVLinearPCMIsFloatKey: True,
        AVF.AVLinearPCMIsNonInterleaved: True,
    }
    # Apple's own LPCM dictionaries always carry this one. Looked up rather
    # than named so a future pyobjc that drops it costs the key, not the call.
    big_endian = getattr(AVF, "AVLinearPCMIsBigEndianKey", None)
    if big_endian is not None:
        settings[big_endian] = False
    output.setAudioSettings_(settings)
    delegate = _MicOutput.alloc().initWithRecorder_(handle)
    output.setSampleBufferDelegate_queue_(delegate, _MIC_Q)
    if not session.canAddOutput_(output):                # pragma: no cover
        session.commitConfiguration()
        raise RuntimeError("the microphone output could not be attached")
    session.addOutput_(output)
    session.commitConfiguration()
    session.startRunning()

    handle.session = session
    handle.mic_output = output
    handle.mic_delegate = delegate


def start(out: str, display, config, spec: dict) -> MuxHandle:
    """Record `display` to `out` (.mov) with an `AVAssetWriter`.

    Takes the `SCStreamConfiguration` `_darwin._configure` already built, so
    the rect, cursor and Retina-scale rules stay in ONE place and this module
    only adds what is specific to writing the movie ourselves.
    """
    return _start(out, display, config, spec, audio_only=False)


def start_audio(out: str, display, config, spec: dict) -> MuxHandle:
    """Record only the sound to `out` (.m4a): system audio, or system + mic.

    The macOS 13.0-14.1 route for `capture.audio({source: "system"|"both"})`;
    14.2+ uses a Core Audio process tap (`_darwin_tap`) and needs no display.
    ScreenCaptureKit has no audio-only stream, so this is the screen stream of
    `start` with the picture thrown away: the configuration is shrunk to a 2x2
    frame once a second (the cheapest thing the stream will still deliver), the
    writer gets NO video input and the `.m4a` file type, and `on_video` ignores
    what does arrive. It needs the Screen Recording permission, like `start`.
    """
    config.setWidth_(2)
    config.setHeight_(2)
    config.setMinimumFrameInterval_(CM.CMTimeMake(1, 1))
    return _start(out, display, config, spec, audio_only=True)


def _start(out: str, display, config, spec: dict, *, audio_only: bool) -> MuxHandle:
    audio = spec.get("audio") or None
    handle = MuxHandle(out, audio if audio in ("system", "mic", "both") else None,
                       audio_only=audio_only)

    config.setQueueDepth_(_QUEUE_DEPTH)
    if handle.mixing:
        # Force ScreenCaptureKit's side of the agreement; the microphone's side
        # is set in `_start_mic`.
        config.setSampleRate_(int(_RATE))
        config.setChannelCount_(_CHANNELS)

    url = Foundation.NSURL.fileURLWithPath_(out)
    writer, error = AVF.AVAssetWriter.alloc().initWithURL_fileType_error_(
        url, AVF.AVFileTypeAppleM4A if audio_only else AVF.AVFileTypeQuickTimeMovie,
        None)
    if writer is None:
        raise RuntimeError(f"could not open {out} for writing: {error}")
    handle.writer = writer

    if not audio_only:
        video_input = AVF.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
            AVF.AVMediaTypeVideo,
            _video_settings(int(config.width()), int(config.height())))
        video_input.setExpectsMediaDataInRealTime_(True)
        if not writer.canAddInput_(video_input):         # pragma: no cover
            raise RuntimeError("the writer refused a video input")
        writer.addInput_(video_input)
        handle.video_input = video_input

    if handle.audio is not None:
        audio_input = AVF.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
            AVF.AVMediaTypeAudio, _audio_settings(_CHANNELS))
        audio_input.setExpectsMediaDataInRealTime_(True)
        if not writer.canAddInput_(audio_input):         # pragma: no cover
            raise RuntimeError("the writer refused an audio input")
        writer.addInput_(audio_input)
        handle.audio_input = audio_input

    if not writer.startWriting():
        raise RuntimeError("the writer would not start: "
                           + (handle._writer_error() or "unknown error"))

    content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
        display, [])
    watch = _StreamWatch.alloc().initWithRecorder_(handle)
    stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
        content_filter, config, watch)
    output = _ScreenOutput.alloc().initWithRecorder_(handle)

    ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(
        output, _TYPE_SCREEN, _SCREEN_Q, None)
    if not ok:
        raise RuntimeError(f"could not attach the screen output: {error}")
    if handle.audio in ("system", "both"):
        ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            output, _TYPE_AUDIO, _AUDIO_Q, None)
        if not ok:
            raise RuntimeError(f"could not attach the audio output: {error}")

    handle.stream = stream
    handle.output = output
    handle.watch = watch

    if handle.audio in ("mic", "both"):
        _start_mic(handle, spec)

    started = _Wait("starting the capture")
    stream.startCaptureWithCompletionHandler_(started.done)
    try:
        started.result()
    except Exception:
        stop(handle, keep=False)
        raise
    handle.started_at = time.monotonic()
    return handle


def warning(handle: MuxHandle) -> str | None:
    """Advice when an audio-only system recording heard nothing, else None.

    The 13.0-14.1 route cannot tell "Screen Recording granted after this app
    started" (macOS delivers silence or nothing until a relaunch) from an idle
    Mac, so it says what it saw. Reads state; changes none."""
    if (not handle.audio_only or handle.audio not in ("system", "both")
            or not handle.started_at or handle.system_heard):
        return None
    if time.monotonic() - handle.started_at < _WARN_AFTER_S:
        return None
    if handle.system_buffers == 0:
        return ("no system audio reached the recording — quit and reopen this "
                "app after granting Screen Recording, then try again")
    return ("no system audio was captured — either nothing was playing, or "
            "Screen Recording was granted after this app started (quit and "
            "reopen it, then try again)")


def stop(handle: MuxHandle, *, keep: bool = True) -> None:
    """End the capture and return only once the movie is playable.

    Order matters and is the whole correctness story: the SOURCES stop first so
    no callback is in flight, and only then are the inputs marked finished. The
    other order races a straggling frame against `markAsFinished`, which raises
    out of a dispatch queue where nothing is listening.
    """
    if handle.stream is not None:
        wait = _Wait("stopping the capture")
        handle.stream.stopCaptureWithCompletionHandler_(wait.done)
        try:
            wait.result()
        except RuntimeError:
            # A stream that will not confirm its own stop must not keep us from
            # closing the file — that is the difference between a recording
            # with a missing tail and one that does not play at all.
            pass
    if handle.session is not None:
        handle.session.stopRunning()
    if not keep:
        if handle.writer is not None:
            handle.writer.cancelWriting()
        return
    handle.finish()


def failure(handle: MuxHandle) -> str | None:
    """The error this recording has ALREADY died of, asked every tick."""
    if handle.error:
        return handle.error
    if handle.writer is not None and \
            handle.writer.status() == AVF.AVAssetWriterStatusFailed:
        return handle._writer_error() or "the writer failed"
    return None


def pause(handle: MuxHandle) -> None:
    """Stop appending; the stream and the writer keep running."""
    handle.pause()


def resume(handle: MuxHandle) -> None:
    handle.resume()


def level(handle: MuxHandle) -> float:
    """0..1 loudness of the most recent audio; asking turns metering on."""
    handle.level_polled_at = time.monotonic()
    return 0.0 if handle.shifter.paused else handle.last_level
