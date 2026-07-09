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


def test_walk_hidden_returns_dot_entries_and_descends_dot_dirs(tmp_path):
    _make_tree(tmp_path)
    data = (
        _client(tmp_path)
        .get("/api/fs/walk", params={"path": str(tmp_path), "hidden": "1"})
        .json()
    )
    rels = {e["rel"] for e in data["entries"]}
    assert ".secret" in rels
    assert ".hidden" in rels
    assert ".hidden/nope.txt" in rels  # descended into the dot-dir
    # non-hidden entries are still present alongside them
    assert {"a.txt", "sub", "sub/b.txt", "sub/deep", "sub/deep/c.txt"} <= rels


def test_walk_hidden_still_prunes_ignored_dirs(tmp_path):
    _make_tree(tmp_path)
    data = (
        _client(tmp_path)
        .get("/api/fs/walk", params={"path": str(tmp_path), "hidden": "1"})
        .json()
    )
    rels = {e["rel"] for e in data["entries"]}
    assert not any("node_modules" in r for r in rels)  # ignored dir never descended


def test_walk_default_still_prunes_hidden(tmp_path):
    _make_tree(tmp_path)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert not any(r.startswith(".") or "/." in r for r in rels)


# --- BFS ordering -------------------------------------------------------------


def _depth(rel):
    return rel.count("/")


def test_walk_is_breadth_first(tmp_path):
    # aaa/ sorts before zzz.txt within a level, but zzz.txt (depth 0) must
    # still come before anything inside aaa/ (depth 1).
    (tmp_path / "aaa").mkdir()
    (tmp_path / "aaa" / "inner.txt").write_text("x", encoding="utf-8")
    (tmp_path / "aaa" / "deep").mkdir()
    (tmp_path / "aaa" / "deep" / "leaf.txt").write_text("x", encoding="utf-8")
    (tmp_path / "zzz.txt").write_text("x", encoding="utf-8")
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    depths = [_depth(e["rel"]) for e in data["entries"]]
    assert depths == sorted(depths)  # never a deep entry before a shallower one


def test_walk_truncation_keeps_shallow_coverage(tmp_path, monkeypatch):
    # The old depth-first walk let one big subtree starve its siblings out of
    # the cap entirely. BFS must emit every top-level entry before ANY deep
    # one, so a truncated walk still covers the whole first level.
    big = tmp_path / "aaa_big"
    big.mkdir()
    for i in range(50):
        (big / f"f{i}.txt").write_text("x", encoding="utf-8")
    for name in ("bbb", "ccc", "ddd"):
        d = tmp_path / name
        d.mkdir()
        (d / "child.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(server, "WALK_MAX_ENTRIES", 10)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    assert data["truncated"] is True
    rels = {e["rel"] for e in data["entries"]}
    assert {"aaa_big", "bbb", "ccc", "ddd"} <= rels  # full top-level coverage


# --- pruning ------------------------------------------------------------------


def test_walk_app_bundle_is_leaf(tmp_path):
    # macOS packages are emitted as one dir entry but never descended.
    app = tmp_path / "Cool.app"
    app.mkdir()
    (app / "Contents").mkdir()
    (app / "Contents" / "Info.plist").write_text("x", encoding="utf-8")
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert "Cool.app" in rels
    assert not any(r.startswith("Cool.app/") for r in rels)


def test_walk_hidden_still_prunes_git_and_venv(tmp_path):
    # .git/.venv are machine-managed noise, pruned even under hidden=1 —
    # otherwise a ".py" extension search floods with .git object files.
    for name in (".git", ".venv"):
        d = tmp_path / name
        d.mkdir()
        (d / "junk.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    data = (
        _client(tmp_path)
        .get("/api/fs/walk", params={"path": str(tmp_path), "hidden": "1"})
        .json()
    )
    rels = {e["rel"] for e in data["entries"]}
    assert ".env" in rels  # a real dotfile still shows
    assert ".git" not in rels and ".venv" not in rels
    assert not any(r.startswith((".git/", ".venv/")) for r in rels)


def test_walk_symlink_dir_not_descended(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    (target / "inside.txt").write_text("x", encoding="utf-8")
    (tmp_path / "link").symlink_to(target)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert "link" in rels  # the symlink itself is listed
    assert "real/inside.txt" in rels  # real dir walked once
    assert "link/inside.txt" not in rels  # ...not twice through the link


# --- streaming (stream=1, NDJSON) ----------------------------------------------


def _stream_lines(client, path, **params):
    import json

    with client.stream(
        "GET", "/api/fs/walk", params={"path": path, "stream": "1", **params}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in resp.iter_lines() if line.strip()]


def test_walk_stream_batches_and_terminal_record(tmp_path, monkeypatch):
    for i in range(7):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(server, "WALK_BATCH_SIZE", 3)
    lines = _stream_lines(_client(tmp_path), str(tmp_path))
    *batches, terminal = lines
    assert terminal == {"done": True, "truncated": False, "total": 7}
    assert [len(b["entries"]) for b in batches] == [3, 3, 1]
    rels = [e["rel"] for b in batches for e in b["entries"]]
    assert sorted(rels) == sorted(f"f{i}.txt" for i in range(7))


def test_walk_stream_same_entries_as_plain(tmp_path):
    _make_tree(tmp_path)
    client = _client(tmp_path)
    plain = client.get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    lines = _stream_lines(client, str(tmp_path))
    streamed = [e for line in lines if "entries" in line for e in line["entries"]]
    assert streamed == plain["entries"]  # same content, same (BFS) order


def test_walk_stream_truncation(tmp_path, monkeypatch):
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(server, "WALK_MAX_ENTRIES", 4)
    lines = _stream_lines(_client(tmp_path), str(tmp_path))
    terminal = lines[-1]
    assert terminal["done"] is True
    assert terminal["truncated"] is True
    assert terminal["total"] == 4
    assert sum(len(line["entries"]) for line in lines[:-1]) == 4


def test_walk_stream_not_a_directory(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a", encoding="utf-8")
    resp = _client(tmp_path).get(
        "/api/fs/walk", params={"path": str(f), "stream": "1"}
    )
    assert resp.status_code == 400
    assert "not a directory" in resp.json()["error"]
