"""Tests for GET/PUT /api/bookmarks (fused_render/shell/bookmarks.py) — the
server-side bookmark store at ~/.fused-render/bookmarks.json.

FUSED_RENDER_HOME is redirected to a tmp dir so no test touches the real home.
"""
import json

from fastapi.testclient import TestClient

from fused_render.server import create_app


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


def _client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app), home


def test_get_reports_not_exists_when_absent(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/api/bookmarks")
    assert resp.status_code == 200
    assert resp.json() == {"exists": False, "bookmarks": []}


def test_put_then_get_roundtrips(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    tree = [
        {"id": "1", "name": "a", "url": "/view/x", "created_at": 1},
        {
            "id": "2",
            "type": "folder",
            "name": "F",
            "collapsed": False,
            "children": [{"id": "3", "name": "b", "url": "/view/y", "created_at": 2}],
        },
    ]
    put = client.put("/api/bookmarks", json=tree, headers=FUSED)
    assert put.status_code == 200
    assert put.json() == {"ok": True, "count": 2}

    # File written under the overridden home, valid JSON.
    saved = json.loads((home / "bookmarks.json").read_text(encoding="utf-8"))
    assert saved == tree

    get = client.get("/api/bookmarks")
    assert get.json() == {"exists": True, "bookmarks": tree}


def test_empty_list_still_reports_exists(tmp_path, monkeypatch):
    # A user who deleted every bookmark leaves []; exists must stay true so the
    # shell never re-imports the old localStorage data (the one-time gate).
    client, _ = _client(tmp_path, monkeypatch)
    assert client.put("/api/bookmarks", json=[], headers=FUSED).status_code == 200
    assert client.get("/api/bookmarks").json() == {"exists": True, "bookmarks": []}


def test_corrupt_file_reports_not_exists(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    (home / "bookmarks.json").write_text("{ not json", encoding="utf-8")
    assert client.get("/api/bookmarks").json() == {"exists": False, "bookmarks": []}


def test_put_without_fused_header_is_rejected(tmp_path, monkeypatch):
    # D3 guard: a blind cross-origin PUT (no X-Fused) must not write.
    client, home = _client(tmp_path, monkeypatch)
    resp = client.put("/api/bookmarks", json=[{"id": "1", "name": "a", "url": "/x", "created_at": 1}])
    assert resp.status_code == 403
    assert not (home / "bookmarks.json").exists()


def test_put_overwrites_last_write_wins(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    client.put("/api/bookmarks", json=[{"id": "1", "name": "a", "url": "/view/x", "created_at": 1}], headers=FUSED)
    client.put("/api/bookmarks", json=[{"id": "2", "name": "b", "url": "/view/y", "created_at": 2}], headers=FUSED)
    got = client.get("/api/bookmarks").json()
    assert [b["id"] for b in got["bookmarks"]] == ["2"]
