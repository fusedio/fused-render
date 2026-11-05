"""The local-network listener after the simplification (docs/LAN_SIMPLIFICATION.md):
one self-signed certificate pinned by fingerprint, the computer's own local
name in the pairing code, a Host allowlist of that name and the computer's
addresses, and the computer-side reachability warnings (lan_doctor.py).
No mDNS, no watcher, no http listener, no CA — the tests pin that down too."""
import asyncio
import hashlib
import json

import pytest

from fused_render import lan, lan_doctor, lan_tls


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    lan_tls._cert_by_dir.clear()
    lan_doctor.reset_cache()
    monkeypatch.setattr(lan._controller, "hostname", "studio.local")
    monkeypatch.setattr(lan._controller, "ips", ["192.168.1.20"])
    return tmp_path


def call(app, path, headers=(), host=b"studio.local:1889", query=b""):
    scope = {"type": "http", "path": path, "method": "GET", "query_string": query, "scheme": "https",
             "headers": [(b"host", host), *headers]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    headers_out = next(m["headers"] for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, data, headers_out


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})


# ---- the certificate ---------------------------------------------------------


def test_certificate_is_stable_across_restarts_and_fingerprint_matches_the_file(home):
    fp1 = lan_tls.fingerprint()
    lan_tls._cert_by_dir.clear()  # a new process
    fp2 = lan_tls.fingerprint()
    assert fp1 == fp2 and len(fp1) == 64
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    with open(lan_tls.cert_path(), "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    assert hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest() == fp1
    # Self-signed, not a CA, no address or name baked in: nothing to reissue
    # when the computer moves.
    assert cert.issuer == cert.subject
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    with pytest.raises(x509.ExtensionNotFound):
        cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)


def test_a_half_present_certificate_is_replaced(home):
    fp1 = lan_tls.fingerprint()
    import os

    os.unlink(lan_tls.key_path())
    lan_tls._cert_by_dir.clear()
    assert lan_tls.fingerprint() != fp1


# ---- the host allowlist --------------------------------------------------------


def test_host_allowlist_admits_the_local_name_and_own_addresses_only(home):
    app = lan.LanApp(_inner)
    for host in (b"studio.local", b"studio.local:1889", b"STUDIO.LOCAL.", b"studio", b"192.168.1.20:1889"):
        status, body, _ = call(app, "/", host=host)
        assert status == 403 and b"not paired" in body, host  # reached the pairing page: admitted
    for host in (b"render.fused.local", b"evil.example", b"10.0.0.9", b""):
        assert call(app, "/", host=host)[0] == 404, host


# ---- pairing ---------------------------------------------------------------------


def test_pair_sets_a_secure_cookie_and_records_the_hostname(home):
    app = lan.LanApp(_inner)
    token = lan.mint_pair_token()
    status, _, headers = call(app, "/pair", query=f"t={token}".encode(),
                              headers=[(b"x-fused-device-name", b"Vas iPhone")])
    assert status == 302
    cookie = next(v.decode() for k, v in headers if k == b"set-cookie")
    assert cookie.startswith(lan.COOKIE + "=") and "Secure" in cookie and "HttpOnly" in cookie
    devices = lan._read_devices()
    assert devices[0]["name"] == "Vas iPhone" and devices[0]["hostname"] == lan_doctor.local_hostname()
    # Spent: the same code pairs nothing else.
    status, body, _ = call(app, "/pair", query=f"t={token}".encode())
    assert status == 403 and b"expired" in body
    # The cookie works whatever host it arrives under (the app re-sends it to
    # the fallback address): only the value is checked.
    secret = cookie.split(";")[0].split("=", 1)[1]
    status, body, _ = call(app, "/api/lan/apps", headers=[(b"cookie", f"{lan.COOKIE}={secret}".encode())],
                           host=b"192.168.1.20:1889")
    assert status == 200 and json.loads(body)["host"] == "studio.local"


def test_removed_doors_are_gone(home):
    """No CA to fetch, no trust-on-first-use, no browser runtime shim."""
    app = lan.LanApp(_inner)
    secret, _ = lan._pair_device("", "phone")
    cookie = (b"cookie", f"{lan.COOKIE}={secret}".encode())
    for path in ("/lan/ca.pem", "/api/lan/tls"):
        assert call(app, path, headers=[cookie])[0] == 404, path
    assert not hasattr(lan, "_phone_runtime")
    assert not hasattr(lan, "HOSTNAME")
    assert not hasattr(lan._controller, "_zeroconf")
    assert not hasattr(lan._controller, "_watch")


# ---- the pairing code ------------------------------------------------------------


def test_pair_token_names_the_local_name_and_carries_fingerprint_and_ip(home, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = FastAPI()
    api.include_router(lan.router)
    client = TestClient(api)
    # Nothing listening: no code, with the reason.
    monkeypatch.setattr(lan._controller, "_thread", None)
    monkeypatch.setattr(lan._controller, "error", "no free port among 1889")
    r = client.get("/api/lan/pair-token")
    assert r.status_code == 503 and "no free port" in r.json()["error"]

    class Alive:
        def is_alive(self):
            return True

    monkeypatch.setattr(lan._controller, "_thread", Alive())
    monkeypatch.setattr(lan._controller, "port", 1889)
    monkeypatch.setattr(lan_doctor, "best_ip", lambda: "192.168.1.20")
    r = client.get("/api/lan/pair-token")
    assert r.status_code == 200, r.text
    body = r.json()
    fp = lan_tls.fingerprint()
    assert body["url"].startswith("https://studio.local:1889/pair?t=")
    assert f"fp={fp}" in body["url"] and "ip=192.168.1.20" in body["url"]
    assert body["fingerprint"] == fp and body["hostname"] == "studio.local" and body["ip"] == "192.168.1.20"
    # No OS name: the address is the host, and `ip` is not repeated.
    monkeypatch.setattr(lan._controller, "hostname", None)
    body = client.get("/api/lan/pair-token").json()
    assert body["url"].startswith("https://192.168.1.20:1889/pair?t=") and "ip=" not in body["url"]


def test_status_carries_warnings_and_never_raises(home, monkeypatch):
    monkeypatch.setattr(lan._controller, "_thread", None)
    status = lan.status()
    assert status["running"] is False and status["warnings"] == [] and status["fingerprint"] is None
    assert set(status) == {"running", "url", "hostname", "ip", "port", "error", "fingerprint", "devices", "warnings"}


# ---- the doctor --------------------------------------------------------------------


def test_doctor_is_quiet_while_not_running():
    assert lan_doctor.warnings(running=False, port=None, ip=None, fingerprint=None, hostname=None) == []


def test_doctor_names_each_cause(monkeypatch):
    monkeypatch.setattr(lan_doctor, "interfaces", lambda: [("utun3", "10.8.0.2")])
    monkeypatch.setattr(lan_doctor, "default_route_interface", lambda: "utun3")
    monkeypatch.setattr(lan_doctor, "firewall", lambda port=None: {"kind": "macos", "on": True, "block_all": False, "stealth": True, "blocked": False})
    monkeypatch.setattr(lan_doctor, "hostname_resolves", lambda name: False)
    monkeypatch.setattr(lan_doctor, "self_connect", lambda ip, port, fp: False)
    out = lan_doctor.warnings(running=True, port=1889, ip="10.8.0.2", fingerprint="ab", hostname="studio.local",
                              devices=[{"hostname": "old-name.local"}, {"hostname": "studio.local"}])
    assert [w["code"] for w in out] == ["no_lan", "vpn", "firewall", "hostname", "listener", "renamed"]
    assert "old-name.local" in out[-1]["message"]


def test_doctor_is_silent_when_everything_checks_out(monkeypatch):
    monkeypatch.setattr(lan_doctor, "interfaces", lambda: [("en0", "192.168.1.20"), ("utun3", "10.8.0.2")])
    monkeypatch.setattr(lan_doctor, "default_route_interface", lambda: "en0")
    monkeypatch.setattr(lan_doctor, "firewall", lambda port=None: {"kind": "macos", "on": True, "block_all": False, "stealth": False, "blocked": False})
    monkeypatch.setattr(lan_doctor, "hostname_resolves", lambda name: True)
    monkeypatch.setattr(lan_doctor, "self_connect", lambda ip, port, fp: True)
    assert lan_doctor.warnings(running=True, port=1889, ip="192.168.1.20", fingerprint="ab",
                               hostname="studio.local", devices=[{"hostname": "studio.local"}]) == []


def test_best_ip_skips_tunnels(monkeypatch):
    monkeypatch.setattr(lan_doctor, "interfaces", lambda: [("utun3", "10.8.0.2"), ("en0", "192.168.1.20")])
    monkeypatch.setattr(lan_doctor, "lan_ip", lambda: "10.8.0.2")  # the VPN owns the default route
    assert lan_doctor.best_ip() == "192.168.1.20"
    assert lan_doctor.lan_ips() == ["10.8.0.2", "192.168.1.20"]


def test_self_connect_pins_the_fingerprint(home):
    """A real handshake against the listener's own certificate: the doctor's
    check is the same check the phone makes."""
    import socket
    import threading

    lan_tls.ensure_cert()
    ctx = lan_tls.server_ssl_context(lan_tls.cert_path(), lan_tls.key_path())
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(2)
    port = lsock.getsockname()[1]

    def serve():
        for _ in range(2):
            conn, _ = lsock.accept()
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=serve, daemon=True).start()
    assert lan_doctor._self_connect("127.0.0.1", port, lan_tls.fingerprint(), 5.0) is True
    assert lan_doctor._self_connect("127.0.0.1", port, "00" * 32, 5.0) is False
    lsock.close()


def test_macos_firewall_listapps_numbered_entries(monkeypatch):
    """`socketfilterfw --listapps` numbers its entries; the running executable
    inside a blocked .app must still be matched."""
    exe = "/Applications/Fused Render.app/Contents/MacOS/python"
    answers = {
        "--getglobalstate": "Firewall is enabled. (State = 1)\n",
        "--getblockall": "Block all DISABLED!\n",
        "--getstealthmode": "Stealth mode disabled\n",
        "--listapps": "Total number of apps = 2 \n\n1 :  /Applications/Zoom.app \n \t ( Allow incoming connections )\n"
                      "2 :  /Applications/Fused Render.app \n \t ( Block incoming connections )\n",
    }
    monkeypatch.setattr(lan_doctor.sys, "platform", "darwin")
    monkeypatch.setattr(lan_doctor, "_is_mac", lambda: True)
    monkeypatch.setattr(lan_doctor.os.path, "exists", lambda p: True)
    monkeypatch.setattr(lan_doctor, "_run", lambda args, timeout=2.0: answers.get(args[-1]))
    monkeypatch.setattr(lan_doctor.sys, "executable", exe)
    monkeypatch.setattr(lan_doctor.os.path, "realpath", lambda p: p)
    fw = lan_doctor._firewall(1889)
    assert fw == {"kind": "macos", "on": True, "block_all": False, "stealth": False, "blocked": True}


def test_linux_firewall_warns_only_when_active_without_a_rule(monkeypatch):
    monkeypatch.setattr(lan_doctor.sys, "platform", "linux")
    monkeypatch.setattr(lan_doctor, "_run", lambda args, timeout=2.0:
                        "Status: active\n\nTo  Action  From\n22/tcp  ALLOW  Anywhere\n" if args[0] == "ufw" else None)
    assert lan_doctor._firewall(1889)["blocked"] is True
    monkeypatch.setattr(lan_doctor, "_run", lambda args, timeout=2.0:
                        "Status: active\n1889/tcp  ALLOW  Anywhere\n" if args[0] == "ufw" else None)
    assert lan_doctor._firewall(1889)["blocked"] is False
    # Not readable (needs root): unknown, never a warning.
    monkeypatch.setattr(lan_doctor, "_run", lambda args, timeout=2.0: None)
    assert lan_doctor._firewall(1889) is None
    out = lan_doctor.warnings(running=True, port=1889, ip="192.168.1.20", fingerprint="ab", hostname=None)
    assert "firewall" not in [w["code"] for w in out]


def test_status_admits_a_new_address_on_a_running_listener(home, monkeypatch):
    """A new DHCP lease must be admitted by the Host allowlist without a
    restart: status() refreshes the list, and _host_ok also reads live."""
    class Alive:
        def is_alive(self):
            return True

    monkeypatch.setattr(lan._controller, "_thread", Alive())
    monkeypatch.setattr(lan._controller, "port", 1889)
    monkeypatch.setattr(lan_doctor, "lan_ips", lambda: ["192.168.1.99"])
    monkeypatch.setattr(lan_doctor, "best_ip", lambda: "192.168.1.99")
    monkeypatch.setattr(lan_doctor, "warnings", lambda **kw: [])
    monkeypatch.setattr(lan._controller, "error", "no network address found")
    status = lan.status()
    assert lan._controller.ips == ["192.168.1.99"] and status["error"] is None
    assert call(lan.LanApp(_inner), "/", host=b"192.168.1.99:1889")[0] == 403
    # Even before a prefs read refreshes the list, the live read admits it.
    monkeypatch.setattr(lan._controller, "ips", ["10.0.0.5"])
    assert call(lan.LanApp(_inner), "/", host=b"192.168.1.99:1889")[0] == 403


def test_best_ip_skips_virtual_adapters(monkeypatch):
    monkeypatch.setattr(lan_doctor, "interfaces", lambda: [("docker0", "172.17.0.1"), ("vboxnet0", "192.168.56.1"),
                                                           ("bridge100", "192.168.2.1"), ("en0", "192.168.1.20")])
    monkeypatch.setattr(lan_doctor, "lan_ip", lambda: "172.17.0.1")
    assert lan_doctor.best_ip() == "192.168.2.1"  # Internet Sharing's bridge counts; Docker's does not


def test_a_mismatched_key_pair_is_replaced(home):
    """A new server.pem beside an older server.key would fail every handshake."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    fp1 = lan_tls.fingerprint()
    other = ec.generate_private_key(ec.SECP256R1())
    with open(lan_tls.key_path(), "wb") as f:
        f.write(other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    lan_tls._cert_by_dir.clear()
    assert lan_tls.fingerprint() != fp1
    lan_tls.server_ssl_context(lan_tls.cert_path(), lan_tls.key_path())  # the new pair loads
