"""Away access on the phone listener (lan.py + tunnel/host.py,
docs/REMOTE_ACCESS.md): how a request is classified (lan / tailscale /
relay) from its transport, what an away request may and may not do, and the
routes a phone is told about."""
import asyncio
import json

import pytest

from fused_render import lan, lan_doctor, lan_tls
from fused_render.tunnel import host as tunnel_host
from fused_render.tunnel import tailscale

RELAY_HOST = "a" * 26 + ".relay.test"
TS_HOST = "studio.tail1234.ts.net"
TS_IP = "100.101.1.2"
PHONE_IP = "203.0.113.9"
RELAY_LEG_PORT = 50123


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    monkeypatch.delenv("FUSED_RENDER_RELAY", raising=False)
    lan_tls._cert_by_dir.clear()
    lan_doctor.reset_cache()
    monkeypatch.setattr(lan._controller, "hostname", "studio.local")
    monkeypatch.setattr(lan._controller, "ips", ["192.168.1.20"])
    monkeypatch.setattr(lan_doctor, "lan_ips", lambda: ["192.168.1.20"])
    monkeypatch.setattr(lan_doctor, "best_ip", lambda: "192.168.1.20")
    return tmp_path


@pytest.fixture
def away(home, monkeypatch):
    """Away access ON, with a connected relay and a tailnet (both faked at
    the tunnel/host.py seam: the real agent is exercised in test_tunnel.py)."""
    r = tunnel_host.remote
    monkeypatch.setattr(r, "enabled", True)
    monkeypatch.setattr(r, "relay_endpoint", lambda: (RELAY_HOST, 443))
    monkeypatch.setattr(r, "tailnet", lambda: {"host": TS_HOST, "ips": [TS_IP]})
    monkeypatch.setattr(r, "peer_for", lambda port: PHONE_IP if port == RELAY_LEG_PORT else None)
    return r


def call(app, path, *, host, client, headers=(), query=b"", kind="http"):
    scope = {"type": kind, "path": path, "method": "GET", "query_string": query, "scheme": "https",
             "headers": [(b"host", host.encode()), *headers], "client": client}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    if kind == "websocket":
        return sent
    start = next(m for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], data, start["headers"]


async def _echo(scope, receive, send):
    """The inner app, reporting what it was handed."""
    body = json.dumps({"client": list(scope.get("client") or []), "via": scope.get("fused.via")}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def _cookie(name="phone"):
    secret, record = lan._pair_device("", name)
    return (b"cookie", f"{lan.COOKIE}={secret}".encode()), record


RELAY = dict(host=RELAY_HOST, client=("127.0.0.1", RELAY_LEG_PORT))
TAILNET = dict(host=TS_HOST + ":1889", client=(TS_IP, 40000))
WIFI = dict(host="studio.local:1889", client=("192.168.1.30", 40000))


# ---- classification ----------------------------------------------------------------


def test_classification_comes_from_the_transport_not_the_client(away):
    assert lan._via({"client": ("127.0.0.1", RELAY_LEG_PORT)}) == ("relay", PHONE_IP)
    # Loopback the agent did NOT open is the computer itself: never "relay".
    assert lan._via({"client": ("127.0.0.1", 1)}) == ("lan", "127.0.0.1")
    assert lan._via({"client": (TS_IP, 1)}) == ("tailscale", TS_IP)
    assert lan._via({"client": ("192.168.1.30", 1)}) == ("lan", "192.168.1.30")
    # Headers cannot claim a route.
    assert lan._via({"client": ("192.168.1.30", 1), "headers": [(b"x-forwarded-for", PHONE_IP.encode())]})[0] == "lan"


def test_a_lan_numbered_from_the_tailnet_range_stays_the_wifi(away, monkeypatch):
    """Some routers hand out 100.64/10 addresses on the LOCAL network: with
    the computer on such a LAN, a phone beside it is `lan`, not `tailscale`."""
    monkeypatch.setattr(lan_doctor, "lan_ips", lambda: ["100.70.3.10"])
    assert lan._via({"client": ("100.70.3.44", 1)})[0] == "lan"
    assert lan._via({"client": (TS_IP, 1)})[0] == "tailscale"


def test_stopping_away_access_clears_its_switch(home):
    r = tunnel_host._Remote()
    r.apply(True, "", lambda: None)
    assert r.enabled
    r.stop()
    assert not r.enabled and r.hosts() == set() and r.routes() == []


def test_a_relayed_request_reaches_the_inner_app_as_the_phone_never_as_loopback(away):
    cookie, _ = _cookie()
    # A forwarded prefix route shows the inner app's view of the request.
    status, body, _ = call(lan.LanApp(_echo), "/static/x.js", headers=[cookie], **RELAY)
    assert status == 200
    seen = json.loads(body)
    assert seen == {"client": [PHONE_IP, 0], "via": "relay"}


def test_away_routes_are_closed_while_away_access_is_off(home, monkeypatch):
    monkeypatch.setattr(tunnel_host.remote, "enabled", False)
    monkeypatch.setattr(tunnel_host.remote, "peer_for", lambda port: PHONE_IP)
    cookie, _ = _cookie()
    app = lan.LanApp(_echo)
    assert call(app, "/static/x.js", headers=[cookie], host="studio.local", client=("127.0.0.1", 5))[0] == 404
    assert call(app, "/static/x.js", headers=[cookie], host="studio.local", client=(TS_IP, 5))[0] == 404
    sent = call(app, "/api/fs/events", headers=[cookie], host="studio.local", client=(TS_IP, 5), kind="websocket")
    assert sent == [{"type": "websocket.close", "code": 1008}]
    # The Wi-Fi is untouched.
    assert call(app, "/static/x.js", headers=[cookie], **WIFI)[0] == 200


def test_host_allowlist_admits_away_names_only_while_on(home, away, monkeypatch):
    cookie, _ = _cookie()
    app = lan.LanApp(_echo)
    for host in (RELAY_HOST, RELAY_HOST + ":443", TS_HOST, TS_IP):
        assert call(app, "/static/x.js", headers=[cookie], host=host, client=("192.168.1.30", 1))[0] == 200, host
    monkeypatch.setattr(tunnel_host.remote, "enabled", False)
    for host in (RELAY_HOST, TS_HOST, TS_IP):
        assert call(app, "/static/x.js", headers=[cookie], host=host, client=("192.168.1.30", 1))[0] == 404, host
    # Never another relay name, even while on.
    monkeypatch.setattr(tunnel_host.remote, "enabled", True)
    other = "b" * 26 + ".relay.test"
    assert call(app, "/static/x.js", headers=[cookie], host=other, client=("192.168.1.30", 1))[0] == 404


# ---- what away requests may not do ----------------------------------------------------


@pytest.mark.parametrize("route", [RELAY, TAILNET])
def test_pairing_is_refused_away_and_the_code_is_not_spent(away, route):
    token = lan.mint_pair_token()
    status, body, headers = call(lan.LanApp(_echo), "/pair", query=f"t={token}".encode(), **route)
    assert status == 403 and b"same Wi-Fi" in body
    assert not any(k == b"set-cookie" for k, _ in headers)
    assert lan._read_devices() == []
    # Still good on the Wi-Fi.
    status, _, headers = call(lan.LanApp(_echo), "/pair", query=f"t={token}".encode(), **WIFI)
    assert status == 302 and any(k == b"set-cookie" for k, _ in headers)


@pytest.mark.parametrize("route", [RELAY, TAILNET])
def test_unpaired_away_requests_get_401(away, route):
    assert call(lan.LanApp(_echo), "/static/x.js", **route)[0] == 401


def test_a_phone_limited_to_the_wifi_is_refused_away(away):
    cookie, record = _cookie()
    assert lan.set_device_remote(record["id"], False)
    app = lan.LanApp(_echo)
    assert call(app, "/static/x.js", headers=[cookie], **RELAY)[0] == 403
    assert call(app, "/static/x.js", headers=[cookie], **TAILNET)[0] == 403
    status, body, _ = call(app, "/", headers=[cookie], **RELAY)
    assert status == 403 and b"Away access is off" in body
    sent = call(app, "/api/fs/events", headers=[cookie], query=b"path=/x", kind="websocket", **RELAY)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert call(app, "/static/x.js", headers=[cookie], **WIFI)[0] == 200
    assert lan.set_device_remote(record["id"], True)
    assert call(app, "/static/x.js", headers=[cookie], **RELAY)[0] == 200


def test_the_device_list_says_how_each_phone_last_came_in(away):
    cookie, record = _cookie()
    call(lan.LanApp(_echo), "/static/x.js", headers=[cookie], **RELAY)
    [dev] = lan.list_devices()
    assert dev["last_via"] == "relay" and dev["remote"] is True
    call(lan.LanApp(_echo), "/static/x.js", headers=[cookie], **WIFI)
    assert lan.list_devices()[0]["last_via"] == "lan"


# ---- routes and the pairing code ---------------------------------------------------------


def test_routes_list_wifi_then_tailnet_then_relay(away, monkeypatch):
    monkeypatch.setattr(type(lan._controller), "running", property(lambda self: True))
    monkeypatch.setattr(lan._controller, "port", 1889)
    monkeypatch.setattr(lan._controller, "fingerprint", lambda: "f" * 64)
    cookie, _ = _cookie()
    status, body, _ = call(lan.LanApp(_echo), "/api/lan/routes", headers=[cookie], **RELAY)
    assert status == 200
    got = json.loads(body)
    assert got["fingerprint"] == "f" * 64
    assert got["routes"] == [
        {"kind": "lan", "host": "studio.local", "port": 1889},
        {"kind": "lan", "host": "192.168.1.20", "port": 1889},
        {"kind": "tailscale", "host": TS_HOST, "port": 1889},
        {"kind": "relay", "host": RELAY_HOST, "port": 443},
    ]
    # Unpaired: nothing.
    assert call(lan.LanApp(_echo), "/api/lan/routes", **WIFI)[0] == 401
    # A phone limited to the Wi-Fi is not told the away routes.
    limited, record = _cookie("limited")
    lan.set_device_remote(record["id"], False)
    _, body, _ = call(lan.LanApp(_echo), "/api/lan/routes", headers=[limited], **WIFI)
    assert [r["kind"] for r in json.loads(body)["routes"]] == ["lan", "lan"]


def test_pair_code_carries_away_routes(away, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(lan._controller, "url", lambda: "https://studio.local:1889/")
    monkeypatch.setattr(lan._controller, "fingerprint", lambda: "f" * 64)
    app = FastAPI()
    app.include_router(lan.router)
    got = TestClient(app).get("/api/lan/pair-token").json()
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(got["url"]).query)
    assert q["r"] == [f"{RELAY_HOST}:443"] and q["ts"] == [TS_HOST]
    assert got["relay"] == f"{RELAY_HOST}:443" and got["tailscale"] == TS_HOST


def test_pair_code_has_no_away_routes_while_off(home, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(tunnel_host.remote, "enabled", False)
    monkeypatch.setattr(lan._controller, "url", lambda: "https://studio.local:1889/")
    monkeypatch.setattr(lan._controller, "fingerprint", lambda: "f" * 64)
    app = FastAPI()
    app.include_router(lan.router)
    url = TestClient(app).get("/api/lan/pair-token").json()["url"]
    assert "r=" not in url.split("?")[1].split("&")[0] and "&r=" not in url and "&ts=" not in url


def test_device_away_switch_endpoint_needs_the_fused_header(home):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _, record = _cookie()
    app = FastAPI()
    app.include_router(lan.router)
    client = TestClient(app)
    url = f"/api/lan/devices/{record['id']}"
    assert client.patch(url, json={"remote": False}).status_code == 403
    assert client.patch(url, json={"remote": "no"}, headers={"X-Fused": "1"}).status_code == 400
    assert client.patch("/api/lan/devices/nope", json={"remote": False}, headers={"X-Fused": "1"}).status_code == 404
    got = client.patch(url, json={"remote": False}, headers={"X-Fused": "1"}).json()
    assert got["devices"][0]["remote"] is False


def test_the_device_switch_is_not_reachable_from_the_phone(away):
    """/api/lan/* management stays loopback-only: the LAN wrapper never
    forwards it, so a phone cannot widen its own access."""
    cookie, record = _cookie()
    status, _, _ = call(lan.LanApp(_echo), f"/api/lan/devices/{record['id']}", headers=[cookie], **WIFI)
    assert status == 404


# ---- lifecycle and prefs -------------------------------------------------------------------


def test_turning_phone_access_off_stops_away_access(home, monkeypatch):
    stopped = []
    monkeypatch.setattr(tunnel_host.remote, "stop", lambda: stopped.append(True))
    lan.apply(False)
    assert stopped == [True]


def test_apply_remote_follows_both_switches(home, monkeypatch):
    calls = []
    monkeypatch.setattr(tunnel_host.remote, "apply", lambda want, setting, port: calls.append((want, setting)))
    from fused_render.shell import prefs

    monkeypatch.setattr(prefs, "lan_enabled", lambda: True)
    monkeypatch.setattr(prefs, "lan_remote_enabled", lambda: True)
    monkeypatch.setattr(prefs, "lan_relay", lambda: "relay.example.com:8443")
    lan.apply_remote()
    monkeypatch.setattr(prefs, "lan_enabled", lambda: False)
    lan.apply_remote()
    monkeypatch.setenv("FUSED_RENDER_RELAY", "env.example.com")
    monkeypatch.setattr(prefs, "lan_enabled", lambda: True)
    lan.apply_remote()
    assert calls == [(True, "relay.example.com:8443"), (False, "relay.example.com:8443"),
                     (True, "env.example.com")]


def test_remote_controller_reports_a_bad_setting_without_starting(home):
    r = tunnel_host._Remote()
    r.apply(True, "not a host!", lambda: None)
    assert r.error and "relay setting" in r.error
    assert r.status()["relay"] is None
    r.apply(False, "", lambda: None)
    assert r.error is None


def test_prefs_validate_the_relay_setting(home, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fused_render.shell import prefs

    monkeypatch.setattr(lan, "apply_remote", lambda: None)
    app = FastAPI()
    app.include_router(prefs.router)
    client = TestClient(app, headers={"X-Fused": "1"})
    assert client.put("/api/prefs", json={"lan_relay": "bad host!"}).status_code == 400
    assert client.put("/api/prefs", json={"lan_remote_enabled": "yes"}).status_code == 400
    assert client.put("/api/prefs", json={"lan_relay": "relay.example.com:9443", "lan_remote_enabled": True}).status_code == 200
    assert prefs.lan_relay() == "relay.example.com:9443" and prefs.lan_remote_enabled() is True
    snap = client.get("/api/prefs").json()["lan"]
    assert snap["remote_enabled"] is True and snap["relay_setting"] == "relay.example.com:9443"


# ---- tailscale ---------------------------------------------------------------------------------


def test_tailscale_status_parsing():
    assert tailscale.parse({"BackendState": "Stopped"}) is None
    assert tailscale.parse("junk") is None
    got = tailscale.parse({"BackendState": "Running",
                           "Self": {"DNSName": "Studio.tail1234.ts.net.", "TailscaleIPs": ["100.101.1.2", "fd7a:115c:a1e0::1", "8.8.8.8"]}})
    assert got == {"host": "studio.tail1234.ts.net", "ips": ["100.101.1.2", "fd7a:115c:a1e0::1"]}


def test_tailnet_address_ranges():
    assert tailscale.is_tailnet_ip("100.64.0.1") and tailscale.is_tailnet_ip("100.127.255.254")
    assert not tailscale.is_tailnet_ip("100.128.0.1") and not tailscale.is_tailnet_ip("192.168.1.1")
    assert tailscale.is_tailnet_ip("fd7a:115c:a1e0:ab12::1") and not tailscale.is_tailnet_ip("garbage")
