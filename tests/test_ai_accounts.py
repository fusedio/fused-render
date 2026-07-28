"""Tests for fused_render/ai_accounts.py: /api/ai/accounts list/connect/
disconnect against the bundled AI proxy.

ai_proxy.py's thin wrappers (start_login/submit_login_code/poll_login_status/
cancel_login/list_credentials/delete_credential/status) are mocked directly —
no test here spawns a real cli-proxy-api or talks to a real management API,
mirroring test_server_ai.py's "avoid the real proxy" discipline.

The callback listener IS exercised for real: an actual HTTP GET is fired at
the bound port to prove capture + release. The fixed provider ports
(54545/1455) are patched to ephemeral ones for every test so nothing here can
collide with a genuine login in progress on the machine running them.
"""
import socket
import time
import urllib.request

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fused_render import ai_accounts

FUSED = {"X-Fused": "1"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    # Give every test its own ephemeral callback ports — never the real fixed
    # 54545/1455, which could collide with an actual login in progress on the
    # machine running these tests.
    monkeypatch.setitem(ai_accounts._CALLBACK, "claude",
                         {**ai_accounts._CALLBACK["claude"], "port": _free_port()})
    monkeypatch.setitem(ai_accounts._CALLBACK, "codex",
                         {**ai_accounts._CALLBACK["codex"], "port": _free_port()})
    # Real timeout is 5 minutes; tests that exercise the timeout path can't
    # wait that long.
    monkeypatch.setattr(ai_accounts, "_CALLBACK_TIMEOUT_S", 2.0)
    yield
    # A listener thread left running by a failed test must never leak into
    # the next one (it would hold its ephemeral port and keep polling).
    with ai_accounts._LOCK:
        entry, ai_accounts._active = ai_accounts._active, None
    if entry is not None:
        entry.cancel_event.set()
        try:
            entry.http_server.server_close()
        except OSError:
            pass


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ai_accounts.router)
    return TestClient(app)


def _wait_for(fn, deadline: float = 5.0):
    end = time.monotonic() + deadline
    while True:
        value = fn()
        if value:
            return value
        assert time.monotonic() < end, "timed out waiting"
        time.sleep(0.02)


# -- provider / name validation --------------------------------------------------


def test_connect_rejects_unknown_provider(client):
    resp = client.post("/api/ai/accounts/connect", json={"provider": "gemini"}, headers=FUSED)
    assert resp.status_code == 400
    assert "provider" in resp.json()["error"]


def test_connect_rejects_missing_provider(client):
    resp = client.post("/api/ai/accounts/connect", json={}, headers=FUSED)
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", [
    "..",
    "a/b",
    "a\\b",
    "claude-user..json",   # ".." as a substring, not a whole path segment
    "../../etc/passwd",
])
def test_valid_credential_name_rejects_traversal(bad):
    assert ai_accounts._valid_credential_name(bad) is False


@pytest.mark.parametrize("ok", ["claude-user@example.com.json", "codex-a@b.json"])
def test_valid_credential_name_accepts_normal_names(ok):
    assert ai_accounts._valid_credential_name(ok) is True


def test_delete_rejects_bad_name_at_the_route(client, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_accounts.ai_proxy, "delete_credential",
                         lambda name: calls.append(name))
    resp = client.request(
        "DELETE", "/api/ai/accounts/claude-user..json", headers=FUSED)
    assert resp.status_code == 400
    assert calls == []  # never reached the proxy with an unvalidated name


# -- X-Fused guard on mutations ---------------------------------------------------


def test_mutations_require_x_fused_header(client):
    assert client.post("/api/ai/accounts/connect", json={"provider": "claude"}).status_code == 403
    assert client.post("/api/ai/accounts/connect/cancel").status_code == 403
    assert client.request("DELETE", "/api/ai/accounts/claude-a@b.json").status_code == 403


def test_get_routes_do_not_require_x_fused_header(client, monkeypatch):
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": False})
    assert client.get("/api/ai/accounts").status_code == 200
    assert client.get("/api/ai/accounts/connect/status").status_code == 200


# -- listing -----------------------------------------------------------------------


def test_listing_is_cheap_when_not_running(client, monkeypatch):
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": False})

    def _boom():
        raise AssertionError("list_credentials must not be called when not running")

    monkeypatch.setattr(ai_accounts.ai_proxy, "list_credentials", _boom)
    resp = client.get("/api/ai/accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"supervised": True, "running": False, "accounts": [], "login": None}


def test_listing_filters_providers_and_omits_token_material(client, monkeypatch):
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": True})
    files = [
        {
            "name": "claude-a@b.com.json", "provider": "claude", "email": "a@b.com",
            "label": "a@b.com", "disabled": False,
            "id_token": {"sub": "should-never-appear"},
            "path": "/secret/state/dir/auths/claude-a@b.com.json",
            "access_token": "super-secret",
        },
        {
            "name": "codex-c@d.com.json", "provider": "codex", "email": "c@d.com",
            "label": "c@d.com", "disabled": True,
        },
        {"name": "gemini-e@f.com.json", "provider": "gemini", "email": "e@f.com"},
    ]
    monkeypatch.setattr(ai_accounts.ai_proxy, "list_credentials", lambda: files)
    resp = client.get("/api/ai/accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["supervised"] is True
    assert body["running"] is True
    assert body["accounts"] == [
        {"provider": "claude", "email": "a@b.com", "label": "a@b.com",
         "disabled": False, "name": "claude-a@b.com.json"},
        {"provider": "codex", "email": "c@d.com", "label": "c@d.com",
         "disabled": True, "name": "codex-c@d.com.json"},
    ]
    # No token material or on-disk paths anywhere in the response.
    dumped = str(body)
    assert "id_token" not in dumped
    assert "access_token" not in dumped
    assert "secret/state/dir" not in dumped


def test_listing_degrades_gracefully_on_a_listing_failure(client, monkeypatch):
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": True})

    def _boom():
        raise RuntimeError("this proxy build does not support account management")

    monkeypatch.setattr(ai_accounts.ai_proxy, "list_credentials", _boom)
    resp = client.get("/api/ai/accounts")
    assert resp.status_code == 200  # never a 500 over a listing hiccup
    assert resp.json()["accounts"] == []


# -- connect: single-flight ---------------------------------------------------------


def test_connect_is_single_flight(client, monkeypatch):
    monkeypatch.setattr(
        ai_accounts.ai_proxy, "start_login",
        lambda provider: {"state": "st-1", "url": "https://auth.example/1", "status": "ok"})
    first = client.post("/api/ai/accounts/connect", json={"provider": "claude"}, headers=FUSED)
    assert first.status_code == 200, first.text
    assert first.json() == {"authorize_url": "https://auth.example/1", "state": "st-1"}

    second = client.post("/api/ai/accounts/connect", json={"provider": "claude"}, headers=FUSED)
    assert second.status_code == 409
    assert "already in progress" in second.json()["error"]

    # Even a DIFFERENT provider is refused — only one login in flight overall
    # (the doc: ports are fixed, so concurrency is structural, not queued).
    third = client.post("/api/ai/accounts/connect", json={"provider": "codex"}, headers=FUSED)
    assert third.status_code == 409


def test_connect_maps_ui_provider_to_proxy_auth_provider(client, monkeypatch):
    seen = []
    monkeypatch.setattr(
        ai_accounts.ai_proxy, "start_login",
        lambda provider: seen.append(provider) or {"state": "s", "url": "https://x"})
    client.post("/api/ai/accounts/connect", json={"provider": "claude"}, headers=FUSED)
    assert seen == ["anthropic"]


def test_connect_start_login_failure_frees_the_port(client, monkeypatch):
    def _boom(provider):
        raise RuntimeError("management surface unreachable")

    monkeypatch.setattr(ai_accounts.ai_proxy, "start_login", _boom)
    resp = client.post("/api/ai/accounts/connect", json={"provider": "claude"}, headers=FUSED)
    assert resp.status_code == 502
    assert ai_accounts._active is None
    # The port must be free again — a fresh bind on it must succeed.
    port = ai_accounts._CALLBACK["claude"]["port"]
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


# -- cancel ---------------------------------------------------------------------


def test_cancel_is_a_safe_noop_when_idle(client):
    resp = client.post("/api/ai/accounts/connect/cancel", headers=FUSED)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "canceled": False}


def test_cancel_tears_down_an_in_flight_login(client, monkeypatch):
    monkeypatch.setattr(
        ai_accounts.ai_proxy, "start_login",
        lambda provider: {"state": "st-1", "url": "https://auth.example/1"})
    canceled = []
    monkeypatch.setattr(ai_accounts.ai_proxy, "cancel_login", lambda state: canceled.append(state))
    assert client.post(
        "/api/ai/accounts/connect", json={"provider": "claude"}, headers=FUSED
    ).status_code == 200

    resp = client.post("/api/ai/accounts/connect/cancel", headers=FUSED)
    assert resp.json() == {"ok": True, "canceled": True}
    assert canceled == ["st-1"]
    assert ai_accounts._active is None

    # A new connect is accepted immediately — cancel actually freed the slot.
    port = ai_accounts._CALLBACK["claude"]["port"]
    _wait_for(lambda: _can_bind(port))
    resp2 = client.post("/api/ai/accounts/connect", json={"provider": "claude"}, headers=FUSED)
    assert resp2.status_code == 200, resp2.text


def _can_bind(port: int) -> bool:
    # Match HTTPServer's own allow_reuse_address=1 so a lingering TIME_WAIT
    # connection socket from the just-finished one-shot request doesn't make
    # a released port look "still busy" to this probe.
    try:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


# -- the callback listener --------------------------------------------------------


def test_callback_listener_captures_code_and_releases_port(client, monkeypatch):
    monkeypatch.setattr(
        ai_accounts.ai_proxy, "start_login",
        lambda provider: {"state": "abc123", "url": "https://auth.example/abc"})
    submitted = []
    monkeypatch.setattr(
        ai_accounts.ai_proxy, "submit_login_code",
        lambda provider, state, code: submitted.append((provider, state, code)))

    resp = client.post("/api/ai/accounts/connect", json={"provider": "claude"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    port = ai_accounts._CALLBACK["claude"]["port"]

    # Simulate the browser landing on our loopback listener after the user
    # approves in the provider's consent screen.
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/callback?code=CODE-1&state=abc123", timeout=5
    ) as r:
        assert r.status == 200
        assert b"FusedRender" in r.read()

    _wait_for(lambda: submitted)
    assert submitted == [("anthropic", "abc123", "CODE-1")]
    assert ai_accounts._active.phase == "exchanging"

    # The port must be released promptly once the one-shot request lands —
    # not held for the rest of the (multi-minute) callback timeout.
    _wait_for(lambda: _can_bind(port))


def test_callback_listener_rejects_state_mismatch(client, monkeypatch):
    monkeypatch.setattr(
        ai_accounts.ai_proxy, "start_login",
        lambda provider: {"state": "expected-state", "url": "https://auth.example/x"})
    submitted = []
    monkeypatch.setattr(
        ai_accounts.ai_proxy, "submit_login_code",
        lambda provider, state, code: submitted.append((provider, state, code)))

    client.post("/api/ai/accounts/connect", json={"provider": "claude"}, headers=FUSED)
    port = ai_accounts._CALLBACK["claude"]["port"]
    urllib.request.urlopen(
        f"http://127.0.0.1:{port}/callback?code=CODE-1&state=WRONG", timeout=5).close()

    entry = _wait_for(lambda: ai_accounts._active if ai_accounts._active.phase == "failed" else None)
    assert "did not match" in entry.detail
    assert submitted == []  # never handed a mismatched code to the proxy


def test_callback_listener_times_out_when_browser_never_arrives(client, monkeypatch):
    monkeypatch.setattr(
        ai_accounts.ai_proxy, "start_login",
        lambda provider: {"state": "st-1", "url": "https://auth.example/x"})
    client.post("/api/ai/accounts/connect", json={"provider": "claude"}, headers=FUSED)
    port = ai_accounts._CALLBACK["claude"]["port"]

    entry = _wait_for(
        lambda: ai_accounts._active if ai_accounts._active.phase == "failed" else None,
        deadline=5.0)
    assert "timed out" in entry.detail
    _wait_for(lambda: _can_bind(port))


# -- status polling -----------------------------------------------------------------


class _DummyServer:
    """Stands in for the real HTTPServer in status-only tests, which install
    an _ActiveConnect directly and never run the listener thread. Shaped
    enough to survive the autouse teardown's cancel/close, so those tests
    don't need a real bound socket just to satisfy cleanup."""

    timeout = 1.0
    captured = None

    def handle_request(self) -> None:
        raise AssertionError("status-only tests must never run the listener")

    def server_close(self) -> None:
        pass


def _install_active(state="st-1", phase="exchanging"):
    entry = ai_accounts._ActiveConnect(
        provider="claude", auth_provider="anthropic", state=state,
        http_server=_DummyServer(), phase=phase,
    )
    ai_accounts._active = entry
    return entry


def test_status_idle_when_nothing_in_flight(client):
    assert client.get("/api/ai/accounts/connect/status").json() == {
        "state": "idle", "detail": None,
    }


def test_status_reports_waiting_browser_without_polling_the_proxy(client, monkeypatch):
    _install_active(phase="waiting_browser")

    def _boom(state):
        raise AssertionError("must not poll the proxy while still waiting on the browser")

    monkeypatch.setattr(ai_accounts.ai_proxy, "poll_login_status", _boom)
    assert client.get("/api/ai/accounts/connect/status").json() == {
        "state": "waiting_browser", "detail": None,
    }


def test_status_maps_wait_ok_and_error(client, monkeypatch):
    _install_active(phase="exchanging")
    monkeypatch.setattr(ai_accounts.ai_proxy, "poll_login_status", lambda state: {"status": "wait"})
    assert client.get("/api/ai/accounts/connect/status").json() == {
        "state": "exchanging", "detail": None,
    }
    assert ai_accounts._active.phase == "exchanging"

    monkeypatch.setattr(ai_accounts.ai_proxy, "poll_login_status", lambda state: {"status": "ok"})
    assert client.get("/api/ai/accounts/connect/status").json() == {
        "state": "done", "detail": None,
    }
    assert ai_accounts._active.phase == "done"

    _install_active(phase="exchanging")
    monkeypatch.setattr(
        ai_accounts.ai_proxy, "poll_login_status",
        lambda state: {"status": "error", "error": "Failed to exchange authorization code"})
    resp = client.get("/api/ai/accounts/connect/status").json()
    assert resp["state"] == "failed"
    assert "Failed to exchange" in resp["detail"]
    assert ai_accounts._active.phase == "failed"


def test_status_poll_failure_maps_to_failed(client, monkeypatch):
    _install_active(phase="exchanging")

    def _boom(state):
        raise RuntimeError("this proxy build does not support account management")

    monkeypatch.setattr(ai_accounts.ai_proxy, "poll_login_status", _boom)
    resp = client.get("/api/ai/accounts/connect/status").json()
    assert resp["state"] == "failed"
    assert "does not support account management" in resp["detail"]


def test_listing_includes_login_snapshot(client, monkeypatch):
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": False})
    _install_active(phase="exchanging", state="st-9")
    body = client.get("/api/ai/accounts").json()
    assert body["login"] == {"provider": "claude", "state": "exchanging", "detail": None}


# -- delete --------------------------------------------------------------------


def test_delete_calls_proxy_and_returns_ok(client, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_accounts.ai_proxy, "delete_credential", lambda name: calls.append(name))
    resp = client.request("DELETE", "/api/ai/accounts/claude-a@b.com.json", headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert calls == ["claude-a@b.com.json"]


def test_delete_surfaces_proxy_error(client, monkeypatch):
    def _boom(name):
        raise RuntimeError("invalid name")

    monkeypatch.setattr(ai_accounts.ai_proxy, "delete_credential", _boom)
    resp = client.request("DELETE", "/api/ai/accounts/claude-a@b.com.json", headers=FUSED)
    assert resp.status_code == 502
    assert "invalid name" in resp.json()["error"]
