"""macOS capture: ScreenCaptureKit for pixels, AVFoundation for the microphone.

**Nothing shells out and no frames pass through Python.** `SCRecordingOutput`
(macOS 15+) writes the .mov itself and `AVAudioRecorder` writes the .m4a, so this module starts a stream, stops a stream, and never touches a
sample buffer — which is the reason it can be this short, and the reason a
90-minute recording costs no Python at all while it runs. It is also the rule
AI-10 states about `ffmpeg` applied to `/usr/bin/screencapture`: the binary is
not ours, and it cannot capture system audio anyway.

**Everything Apple is confined here.** `capture/__init__.py` owns ids, paths,
job rows and the registry; this module answers `probe`, `start_screen`,
`start_audio` and `screenshot`, and returns opaque handles the neutral half only
ever hands back to `stop`.

**No run loop, anywhere, and that constraint chose one of the two APIs here.**
`start` and `stop` are two HTTP requests, so they arrive on two different uvicorn
worker threads, and neither the packaged app nor a source install can promise a
run loop on whatever thread AVFoundation would like to talk to. ScreenCaptureKit
is fine with that — dispatch queues throughout. `AVCaptureAudioFileOutput` was
not, and hung forever; `AVAudioRecorder` is (see `start_audio`, which records
what that cost).

Every async API is bridged to a `threading.Event` with a timeout: a capture that
never starts must raise, not hang a request forever.
"""

from __future__ import annotations

import os
import platform
import threading
import time

import objc
import AVFoundation as AVF
import Foundation
import Quartz
import ScreenCaptureKit as SCK

#: `kAudioFormatMPEG4AAC` and `AVAudioQualityHigh`. Spelled out because pyobjc
#: exposes neither as a name: the CoreAudio format is a FourCC ('aac ') and the
#: quality is a plain enum.
_AAC = 1633772320
_QUALITY_HIGH = 96

#: `SCRecordingOutput` — the API that writes the movie for us — is macOS 15.
#: `SCScreenshotManager` is 14. Below those this module has nothing to offer
#: that is worth the frame plumbing, so it says so instead (an `AVAssetWriter`
#: encoder for 12.3–14 is a later decision, not a silent degradation).
RECORD_MIN = (15, 0)
SHOT_MIN = (14, 0)

#: How long to wait for a start/stop/screenshot completion handler. Generous —
#: a first capture can sit behind the TCC prompt the user has to answer — but
#: finite, because a request that never answers is worse than one that fails.
WAIT_S = 120.0

#: How long a screen `stop()` waits for the "the movie is written" callback
#: before it falls back to watching the file settle. SHORT on purpose: the
#: callback is the nice path, not the contract — see `_settle`.
FINISH_S = 15.0


def _os_version() -> tuple[int, int]:
    parts = platform.mac_ver()[0].split(".")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except ValueError:                                   # pragma: no cover
        return (0, 0)


def _too_old(minimum: tuple[int, int]) -> str:
    """The reason string when this Mac is below `minimum`, else ""."""
    version = _os_version()
    if version >= minimum:
        return ""
    return (f"needs macOS {minimum[0]} — this Mac runs "
            f"{platform.mac_ver()[0] or 'an unknown version'}")


def _screen_granted() -> bool:
    """Screen Recording, asked WITHOUT prompting.

    `CGPreflightScreenCaptureAccess` is the whole reason `sources()` can be
    called from a render path: enumerating `SCShareableContent` would answer the
    same question by raising the system dialog, which a page asking "can I?" must
    never do.
    """
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def _mic_granted() -> bool:
    return (AVF.AVCaptureDevice.authorizationStatusForMediaType_(
        AVF.AVMediaTypeAudio) == 3)                      # AVAuthorizationStatusAuthorized


def probe() -> dict:
    """`fused.capture.sources()`'s payload. Reads state; changes none."""
    record_old = _too_old(RECORD_MIN)
    shot_old = _too_old(SHOT_MIN)
    granted = _screen_granted()
    video_reason = record_old or (
        "" if granted else "Screen Recording permission has not been granted — "
        "the first capture asks for it, or grant it in System Settings › "
        "Privacy & Security › Screen & System Audio Recording")

    displays = []
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if err == 0:
        main = Quartz.CGMainDisplayID()
        for display_id in list(ids)[:count]:
            displays.append({
                "id": int(display_id),
                "width": int(Quartz.CGDisplayPixelsWide(display_id)),
                "height": int(Quartz.CGDisplayPixelsHigh(display_id)),
                "main": bool(display_id == main),
            })

    mics = []
    default = AVF.AVCaptureDevice.defaultDeviceWithMediaType_(AVF.AVMediaTypeAudio)
    default_id = str(default.uniqueID()) if default is not None else None
    for device in AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeAudio):
        mics.append({
            "id": str(device.uniqueID()),
            "name": str(device.localizedName()),
            "default": str(device.uniqueID()) == default_id,
        })

    return {
        "video": {"available": not record_old, "granted": granted,
                  "reason": video_reason or None},
        "audio": {"available": True, "granted": _mic_granted(),
                  "reason": None if _mic_granted() else
                  "Microphone permission has not been granted — the first "
                  "recording asks for it"},
        "systemAudio": {"available": not record_old,
                        "reason": record_old or None},
        "screenshot": {"available": not shot_old, "granted": granted,
                       "reason": shot_old or video_reason or None},
        "displays": displays,
        "microphones": mics,
    }


# ------------------------------------------------------------------- helpers


class _Wait:
    """A completion handler as a blocking call, with the error it carried."""

    def __init__(self, what: str):
        self.what = what
        self.event = threading.Event()
        self.error = None

    def done(self, error=None) -> None:
        if error is not None:
            self.error = str(error.localizedDescription()
                             if hasattr(error, "localizedDescription") else error)
        self.event.set()

    def result(self) -> None:
        if not self.event.wait(WAIT_S):
            raise RuntimeError(f"{self.what} did not answer within "
                               f"{int(WAIT_S)}s")
        if self.error:
            raise RuntimeError(f"{self.what} failed: {self.error}")


def _settle(path: str, timeout: float = FINISH_S) -> None:
    """Wait until `path` stops growing — the "it is written" signal of last resort.

    Only the SCREEN path uses this, and only when the recording delegate did not
    call back inside `FINISH_S`. A movie that was written is not a failure
    however quiet its delegate was, and what is observable without it is the
    write itself: the last thing a QuickTime writer does is append the `moov`
    atom, which shows up here as one final jump in size followed by silence.
    Three quiet reads is the atom having landed.
    """
    deadline = time.monotonic() + timeout
    last = -1
    quiet = 0
    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        if size > 0 and size == last:
            quiet += 1
            if quiet >= 3:
                return
        else:
            quiet = 0
        last = size
        time.sleep(0.1)


def _require_record() -> None:
    old = _too_old(RECORD_MIN)
    if old:
        from fused_render.capture import Unsupported

        raise Unsupported("screen recording " + old)


def _display(display_id) -> object:
    """The `SCDisplay` for a display id — and the call that may PROMPT.

    `SCShareableContent` is the only way to a content filter, and it is also
    what raises the TCC dialog on a machine that has not granted Screen
    Recording. That is deliberate and it is why `probe()` uses
    `CGPreflightScreenCaptureAccess` instead: the prompt rides the first real
    capture, where the user has just asked for one.
    """
    box: dict = {}
    wait = _Wait("listing displays")

    def handler(content, error):
        box["content"] = content
        wait.done(error)

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(handler)
    wait.result()
    displays = list(box.get("content").displays()) if box.get("content") else []
    if not displays:
        raise RuntimeError(
            "no capturable display — Screen Recording permission is most "
            "likely denied (System Settings › Privacy & Security)")
    if display_id in (None, "", 0):
        wanted = Quartz.CGMainDisplayID()
    else:
        try:
            wanted = int(display_id)
        except (TypeError, ValueError):
            from fused_render.capture import CaptureError

            raise CaptureError(f"'display' must be a display id, not "
                               f"{display_id!r}")
    for display in displays:
        if int(display.displayID()) == int(wanted):
            return display
    from fused_render.capture import CaptureError

    known = ", ".join(str(int(d.displayID())) for d in displays)
    raise CaptureError(f"no such display: {wanted} (this machine has {known})")


def _display_scale(display) -> int:
    """Backing pixels per point for this display — 2 on a Retina panel, 1 else."""
    mode = Quartz.CGDisplayCopyDisplayMode(display.displayID())
    if mode is None:                                     # pragma: no cover
        return 1
    points = max(1, int(Quartz.CGDisplayModeGetWidth(mode)))
    pixels = int(Quartz.CGDisplayModeGetPixelWidth(mode))
    return max(1, round(pixels / points))


def _configure(display, spec) -> object:
    config = SCK.SCStreamConfiguration.alloc().init()
    # Pixels, not points. `SCDisplay.width` — and `CGDisplayPixelsWide`, despite
    # its name — are POINTS: on the Mac this was written on both say 1800 while
    # the panel is really 3600 wide. Left at that, every recording and every
    # screenshot came out at half resolution and looked soft, with nothing in the
    # API to suggest why. The display MODE is the only thing that knows both
    # numbers, so the scale comes from there.
    scale = _display_scale(display)
    rect = spec.get("rect")
    if rect:
        x, y, w, h = rect
        config.setSourceRect_(Foundation.NSMakeRect(x, y, w, h))
        config.setWidth_(int(w * scale))
        config.setHeight_(int(h * scale))
    else:
        config.setWidth_(int(display.width()) * scale)
        config.setHeight_(int(display.height()) * scale)
    config.setShowsCursor_(bool(spec.get("cursor", True)))
    audio = spec.get("audio")
    if audio in ("system", "both"):
        config.setCapturesAudio_(True)
    if audio in ("mic", "both"):
        config.setCaptureMicrophone_(True)
        # The one place a specific microphone CAN be chosen — see
        # `start_audio`'s note about why audio-only cannot.
        if spec.get("device"):
            config.setMicrophoneCaptureDeviceID_(str(spec["device"]))
    return config


class _RecordingDelegate(Foundation.NSObject):
    """`SCRecordingOutputDelegate` — the only reason this is an ObjC class.

    `stop()` must not return before the movie is FINALISED: a .mov whose moov
    atom was never written does not play, and the caller is handed that path the
    moment `stop()` resolves. `stopCapture`'s own completion fires before the
    output has finished writing, so the finish callback is what is actually
    waited on.
    """

    def initWithWait_(self, wait):
        self = objc.super(_RecordingDelegate, self).init()
        if self is None:                                 # pragma: no cover
            return None
        self._wait = wait
        return self

    def recordingOutputDidStartRecording_(self, output):
        pass

    def recordingOutput_didFailWithError_(self, output, error):
        self._wait.done(error)

    def recordingOutputDidFinishRecording_(self, output):
        self._wait.done(None)


class _ScreenHandle:
    def __init__(self, stream, output, finished: _Wait, path: str):
        self.stream = stream
        self.output = output
        self.finished = finished
        self.path = path


def start_screen(out: str, spec: dict) -> _ScreenHandle:
    """Start recording a display to `out` (.mov). Returns when frames flow."""
    _require_record()
    display = _display(spec.get("display"))
    content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
        display, [])
    config = _configure(display, spec)

    stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
        content_filter, config, None)

    finished = _Wait("finishing the recording")
    delegate = _RecordingDelegate.alloc().initWithWait_(finished)
    out_config = SCK.SCRecordingOutputConfiguration.alloc().init()
    out_config.setOutputURL_(Foundation.NSURL.fileURLWithPath_(out))
    out_config.setOutputFileType_(AVF.AVFileTypeQuickTimeMovie)
    output = SCK.SCRecordingOutput.alloc().initWithConfiguration_delegate_(
        out_config, delegate)

    ok, error = stream.addRecordingOutput_error_(output, None)
    if not ok:
        raise RuntimeError(f"could not attach the recording output: {error}")

    started = _Wait("starting the capture")
    stream.startCaptureWithCompletionHandler_(started.done)
    started.result()
    # The delegate is kept alive by the handle: pyobjc holds no strong reference
    # for the ObjC side, and a collected delegate takes the finish callback —
    # and so the guarantee that `stop()` returns a playable file — with it.
    handle = _ScreenHandle(stream, output, finished, out)
    handle.delegate = delegate
    return handle


class _AudioHandle:
    def __init__(self, recorder, path: str):
        self.recorder = recorder
        self.path = path


def start_audio(out: str, spec: dict) -> _AudioHandle:
    """Start recording the microphone to `out` (.m4a), via `AVAudioRecorder`.

    **`AVCaptureSession` + `AVCaptureAudioFileOutput` was the first attempt and
    it does not work in this app.** That output is thread-affine in a way nothing
    in its API suggests: `stopRecording` marshals with
    `performSelector:onThread:waitUntilDone:YES` and blocks until a RUN LOOP runs
    it. Here `start` and `stop` are two HTTP requests on two different uvicorn
    worker threads, and the marshal target has no run loop in either the
    packaged app or a source install — so stopping a microphone recording hung
    forever, in AVFCapture, with the file already fully written (sampled:
    `-[AVCaptureAudioFileOutput stopRecording]` → `-[_NSThreadPerformInfo
    wait]`). Funnelling every call through one owned run-loop thread did not fix
    it either, which is what identified the target as the MAIN thread.

    `AVAudioRecorder` has no such affinity: measured 5 ms for a `stop()` from a
    different thread than `record()`, with a valid file on disk when it returns.

    The cost is real and is refused rather than hidden: this API records the
    SYSTEM's current input device and cannot choose one. `capture/__init__.py`
    rejects `device` here and says where it does work — a screen recording's
    `audio: "mic"`, which goes through `SCStreamConfiguration` and does take a
    device id. `sources().microphones[].default` is the one this will use.
    """
    url = Foundation.NSURL.fileURLWithPath_(out)
    settings = {
        AVF.AVFormatIDKey: _AAC,
        AVF.AVSampleRateKey: 44100.0,
        AVF.AVNumberOfChannelsKey: 1,
        AVF.AVEncoderAudioQualityKey: _QUALITY_HIGH,
    }
    recorder, error = AVF.AVAudioRecorder.alloc().initWithURL_settings_error_(
        url, settings, None)
    if recorder is None:
        raise RuntimeError(f"could not open the microphone: {error}")
    if not recorder.record():
        # The most common cause by far, and the one worth naming: microphone
        # permission. `record()` answers False rather than raising.
        granted = _mic_granted()
        raise RuntimeError(
            "the microphone did not start" + ("" if granted else
            " — Microphone permission has not been granted to this app "
            "(System Settings › Privacy & Security › Microphone)"))
    return _AudioHandle(recorder, out)


def stop(handle) -> None:
    """End a capture and return only once the file on disk is playable.

    Two mechanisms, because the two frameworks answer differently.
    ScreenCaptureKit calls its delegate on a dispatch queue, so the finish
    callback really arrives and is waited on — bounded, then `_settle` takes
    over rather than raising, since a movie that was written is not a failure
    however quiet its delegate was. AVFoundation's callback needs a run loop
    nobody here can promise, so audio only ever watches the file.
    """
    if isinstance(handle, _ScreenHandle):
        wait = _Wait("stopping the capture")
        handle.stream.stopCaptureWithCompletionHandler_(wait.done)
        wait.result()
        if not handle.finished.event.wait(FINISH_S):
            _settle(handle.path)
        elif handle.finished.error:
            raise RuntimeError("the recording failed: " + handle.finished.error)
        return
    if isinstance(handle, _AudioHandle):
        # Synchronous: `AVAudioRecorder.stop()` returns with the file written,
        # from any thread — which is the whole reason it is the audio backend
        # (see `start_audio`).
        handle.recorder.stop()
        return
    raise RuntimeError(f"not a capture handle: {handle!r}")   # pragma: no cover


# --------------------------------------------------------------------- still


def screenshot(out: str, spec: dict) -> dict:
    """One frame to `out`, via `SCScreenshotManager` (macOS 14+)."""
    old = _too_old(SHOT_MIN)
    if old:
        from fused_render.capture import Unsupported

        raise Unsupported("screenshots " + old)

    display = _display(spec.get("display"))
    content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
        display, [])
    config = _configure(display, {"rect": spec.get("rect"),
                                  "cursor": spec.get("cursor", False)})

    box: dict = {}
    wait = _Wait("taking the screenshot")

    def handler(image, error):
        box["image"] = image
        wait.done(error)

    SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
        content_filter, config, handler)
    wait.result()
    image = box.get("image")
    if image is None:
        raise RuntimeError("the screenshot came back empty")

    utype = ("public.png" if spec.get("format", "png") == "png"
             else "public.jpeg")
    url = Foundation.NSURL.fileURLWithPath_(out)
    dest = Quartz.CGImageDestinationCreateWithURL(url, utype, 1, None)
    if dest is None:
        raise RuntimeError(f"could not write {out}")
    Quartz.CGImageDestinationAddImage(dest, image, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise RuntimeError(f"could not encode {out}")
    return {"width": int(Quartz.CGImageGetWidth(image)),
            "height": int(Quartz.CGImageGetHeight(image))}
