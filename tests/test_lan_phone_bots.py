"""The LAN listener's phone bots door (fused_render/lan.py, docs/bots.md §14):
`/bots` and `/bots/<id>` serve the lan page for a paired device, the
`/api/phone/bots` prefix is forwarded with `X-Fused-Device` stamped from the
pairing record (a client-sent one dropped), and the desktop's `/api/bots`
stays a 404. Driven through `LanApp.__call__` with a recording inner app."""
import asyncio
import json

import pytest

from fused_render import lan


class Inner:
    """Records the scope it was handed and answers 200 with the headers it saw."""

    def __init__(self):
        self.scopes = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope)
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        out = json.dumps({"headers": [[k.decode(), v.decode()] for k, v in scope["headers"]],
                          "body": body.decode()}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": out})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    static = tmp_path / "static"
    (static / "lan").mkdir(parents=True)
    (static / "shell-dist").mkdir()
    (static / "shell-dist" / "lan.html").write_text("<!doctype html><title>lan page</title>")
    monkeypatch.setattr(lan, "_STATIC_DIR", str(static / "lan"))
    return tmp_path


def call(app, path, method="GET", headers=(), body=b"", query=b""):
    scope = {"type": "http", "path": path, "method": method, "query_string": query, "scheme": "http",
             "headers": [(b"host", lan.HOSTNAME.encode()), *headers]}
    sent = []
    chunks = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if chunks:
            return chunks.pop(0)
        await asyncio.sleep(3600)  # the client stays connected (FileResponse listens for a disconnect)

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, data, scope


def paired_cookie():
    secret, record = lan._pair_device("Mozilla/5.0 (iPhone) FusedRender-iOS/1.0")
    return (b"cookie", f"{lan.COOKIE}={secret}".encode()), record


def test_unpaired_is_refused(home):
    inner = Inner()
    app = lan.LanApp(inner)
    for path in ("/api/phone/bots", "/api/phone/bots/abc/send"):
        status, body, _ = call(app, path)
        assert status == 401 and json.loads(body) == {"error": "not paired"}
    status, body, _ = call(app, "/bots")  # a page: the how-to-pair page, as at /
    assert status == 403 and b"Scan to pair" in body
    # A cookie for a device that was never paired (or was revoked) is no better.
    status, _, _ = call(app, "/api/phone/bots", headers=[(b"cookie", f"{lan.COOKIE}=nope".encode())])
    assert status == 401
    assert inner.scopes == []


def test_paired_bots_pages_serve_the_lan_page(home):
    cookie, _ = paired_cookie()
    app = lan.LanApp(Inner())
    for path in ("/bots", "/bots/6a96b827"):
        status, body, _ = call(app, path, headers=[cookie])
        assert status == 200 and b"lan page" in body, path
    assert call(app, "/bots/a/b", headers=[cookie])[0] == 404  # one segment only
    assert call(app, "/bots", method="POST", headers=[cookie])[0] == 404


def test_paired_bots_page_says_when_not_built(home):
    cookie, _ = paired_cookie()
    (home / "static" / "shell-dist" / "lan.html").unlink()
    status, body, _ = call(lan.LanApp(Inner()), "/bots", headers=[cookie])
    assert status == 503 and b"not built" in body


def test_phone_bots_forwarded_with_the_device_from_the_record(home):
    cookie, record = paired_cookie()
    inner = Inner()
    app = lan.LanApp(inner)
    spoof = (b"x-fused-device", b"someone-else")
    for method, path in (("GET", "/api/phone/bots"), ("GET", "/api/phone/bots/b1?since=3"),
                         ("POST", "/api/phone/bots/b1/send"), ("DELETE", "/api/phone/bots/b1")):
        status, body, scope = call(app, path, method=method, body=b'{"text":"hi"}' if method == "POST" else b"",
                                   headers=[cookie, spoof, (b"X-Fused-Device", b"also-spoofed"), (b"x-fused", b"1")])
        assert status == 200, (method, path)
        seen = json.loads(body)
        devices = [v for k, v in seen["headers"] if k.lower() == "x-fused-device"]
        assert devices == [record["id"]], devices
        assert ["x-fused", "1"] in seen["headers"]
        if method == "POST":
            assert seen["body"] == '{"text":"hi"}'
        # The caller's scope is left as it arrived (the forward works on a copy).
        assert spoof in scope["headers"]
    assert len(inner.scopes) == 4
    # Other methods are not part of the door.
    assert call(app, "/api/phone/bots/b1", method="PUT", headers=[cookie])[0] == 404


def test_desktop_bots_api_stays_closed(home):
    cookie, _ = paired_cookie()
    inner = Inner()
    app = lan.LanApp(inner)
    for method, path in (("GET", "/api/bots"), ("POST", "/api/bots/b1/send"), ("POST", "/api/bots/b1/takeover"),
                         ("GET", "/api/bots/roster"), ("GET", "/api/bot-apps"), ("GET", "/api/phone/botsx"),
                         ("GET", "/api/phone")):
        assert call(app, path, method=method, headers=[cookie])[0] == 404, (method, path)
    assert inner.scopes == []


def test_paired_record_bumps_last_seen(home, monkeypatch):
    cookie, record = paired_cookie()
    devices = lan._read_devices()
    devices[0]["last_seen"] = 0
    lan._write_devices(devices)
    scope = {"headers": [cookie]}
    got = lan._paired_record(scope)
    assert got["id"] == record["id"] and lan._paired(scope) is True
    assert lan._read_devices()[0]["last_seen"] > 0
    assert lan._paired_record({"headers": []}) is None and lan._paired({"headers": []}) is False


def test_dot_segments_never_forward(home):
    # The inner app forwards the owned prefix to the bots' owner with httpx, which resolves `..`: the door must
    # not become `/api/bots/<id>/takeover` (bots/routes.forward_middleware refuses them as well).
    cookie, _ = paired_cookie()
    inner = Inner()
    app = lan.LanApp(inner)
    for method, path in (("POST", "/api/phone/bots/../../bots/b1/takeover"), ("GET", "/api/phone/bots/.."),
                         ("POST", "/api/phone/bots/b1/./send"), ("DELETE", "/api/phone/bots/../bots/b1")):
        assert call(app, path, method=method, headers=[cookie, (b"x-fused", b"1")])[0] == 404, (method, path)
    assert inner.scopes == []
    # `/bots/..` is only ever the lan page itself.
    status, body, _ = call(app, "/bots/..", headers=[cookie])
    assert status == 200 and b"lan page" in body
