"""The request ring wired into the middleware, and /api/system/net/*."""
import json

import pytest
from fastapi.testclient import TestClient

from fused_render import netlog
from fused_render.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    fresh = netlog.NetLog()
    monkeypatch.setattr(netlog, "_log", fresh)
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_middleware_records_requests_with_bytes_and_source(client):
    r = client.get("/api/health", headers={"X-Fused-Page": "/monitor"})
    assert r.status_code == 200
    rows = client.get("/api/system/net/requests?own=1").json()["rows"]
    row = next(x for x in rows if x["route"] == "/api/health")
    assert row["status"] == 200 and row["outcome"] == "ok"
    assert row["source"] == "shell /monitor"
    assert row["bytes"] == len(r.content)
    assert row["held_ms"] is not None


def test_dashboard_polls_are_flagged_self_and_hidden_by_default(client):
    client.get("/api/system/net/summary")
    assert all(not x["self"] for x in client.get("/api/system/net/requests").json()["rows"])
    own = client.get("/api/system/net/requests?own=1").json()["rows"]
    assert any(x["self"] and x["route"] == "/api/system/net/summary" for x in own)


def test_404_and_error_status_recorded(client):
    client.get("/api/definitely-missing")
    rows = client.get("/api/system/net/requests").json()["rows"]
    assert any(x["status"] == 404 for x in rows)


def test_server_timing_only_while_watched(client):
    assert "server-timing" not in client.get("/api/health").headers
    client.get("/api/system/net/summary")  # now watched
    h = client.get("/api/health").headers
    assert h["server-timing"].startswith("fr;dur=") and "fr-watch" in h["server-timing"]
    # never on a non-API path
    assert "server-timing" not in client.get("/static/runtime.js").headers


def test_beacon_merges_and_is_not_logged(client):
    client.get("/api/health", headers={"X-Fused-Page": "/monitor"})
    body = {"page": "/monitor", "routes": {"/api/health": {
        "count": 3, "sum": 900, "max": 700, "samples": [100, 100, 700]}}}
    r = client.post("/api/system/net/client", content=json.dumps(body),
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 204
    groups = client.get("/api/system/net/groups").json()["groups"]
    g = next(x for x in groups if x["route"] == "/api/health")
    assert g["queue_max_ms"] == 700
    assert client.get("/api/system/net/summary").json()["browser_queue_p95_ms"] > 100
    own = client.get("/api/system/net/requests?own=1").json()["rows"]
    assert not any(x["route"] == "/api/system/net/client" for x in own)


def test_beacon_rejects_garbage(client):
    assert client.post("/api/system/net/client", content="nope").status_code == 400
    assert client.post("/api/system/net/client", content="{}").status_code == 400
    assert client.post("/api/system/net/client",
                       content=b"x" * (300 * 1024)).status_code == 413


def test_sockets_endpoint_lists_open_socket(client):
    with client.websocket_connect("/api/fs/events?path=/tmp") as ws:
        socks = client.get("/api/system/net/sockets").json()["sockets"]
        assert any(s["kind"] == "fs-events" and s["open"] == 1 for s in socks)
    # closed once the handler returns
    import time
    for _ in range(50):
        if not client.get("/api/system/net/sockets").json()["sockets"]:
            break
        time.sleep(0.05)
    assert client.get("/api/system/net/sockets").json()["sockets"] == []


def test_streamed_response_stays_in_flight_until_body_ends(client):
    # /static/runtime.js is a FileResponse: finished only when its body is read.
    r = client.get("/static/runtime.js")
    assert r.status_code == 200
    rows = client.get("/api/system/net/requests").json()["rows"]
    row = next(x for x in rows if x["route"] == "/static/runtime.js")
    assert row["outcome"] == "ok" and row["bytes"] == len(r.content)


def _beacon(c, origin=None):
    headers = {"Content-Type": "text/plain"}
    if origin is not None:
        headers["Origin"] = origin
    body = {"page": "/monitor", "routes": {"/api/health": {
        "count": 1, "sum": 5, "max": 5, "samples": [5]}}}
    return c.post("/api/system/net/client", content=json.dumps(body), headers=headers)


def test_beacon_refuses_a_cross_origin_post(tmp_path, monkeypatch):
    monkeypatch.setattr(netlog, "_log", netlog.NetLog())
    c = TestClient(create_app(start_dir=str(tmp_path)), base_url="http://127.0.0.1:8765")
    assert _beacon(c, "https://evil.example").status_code == 403
    assert _beacon(c, "http://localhost:9999").status_code == 403  # another port
    assert _beacon(c, "null").status_code == 403
    assert netlog.get()._client == {}


def test_beacon_accepts_same_origin_or_no_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(netlog, "_log", netlog.NetLog())
    c = TestClient(create_app(start_dir=str(tmp_path)), base_url="http://127.0.0.1:8765")
    assert _beacon(c, "http://127.0.0.1:8765").status_code == 204
    assert _beacon(c, "http://localhost:8765").status_code == 204
    assert _beacon(c).status_code == 204
