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


# --- D97 duplicate-name migration (GET-time, one write, idempotent) -----------


def _bm(id, name, created_at):
    return {"id": id, "name": name, "url": f"/view/{id}", "created_at": created_at}


def _write_tree(home, tree):
    home.mkdir(parents=True, exist_ok=True)
    (home / "bookmarks.json").write_text(json.dumps(tree), encoding="utf-8")


def test_migration_suffixes_duplicates_oldest_keeps_name(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Newest listed first: created_at, not list order, decides who keeps "a".
    _write_tree(home, [_bm("2", "a", 20), _bm("1", "a", 10), _bm("3", "a", 30)])
    got = {b["id"]: b["name"] for b in client.get("/api/bookmarks").json()["bookmarks"]}
    assert got == {"1": "a", "2": "a-1", "3": "a-2"}
    # Migrated tree persisted to disk.
    saved = json.loads((home / "bookmarks.json").read_text(encoding="utf-8"))
    assert {b["id"]: b["name"] for b in saved} == got


def test_migration_is_case_insensitive_and_global_across_folders(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    folder = {"id": "f", "type": "folder", "name": "a", "collapsed": False,
              "children": [_bm("2", "A", 20)]}
    _write_tree(home, [_bm("1", "a", 10), folder])
    got = client.get("/api/bookmarks").json()["bookmarks"]
    assert got[0]["name"] == "a"
    # Folder child collides with the top-level bookmark despite the case, but
    # the folder's own name "a" is a separate namespace and stays untouched.
    assert got[1]["name"] == "a"
    assert got[1]["children"][0]["name"] == "A-1"


def test_migration_suffix_skips_existing_names(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # A pre-existing literal "a-1" blocks that suffix: the duplicate jumps to -2.
    _write_tree(home, [_bm("1", "a", 10), _bm("2", "a", 20), _bm("3", "a-1", 30)])
    got = {b["id"]: b["name"] for b in client.get("/api/bookmarks").json()["bookmarks"]}
    assert got == {"1": "a", "2": "a-2", "3": "a-1"}


def test_migration_is_idempotent_and_writes_only_on_change(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    _write_tree(home, [_bm("1", "a", 10), _bm("2", "a", 20)])
    first = client.get("/api/bookmarks").json()["bookmarks"]
    saved = (home / "bookmarks.json").read_text(encoding="utf-8")
    # Second GET: already unique, so the exact bytes on disk are untouched
    # (write_json would reformat — unchanged text proves no write happened).
    assert client.get("/api/bookmarks").json()["bookmarks"] == first
    assert (home / "bookmarks.json").read_text(encoding="utf-8") == saved


def test_migration_leaves_unique_tree_unwritten(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Compact JSON (no indent) differs from write_json's output; surviving a GET
    # byte-identical proves the no-duplicates path never writes.
    _write_tree(home, [_bm("1", "a", 10), _bm("2", "b", 20)])
    raw = (home / "bookmarks.json").read_text(encoding="utf-8")
    assert client.get("/api/bookmarks").json()["exists"] is True
    assert (home / "bookmarks.json").read_text(encoding="utf-8") == raw
