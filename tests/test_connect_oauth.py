"""connect.oauth against tests/_fake_vendor.py (loopback + PKCE, paste
fallback, device code, refresh) and against MockTransport for the token
shapes the fake does not speak (Slack's envelope, GitHub's form body)."""
import base64
import hashlib
import json
import os
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fake_vendor import FAKE_API_KEY, FAKE_CLIENT_ID, FakeVendor

from fused_render.connect import oauth
from fused_render.connect.oauth import (
    DeviceFlow,
    FlowCancelled,
    FlowError,
    FlowTimeout,
    LoopbackFlow,
    TokenSet,
)

ANY_PORT = {"host": "127.0.0.1", "ports": None, "path": "/callback"}


@pytest.fixture
def vendor():
    v = FakeVendor(port=0).start()
    yield v
    v.stop()


def _browser():
    # The "browser": follows the vendor's 302 to the loopback listener.
    return httpx.Client(timeout=10, follow_redirects=True)


def _flow(vendor, **kw):
    kw.setdefault("redirect", ANY_PORT)
    return LoopbackFlow(authorize_url=vendor.base_url + "/authorize",
                        token_url=vendor.base_url + "/token", client_id=FAKE_CLIENT_ID,
                        scopes={"scope": "read write"}, **kw)


def _free_ports(n):
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def test_pkce_pair_is_s256():
    v, c = oauth.pkce_pair()
    assert 43 <= len(v) <= 128
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert oauth.pkce_pair()[0] != v


def test_redirect_constants_and_uris():
    assert (oauth.REDIRECT_HOST, oauth.REDIRECT_PATH) == ("localhost", "/callback")
    assert oauth.REDIRECT_PORTS == [47823, 47824, 47825, 47826, 47827]
    assert oauth.redirect_uris()[0] == "http://localhost:47823/callback"
    assert len(oauth.redirect_uris()) == 5
    assert oauth.redirect_uris(ANY_PORT) == []


def test_tokenset_repr_hides_tokens():
    t = TokenSet("SECRET-A", "SECRET-R", None, "Bearer", None, {"access_token": "SECRET-A"})
    assert "SECRET" not in repr(t)


# ------------------------------------------------------------- loopback ---

def test_loopback_flow_end_to_end(vendor):
    flow = _flow(vendor, extra_authorize={"resource": vendor.base_url + "/mcp"},
                 extra_token={"resource": vendor.base_url + "/mcp"})
    url = flow.start()
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["client_id"] == FAKE_CLIENT_ID and q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256" and q["state"] == flow.state
    assert q["redirect_uri"] == flow.redirect_uri and flow.redirect_uri.startswith("http://127.0.0.1:")
    assert q["scope"] == "read write" and q["resource"] == vendor.base_url + "/mcp"
    assert flow.listening
    with _browser() as b:
        page = b.get(url)
    assert page.status_code == 200 and "close this tab" in page.text
    t = flow.result(timeout=5)
    assert t.access.startswith("at-") and t.refresh.startswith("rt-")
    assert t.token_type == "Bearer" and t.scope == "read write"
    assert 55 < t.expires_at - time.time() <= 60
    form = vendor.token_requests[-1]
    assert form["grant_type"] == "authorization_code" and form["code_verifier"]
    assert form["redirect_uri"] == flow.redirect_uri and form["resource"] == vendor.base_url + "/mcp"
    assert "client_secret" not in form
    assert oauth.identity(vendor.base_url + "/me", t.access) == "fake-user"


def test_listener_binds_first_free_port_of_the_list(vendor):
    busy, free, spare = _free_ports(3)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", busy))
    blocker.listen(1)
    try:
        flow = _flow(vendor, redirect={"host": "localhost", "ports": [busy, free, spare],
                                       "path": "/callback"})
        url = flow.start()
        assert flow.redirect_uri == f"http://localhost:{free}/callback"
        with _browser() as b:
            assert b.get(url).status_code == 200
        assert flow.result(timeout=5).access
    finally:
        blocker.close()


def test_all_ports_busy_still_offers_the_paste(vendor):
    (busy,) = _free_ports(1)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", busy))
    blocker.listen(1)
    try:
        flow = _flow(vendor, redirect={"host": "127.0.0.1", "ports": [busy], "path": "/callback"})
        url = flow.start()
        assert not flow.listening and flow.redirect_uri == f"http://127.0.0.1:{busy}/callback"
        with httpx.Client() as b:
            loc = b.get(url).headers["location"]
        flow.complete_from_url(loc)
        assert flow.result(timeout=5).access
    finally:
        blocker.close()


def test_state_mismatch_is_ignored_and_the_flow_keeps_waiting(vendor):
    flow = _flow(vendor)
    flow.start()
    with _browser() as b:
        r = b.get(flow.redirect_uri + "?code=x&state=wrong")
        assert r.status_code == 400 and "different" in r.text
        r = b.get(flow.redirect_uri.replace("/callback", "/favicon.ico"))
        assert r.status_code == 404
        with pytest.raises(FlowTimeout):
            flow.result(timeout=0.3)


def test_paste_fallback_completes_with_the_listener_unreached(vendor):
    flow = _flow(vendor)
    url = flow.start()
    with httpx.Client() as b:  # does not follow: the browser "cannot reach" us
        r = b.get(url)
    assert r.status_code == 302
    pasted = "  " + r.headers["location"] + "\n"
    with pytest.raises(FlowError):
        flow.complete_from_url("http://localhost:1/callback?state=" + flow.state)  # no code
    with pytest.raises(FlowError):
        flow.complete_from_url(pasted.replace(flow.state, "other"))
    flow.complete_from_url(pasted)
    assert flow.result(timeout=5).access
    with pytest.raises(FlowError):
        flow.complete_from_url(pasted)  # already finished


def test_vendor_denial_is_a_flow_error(vendor):
    flow = _flow(vendor)
    url = flow.start() + "&approve=manual"
    with _browser() as b:
        page = b.get(url)
        assert 'id="allow"' in page.text
        deny = b.get(vendor.base_url + page.text.split('id="deny" href="')[1].split('"')[0]
                     .replace("&amp;", "&"))
    assert deny.status_code == 400
    with pytest.raises(FlowError) as e:
        flow.result(timeout=5)
    assert e.value.code == "access_denied" and not isinstance(e.value, FlowCancelled)


def test_manual_approve_page_allow_link(vendor):
    flow = _flow(vendor)
    with _browser() as b:
        page = b.get(flow.start() + "&approve=manual")
        href = page.text.split('id="allow" href="')[1].split('"')[0].replace("&amp;", "&")
        assert b.get(vendor.base_url + href).status_code == 200
    assert flow.result(timeout=5).access


def test_cancel_wakes_a_waiting_result(vendor):
    flow = _flow(vendor)
    flow.start()
    out = {}

    def wait():
        try:
            flow.result(timeout=10)
        except Exception as exc:  # noqa: BLE001
            out["exc"] = exc

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.1)
    flow.cancel()
    t.join(5)
    assert isinstance(out.get("exc"), FlowCancelled)
    time.sleep(0.7)  # the listener notices within one 0.5 s tick
    with pytest.raises(httpx.HTTPError):
        httpx.get(flow.redirect_uri + "?code=x&state=" + flow.state, timeout=1)


def test_bad_verifier_is_refused_by_the_vendor(vendor):
    flow = _flow(vendor)
    flow._verifier = "not-the-one-that-made-the-challenge-xxxxxxxxxxxxxxxxxxx"
    with _browser() as b:
        page = b.get(flow.start())
    assert page.status_code == 502
    with pytest.raises(FlowError) as e:
        flow.result(timeout=5)
    assert e.value.code == "invalid_grant"


def test_token_post_shape_and_client_secret_only_when_given():
    seen = []

    def handler(req):
        seen.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
        return httpx.Response(200, json={"access_token": "a", "expires_in": 10})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    for secret in (None, "desktop-secret"):
        flow = LoopbackFlow(authorize_url="https://v.example/auth", token_url="https://v.example/token",
                            client_id="cid", redirect=ANY_PORT, client_secret=secret,
                            extra_token={"resource": "https://v.example/mcp"}, http=http)
        url = flow.start()
        flow.complete_from_url(f"http://x/callback?code=C&state={flow.state}")
        t = flow.result(timeout=2)
        assert t.access == "a" and t.refresh is None and t.token_type == "Bearer"
        assert "?" in url
    assert seen[0] == {"grant_type": "authorization_code", "code": "C",
                       "code_verifier": seen[0]["code_verifier"], "client_id": "cid",
                       "redirect_uri": seen[0]["redirect_uri"], "resource": "https://v.example/mcp"}
    assert "client_secret" not in seen[0] and seen[1]["client_secret"] == "desktop-secret"


def test_authorize_url_with_an_existing_query():
    flow = LoopbackFlow(authorize_url="https://v.example/auth?prompt=consent", token_url="https://t",
                        client_id="cid", redirect=ANY_PORT, extra_authorize={"access_type": "offline"})
    url = flow.start()
    flow.cancel()
    q = parse_qs(urlparse(url).query)
    assert q["prompt"] == ["consent"] and q["access_type"] == ["offline"]


# ------------------------------------------------------- token shapes ---

def _mock(body, status=200, headers=None):
    def handler(req):
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(status, text=body, headers=headers)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_slack_user_token_envelope():
    body = {"ok": True, "access_token": "xoxb-bot", "token_type": "bot",
            "authed_user": {"id": "U1", "scope": "search:read", "access_token": "xoxp-user",
                            "token_type": "user", "refresh_token": "xoxe-1-r", "expires_in": 43200}}
    t = oauth.refresh(token_url="https://slack.com/api/oauth.v2.access", client_id="1.2",
                      refresh_token="old", http=_mock(body))
    assert t.access == "xoxp-user" and t.refresh == "xoxe-1-r" and t.scope == "search:read"
    assert 43000 < t.expires_at - time.time() <= 43200 and t.raw["authed_user"]["id"] == "U1"


def test_slack_ok_false_is_a_flow_error():
    with pytest.raises(FlowError) as e:
        oauth.refresh(token_url="https://slack.com/api/oauth.v2.access", client_id="1.2",
                      refresh_token="old", http=_mock({"ok": False, "error": "invalid_refresh_token"}))
    assert e.value.code == "invalid_refresh_token" and str(e.value) == "invalid_refresh_token"


def test_form_encoded_and_error_with_200_bodies():
    t = oauth.refresh(token_url="https://t", client_id="c", refresh_token="r",
                      http=_mock("access_token=gho_x&token_type=bearer&scope=repo",
                                 headers={"content-type": "application/x-www-form-urlencoded"}))
    assert t.access == "gho_x" and t.scope == "repo" and t.expires_at is None
    with pytest.raises(FlowError) as e:
        oauth.refresh(token_url="https://t", client_id="c", refresh_token="r",
                      http=_mock({"error": "bad_refresh_token", "error_description": "nope"}))
    assert e.value.code == "bad_refresh_token" and "nope" in str(e.value)
    with pytest.raises(FlowError):
        oauth.refresh(token_url="https://t", client_id="c", refresh_token="r", http=_mock("", 500))
    with pytest.raises(FlowError):
        oauth.refresh(token_url="https://t", client_id="c", refresh_token="r", http=_mock({}))


# -------------------------------------------------------------- refresh ---

def test_refresh_rotates_and_the_old_token_dies(vendor):
    first = vendor.issue()
    t = oauth.refresh(token_url=vendor.base_url + "/token", client_id=FAKE_CLIENT_ID,
                      refresh_token=first["refresh_token"])
    assert t.access != first["access_token"] and t.refresh and t.refresh != first["refresh_token"]
    assert vendor.token_requests[-1]["grant_type"] == "refresh_token"
    with pytest.raises(FlowError) as e:
        oauth.refresh(token_url=vendor.base_url + "/token", client_id=FAKE_CLIENT_ID,
                      refresh_token=first["refresh_token"])
    assert e.value.code == "invalid_grant"


def test_refresh_without_rotation_returns_none_and_sends_extras():
    seen = {}

    def handler(req):
        seen.update({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    t = oauth.refresh(token_url="https://t", client_id="c", refresh_token="keep", client_secret="s",
                      extra={"resource": "https://r/mcp"},
                      http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert t.access == "new" and t.refresh is None
    assert seen == {"grant_type": "refresh_token", "refresh_token": "keep", "client_id": "c",
                    "client_secret": "s", "resource": "https://r/mcp"}


def test_expired_access_token_is_rejected_then_refresh_fixes_it(vendor):
    first = vendor.issue()
    assert oauth.identity(vendor.base_url + "/me", first["access_token"]) == "fake-user"
    vendor.expire_all()
    assert oauth.identity(vendor.base_url + "/me", first["access_token"]) is None
    t = oauth.refresh(token_url=vendor.base_url + "/token", client_id=FAKE_CLIENT_ID,
                      refresh_token=first["refresh_token"])
    assert oauth.identity(vendor.base_url + "/me", t.access) == "fake-user"
    assert oauth.identity(vendor.base_url + "/me", FAKE_API_KEY) == "fake-user"


# --------------------------------------------------------------- device ---

def _device(vendor, **kw):
    return DeviceFlow(device_url=vendor.base_url + "/device", token_url=vendor.base_url + "/token",
                      client_id=FAKE_CLIENT_ID, scope="read write", **kw)


def test_device_flow_polls_until_approved(vendor):
    flow = _device(vendor)
    info = flow.start()
    assert info["user_code"].startswith("FAKE-") and info["verification_uri"].endswith("/device/verify")
    assert info["verification_uri_complete"] and info["expires_in"] == 900
    assert "device_code" not in info
    t = flow.result(timeout=10)
    assert t.access.startswith("at-") and t.refresh
    polls = [r for r in vendor.token_requests if r["grant_type"].endswith("device_code")]
    assert len(polls) == 3
    assert all(r["client_id"] == FAKE_CLIENT_ID for r in polls)


def test_device_flow_honours_slow_down(vendor):
    vendor.device_slow_down = True
    flow = _device(vendor)
    flow.start()
    before = flow.interval
    assert flow.result(timeout=10).access
    assert flow.interval == pytest.approx(before + 0.05)


def test_device_flow_slow_down_without_interval_adds_five_seconds(monkeypatch):
    calls = []

    def handler(req):
        if req.url.path == "/device":
            return httpx.Response(200, json={"device_code": "d", "user_code": "U",
                                             "verification_uri": "https://v/d", "interval": 0})
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(200, json={"access_token": "a"})

    monkeypatch.setattr(oauth, "SLOW_DOWN_STEP", 0.01)
    flow = DeviceFlow(device_url="https://v/device", token_url="https://v/token", client_id="c",
                      http=httpx.Client(transport=httpx.MockTransport(handler)))
    flow.start()
    assert flow.result(timeout=5).access == "a" and flow.interval == pytest.approx(0.01)


@pytest.mark.parametrize("outcome, code", [("deny", "access_denied"), ("expire", "expired_token")])
def test_device_flow_terminal_errors(vendor, outcome, code):
    vendor.device_outcome = outcome
    flow = _device(vendor)
    flow.start()
    with pytest.raises(FlowError) as e:
        flow.result(timeout=10)
    assert e.value.code == code


def test_device_flow_cancel(vendor):
    vendor.device_interval = 5
    flow = _device(vendor)
    flow.start()
    threading.Timer(0.1, flow.cancel).start()
    t0 = time.time()
    with pytest.raises(FlowCancelled):
        flow.result(timeout=30)
    assert time.time() - t0 < 2


def test_device_flow_timeout(vendor):
    vendor.device_interval = 0.05
    vendor.device_outcome = "approve"
    flow = _device(vendor)
    flow.start()
    with pytest.raises(FlowTimeout):
        flow.result(timeout=0.01)


def test_device_start_error_and_accept_header():
    seen = {}

    def handler(req):
        seen["accept"] = req.headers.get("accept")
        return httpx.Response(400, json={"error": "invalid_client"})

    flow = DeviceFlow(device_url="https://v/device", token_url="https://v/token", client_id="c",
                      http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(FlowError) as e:
        flow.start()
    assert e.value.code == "invalid_client" and seen["accept"] == "application/json"


# ---------------------------------------------------- identity / revoke ---

def test_slack_identity_is_posted_and_reads_user():
    seen = {}

    def handler(req):
        seen["method"], seen["auth"] = req.method, req.headers["authorization"]
        return httpx.Response(200, json={"ok": True, "user": "ada", "team": "Acme", "user_id": "U1"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert oauth.identity("https://slack.com/api/auth.test", "xoxp-1", http=http) == "ada"
    assert seen == {"method": "POST", "auth": "Bearer xoxp-1"}
    bad = _mock({"ok": False, "error": "invalid_auth"})
    assert oauth.identity("https://slack.com/api/auth.test", "x", http=bad) is None


def test_revoke_at_the_fake_and_never_raises(vendor):
    first = vendor.issue()
    assert oauth.revoke(vendor.base_url + "/revoke", first["access_token"], client_id=FAKE_CLIENT_ID)
    assert first["access_token"] in vendor.revoked
    assert oauth.identity(vendor.base_url + "/me", first["access_token"]) is None
    (dead,) = _free_ports(1)
    assert oauth.revoke(f"http://127.0.0.1:{dead}/revoke", "x") is False
    assert oauth.revoke("https://slack.com/api/auth.revoke", "x",
                        http=_mock({"ok": False, "error": "invalid_auth"})) is False


# ------------------------------------------------------ fake vendor MCP ---

def _rpc(vendor, method, params=None, *, id=1, token=None, session=None, sse=False):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}
    if id is not None:
        body["id"] = id
    return httpx.post(vendor.base_url + "/mcp" + ("?sse=1" if sse else ""), json=body,
                      headers=headers, timeout=5)


@pytest.mark.parametrize("sse", [False, True])
def test_fake_vendor_mcp_endpoint(vendor, sse):
    tok = vendor.issue()["access_token"]
    r = _rpc(vendor, "tools/list", token="wrong")
    assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]
    r = _rpc(vendor, "initialize", {"protocolVersion": "2025-06-18"}, token=tok, sse=sse)
    sid = r.headers["mcp-session-id"]

    def result(resp):
        if not sse:
            return resp.json()
        assert resp.headers["content-type"].startswith("text/event-stream")
        msgs = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
        assert msgs[0]["method"] == "notifications/message"
        return msgs[-1]

    assert result(r)["result"]["serverInfo"]["name"] == "fakevendor"
    assert _rpc(vendor, "tools/list", token=tok).status_code == 400  # no session
    assert _rpc(vendor, "notifications/initialized", id=None, token=tok, session=sid).status_code == 202
    tools = result(_rpc(vendor, "tools/list", id=2, token=tok, session=sid, sse=sse))["result"]["tools"]
    assert {t["name"]: t.get("annotations") for t in tools} == {
        "echo": {"readOnlyHint": True}, "write_note": None}
    echo = result(_rpc(vendor, "tools/call", {"name": "echo", "arguments": {"text": "hi"}}, id=3,
                       token=tok, session=sid, sse=sse))
    assert echo["id"] == 3 and echo["result"]["content"] == [{"type": "text", "text": "hi"}]
    note = result(_rpc(vendor, "tools/call", {"name": "write_note", "arguments": {"title": "t"}},
                       id=4, token=FAKE_API_KEY, session=sid, sse=sse))
    assert note["result"]["isError"] is False and vendor.notes == [{"title": "t"}]


def test_fake_vendor_runs_standalone():
    import subprocess

    (port,) = _free_ports(1)
    here = os.path.dirname(os.path.abspath(__file__))
    p = subprocess.Popen([sys.executable, os.path.join(here, "_fake_vendor.py"), "--port", str(port)],
                         stdout=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline().strip()
        assert line == f"http://127.0.0.1:{port}"
        r = httpx.get(line + "/.well-known/oauth-authorization-server", timeout=5)
        assert r.json()["code_challenge_methods_supported"] == ["S256"]
    finally:
        p.terminate()
        p.wait(5)
