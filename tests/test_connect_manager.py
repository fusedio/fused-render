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
    _pack(root, "tlsdemo", {"name": "TlsDemo", "privacy": "p", "tools": {"remote": None}, "auth": [
        dict(PKCE, redirect={"scheme": "https", "host": "localhost", "ports": None, "path": "/callback"})]})
    _pack(root, "gdemo", {"name": "GDemo", "privacy": "p", "auth": [
        dict(PKCE, client_id="", byo={"docs": "https://g.example/creds", "secret": "Client secret"})]})
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
    assert "cert_warning" not in demo["auth"][0]
    # An https localhost redirect (Slack): the list says so, so the card can warn BEFORE Connect.
    assert by["tlsdemo"]["auth"][0]["cert_warning"] is True and by["tlsdemo"]["auth"][0]["available"]
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


def test_a_vendor_that_cannot_check_a_pasted_key_stores_nothing(monkeypatch):
    monkeypatch.setattr(manager, "_http", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(ValueError, match="could not check"):
        manager.login("demo", kind="api-key", value="k")
    assert store.get("demo") is None


def test_identity_probe_failure_keeps_the_grant(loopback, monkeypatch):
    """The token endpoint just issued the token; a label probe that cannot be
    reached (or is rate-limited) must not throw the sign-in away."""
    def down(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(manager, "_http", lambda: httpx.Client(transport=httpx.MockTransport(down)))
    manager.login("demo")
    st = manager.callback("demo", "http://localhost:47823/callback?code=c&state=s")
    assert st["state"] == "connected" and st.get("account") is None
    assert store.get("demo")["access"] == "good"
    manager.logout("demo")
    v = Vendor(good=())    # 403 on /me, as GitHub answers a rate limit
    monkeypatch.setattr(manager, "_http", lambda: httpx.Client(transport=httpx.MockTransport(v)))
    manager.login("demo")
    st = manager.callback("demo", "http://localhost:47823/callback?code=c&state=s")
    assert st["state"] == "connected" and store.get("demo")["access"] == "good"


def test_reconnect_keeps_the_client_id_the_sign_in_was_made_with(loopback, vendor):
    manager.login("demo", kind="oauth-pkce", client_id="my-app")
    manager.callback("demo", "http://localhost:47823/callback?code=c&state=s")
    assert store.get("demo")["client_id"] == "my-app"
    manager.mark_reconnect("demo")
    assert manager.status("demo")["state"] == "reconnect"
    manager.login("demo", kind="oauth-pkce")       # what the Reconnect button sends
    assert loopback.instances[-1].kw["client_id"] == "my-app"
    manager.callback("demo", "http://localhost:47823/callback?code=c&state=s")
    assert manager.status("demo")["state"] == "connected"
    # A paste still wins, and Disconnect forgets the choice.
    manager.login("demo", kind="oauth-pkce", client_id="other")
    assert loopback.instances[-1].kw["client_id"] == "other"
    manager.cancel("demo")
    manager._flows["demo"].finished.wait(5)
    manager.logout("demo")
    manager.login("demo", kind="oauth-pkce")
    assert loopback.instances[-1].kw["client_id"] == "cid"


def test_byo_secret_rides_with_the_id_and_lives_in_the_record(loopback, vendor, monkeypatch):
    """Google's desktop clients: the Advanced form posts the id and the secret (`value`);
    the secret goes to the flow, is kept in the record, and refresh and Reconnect reuse
    it. Without the secret the pack asked for, the sign-in is refused before a tab opens."""
    with pytest.raises(ValueError, match="client secret"):
        manager.login("gdemo", kind="oauth-pkce", client_id="my.apps")
    assert "gdemo" not in manager._flows
    manager.login("gdemo", kind="oauth-pkce", client_id="my.apps", value=" GOCSPX-s ")
    assert loopback.instances[-1].kw["client_id"] == "my.apps"
    assert loopback.instances[-1].kw["client_secret"] == "GOCSPX-s"
    manager.callback("gdemo", "http://localhost:47823/callback?code=c&state=s")
    rec = store.get("gdemo")
    assert rec["client_id"] == "my.apps" and rec["client_secret"] == "GOCSPX-s"
    assert "GOCSPX-s" not in json.dumps(manager.status("gdemo"))
    assert "GOCSPX-s" not in json.dumps(manager.list())
    calls = []

    def fake_refresh(**kw):
        calls.append(kw)
        return _ts("a2", None)
    monkeypatch.setattr(manager.oauth, "refresh", fake_refresh)
    store.put("gdemo", {**rec, "refresh": "r1", "expires_at": time.time() - 1})
    assert manager.token("gdemo") == "a2"
    assert calls[0]["client_secret"] == "GOCSPX-s" and calls[0]["client_id"] == "my.apps"
    manager.mark_reconnect("gdemo")
    manager.login("gdemo", kind="oauth-pkce")       # the Reconnect button: only the kind
    assert loopback.instances[-1].kw["client_id"] == "my.apps"
    assert loopback.instances[-1].kw["client_secret"] == "GOCSPX-s"


def test_environment_secret_is_not_copied_into_the_record(loopback, vendor, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_GDEMO_CLIENT_ID", "env.apps")
    monkeypatch.setenv("FUSED_RENDER_CONNECT_GDEMO_CLIENT_SECRET", "env-secret")
    manager.login("gdemo", kind="oauth-pkce")
    assert loopback.instances[-1].kw["client_secret"] == "env-secret"
    manager.callback("gdemo", "http://localhost:47823/callback?code=c&state=s")
    rec = store.get("gdemo")
    assert rec["client_id"] == "env.apps" and "client_secret" not in rec
    calls = []

    def fake_refresh(**kw):
        calls.append(kw)
        return _ts("a2", None)
    monkeypatch.setattr(manager.oauth, "refresh", fake_refresh)
    store.put("gdemo", {**rec, "refresh": "r1", "expires_at": time.time() - 1})
    manager.token("gdemo")
    assert calls[0]["client_secret"] == "env-secret"   # read from the pack (the environment) at refresh time


def test_mcp_oauth_sends_the_resource_the_metadata_names(loopback, monkeypatch, tmp_path):
    """RFC 8707: the `resource` on both OAuth legs is the identifier the server's
    protected-resource metadata declares, not necessarily the URL we connect to."""
    from fused_render.connect import discovery
    url = "http://127.0.0.1:9/mcp"
    _pack(tmp_path / "packs", "mcpdemo", {"name": "McpDemo", "privacy": "p", "tools": {"remote": url},
                                          "auth": [{"kind": "mcp-oauth", "resource": url}]})
    d = discovery.Discovered(resource="http://127.0.0.1:9/", issuer="https://as.example",
                             authorization_endpoint="https://as.example/authorize",
                             token_endpoint="https://as.example/token", registration_endpoint=None,
                             revocation_endpoint=None, scopes=[], cimd_supported=False,
                             pkce_methods=["S256"], raw={})
    monkeypatch.setattr(discovery, "discover", lambda u: d)
    monkeypatch.setattr(discovery, "choose_client", lambda *a, **k: ("cid", None))
    manager.login("mcpdemo")
    kw = loopback.instances[-1].kw
    assert kw["extra_authorize"] == {"resource": "http://127.0.0.1:9/"}
    assert kw["extra_token"] == {"resource": "http://127.0.0.1:9/"}
    st = manager.callback("mcpdemo", "http://localhost:47823/callback?code=c&state=s")
    assert st["state"] == "connected"
    assert store.get("mcpdemo")["resource"] == "http://127.0.0.1:9/"


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
