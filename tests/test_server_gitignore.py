"""Tests for the `ignored` field on GET /api/fs/list and /api/fs/walk
(fused_render/server.py) — files matched by .gitignore inside a git work tree
are flagged so the shell can dim them. Non-repos, and installs without git,
degrade to `ignored: False` everywhere (dimming is a hint, never required)."""
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


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


def test_walk_flags_gitignored_entries(tmp_path):
    _make_repo(tmp_path)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    by_rel = {e["rel"]: e for e in data["entries"]}
    assert by_rel["src/app.log"]["ignored"] is True  # nested match
    assert by_rel["src/main.py"]["ignored"] is False


def test_list_outside_git_repo_flags_nothing(tmp_path):
    # No `git init` — check-ignore exits 128, we swallow it and flag nothing.
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.log").write_text("b", encoding="utf-8")
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(tmp_path)}).json()
    assert all(e["ignored"] is False for e in data["entries"])
