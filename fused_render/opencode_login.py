"""Signing OpenCode in, by running its own `opencode auth login`.

The OpenCode twin of `gemini_login.py`, and structurally that module: a live
child under a lock, a watchdog, a cancel, and a client that polls until the
health snapshot says signed in. The public surface (`start`, `status`,
`cancel`, `reset`, `LoginError`, the record's keys) is IDENTICAL so the
frontend's generic sign-in strip works unchanged.

What differs is the child (docs/OPENCODE_BACKEND.md D10). OpenCode has no
ACP `authenticate` flow worth driving: `initialize` lists one method,
`opencode-login`, whose description is "Run `opencode auth login` in the
terminal". That command is INTERACTIVE — a provider picker, then a key or a
browser flow, on a TTY — so the module spawns it under a pty (`pty.openpty`;
pipes on a platform without one) and the record's `detail` tells the strip
to say "finish signing in in the terminal window". The child's output is
kept IN MEMORY ONLY, capped, for the failure sentence; a pasted key or an
authorize URL reaches no snapshot and no log.

**Success is never inferred from the exit code alone.** A clean exit
re-measures the health snapshot; whether the sign-in took is the ACP probe's
answer ("at least one usable provider").
"""
from __future__ import annotations

import codecs
import collections
import dataclasses
import logging
import os
import subprocess
import threading
import time
from typing import Optional

from fused_render import opencode_health
from fused_render.claude_health import SUBPROCESS_KWARGS

logger = logging.getLogger(__name__)

#: A person at a provider picker, then a key paste or a browser round trip.
LOGIN_TIMEOUT_S = 10 * 60.0
CANCEL_GRACE_S = 5.0
REAP_TIMEOUT_S = 30.0
_OUTPUT_TAIL_LINES = 40
#: os.read on the raw fd — see claude_login._READ_CHUNK for why the buffered
#: forms deadlock against a child that prints a prompt with no newline.
_READ_CHUNK = 2048

LOGIN_ARGS = ("auth", "login")
#: What the strip shows while the child runs (the record's `detail`).
DETAIL = {"kind": "terminal",
          "message": "finish signing in in the terminal window",
          "command": "opencode auth login"}


class LoginError(RuntimeError):
    """A refusal with a sentence the strip can show as-is."""


@dataclasses.dataclass
class _Login:
    proc: subprocess.Popen
    started_at: float
    #: The child's own words, capped, IN MEMORY ONLY.
    tail: collections.deque
    #: The pty master fd (None when the child runs on pipes).
    master_fd: Optional[int] = None
    error: Optional[str] = None
    #: `DETAIL` while the child runs; kept on the record afterwards too, so
    #: a strip that polls late still reads the same shape.
    detail: Optional[dict] = None
    #: True once the child exited 0, else the exit code's sentence.
    outcome: Optional[object] = None
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
    """One output line joins the tail (control sequences and all — it is
    only ever read back for the failure sentence)."""
    cleaned = line.rstrip("\r")
    if cleaned.strip():
        login.tail.append(cleaned)


def _failure(login: _Login, code: int) -> str:
    """One sentence for a sign-in that did not take: the child's last
    non-URL, non-prompt line, so a pasted key or an authorize URL never
    reaches the UI."""
    for line in reversed(login.tail):
        cleaned = line.strip()
        if not cleaned or "://" in cleaned:
            continue
        if cleaned.lower().startswith(("enter ", "paste ", "api key")):
            continue
        return cleaned
    return f"the sign-in ended without signing in (exit code {code})"


def _drain(login: _Login) -> None:
    """Keep a bounded tail of the child's output, until EOF (or EIO on a pty
    whose slave side closed)."""
    fd = login.master_fd
    if fd is None:
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
                _note_line(login, line)
    except (OSError, ValueError):
        pass   # EIO: the pty's last writer went away
    if buffer.strip():
        _note_line(login, buffer)


def _close_master(login: _Login) -> None:
    fd, login.master_fd = login.master_fd, None
    if fd is not None:
        try:
            os.close(fd)
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
    child is silent for as long as the human is at the picker."""
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
            _close_master(login)
            try:
                if login.proc.stdin is not None:
                    login.proc.stdin.close()
            except (OSError, ValueError):
                pass
        login.outcome = True if code == 0 else code

        if login.timed_out:
            login.error = (f"the sign-in was not finished within "
                           f"{int(LOGIN_TIMEOUT_S) // 60} minutes and was stopped")
        elif login.canceled:
            login.error = None
        elif login.outcome is not True:
            login.error = _failure(login, code)
        else:
            # THE RE-PROBE, server-side and before `settled` — the same
            # argument claude_login._run makes. A clean exit is not success;
            # the ACP probe the refresh runs is.
            try:
                opencode_health.summary_refreshed()
            except Exception:  # noqa: BLE001 - a failed probe is not a failed login
                logger.warning("the OpenCode sign-in finished but the re-probe failed")
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
    """opencode_health's child env (autoupdate off, PATH augmented), plus a
    TERM so the picker renders."""
    env = opencode_health._child_env()
    env.setdefault("TERM", "xterm-256color")
    return env


def _spawn(path: str) -> tuple:
    """`(proc, master_fd)`: the child under a pty where the platform has one,
    else on pipes (the command then runs non-interactively and reports what
    it needs)."""
    argv = [path, *LOGIN_ARGS]
    try:
        import pty
        master, slave = pty.openpty()
    except (ImportError, OSError):
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=_child_env(), **SUBPROCESS_KWARGS)
        return proc, None
    try:
        proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave,
            env=_child_env(), **SUBPROCESS_KWARGS)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return proc, master


def start() -> dict:
    """Run `opencode auth login` under a pty, and return the opening record.
    Refuses rather than queues: two sign-ins means two pickers writing the
    same `auth.json`."""
    path, _source = opencode_health.resolve()
    if not path or not opencode_health.executable(path):
        raise LoginError("there is no OpenCode on this machine to sign in to")

    global _active
    with _LOCK:
        if _active is not None and _active.alive():
            raise LoginError(
                "an OpenCode sign-in is already waiting in the terminal")
        try:
            proc, master = _spawn(path)
        except (OSError, ValueError) as exc:
            raise LoginError(f"could not start the sign-in: {exc}") from exc
        login = _Login(proc=proc, started_at=time.time(),
                       tail=collections.deque(maxlen=_OUTPUT_TAIL_LINES),
                       master_fd=master, detail=dict(DETAIL))
        _active = login
        opening = _record(login)

    threading.Thread(target=_run, args=(login,), daemon=True).start()
    return opening


def cancel() -> dict:
    """End a sign-in the user gave up on. Idempotent; `terminate` so the
    child can leave its picker cleanly."""
    login = _live()
    if login is None:
        return {**status(), "canceled": False}
    login.canceled = True
    _stop(login, force=False)
    if not login.settled.wait(CANCEL_GRACE_S):
        logger.warning("the OpenCode sign-in child did not stop when asked")
    return {**_record(login), "canceled": True}


def reset() -> None:
    """Drop the record. For tests — production has `cancel`."""
    global _active
    with _LOCK:
        _active = None
