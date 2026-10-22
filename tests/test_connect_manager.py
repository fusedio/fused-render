"""connect/manager.py: sign-in kinds, status, token refresh, logout.

Vendor HTTP goes to an `httpx.MockTransport` (`manager._http`), the vendor CLI is
a shell script on a temp PATH, and the browser flows use a stand-in LoopbackFlow
so join / cancel / paste can be driven without a listener. The end-to-end flow
against the real fake vendor lives in test_connect_routes.py.
"""
import json
import os
import stat
import threading
import time

import httpx
import pytest

from fused_render.connect import manager, oauth, store, tools


def _pack(root, key, doc):
    d = root / key
    d.mkdir(parents=True)
    (d / "provider.json").write_text(json.dumps(doc))
    return d


PKCE = {"kind": "oauth-pkce", "client_id": "cid", "authorize_url": "https://demo.example/authorize",
        "token_url": "https://demo.example/token", "revoke_url": "https://demo.example/revoke",
        "identity_url": "https://demo.example/me",
        "redirect": {"host": "localhost", "ports": None, "path": "/callback"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CONNECT_STORE", "file")
    monkeypatch.setenv("FUSED_RENDER_CONNECT_OPEN_BROWSER", "0")
    monkeypatch.delenv("FUSED_RENDER_CONNECT_NOCID_CLIENT_ID", raising=False)
    root = tmp_path / "packs"
    monkeypatch.setenv("FUSED_RENDER_CONNECT_PROVIDERS", str(root))
    _pack(root, "demo", {"name": "Demo", "privacy": "p", "tools": {"remote": None}, "auth": [
        PKCE,
        {"kind": "cli", "command": ["fakecli", "token"], "identity_url": "https://demo.example/me"},
        {"kind": "api-key", "label": "Token", "identity_url": "https://demo.example/me"}]})
    _pack(root, "nocid", {"name": "NoCid", "privacy": "p", "auth": [
        dict(PKCE, client_id=""),
        {"kind": "cli", "command": ["definitely-not-installed-xyz"], "identity_url": "https://demo.example/me"},
        {"kind": "api-key", "label": "Token", "identity_url": "https://demo.example/me"}]})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "fakecli"
    cli.write_text(f"#!/bin/sh\ncat {tmp_path}/cli-token 2>/dev/null || exit 1\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    manager._flows.clear()
    manager._reconnect.clear()
    tools._cache.clear()
    yield
    for f in list(manager._flows.values()):
        f.cancelled = True
        if f.handle is not None:
            f.handle.cancel()
    manager._flows.clear()


class Vendor:
    def __init__(self, good=("good",)):
        self.good = set(good)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        tok = request.headers.get("authorization", "")[7:]
        if request.url.path == "/me":
            if tok not in self.good:
                return httpx.Response(401)
            return httpx.Response(200, json={"login": "octo", "id": 1})
        if request.url.path == "/revoke":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


@pytest.fixture()
def vendor(monkeypatch):
    v = Vendor()
    monkeypatch.setattr(manager, "_http", lambda: httpx.Client(transport=httpx.MockTransport(v)))
    return v


def test_list_and_availability(monkeypatch):
    by = {p["key"]: p for p in manager.list()}
    demo = by["demo"]
    assert demo["state"] == "disconnected" and demo["name"] == "Demo"
    assert [a["kind"] for a in demo["auth"]] == ["oauth-pkce", "cli", "api-key"]
    assert all(a["available"] for a in demo["auth"])
    assert demo["tools"] == {"remote": None, "native": False, "count": 0}
    assert demo["store"] == "file"
    nocid = {a["kind"]: a for a in by["nocid"]["auth"]}
    assert nocid["oauth-pkce"]["available"] is False
    assert "FUSED_RENDER_CONNECT_NOCID_CLIENT_ID" in nocid["oauth-pkce"]["reason"]
    assert nocid["cli"]["available"] is False
    monkeypatch.setenv("FUSED_RENDER_CONNECT_NOCID_CLIENT_ID", "from-env")
    nocid = {a["kind"]: a for a in {p["key"]: p for p in manager.list()}["nocid"]["auth"]}
    assert nocid["oauth-pkce"]["available"] is True


def test_unavailable_kind_refused_unless_byo_client_id():
    with pytest.raises(ValueError, match="CLIENT_ID"):
        manager.login("nocid", kind="oauth-pkce")
    with pytest.raises(manager.UnknownProvider):
        manager.status("missing")


def test_api_key_login_status_logout(vendor):
    with pytest.raises(ValueError, match="did not accept"):
        manager.login("demo", kind="api-key", value="bad")
    assert store.get("demo") is None
    st = manager.login("demo", kind="api-key", value="good")
    assert st["state"] == "connected" and st["account"] == "octo" and st["kind"] == "api-key"
    assert "good" not in json.dumps(st)
    assert store.get("demo")["access"] == "good"
    assert manager.token("demo") == "good"
    out = manager.logout("demo")
    assert out["state"] == "disconnected" and out["revoked"] is None
    assert store.get("demo") is None
    with pytest.raises(manager.NotConnected):
        manager.token("demo")


def test_cli_login_rereads_token_each_time(vendor, tmp_path):
    with pytest.raises(ValueError, match="printed no token"):
        manager.login("demo", kind="cli")
    (tmp_path / "cli-token").write_text("good\n")
    st = manager.login("demo", kind="cli")
    assert st["state"] == "connected" and st["account"] == "octo"
    assert store.get("demo") == {"kind": "cli", "account": "octo"}   # no token persisted
    assert manager.token("demo") == "good"
    (tmp_path / "cli-token").write_text("rotated\n")
    assert manager.token("demo") == "good"                        # cached for CLI_CACHE_S
    assert manager.token("demo", force_refresh=True) == "rotated"  # a 401's retry re-runs the CLI
    assert manager.token("demo") == "rotated"
    (tmp_path / "cli-token").unlink()
    with pytest.raises(manager.NotConnected):
        manager.token("demo", force_refresh=True)
    assert manager.status("demo")["state"] == "reconnect"
    manager.logout("demo")
    assert manager.status("demo")["state"] == "disconnected"


def _ts(access, refresh=None, expires_in=3600):
    return oauth.TokenSet(access=access, refresh=refresh, expires_at=time.time() + expires_in,
                          token_type="Bearer", scope=None, raw={})


def test_token_refreshes_near_expiry(monkeypatch):
    store.put("demo", {"kind": "oauth-pkce", "access": "a1", "refresh": "r1", "expires_at": time.time() + 30,
                       "token_url": "https://demo.example/token", "client_id": "cid"})
    calls = []

    def fake_refresh(**kw):
        calls.append(kw)
        return _ts("a2", "r2")
    monkeypatch.setattr(manager.oauth, "refresh", fake_refresh)
    assert manager.token("demo") == "a2"
    assert calls[0]["refresh_token"] == "r1" and calls[0]["token_url"] == "https://demo.example/token"
    assert store.get("demo")["refresh"] == "r2"
    assert manager.token("demo") == "a2"      # now fresh: no second refresh
    assert len(calls) == 1
    assert manager.token("demo", force_refresh=True) == "a2"   # a refused token: refresh anyway
    assert len(calls) == 2 and calls[1]["refresh_token"] == "r2"


def test_concurrent_refresh_happens_once(monkeypatch):
    store.put("demo", {"kind": "oauth-pkce", "access": "a1", "refresh": "r1", "expires_at": time.time() - 1,
                       "token_url": "https://demo.example/token", "client_id": "cid"})
    calls = []
    gate = threading.Event()

    def slow_refresh(**kw):
        calls.append(kw)
        gate.wait(2)
        return _ts("a2", "r2")
    monkeypatch.setattr(manager.oauth, "refresh", slow_refresh)
    out = []
    threads = [threading.Thread(target=lambda: out.append(manager.token("demo"))) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)
    assert out == ["a2", "a2", "a2"] and len(calls) == 1


def test_refresh_failure_marks_reconnect(monkeypatch):
    store.put("demo", {"kind": "oauth-pkce", "access": "a1", "refresh": "r1", "expires_at": time.time() - 1,
                       "token_url": "https://demo.example/token", "client_id": "cid"})

    def boom(**kw):
        raise oauth.FlowError("invalid_grant")
    monkeypatch.setattr(manager.oauth, "refresh", boom)
    with pytest.raises(manager.NotConnected):
        manager.token("demo")
    st = manager.status("demo")
    assert st["state"] == "reconnect" and st["kind"] == "oauth-pkce"


class FakeLoopback:
    """A LoopbackFlow stand-in: result() blocks until complete_from_url or cancel."""
    instances = []

    def __init__(self, **kw):
        self.kw = kw
        self.done = threading.Event()
        self.cancelled = False
        self.redirect_uri = "http://localhost:47823/callback"
        FakeLoopback.instances.append(self)

    def start(self):
        return "https://demo.example/authorize?state=s"

    def result(self, timeout=600):
        self.done.wait(timeout)
        if self.cancelled:
            raise oauth.FlowCancelled("cancelled")
        return _ts("good", "r1")

    def complete_from_url(self, url):
        assert "code=" in url
        self.done.set()

    def cancel(self):
        self.cancelled = True
        self.done.set()


@pytest.fixture()
def loopback(monkeypatch):
    FakeLoopback.instances = []
    monkeypatch.setattr(manager.oauth, "LoopbackFlow", FakeLoopback)
    return FakeLoopback


def test_pkce_login_joins_then_paste_completes_then_revokes(loopback, vendor, monkeypatch):
    opened = []
    monkeypatch.setattr(manager.webbrowser, "open", lambda u: opened.append(u) or True)
    st = manager.login("demo")
    assert st["state"] == "pending" and st["joined"] is False
    assert st["url"] == "https://demo.example/authorize?state=s"
    assert opened == []      # FUSED_RENDER_CONNECT_OPEN_BROWSER=0
    again = manager.login("demo")
    assert again["joined"] is True and len(loopback.instances) == 1
    kw = loopback.instances[0].kw
    assert kw["client_id"] == "cid" and kw["token_url"] == PKCE["token_url"]
    st = manager.callback("demo", "http://localhost:47823/callback?code=c&state=s")
    assert st["state"] == "connected" and st["account"] == "octo"
    rec = store.get("demo")
    assert rec["token_url"] == PKCE["token_url"] and rec["revoke_url"] == PKCE["revoke_url"]
    assert rec["refresh"] == "r1" and rec["client_id"] == "cid"
    out = manager.logout("demo")
    assert out["revoked"] is True and out["state"] == "disconnected"
    rv = [r for r in vendor.requests if r.url.path == "/revoke"]
    assert rv and "token=good" in rv[0].content.decode() and rv[0].headers["authorization"] == "Bearer good"


def test_browser_opened_when_allowed(loopback, vendor, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_OPEN_BROWSER", "1")
    opened = []
    monkeypatch.setattr(manager.webbrowser, "open", lambda u: opened.append(u) or True)
    manager.login("demo")
    assert opened == ["https://demo.example/authorize?state=s"]


def test_cancel_drops_the_flow(loopback, vendor):
    manager.login("demo")
    st = manager.cancel("demo")
    assert st["state"] == "disconnected"
    flow = manager._flows["demo"]
    assert flow.finished.wait(5)
    assert store.get("demo") is None and manager.status("demo")["state"] == "disconnected"
    with pytest.raises(ValueError):
        manager.callback("demo", "http://localhost:47823/callback?code=c&state=s")


def test_logout_revoke_failure_still_deletes(monkeypatch):
    def down(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(manager, "_http", lambda: httpx.Client(transport=httpx.MockTransport(down)))
    store.put("demo", {"kind": "oauth-pkce", "access": "a", "revoke_url": "https://demo.example/revoke"})
    out = manager.logout("demo")
    assert out["revoked"] is False and "warning" in out
    assert store.get("demo") is None


def test_slack_identity_is_user_at_team(monkeypatch):
    def h(request):
        assert request.method == "POST"
        return httpx.Response(200, json={"ok": True, "user": "ana", "team": "acme"})
    monkeypatch.setattr(manager, "_http", lambda: httpx.Client(transport=httpx.MockTransport(h)))
    assert manager._identity("https://slack.com/api/auth.test", "xoxp") == "ana@acme"
