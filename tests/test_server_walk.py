"""Tests for GET /api/fs/walk (fused_render/server.py) — the recursive listing
backing the explorer search."""
from fastapi.testclient import TestClient

from fused_render import server
from fused_render.server import create_app


def _client(tmp_path):
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app)


def _make_tree(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "deep" / "c.txt").write_text("c", encoding="utf-8")
    # hidden file + hidden dir + ignored dir — all pruned
    (tmp_path / ".secret").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "nope.txt").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")


def test_walk_not_a_directory(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a", encoding="utf-8")
    resp = _client(tmp_path).get("/api/fs/walk", params={"path": str(f)})
    assert resp.status_code == 400
    assert "not a directory" in resp.json()["error"]


def test_walk_recurses_and_reports_rels(tmp_path):
    _make_tree(tmp_path)
    resp = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["truncated"] is False
    rels = {e["rel"] for e in data["entries"]}
    assert rels == {"a.txt", "sub", "sub/b.txt", "sub/deep", "sub/deep/c.txt"}
    by_rel = {e["rel"]: e for e in data["entries"]}
    assert by_rel["sub"]["is_dir"] is True
    assert by_rel["sub"]["size"] is None
    assert by_rel["a.txt"]["is_dir"] is False
    assert by_rel["a.txt"]["size"] == 1


def test_walk_prunes_hidden_and_ignored(tmp_path):
    _make_tree(tmp_path)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert not any(r.startswith(".") or "/." in r for r in rels)  # no hidden entries
    assert not any("node_modules" in r for r in rels)  # ignored dir never descended


def test_walk_truncation_flag(tmp_path, monkeypatch):
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(server, "WALK_MAX_ENTRIES", 3)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    assert data["truncated"] is True
    assert len(data["entries"]) == 3
