"""Single-instance election + Unix-socket IPC resilience.

Unix sockets exist on both Linux and macOS, so this backend's IPC is exercised
in this repo's macOS CI too (not marked Linux-only). The hostile-client cases
mirror tests/test_supervisor_instance.py's named-pipe cases, ported to the
socket transport: a truncated frame, an oversized declared length, a slow
client, and a stop-while-parked must each be survivable — one broken client
must never kill the accept loop, and stop must unblock cleanly.
"""
import errno
import queue
import shutil
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="Unix domain sockets required"
)

# instance.py imports fcntl (POSIX-only): importorskip keeps this module from
# ERRORing at collection on Windows, where fcntl is absent (sibling convention:
# test_supervisor_linux_tree.py guards the same way). AF_UNIX exists on macOS
# too, so the tests below still run there — only fcntl gates the import.
pytest.importorskip("fcntl")

from fused_render.supervisor import protocol
from fused_render.supervisor._linux import instance

_MAGIC = 0x3153_5246


@pytest.fixture
def runtime():
    # A SHORT dir: AF_UNIX socket paths have a ~104-char limit on macOS, and
    # pytest's tmp_path blows past it. /tmp keeps the socket path short on both
    # macOS (/private/tmp) and Linux.
    directory = tempfile.mkdtemp(prefix="frs", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _names(runtime: Path) -> "instance.InstanceNames":
    return instance.InstanceNames(
        lock=runtime / "supervisor.lock", socket=runtime / "supervisor.sock"
    )


@pytest.fixture
def served(runtime):
    names = _names(runtime)
    requests: "queue.Queue[instance.Request]" = queue.Queue()
    stop = threading.Event()
    logs: list[str] = []
    thread = threading.Thread(
        target=instance._serve_socket,
        args=(names, requests, stop, logs.append),
        daemon=True,
    )
    thread.start()
    # Wait for the listener to bind before any client connects.
    deadline = time.monotonic() + 5
    while not names.socket.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    yield names, requests, thread, logs
    stop.set()
    thread.join(timeout=5)


def _answer_next(requests, status=0):
    def answer():
        requests.get(timeout=5).response.put(status)

    threading.Thread(target=answer, daemon=True).start()


def _send(names, frame: bytes, timeout: float = 5.0) -> int | None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(names.socket))
        s.sendall(frame)
        data = b""
        while len(data) < 4:
            chunk = s.recv(4 - len(data))
            if not chunk:
                return None
            data += chunk
        return struct.unpack("<I", data)[0]


def test_valid_command_is_forwarded_and_acked(served):
    names, requests, thread, _logs = served
    _answer_next(requests, status=0)
    assert _send(names, protocol.encode(protocol.OpenHome())) == 0
    assert thread.is_alive()


def test_truncated_frame_does_not_kill_the_loop(served):
    names, requests, thread, _logs = served
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(names.socket))
        s.sendall(b"\x00\x01\x02")  # fewer than the 12-byte header
        s.shutdown(socket.SHUT_WR)
        # The server treats this as a broken client (status 1) or drops it.
        try:
            data = s.recv(4)
        except OSError:
            data = b""
    if data:
        assert struct.unpack("<I", data)[0] == 1
    _answer_next(requests, status=0)
    assert _send(names, protocol.encode(protocol.OpenHome())) == 0
    assert thread.is_alive()


def test_oversized_declared_length_is_rejected(served):
    names, requests, thread, _logs = served
    # Declares 1_000_000 UTF-16 units (>_MAX_PATH_UNITS) but sends no payload:
    # the reader must reject on the length, never try to read a gigabyte.
    header = struct.pack("<IHHI", _MAGIC, 1, 1, 1_000_000)
    assert _send(names, header) == 1
    _answer_next(requests, status=0)
    assert _send(names, protocol.encode(protocol.OpenHome())) == 0
    assert thread.is_alive()


def test_slow_client_is_bounded_and_does_not_stall_the_loop(served):
    names, requests, thread, _logs = served
    # Connect, send a partial header, then stall: the per-client read deadline
    # must fire and free the loop for the next client.
    slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    slow.connect(str(names.socket))
    slow.sendall(b"\x46")  # one byte of the magic, then nothing
    try:
        _answer_next(requests, status=0)
        assert _send(names, protocol.encode(protocol.OpenHome())) == 0
        assert thread.is_alive()
    finally:
        slow.close()


def test_stop_while_parked_unblocks_cleanly(runtime):
    names = _names(runtime)
    requests: "queue.Queue[instance.Request]" = queue.Queue()
    stop = threading.Event()
    thread = threading.Thread(
        target=instance._serve_socket, args=(names, requests, stop, None), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not names.socket.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    # No client ever connects; the loop is parked in select. stop must end it.
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_election_primary_then_secondary(runtime):
    names = _names(runtime)
    first = instance.acquire(names)
    second = instance.acquire(names)
    try:
        assert isinstance(first, instance.PrimaryInstance)
        assert isinstance(second, instance.SecondaryInstance)
    finally:
        first.release()


def test_send_raises_command_rejected_on_nonzero_status(runtime):
    names = _names(runtime)
    primary = instance.acquire(names)
    assert isinstance(primary, instance.PrimaryInstance)
    requests: "queue.Queue[instance.Request]" = queue.Queue()
    primary.serve(requests)
    deadline = time.monotonic() + 5
    while not names.socket.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        _answer_next(requests, status=1)
        secondary = instance.SecondaryInstance(names)
        with pytest.raises(instance.CommandRejected):
            secondary.send(protocol.Open("/tmp/nope"), timeout=5)
    finally:
        primary.stop_serving()
        primary.release()


def test_secondary_wait_for_exit_returns_when_primary_releases(runtime):
    names = _names(runtime)
    primary = instance.acquire(names)
    assert isinstance(primary, instance.PrimaryInstance)
    secondary = instance.SecondaryInstance(names)

    def release_soon():
        time.sleep(0.3)
        primary.release()

    threading.Thread(target=release_soon, daemon=True).start()
    secondary.wait_for_exit(timeout=5)  # returns once the flock is released


def test_serve_socket_bind_failure_logs_and_exits(runtime, monkeypatch):
    # A bind that never succeeds must fail loudly (log + return), never kill
    # the IPC thread silently. Force it by parking a directory where the socket
    # should bind, and speed up the retry/give-up loop.
    names = _names(runtime)
    names.socket.mkdir()
    monkeypatch.setattr(instance, "_BIND_GIVE_UP_AFTER_ATTEMPTS", 2)
    monkeypatch.setattr(instance, "_BIND_RETRY_START_S", 0.001)
    monkeypatch.setattr(instance, "_BIND_RETRY_CAP_S", 0.001)
    requests: "queue.Queue[instance.Request]" = queue.Queue()
    stop = threading.Event()
    logs: list[str] = []
    thread = threading.Thread(
        target=instance._serve_socket,
        args=(names, requests, stop, logs.append),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()  # gave up, did not hang or crash unobserved
    assert any("giving up" in message for message in logs)


def test_acquire_reraises_unexpected_flock_error(runtime, monkeypatch):
    # A non-contention errno (ENOLCK) is a real fault: acquire must re-raise so
    # __main__'s fatal path reports it, not silently demote to a secondary.
    names = _names(runtime)

    def boom(fd, op):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(instance.fcntl, "flock", boom)
    with pytest.raises(OSError):
        instance.acquire(names)


def test_acquire_treats_would_block_as_secondary(runtime, monkeypatch):
    # EWOULDBLOCK means a primary holds the lock → become a secondary.
    names = _names(runtime)

    def blocked(fd, op):
        raise BlockingIOError(errno.EWOULDBLOCK, "locked")

    monkeypatch.setattr(instance.fcntl, "flock", blocked)
    assert isinstance(instance.acquire(names), instance.SecondaryInstance)


def test_serve_thread_exits_after_shutdown_for_upgrade(served):
    # core._teardown's UPGRADE path skips _stop_pipe on the contract that the
    # serving thread self-stops once a ShutdownForUpgrade is answered. Assert
    # the backend honors it (mirrors _win32/_serve_pipe's should_stop).
    names, requests, thread, _logs = served
    _answer_next(requests, status=0)
    assert _send(names, protocol.encode(protocol.ShutdownForUpgrade())) == 0
    deadline = time.monotonic() + 5
    while thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not thread.is_alive()
