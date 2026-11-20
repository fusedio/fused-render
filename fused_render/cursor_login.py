"""Signing Cursor's CLI in, by letting `cursor-agent login` do the work.

The Cursor twin of `codex_login.py`, and structurally that module: a live
child under a lock, a watchdog, a cancel, and a client that polls until the
health snapshot says signed in. Read `claude_login.py`'s docstring for why
this is not an install action (it waits on a PERSON, so it needs a cancel
and must not hold the install slot).

THE BROWSER FLOW (docs/CURSOR_BACKEND.md D10). `cursor-agent login` opens
the user's browser on Cursor's login page itself and polls Cursor's backend
until that page reports the sign-in, then stores the credential and exits 0.
There is no code to type, so nothing from the child's output reaches the UI:
the strip's own "finish signing in with the browser window that just
opened" is the whole instruction. The login URL the child prints carries a
one-time challenge, so the tail it lands in stays in memory and the failure
sentence skips any line holding a URL.

**The child's environment is the user's own**, minus `NO_OPEN_BROWSER`
(the one variable that would stop the window this flow needs).

**Success is never taken from the child's word alone.** A clean exit
re-measures the health snapshot; whether the sign-in took is
`cursor-agent status`'s answer.
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

from fused_render import cursor_health
from fused_render.claude_health import SUBPROCESS_KWARGS

logger = logging.getLogger(__name__)

#: A person at a browser login page.
LOGIN_TIMEOUT_S = 10 * 60.0
CANCEL_GRACE_S = 5.0
REAP_TIMEOUT_S = 30.0
_OUTPUT_TAIL_LINES = 40
#: os.read on the raw fd — see claude_login._READ_CHUNK for why the buffered
#: forms deadlock against a child that prints a prompt with no newline.
_READ_CHUNK = 2048

#: CSI escapes (colours, cursor moves) — the CLI may emit them under a pipe.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
#: The CLI's own success words (`status`'s "Login successful!" family).
_SUCCESS = ("Login successful", "Logged in as")


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
    #: Always None: the browser flow has no code to show (kept so the
    #: record's keys match every other agent's).
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
    """Note the success line, if this (plain) line is it."""
    if any(mark in line for mark in _SUCCESS):
        login.succeeded = True


def _failure(login: _Login, code: int) -> str:
    """One sentence for a child that exited without signing in — the child's
    last line that holds no URL."""
    for line in reversed(login.tail):
        cleaned = line.strip()
        if not cleaned or "://" in cleaned:
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
    child is silent for as long as the human is at the login page."""
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
            # line are the child's word; `status` is the answer.
            try:
                cursor_health.summary_refreshed()
            except Exception:  # noqa: BLE001 - a failed probe is not a failed login
                logger.warning("the Cursor sign-in finished but the re-probe failed")
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


def _child_env() -> dict:
    """`cursor_health._child_env` with the browser allowed again: that one
    sets `NO_OPEN_BROWSER=1` for the probes, and this flow is the browser."""
    env = cursor_health._child_env()
    env.pop("NO_OPEN_BROWSER", None)
    if os.environ.get("NO_OPEN_BROWSER"):
        env["NO_OPEN_BROWSER"] = os.environ["NO_OPEN_BROWSER"]
    return env


def start() -> dict:
    """Start a browser sign-in, and return the opening record. Refuses
    rather than queues: two login pages in flight would leave the user
    guessing which one counts."""
    path, _source = cursor_health.resolve()
    if not path or not cursor_health.executable(path):
        raise LoginError("there is no Cursor CLI on this machine to sign in to")

    cmd = [path, "login"]

    global _active
    with _LOCK:
        if _active is not None and _active.alive():
            raise LoginError("a Cursor sign-in is already waiting in the browser")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=_child_env(),
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

    SIGTERM first, SIGKILL only if that is ignored: `cursor-agent` is a
    launcher script in front of the Node process that polls for the login,
    and a SIGKILL cannot be forwarded — killing first could orphan the
    poller, and a login finished after Cancel would still sign the CLI in.
    """
    login = _live()
    if login is None:
        return {**status(), "canceled": False}
    login.canceled = True
    _stop(login, force=False)
    if not login.settled.wait(CANCEL_GRACE_S):
        _stop(login, force=True)
        if not login.settled.wait(CANCEL_GRACE_S):
            logger.warning("the Cursor sign-in child did not stop when killed")
    return {**_record(login), "canceled": True}


def reset() -> None:
    """Drop the record. For tests — production has `cancel`."""
    global _active
    with _LOCK:
        _active = None
