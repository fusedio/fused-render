"""Single-instance election + Unix-socket IPC.

The Linux counterpart to `_win32/instance.py`, solved the Linux-native way:

  Election  — `flock(LOCK_EX|LOCK_NB)` on `supervisor.lock`. The kernel releases
              an flock on *any* death, including SIGKILL, so a crashed primary
              never wedges the next launch — the same "abandoned mutex" property
              the Windows named mutex has, without a heartbeat.
  IPC       — a Unix stream socket (`supervisor.sock`, mode 0600) in the same
              0700 runtime dir, carrying the *identical* `protocol.py` frames
              plus a 4-byte status reply.

Where the socket makes this simpler than the named-pipe idiom it takes the
simpler road: `select` lets one accept loop poll a stop flag between clients
with no NOWAIT gymnastics, no FIRST_PIPE_INSTANCE reconnect race, and no
self-poke to unblock. The invariants that DO carry over: a bounded per-client
read deadline (a slow/hung client cannot stall the loop), one broken client
never kills the accept loop, and `stop_serving()` unblocks cleanly.
"""
from __future__ import annotations

import errno
import fcntl
import os
import queue
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fused_render.supervisor import protocol
from fused_render.supervisor.paths import linux_runtime_dir

# 12-byte header (magic, version, opcode, n_units) + n_units UTF-16 code units.
# Mirrors protocol.py's own limits so an oversized declared length is rejected
# before a single payload byte is read.
_HEADER_LEN = 12
_MAX_PATH_UNITS = 32_767
_MAX_FRAME = _HEADER_LEN + _MAX_PATH_UNITS * 2

_CLIENT_READ_DEADLINE_S = 5.0  # DoS guard: a slow/hung client is dropped, not awaited
_SELECT_TICK_S = 0.25          # how often the accept loop re-checks the stop flag
_REQUEST_ANSWER_TIMEOUT_S = 20.0

# bind()/listen() retry (same resilience rule as _win32/_serve_pipe's
# CreateNamedPipe retry): a transient bind failure must not silently kill IPC.
_BIND_RETRY_START_S = 0.05
_BIND_RETRY_CAP_S = 2.0
_BIND_GIVE_UP_AFTER_ATTEMPTS = 10


@dataclass(frozen=True)
class InstanceNames:
    lock: Path
    socket: Path

    @classmethod
    def current_user(cls) -> "InstanceNames":
        runtime = linux_runtime_dir()
        return cls(lock=runtime / "supervisor.lock", socket=runtime / "supervisor.sock")


@dataclass
class Request:
    command: protocol.Command
    # Handler calls .put(status): 0 (ok) or 1 (rejected).
    response: "queue.Queue[int]"


class CommandRejected(OSError):
    """The primary received the command and answered non-zero (e.g. a forwarded
    Open failed) — a healthy primary IS running; only the specific command
    failed. Distinct from a connection/timeout failure, so callers must not
    report it as "the app could not start" (same contract as the Win32
    backend)."""


def acquire(names: InstanceNames) -> "PrimaryInstance | SecondaryInstance":
    _ensure_runtime_dir(names.lock.parent)
    fd = os.open(names.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        # Only "another holder has the lock" means a primary already exists →
        # become a secondary and forward. flock(2) reports that under LOCK_NB
        # as EWOULDBLOCK/EAGAIN (EACCES on some filesystems). Any other errno
        # (ENOLCK, EIO, ...) is a genuine fault: close the fd and re-raise so
        # __main__'s fatal path reports it, rather than silently demoting to a
        # secondary that then just times out forwarding. Mirrors _win32, which
        # branches only on ERROR_ALREADY_EXISTS.
        os.close(fd)
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return SecondaryInstance(names)
        raise
    return PrimaryInstance(fd, names)


class PrimaryInstance:
    def __init__(self, lock_fd: int, names: InstanceNames):
        self._lock_fd = lock_fd
        self.names = names
        self._stop = threading.Event()

    def serve(self, requests: "queue.Queue[Request]", log=None) -> threading.Thread:
        thread = threading.Thread(
            target=_serve_socket,
            args=(self.names, requests, self._stop, log),
            daemon=True,
        )
        thread.start()
        return thread

    def stop_serving(self) -> None:
        """Idempotent. Setting the flag is enough: the accept loop is parked in
        a `select` with a _SELECT_TICK_S timeout, never in a blocking accept, so
        it notices within one tick and exits — no self-connect poke needed."""
        self._stop.set()

    def release(self) -> None:
        """Drop the election lock and remove the socket file. The normal
        teardown path never calls this (the process just exits and the kernel
        releases the flock); it exists for the early ShutdownForUpgrade branch
        in core.run()."""
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._lock_fd)
        except OSError:
            pass
        try:
            os.unlink(self.names.socket)
        except OSError:
            pass


class SecondaryInstance:
    def __init__(self, names: InstanceNames):
        self.names = names

    def send(self, command: protocol.Command, timeout: float) -> None:
        frame = protocol.encode(command)
        deadline = time.monotonic() + timeout
        while True:
            try:
                status = self._round_trip(frame, deadline)
                if status == 0:
                    return
                if status == 1:
                    raise CommandRejected("supervisor rejected the command")
            except CommandRejected:
                raise
            except OSError:
                pass  # primary not up yet / mid-restart — retry until deadline
            if time.monotonic() >= deadline:
                raise TimeoutError("supervisor did not respond")
            time.sleep(0.05)

    def _round_trip(self, frame: bytes, deadline: float) -> int | None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(max(0.05, deadline - time.monotonic()))
            s.connect(str(self.names.socket))
            s.sendall(frame)
            data = _recv_exact(s, 4, deadline)
        return struct.unpack("<I", data)[0]

    def wait_for_exit(self, timeout: float) -> None:
        """Return once the primary has exited, detected by the election flock
        becoming acquirable (the kernel releases it on the primary's death,
        including SIGKILL). This is the analog of the Win32 backend waiting on
        the abandoned mutex."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._lock_free():
                return
            time.sleep(0.1)
        if self._lock_free():
            return
        raise TimeoutError("supervisor did not exit")

    def _lock_free(self) -> bool:
        try:
            fd = os.open(self.names.lock, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return True  # lock file gone entirely — primary is certainly gone
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)


def _serve_socket(
    names: InstanceNames,
    requests: "queue.Queue[Request]",
    stop: threading.Event,
    log=None,
) -> None:
    _ensure_runtime_dir(names.socket.parent)
    server = _bind_listen(names, stop, log)
    if server is None:
        return  # bind never succeeded; the giving-up reason is already logged.
    try:
        server.setblocking(False)
        while not stop.is_set():
            try:
                ready, _, _ = select.select([server], [], [], _SELECT_TICK_S)
            except OSError:
                break
            if not ready:
                continue
            try:
                conn, _ = server.accept()
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                continue
            # Handle each client on its own short-lived daemon thread so a slow
            # or hung client (bounded by _CLIENT_READ_DEADLINE_S) can never
            # stall the accept loop — the Linux-native answer to the Win32
            # backend's single-threaded NOWAIT poll. Localhost single-user IPC,
            # so unbounded per-connection threads are an acceptable simplicity.
            threading.Thread(
                target=_client_worker,
                args=(conn, requests, stop, log),
                daemon=True,
                name="fused-render-ipc-client",
            ).start()
    finally:
        server.close()
        try:
            os.unlink(names.socket)
        except OSError:
            pass


def _bind_listen(
    names: InstanceNames, stop: threading.Event, log
) -> "socket.socket | None":
    """Bind + listen on the IPC socket, retrying a transient bind failure with
    backoff before giving up loudly. Mirrors _win32/_serve_pipe's CreateNamedPipe
    retry: an unhandled OSError here would kill the IPC daemon thread silently,
    leaving the primary alive but undiscoverable and unable to answer a
    forwarded open or ShutdownForUpgrade. Returns a listening socket, or None
    once it gives up (a stop request during backoff also returns None)."""
    delay = _BIND_RETRY_START_S
    failures = 0
    while not stop.is_set():
        try:
            os.unlink(names.socket)  # clear a stale socket from a crashed primary
        except FileNotFoundError:
            pass
        except OSError as error:
            if log is not None:
                log(f"could not remove stale socket: {error}")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(names.socket))
            os.chmod(names.socket, 0o600)
            server.listen(16)
            return server
        except OSError as error:
            server.close()
            failures += 1
            if failures >= _BIND_GIVE_UP_AFTER_ATTEMPTS:
                if log is not None:
                    log(f"socket server giving up after {failures} bind failures: {error}")
                return None
            if log is not None and failures == 1:
                log(f"socket bind failed, retrying: {error}")
            if stop.wait(delay):
                return None
            delay = min(delay * 2, _BIND_RETRY_CAP_S)
    return None


def _client_worker(
    conn: socket.socket, requests: "queue.Queue[Request]", stop: threading.Event, log
) -> None:
    with conn:
        try:
            _handle_client(conn, requests, stop)
        except Exception as error:  # noqa: BLE001 - one broken client must not kill IPC
            # No caller to re-raise to (daemon thread); swallow so a single
            # malformed client cannot take down single-instance IPC.
            if log is not None:
                log(f"socket client handling failed: {error}")


def _handle_client(
    conn: socket.socket, requests: "queue.Queue[Request]", stop: threading.Event
) -> None:
    deadline = time.monotonic() + _CLIENT_READ_DEADLINE_S
    status = 1
    command = None
    try:
        header = _recv_exact(conn, _HEADER_LEN, deadline)
        _magic, _version, _opcode, n_units = struct.unpack("<IHHI", header)
        if n_units <= _MAX_PATH_UNITS:
            # Oversized declared length is left as command=None (rejected with
            # status 1) — never read the declared payload, so a client cannot
            # make the server allocate a gigabyte.
            payload = _recv_exact(conn, n_units * 2, deadline) if n_units else b""
            command = protocol.decode(header + payload)
    except (OSError, protocol.ProtocolError, struct.error):
        command = None

    if command is not None:
        response: "queue.Queue[int]" = queue.Queue(maxsize=1)
        requests.put(Request(command, response))
        try:
            status = response.get(timeout=_REQUEST_ANSWER_TIMEOUT_S)
        except queue.Empty:
            status = 1
    try:
        conn.sendall(struct.pack("<I", status))
    except OSError:
        pass
    # Self-stop the accept loop after answering a ShutdownForUpgrade, matching
    # _win32/_serve_pipe. core._teardown's UPGRADE path skips _stop_pipe on the
    # contract that the serving thread stops itself once the upgrade is
    # answered; the select loop notices the flag within one _SELECT_TICK_S.
    if isinstance(command, protocol.ShutdownForUpgrade):
        stop.set()


def _recv_exact(conn: socket.socket, n: int, deadline: float) -> bytes:
    """Read exactly `n` bytes or raise. Bounded by the shared deadline so a slow
    or hung client cannot stall the caller past _CLIENT_READ_DEADLINE_S."""
    if n == 0:
        return b""
    buffer = bytearray()
    while len(buffer) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("client read deadline exceeded")
        conn.settimeout(remaining)
        chunk = conn.recv(n - len(buffer))
        if not chunk:
            raise ConnectionError("client closed before sending a full frame")
        buffer.extend(chunk)
    return bytes(buffer)


def _ensure_runtime_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
