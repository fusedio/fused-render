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
        "version": 2,
        "widgets": [
            {"id": "s", "source": "search", "size": "4x1", "format": "bar"},
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
        {"version": 3, "widgets": []},
        {"version": 0, "widgets": []},
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


def test_app_widget_roundtrips_app_path(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "x", "source": "app", "size": "2x2", "format": "live", "appPath": "/w/my app"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_app_widget_roundtrips_web_url(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "u", "source": "app", "size": "2x2", "format": "live", "appPath": "https://example.com/dash"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_tall_size_roundtrips(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {"version": 1, "widgets": [{"id": "t", "source": "tasks", "size": "1x2", "format": "list"}]}
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_app_path_dropped_on_other_sources_and_when_oversized(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "a", "source": "apps", "size": "4x1", "format": "cards", "appPath": "/w/x"},
            {"id": "b", "source": "app", "size": "2x2", "format": "live", "appPath": "p" * 4097},
            # Like a folder with no folderId: kept here, dropped by the client's normalize.
            {"id": "c", "source": "app", "size": "2x2", "format": "live"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    got = client.get("/api/home/layout").json()["layout"]["widgets"]
    assert all("appPath" not in x for x in got)
    assert [x["id"] for x in got] == ["a", "b", "c"]


def test_old_version_document_is_kept_as_is(tmp_path, monkeypatch):
    # The server does not migrate: the client prepends the search widget to a
    # version-1 document and stamps 2 on its next write.
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    old = {"version": 1, "widgets": [{"id": "a", "source": "apps", "size": "4x1", "format": "cards"}]}
    (home / "home_layout.json").write_text(json.dumps(old), encoding="utf-8")
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": old}
    new = {"version": 2, "widgets": [{"id": "s", "source": "search", "size": "2x1", "format": "bar"}, *old["widgets"]]}
    assert client.put("/api/home/layout", json=new, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": new}
