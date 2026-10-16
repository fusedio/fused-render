"""Signing GitHub Copilot CLI in, by letting `copilot login` do the work.

The Copilot twin of `claude_login.py`, and structurally that module: a live
child under a lock, a watchdog, a cancel, and a client that polls until the
health snapshot says signed in. Read that docstring for why this is not a third
install action (it waits on a PERSON, so it needs a cancel and must not hold the
install slot).

What `copilot login` does under pipes (observed 2026-10-16, 1.0.98): on a
desktop it runs the browser flow — opens the browser itself, prints the
authorize URL as a fallback, and blocks on its own loopback callback. On a
machine it judges headless (SSH, containers, CI) it falls back to the device
flow and prints a one-time code with a URL to paste it at. The app handles
neither URL: the browser URL carries `state` and `code_challenge` and is never
shown; the device code IS shown, because the user has to type it somewhere.

**The gh token is stripped from the child's env.** `copilot login` honours
COPILOT_GITHUB_TOKEN/GH_TOKEN/GITHUB_TOKEN and uses one instead of opening a
browser — the opposite of what pressing "Sign in" means. The health probe
falls back to the gh token (D10) and this module deliberately does not.

**Success is never inferred from the exit code.** A clean exit re-measures the
health snapshot; whether the sign-in took is the ACP probe's answer.
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

from fused_render import copilot_health
from fused_render.claude_health import SUBPROCESS_KWARGS

logger = logging.getLogger(__name__)

#: Same ceiling as claude_login.LOGIN_TIMEOUT_S, same reason.
LOGIN_TIMEOUT_S = 600.0
CANCEL_GRACE_S = 5.0
REAP_TIMEOUT_S = 30.0
_OUTPUT_TAIL_LINES = 40
#: os.read on the raw fd — see claude_login._READ_CHUNK for why the buffered
#: forms deadlock against a child that prints a prompt with no newline.
_READ_CHUNK = 2048

#: A device-flow one-time code, as GitHub spells them (`ABCD-1234`).
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")
#: The device-flow URL is the one WITHOUT a query string; the browser-flow
#: authorize URL carries `state`/`code_challenge` and is never surfaced.
_DEVICE_URL_RE = re.compile(r"(https://github\.com/login/device\S*)")


class LoginError(RuntimeError):
    """A refusal with a sentence the strip can show as-is."""


@dataclasses.dataclass
class _Login:
    proc: subprocess.Popen
    started_at: float
    #: The child's own words, capped, IN MEMORY ONLY — the authorize line
    #: carries OAuth state (claude_login._Login.tail has the argument).
    tail: collections.deque
    error: Optional[str] = None
    #: `{code, url}` once the child printed a device-flow code; None on the
    #: browser flow. The one thing from the tail that reaches the UI.
    detail: Optional[dict] = None
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


def _note_device_code(login: _Login, line: str) -> None:
    """Pick a device code / URL out of one line, if it carries one. The
    authorize line is skipped outright: its `state` is random base64url and
    could spell something the code pattern accepts."""
    if "oauth/authorize" in line:
        return
    code = _DEVICE_CODE_RE.search(line)
    url = _DEVICE_URL_RE.search(line)
    if not code and not url:
        return
    detail = dict(login.detail or {"code": None, "url": None})
    if code:
        detail["code"] = code.group(1)
    if url and "?" not in url.group(1):
        detail["url"] = url.group(1)
    login.detail = detail


def _failure(login: _Login, code: int) -> str:
    """One sentence for a child that exited without signing in — the child's
    last non-URL line, so the authorize URL never reaches the UI."""
    for line in reversed(login.tail):
        cleaned = line.strip()
        if not cleaned or "://" in cleaned:
            continue
        if cleaned.startswith(("Opening your browser", "If it doesn't open",
                               "Waiting for authorization")):
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
    try:
        while True:
            raw = os.read(fd, _READ_CHUNK)
            if not raw:
                break
            buffer += decoder.decode(raw)
            *lines, buffer = buffer.split("\n")
            for line in lines:
                if line.strip():
                    login.tail.append(line.rstrip("\r"))
                    _note_device_code(login, line)
    except (OSError, ValueError):
        pass
    if buffer.strip():
        login.tail.append(buffer.rstrip("\r"))
        _note_device_code(login, buffer)
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
    child is silent for as long as the human is in the browser."""
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
            # The PIPE start() opened for the device flow; nothing writes it.
            try:
                if login.proc.stdin is not None:
                    login.proc.stdin.close()
            except (OSError, ValueError):
                pass

        if login.timed_out:
            login.error = (f"the sign-in was not finished within "
                           f"{int(LOGIN_TIMEOUT_S) // 60} minutes and was stopped")
        elif login.canceled:
            login.error = None
        elif code != 0:
            login.error = _failure(login, code)
        else:
            # THE RE-PROBE, server-side and before `settled` — the same
            # argument claude_login._run makes. A clean exit is not success;
            # the ACP probe the refresh runs is.
            try:
                copilot_health.summary_refreshed()
            except Exception:  # noqa: BLE001 - a failed probe is not a failed login
                logger.warning("the Copilot sign-in finished but the re-probe failed")
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
    """copilot_health's child env MINUS every token variable — see the module
    docstring: a token in the env makes `copilot login` skip the browser."""
    env = copilot_health._child_env()
    for name in copilot_health.TOKEN_ENV:
        env.pop(name, None)
    return env


def start() -> dict:
    """Open a browser sign-in, and return the opening record. Refuses rather
    than queues: two sign-ins means two loopback listeners racing."""
    path, _source = copilot_health.resolve()
    if not path or not copilot_health.executable(path):
        raise LoginError("there is no Copilot CLI on this machine to sign in to")

    cmd = [path, "login"]

    global _active
    with _LOCK:
        if _active is not None and _active.alive():
            raise LoginError(
                "a Copilot sign-in is already waiting in your browser")
        try:
            proc = subprocess.Popen(
                cmd,
                # A PIPE, NOT DEVNULL: the device-flow fallback may wait on
                # stdin, and closing it buys nothing on the browser path.
                stdin=subprocess.PIPE,
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
    """End a sign-in the user gave up on. Idempotent; `terminate` so the
    child closes its loopback socket itself."""
    login = _live()
    if login is None:
        return {**status(), "canceled": False}
    login.canceled = True
    _stop(login, force=False)
    if not login.settled.wait(CANCEL_GRACE_S):
        logger.warning("the Copilot sign-in child did not stop when asked")
    return {**_record(login), "canceled": True}


def reset() -> None:
    """Drop the record. For tests — production has `cancel`."""
    global _active
    with _LOCK:
        _active = None
