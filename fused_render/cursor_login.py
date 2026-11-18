"""Signing Cursor CLI in, by running its own `cursor-agent login`.

The Cursor twin of `opencode_login.py` and `gemini_login.py`, and
structurally those modules: a live child under a lock, a watchdog, a cancel,
and a client that polls until the health snapshot says signed in. The public
surface (`start`, `status`, `cancel`, `reset`, `LoginError`, the record's
keys) is IDENTICAL so the frontend's generic sign-in strip works unchanged.

`cursor-agent login` opens the browser itself, prints the authorize URL as a
fallback, waits on its own callback and stores the credential. It runs under
a pty (`pty.openpty`; pipes on a platform without one) because the CLI draws
a TTY UI. **The browser is the point here**, so `NO_OPEN_BROWSER` is removed
from the child's environment (the health probes set it for the opposite
reason). The printed URL line is kept on the record's `detail` as a fallback
link for a machine where no browser opened; the rest of the child's output is
kept IN MEMORY ONLY, capped, for the failure sentence.

**Success is never inferred from the exit code alone.** A clean exit
re-measures the health snapshot; whether the sign-in took is `status`'s
answer.
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

#: A person in a browser tab.
LOGIN_TIMEOUT_S = 10 * 60.0
CANCEL_GRACE_S = 5.0
REAP_TIMEOUT_S = 30.0
_OUTPUT_TAIL_LINES = 40
#: os.read on the raw fd — see claude_login._READ_CHUNK for why the buffered
#: forms deadlock against a child that prints a prompt with no newline.
_READ_CHUNK = 2048

LOGIN_ARGS = ("login",)
#: What the strip shows while the child runs (the record's `detail`); `url`
#: fills in once the child prints its authorize line.
DETAIL = {"kind": "browser",
          "message": "finish signing in with the browser window that just opened",
          "command": "cursor-agent login",
          "url": None}
_URL_RE = re.compile(r"https?://\S+")


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
    only ever read back for the failure sentence); a URL in it is the
    fallback link."""
    cleaned = line.rstrip("\r")
    if not cleaned.strip():
        return
    login.tail.append(cleaned)
    url = _URL_RE.search(cleaned)
    if url and login.detail is not None and not login.detail.get("url"):
        login.detail = {**login.detail, "url": url.group(0).rstrip(").,")}


def _failure(login: _Login, code: int) -> str:
    """One sentence for a sign-in that did not take: the child's last
    non-URL, non-progress line, so an authorize URL never reaches the
    failure text."""
    for line in reversed(login.tail):
        cleaned = line.strip()
        if not cleaned or "://" in cleaned:
            continue
        if cleaned.lower().startswith(("opening ", "waiting ", "press ", "visit ")):
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
            # the `status` probe the refresh runs is.
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
    """cursor_health's child env MINUS `NO_OPEN_BROWSER` — see the module
    docstring: here the browser window is what pressing "Sign in" means — plus
    a TERM so the CLI's UI renders."""
    env = cursor_health._child_env()
    env.pop("NO_OPEN_BROWSER", None)
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
    """Run `cursor-agent login` under a pty, and return the opening record.
    Refuses rather than queues: two sign-ins means two browser tabs racing the
    same credential store."""
    path, _source = cursor_health.resolve()
    if not path or not cursor_health.executable(path):
        raise LoginError("there is no Cursor on this machine to sign in to")

    global _active
    with _LOCK:
        if _active is not None and _active.alive():
            raise LoginError(
                "an Cursor sign-in is already waiting in your browser")
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
    child can leave its prompt cleanly."""
    login = _live()
    if login is None:
        return {**status(), "canceled": False}
    login.canceled = True
    _stop(login, force=False)
    if not login.settled.wait(CANCEL_GRACE_S):
        logger.warning("the Cursor sign-in child did not stop when asked")
    return {**_record(login), "canceled": True}


def reset() -> None:
    """Drop the record. For tests — production has `cancel`."""
    global _active
    with _LOCK:
        _active = None
