"""macOS capture: ScreenCaptureKit for pixels, AVFoundation for the microphone,
Core Audio process taps for audio-only system audio.

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

**Audio-only has three recorders, picked by `source` and by OS version**
(`start_audio`): `"mic"` is `AVAudioRecorder`; `"system"` / `"both"` is a Core
Audio process tap on macOS 14.2+ (`_darwin_tap` — no display, no Screen
Recording permission), and on 13.0–14.1 the audio-only stream of `_darwin_mux`
(the Screen Recording permission, an .m4a with no video track).

Every async API is bridged to a `threading.Event` with a timeout: a capture that
never starts must raise, not hang a request forever.
"""

from __future__ import annotations

import math
import os
import platform
import threading
import time

import objc
import AVFoundation as AVF
import Foundation
import Quartz
import ScreenCaptureKit as SCK

from fused_render.capture import _pause

#: `kAudioFormatMPEG4AAC` and `AVAudioQualityHigh`. Spelled out because pyobjc
#: exposes neither as a name: the CoreAudio format is a FourCC ('aac ') and the
#: quality is a plain enum.
_AAC = 1633772320
_QUALITY_HIGH = 96

#: **The floor is per VERB, not one number** (SPEC CP-8).
#:
#: Recording's floor is the floor of the thing that makes it worth doing
#: natively: a page can already record a screen with `MediaRecorder`, and
#: SYSTEM AUDIO is the one thing it cannot do on macOS. That is
#: `SCStreamConfiguration.capturesAudio`, which is macOS 13 (and, from 14.2, a
#: Core Audio process tap for audio-only) — below 13 this module has nothing to
#: offer that the browser does not already do better.
RECORD_MIN = (13, 0)
#: `SCRecordingOutput` — the API that writes the movie for us — is macOS 15.
#: Below it `_darwin_mux` supplies the `AVAssetWriter` that does the same job,
#: which is the "later decision" the first cut of this comment deferred (D417).
SCRO_MIN = (15, 0)
#: `SCScreenshotManager` is 14. `CGDisplayCreateImage` covers 13 (`_cg_shot`),
#: so the still's floor is 13 as well.
SHOT_MIN = (13, 0)
SCSHOT_MIN = (14, 0)

#: Env var forcing the `AVAssetWriter` path on a Mac new enough for
#: `SCRecordingOutput`. Every API `_darwin_mux` uses exists on 15 too, so this
#: is how the 13–14 recorder can be exercised on the machines most likely to be
#: developing it — otherwise the only code path nobody here can run is the one
#: that was just written.
FORCE_MUX = "FUSED_CAPTURE_FORCE_MUX"

#: Same idea for audio-only system audio: force the 13.0–14.1 ScreenCaptureKit
#: route on a Mac new enough for process taps, so the fallback can be exercised
#: by whoever is developing it on a current machine.
FORCE_SCK_AUDIO = "FUSED_CAPTURE_FORCE_SCK_AUDIO"

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


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _use_tap() -> bool:
    """Whether audio-only system audio is a Core Audio tap (14.2+) rather than
    the ScreenCaptureKit stream — unless the fallback is deliberately forced."""
    from fused_render.capture import _darwin_tap

    return (not _env_on(FORCE_SCK_AUDIO)
            and _os_version() >= _darwin_tap.TAP_MIN)


def _too_old(minimum: tuple[int, int]) -> str:
    """The reason string when this Mac is below `minimum`, else ""."""
    version = _os_version()
    if version >= minimum:
        return ""
    return (f"needs macOS {minimum[0]} — this Mac runs "
            f"{platform.mac_ver()[0] or 'an unknown version'}")


#: System Settings › Privacy & Security › Screen & System Audio Recording.
#: The legacy `com.apple.preference.security` anchor still routes on 13+.
SHOT_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")


def settings_url() -> str:
    """`capture.open_settings`'s hook: where Screen Recording is granted."""
    return SHOT_SETTINGS_URL


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
    # System audio, audio-only: a process tap from 14.2 (its own permission,
    # which has no public preflight, so `granted` is None = "unknown"), the
    # Screen Recording route below it (whose grant IS preflightable).
    if record_old:
        system_audio = {"available": False, "reason": record_old}
    elif _use_tap():
        system_audio = {"available": True, "granted": None, "reason": None}
    else:
        system_audio = {"available": True, "granted": granted,
                        "reason": None if granted else video_reason}

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
        "systemAudio": system_audio,
        "screenshot": {"available": not shot_old, "granted": granted,
                       "reason": shot_old or video_reason or None},
        "displays": displays,
        "microphones": mics,
    }


def ext(mode: str, spec: dict) -> str:
    """The container this backend writes. Asked rather than assumed by the
    neutral half, because the sink backends write whatever `MediaRecorder`
    produced and cannot promise a QuickTime movie (SPEC CP-5)."""
    return ".mov" if mode == "screen" else ".m4a"


def refuse(mode: str, spec: dict) -> str | None:
    """What this backend cannot honour, and where the caller can ask instead.

    One option, one platform, one sentence — and it lives HERE rather than in
    the neutral half because the sentence names System Settings, which is wrong
    advice on Linux.

    Audio-only MICROPHONE records through `AVAudioRecorder`, which has no device
    selection. The API that did — `AVCaptureAudioFileOutput` — deadlocked on a run loop
    this app cannot provide (see `start_audio`), so the capability is genuinely
    gone rather than merely unimplemented. A silently-wrong microphone is a
    recording the user has to make twice, so this says where choosing one works.
    """
    if mode == "audio" and spec.get("device") and spec.get("audio") == "system":
        return ("source 'system' records no microphone, so 'device' does not "
                "apply — use source: 'both' to mix a chosen microphone in")
    if (mode == "audio" and spec.get("device")
            and spec.get("audio") in (None, "mic")):
        return ("audio-only recording uses the system's current input device, "
                "so 'device' cannot be chosen here — record the screen with "
                "audio: 'mic' to pick a specific microphone, or change the "
                "input in System Settings › Sound "
                "(sources().microphones tells you which is current)")
    return None


# ------------------------------------------------------------------- helpers


class _Timeout(RuntimeError):
    """`_Wait.result` gave up waiting: the call never answered."""


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

    def result(self, timeout: float = WAIT_S) -> None:
        if not self.event.wait(timeout):
            raise _Timeout(f"{self.what} did not answer within "
                           f"{int(timeout)}s")
        if self.error:
            raise RuntimeError(f"{self.what} failed: {self.error}")


def _settle(path: str, timeout: float = FINISH_S) -> bool:
    """Wait until `path` stops growing — the "it is written" signal of last resort.

    Only the SCREEN path uses this, and only when the recording delegate did not
    call back inside `FINISH_S`. A movie that was written is not a failure
    however quiet its delegate was, and what is observable without it is the
    write itself: the last thing a QuickTime writer does is append the `moov`
    atom, which shows up here as one final jump in size followed by silence.
    Three quiet reads is the atom having landed. **Answers whether that
    happened**: a file still growing when the deadline passes is not a finished
    recording, and returning the same `None` for both would hand the caller a
    path to a possibly-unfinalised movie labelled done.
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
                return True
        else:
            quiet = 0
        last = size
        time.sleep(0.1)
    return False


def _require_record() -> None:
    old = _too_old(RECORD_MIN)
    if old:
        from fused_render.capture import Unsupported

        raise Unsupported("screen recording " + old)


#: How long a looked-up `SCDisplay` is reused before the shareable content is
#: enumerated again. Enumerating is THE cost of a still: `SCShareableContent`
#: walks every window of every app, and on a busy Mac that took 1–7 s per
#: screenshot (server log, 2026-10-28) against ~100 ms for the shot itself —
#: the canvas chat's "instant" native picture was timing out on it. A display
#: object stays valid while the panel is plugged in, so a short cache costs
#: nothing but up to DISPLAY_CACHE_S of staleness after a display is
#: (un)plugged or changes mode — and a shot that FAILS on a cached display
#: evicts it and retries once on a fresh enumeration (`_forget_display`).
DISPLAY_CACHE_S = 30.0
_display_cache: dict = {}   # display id -> (SCDisplay, time.monotonic())


def _shareable_displays() -> list:
    """Every `SCDisplay` ScreenCaptureKit will share — and the call that may
    PROMPT. `SCShareableContent` is the only way to a content filter, and it
    is also what raises the TCC dialog on a machine that has not granted
    Screen Recording. That is deliberate and it is why `probe()` uses
    `CGPreflightScreenCaptureAccess` instead: the prompt rides the first real
    capture, where the user has just asked for one.

    On-screen windows only, desktop windows excluded: the DISPLAYS are the
    same either way, and the full walk (every window of every app, hidden
    ones included) is what made this slow."""
    box: dict = {}
    wait = _Wait("listing displays")

    def handler(content, error):
        box["content"] = content
        wait.done(error)

    SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        True, True, handler)
    wait.result()
    return list(box.get("content").displays()) if box.get("content") else []


def _forget_display(display) -> None:
    """Drop one cached `SCDisplay` (after a capture on it failed), so the
    next lookup enumerates again."""
    try:
        _display_cache.pop(int(display.displayID()), None)
    except Exception:                                    # noqa: BLE001
        _display_cache.clear()


def _display(display_id) -> object:
    """The `SCDisplay` for a display id, from the cache when it is fresh
    (`DISPLAY_CACHE_S`), else from a new enumeration."""
    if display_id in (None, "", 0):
        wanted = Quartz.CGMainDisplayID()
    else:
        try:
            wanted = int(display_id)
        except (TypeError, ValueError):
            from fused_render.capture import CaptureError

            raise CaptureError(f"'display' must be a display id, not "
                               f"{display_id!r}")
    hit = _display_cache.get(int(wanted))
    if hit and time.monotonic() - hit[1] < DISPLAY_CACHE_S:
        return hit[0]
    displays = _shareable_displays()
    if not displays:
        raise RuntimeError(
            "no capturable display — Screen Recording permission is most "
            "likely denied (System Settings › Privacy & Security)")
    now = time.monotonic()
    _display_cache.clear()
    for display in displays:
        _display_cache[int(display.displayID())] = (display, now)
    for display in displays:
        if int(display.displayID()) == int(wanted):
            return display
    from fused_render.capture import CaptureError

    known = ", ".join(str(int(d.displayID())) for d in displays)
    raise CaptureError(f"no such display: {wanted} (this machine has {known})")


def locate(rect, dpr: float):
    """`capture.shot_region`'s hook: which display a global rect sits on, and
    the same rect in that display's own coordinates.

    A browser's `screenX`/`getBoundingClientRect` are CSS pixels of the screen,
    and on the Mac those are POINTS — the unit `CGDisplayBounds` reports and
    the unit `sourceRect` wants — so `dpr` is deliberately unused here (the
    panel's backing scale is `_configure`'s business, from the display mode).
    Both spaces share the main display's top-left as origin, so the map is one
    subtraction. A rect not entirely inside one display is REFUSED, not
    clipped: the caller is about to bake the answer into an artifact.
    """
    from fused_render.capture import CaptureError

    x, y, w, h = rect
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if err != 0:                                          # pragma: no cover
        raise RuntimeError("could not list displays")
    for display_id in list(ids)[:count]:
        b = Quartz.CGDisplayBounds(display_id)
        bx, by = b.origin.x, b.origin.y
        bw, bh = b.size.width, b.size.height
        if bx <= x and by <= y and x + w <= bx + bw and y + h <= by + bh:
            return int(display_id), (x - bx, y - by, w, h)
    raise CaptureError(
        "the region to photograph is not entirely on one display — move the "
        "window fully on screen and export again")


def _display_scale(display) -> int:
    """Backing pixels per point for this display — 2 on a Retina panel, 1 else."""
    mode = Quartz.CGDisplayCopyDisplayMode(display.displayID())
    if mode is None:                                     # pragma: no cover
        return 1
    points = max(1, int(Quartz.CGDisplayModeGetWidth(mode)))
    pixels = int(Quartz.CGDisplayModeGetPixelWidth(mode))
    return max(1, round(pixels / points))


def _use_mux() -> bool:
    """Whether this recording is written by `_darwin_mux` rather than by
    `SCRecordingOutput` — below macOS 15, or when deliberately forced."""
    if os.environ.get(FORCE_MUX, "").strip().lower() in ("1", "true", "yes"):
        return True
    return _os_version() < SCRO_MIN


def _configure(display, spec, *, cursor_default: bool = True,
               stream_mic: bool = True) -> object:
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
    # `cursor` arrives RAW (None when the caller said nothing) so a backend that
    # cannot honour it can tell the two apart — this one can, so it just applies
    # the default for the kind of capture: a recording shows the pointer, a
    # still does not.
    cursor = spec.get("cursor")
    config.setShowsCursor_(cursor_default if cursor is None else bool(cursor))
    audio = spec.get("audio")
    if audio in ("system", "both"):
        config.setCapturesAudio_(True)
    # `captureMicrophone` is macOS 15, and on the `_darwin_mux` path the
    # microphone comes from an `AVCaptureSession` at every version — asking the
    # stream for it as well would mix the same voice into the movie twice.
    if (audio in ("mic", "both") and stream_mic
            and config.respondsToSelector_(b"setCaptureMicrophone:")):
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
    if _use_mux():
        from fused_render.capture import _darwin_mux

        return _darwin_mux.start(
            out, display, _configure(display, spec, stream_mic=False), spec)

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
        self.paused = False


def start_audio(out: str, spec: dict):
    """Start an audio-only recording to `out` (.m4a), by `spec["audio"]`.

    `"mic"` (and nothing at all) is `AVAudioRecorder`, below. `"system"` and
    `"both"` need the system's output mix: a Core Audio process tap on 14.2+
    (`_darwin_tap`), the audio-only ScreenCaptureKit stream on 13.0–14.1 or when
    `FORCE_SCK_AUDIO` says so. Both are "recording", so the 13.0 floor applies.
    """
    if spec.get("audio") in ("system", "both"):
        _require_record()
        if _use_tap():
            from fused_render.capture import _darwin_tap

            return _darwin_tap.start(out, spec)
        from fused_render.capture import _darwin_mux

        try:
            display = _display(None)
            return _darwin_mux.start_audio(
                out, display, _configure(display, spec, stream_mic=False), spec)
        except Exception as exc:
            # Whatever the SCStream/SCShareableContent error says, a missing
            # Screen Recording grant is the cause worth naming — and in terms of
            # sound, since nobody asked for a screen recording here.
            if not _screen_granted():
                raise RuntimeError(_system_audio_permission_message()) from exc
            raise
    return _start_mic_recorder(out, spec)


def _system_audio_permission_message() -> str:
    """The 13.0-14.1 audio-only error for a missing Screen Recording grant."""
    version = platform.mac_ver()[0] or "this version of macOS"
    message = (f"recording system audio on macOS {version} needs the Screen "
               "Recording permission — macOS calls it that, but only sound is "
               "recorded, no picture. Grant it in System Settings → Privacy & "
               "Security → Screen Recording, then quit and reopen the app.")
    if (14, 0) <= _os_version() < (14, 2):
        message += (" Updating to macOS 14.2 or later uses the narrower "
                    "System Audio Recording permission instead.")
    return message


def _start_mic_recorder(out: str, spec: dict) -> _AudioHandle:
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
    # Metering costs next to nothing and is what the recording pill's meter
    # reads (`level`); it has to be on before `record()`.
    recorder.setMeteringEnabled_(True)
    if not recorder.record():
        # The most common cause by far, and the one worth naming: microphone
        # permission. `record()` answers False rather than raising.
        granted = _mic_granted()
        raise RuntimeError(
            "the microphone did not start" + ("" if granted else
            " — Microphone permission has not been granted to this app "
            "(System Settings › Privacy & Security › Microphone)"))
    return _AudioHandle(recorder, out)


def failure(handle) -> str | None:
    """The error a recording has ALREADY died of, or None — asked every tick.

    Without this a stream that dies at minute two (disk full, a display that
    went away) keeps its job row ticking "Recording" until `maxSeconds`, and the
    user narrates twenty-eight minutes into a file nothing is writing.
    `SCRecordingOutput` tells its delegate immediately; this is the read side of
    that, and it is why the delegate stores the error instead of only unblocking
    `stop`.
    """
    from fused_render.capture import _darwin_mux, _darwin_tap

    if isinstance(handle, _darwin_tap.TapHandle):
        return _darwin_tap.failure(handle)
    if isinstance(handle, _darwin_mux.MuxHandle):
        return _darwin_mux.failure(handle)
    if isinstance(handle, _ScreenHandle):
        return handle.finished.error or None
    return None


def warning(handle) -> str | None:
    """Advice about a recording that is ending, or None. Optional backend hook.

    The process tap and the audio-only ScreenCaptureKit route have some:
    neither can tell a denied permission from an idle Mac, so each reports that
    nothing was heard instead of failing."""
    from fused_render.capture import _darwin_mux, _darwin_tap

    if isinstance(handle, _darwin_tap.TapHandle):
        return _darwin_tap.warning(handle)
    if isinstance(handle, _darwin_mux.MuxHandle):
        return _darwin_mux.warning(handle)
    return None


def can_pause(handle) -> bool:
    """Whether this recording can be paused. Optional backend hook.

    Every audio recording can: the microphone recorder, the process tap, and the
    audio-only ScreenCaptureKit route (the only one macOS 13.0-14.1 gets). A
    screen recording cannot — pausing a video would need frame retiming too."""
    from fused_render.capture import _darwin_mux, _darwin_tap

    if isinstance(handle, (_AudioHandle, _darwin_tap.TapHandle)):
        return True
    return isinstance(handle, _darwin_mux.MuxHandle) and handle.audio_only


def pause(handle) -> None:
    """Stop recording until `resume`; the file stays open and keeps its audio."""
    from fused_render.capture import _darwin_mux, _darwin_tap

    if isinstance(handle, _AudioHandle):
        handle.recorder.pause()
        handle.paused = True
    elif isinstance(handle, _darwin_tap.TapHandle):
        _darwin_tap.pause(handle)
    elif isinstance(handle, _darwin_mux.MuxHandle) and handle.audio_only:
        _darwin_mux.pause(handle)
    else:
        raise RuntimeError("pausing is not supported for screen recordings")


def resume(handle) -> None:
    from fused_render.capture import _darwin_mux, _darwin_tap

    if isinstance(handle, _AudioHandle):
        if not handle.recorder.record():
            raise RuntimeError("the microphone did not resume")
        handle.paused = False
    elif isinstance(handle, _darwin_tap.TapHandle):
        _darwin_tap.resume(handle)
    elif isinstance(handle, _darwin_mux.MuxHandle) and handle.audio_only:
        _darwin_mux.resume(handle)
    else:
        raise RuntimeError("pausing is not supported for screen recordings")


def level(handle) -> float:
    """A 0..1 loudness for the recording pill's meter. Never raises."""
    try:
        from fused_render.capture import _darwin_mux, _darwin_tap

        if isinstance(handle, _AudioHandle):
            if handle.paused:
                return 0.0
            handle.recorder.updateMeters()
            return _pause.db_to_level(
                float(handle.recorder.averagePowerForChannel_(0)))
        if isinstance(handle, _darwin_tap.TapHandle):
            return _darwin_tap.level(handle)
        if isinstance(handle, _darwin_mux.MuxHandle):
            return _darwin_mux.level(handle)
    except Exception:                                    # noqa: BLE001
        pass
    return 0.0


def stop(handle) -> None:
    """End a capture and return only once the file on disk is playable.

    Two mechanisms, because the two frameworks answer differently.
    ScreenCaptureKit calls its delegate on a dispatch queue, so the finish
    callback really arrives and is waited on — bounded, then `_settle` takes
    over rather than raising, since a movie that was written is not a failure
    however quiet its delegate was. AVFoundation's callback needs a run loop
    nobody here can promise, so audio only ever watches the file.
    """
    from fused_render.capture import _darwin_mux, _darwin_tap

    if isinstance(handle, _darwin_tap.TapHandle):
        _darwin_tap.stop(handle)
        return
    if isinstance(handle, _darwin_mux.MuxHandle):
        _darwin_mux.stop(handle)
        return
    if isinstance(handle, _ScreenHandle):
        wait = _Wait("stopping the capture")
        handle.stream.stopCaptureWithCompletionHandler_(wait.done)
        wait.result()
        if not handle.finished.event.wait(FINISH_S):
            if not _settle(handle.path):
                raise RuntimeError(
                    "the recording did not finish writing — the file may be "
                    "incomplete: " + handle.path)
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


def _write_image(out: str, image, jpeg: bool) -> dict:
    """Encode a `CGImage` to `out`. Shared by both still paths.

    The container follows the OUTPUT NAME rather than a `format` option: a
    caller who asked for "shot.jpg" and got PNG bytes under that name has a
    file every other tool will misread, and two ways to say one thing is one
    too many on a forever surface.
    """
    utype = "public.jpeg" if jpeg else "public.png"
    url = Foundation.NSURL.fileURLWithPath_(out)
    dest = Quartz.CGImageDestinationCreateWithURL(url, utype, 1, None)
    if dest is None:
        raise RuntimeError(f"could not write {out}")
    Quartz.CGImageDestinationAddImage(dest, image, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise RuntimeError(f"could not encode {out}")
    return {"width": int(Quartz.CGImageGetWidth(image)),
            "height": int(Quartz.CGImageGetHeight(image))}


def _cg_shot(out: str, display, spec: dict) -> dict:
    """The still on macOS 13, where `SCScreenshotManager` does not exist yet.

    `CGDisplayCreateImage` predates all of ScreenCaptureKit and is deprecated
    from 14 — which costs nothing, because 14 is exactly where the other path
    takes over. It returns BACKING pixels, so the Retina rule `_display_scale`
    exists for is honoured here for free rather than reimplemented.

    The one thing it cannot do is draw the pointer, so `cursor: true` is
    REFUSED with a sentence naming the version that can, rather than quietly
    handing back a still with no cursor in it (the AI-10/D319 posture).
    """
    if spec.get("cursor"):
        from fused_render.capture import CaptureError

        raise CaptureError(
            "'cursor' cannot be honoured for a screenshot on this Mac — "
            "CGDisplayCreateImage never draws the pointer, and "
            "SCScreenshotManager, which does, arrives in macOS 14")
    display_id = display.displayID()
    rect = spec.get("rect")
    if rect:
        x, y, w, h = rect
        image = Quartz.CGDisplayCreateImageForRect(
            display_id, Quartz.CGRectMake(x, y, w, h))
    else:
        image = Quartz.CGDisplayCreateImage(display_id)
    if image is None:
        raise RuntimeError(
            "the screenshot came back empty — Screen Recording permission is "
            "most likely denied (System Settings › Privacy & Security)")
    return _write_image(out, image, bool(spec.get("jpeg")))


def screenshot(out: str, spec: dict) -> dict:
    """One frame to `out` — `SCScreenshotManager` on 14+, CoreGraphics on 13."""
    old = _too_old(SHOT_MIN)
    if old:
        from fused_render.capture import Unsupported

        raise Unsupported("screenshots " + old)

    display = _display(spec.get("display"))
    shot = _cg_shot if _too_old(SCSHOT_MIN) else _sck_shot
    try:
        return shot(out, display, spec)
    except Exception:                                    # noqa: BLE001
        # A cached display may be stale (unplugged, mode changed, woke from
        # sleep): once more on a fresh enumeration before giving up.
        if _display_cache.get(int(display.displayID()), (None,))[0] is not display:
            raise
        _forget_display(display)
        return shot(out, _display(spec.get("display")), spec)


def _sck_shot(out: str, display, spec: dict) -> str:
    """One ScreenCaptureKit still of `display`, cropped to `spec["rect"]`
    when given."""
    content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
        display, [])
    config = _configure(display, {"rect": spec.get("rect"),
                                  "cursor": spec.get("cursor")},
                        cursor_default=False)

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

    return _write_image(out, image, bool(spec.get("jpeg")))


# -------------------------------------------------------------- window still
#
# `shot_region` photographs the PIXELS UNDER A RECT of the screen, so the
# window the page lives in has to be in front, on one display and not behind
# another app — which an agent's gesture-less `canvas_screenshot` cannot
# arrange (D1472: the user is reading the chat with another window up, and
# the one picture that would have shown the bug was refused). `shot_window`
# asks ScreenCaptureKit for THE WINDOW instead: a desktop-independent window
# filter draws it from its own backing store, so another window on top,
# another Space or another display change nothing. The page names its window
# by the frame the browser reports (`screenX/Y`, `outerWidth/Height`, points,
# top-left origin — the same space `SCWindow.frame` is in) and the match is
# by that box, not by a window id the page cannot know.
#
# Still needs the Screen Recording grant, and still a picture of what the
# window LAST DREW: a web view in a fully occluded window stops painting, so
# the page keeps its own "is this document visible" gate and takes the slow
# in-page road when it is not (canvas-shot-native.ts).

#: How long a ScreenCaptureKit WINDOW list is reused (`_sc_window`). The
#: list only supplies the `SCWindow` object for an id the live window server
#: list already chose; a window newer than the copy is fetched by one fresh
#: walk, and a window that has since closed fails the capture and clears it.
WINDOW_CACHE_S = 5.0
#: How long a window walk may take before it is given up: a process with no
#: windows of its own has seen `getCurrentProcessShareableContent` never
#: answer at all (2026-10-31), and the full walk is seconds on a busy Mac —
#: either way the caller falls to its next road rather than waiting `WAIT_S`.
WINDOW_WALK_S = 10.0
#: How long one window still may take. ~100 ms when it answers at all; a
#: call that does not is not retried, and the native lock every still
#: shares is not held for `WAIT_S` on its account.
WINDOW_SHOT_S = 15.0
#: Latched the first time the own-process walk does not answer: that walk
#: is skipped from then on (the full walk serves), so one hang is paid once
#: per process, not on every still.
_ours_walk_off = False
#: How far (points, per edge) the page's frame may sit from a window's before
#: they are not the same window: a title bar the browser counts differently,
#: a shadow, a half-pixel. Two app windows a user has not deliberately
#: stacked are further apart than this.
WINDOW_MATCH_SLACK = 48.0
#: Unlocked on purpose: every reader and writer is `shot_window`, which the
#: neutral half runs under `with _native():` (one backend call at a time).
_window_cache: dict = {}   # "ours" | "all" -> ({window id: SCWindow}, time.monotonic())


def _cg_windows() -> list:
    """Every window the window server has, LIVE, as `pick_window` reads one:
    id, frame, title, layer, on-screen. `CGWindowListCopyWindowInfo` answers
    in milliseconds and its titles are current — a tab switched behind a
    window shows up at once — where a ScreenCaptureKit list is a snapshot
    that took seconds to walk (and a cached one held the OLD title long
    enough to photograph the wrong tab, 2026-10-31). The title needs the
    Screen Recording grant; without it every window is untitled and the box
    alone decides, as `pick_window` allows."""
    rows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    out = []
    for row in rows:
        bounds = row.get("kCGWindowBounds") or {}
        try:
            frame = (float(bounds["X"]), float(bounds["Y"]),
                     float(bounds["Width"]), float(bounds["Height"]))
            wid = int(row.get("kCGWindowNumber"))
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"id": wid, "frame": frame,
                    "title": str(row.get("kCGWindowName") or ""),
                    "layer": int(row.get("kCGWindowLayer") or 0),
                    "onscreen": bool(row.get("kCGWindowIsOnscreen"))})
    return out


def _shareable_windows(ours: bool) -> dict:
    """`{window id: SCWindow}` for every window ScreenCaptureKit will share,
    ON-SCREEN OR NOT — a window on another Space, or behind another app, is
    exactly the one `shot_window` exists for. `ours` walks this process's
    windows alone (macOS 14.4+; the packaged app runs this server
    in-process, so the shell's windows ARE ours), a fraction of the full
    walk's cost on a busy Mac; the full walk is for a page in a browser.
    Bounded by `WINDOW_WALK_S`."""
    box: dict = {}
    wait = _Wait("listing windows")

    def handler(content, error):
        box["content"] = content
        wait.done(error)

    if ours:
        SCK.SCShareableContent.getCurrentProcessShareableContentWithCompletionHandler_(handler)
    else:
        SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, False, handler)
    wait.result(WINDOW_WALK_S)
    content = box.get("content")
    return {int(w.windowID()): w for w in content.windows()} if content else {}


def _can_walk_ours() -> bool:
    return hasattr(SCK.SCShareableContent,
                   "getCurrentProcessShareableContentWithCompletionHandler_")


def _sc_window(wid: int):
    """The `SCWindow` for a window id the live list chose. Every cached list
    (`WINDOW_CACHE_S`) is tried first — this process's own, then every
    app's — and only when none holds the id (a window newer than the copies)
    are the lists walked afresh, once each, in that order. A walk that does
    not answer (`WINDOW_WALK_S`) is skipped, not fatal. None when no list
    has the window."""
    global _ours_walk_off
    now = time.monotonic()
    walks = (["ours"] if _can_walk_ours() and not _ours_walk_off else []) + ["all"]
    for which in walks:
        hit = _window_cache.get(which)
        if hit and now - hit[1] < WINDOW_CACHE_S and wid in hit[0]:
            return hit[0][wid]
    for which in walks:
        try:
            windows = _shareable_windows(which == "ours")
        except _Timeout:
            if which == "ours":
                _ours_walk_off = True
            continue
        except RuntimeError:
            continue
        _window_cache[which] = (windows, now)
        if wid in windows:
            return windows[wid]
    return None


def pick_window(windows: list, frame, title: str | None = None,
                slack: float = WINDOW_MATCH_SLACK):
    """The window whose box is the page's `frame` (x, y, w, h in points):
    the closest by the sum of the four edge distances, within `slack` on
    every edge; None when nothing is that close. Only ordinary windows
    (layer 0 — a status item, a tooltip or a sheet is never the page's), an
    on-screen one winning a tie.

    `title` is the page's `document.title`, and it is the SECOND KEY: a
    window's title carries the page it shows (the desktop app's window is
    titled by the page; a browser's by its front tab, with the browser's
    name AFTER — "T - Google Chrome", "T — Mozilla Firefox", so the page's
    title is the head of the window's, never a substring somewhere inside
    a longer unrelated one). Among the windows at the box, one whose title
    starts with the page's wins; a titled window that does NOT is refused, because
    it shows something else — another tab in front of the page, a second
    window dropped on the very same spot (two Chromium windows at one box
    photographed the wrong one, 2026-10-31) — and a still of it would be a
    confident picture of the wrong thing. An untitled window is matched by
    its box alone, as is every window when the page has no title. Pure, so
    it is tested without a screen."""
    fx, fy, fw, fh = frame
    want = (title or "").strip()
    best = None
    for win in windows:
        if win.get("layer", 0) != 0:
            continue
        x, y, w, h = win["frame"]
        edges = (abs(x - fx), abs(y - fy), abs(w - fw), abs(h - fh))
        if max(edges) > slack:
            continue
        have = str(win.get("title") or "").strip()
        if want and have and not have.startswith(want):
            continue  # titled, and showing something else
        key = (0 if (want and have) else 1, sum(edges), 0 if win.get("onscreen") else 1)
        if best is None or key < best[0]:
            best = (key, win)
    return best[1] if best else None


def _scale_at(x: float, y: float) -> int:
    """Backing pixels per point of the display under a screen point (the
    window's centre): 2 on a Retina panel, 1 else, the main display's when
    the point is on none."""
    err, ids, count = Quartz.CGGetDisplaysWithPoint(Quartz.CGPointMake(x, y), 4, None, None)
    display_id = list(ids)[0] if err == 0 and count else Quartz.CGMainDisplayID()
    mode = Quartz.CGDisplayCopyDisplayMode(display_id)
    if mode is None:                                     # pragma: no cover
        return 1
    points = max(1, int(Quartz.CGDisplayModeGetWidth(mode)))
    pixels = int(Quartz.CGDisplayModeGetPixelWidth(mode))
    return max(1, round(pixels / points))


def crop_box(window_frame, rect, image_size, slack: float = 1.0):
    """Where `rect` (screen points) lands in a still of `window_frame`
    (screen points) that is `image_size` pixels: `(x, y, w, h)` in pixels.
    The scale comes from the IMAGE against the frame rather than from a
    display mode, so a still ScreenCaptureKit rounded is cropped where it
    really is. A rect not inside the window (past `slack` points on any
    edge) is REFUSED with None, as `locate` refuses a rect off its display:
    the page draws the answer at the rect's own size, so a clamped picture
    would be stretched over the wrong pixels, silently. Pure."""
    wx, wy, ww, wh = window_frame
    iw, ih = image_size
    if ww <= 0 or wh <= 0 or iw <= 0 or ih <= 0:
        return None
    x, y, w, h = rect
    if (x < wx - slack or y < wy - slack
            or x + w > wx + ww + slack or y + h > wy + wh + slack):
        return None
    sx, sy = iw / ww, ih / wh
    half_up = lambda v: int(math.floor(v + 0.5))   # not banker's rounding
    left = min(iw, max(0, half_up((x - wx) * sx)))
    top = min(ih, max(0, half_up((y - wy) * sy)))
    right = min(iw, max(0, half_up((x - wx + w) * sx)))
    bottom = min(ih, max(0, half_up((y - wy + h) * sy)))
    if right - left <= 0 or bottom - top <= 0:
        return None
    return (left, top, right - left, bottom - top)


def _sck_window_shot(out: str, win: dict, rect, spec: dict) -> dict:
    """One ScreenCaptureKit still of `win` (a `_window_info` row), the whole
    window at the backing scale of the display under it, cropped to `rect`
    (screen points) and written to `out`."""
    from fused_render.capture import CaptureError

    x, y, w, h = win["frame"]
    scale = _scale_at(x + w / 2, y + h / 2)
    content_filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(win["window"])
    config = SCK.SCStreamConfiguration.alloc().init()
    config.setWidth_(int(w * scale))
    config.setHeight_(int(h * scale))
    config.setShowsCursor_(False)
    box: dict = {}
    wait = _Wait("taking the window screenshot")

    def handler(image, error):
        box["image"] = image
        wait.done(error)

    SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
        content_filter, config, handler)
    wait.result(WINDOW_SHOT_S)
    image = box.get("image")
    if image is None:
        raise RuntimeError("the window screenshot came back empty")
    size = (int(Quartz.CGImageGetWidth(image)), int(Quartz.CGImageGetHeight(image)))
    crop = crop_box(win["frame"], rect, size)
    if crop is None:
        raise CaptureError(
            "the region to photograph is not inside the page's window, or "
            "too small to crop — move the window so the canvas is fully "
            "inside it and try again")
    cropped = Quartz.CGImageCreateWithImageInRect(image, Quartz.CGRectMake(*crop))
    if cropped is None:                                  # pragma: no cover
        raise RuntimeError("the window screenshot could not be cropped")
    return _write_image(out, cropped, bool(spec.get("jpeg")))


def shot_window(out: str, spec: dict) -> dict:
    """`capture.shot_window`'s hook: a still of the window whose frame is
    `spec["window"]` and whose title shows `spec["title"]`, cropped to
    `spec["rect"]` (both boxes in screen points), to `out`. `Unsupported`
    below macOS 14 (`SCScreenshotManager` is the only window still here;
    the region still covers 13), `CaptureError` when no window showing this
    page sits where the page says its own does."""
    from fused_render.capture import CaptureError, Unsupported

    old = _too_old(SCSHOT_MIN)
    if old:
        raise Unsupported("window screenshots " + old)
    frame, rect, title = spec["window"], spec["rect"], spec.get("title")
    info = pick_window(_cg_windows(), frame, title)
    if info is None:
        raise CaptureError(
            "no window showing this page sits where the page says its own "
            "does — the page's window could not be found to photograph")
    win = _sc_window(info["id"])
    if win is None:
        raise RuntimeError("the page's window is not one ScreenCaptureKit will share")
    try:
        return _sck_window_shot(out, {**info, "window": win}, rect, spec)
    except (CaptureError, _Timeout):
        raise                       # a refusal, or a call that never answered: no second try
    except Exception:                                    # noqa: BLE001
        # The cached SCWindow may be gone (closed, re-created on a reload):
        # once more on a fresh walk, and only when that walk still has the
        # window — the same object failing twice is the same failure.
        _window_cache.clear()
        again = _sc_window(info["id"])
        if again is None:
            raise
        return _sck_window_shot(out, {**info, "window": again}, rect, spec)
