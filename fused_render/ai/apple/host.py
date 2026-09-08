"""The Apple tier's process host: one `fused-apple-ai` child per request.

Shaped like `server/ai.py`'s `_spawn_claude_stream` (a child per call, its
stdout read as NDJSON), NOT like a `registry.Runner`: the supervisor keeps ONE
worker per capability and evicts on engine mismatch, so an Apple "runner"
would evict the resident MLX model on every `provider: "apple"` call and be
evicted back on the next local one. The two tiers have to coexist, so this
tier spawns its own children and borrows only the job-row helpers.

One process per request, deliberately (D700 follow-up). The weights live in
the OS's own daemon, not in our child, so a spawn costs a spawn and nothing
else — and it removes the resident-server design's whole cost: no reader
thread, no per-request queues, no respawn logic, no cancel protocol.
Cancellation is `terminate()`; concurrency is two children.

Protocol: see the header of `helper/main.swift`.

**Where the binary comes from**, in order:
  1. `FUSED_RENDER_APPLE_HELPER` — an explicit path (CI, measurement builds).
  2. `Contents/MacOS/fused-apple-ai` beside the packaged interpreter — where
     `build_dmg.sh` puts the prebuilt helper (signed by the Mach-O sweep).
  3. `fused_render/ai/apple/bin/fused-apple-ai` — a checkout's own build
     (`scripts/build_apple_helper.sh`), gitignored.
  4. A dev build on demand into `~/.fused-render/ai/apple/` when `swiftc`
     and a macOS 26 SDK are present; the probe says why when they are not.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PROVIDER = "apple"
HELPER_ENV = "FUSED_RENDER_APPLE_HELPER"
HELPER_NAME = "fused-apple-ai"
#: The first macOS with FoundationModels + SpeechAnalyzer.
MIN_MACOS_MAJOR = 26

_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(_HERE, "helper", "main.swift")
CHECKOUT_BIN = os.path.join(_HERE, "bin", HELPER_NAME)

#: How long a probe answer is trusted before the helper is asked again. Long
#: enough that a page polling the catalog costs nothing; short enough that
#: turning Apple Intelligence on in System Settings shows up without a restart.
PROBE_TTL_S = 30.0
PROBE_TIMEOUT_S = 15.0
#: A generation that produces nothing for this long is dead, not slow — the
#: on-device model answers its first token in well under a second.
FIRST_FRAME_TIMEOUT_S = 120.0


class AppleError(RuntimeError):
    """A tier-level failure with the fused.ai error `type` attached."""

    def __init__(self, type_: str, message: str):
        super().__init__(message)
        self.type = type_


@dataclass
class Availability:
    ok: bool
    #: "available" | "loading" | "unavailable" — `loading` is the OS still
    #: fetching Apple's model, which callers turn into a 409 `model_loading`.
    state: str
    reason: str = ""
    os: str = ""
    image_input: bool = False
    default_locale: str = ""
    speech_locales: tuple[str, ...] = ()
    installed_locales: tuple[str, ...] = ()
    checked_at: float = field(default_factory=time.monotonic)


# ------------------------------------------------------------------ the binary


def _macos_major() -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return None


def platform_problem() -> str | None:
    """Why this MACHINE cannot run the tier at all, before any process starts.
    None when the OS and chip qualify. Cheap, and the reason the helper is
    never even looked for on a Mac below 26 (which keeps the app's own
    `LSMinimumSystemVersion = 11.0` honest — the helper links 26-only
    frameworks and is only ever spawned past this gate)."""
    if sys.platform != "darwin":
        return "the apple provider runs Apple's on-device models and needs macOS"
    if platform.machine() != "arm64":
        return "the apple provider needs Apple Silicon (this Mac is Intel)"
    major = _macos_major()
    if major is not None and major < MIN_MACOS_MAJOR:
        return (f"the apple provider needs macOS {MIN_MACOS_MAJOR} or newer "
                f"(this Mac runs {platform.mac_ver()[0]})")
    return None


def _dev_build_dir() -> str:
    from fused_render.shell.storage import home_dir
    return os.path.join(home_dir(), "ai", "apple")


def _source_digest() -> str:
    try:
        with open(SOURCE, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:16]
    except OSError:
        return "nosource"


def _dev_build(problems: list[str]) -> str | None:
    """Compile the helper into the user dir, once per source revision.

    Only a checkout ever reaches this — the packaged app carries the binary —
    and only when `swiftc` targets a 26 SDK. Failure is recorded in
    `problems` (shown as the probe's reason) rather than raised: a missing
    toolchain is a state the tier reports, not a server error.
    """
    swiftc = shutil.which("swiftc")
    if not swiftc:
        problems.append("no `swiftc` on PATH — install Xcode 26 to build the helper")
        return None
    target = os.path.join(_dev_build_dir(), f"{HELPER_NAME}-{_source_digest()}")
    if os.path.isfile(target) and os.access(target, os.X_OK):
        return target
    os.makedirs(os.path.dirname(target), exist_ok=True)
    cmd = [swiftc, "-O", "-target", f"arm64-apple-macos{MIN_MACOS_MAJOR}.0",
           "-framework", "FoundationModels", "-framework", "Speech",
           "-framework", "AVFoundation", "-o", target, SOURCE]
    try:
        # encoding pinned: a GUI-launched server has no LANG, and swiftc's
        # diagnostics carry curly quotes (test_subprocess_encoding).
        run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        problems.append(f"building the helper failed: {e}")
        return None
    if run.returncode != 0:
        tail = (run.stderr or run.stdout or "").strip().splitlines()[-3:]
        problems.append("building the helper failed (is Xcode 26 selected? `xcode-select -p`): "
                        + " / ".join(tail))
        return None
    return target


def helper_path(problems: list[str] | None = None) -> str | None:
    """The binary to spawn, or None (with the reasons appended to `problems`)."""
    problems = problems if problems is not None else []
    explicit = os.environ.get(HELPER_ENV)
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        problems.append(f"{HELPER_ENV}={explicit!r} is not an executable file")
        return None
    if getattr(sys, "frozen", None) == "macosx_app":
        bundled = os.path.join(os.path.dirname(sys.executable), HELPER_NAME)
        if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
            return bundled
        problems.append("this build of the app ships without the Apple helper")
        return None
    if os.path.isfile(CHECKOUT_BIN) and os.access(CHECKOUT_BIN, os.X_OK):
        return CHECKOUT_BIN
    return _dev_build(problems)


def _binary() -> str:
    problem = platform_problem()
    if problem:
        raise AppleError("unavailable", problem)
    problems: list[str] = []
    path = helper_path(problems)
    if not path:
        raise AppleError("unavailable", "; ".join(problems) or "the Apple helper is missing")
    return path


# ------------------------------------------------------------------ requests


def frames(op: str, request: dict | None = None, *, first_timeout: float = FIRST_FRAME_TIMEOUT_S,
           on_spawn=None):
    """Yield the helper's frames for ONE request, until its `done` or its exit.

    A generator, so a consumer that stops early (a page that disconnected
    mid-stream, a ✕ on the row) triggers the `finally`, which TERMINATES the
    child — the model must not keep generating for nobody. `on_spawn(proc)`
    hands the caller the process for its own cancel path.

    The first frame is bounded (`first_timeout`); after that the child's own
    exit is the bound — a transcription of a long file legitimately says
    nothing for a while, and the job's row is what a caller watches.
    """
    proc = subprocess.Popen(
        [_binary(), op], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    if on_spawn is not None:
        on_spawn(proc)
    finished = False
    # Created BEFORE anything that can raise: the `finally` sets it, and a
    # stdin write failing on an already-dead child must surface as its own
    # AppleError, not as an UnboundLocalError that also skips the terminate.
    got_first = threading.Event()
    watchdog_fired = threading.Event()
    try:
        try:
            proc.stdin.write(json.dumps(request or {}).encode("utf-8"))
            proc.stdin.close()
        except (OSError, ValueError) as e:
            raise AppleError("ai_unavailable", f"the Apple helper did not accept the request: {e}") from e
        # The first-frame timeout, without a reader thread: a timer that kills
        # the child if nothing has arrived, which makes the blocking readline
        # below return.

        def _watchdog() -> None:
            if not got_first.wait(first_timeout) and proc.poll() is None:
                watchdog_fired.set()
                proc.terminate()

        threading.Thread(target=_watchdog, daemon=True, name="apple-ai-watchdog").start()
        for raw in proc.stdout:
            got_first.set()
            try:
                frame = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if frame.get("type") == "done":
                finished = True
                yield frame
                return
            yield frame
        got_first.set()
        # stdout closed with no `done`: the child died, the watchdog fired, or
        # somebody cancelled it. Cancel is `terminate()` (one-shot design: no
        # protocol for it), so the child never says "cancelled" itself — the
        # SIGTERM exit stands in for that frame. Only the watchdog's own
        # SIGTERM is a timeout; every other one was asked for (`cancel()`, a
        # row's ✕, `/api/ai/cancel`), and a consumer sees the same `done` the
        # local tier's worker sends on its cooperative cancel.
        if not finished:
            code = proc.wait()
            if code in (-15, 143) and not watchdog_fired.is_set():
                finished = True
                yield {"type": "done", "ok": True, "cancelled": True, "finishReason": "cancelled"}
                return
            raise AppleError("timeout" if code in (-15, 143) else "ai_error",
                             f"the Apple helper produced nothing for {int(first_timeout)}s"
                             if code in (-15, 143) else
                             f"the Apple helper exited without finishing (code {code})")
    finally:
        got_first.set()
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass


_lock = threading.Lock()
_probe_cache: Availability | None = None
#: Text children in flight, so `/api/ai/cancel` with `provider: "apple"` has
#: something to stop (the local tier's analogue is `supervisor.
#: cancel_generation`, which POSTs `/cancel` to the one worker).
_text_children: set[subprocess.Popen] = set()


def track_text(proc: subprocess.Popen) -> None:
    with _lock:
        _text_children.add(proc)
        # Reap the ones already gone, so the set cannot grow unbounded.
        for child in [c for c in _text_children if c.poll() is not None]:
            _text_children.discard(child)


def cancel(proc: subprocess.Popen) -> None:
    """Stop one request's child. `frames()` turns the resulting SIGTERM exit
    into a `cancelled` done frame, so callers cancel through this and never
    terminate the process themselves."""
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


def cancel_text() -> bool:
    """Stop every `afm-text` generation in flight. True when there was one."""
    with _lock:
        live = [c for c in _text_children if c.poll() is None]
    for child in live:
        cancel(child)
    return bool(live)


def probe(force: bool = False) -> Availability:
    """Whether the tier can answer on this machine right now. Cached for
    `PROBE_TTL_S`; never raises — a failure to probe IS an unavailability."""
    global _probe_cache
    cached = _probe_cache
    if cached is not None and not force and time.monotonic() - cached.checked_at < PROBE_TTL_S:
        return cached
    problem = platform_problem()
    if problem:
        result = Availability(False, "unavailable", problem)
    else:
        try:
            frame = next(frames("probe", first_timeout=PROBE_TIMEOUT_S), None)
            if frame is None or frame.get("type") != "probe":
                error = (frame or {}).get("error") or {}
                result = Availability(False, "unavailable",
                                      str(error.get("message") or "the Apple helper could not report availability"))
            else:
                result = Availability(
                    ok=bool(frame.get("available")),
                    state=str(frame.get("state") or ("available" if frame.get("available") else "unavailable")),
                    reason=str(frame.get("reason") or ""),
                    os=str(frame.get("os") or ""),
                    image_input=bool(frame.get("imageInput")),
                    default_locale=str(frame.get("defaultLocale") or ""),
                    speech_locales=tuple(frame.get("speechLocales") or ()),
                    installed_locales=tuple(frame.get("installedLocales") or ()),
                )
        except AppleError as e:
            result = Availability(False, "unavailable", str(e))
    _probe_cache = result
    return result


def shutdown() -> None:
    """Stop whatever is in flight and forget the probe (app exit, tests)."""
    global _probe_cache
    cancel_text()
    with _lock:
        _text_children.clear()
        _probe_cache = None


def reset() -> None:
    """Tests: forget the cache."""
    shutdown()
