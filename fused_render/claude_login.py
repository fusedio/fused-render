"""Signing Claude Code in, by letting the CLI's own browser flow do the work.

`claude_health.py` establishes that the CLI is installed and NOT signed in;
`claude_install.py` repairs the two states that are a download away. This is the
third repair, and it was the one that looked impossible: `/login` is a TUI slash
command, so the advice the strip shipped with — open a terminal, run `claude`,
type `/login` — really is the only route through that door.

**There is a second door, and it does not need the code pasted anywhere.**
`claude auth login` is a plain subcommand that runs TWO flows at once:

* it opens the user's browser at ``http://localhost:<port>/callback``, a
  loopback listener it binds itself, and completes the sign-in when the redirect
  lands there; and
* it PRINTS a different URL — ``https://platform.claude.com/oauth/code/callback``
  — with a "Paste code here if prompted >" prompt, as the fallback for a machine
  whose browser cannot reach it (ssh, headless, a browser on another device).

Same authorization behind both: identical `state`, identical `code_challenge`,
two redirect targets, whichever completes first. On a desktop app with the user's
browser on the same machine the loopback path is the normal one — so THE APP
NEVER HANDLES AN OAUTH CODE. That is the whole reason this module is small.

**Do not read the printed URL and open it.** It is the paste URL; opening it
forces the user into the manual flow for no reason. The loopback URL is never
printed — the child hands it straight to the browser — so there is nothing here
worth capturing, and the child opens the browser itself, which is the one thing
that works on all three platforms (`BROWSER` is a POSIX convention; macOS and
Windows shell out to `open`/`start` and ignore it).

**Why this is not a third `claude_install` action.** Those two are machine time:
they run to completion on their own, so the record is progress and the endpoint
is fire-and-forget. This waits on a PERSON — it will sit at that prompt until the
watchdog kills it — so it needs a cancel, and it must never occupy the shared
install slot while it does. `canvases.py` already solved exactly this shape for
`fused login` (a CLI that opens a browser and blocks on its own loopback
callback) and the structure below is deliberately that one: a live child under a
lock, a watchdog, and a client that polls until the health snapshot says signed
in.

**Success is never inferred from the exit code.** The child exits on failure too
(`Login failed: …`). The authority on whether this worked is the one the rest of
the module already defers to — `claude auth status`, reached through the refresh
endpoint the strip calls anyway.
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

from fused_render import claude_health, claude_install

logger = logging.getLogger(__name__)

#: How long a sign-in may stay open before the watchdog ends it. Matches
#: canvases.LOGIN_CHILD_TIMEOUT, and for the same reason: this is a person
#: finding a browser window, reading a consent screen and probably a password
#: manager, not a machine doing work. The child itself never gives up — with no
#: browser it sits at the paste prompt indefinitely — so this is the only thing
#: that ends an abandoned attempt.
LOGIN_TIMEOUT_S = 600.0

#: How long `cancel` waits for a terminated child before letting go of it.
CANCEL_GRACE_S = 5.0

#: How many of the child's lines to keep. Bounded, and IN MEMORY ONLY — see
#: `_Login.tail`.
_OUTPUT_TAIL_LINES = 40

#: Read granularity for the drain. Deliberately an `os.read` on the raw fd, and
#: that is not a micro-optimisation — it is the only form that works here.
#:
#: Both buffered forms deadlock against this child. `for line in stdout` blocks
#: holding the paste prompt, which never gets a newline; `stdout.read(n)` blocks
#: until it has n CHARACTERS, and the child prints its ~700 and then waits on a
#: human indefinitely. Either way the drain returns nothing until the child dies,
#: which is exactly when its output has stopped being useful — the
#: `Login failed: …` line would be captured only after the record had already
#: fallen back to a generic sentence. `os.read` returns what one syscall got.
_READ_CHUNK = 2048

#: The child's stdin prompt, stripped out when deriving an error.
#:
#: STRIPPED, NOT SKIPPED, and that distinction was a bug. The prompt carries no
#: newline, so whatever the child says next lands on the SAME line: a real
#: failure arrives as `Paste code here if prompted > Login failed: Request failed
#: with status code 400`. Dropping every line that mentions the prompt therefore
#: threw away the diagnosis along with it, and the record fell back to quoting
#: `Opening browser to sign in…` as the reason the sign-in failed.
_PROMPT_RE = re.compile(r"Paste code here[^\n>]*>\s*")


class LoginError(RuntimeError):
    """A refusal with a sentence the strip can show as-is."""


@dataclasses.dataclass
class _Login:
    proc: subprocess.Popen
    started_at: float
    #: The child's own words, capped. NOT mirrored into `jobs.py` and never
    #: written to disk, which is the one place this module diverges from
    #: claude_install deliberately. That module publishes every line into the
    #: shared job registry because a verbatim installer error is the point. The
    #: same lines here carry the authorize URL's `state` and `code_challenge`,
    #: and phase 2 (the paste fallback) will put a live OAuth code on this
    #: stream. So the tail stays in memory, dies with the process, and leaves it
    #: only as the one derived sentence in `error`.
    tail: collections.deque
    error: Optional[str] = None
    timed_out: bool = False
    canceled: bool = False
    #: Set once the worker has drained, reaped and written `error`.
    #:
    #: IN FLIGHT IS THIS, NOT `proc.poll()`, and the difference is a race the
    #: user would see. The child dies the instant it fails, but its last line —
    #: the only diagnosis there is — is still moving through the drain. A record
    #: that went `in_flight: false` on the exit alone would be read by the strip
    #: in the window before `error` was written, so the row would come back with
    #: no explanation for why the sign-in did not take.
    settled: threading.Event = dataclasses.field(default_factory=threading.Event)

    def alive(self) -> bool:
        return not self.settled.is_set()


_LOCK = threading.Lock()
_active: Optional[_Login] = None


def _live() -> Optional[_Login]:
    """The record, only while its child is still running. Callers hold no lock."""
    with _LOCK:
        return _active if _active is not None and _active.alive() else None


def _failure(login: _Login, code: int) -> str:
    """One sentence for a child that exited without signing in.

    The child's own words where it gave any — `Login failed: Request failed with
    status code 400` is the loopback exchange rejecting the code, and rewording
    it would throw away the only diagnosis on offer.

    LINES CARRYING A URL ARE NEVER USED. The authorize line is the bulk of this
    child's output and it is not an error; excluding it by shape rather than by
    position also keeps the `state` nonce out of the UI by construction.
    """
    for line in reversed(login.tail):
        # The prompt is a PREFIX to strip, not a line to skip — see _PROMPT_RE.
        cleaned = _PROMPT_RE.sub("", line).strip()
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
    # An INCREMENTAL decoder, because reads land on syscall boundaries rather
    # than character ones: the CLI's own "Opening browser to sign in…" ends in a
    # 3-byte ellipsis that a chunk edge can split in half. Decoding each chunk
    # independently would turn that into replacement characters. Same encoding
    # and errors as claude_health.SUBPROCESS_KWARGS, so a genuinely undecodable
    # byte is still replaced rather than raising on a GUI-launched server that
    # inherited no LANG.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while True:
            raw = os.read(fd, _READ_CHUNK)
            if not raw:
                break
            buffer += decoder.decode(raw)
            # Split on newlines but KEEP the trailing fragment: that fragment is
            # usually the paste prompt, which never gets a newline of its own.
            *lines, buffer = buffer.split("\n")
            for line in lines:
                if line.strip():
                    login.tail.append(line.rstrip("\r"))
    except (OSError, ValueError):
        # A killed child closes the pipe under us. Not a failure of the sign-in.
        pass
    if buffer.strip():
        login.tail.append(buffer.rstrip("\r"))
    # Close the read end here rather than leaving it to the Popen's collection:
    # the record is held until the NEXT sign-in replaces it, so an unclosed pipe
    # is an fd kept for as long as the app runs, once per attempt.
    try:
        stream.close()
    except OSError:
        pass


def _expire(login: _Login) -> None:
    """The watchdog body: end a sign-in nobody finished."""
    login.timed_out = True
    try:
        login.proc.kill()  # unblocks the drain, which then sees EOF
    except OSError:
        pass


def _run(login: _Login) -> None:
    """Drain, reap, and settle the record. Never raises out of the thread.

    A TIMER, NOT A DEADLINE CHECKED IN THE READ LOOP — the same argument
    `claude_install._run` makes. This child is silent for the whole time a human
    spends in the browser, so a check that only ran when it spoke would never run
    at all.
    """
    try:
        watchdog = threading.Timer(LOGIN_TIMEOUT_S, _expire, args=(login,))
        watchdog.daemon = True
        watchdog.start()
        try:
            _drain(login)
            code = login.proc.wait(30)
        except subprocess.TimeoutExpired:
            # stdout is at EOF but the child has not reaped. NOT `_expire`: that
            # would label this a ten-minute login timeout in the record, when
            # what actually happened is a child that said its piece and would not
            # exit. Kill it and let its own last words explain it.
            try:
                login.proc.kill()
            except OSError:
                pass
            code = -1
        except OSError:
            code = -1
        finally:
            watchdog.cancel()

        if login.timed_out:
            login.error = (f"the sign-in was not finished within "
                           f"{int(LOGIN_TIMEOUT_S) // 60} minutes and was stopped")
        elif login.canceled:
            # Not a failure. The user said never mind, and inventing a sentence
            # about it would put an error under a row they just dismissed.
            login.error = None
        elif code != 0:
            login.error = _failure(login, code)
        # A CLEAN EXIT IS NOT REPORTED AS SUCCESS, and that is not caution: the
        # child exits 0 having written a credential this process never sees.
        # Whether it worked is `claude auth status`'s answer, and the client asks
        # the refresh endpoint for it.
    finally:
        # LAST, AND UNCONDITIONALLY. `error` must be readable the moment the
        # record stops saying "in flight", and a worker that died on the way out
        # must not wedge the button behind a sign-in nobody can finish.
        login.settled.set()


def _record(login: Optional[_Login]) -> dict:
    """The record as the endpoint answers it.

    `signed_in` is deliberately absent. This says whether a sign-in is in flight;
    whether one WORKED belongs to the health snapshot, and putting a second
    answer to that question here would be two sources of truth for the only fact
    the strip acts on.
    """
    if login is None:
        return {"in_flight": False, "started_at": None, "error": None}
    return {"in_flight": login.alive(), "started_at": login.started_at,
            "error": login.error}


def status() -> dict:
    """The current record. A cheap local read — no spawn, so no guard."""
    with _LOCK:
        return _record(_active)


def start() -> dict:
    """Open a browser sign-in, and return the opening record.

    Refuses rather than queues, like `claude_install.start`: two sign-ins at once
    means two loopback listeners and two authorizations racing, and the honest
    answer to "sign in again while a sign-in is open" is that one is.
    """
    path, _source = claude_health.resolve()
    if not path or not claude_health.executable(path):
        raise LoginError("there is no Claude Code on this machine to sign in to")

    # Through `_probe_cmd` for the reason the updater goes through it: npm
    # installs `claude` as a .cmd shim that CreateProcess cannot run, and a
    # signed-out npm install on Windows is exactly a machine this button is for.
    cmd = claude_health._probe_cmd(path, "auth", "login")

    global _active
    with _LOCK:
        if _active is not None and _active.alive():
            raise LoginError(
                "a Claude Code sign-in is already waiting in your browser")
        try:
            proc = subprocess.Popen(
                cmd,
                shell=isinstance(cmd, str),
                # A PIPE, NOT DEVNULL. Nothing is written to it on the loopback
                # path — but closing stdin forecloses the paste fallback for a
                # machine whose browser never opened, and buys nothing for it.
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=claude_install._child_env(),
                **claude_health.SUBPROCESS_KWARGS,
            )
        except (OSError, ValueError) as exc:
            raise LoginError(f"could not start the sign-in: {exc}") from exc
        login = _Login(proc=proc, started_at=time.time(),
                       tail=collections.deque(maxlen=_OUTPUT_TAIL_LINES))
        _active = login
        opening = _record(login)

    thread = threading.Thread(target=_run, args=(login,), daemon=True)
    thread.start()
    return opening


def cancel() -> dict:
    """End a sign-in the user gave up on. Idempotent.

    `terminate`, not `kill`: the child owns a loopback socket and is a step away
    from a credential store, and asking it to stop is the difference between
    closing those and orphaning them.

    It waits briefly for the child to go, so the record the caller gets back is
    the settled one — a cancel that returned `in_flight: true` would leave the
    strip polling a sign-in the user just abandoned.
    """
    login = _live()
    if login is None:
        return {**status(), "canceled": False}
    login.canceled = True
    try:
        login.proc.terminate()
    except OSError:
        logger.debug("the Claude Code sign-in child was already gone")
    if not login.settled.wait(CANCEL_GRACE_S):
        logger.warning("the Claude Code sign-in child did not stop when asked")
    return {**_record(login), "canceled": True}


def reset() -> None:
    """Drop the record. For tests — production has `cancel`."""
    global _active
    with _LOCK:
        _active = None
