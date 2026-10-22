"""`NoCacheAndLog`, the pure-ASGI replacement for the BaseHTTPMiddleware one."""
import asyncio
import logging
import os

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from fused_render import calls as shell_calls
from fused_render import netlog
from fused_render.server import create_app
from fused_render.server.common import NoCacheAndLog

IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.fixture
def fresh_net(monkeypatch):
    fresh = netlog.NetLog()
    monkeypatch.setattr(netlog, "_log", fresh)
    return fresh


def _scope(path="/api/x", method="GET", query=b"", headers=()):
    return {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": query, "headers": list(headers), "client": ("1.2.3.4", 5),
        "server": ("127.0.0.1", 1777), "scheme": "http", "root_path": "",
    }


async def _drive(app, scope, receive=None):
    sent = []

    async def send(m):
        sent.append(m)

    async def rcv():
        return {"type": "http.request", "body": b"", "more_body": False}

    await NoCacheAndLog(app)(scope, receive or rcv, send)
    return sent


def _hdr(msg, name):
    return dict(msg["headers"]).get(name.encode())


def _app_sending(status=200, headers=(), chunks=(b"ok",)):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": list(headers)})
        for i, c in enumerate(chunks):
            await send({"type": "http.response.body", "body": c, "more_body": i < len(chunks) - 1})
    return app


def test_websocket_scope_passes_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(NoCacheAndLog(app)({"type": "websocket"}, None, None))
    assert seen == ["websocket"]


def test_api_route_gets_no_cache(fresh_net):
    sent = asyncio.run(_drive(_app_sending(), _scope()))
    assert _hdr(sent[0], "cache-control") == b"no-cache"


def test_immutable_only_for_200_and_304_on_hashed_assets(fresh_net):
    p = "/static/shell-dist/assets/x.js"
    for status, want in ((200, IMMUTABLE), (304, IMMUTABLE), (404, "no-cache"), (500, "no-cache")):
        sent = asyncio.run(_drive(_app_sending(status), _scope(p)))
        assert _hdr(sent[0], "cache-control").decode() == want, status


def test_marketplace_asset_keeps_own_cache_control(fresh_net):
    p = "/api/marketplace/asset/a/icon.svg"
    own = [(b"cache-control", b"public, max-age=3600")]
    sent = asyncio.run(_drive(_app_sending(200, own), _scope(p)))
    assert _hdr(sent[0], "cache-control") == b"public, max-age=3600"
    sent = asyncio.run(_drive(_app_sending(404, own), _scope(p)))
    assert _hdr(sent[0], "cache-control") == b"no-cache"


def test_content_versioned_url_is_immutable_and_wrong_token_is_not(tmp_path):
    from fused_render.server.asset_version import asset_version

    client = TestClient(create_app(start_dir=str(tmp_path)))
    p = "/static/runtime.js"
    assert client.get(p, params={"v": asset_version(p)}).headers["cache-control"] == IMMUTABLE
    assert client.get(p, params={"v": "nope"}).headers["cache-control"] == "no-cache"
    assert client.get(p).headers["cache-control"] == "no-cache"


def test_skip_prefixes_produce_no_access_line(caplog, fresh_net):
    caplog.set_level(logging.INFO, logger="fused_render.server.common")
    asyncio.run(_drive(_app_sending(), _scope("/static/a.js")))
    asyncio.run(_drive(_app_sending(), _scope("/template-assets/a.js")))
    assert not [r for r in caplog.records if "->" in r.getMessage()]
    asyncio.run(_drive(_app_sending(), _scope("/api/x")))
    assert any("GET /api/x -> 200" in r.getMessage() for r in caplog.records)


def test_streamed_response_stays_in_flight_until_last_chunk(fresh_net):
    checks = {}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ab", "more_body": True})
        checks["mid"] = list(fresh_net._open.values())
        await send({"type": "http.response.body", "body": b"c", "more_body": False})
        checks["end"] = list(fresh_net._open.values())

    asyncio.run(_drive(app, _scope()))
    assert len(checks["mid"]) == 1 and checks["mid"][0]["status"] == 200
    assert checks["end"] == []
    assert _last(fresh_net)["outcome"] == "ok" and _last(fresh_net)["bytes"] == 3


def _last(fresh_net):
    with fresh_net._lock:
        return list(fresh_net._finished)[-1]


def test_stream_abandoned_midway_is_disconnected(fresh_net):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ab", "more_body": True})
        # starlette's disconnect listener ends the call quietly: no last chunk

    asyncio.run(_drive(app, _scope()))
    assert _last(fresh_net)["outcome"] == "disconnected"


def test_send_failure_mid_stream_is_disconnected(fresh_net):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise ClientDisconnect()

    with pytest.raises(ClientDisconnect):
        asyncio.run(_drive(app, _scope()))
    assert _last(fresh_net)["outcome"] == "disconnected"


def test_cancel_before_headers_is_disconnected(fresh_net):
    async def app(scope, receive, send):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_drive(app, _scope()))
    assert _last(fresh_net)["outcome"] == "disconnected"
    assert fresh_net._open == {}


def test_fused_call_visible_to_route_and_receive_untouched(fresh_net):
    got = {}

    async def app(scope, receive, send):
        got["call"] = scope["state"]["fused_call"]
        got["rcv"] = receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def rcv():
        return {"type": "http.request"}

    asyncio.run(_drive(app, _scope(), rcv))
    assert got["rcv"] is rcv and got["call"] is None


def test_unhandled_500_echoes_err_id_matching_call_log(tmp_path, monkeypatch, fresh_net):
    finished = []
    real = shell_calls.finish
    monkeypatch.setattr(
        shell_calls, "finish", lambda call, **kw: (finished.append(kw), real(call, **kw))[1]
    )
    app = create_app(start_dir=str(tmp_path))

    @app.get("/api/_boom")
    def boom():
        raise RuntimeError("kaboom")

    @app.get("/api/_state")
    def state(request: __import__("fastapi").Request):
        return {"has": hasattr(request.state, "fused_call")}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/_state").json() == {"has": True}
    r = client.get("/api/_boom", headers={"X-Fused-Page": str(tmp_path / "p.html")})
    assert r.status_code == 500
    assert _last(fresh_net)["outcome"] == "error"
    import re

    m = re.search(r"err_id ([0-9a-f]{12})", r.text)
    assert m
    errs = [kw for kw in finished if kw.get("err_id")]
    assert errs and errs[-1]["err_id"] == m.group(1)


def test_cors_preflight_and_lan_still_behave(tmp_path):
    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.options("/api/health", headers={"Origin": "http://evil.example",
                                               "Access-Control-Request-Method": "GET"})
    assert r.status_code in (200, 400, 403, 404, 405)
    assert client.get("/api/health").status_code == 200
