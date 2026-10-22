"""Signing OpenAI Codex CLI in, by letting `codex login --device-auth` do the work.

The Codex twin of `copilot_login.py`, and structurally that module: a live
child under a lock, a watchdog, a cancel, and a client that polls until the
health snapshot says signed in. Read `claude_login.py`'s docstring for why this
is not a third install action (it waits on a PERSON, so it needs a cancel and
must not hold the install slot).

THE DEVICE FLOW, ALWAYS. Plain `codex login` opens a browser and blocks on a
loopback callback; `--device-auth` prints a URL and a one-time code and polls
until someone enters the code there (D10, verified 2026-10-21, stdout):

    1. Open this link in your browser and sign in to your account
       https://auth.openai.com/codex/device
    2. Enter this one-time code (expires in 15 minutes)
       9N23-GJGFW

then `Successfully logged in` and exit 0. The CLI colours both values with
ANSI escapes even under a pipe (`\\x1b[94m…\\x1b[0m`), so every line is
stripped of them before it is read. The device flow is the one that works the
same on a desktop, over SSH and in a container, and its two values are the
only things from the child's output that reach the UI.

**The child's environment is the user's own.** CODEX_HOME passes through
untouched — it is where the credential lands, and the health probe reads it
from the same place.

**Success is never taken from the child's word alone.** A clean exit (or the
success line) re-measures the health snapshot; whether the sign-in took is the
app-server probe's answer.
"""
from __future__ import annotations

import codecs
import collections
import dataclasses
import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional

from fused_render import codex_health
from fused_render.claude_health import SUBPROCESS_KWARGS

logger = logging.getLogger(__name__)

#: Longer than claude_login's ten minutes on purpose: Codex's own code lives
#: 15 minutes, and a watchdog that fires first kills a sign-in the user could
#: still finish. A minute of margin past the CLI's own expiry.
LOGIN_TIMEOUT_S = 16 * 60.0
CANCEL_GRACE_S = 5.0
REAP_TIMEOUT_S = 30.0
_OUTPUT_TAIL_LINES = 40
#: os.read on the raw fd — see claude_login._READ_CHUNK for why the buffered
#: forms deadlock against a child that prints a prompt with no newline.
_READ_CHUNK = 2048

#: CSI escapes (colours, cursor moves) — the CLI emits them under a pipe.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
#: A device-flow one-time code, as OpenAI spells them (`9N23-GJGFW`: four,
#: dash, five). Upper-case only, word-bounded, so the banner's `v0.163.0` and
#: ordinary prose never match.
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{5})\b")
#: The device page. A URL with a query string is never surfaced: that shape is
#: the browser flow's authorize URL, which carries OAuth `state`.
_DEVICE_URL_RE = re.compile(r"(https://auth\.openai\.com/codex/device\S*)")
#: The CLI's own success line (verified).
_SUCCESS = "Successfully logged in"
#: The instruction lines around the URL and code — never a failure reason.
_CHROME = ("WARNING:", "Welcome to Codex", "OpenAI's command-line",
           "Follow these steps", "1. Open this link", "2. Enter this one-time code",
           "Continue only if")


def _plain(line: str) -> str:
    """One line of the child's output without its ANSI escapes or a CR."""
    return _ANSI_RE.sub("", line).rstrip("\r")


class LoginError(RuntimeError):
    """A refusal with a sentence the strip can show as-is."""


@dataclasses.dataclass
class _Login:
    proc: subprocess.Popen
    started_at: float
    #: The child's own words (escapes stripped), capped, IN MEMORY ONLY —
    #: claude_login._Login.tail has the argument. Never in a response.
    tail: collections.deque
    error: Optional[str] = None
    #: `{code, url}` once the child printed them. The one thing from the tail
    #: that reaches the UI: the user has to type the code at the URL.
    detail: Optional[dict] = None
    #: The child printed its success line.
    succeeded: bool = False
    timed_out: bool = False
    canceled: bool = False
    settled: threading.Event = dataclasses.field(default_factory=threading.Event)

    def alive(self) -> bool:
        return not self.settled.is_set()


_LOCK = threading.Lock()
_active: Optional[_Login] = None


def _live() -> Optional[_Login]:
    with _LOCK:
        return _active if _active is not None and _active.alive() else None


def _note_line(login: _Login, line: str) -> None:
    """Pick the device code / URL / success line out of one (plain) line."""
    if _SUCCESS in line:
        login.succeeded = True
    code = _DEVICE_CODE_RE.search(line)
    url = _DEVICE_URL_RE.search(line)
    if not code and not url:
        return
    if url and "?" in url.group(1):
        url = None
        code = None  # an authorize URL's `state` could spell a "code"
    if not code and not url:
        return
    detail = dict(login.detail or {"code": None, "url": None})
    if code:
        detail["code"] = code.group(1)
    if url:
        detail["url"] = url.group(1)
    login.detail = detail


def _failure(login: _Login, code: int) -> str:
    """One sentence for a child that exited without signing in — the child's
    last line that is neither a URL, the code, nor the instructions around
    them."""
    for line in reversed(login.tail):
        cleaned = line.strip()
        if not cleaned or "://" in cleaned or _DEVICE_CODE_RE.fullmatch(cleaned):
            continue
        if cleaned.startswith(_CHROME):
            continue
        return cleaned
    return f"the sign-in ended without signing in (exit code {code})"


def _drain(login: _Login) -> None:
    """Keep a bounded tail of the child's output, until EOF."""
    stream = login.proc.stdout
    if stream is None:
        return
    try:
        fd = stream.fileno()
    except (OSError, ValueError):
        return
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def _take(raw_line: str) -> None:
        line = _plain(raw_line)
        if line.strip():
            login.tail.append(line)
            _note_line(login, line)

    try:
        while True:
            raw = os.read(fd, _READ_CHUNK)
            if not raw:
                break
            buffer += decoder.decode(raw)
            *lines, buffer = buffer.split("\n")
            for line in lines:
                _take(line)
    except (OSError, ValueError):
        pass
    _take(buffer)
    try:
        stream.close()
    except OSError:
        pass


def _stop(login: _Login, force: bool) -> None:
    try:
        login.proc.kill() if force else login.proc.terminate()
    except OSError:
        pass


def _expire(login: _Login) -> None:
    login.timed_out = True
    _stop(login, force=True)


def _run(login: _Login) -> None:
    """Drain, reap, settle. A timer, not a deadline in the read loop — the
    child is silent for as long as the human is at the device page."""
    try:
        watchdog = threading.Timer(LOGIN_TIMEOUT_S, _expire, args=(login,))
        watchdog.daemon = True
        watchdog.start()
        try:
            _drain(login)
            code = login.proc.wait(REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _stop(login, force=True)
            code = -1
        except OSError:
            code = -1
        finally:
            watchdog.cancel()

        if login.timed_out:
            login.error = (f"the sign-in was not finished within "
                           f"{int(LOGIN_TIMEOUT_S) // 60} minutes and was stopped")
        elif login.canceled:
            login.error = None
        elif code != 0 and not login.succeeded:
            login.error = _failure(login, code)
        else:
            # THE RE-PROBE, server-side and before `settled` — the same
            # argument claude_login._run makes. The exit code and the success
            # line are the child's word; the app-server probe is the answer.
            try:
                codex_health.summary_refreshed()
            except Exception:  # noqa: BLE001 - a failed probe is not a failed login
                logger.warning("the Codex sign-in finished but the re-probe failed")
    finally:
        login.settled.set()


def _record(login: Optional[_Login]) -> dict:
    """The record as the endpoint answers it. `signed_in` is absent on
    purpose — that fact belongs to the health snapshot alone."""
    if login is None:
        return {"in_flight": False, "started_at": None, "error": None,
                "detail": None}
    return {"in_flight": login.alive(), "started_at": login.started_at,
            "error": login.error, "detail": login.detail}


def status() -> dict:
    with _LOCK:
        return _record(_active)


def start() -> dict:
    """Start a device-code sign-in, and return the opening record (the code
    and URL follow on `status()` once the child prints them). Refuses rather
    than queues: two device codes in flight would leave the user guessing
    which one to type."""
    path, _source = codex_health.resolve()
    if not path or not codex_health.executable(path):
        raise LoginError("there is no Codex CLI on this machine to sign in to")

    cmd = [path, "login", "--device-auth"]

    global _active
    with _LOCK:
        if _active is not None and _active.alive():
            raise LoginError("a Codex sign-in is already waiting for its code")
        try:
            proc = subprocess.Popen(
                cmd,
                # The device flow reads nothing; an open pipe nobody writes
                # would only be one more thing to close.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=codex_health._child_env(),
                **SUBPROCESS_KWARGS,
            )
        except (OSError, ValueError) as exc:
            raise LoginError(f"could not start the sign-in: {exc}") from exc
        login = _Login(proc=proc, started_at=time.time(),
                       tail=collections.deque(maxlen=_OUTPUT_TAIL_LINES))
        _active = login
        opening = _record(login)

    threading.Thread(target=_run, args=(login,), daemon=True).start()
    return opening


def cancel() -> dict:
    """End a sign-in the user gave up on. Idempotent.

    SIGTERM first, SIGKILL only if that is ignored: an npm install's `codex`
    is a Node wrapper that forwards SIGTERM to the native binary, which is
    the process actually polling the device endpoint. A SIGKILL cannot be
    forwarded, so killing first would orphan the poller — and a code the
    user enters after pressing Cancel would still sign them in.
    """
    login = _live()
    if login is None:
        return {**status(), "canceled": False}
    login.canceled = True
    _stop(login, force=False)
    if not login.settled.wait(CANCEL_GRACE_S):
        _stop(login, force=True)
        if not login.settled.wait(CANCEL_GRACE_S):
            logger.warning("the Codex sign-in child did not stop when killed")
    return {**_record(login), "canceled": True}


def reset() -> None:
    """Drop the record. For tests — production has `cancel`."""
    global _active
    with _LOCK:
        _active = None
