"""The unix-socket protocol between the Linux supervisor / server and the
native-window host (window_host_ipc.py). Pure stdlib sockets, so it runs on
every OS that has AF_UNIX (macOS included) — no GTK involved."""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from fused_render import window_host_ipc as ipc

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="Unix domain sockets required"
)


@pytest.fixture
def sock_path():
    # AF_UNIX paths are capped (~104 bytes on macOS); pytest's tmp_path is too long.
    d = tempfile.mkdtemp(prefix="fr")
    try:
        yield os.path.join(d, "h.sock")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def server(sock_path):
    stop = threading.Event()
    seen = []

    def handler(cmd):
        seen.append(cmd)
        if cmd.get("cmd") == "boom":
            raise RuntimeError("handler exploded")
        return {"ok": True, "echo": cmd.get("cmd")}

    thread = ipc.serve(sock_path, handler, stop)
    deadline = time.monotonic() + 3
    while not os.path.exists(sock_path) and time.monotonic() < deadline:
        time.sleep(0.01)
    yield seen
    stop.set()
    thread.join(timeout=3)


def test_socket_path_lives_in_the_runtime_dir():
    assert ipc.socket_path(Path("/run/user/1000/fused-render")) == \
        Path("/run/user/1000/fused-render/window-host.sock")


def test_round_trip(sock_path, server):
    reply = ipc.request(sock_path, {"cmd": "open", "url": "http://127.0.0.1:1/"})
    assert reply == {"ok": True, "echo": "open"}
    assert server == [{"cmd": "open", "url": "http://127.0.0.1:1/"}]


def test_socket_is_owner_only(sock_path, server):
    assert (os.stat(sock_path).st_mode & 0o777) == 0o600


def test_ping(sock_path, server):
    assert ipc.ping(sock_path) is True


def test_handler_exception_becomes_a_not_ok_reply(sock_path, server):
    reply = ipc.request(sock_path, {"cmd": "boom"})
    assert reply["ok"] is False and "exploded" in reply["reason"]
    # ...and the accept loop survived it.
    assert ipc.ping(sock_path) is True


def test_garbage_client_does_not_kill_the_loop(sock_path, server):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(sock_path)
        s.sendall(b"this is not json\n")
        reply = json.loads(s.makefile().readline())
    assert reply["ok"] is False
    assert ipc.ping(sock_path) is True


def test_oversized_request_is_rejected(sock_path, server):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(3)
        s.connect(sock_path)
        try:
            s.sendall(b"x" * (ipc.MAX_LINE + 10))
            data = s.recv(4096)
        except OSError:
            data = b""
    assert data == b"" or b'"ok": false' in data
    assert ipc.ping(sock_path) is True


def test_missing_socket_is_host_unavailable(sock_path):
    with pytest.raises(ipc.HostUnavailable):
        ipc.request(sock_path, {"cmd": "ping"})
    assert ipc.ping(sock_path) is False


def test_stale_socket_file_is_host_unavailable(sock_path):
    # A crashed host leaves its socket file behind; connect() must read as
    # "unavailable", never raise something the caller does not catch.
    s = socket.socket(socket.AF_UNIX)
    s.bind(sock_path)
    s.close()
    with pytest.raises(ipc.HostUnavailable):
        ipc.request(sock_path, {"cmd": "ping"})


def test_silent_host_times_out_as_unavailable(sock_path):
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(sock_path)
    srv.listen(1)
    try:
        with pytest.raises(ipc.HostUnavailable):
            ipc.request(sock_path, {"cmd": "ping"}, timeout=0.2)
    finally:
        srv.close()


def test_a_slow_request_does_not_block_a_concurrent_ping(sock_path):
    # Preferences treats a 0.3s ping as "is the host up"; a one-connection-at-
    # a-time accept loop would starve that ping while an `open` or
    # `set_enabled` is still in flight (e.g. waiting on the GTK main thread),
    # making the Native windows section flicker away for no reason.
    stop = threading.Event()
    gate = threading.Event()

    def handler(cmd):
        if cmd.get("cmd") == "slow":
            gate.wait(3)
        return {"ok": True}

    thread = ipc.serve(sock_path, handler, stop)
    deadline = time.monotonic() + 3
    while not os.path.exists(sock_path) and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        result = {}

        def slow_client():
            result["reply"] = ipc.request(sock_path, {"cmd": "slow"}, timeout=3)

        slow = threading.Thread(target=slow_client)
        slow.start()
        time.sleep(0.2)  # let the slow request be accepted and start blocking
        assert ipc.ping(sock_path, timeout=0.5) is True
        gate.set()
        slow.join(timeout=3)
        assert result["reply"] == {"ok": True}
    finally:
        stop.set()
        thread.join(timeout=3)


def test_serve_replaces_a_stale_socket_file(sock_path):
    s = socket.socket(socket.AF_UNIX)
    s.bind(sock_path)
    s.close()
    stop = threading.Event()
    thread = ipc.serve(sock_path, lambda c: {"ok": True}, stop)
    try:
        deadline = time.monotonic() + 3
        while not ipc.ping(sock_path) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ipc.ping(sock_path)
    finally:
        stop.set()
        thread.join(timeout=3)
