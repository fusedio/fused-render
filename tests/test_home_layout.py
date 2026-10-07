"""Tests for GET/PUT /api/home/layout (fused_render/shell/home_layout.py)."""
import json

from fastapi.testclient import TestClient

from fused_render.server import create_app

FUSED = {"X-Fused": "1"}


def _client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return TestClient(create_app(start_dir=str(tmp_path))), home


def _layout():
    return {
        "version": 1,
        "widgets": [
            {"id": "a", "source": "apps", "size": "4x1", "format": "cards"},
            {"id": "b", "source": "folder", "size": "2x1", "format": "list", "folderId": "f1"},
        ],
    }


def test_get_absent(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/api/home/layout")
    assert resp.status_code == 200
    assert resp.json() == {"exists": False, "layout": None}


def test_put_then_get_roundtrips(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    lay = _layout()
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert json.loads((home / "home_layout.json").read_text("utf-8")) == lay
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_put_requires_fused_header(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert client.put("/api/home/layout", json=_layout()).status_code == 403


def test_put_invalid_is_400(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    w = {"id": "a", "source": "apps", "size": "4x1", "format": "cards"}
    bad = [
        {"version": 2, "widgets": []},
        {"version": 1, "widgets": "x"},
        {"version": 1, "widgets": [1]},
        {"version": 1, "widgets": [{**w, "source": "nope"}]},
        {"version": 1, "widgets": [{**w, "size": "9x9"}]},
        {"version": 1, "widgets": [{**w, "format": 3}]},
        {"version": 1, "widgets": [w] * 49},
    ]
    for body in bad:
        assert client.put("/api/home/layout", json=body, headers=FUSED).status_code == 400, body
    assert client.get("/api/home/layout").json()["exists"] is False


def test_unknown_keys_dropped(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _layout()
    lay["extra"] = 1
    lay["widgets"][0]["junk"] = "x"
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    got = client.get("/api/home/layout").json()["layout"]
    assert got == _layout()


def test_corrupt_file_reports_absent(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    (home / "home_layout.json").write_text("{ nope", encoding="utf-8")
    assert client.get("/api/home/layout").json() == {"exists": False, "layout": None}
