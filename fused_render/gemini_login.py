"""Signing Google Gemini CLI in, by letting the CLI's own OAuth flow do the work.

The Gemini twin of `copilot_login.py` and `codex_login.py`, and structurally
those modules: a live child under a lock, a watchdog, a cancel, and a client
that polls until the health snapshot says signed in. Read `claude_login.py`'s
docstring for why this is not a third install action (it waits on a PERSON,
so it needs a cancel and must not hold the install slot).

Gemini CLI has no `login` subcommand: signing in is the interactive `/auth`
dialog, or — the road taken here — ACP's own `authenticate` request. The
child is `gemini --acp`; the module sends `initialize` then `authenticate
{methodId: "oauth-personal"}` (the "Log in with Google" method every
`initialize` reply lists, verified 0.65.0), and the CLI runs its web flow:
opens the browser itself, prints the authorize URL as a fallback, listens on
its own loopback callback for up to five minutes, caches the token under
`~/.gemini/oauth_creds.json` and records `oauth-personal` as the selected
method in `settings.json`, so every later session signs in that way. The
request's reply (a result, or a `-32000` error with the CLI's sentence) is
the outcome.

**The browser is the point here**, so `NO_BROWSER` is removed from the
child's environment (the health probe sets it for the opposite reason). The
authorize URL carries `state` and a `code_challenge` and is never shown; the
strip says "finish signing in with the browser window that just opened", as
it does for Claude.

**Success is never inferred from the reply alone.** A result re-measures the
health snapshot; whether the sign-in took is the ACP probe's answer.
"""
from __future__ import annotations

import codecs
import collections
import dataclasses
import json
import logging
import os
import subprocess
import threading
import time
from typing import Optional

from fused_render import gemini_health
from fused_render.claude_health import SUBPROCESS_KWARGS

logger = logging.getLogger(__name__)

#: The CLI's own web flow gives up after five minutes; one more for the
#: initialize handshake and the token exchange, then this stops waiting.
LOGIN_TIMEOUT_S = 6 * 60.0
CANCEL_GRACE_S = 5.0
REAP_TIMEOUT_S = 30.0
_OUTPUT_TAIL_LINES = 40
#: os.read on the raw fd — see claude_login._READ_CHUNK for why the buffered
#: forms deadlock against a child that prints a prompt with no newline.
_READ_CHUNK = 2048

OAUTH_METHOD = "oauth-personal"
_INIT_ID = 1
_AUTH_ID = 2


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
    #: Always None: Gemini's flow has no device code to type. Kept so the
    #: record has the same keys as the other agents' (`ClaudeLoginStatus`).
    detail: Optional[dict] = None
    #: The `authenticate` reply, once it came: True on a result, else the
    #: error's message.
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
    """One stdout line: a JSON-RPC reply to our `authenticate` is the
    outcome; anything else (the flow's own prose) joins the tail."""
    try:
        msg = json.loads(line)
    except ValueError:
        login.tail.append(line.rstrip("\r"))
        return
    if not isinstance(msg, dict) or "method" in msg:
        return   # a notification or a server request: not an answer
    if msg.get("id") == _AUTH_ID:
        if "error" in msg:
            err = msg["error"]
            login.outcome = str((err.get("message") if isinstance(err, dict) else err)
                                or "the sign-in was refused")
        else:
            login.outcome = True
        # Done with the child: EOF on stdin ends the ACP server.
        try:
            if login.proc.stdin is not None:
                login.proc.stdin.close()
        except (OSError, ValueError):
            pass


def _failure(login: _Login, code: int) -> str:
    """One sentence for a sign-in that did not take: the CLI's own refusal
    when it gave one, else the child's last non-URL line, so the authorize
    URL never reaches the UI."""
    if isinstance(login.outcome, str) and login.outcome:
        return login.outcome
    for line in reversed(login.tail):
        cleaned = line.strip()
        if not cleaned or "://" in cleaned:
            continue
        if cleaned.startswith(("Attempting to open", "Otherwise navigate",
                               "Waiting for authentication")):
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
                    _note_line(login, line)
    except (OSError, ValueError):
        pass
    if buffer.strip():
        _note_line(login, buffer)
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
        elif login.outcome is not True:
            login.error = _failure(login, code)
        else:
            # THE RE-PROBE, server-side and before `settled` — the same
            # argument claude_login._run makes. A result is not success; the
            # ACP probe the refresh runs is.
            try:
                gemini_health.summary_refreshed()
            except Exception:  # noqa: BLE001 - a failed probe is not a failed login
                logger.warning("the Gemini sign-in finished but the re-probe failed")
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
    """gemini_health's child env MINUS `NO_BROWSER` — see the module
    docstring: here the browser window is what pressing "Sign in" means."""
    env = gemini_health._child_env()
    env.pop("NO_BROWSER", None)
    return env


def start() -> dict:
    """Open a browser sign-in, and return the opening record. Refuses rather
    than queues: two sign-ins means two loopback listeners racing."""
    path, _source = gemini_health.resolve()
    if not path or not gemini_health.executable(path):
        raise LoginError("there is no Gemini CLI on this machine to sign in to")

    global _active
    with _LOCK:
        if _active is not None and _active.alive():
            raise LoginError(
                "a Gemini sign-in is already waiting in your browser")
        try:
            proc = subprocess.Popen(
                [path, "--acp"],
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
    try:
        for msg in (
            {"jsonrpc": "2.0", "id": _INIT_ID, "method": "initialize",
             "params": {"protocolVersion": 1, "clientCapabilities": {},
                        "clientInfo": {"name": "fused-render", "version": "1"}}},
            {"jsonrpc": "2.0", "id": _AUTH_ID, "method": "authenticate",
             "params": {"methodId": OAUTH_METHOD}},
        ):
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
    except (OSError, ValueError) as exc:
        login.error = f"could not start the sign-in: {exc}"
        _stop(login, force=True)
        login.settled.set()
        raise LoginError(login.error) from exc

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
        logger.warning("the Gemini sign-in child did not stop when asked")
    return {**_record(login), "canceled": True}


def reset() -> None:
    """Drop the record. For tests — production has `cancel`."""
    global _active
    with _LOCK:
        _active = None
