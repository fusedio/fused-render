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
from starlette.websockets import WebSocketDisconnect

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
    """Regression guard for finding 9 (code review, PR #1290): the route
    validated that `resize` is a 2-element list but not that the elements
    are numeric. `int("a")` raising ValueError used to propagate out of the
    handler (not caught by `except WebSocketDisconnect`), tearing down an
    otherwise healthy terminal over one bad control frame."""
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


def test_unknown_id_closes_with_1008(client, scratch_registry):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/terminal/does-not-exist/stream"):
            pass
    assert excinfo.value.code == 1008


def test_create_requires_x_fused_header(client, scratch_registry):
    resp = client.post("/api/terminal", json={})
    assert resp.status_code == 403
