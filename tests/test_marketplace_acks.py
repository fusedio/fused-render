"""GET /api/marketplace/acks + POST /api/marketplace/ack — the Python-notice
acknowledgements, moved off localStorage (D1384)."""
import json
import os

from fastapi.testclient import TestClient

from fused_render import marketplace
from fused_render.server import create_app

FUSED = {"X-Fused": "1"}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "ws"))
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_get_absent_is_empty(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/marketplace/acks").json() == {"slugs": []}


def test_ack_roundtrips_and_is_idempotent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/marketplace/ack", json={"slug": "zeta-app"}, headers=FUSED)
    assert r.status_code == 200 and r.json() == {"ok": True, "slugs": ["zeta-app"]}
    r = client.post("/api/marketplace/ack", json={"slug": "alpha"}, headers=FUSED)
    assert r.json()["slugs"] == ["alpha", "zeta-app"]
    r = client.post("/api/marketplace/ack", json={"slug": "alpha"}, headers=FUSED)
    assert r.json()["slugs"] == ["alpha", "zeta-app"]
    assert client.get("/api/marketplace/acks").json() == {"slugs": ["alpha", "zeta-app"]}
    with open(marketplace.acks_path()) as f:
        assert json.load(f) == {"python": ["alpha", "zeta-app"]}


def test_ack_requires_fused_header(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/marketplace/ack", json={"slug": "alpha"})
    assert r.status_code == 403
    assert not os.path.exists(marketplace.acks_path())


def test_bad_slug_is_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    for body in ({"slug": "../etc"}, {"slug": "Alpha"}, {"slug": "a"}, {"slug": 3},
                 {"slug": ""}, {}):
        r = client.post("/api/marketplace/ack", json=body, headers=FUSED)
        assert r.status_code == 400, body
    assert client.get("/api/marketplace/acks").json() == {"slugs": []}


def test_corrupt_file_reads_empty_and_skips_non_slugs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    os.makedirs(marketplace.state_dir(), exist_ok=True)
    with open(marketplace.acks_path(), "w") as f:
        f.write("{not json")
    assert client.get("/api/marketplace/acks").json() == {"slugs": []}
    with open(marketplace.acks_path(), "w") as f:
        json.dump({"python": ["ok-app", "../bad", 7]}, f)
    assert client.get("/api/marketplace/acks").json() == {"slugs": ["ok-app"]}
