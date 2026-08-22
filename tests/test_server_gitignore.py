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


# -- _repo_toplevel memoization -----------------------------------------------
#
# `_repo_toplevel` used to shell out to `git rev-parse --show-toplevel` on
# EVERY call, unconditionally — and the rank path (index/query.py) calls it
# once per gitignore-filter pass, twice on a query that escalates to the
# subsequence pass. These tests assert the memoization behaviourally (spawn
# COUNT), not by wall clock: a wall-clock assertion cannot fail on a fast
# machine even when the cache does nothing, a spawn-count assertion can.

from fused_render.server import gitignore as _gi


@pytest.fixture(autouse=True)
def _clear_toplevel_cache():
    _gi._reset_toplevel_cache()
    yield
    _gi._reset_toplevel_cache()


def _spy_on_subprocess_run(monkeypatch):
    calls = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_gi.subprocess, "run", spy)
    return calls


def test_repo_toplevel_memoizes_a_successful_lookup(tmp_path, monkeypatch):
    _git_init(tmp_path)
    calls = _spy_on_subprocess_run(monkeypatch)
    first = _gi._repo_toplevel(str(tmp_path))
    for _ in range(5):
        assert _gi._repo_toplevel(str(tmp_path)) == first
    assert len(calls) == 1
    assert first is not None


def test_repo_toplevel_memoizes_an_ordinary_negative(tmp_path, monkeypatch):
    # No `git init`: an ordinary "not a git repository" negative.
    calls = _spy_on_subprocess_run(monkeypatch)
    for _ in range(5):
        assert _gi._repo_toplevel(str(tmp_path)) is None
    assert len(calls) == 1


def test_repo_toplevel_does_not_cache_a_transient_failure(tmp_path, monkeypatch):
    # `subprocess.run` raising is git being unusable RIGHT NOW, not a fact
    # about `path` — every subsequent call must retry, not freeze the miss.
    calls = []

    def always_times_out(cmd, *args, **kwargs):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(_gi.subprocess, "run", always_times_out)
    assert _gi._repo_toplevel(str(tmp_path)) is None
    assert _gi._repo_toplevel(str(tmp_path)) is None
    assert len(calls) == 2


def test_repo_toplevel_does_not_cache_an_abnormal_refusal(tmp_path, monkeypatch):
    # exit 128 with stderr that is NOT the ordinary "not a git repository"
    # message — e.g. "detected dubious ownership". A fixable environment
    # problem, not a fact about the path, so it must not stick.
    calls = []
    real_run = subprocess.run

    def refuse_abnormally(cmd, *args, **kwargs):
        calls.append(cmd)
        result = real_run(cmd, *args, **kwargs)
        result.returncode = 128
        result.stderr = b"fatal: detected dubious ownership in repository"
        return result

    monkeypatch.setattr(_gi.subprocess, "run", refuse_abnormally)
    assert _gi._repo_toplevel(str(tmp_path)) is None
    assert _gi._repo_toplevel(str(tmp_path)) is None
    assert len(calls) == 2


def test_repo_toplevel_cache_expires_after_the_ttl(tmp_path, monkeypatch):
    _git_init(tmp_path)
    calls = _spy_on_subprocess_run(monkeypatch)
    fake_now = [1000.0]
    monkeypatch.setattr(_gi.time, "monotonic", lambda: fake_now[0])

    assert _gi._repo_toplevel(str(tmp_path)) is not None
    assert len(calls) == 1

    fake_now[0] += _gi._TOPLEVEL_MAX_AGE_S - 1
    assert _gi._repo_toplevel(str(tmp_path)) is not None
    assert len(calls) == 1  # still within the TTL

    fake_now[0] += 2
    assert _gi._repo_toplevel(str(tmp_path)) is not None
    assert len(calls) == 2  # TTL elapsed, re-asked


def test_repo_toplevel_cache_is_bounded(tmp_path, monkeypatch):
    calls = _spy_on_subprocess_run(monkeypatch)
    monkeypatch.setattr(_gi, "_TOPLEVEL_CACHE_SIZE", 2)
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for d in (a, b, c):
        d.mkdir()
        _gi._repo_toplevel(str(d))
    assert len(_gi._toplevel_cache) == 2
    # `a` was evicted (least-recently used) — asking again re-spawns.
    before = len(calls)
    _gi._repo_toplevel(str(a))
    assert len(calls) == before + 1
