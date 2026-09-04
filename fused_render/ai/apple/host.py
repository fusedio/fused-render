"""The Apple tier's process host: one resident `fused-apple-ai`, NDJSON over stdio.

Shaped like `server/ai.py`'s `_AiSession` (a long-lived child the server
owns), NOT like a `registry.Runner`: the supervisor keeps ONE worker per
capability and evicts on engine mismatch, so an Apple "runner" would evict
the resident MLX model on every `provider: "apple"` call and be evicted
back on the next local one. The two tiers have to coexist, so this tier
holds its own process and borrows only the job-row helpers.

Protocol: see the header of `helper/main.swift`. Every request carries an
`id`; the reader thread demultiplexes replies into a per-request queue, so
a text stream and a transcription can run through the same child at once.
The child is spawned lazily on first use, respawned if it dies, and killed
from `shutdown()` at app exit.

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
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
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
#: enough that a page polling `models.list()` costs nothing; short enough that
#: turning Apple Intelligence on in System Settings shows up without a restart.
PROBE_TTL_S = 30.0
PROBE_TIMEOUT_S = 15.0
#: A generation that produces nothing for this long is dead, not slow — the
#: on-device model answers its first token in well under a second.
FIRST_FRAME_TIMEOUT_S = 120.0
FRAME_TIMEOUT_S = 600.0


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
    release = platform.mac_ver()[0]
    try:
        return int(release.split(".")[0])
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
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
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
    bundled = os.path.join(os.path.dirname(sys.executable), HELPER_NAME)
    if getattr(sys, "frozen", None) == "macosx_app":
        if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
            return bundled
        problems.append("this build of the app ships without the Apple helper")
        return None
    if os.path.isfile(CHECKOUT_BIN) and os.access(CHECKOUT_BIN, os.X_OK):
        return CHECKOUT_BIN
    return _dev_build(problems)


# ------------------------------------------------------------------ the child


class _Helper:
    """One child process and the reader thread that fans its replies out."""

    def __init__(self, path: str):
        self.path = path
        self.proc: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._queues: dict[str, queue.Queue] = {}
        self._queues_lock = threading.Lock()
        self._reader: threading.Thread | None = None

    # -- lifecycle

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.alive():
            return
        self.proc = subprocess.Popen(
            [self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        self._reader = threading.Thread(target=self._pump, name="apple-ai-reader", daemon=True)
        self._reader.start()

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        self._fail_all("the Apple helper stopped")

    def _fail_all(self, message: str) -> None:
        with self._queues_lock:
            pending, self._queues = self._queues, {}
        for q in pending.values():
            q.put({"type": "done", "ok": False,
                   "error": {"type": "ai_unavailable", "message": message}})

    def _pump(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            try:
                frame = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            rid = frame.get("id")
            with self._queues_lock:
                q = self._queues.get(rid) if isinstance(rid, str) else None
            if q is not None:
                q.put(frame)
        # stdout closed: the child is gone. Anything still waiting learns now
        # rather than at its own timeout.
        self._fail_all("the Apple helper exited unexpectedly")

    # -- requests

    def send(self, request: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise AppleError("ai_unavailable", "the Apple helper is not running")
        line = (json.dumps(request) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as e:
                raise AppleError("ai_unavailable", f"the Apple helper did not accept the request: {e}") from e

    def open(self, op: str, fields: dict) -> tuple[str, queue.Queue]:
        rid = uuid.uuid4().hex
        q: queue.Queue = queue.Queue()
        with self._queues_lock:
            self._queues[rid] = q
        try:
            self.send({"id": rid, "op": op, **fields})
        except AppleError:
            with self._queues_lock:
                self._queues.pop(rid, None)
            raise
        return rid, q

    def close(self, rid: str) -> None:
        with self._queues_lock:
            self._queues.pop(rid, None)

    def cancel(self, rid: str) -> None:
        try:
            self.send({"id": rid, "op": "cancel"})
        except AppleError:
            pass


_lock = threading.Lock()
_helper: _Helper | None = None
_probe_cache: Availability | None = None
#: Request ids of text generations in flight, so `/api/ai/cancel` with
#: `provider: "apple"` has something to stop (the local tier's analogue is
#: `supervisor.cancel_generation`, which POSTs `/cancel` to the one worker).
_text_in_flight: set[str] = set()


def _get_helper() -> _Helper:
    problem = platform_problem()
    if problem:
        raise AppleError("unavailable", problem)
    with _lock:
        global _helper
        if _helper is None or not _helper.alive():
            problems: list[str] = []
            path = helper_path(problems)
            if not path:
                raise AppleError("unavailable",
                                 "; ".join(problems) or "the Apple helper is missing")
            _helper = _Helper(path)
            _helper.start()
        return _helper


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
            helper = _get_helper()
            rid, q = helper.open("probe", {})
            try:
                frame = q.get(timeout=PROBE_TIMEOUT_S)
            finally:
                helper.close(rid)
            if frame.get("type") != "probe":
                error = frame.get("error") or {}
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
        except queue.Empty:
            result = Availability(False, "unavailable", "the Apple helper did not answer the availability check")
    _probe_cache = result
    return result


def frames(op: str, fields: dict, *, first_timeout: float = FIRST_FRAME_TIMEOUT_S,
           timeout: float = FRAME_TIMEOUT_S, track_text: bool = False):
    """Yield the helper's frames for one request until its `done`.

    A generator, so a consumer that stops early (a page that disconnected
    mid-stream) triggers the `finally`, which CANCELS the request in the
    helper — the model must not keep generating for nobody.
    """
    helper = _get_helper()
    rid, q = helper.open(op, fields)
    if track_text:
        with _lock:
            _text_in_flight.add(rid)
    finished = False
    try:
        wait = first_timeout
        while True:
            try:
                frame = q.get(timeout=wait)
            except queue.Empty:
                raise AppleError("timeout", f"the Apple helper produced nothing for {int(wait)}s")
            wait = timeout
            if frame.get("type") == "done":
                finished = True
                yield frame
                return
            yield frame
    finally:
        if not finished:
            helper.cancel(rid)
        helper.close(rid)
        if track_text:
            with _lock:
                _text_in_flight.discard(rid)


def cancel_text() -> bool:
    """Stop every `afm-text` generation in flight. True when there was one."""
    with _lock:
        pending = list(_text_in_flight)
        helper = _helper
    if not pending or helper is None:
        return False
    for rid in pending:
        helper.cancel(rid)
    return True


def shutdown() -> None:
    global _helper, _probe_cache
    with _lock:
        helper, _helper = _helper, None
        _probe_cache = None
    if helper is not None:
        helper.stop()


def reset() -> None:
    """Tests: forget the process and the cache."""
    shutdown()
