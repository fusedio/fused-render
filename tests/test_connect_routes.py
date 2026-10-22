"""/api/connect/*: the HTTP surface, and one end-to-end sign-in against the fake vendor.

The route tests use the fake vendor's pack (`tests/_fake_provider`) with the fake
running on 127.0.0.1, so 404 / 409 / the X-Fused guard / the api-key paste are
checked against real registry, store (file backend under a temp home) and HTTP.
The end-to-end test drives the whole `mcp-oauth` path: discovery, DCR, the
loopback listener, the token exchange with PKCE and `resource`, the identity
from `initialize`, tools/list and tools/call through the MCP client, refresh,
and logout with revocation. The browser is a stand-in that GETs the authorize
URL and follows the fake's auto-approve redirect to the listener.
"""
import os
import threading

import httpx
import pytest
from _fake_vendor import FAKE_API_KEY, FAKE_LOGIN, FakeVendor
from fastapi.testclient import TestClient

from fused_render.connect import manager, store, tools
from fused_render.server import create_app

FUSED = {"X-Fused": "1"}
PACKS = os.path.join(os.path.dirname(__file__), "_fake_provider")


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    # 300 s tokens: the fake's default 60 s equals manager.REFRESH_MARGIN, which
    # would refresh on every token() call; the refresh path is driven by expire_all().
    v = FakeVendor(access_ttl=300).start()
    monkeypatch.setenv("FUSED_RENDER_FAKE_VENDOR_URL", v.base_url)
    monkeypatch.setenv("FUSED_RENDER_CONNECT_PROVIDERS", PACKS)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CONNECT_STORE", "file")
    monkeypatch.setenv("FUSED_RENDER_CONNECT_OPEN_BROWSER", "0")
    monkeypatch.delenv("FUSED_RENDER_CLIENT_METADATA_URL", raising=False)
    for d in (manager._flows, tools._cache, tools._clients, tools._native):
        d.clear()
    manager._reconnect.clear()
    yield v
    for f in list(manager._flows.values()):
        f.cancelled = True
        if f.handle is not None:
            f.handle.cancel()
    manager._flows.clear()
    tools.invalidate("fakevendor")
    v.stop()


@pytest.fixture()
def client(tmp_path, fake):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_list_and_status(client):
    rows = client.get("/api/connect").json()
    fv = next(r for r in rows if r["key"] == "fakevendor")
    assert fv["name"] == "Fake Vendor" and fv["state"] == "disconnected"
    assert [a["kind"] for a in fv["auth"]] == ["mcp-oauth", "oauth-pkce", "oauth-device", "api-key"]
    assert fv["icon"] == "/api/connect/fakevendor/icon"
    assert fv["tools"]["remote"].endswith("/mcp")
    assert client.get("/api/connect/fakevendor").json()["state"] == "disconnected"
    icon = client.get("/api/connect/fakevendor/icon")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg")


def test_unknown_key_is_404(client):
    assert client.get("/api/connect/nope").status_code == 404
    assert client.get("/api/connect/nope/tools").status_code == 404
    assert client.get("/api/connect/nope/icon").status_code == 404
    for path in ("login", "logout", "cancel", "callback", "tools/call", "key"):
        r = client.post(f"/api/connect/nope/{path}", json={"name": "x", "url": "x", "value": "x"}, headers=FUSED)
        assert r.status_code == 404, path
        assert "error" in r.json()


def test_posts_need_x_fused(client):
    for path in ("login", "logout", "cancel", "callback", "tools/call", "key"):
        r = client.post(f"/api/connect/fakevendor/{path}", json={})
        assert r.status_code == 403, path


def test_bad_bodies_are_400(client):
    assert client.post("/api/connect/fakevendor/login", json={"kind": 3}, headers=FUSED).status_code == 400
    assert client.post("/api/connect/fakevendor/login", json=[1], headers=FUSED).status_code == 400
    assert client.post("/api/connect/fakevendor/login", json={"kind": "nope"}, headers=FUSED).status_code == 400
    assert client.post("/api/connect/fakevendor/key", json={}, headers=FUSED).status_code == 400
    assert client.post("/api/connect/fakevendor/callback", json={}, headers=FUSED).status_code == 400
    assert client.post("/api/connect/fakevendor/callback", json={"url": "http://x/?code=1"},
                       headers=FUSED).status_code == 409    # nothing waiting
    assert client.post("/api/connect/fakevendor/tools/call", json={"args": {}}, headers=FUSED).status_code == 400
    assert client.post("/api/connect/fakevendor/tools/call", json={"name": "echo", "args": [1]},
                       headers=FUSED).status_code == 400


def test_tools_call_409_when_not_connected(client):
    r = client.post("/api/connect/fakevendor/tools/call", json={"name": "echo", "args": {"text": "x"}}, headers=FUSED)
    assert r.status_code == 409 and r.json()["code"] == "not_connected"
    assert client.get("/api/connect/fakevendor/tools").json() == []


def test_api_key_paste_then_tools(client, fake):
    r = client.post("/api/connect/fakevendor/key", json={"value": "wrong"}, headers=FUSED)
    assert r.status_code == 400
    r = client.post("/api/connect/fakevendor/login", json={"kind": "api-key", "value": FAKE_API_KEY}, headers=FUSED)
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["state"] == "connected" and st["account"] == FAKE_LOGIN
    assert FAKE_API_KEY not in r.text
    names = [t["name"] for t in client.get("/api/connect/fakevendor/tools").json()]
    assert names == ["echo", "write_note"]
    r = client.post("/api/connect/fakevendor/tools/call", json={"name": "echo", "args": {"text": "hey"}},
                    headers=FUSED)
    out = r.json()
    assert out["ok"] is True and out["text"] == "hey" and out["isError"] is False
    assert FAKE_API_KEY not in r.text
    assert client.post("/api/connect/fakevendor/logout", headers=FUSED).json()["state"] == "disconnected"


def _fake_browser(opened):
    """webbrowser.open stand-in: GET the authorize URL and follow the redirect
    to the loopback listener, on a thread, the way a browser tab would."""
    def open_(url):
        opened.append(url)

        def go():
            with httpx.Client(follow_redirects=True, trust_env=False, timeout=10) as c:
                c.get(url)
        threading.Thread(target=go, daemon=True).start()
        return True
    return open_


def test_end_to_end_mcp_oauth(fake, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_OPEN_BROWSER", "1")
    opened = []
    monkeypatch.setattr(manager.webbrowser, "open", _fake_browser(opened))
    st = manager.login("fakevendor", kind="mcp-oauth")
    assert st["kind"] == "mcp-oauth" and opened and opened[0].startswith(fake.base_url + "/authorize")
    assert "resource=" in opened[0]
    flow = manager._flows["fakevendor"]
    assert flow.finished.wait(15), "the sign-in did not finish"
    st = manager.status("fakevendor")
    assert st["state"] == "connected", st
    assert st["account"] == "fakevendor"     # serverInfo.name
    rec = store.get("fakevendor")
    assert rec["resource"] == fake.base_url + "/mcp"
    assert rec["client_id"] in fake.clients                 # registered by DCR
    assert rec["token_url"] == fake.base_url + "/token" and rec["refresh"]
    assert any(t.get("resource") == rec["resource"] for t in fake.token_requests)

    by = {t.name: t for t in tools.list_tools("fakevendor")}
    assert set(by) == {"echo", "write_note"}
    assert by["echo"].read_only is True and by["write_note"].read_only is False
    out = tools.call_tool("fakevendor", "echo", {"text": "round trip"})
    assert out["ok"] is True and out["text"] == "round trip"

    # A token the vendor has aged out: one refresh, one retry, then it works.
    fake.expire_all()
    old = store.get("fakevendor")["access"]
    out = tools.call_tool("fakevendor", "write_note", {"title": "t"})
    assert out["ok"] is True and fake.notes == [{"title": "t"}]
    assert store.get("fakevendor")["access"] != old

    cur = store.get("fakevendor")
    out = manager.logout("fakevendor")
    assert out["state"] == "disconnected" and out["revoked"] is True
    assert store.get("fakevendor") is None
    assert tools.list_tools("fakevendor") == []
    assert cur["access"] in fake.revoked and cur["refresh"] in fake.revoked


def test_end_to_end_mcp_oauth_sse(fake, monkeypatch):
    """The same session over `text/event-stream` answers (?sse=1 on the resource)."""
    rec_resource = fake.base_url + "/mcp?sse=1"
    tok = fake.issue()
    from fused_render.connect.mcp_client import McpClient
    c = McpClient(rec_resource, tok["access_token"])
    try:
        assert c.initialize()["serverInfo"]["name"] == "fakevendor"
        assert [t["name"] for t in c.list_tools()] == ["echo", "write_note"]
        assert c.call("echo", {"text": "sse"})["content"][0]["text"] == "sse"
    finally:
        c.close()


def test_end_to_end_oauth_pkce_through_routes(client, fake, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_OPEN_BROWSER", "1")
    opened = []
    monkeypatch.setattr(manager.webbrowser, "open", _fake_browser(opened))
    r = client.post("/api/connect/fakevendor/login", json={"kind": "oauth-pkce"}, headers=FUSED)
    assert r.status_code == 200 and r.json()["url"] == opened[0]
    assert manager._flows["fakevendor"].finished.wait(15)
    st = client.get("/api/connect/fakevendor").json()
    assert st["state"] == "connected" and st["account"] == FAKE_LOGIN and st["kind"] == "oauth-pkce"
    out = client.post("/api/connect/fakevendor/tools/call", json={"name": "echo", "args": {"text": "pkce"}},
                      headers=FUSED).json()
    assert out["text"] == "pkce"
    assert client.post("/api/connect/fakevendor/logout", headers=FUSED).json()["revoked"] is True


def test_end_to_end_device_flow(fake):
    fake.device_interval = 0.4    # pending long enough for the reply to see it
    st = manager.login("fakevendor", kind="oauth-device")
    assert st["state"] == "pending" and st["user_code"] and st["verification_uri"]
    assert manager._flows["fakevendor"].finished.wait(15)
    st = manager.status("fakevendor")
    assert st["state"] == "connected" and st["account"] == FAKE_LOGIN
