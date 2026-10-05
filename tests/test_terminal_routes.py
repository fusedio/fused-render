"""Coverage for /api/terminal (fused_render/server/routers/terminal.py):
session create/list/delete plus the WS byte stream.

Follows tests/test_server_fs_events.py for TestClient.websocket_connect.
Sessions run a bare `/bin/sh` via a monkeypatched `pty_session.resolve_profile`
(see tests/test_pty_session.py's `_profile` helper) rather than the host's
real login shell, for determinism.

Every test uses its own scratch `PtySessionRegistry`
(`monkeypatch.setattr(pty_session, "REGISTRY", ...)`) and tears it down with
`shutdown_all()`, which kills every child, joins every reader thread, and
closes every master fd — nothing is left running or holding an fd open past
the test.
"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import pty_session
from fused_render.server import create_app
from fused_render.terminal_profiles import TerminalProfile

pytestmark = pytest.mark.skipif(os.name == "nt", reason="pty is unix-only")

_HEADERS = {"X-Fused": "1"}


@pytest.fixture
def scratch_registry(monkeypatch, tmp_path):
    reg = pty_session.PtySessionRegistry()
    monkeypatch.setattr(pty_session, "REGISTRY", reg)
    monkeypatch.setattr(
        pty_session, "resolve_profile",
        lambda cwd=None: TerminalProfile(
            shell="/bin/sh", argv=["/bin/sh"], env=dict(os.environ),
            cwd=str(tmp_path)))
    yield reg
    reg.shutdown_all()


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _read_until(ws, needle: bytes, tries: int = 200) -> bytes:
    seen = b""
    for _ in range(tries):
        seen += ws.receive_bytes()
        if needle in seen:
            return seen
    raise AssertionError(f"{needle!r} not seen in {seen!r}")


def test_create_attach_type_and_receive_output(client, scratch_registry):
    resp = client.post("/api/terminal", json={}, headers=_HEADERS)
    assert resp.status_code == 200
    sid = resp.json()["id"]

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        ws.receive_bytes()  # initial (possibly empty) scrollback replay
        ws.send_bytes(b"echo hi\n")
        _read_until(ws, b"hi")


def test_list_reports_shell_name_and_cwd(client, scratch_registry, tmp_path):
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]
    rows = client.get("/api/terminal").json()["sessions"]
    row = next(r for r in rows if r["id"] == sid)
    assert row["shell"] == "sh"
    assert row["cwd"] == str(tmp_path)
    assert row["alive"] is True


def test_detach_and_reattach_replays_scrollback(client, scratch_registry):
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        ws.receive_bytes()
        ws.send_bytes(b"echo marker123\n")
        _read_until(ws, b"marker123")

    # Give the reader thread a beat to land the last bytes in the ring before
    # reattaching (the write above already blocked on `hi`... wait, no —
    # avoid flakiness from the tail landing after the socket already closed).
    time.sleep(0.1)

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        replay = ws.receive_bytes()
        assert b"marker123" in replay


def test_resize_control_frame_reaches_the_session(client, scratch_registry):
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        ws.receive_bytes()
        ws.send_text(json.dumps({"resize": [40, 120]}))
        ws.send_bytes(b"stty size\n")
        _read_until(ws, b"40 120")


def test_malformed_resize_values_do_not_kill_the_session(client, scratch_registry):
    """The route validates that `resize` is a 2-element list but the
    elements themselves can still be non-numeric. `int("a")` raising
    ValueError would propagate out of the handler (not caught by `except
    WebSocketDisconnect`), tearing down an otherwise healthy terminal over
    one bad control frame — the `except (TypeError, ValueError)` in
    routers/terminal.py guards against exactly that."""
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        ws.receive_bytes()
        ws.send_text(json.dumps({"resize": ["a", "b"]}))
        # The session must still be usable afterward — this would hang/raise
        # if the handler had already torn the socket down.
        ws.send_bytes(b"echo still-alive\n")
        _read_until(ws, b"still-alive")


def test_delete_kills_the_session(client, scratch_registry):
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]

    resp = client.delete(f"/api/terminal/{sid}", headers=_HEADERS)
    assert resp.status_code == 200

    deadline = time.time() + 5
    session = scratch_registry.get(sid)
    while time.time() < deadline and session.alive:
        time.sleep(0.02)
    assert not session.alive


def test_second_attach_shares_the_same_shell(client, scratch_registry):
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        ws.receive_bytes()
        ws.send_bytes(b"echo shared999\n")
        _read_until(ws, b"shared999")

    time.sleep(0.1)

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        replay = ws.receive_bytes()
        assert b"shared999" in replay


def test_unknown_id_gets_an_exit_frame_instead_of_a_bare_reject(client, scratch_registry):
    """An id the registry doesn't know (never created, or reaped by a
    server restart's `shutdown_all`) accepts the handshake, sends the same
    `{"exit": null}` frame a normally-dying session sends, then closes — so
    the client's existing exit path (drop the cached id, mint a fresh shell
    on next open) fires instead of a reconnect loop that treats every close
    as a blip and retries forever."""
    with client.websocket_connect("/api/terminal/does-not-exist/stream") as ws:
        msg = json.loads(ws.receive_text())
        assert msg == {"exit": None}


def test_create_requires_x_fused_header(client, scratch_registry):
    resp = client.post("/api/terminal", json={})
    assert resp.status_code == 403


def test_input_route_writes_into_the_pty_without_a_stream_socket(client, scratch_registry):
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]
    # No pre-poll here: the route itself waits out the child's `setsid()`
    # race (see terminal.py's api_terminal_input) before it would 409.
    resp = client.post(f"/api/terminal/{sid}/input", json={"data": "echo inputted\n"},
                        headers=_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        replay = ws.receive_bytes()
        # Either the echoed command already landed in scrollback before this
        # attach, or it lands on the stream shortly after.
        if b"inputted" not in replay:
            _read_until(ws, b"inputted")


def test_input_route_requires_x_fused_header(client, scratch_registry):
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]
    resp = client.post(f"/api/terminal/{sid}/input", json={"data": "x"})
    assert resp.status_code == 403


def test_input_route_rejects_non_string_data(client, scratch_registry):
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]
    resp = client.post(f"/api/terminal/{sid}/input", json={"data": 42}, headers=_HEADERS)
    assert resp.status_code == 400


def test_input_route_404s_for_unknown_session(client, scratch_registry):
    resp = client.post("/api/terminal/does-not-exist/input", json={"data": "x"},
                        headers=_HEADERS)
    assert resp.status_code == 404


def test_input_route_409s_while_a_child_holds_the_foreground(client, scratch_registry):
    """An "open in terminal / run a command" request against an existing
    session (TerminalDrawer.tsx) must not type into whatever program the
    shell is currently running — `sleep 30` becomes the pty's foreground
    process group the moment it starts, and the route refuses the write
    instead of sending `cd ... && cmd\\r` into it."""
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]
    session = scratch_registry.get(sid)
    # A fresh pty reads as "not the shell" until setsid() lands; wait for it first.
    assert session.wait_shell_foreground(timeout=5)
    session.write(b"sleep 30\n")

    deadline = time.time() + 5
    while time.time() < deadline and session.shell_is_foreground():
        time.sleep(0.02)
    assert not session.shell_is_foreground()

    resp = client.post(f"/api/terminal/{sid}/input", json={"data": "echo should-not-run\n"},
                        headers=_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["error"] == "terminal is busy"


def test_non_dict_control_frame_does_not_kill_the_socket(client, scratch_registry):
    """A syntactically valid JSON frame that isn't an object (`5`, `[1, 2]`)
    has no `.get` — the route must ignore it rather than raise
    AttributeError out of the receive loop, which `except
    WebSocketDisconnect` does not catch and would otherwise kill the whole
    socket over one stray frame."""
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        ws.receive_bytes()
        ws.send_text(json.dumps(5))
        ws.send_text(json.dumps([1, 2]))
        ws.send_bytes(b"echo still-alive\n")
        _read_until(ws, b"still-alive")


def test_out_of_range_resize_does_not_kill_the_socket(client, scratch_registry):
    """`struct.pack`'s "HHHH" format (PtySession.resize) only accepts
    unsigned 16-bit ints; the route clamps/ignores anything outside 1..65535
    instead of letting `struct.error` propagate out of the receive loop."""
    sid = client.post("/api/terminal", json={}, headers=_HEADERS).json()["id"]

    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        ws.receive_bytes()
        ws.send_text(json.dumps({"resize": [0, 100000]}))
        ws.send_bytes(b"echo still-alive\n")
        _read_until(ws, b"still-alive")
