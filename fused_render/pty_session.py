"""The pty session registry for the status-bar terminal.

Fork safety is the one way this module can crash the whole server, not just
one session. `pty.fork()` and any `subprocess.Popen` with `cwd=`,
`start_new_session=True`, or `preexec_fn` take the fork path; a fork() in a
process with libproj resident (any process that has imported pyproj/geopandas,
which this server has) runs PROJ's SQLite atfork handler against an
inherited-but-now-invalid handle and dies SIGSEGV before it can exec anything.
`fused_render/claude_spawn.py:129-134` documents this exact failure mode for
an identical situation — read it before touching the Popen call below.

The discipline this module follows to stay on the posix_spawn path:
  * `os.openpty()` runs HERE, in the server process. It is a plain syscall
    (two ptmx/pts opens under the hood), not a fork — safe.
  * The Popen below passes `close_fds=False` (the default, `True`, forces the
    fork+exec path on macOS/Linux — see executor.py's `_run_python` for the
    same fact verified against a different call site), an absolute
    interpreter path (`sys.executable`), and deliberately no `cwd=`, no
    `start_new_session=True`, no `preexec_fn`.
  * `setsid()`, `TIOCSCTTY`, and `chdir()` — everything that would otherwise
    tempt a caller into `start_new_session=True` or `cwd=` — move into
    `fused_render/_pty_exec_helper.py`, a bare-python target with no
    `fused_render` import, run in the freshly spawned child where fork() is
    once again safe (no libproj there yet).

Task 2 of PLAN-status-bar-terminal.md's test file asserts these Popen kwargs
explicitly, because a runtime SIGSEGV here would not otherwise point back at
this code.
"""
from __future__ import annotations

import fcntl
import os
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from queue import SimpleQueue
from typing import Optional

from fused_render.terminal_profiles import TerminalProfile, resolve_profile

_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pty_exec_helper.py")

#: Enough to repaint a screen on reattach without keeping unbounded history.
SCROLLBACK_CAP = 256 * 1024
_READ_CHUNK = 4096
#: SIGHUP, then SIGKILL after this long if the shell (or something it spawned,
#: e.g. vim) is still holding the terminal.
_KILL_GRACE_S = 2.0
#: A thread per live shell is fine at this size; if the cap ever rises this
#: wants the shared-ticker treatment fused_render/server/watch.py's
#: _WATCH_REGISTRY already gives stats (one ticker, many subscribers).
MAX_SESSIONS = 8


class SessionLimitError(RuntimeError):
    """Raised by `PtySessionRegistry.create` at the live-session cap."""


class PtySession:
    """One pty-backed shell: the master fd, its child process, a bounded
    scrollback ring, and the subscribers currently streaming its output."""

    def __init__(self, sid: str, profile: TerminalProfile):
        self.id = sid
        self.profile = profile
        self.alive = True
        self.exit_code: Optional[int] = None

        self._lock = threading.RLock()
        self._subscribers: list[SimpleQueue] = []
        self._scrollback = bytearray()

        master_fd, slave_fd = os.openpty()
        self.master_fd = master_fd
        try:
            # See module docstring: close_fds=False, absolute interpreter
            # path, no cwd=, no start_new_session=True, no preexec_fn. This
            # combination is what keeps this Popen on the posix_spawn path.
            self.proc = subprocess.Popen(
                [sys.executable, _HELPER, profile.cwd, *profile.argv],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                env=profile.env,
                close_fds=False,
            )
        finally:
            # The parent's copy of the slave fd must close so the master's
            # read() only sees EOF once the CHILD's copies are gone too
            # (the child inherited its own via stdin/stdout/stderr).
            os.close(slave_fd)

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- I/O ---------------------------------------------------------------

    def _read_loop(self) -> None:
        while True:
            try:
                data = os.read(self.master_fd, _READ_CHUNK)
            except OSError:
                # EIO is the ordinary "slave side fully closed" signal for a
                # pty master, not an error worth surfacing.
                data = b""
            if not data:
                break
            with self._lock:
                self._scrollback.extend(data)
                overflow = len(self._scrollback) - SCROLLBACK_CAP
                if overflow > 0:
                    del self._scrollback[:overflow]
                subs = list(self._subscribers)
            for q in subs:
                q.put(("data", data))

        code = self.proc.wait()
        with self._lock:
            self.alive = False
            self.exit_code = code
            subs = list(self._subscribers)
        for q in subs:
            q.put(("exit", code))
        try:
            os.close(self.master_fd)
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        if not self.alive:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        if not self.alive:
            return
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def scrollback(self) -> bytes:
        with self._lock:
            return bytes(self._scrollback)

    def subscribe(self) -> SimpleQueue:
        q: SimpleQueue = SimpleQueue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: SimpleQueue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # -- lifecycle -----------------------------------------------------------

    def kill(self) -> None:
        """SIGHUP the process group, then SIGKILL after a grace period. Safe
        to call more than once, and safe to call after the child has already
        exited on its own."""
        pgid = self.proc.pid  # setsid() in the helper makes this its own pgid
        try:
            os.killpg(pgid, signal.SIGHUP)
        except ProcessLookupError:
            return
        deadline = time.time() + _KILL_GRACE_S
        while time.time() < deadline and self.alive:
            time.sleep(0.05)
        if self.alive:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def join(self, timeout: Optional[float] = None) -> None:
        self._reader.join(timeout=timeout)


class PtySessionRegistry:
    """Per-app-instance registry of live pty sessions, capped at
    MAX_SESSIONS."""

    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: dict[str, PtySession] = {}

    def create(self, cwd: Optional[str] = None) -> PtySession:
        with self._lock:
            live = sum(1 for s in self._sessions.values() if s.alive)
            if live >= MAX_SESSIONS:
                raise SessionLimitError(
                    f"terminal session cap ({MAX_SESSIONS}) reached")
            profile = resolve_profile(cwd=cwd)
            if profile is None:
                raise RuntimeError("terminal is not supported on this platform")
            sid = uuid.uuid4().hex
            session = PtySession(sid, profile)
            self._sessions[sid] = session
            return session

    def get(self, sid: str) -> Optional[PtySession]:
        return self._sessions.get(sid)

    def list(self) -> list[PtySession]:
        with self._lock:
            return list(self._sessions.values())

    def kill(self, sid: str) -> bool:
        session = self._sessions.get(sid)
        if session is None:
            return False
        session.kill()
        return True

    def reap_dead(self) -> None:
        """Drop sessions whose child has exited from the registry so a new
        session can be created in their place. Live subscribers already got
        the {"exit": code} frame from the reader thread; this only affects
        `create`'s cap accounting."""
        with self._lock:
            dead = [sid for sid, s in self._sessions.items() if not s.alive]
            for sid in dead:
                del self._sessions[sid]

    def shutdown_all(self) -> None:
        """Kill every live session and wait for its reader thread to notice.
        Wired to the server's shutdown handler (Task 6) so a server restart
        never leaves an orphaned shell running."""
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            s.kill()
        for s in sessions:
            s.join(timeout=_KILL_GRACE_S + 1.0)


#: The one registry the server process uses. Tests construct their own
#: `PtySessionRegistry()` instead of touching this, so a test session never
#: leaks into another test or a real request.
REGISTRY = PtySessionRegistry()
