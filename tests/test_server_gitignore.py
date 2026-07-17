"""Tests for the `ignored` field on GET /api/fs/list and /api/fs/walk
(fused_render/server.py) — files matched by .gitignore inside a git work tree
are flagged so the shell can dim them. Non-repos, and installs without git,
degrade to `ignored: False` everywhere (dimming is a hint, never required)."""

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _git_init(path):
    # Minimal repo: no commits needed — check-ignore only reads the rules.
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _make_repo(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("k", encoding="utf-8")
    (tmp_path / "debug.log").write_text("l", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.o").write_text("o", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.log").write_text("l", encoding="utf-8")
    (tmp_path / "src" / "main.py").write_text("m", encoding="utf-8")


def test_list_flags_gitignored_entries(tmp_path):
    _make_repo(tmp_path)
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(tmp_path)}).json()
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name["debug.log"]["ignored"] is True
    assert by_name["build"]["ignored"] is True  # an ignored directory is flagged
    assert by_name["keep.txt"]["ignored"] is False
    assert by_name["src"]["ignored"] is False
    assert by_name[".gitignore"]["ignored"] is False


def test_walk_excludes_gitignored_entries(tmp_path):
    # The walk PRUNES gitignored entries outright (see _walk_bfs) — search
    # never sees them, so walk entries carry no `ignored` dimming flag (that
    # stays a /api/fs/list concern, where ignored entries are still shown).
    _make_repo(tmp_path)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert "src/main.py" in rels
    assert "src/app.log" not in rels  # nested gitignore match pruned
    assert all("ignored" not in e for e in data["entries"])


def test_list_flags_dot_git_directory(tmp_path):
    # git never reports `.git` via check-ignore, but inside a work tree we dim
    # it anyway — it's repo plumbing, not user data.
    _make_repo(tmp_path)
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(tmp_path)}).json()
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name[".git"]["ignored"] is True


def test_list_outside_git_repo_flags_nothing(tmp_path):
    # No `git init` — check-ignore exits 128, we swallow it and flag nothing.
    # A stray `.git`-named file here must NOT be dimmed: no work tree, no git.
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.log").write_text("b", encoding="utf-8")
    (tmp_path / ".git").write_text("not a repo", encoding="utf-8")
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(tmp_path)}).json()
    assert all(e["ignored"] is False for e in data["entries"])
