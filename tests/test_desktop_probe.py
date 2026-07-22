"""Token-verified desktop readiness probe (desktop_probe.py) and the server
side it checks against (/api/config's desktop_instance echo).

The contract: a launch publishes its instance id + a per-launch token into the
env, the server echoes them from /api/config, and the probe reports ready only
when the echo matches — so a decoy server holding the port cannot satisfy
startup. Pure stdlib + a real local uvicorn instance; no pywin32, so this runs
on every platform.
"""
import contextlib
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from fused_render import desktop_probe
from fused_render.server import create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _serve(app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def desktop_env(monkeypatch):
    token = "a" * 64
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_ID", desktop_probe.DESKTOP_INSTANCE_ID)
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN", token)
    return token


# ---- server-side /api/config echo (the contract owner) ---------------------


def test_config_echoes_token_when_header_matches(tmp_path, desktop_env):
    client = TestClient(create_app(start_dir=str(tmp_path)))
    body = client.get("/api/config", headers={"X-Fused-Desktop-Token": desktop_env}).json()
    assert body["desktop_instance"] == {
        "id": desktop_probe.DESKTOP_INSTANCE_ID,
        "token": desktop_env,
    }


def test_config_hides_token_when_header_absent_or_wrong(tmp_path, desktop_env):
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert client.get("/api/config").json()["desktop_instance"] == {
        "id": desktop_probe.DESKTOP_INSTANCE_ID
    }
    body = client.get("/api/config", headers={"X-Fused-Desktop-Token": "wrong"}).json()
    assert body["desktop_instance"] == {"id": desktop_probe.DESKTOP_INSTANCE_ID}


def test_config_has_no_desktop_instance_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_DESKTOP_INSTANCE_ID", raising=False)
    monkeypatch.delenv("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN", raising=False)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert "desktop_instance" not in client.get("/api/config").json()


# ---- the probe against a real local port -----------------------------------


def test_matching_server_true_for_our_token(tmp_path, desktop_env):
    with _serve(create_app(start_dir=str(tmp_path))) as port:
        assert desktop_probe.matching_server(port, desktop_env) is True


def test_matching_server_false_for_wrong_token(tmp_path, desktop_env):
    with _serve(create_app(start_dir=str(tmp_path))) as port:
        assert desktop_probe.matching_server(port, "b" * 64) is False


def test_decoy_server_without_desktop_env_is_not_matched(tmp_path, monkeypatch):
    # The whole point: a server that answers 200 but is not ours (no desktop
    # env set, so no token echo) must not satisfy the probe.
    monkeypatch.delenv("FUSED_RENDER_DESKTOP_INSTANCE_ID", raising=False)
    monkeypatch.delenv("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN", raising=False)
    with _serve(create_app(start_dir=str(tmp_path))) as port:
        assert desktop_probe.matching_server(port, "any-token") is False


def test_wait_until_ready_true_when_server_answers(tmp_path, desktop_env):
    with _serve(create_app(start_dir=str(tmp_path))) as port:
        assert desktop_probe.wait_until_ready(port, desktop_env, timeout_s=10) is True


def test_wait_until_ready_times_out_on_dead_port():
    assert (
        desktop_probe.wait_until_ready(_free_port(), "t", timeout_s=0.3, poll_interval=0.05)
        is False
    )


def test_wait_until_ready_aborts_when_on_poll_raises():
    def boom():
        raise RuntimeError("child process exited")

    with pytest.raises(RuntimeError, match="child process exited"):
        desktop_probe.wait_until_ready(_free_port(), "t", timeout_s=5, on_poll=boom)
