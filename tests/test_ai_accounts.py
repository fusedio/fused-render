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
def _reset_state(monkeypatch, tmp_path):
    # Redirect FUSED_RENDER_HOME so the routing-strategy pref (read on every
    # GET, written by the new PUT route) never touches a developer's real
    # ~/.fused-render/prefs.json — same discipline as test_shell_prefs.py's
    # _client(). Must be set before anything in this module reads prefs.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
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
    monkeypatch.setattr(ai_accounts.ai_proxy, "list_api_keys",
                         lambda provider: _boom())
    resp = client.get("/api/ai/accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "supervised": True, "running": False, "accounts": [], "api_keys": [],
        "routing_strategy": "round-robin", "login": None,
    }


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
    monkeypatch.setattr(ai_accounts.ai_proxy, "list_api_keys", lambda provider: [])
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


def test_listing_masks_api_keys(client, monkeypatch):
    # The one non-negotiable rule for this surface: a full API key must
    # never appear anywhere in a response the client can read.
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": True})
    monkeypatch.setattr(ai_accounts.ai_proxy, "list_credentials", lambda: [])
    full_key = "sk-ant-super-secret-do-not-leak-1234"

    def _list_api_keys(provider):
        if provider == "claude":
            return [{"api-key": full_key, "base-url": "", "auth-index": "idx-1"}]
        return []

    monkeypatch.setattr(ai_accounts.ai_proxy, "list_api_keys", _list_api_keys)
    resp = client.get("/api/ai/accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_keys"] == [
        {"provider": "claude", "hint": "..." + full_key[-4:], "auth_index": "idx-1"},
    ]
    assert full_key not in str(body)


def test_listing_degrades_gracefully_on_a_listing_failure(client, monkeypatch):
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": True})

    def _boom():
        raise RuntimeError("this proxy build does not support account management")

    monkeypatch.setattr(ai_accounts.ai_proxy, "list_credentials", _boom)
    monkeypatch.setattr(ai_accounts.ai_proxy, "list_api_keys",
                         lambda provider: (_ for _ in ()).throw(
                             RuntimeError("this proxy build does not support account management")))
    resp = client.get("/api/ai/accounts")
    assert resp.status_code == 200  # never a 500 over a listing hiccup
    assert resp.json()["accounts"] == []
    assert resp.json()["api_keys"] == []


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


@pytest.mark.parametrize("settled", ["done", "failed"])
def test_listing_reports_no_login_once_an_attempt_settles(client, monkeypatch, settled):
    """A finished attempt must not read as in-flight.

    Regression guard for a bug that made the SECOND provider unconnectable.
    _active is deliberately kept after an attempt settles so /connect/status
    can still report its outcome, but the listing's `login` field is what the
    page uses to decide "is a login in progress" — and reporting a settled
    attempt there left every Connect button disabled forever, so connecting
    Claude locked the user out of ChatGPT until a restart.
    """
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": False})
    _install_active(phase=settled, state="st-settled")
    assert client.get("/api/ai/accounts").json()["login"] is None
    # ...while the status route still reports the outcome to whoever was polling.
    assert client.get("/api/ai/accounts/connect/status").json()["state"] == settled


@pytest.mark.parametrize("settled", ["done", "failed"])
def test_connect_is_allowed_again_once_the_previous_attempt_settles(
    client, monkeypatch, settled
):
    """The other half of the same bug: a settled attempt must not hold the
    single-flight gate either, or a second provider could never start."""
    monkeypatch.setattr(ai_accounts.ai_proxy, "start_login",
                         lambda p: {"state": "st-new", "url": "https://example.test/auth"})
    _install_active(phase=settled, state="st-old")
    resp = client.post("/api/ai/accounts/connect", json={"provider": "codex"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "st-new"


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


# -- API keys ------------------------------------------------------------------


def _install_fake_key_store(monkeypatch, store=None):
    """A tiny in-memory stand-in for the proxy's per-provider api-key arrays,
    faithful to the one behaviour these tests care about: replace_api_keys
    hands back auth-index by array position, exactly like the real PUT
    (docs/AI_PROXY_MANAGEMENT_API.md — auth-index is server-assigned)."""
    store = store if store is not None else {"claude": [], "codex": []}

    def list_api_keys(provider):
        return [dict(e) for e in store.get(provider, [])]

    def replace_api_keys(provider, entries):
        store[provider] = [
            {**{k: v for k, v in e.items() if k != "auth-index"},
             "auth-index": f"{provider}-{i}"}
            for i, e in enumerate(entries)
        ]
        return {"status": "ok"}

    monkeypatch.setattr(ai_accounts.ai_proxy, "list_api_keys", list_api_keys)
    monkeypatch.setattr(ai_accounts.ai_proxy, "replace_api_keys", replace_api_keys)
    return store


def test_add_key_rejects_unknown_provider(client, monkeypatch):
    _install_fake_key_store(monkeypatch)
    resp = client.post(
        "/api/ai/accounts/keys", json={"provider": "gemini", "api_key": "sk-abcdefgh"},
        headers=FUSED,
    )
    assert resp.status_code == 400
    assert "provider" in resp.json()["error"]


@pytest.mark.parametrize("bad_key", [
    None, "", "   ", "short", "has space", "\t\n", "x" * 5000,
])
def test_add_key_rejects_bad_api_key(client, monkeypatch, bad_key):
    _install_fake_key_store(monkeypatch)
    resp = client.post(
        "/api/ai/accounts/keys", json={"provider": "claude", "api_key": bad_key},
        headers=FUSED,
    )
    assert resp.status_code == 400
    assert "api_key" in resp.json()["error"]


def test_add_key_requires_x_fused_header(client, monkeypatch):
    _install_fake_key_store(monkeypatch)
    resp = client.post(
        "/api/ai/accounts/keys", json={"provider": "claude", "api_key": "sk-ant-abcdefgh"})
    assert resp.status_code == 403


def test_add_key_persists_and_returns_masked_hint(client, monkeypatch):
    _install_fake_key_store(monkeypatch)
    key = "sk-ant-abcdefgh12345"
    resp = client.post(
        "/api/ai/accounts/keys", json={"provider": "claude", "api_key": key}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["provider"] == "claude"
    assert body["hint"] == "..." + key[-4:]
    assert body["auth_index"] == "claude-0"
    assert key not in resp.text  # the full key never travels back to the client


def test_add_key_is_read_modify_write(client, monkeypatch):
    # A pre-existing key for the provider (and an unrelated one for the OTHER
    # provider) must both survive an add — the whole point of read-modify-
    # write over a naive PUT [{new}] is that it can't clobber what's there.
    store = {
        "claude": [{"api-key": "sk-ant-existing-key", "base-url": "", "auth-index": "old-0"}],
        "codex": [{"api-key": "sk-codex-existing", "base-url": "https://api.openai.com/v1",
                   "auth-index": "old-0"}],
    }
    _install_fake_key_store(monkeypatch, store)
    resp = client.post(
        "/api/ai/accounts/keys", json={"provider": "claude", "api_key": "sk-ant-new-key-999"},
        headers=FUSED)
    assert resp.status_code == 200, resp.text
    claude_keys = {e["api-key"] for e in store["claude"]}
    assert claude_keys == {"sk-ant-existing-key", "sk-ant-new-key-999"}
    # The codex list (a different provider entirely) is untouched.
    assert [e["api-key"] for e in store["codex"]] == ["sk-codex-existing"]


def test_add_key_defaults_base_url_for_codex(client, monkeypatch):
    store = {"claude": [], "codex": []}
    _install_fake_key_store(monkeypatch, store)
    resp = client.post(
        "/api/ai/accounts/keys", json={"provider": "codex", "api_key": "sk-codex-abcdefgh"},
        headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert store["codex"][0]["base-url"] == ai_accounts._CODEX_DEFAULT_BASE_URL

    # Claude gets no such default — the doc shows Claude persisting fine
    # with an empty base-url, so forcing one there would be pure guesswork.
    resp2 = client.post(
        "/api/ai/accounts/keys", json={"provider": "claude", "api_key": "sk-ant-abcdefgh"},
        headers=FUSED)
    assert resp2.status_code == 200, resp2.text
    assert "base-url" not in store["claude"][0]


def test_add_key_surfaces_error_when_write_does_not_persist(client, monkeypatch):
    # The trap, generalized: whatever the reason (the codex-no-base-url
    # silent drop, or anything else), if a read-back after PUT does not show
    # the key, this must be a clean error — never a false "ok".
    monkeypatch.setattr(ai_accounts.ai_proxy, "list_api_keys", lambda provider: [])
    monkeypatch.setattr(ai_accounts.ai_proxy, "replace_api_keys",
                         lambda provider, entries: {"status": "ok"})
    resp = client.post(
        "/api/ai/accounts/keys", json={"provider": "codex", "api_key": "sk-codex-abcdefgh"},
        headers=FUSED)
    assert resp.status_code == 502
    assert "did not persist" in resp.json()["error"]


def test_add_key_surfaces_proxy_error_on_write(client, monkeypatch):
    monkeypatch.setattr(ai_accounts.ai_proxy, "list_api_keys", lambda provider: [])

    def _boom(provider, entries):
        raise RuntimeError("management surface unreachable")

    monkeypatch.setattr(ai_accounts.ai_proxy, "replace_api_keys", _boom)
    resp = client.post(
        "/api/ai/accounts/keys", json={"provider": "claude", "api_key": "sk-ant-abcdefgh"},
        headers=FUSED)
    assert resp.status_code == 502
    assert "management surface unreachable" in resp.json()["error"]


def test_delete_key_by_auth_index_removes_only_target(client, monkeypatch):
    store = {
        "claude": [
            {"api-key": "sk-ant-keep", "base-url": "", "auth-index": "keep-me"},
            {"api-key": "sk-ant-gone", "base-url": "", "auth-index": "delete-me"},
        ],
        "codex": [
            {"api-key": "sk-codex-untouched", "base-url": "https://api.openai.com/v1",
             "auth-index": "delete-me"},  # same auth-index string, different provider
        ],
    }
    _install_fake_key_store(monkeypatch, store)
    resp = client.request("DELETE", "/api/ai/accounts/keys/delete-me", headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    # Only the claude entry with that exact auth-index is gone.
    assert [e["api-key"] for e in store["claude"]] == ["sk-ant-keep"]
    # The codex entry sharing the same auth-index string is untouched: the
    # route stops at the first provider whose list actually shrank.
    assert [e["api-key"] for e in store["codex"]] == ["sk-codex-untouched"]


def test_delete_key_requires_x_fused_header(client, monkeypatch):
    _install_fake_key_store(monkeypatch)
    resp = client.request("DELETE", "/api/ai/accounts/keys/anything")
    assert resp.status_code == 403


def test_delete_key_bogus_auth_index_is_a_clean_error(client, monkeypatch):
    store = {
        "claude": [{"api-key": "sk-ant-keep", "base-url": "", "auth-index": "real-index"}],
        "codex": [],
    }
    _install_fake_key_store(monkeypatch, store)
    resp = client.request("DELETE", "/api/ai/accounts/keys/no-such-index", headers=FUSED)
    assert resp.status_code == 404
    # Nothing was mutated.
    assert [e["api-key"] for e in store["claude"]] == ["sk-ant-keep"]


# -- routing strategy ------------------------------------------------------------


def test_routing_strategy_defaults_to_round_robin_in_listing(client, monkeypatch):
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": False})
    body = client.get("/api/ai/accounts").json()
    assert body["routing_strategy"] == "round-robin"


def test_put_routing_strategy_persists_and_restarts(client, monkeypatch):
    restarted = []
    monkeypatch.setattr(ai_accounts.ai_proxy, "restart_ai_proxy", lambda: restarted.append(1) or True)
    resp = client.put(
        "/api/ai/accounts/routing-strategy", json={"strategy": "fill-first"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "strategy": "fill-first", "restarted": True}
    assert restarted == [1]
    # Persisted — the next read (e.g. the listing) sees the new choice.
    assert ai_accounts.prefs.ai_routing_strategy() == "fill-first"
    monkeypatch.setattr(ai_accounts.ai_proxy, "status",
                         lambda: {"supervised": True, "running": False})
    assert client.get("/api/ai/accounts").json()["routing_strategy"] == "fill-first"


def test_put_routing_strategy_rejects_bad_value(client, monkeypatch):
    restarted = []
    monkeypatch.setattr(ai_accounts.ai_proxy, "restart_ai_proxy", lambda: restarted.append(1))
    resp = client.put(
        "/api/ai/accounts/routing-strategy", json={"strategy": "fastest"}, headers=FUSED)
    assert resp.status_code == 400
    assert restarted == []  # rejected before any attempt to restart the proxy
    assert ai_accounts.prefs.ai_routing_strategy() == "round-robin"  # unchanged


def test_put_routing_strategy_requires_x_fused_header(client, monkeypatch):
    resp = client.put("/api/ai/accounts/routing-strategy", json={"strategy": "fill-first"})
    assert resp.status_code == 403
    assert ai_accounts.prefs.ai_routing_strategy() == "round-robin"  # unchanged


# -- config regeneration must not destroy API keys -----------------------------
#
# shell/ai_proxy._write_config rewrites config.yaml from scratch on every spawn,
# and provider API keys live in that same file (the proxy writes them there when
# the management API adds one). So a regenerate that ignores them is silent data
# loss for every key the user added — including via the routing-strategy control,
# whose whole job is to restart the proxy. OAuth credentials are unaffected: they
# are separate files under auth-dir.


def _config_with_keys(tmp_path, monkeypatch, body: str) -> str:
    from fused_render.shell import ai_proxy as ap

    monkeypatch.setattr(ap, "_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ap, "_auth_dir", lambda: str(tmp_path / "auths"))
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(ap, "_config_path", lambda: str(cfg))
    cfg.write_text(body, encoding="utf-8")
    ap._write_config(1234, "gen-api-key", "gen-mgmt-key", "fill-first")
    return cfg.read_text(encoding="utf-8")


def test_write_config_preserves_existing_api_keys(tmp_path, monkeypatch):
    existing = (
        'host: "127.0.0.1"\n'
        "port: 9\n"
        "claude-api-key:\n"
        "  - api-key: sk-ant-KEEP-1\n"
        '    base-url: ""\n'
        "codex-api-key:\n"
        "  - api-key: sk-KEEP-2\n"
        "    base-url: https://api.openai.com/v1\n"
    )
    out = _config_with_keys(tmp_path, monkeypatch, existing)
    assert "sk-ant-KEEP-1" in out
    assert "sk-KEEP-2" in out
    # ...and the regenerated settings still took effect.
    assert 'strategy: "fill-first"' in out
    assert "gen-api-key" in out


def test_write_config_drops_unrelated_trailing_sections(tmp_path, monkeypatch):
    """Only the api-key blocks are carried over.

    The proxy rewrites this file itself and appends its own resolved settings
    (credential-concurrency, ws-auth, ...). Those are its defaults to
    regenerate, not ours to pin — copying them forward would freeze whatever
    the previous version happened to emit.
    """
    existing = (
        "claude-api-key:\n"
        "  - api-key: sk-ant-KEEP-3\n"
        "credential-concurrency:\n"
        "  reclaim-grace: 5s\n"
        "ws-auth: true\n"
    )
    out = _config_with_keys(tmp_path, monkeypatch, existing)
    assert "sk-ant-KEEP-3" in out
    assert "credential-concurrency" not in out
    assert "reclaim-grace" not in out


def test_write_config_with_no_existing_file_is_a_clean_first_spawn(tmp_path, monkeypatch):
    from fused_render.shell import ai_proxy as ap

    monkeypatch.setattr(ap, "_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ap, "_auth_dir", lambda: str(tmp_path / "auths"))
    monkeypatch.setattr(ap, "_config_path", lambda: str(tmp_path / "config.yaml"))
    ap._write_config(1234, "gen-api-key", "gen-mgmt-key", "round-robin")
    out = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "api-key" in out  # our own generated inbound key
    assert "claude-api-key" not in out  # nothing to preserve, nothing invented


# -- spawn failure must not leak a process (bugbot) -----------------------------


class _FakePopen:
    """A spawned process that stays ALIVE but never becomes healthy."""

    def __init__(self, pid=424242):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return None  # still running, the whole point

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


def _stub_spawn(monkeypatch, tmp_path, proc):
    """Point ai_proxy at a throwaway home and make Popen return `proc`."""
    from fused_render.shell import ai_proxy as ap

    monkeypatch.setattr(ap, "_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ap, "_auth_dir", lambda: str(tmp_path / "auths"))
    monkeypatch.setattr(ap, "_config_path", lambda: str(tmp_path / "config.yaml"))
    monkeypatch.setattr(ap, "_state_path", lambda: str(tmp_path / "ai_proxy.json"))
    monkeypatch.setattr(ap, "_log_path", lambda: str(tmp_path / "proxy.log"))
    monkeypatch.setattr(ap, "ai_proxy_bin", lambda: "/fake/cli-proxy-api")
    monkeypatch.setattr(ap, "_probe_models", lambda *a, **k: False)  # never healthy
    monkeypatch.setattr(ap, "_STARTUP_TIMEOUT_S", 0.3)
    monkeypatch.setattr(ap.subprocess, "Popen", lambda *a, **k: proc)
    return ap


def test_unhealthy_spawn_is_terminated_not_leaked(monkeypatch, tmp_path):
    """An alive-but-unhealthy spawn must be killed before giving up.

    Otherwise it leaks forever: no state file is written, so nothing later
    knows its pid, ownership can never be proven, and each retry spawns another
    beside it — and under FUSED_RENDER_AI_PROXY_PERSIST setsid has already
    detached it, so app teardown won't collect it either.
    """
    proc = _FakePopen()
    ap = _stub_spawn(monkeypatch, tmp_path, proc)
    with pytest.raises(RuntimeError, match="did not become healthy"):
        ap.ensure_ai_proxy()
    assert proc.terminated, "the unhealthy child was left running"
    assert proc.waited, "the killed child was never reaped"
    assert ap._child is None  # handle cleared, nothing stale to reap later
    assert not (tmp_path / "ai_proxy.json").exists()  # no state for a failed spawn


def test_hung_recorded_instance_is_killed_before_a_respawn(monkeypatch, tmp_path):
    """A recorded instance that stops answering but is still ALIVE must be
    killed before we overwrite the config and state that describe it — else it
    keeps running, unreachable and unidentifiable, so nothing can ever reap it.
    """
    proc = _FakePopen(pid=515151)
    ap = _stub_spawn(monkeypatch, tmp_path, proc)
    (tmp_path / "ai_proxy.json").write_text(
        '{"port": 65000, "pid": 999001, "api_key": "old", '
        '"management_key": "old-m", "config": "/old/config.yaml"}',
        encoding="utf-8")
    monkeypatch.setattr(ap, "_pid_alive", lambda pid: pid == 999001)
    killed = []
    monkeypatch.setattr(ap, "_kill_current_ai_proxy", lambda: killed.append(True))
    with pytest.raises(RuntimeError, match="did not become healthy"):
        ap.ensure_ai_proxy()
    assert killed == [True], "the hung instance was left behind"


def test_unconfirmable_hung_instance_does_not_block_a_respawn(monkeypatch, tmp_path):
    """If the stale pid can't be proven ours (a recycled pid on an unrelated
    process), refusing to spawn would be worse than the leak — so the kill
    failure is logged and the spawn proceeds."""
    proc = _FakePopen(pid=525252)
    ap = _stub_spawn(monkeypatch, tmp_path, proc)
    (tmp_path / "ai_proxy.json").write_text(
        '{"port": 65001, "pid": 999002, "api_key": "old", '
        '"management_key": "old-m", "config": "/old/config.yaml"}',
        encoding="utf-8")
    monkeypatch.setattr(ap, "_pid_alive", lambda pid: pid == 999002)

    def _refuse():
        raise RuntimeError("refusing to kill pid 999002: not confirmed ours")

    monkeypatch.setattr(ap, "_kill_current_ai_proxy", _refuse)
    # Reaches the spawn (and so the unhealthy path) rather than propagating the
    # kill refusal.
    with pytest.raises(RuntimeError, match="did not become healthy"):
        ap.ensure_ai_proxy()
    assert proc.terminated
