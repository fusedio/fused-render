"""Signing in to GitHub, by letting `gh`'s own browser flow do the work.

`github_setup.py` establishes that `gh` is installed and NOT signed in; this
is the repair. `gh auth login --web --git-protocol https` runs the OAuth
device-ish flow entirely on its own: it opens the user's browser, shows a
one-time code to confirm there, and waits for the confirmation to land —
without this app ever handling a token or a callback.

**Why this is not a third `github_setup` install action.** Install is machine
time: it downloads a binary and runs to completion on its own, so the record
is progress and the endpoint is fire-and-forget. This waits on a PERSON — it
sits at the browser until they act, or until the watchdog below gives up on
them — so it needs a cancel, and it must never occupy the shared install slot
while it does. `claude_login.py` already solved exactly this shape for
`claude auth login` and the structure here is deliberately that one: a live
child under its own lock, a watchdog, and a client that polls until the
health snapshot says signed in.

**What `--web` actually does at the terminal**, which shapes the child/drain
logic below and is simpler than `claude auth login`'s two-flow shape (no
paste-code fallback prompt to strip — `--web` has no manual-entry path):

    ! First copy your one-time code: 1234-ABCD
    Press Enter to open github.com in your browser...
    ✓ Authentication complete.

The second line is a REAL stdin prompt — unlike `claude auth login`'s
loopback path, `gh` will not proceed until something arrives on stdin. So the
child needs `stdin=PIPE` (same as claude_login.py, though for a different
reason: that module never writes to it either, keeping the pipe open only so
a headless machine's paste fallback stays possible), and this module writes
one newline right after the child starts to get past that prompt without a
human at the keyboard. On success the child prints "✓ Authentication
complete." and exits 0; on failure or timeout it exits non-zero with its own
words on stderr, merged into stdout by `stderr=STDOUT` below (same as
claude_login.py's Popen kwargs).

**On success this module also runs `gh auth setup-git`** — a quick, separate
`gh` invocation, run synchronously after the login child settles and before
declaring success. That is what makes a later `git push` work without this
app ever holding a credential of its own: the whole reason "Publish to
GitHub" delegates to `gh` instead of doing OAuth here is to let `gh` keep
being the thing git authenticates through.

**Success is never inferred from the exit code — least of all here.** The
child can exit non-zero for reasons that have nothing to do with whether the
sign-in itself took (a `setup-git`-shaped hiccup inside the flow, a
transient write to `gh`'s own config after the browser already confirmed),
so an exit-code check would misreport a real sign-in as a failure. The
authority this module defers to, exactly as `claude_login.py:_run` defers to
`claude_health.summary_refreshed()`, is a fresh `gh auth status` re-probe:
`github_setup.summary_refreshed()`. `setup-git` runs only once that re-probe
says `signed_in`, never before and never on its say-so alone.
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

from fused_render import github_setup

logger = logging.getLogger(__name__)

#: How long a sign-in may stay open before the watchdog ends it. Matches
#: claude_login.LOGIN_TIMEOUT_S, for the same reason: this is a person finding
#: a browser window and a one-time code, not a machine doing work. The child
#: itself never gives up — with no browser it sits at the prompt indefinitely
#: — so this is the only thing that ends an abandoned attempt.
LOGIN_TIMEOUT_S = 600.0

#: How long `cancel` waits for a terminated child before letting go of it.
CANCEL_GRACE_S = 5.0

#: How long to wait for a child to reap after its stdout has reached EOF. It
#: has already said everything it is going to; this is only the gap between
#: the pipe closing and the process record going away.
REAP_TIMEOUT_S = 30.0

#: How many of the child's lines to keep. Bounded, and IN MEMORY ONLY — see
#: `_Login.tail`.
_OUTPUT_TAIL_LINES = 40

#: Read granularity for the drain. Deliberately an `os.read` on the raw fd —
#: see `_drain`'s docstring for why that is the only form that does not
#: deadlock against this child.
_READ_CHUNK = 2048

#: How long the separate, synchronous `gh auth setup-git` call may take. It
#: rewrites a few lines of local git config, not a network call of its own —
#: bounded anyway so a wedged `gh` cannot pin the record after a sign-in that
#: otherwise succeeded.
_SETUP_GIT_TIMEOUT_S = 15

#: Lines that are normal, expected chatter from a HEALTHY `--web` run rather
#: than a diagnosis of a failed one — the one-time code banner and the stdin
#: prompt it waits on. Excluded from `_failure`'s search so a sign-in that
#: fails on its very first line does not get blamed on either.
_INFO_LINE_RE = re.compile(
    r"^\W*(?:First copy your one-time code|Press Enter to open github\.com)")


class LoginError(RuntimeError):
    """A refusal with a sentence the strip can show as-is."""


@dataclasses.dataclass
class _Login:
    proc: subprocess.Popen
    started_at: float
    #: The resolved `gh` path this child was started from — kept on the
    #: record rather than re-derived from `proc.args` (a fake `Popen` in tests
    #: need not carry one, and the real one is a list already known at
    #: `start()` time) so `_setup_git` can reuse the exact binary the sign-in
    #: itself ran.
    path: str
    #: The child's own words, capped. NOT mirrored into `jobs.py` and never
    #: written to disk — same discipline as claude_login._Login.tail. Nothing
    #: printed here carries a secret the way an OAuth `state`/PKCE challenge
    #: would, but there is still no reason for a sign-in's blow-by-blow to
    #: outlive the attempt, so the tail stays in memory and dies with it.
    tail: collections.deque
    error: Optional[str] = None
    timed_out: bool = False
    canceled: bool = False
    #: Set once the worker has drained, reaped, decided success, and (when
    #: applicable) run `setup-git`. IN FLIGHT IS THIS, NOT `proc.poll()` — the
    #: same race claude_login._Login.settled documents: the child can exit
    #: before the re-probe and `setup-git` call that decide what its exit
    #: meant have finished running.
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
    """One sentence for a child whose sign-in the re-probe did not confirm.

    The child's own last substantive line where it left one — "error: could
    not open a browser: exec: \"xdg-open\": executable file not found in
    $PATH" is a real, actionable diagnosis, and rewording it would throw that
    away. Lines that are just the one-time-code banner or the stdin prompt are
    skipped by shape (`_INFO_LINE_RE`) since they describe a HEALTHY run in
    progress, not why one failed.
    """
    for line in reversed(login.tail):
        cleaned = line.strip()
        if not cleaned or _INFO_LINE_RE.match(cleaned):
            continue
        return cleaned
    return f"the sign-in ended without signing in (exit code {code})"


def _drain(login: _Login) -> None:
    """Keep a bounded tail of the child's output, until EOF.

    Read granularity: a raw `os.read` on the underlying fd, and that is not a
    micro-optimisation — it is the only form that does not deadlock against
    this child. `for line in stdout` blocks waiting for a newline, and the
    stdin prompt above never gets one; `stdout.read(n)` blocks until it has n
    CHARACTERS, and `gh` prints well under that before it goes quiet waiting
    on the browser. Either buffered form would return nothing until the child
    exits, which is exactly when its output has stopped being useful for a
    still-running sign-in — see `test_output_is_captured_while_the_child_is_
    still_waiting`, the same regression claude_login.py's own `_drain`
    docstring documents for `claude auth login`. `os.read` returns whatever
    one syscall got, so a child that has spoken and is now waiting shows up in
    the tail immediately instead of only at exit.
    """
    stream = login.proc.stdout
    if stream is None:
        return
    try:
        fd = stream.fileno()
    except (OSError, ValueError):
        return
    buffer = ""
    # An INCREMENTAL decoder, because reads land on syscall boundaries rather
    # than character ones: a multi-byte UTF-8 character (the ✓ in "✓
    # Authentication complete.") can straddle a chunk edge. Decoding each
    # chunk independently would turn a split character into a replacement
    # glyph. Same encoding/errors as github_setup.SUBPROCESS_KWARGS, so a
    # genuinely undecodable byte is replaced rather than raising on a
    # GUI-launched server that inherited no LANG.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while True:
            raw = os.read(fd, _READ_CHUNK)
            if not raw:
                break
            buffer += decoder.decode(raw)
            # Split on newlines but KEEP the trailing fragment: that fragment
            # is usually the stdin prompt, which never gets a newline of its
            # own until the child moves past it.
            *lines, buffer = buffer.split("\n")
            for line in lines:
                if line.strip():
                    login.tail.append(line.rstrip("\r"))
    except (OSError, ValueError):
        # A killed child closes the pipe under us. Not a failure of the sign-in.
        pass
    if buffer.strip():
        login.tail.append(buffer.rstrip("\r"))
    try:
        stream.close()
    except OSError:
        pass


def _stop(login: _Login, force: bool) -> None:
    """Stop the child.

    `gh` ships as a real, native executable on every platform this app
    targets — never an npm-style `.cmd` shim — so, unlike
    `claude_login._stop`, there is no `shell=True` indirection to walk a
    process tree through: the child IS `gh` everywhere, and `terminate`/`kill`
    reach it directly on POSIX and Windows alike.

    `terminate`, not `kill`, for a plain cancel: the child is mid-OAuth-flow
    and possibly about to write a credential, and asking it to stop is the
    difference between it closing that out and being cut off mid-write. The
    watchdog still uses `force=True` — an abandoned sign-in gets no such
    courtesy.
    """
    proc = login.proc
    try:
        proc.kill() if force else proc.terminate()
    except OSError:
        pass


def _expire(login: _Login) -> None:
    """The watchdog body: end a sign-in nobody finished."""
    login.timed_out = True
    _stop(login, force=True)  # unblocks the drain, which then sees EOF


def _setup_git(path: str) -> None:
    """`gh auth setup-git`, run once and only behind a confirmed sign-in.

    This is the entire reason the app delegates to `gh` rather than doing
    OAuth itself: it points git's own credential helper at `gh`, so `git
    push` works afterwards without this process ever holding a token.

    Never raises. A `setup-git` hiccup does not undo a real sign-in — `gh
    auth status` already confirmed the credential exists — so this is logged
    and swallowed rather than turned into a login failure the user did not
    actually have.
    """
    try:
        subprocess.run(
            [path, "auth", "setup-git"], capture_output=True,
            timeout=_SETUP_GIT_TIMEOUT_S, **github_setup.SUBPROCESS_KWARGS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("gh auth setup-git could not be run after sign-in")


def _run(login: _Login) -> None:
    """Drain, reap, decide, and settle the record. Never raises out of the
    thread.

    A TIMER, NOT A DEADLINE CHECKED IN THE READ LOOP — this child is silent
    for the whole time a human spends confirming the code in their browser,
    so a check that only ran when it spoke would never run at all.
    """
    try:
        watchdog = threading.Timer(LOGIN_TIMEOUT_S, _expire, args=(login,))
        watchdog.daemon = True
        watchdog.start()
        try:
            _drain(login)
            code = login.proc.wait(REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # stdout is at EOF but the child has not reaped. NOT `_expire`:
            # that would label this a ten-minute login timeout in the record,
            # when what actually happened is a child that said its piece and
            # would not exit.
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
            # Not a failure. The user said never mind.
            login.error = None
        else:
            # THE RE-PROBE IS THE ONLY AUTHORITY, regardless of `code`. A
            # non-zero exit here does not mean the sign-in failed — see the
            # module docstring — so this asks `gh auth status` unconditionally
            # rather than only on a clean exit the way claude_login._run does.
            #
            # SERVER-SIDE, AND BEFORE `settled`, for the same reason
            # claude_login._run re-probes early: leaving it to the strip's own
            # poll made it conditional on the strip still being mounted, so a
            # user who signed in and walked away would come back to a
            # freshly-mounted strip reading a minute-old "signed out".
            try:
                fresh = github_setup.summary_refreshed()
            except Exception:  # noqa: BLE001 - a failed probe is not a failed login
                logger.warning("the sign-in finished but the re-probe failed")
                fresh = {"signed_in": False}
            if fresh.get("signed_in"):
                # `setup-git` behind the confirmed sign-in only — never on the
                # child's exit code alone.
                _setup_git(login.path)
                login.error = None
            else:
                login.error = _failure(login, code)
    finally:
        # LAST, AND UNCONDITIONALLY. `error` must be readable the moment the
        # record stops saying "in flight", and a worker that died on the way
        # out must not wedge the button behind a sign-in nobody can finish.
        login.settled.set()


def _record(login: Optional[_Login]) -> dict:
    """The record as the endpoint answers it.

    `signed_in` is deliberately absent — that is `github_setup.summary()`'s
    answer, and putting a second one here would be two sources of truth for
    the only fact the strip acts on.
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

    Refuses rather than queues, like `github_setup.install_start`: two
    sign-ins at once means two `gh` processes racing the same on-disk
    credential file, and the honest answer to "sign in again while a sign-in
    is open" is that one is.

    Deliberately independent of `github_setup`'s install lock — see the
    module docstring. Starting a login while an install is running (or the
    reverse) is not a conflict either module needs to know about.
    """
    path, _source = github_setup.resolve()
    if not path or not github_setup.executable(path):
        raise LoginError("there is no GitHub CLI on this machine to sign in to")

    cmd = [path, "auth", "login", "--web", "--git-protocol", "https"]

    global _active
    with _LOCK:
        if _active is not None and _active.alive():
            raise LoginError(
                "a GitHub sign-in is already waiting in your browser")
        try:
            proc = subprocess.Popen(
                cmd,
                # A PIPE, NOT DEVNULL — `--web` blocks on stdin at "Press
                # Enter to open github.com in your browser...", so closing it
                # would leave the child waiting on input that can never
                # arrive.
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **github_setup.SUBPROCESS_KWARGS,
            )
        except (OSError, ValueError) as exc:
            raise LoginError(f"could not start the sign-in: {exc}") from exc
        # THE AUTO-ADVANCE. Nothing else is ever written to this pipe — one
        # newline is all `--web` needs to stop waiting on a human who is not
        # there and go open the browser itself. Failures here (a child that
        # closed its own stdin before this line runs) are not fatal: the
        # watchdog still ends a sign-in that never gets past the prompt.
        try:
            if proc.stdin is not None:
                proc.stdin.write("\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        login = _Login(proc=proc, started_at=time.time(), path=path,
                       tail=collections.deque(maxlen=_OUTPUT_TAIL_LINES))
        _active = login
        opening = _record(login)

    thread = threading.Thread(target=_run, args=(login,), daemon=True)
    thread.start()
    return opening


def cancel() -> dict:
    """End a sign-in the user gave up on. Idempotent.

    `terminate`, not `kill` (see `_stop`): the child is a step away from
    writing a credential, and asking it to stop is the difference between
    closing that out cleanly and cutting it off mid-write.

    Waits briefly for the child to go, so the record the caller gets back is
    the settled one — a cancel that returned `in_flight: true` would leave the
    strip polling a sign-in the user just abandoned.
    """
    login = _live()
    if login is None:
        return {**status(), "canceled": False}
    login.canceled = True
    _stop(login, force=False)
    if not login.settled.wait(CANCEL_GRACE_S):
        logger.warning("the GitHub sign-in child did not stop when asked")
    return {**_record(login), "canceled": True}


def reset() -> None:
    """Drop the record. For tests — production has `cancel`."""
    global _active
    with _LOCK:
        _active = None
