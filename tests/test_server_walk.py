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


# --- gitignore-driven pruning ---------------------------------------------------


def _git(cwd, *args):
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _make_repo(root):
    root.mkdir(exist_ok=True)
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("dist/\n*.log\n!keep.log\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("x", encoding="utf-8")
    (root / "dist").mkdir()
    (root / "dist" / "bundle.js").write_text("x", encoding="utf-8")
    (root / "debug.log").write_text("x", encoding="utf-8")
    (root / "keep.log").write_text("x", encoding="utf-8")


def _rels(client, path, **params):
    data = client.get("/api/fs/walk", params={"path": str(path), **params}).json()
    return {e["rel"] for e in data["entries"]}


def test_walk_prunes_gitignored_entries(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    rels = _rels(_client(tmp_path), tmp_path)
    assert "repo/src/main.py" in rels
    assert "repo/keep.log" in rels  # negation pattern honored
    assert "repo/dist" not in rels  # ignored dir not emitted...
    assert not any(r.startswith("repo/dist/") for r in rels)  # ...nor descended
    assert "repo/debug.log" not in rels  # ignored file dropped


def test_walk_gitignore_applies_when_walking_repo_subdir(tmp_path):
    # Walk STARTS below the repo root: no .git is ever seen during the walk,
    # so the rev-parse toplevel lookup must supply the repo.
    repo = tmp_path / "repo"
    _make_repo(repo)
    sub = repo / "src"
    (sub / "out.log").write_text("x", encoding="utf-8")
    rels = _rels(_client(tmp_path), sub)
    assert "main.py" in rels
    assert "out.log" not in rels  # repo rules reach the subdir walk


def test_walk_nested_repo_uses_its_own_rules(tmp_path):
    outer = tmp_path / "outer"
    _make_repo(outer)
    inner = outer / "inner"
    inner.mkdir()
    _git(inner, "init", "-q")
    (inner / ".gitignore").write_text("secret/\n", encoding="utf-8")
    (inner / "secret").mkdir()
    (inner / "secret" / "x.txt").write_text("x", encoding="utf-8")
    (inner / "visible.log").write_text("x", encoding="utf-8")  # outer's *.log must NOT apply
    rels = _rels(_client(tmp_path), tmp_path)
    assert "outer/inner/visible.log" in rels  # inner repo boundary respected
    assert not any("secret" in r for r in rels)  # inner's own ignore applies


def test_walk_non_repo_tree_unaffected(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("x", encoding="utf-8")
    (tmp_path / "debug.log").write_text("x", encoding="utf-8")
    rels = _rels(_client(tmp_path), tmp_path)
    assert "dist/bundle.js" in rels  # no repo, no gitignore semantics
    assert "debug.log" in rels


def test_walk_gitignore_pruning_applies_under_hidden(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    rels = _rels(_client(tmp_path), tmp_path, hidden="1")
    assert "repo/.gitignore" in rels  # dotfile shows under hidden=1
    assert not any(r.startswith("repo/dist") for r in rels)  # pruning still applies
    assert not any(r.startswith("repo/.git/") for r in rels)  # floor still applies


def test_walk_stream_prunes_gitignored_too(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    client = _client(tmp_path)
    lines = _stream_lines(client, str(tmp_path))
    streamed = {e["rel"] for line in lines if "entries" in line for e in line["entries"]}
    assert streamed == _rels(client, tmp_path)  # stream/non-stream parity


def test_walk_standalone_gitignore_without_repo(tmp_path):
    # No `git init` anywhere — a bare .gitignore still prunes (empty-GIT_DIR
    # graft): the exact "content-engine/.env shows up in search" report.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text(".env\nbuild/\n!keep.env\n", encoding="utf-8")
    (proj / ".env").write_text("secret", encoding="utf-8")
    (proj / "keep.env").write_text("x", encoding="utf-8")
    (proj / "notes.md").write_text("x", encoding="utf-8")
    (proj / "build").mkdir()
    (proj / "build" / "out.js").write_text("x", encoding="utf-8")
    rels = _rels(_client(tmp_path), tmp_path, hidden="1")
    assert "proj/notes.md" in rels
    assert "proj/keep.env" in rels  # negation honored without a repo
    assert "proj/.gitignore" in rels  # the ignore file itself is not ignored
    assert "proj/.env" not in rels  # pruned even under hidden=1
    assert not any(r.startswith("proj/build") for r in rels)


def test_walk_standalone_gitignore_cascades_to_subdirs(tmp_path):
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (proj / "sub" / "app.log").write_text("x", encoding="utf-8")
    (proj / "sub" / "app.txt").write_text("x", encoding="utf-8")
    rels = _rels(_client(tmp_path), tmp_path)
    assert "proj/sub/app.txt" in rels
    assert "proj/sub/app.log" not in rels  # root rules reach subdirs
