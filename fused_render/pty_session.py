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

import os
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from queue import SimpleQueue
from typing import Optional

from fused_render.shell_integration import ShellEventParser, integrate
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


#: Size a session is assumed to have when the client never sent one.
DEFAULT_ROWS, DEFAULT_COLS = 24, 80
#: Scrolled-off lines the text renderer keeps (the ring bounds it anyway).
_TEXT_HISTORY = 5000
MAX_TEXT_LINES = 2000
DEFAULT_TEXT_LINES = 200


def render_screen_text(data: bytes, rows: int, cols: int, lines: int) -> str:
    """What a human would see after `data` was written to a `rows` x `cols`
    terminal: scrolled-off history plus the current screen, trailing blank
    lines trimmed, last `lines` lines.

    Replays through `pyte` (a pure-python VT100 emulator) so `\\r` progress
    bars, cursor moves, erases and full-screen redraws collapse to their final
    state instead of arriving as raw escape soup. Done on demand over the
    (<= SCROLLBACK_CAP) ring rather than incrementally in the reader thread: a
    live emulator would cost pure-python CPU on every byte of every session,
    always, to serve a read that happens rarely. Measured ~0.3 s for a full
    256 KiB ring. A ring truncated mid-escape starts with a few garbled
    cells; pyte ignores what it cannot parse.
    """
    import pyte

    rows = max(1, rows)
    cols = max(1, cols)
    screen = pyte.HistoryScreen(cols, rows, history=_TEXT_HISTORY)
    pyte.ByteStream(screen).feed(data)

    def row_text(line) -> str:
        return "".join(line[x].data for x in range(cols)).rstrip()

    out = [row_text(line) for line in screen.history.top]
    out.extend(row.rstrip() for row in screen.display)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out[-max(1, lines):])


def _process_name(pid: int) -> Optional[str]:
    """Best-effort command name of `pid`. Linux reads /proc (no spawn); macOS
    has no /proc, so it shells out to `ps` with the posix_spawn-safe Popen
    shape the module docstring spells out (absolute path, close_fds=False, no
    cwd=), since this runs inside the server process."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        pass
    ps = "/bin/ps" if os.path.exists("/bin/ps") else "/usr/bin/ps"
    try:
        res = subprocess.run([ps, "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2,
                             close_fds=False)
    except (OSError, subprocess.SubprocessError):
        return None
    name = res.stdout.strip()
    return os.path.basename(name) if name else None


class SessionLimitError(RuntimeError):
    """Raised by `PtySessionRegistry.create` at the live-session cap."""


def valid_winsize(rows: object, cols: object) -> Optional[tuple[int, int]]:
    """`(rows, cols)` if both are real ints in the unsigned 16-bit range the
    kernel's winsize takes (1..65535), else None. Bools and floats are
    rejected so a JSON `true`/`24.5` never turns into a size."""
    for v in (rows, cols):
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
            return None
    return int(rows), int(cols)  # type: ignore[arg-type]


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """TIOCSWINSZ on `fd`; an unrepresentable size or a failed ioctl is a no-op."""
    # fcntl/termios imported here, not at module scope: both are
    # POSIX-only and would raise ImportError on Windows the moment
    # ANYTHING imports this module — including `server/app.py`'s
    # unconditional `from ...routers.terminal import router`, which
    # would then take the whole app down before `resolve_profile()`'s
    # `os.name == "nt"` guard ever got a chance to degrade gracefully
    # (see `_pty_exec_helper.py` and `_env_install_worker.py`'s own
    # local `import fcntl` for the same reason). No live session is ever
    # constructed on Windows, so this function is simply never reached there.
    import fcntl
    import termios
    try:
        packed = struct.pack("HHHH", rows, cols, 0, 0)
    except struct.error:
        # Out of range for "HHHH" (an unsigned 16-bit int per field) —
        # the route already clamps/ignores this, but a caller that skips
        # that check (a test, a future direct caller) gets a no-op instead
        # of an unhandled exception.
        return
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


class PtySession:
    """One pty-backed shell: the master fd, its child process, a bounded
    scrollback ring, and the subscribers currently streaming its output."""

    def __init__(self, sid: str, profile: TerminalProfile,
                 rows: Optional[int] = None, cols: Optional[int] = None):
        self.id = sid
        self.profile = profile
        self.alive = True
        self.exit_code: Optional[int] = None
        # Fed from the reader thread; read by the /text route and the list.
        self.shell_state = ShellEventParser()
        self.last_activity = time.time()
        size = valid_winsize(rows, cols)
        self.rows, self.cols = size if size is not None else (DEFAULT_ROWS, DEFAULT_COLS)

        self._lock = threading.RLock()
        self._subscribers: list[SimpleQueue] = []
        self._scrollback = bytearray()

        master_fd, slave_fd = os.openpty()
        self.master_fd = master_fd
        try:
            # The size must be on the pty BEFORE the child starts: zsh reads
            # it once to draw its first prompt's PROMPT_SP padding, and a
            # 0x0 pty makes it assume 80 columns and leave a stray inverse
            # `%`. Done here, in the parent, on the fd — never via
            # preexec_fn — so the Popen below stays on posix_spawn.
            if size is not None:
                _set_winsize(master_fd, *size)
            try:
                # See module docstring: close_fds=False, absolute interpreter
                # path, no cwd=, no start_new_session=True, no preexec_fn.
                # This combination is what keeps this Popen on the
                # posix_spawn path.
                self.proc = subprocess.Popen(
                    [sys.executable, _HELPER, profile.cwd, *profile.argv],
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    env=profile.env,
                    close_fds=False,
                )
            finally:
                # The parent's copy of the slave fd must close so the
                # master's read() only sees EOF once the CHILD's copies are
                # gone too (the child inherited its own via
                # stdin/stdout/stderr).
                os.close(slave_fd)
        except BaseException:
            # Popen itself raised (ENOENT on the helper, EAGAIN under load):
            # no `proc` exists, no registry entry will ever be created for
            # this instance, so nothing else will ever close `master_fd`.
            # Without this, each failed create leaks a pty master.
            os.close(master_fd)
            raise

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
                self.last_activity = time.time()
                self.shell_state.feed(data)
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

    def shell_is_foreground(self) -> bool:
        """True if the shell itself (its own process group, per the
        `setsid()` in `_pty_exec_helper.py`) currently owns the terminal's
        foreground process group — false while a program the shell launched
        (vim, a REPL, `sleep 30`, a dev server) is running in it.

        Used by `POST /api/terminal/{sid}/input` (routers/terminal.py) to
        refuse an "open in terminal / run a command" request against a busy
        pane instead of typing `cd ... && cmd\\r` into whatever program has
        the terminal right now. A dead session, or one where the group
        comparison itself fails (ENOTTY on a fully torn-down master, or the
        pty briefly has no foreground group at all mid-exec), reads as "not
        the shell" — the safer default: refuse the write rather than risk
        typing into the wrong program."""
        if not self.alive:
            return False
        try:
            fg_pgid = os.tcgetpgrp(self.master_fd)
            shell_pgid = os.getpgid(self.proc.pid)
        except OSError:
            return False
        return fg_pgid == shell_pgid

    def wait_shell_foreground(self, timeout: float) -> bool:
        """Poll `shell_is_foreground()` for up to `timeout` seconds, returning
        as soon as it's true.

        A brand-new session's `tcgetpgrp` reads back `0` (no exception) until
        the child's own `setsid()` (`_pty_exec_helper.py`) lands — a real
        race against this method's own caller, not just a test artifact — and
        an interactive login shell's rc files (nvm, a git-aware prompt) can
        briefly run a foreground job of their own right after that. Used by
        `POST /api/terminal/{sid}/input` (routers/terminal.py) as a grace
        period before it 409s, so `fused.terminal.run(cmd)` into a
        freshly created session doesn't spuriously lose the race.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.shell_is_foreground():
                return True
            if not self.alive or time.monotonic() >= deadline:
                return self.shell_is_foreground()
            time.sleep(0.05)

    @property
    def cwd(self) -> str:
        """The live cwd once the shell has reported one (OSC 7), else the
        directory the session was spawned in."""
        return self.shell_state.cwd or self.profile.cwd

    def foreground_name(self) -> Optional[str]:
        """Name of the program holding the terminal, or None while the shell
        itself does (or the session is dead / mid-exec with no foreground
        group). Names the foreground process-group leader."""
        if not self.alive:
            return None
        try:
            fg_pgid = os.tcgetpgrp(self.master_fd)
            shell_pgid = os.getpgid(self.proc.pid)
        except OSError:
            return None
        if fg_pgid <= 0 or fg_pgid == shell_pgid:
            return None
        return _process_name(fg_pgid) or "unknown"

    def text(self, lines: int = DEFAULT_TEXT_LINES) -> str:
        with self._lock:
            data = bytes(self._scrollback)
            rows, cols = self.rows, self.cols
        return render_screen_text(data, rows, cols, lines)

    def snapshot(self) -> dict:
        """The metadata shared by the list and /text responses (camelCase,
        JSON-ready)."""
        st = self.shell_state
        return {
            "id": self.id, "alive": self.alive, "exitCode": self.exit_code,
            "shell": os.path.basename(self.profile.shell),
            "cwd": self.cwd,
            "foreground": self.foreground_name(),
            "lastCommand": st.last_command,
            "lastExit": st.last_exit,
            "lastActivity": self.last_activity,
        }

    def resize(self, rows: int, cols: int) -> None:
        if not self.alive:
            return
        _set_winsize(self.master_fd, rows, cols)
        size = valid_winsize(rows, cols)
        if size is not None:
            self.rows, self.cols = size

    def scrollback(self) -> bytes:
        with self._lock:
            return bytes(self._scrollback)

    def attach(self) -> tuple[bytes, bool, Optional[int], SimpleQueue]:
        """Snapshot scrollback, the alive/exit_code pair, and register a new
        subscriber queue — all under one lock acquisition.

        This exists because calling `scrollback()`, `alive`, and
        `subscribe()` as three separate calls (with `await`s between them)
        would leave a real gap: output the reader thread produces in that
        window would land after the scrollback snapshot but before the
        subscriber queue exists, reaching neither and being lost to that
        client entirely. Worse, if the child exits in that window, the
        `("exit", code)` broadcast could predate the subscription entirely,
        so the client would never learn the shell died. One lock hold closes
        both gaps."""
        q: SimpleQueue = SimpleQueue()
        with self._lock:
            snapshot = bytes(self._scrollback)
            alive = self.alive
            exit_code = self.exit_code
            self._subscribers.append(q)
        return snapshot, alive, exit_code, q

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
        exited on its own.

        The helper's `os.setsid()` makes the child its own process group
        leader, so `killpg(self.proc.pid, ...)` is normally correct — but
        there is a real race between Popen() returning (as soon as
        posix_spawn's exec syscall completes) and the child actually running
        that setsid(), during which the child is still a member of the
        SERVER's own process group. `killpg` on that pid before setsid() has
        landed would target zero processes (a stale group; caught below as
        ProcessLookupError) or, in the worst case if pid ever happened to
        collide with a live pgid, someone else's group — so this checks the
        child's ACTUAL current group with `os.getpgid` and only calls
        `killpg` once that equals its own pid; otherwise it signals the pid
        alone, which is always safe."""
        if not self.alive:
            # The reader thread has already called `self.proc.wait()` and
            # reaped the child by the time `alive` goes False — the pid is
            # therefore free for the OS to hand to an unrelated process.
            # Without this check, `shutdown_all()`/`registry.kill()` calling
            # `kill()` on every session (including long-dead ones) could
            # resolve a recycled pid's `os.getpgid()` as a live process and
            # SIGHUP/SIGKILL it (or its whole group, if it happens to be a
            # group leader) — a process this code has nothing to do with.
            return
        pid = self.proc.pid
        self._signal(pid, signal.SIGHUP)
        deadline = time.time() + _KILL_GRACE_S
        while time.time() < deadline and self.alive:
            time.sleep(0.05)
        if self.alive:
            self._signal(pid, signal.SIGKILL)

    @staticmethod
    def _signal(pid: int, sig: int) -> None:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return
        try:
            if pgid == pid:
                os.killpg(pgid, sig)
            else:
                # setsid() hasn't landed in the child yet — signal only the
                # pid, never the group (which right now is the SERVER's own).
                os.kill(pid, sig)
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

    def create(self, cwd: Optional[str] = None,
               rows: Optional[int] = None,
               cols: Optional[int] = None) -> PtySession:
        with self._lock:
            self._reap_dead_locked()
            live = sum(1 for s in self._sessions.values() if s.alive)
            if live >= MAX_SESSIONS:
                raise SessionLimitError(
                    f"terminal session cap ({MAX_SESSIONS}) reached")
            profile = resolve_profile(cwd=cwd)
            if profile is None:
                raise RuntimeError("terminal is not supported on this platform")
            profile = integrate(profile)
            sid = uuid.uuid4().hex
            session = PtySession(sid, profile, rows=rows, cols=cols)
            self._sessions[sid] = session
            return session

    def get(self, sid: str) -> Optional[PtySession]:
        return self._sessions.get(sid)

    def list(self) -> list[PtySession]:
        with self._lock:
            self._reap_dead_locked()
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
        `create`'s cap accounting and what `list()` reports.

        Called from both `create()` and `list()` (each under the same lock
        this acquires, via `_reap_dead_locked`) — a dead session otherwise
        lingered in the registry for the life of the process, kept
        appearing in `GET /api/terminal`, and let a client "verify" a cached
        id that actually belonged to an exited shell."""
        with self._lock:
            self._reap_dead_locked()

    def _reap_dead_locked(self) -> None:
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
