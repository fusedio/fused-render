"""Tests for the `ignored` field on GET /api/fs/list and /api/fs/walk
(fused_render/server.py) — files matched by .gitignore inside a git work tree
are flagged so the shell can dim them. Non-repos, and installs without git,
degrade to `ignored: False` everywhere (dimming is a hint, never required)."""
import ast
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.server import gitignore

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


# -- the spawn discipline, and the cache that outlived one bad spawn -----------
#
# `_empty_git_dir` backs check-ignore for directories that carry a .gitignore
# without being repos. Its result is memoised for the whole process with no
# invalidation anywhere, so what it decides once, it decides forever.


def _reset_empty_git_dir(monkeypatch):
    monkeypatch.setattr(gitignore, "_EMPTY_GIT_DIR", None)


def test_a_git_init_killed_by_a_signal_is_retried_not_remembered(tmp_path, monkeypatch):
    """A crashed spawn is not evidence that git cannot make a repo.

    This module runs in the SERVER process, where a forked child can die in
    PROJ's atfork handler at ~1ms (`app_git.py` documents the crash and it was
    verified in the field). Under `check=True` that `-11` became a permanent
    `False`, and from then on EVERY un-inited directory silently stopped
    honouring its `.gitignore` — for the life of the server, with nothing to
    read and nothing to retry.
    """
    _reset_empty_git_dir(monkeypatch)
    calls = []

    def _killed(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, -11, b"", b"")

    monkeypatch.setattr(gitignore.subprocess, "run", _killed)
    assert gitignore._empty_git_dir() is None
    assert gitignore._EMPTY_GIT_DIR is None, "a signal must not be cached as a verdict"
    assert gitignore._empty_git_dir() is None
    assert len(calls) == 2, "the next listing must try again"


def test_a_timeout_is_also_no_verdict(tmp_path, monkeypatch):
    _reset_empty_git_dir(monkeypatch)

    def _slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    monkeypatch.setattr(gitignore.subprocess, "run", _slow)
    assert gitignore._empty_git_dir() is None
    assert gitignore._EMPTY_GIT_DIR is None


def test_git_missing_or_refusing_IS_remembered(tmp_path, monkeypatch):
    """The definite half must keep working, or the cache stops being a cache and
    every listing of a non-repo pays a spawn that cannot succeed."""
    _reset_empty_git_dir(monkeypatch)
    monkeypatch.setattr(gitignore.subprocess, "run",
                        lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    assert gitignore._empty_git_dir() is None
    assert gitignore._EMPTY_GIT_DIR is False

    _reset_empty_git_dir(monkeypatch)
    calls = []
    monkeypatch.setattr(
        gitignore.subprocess, "run",
        lambda argv, **kw: (calls.append(argv),
                            subprocess.CompletedProcess(argv, 128, b"", b""))[1])
    assert gitignore._empty_git_dir() is None
    assert gitignore._EMPTY_GIT_DIR is False, "git ran and refused: that is an answer"
    assert gitignore._empty_git_dir() is None
    assert len(calls) == 1, "a definite failure is asked once, not once per listing"


def test_a_retryable_failure_leaves_no_scratch_repos_behind(tmp_path, monkeypatch):
    """Retryable means the mkdtemp happens again, so the failed attempt has to
    take its directory with it or the tempdir fills up one empty repo at a time."""
    _reset_empty_git_dir(monkeypatch)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(gitignore.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, -9, b"", b""))

    for _ in range(3):
        assert gitignore._empty_git_dir() is None
    assert list(tmp_path.iterdir()) == []


def test_the_real_thing_still_produces_a_usable_git_dir(tmp_path, monkeypatch):
    """The whole point, unmocked: with a real git the scratch dir is created,
    cached, and reused."""
    _reset_empty_git_dir(monkeypatch)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    first = gitignore._empty_git_dir()
    assert first and os.path.isdir(first)
    assert gitignore._empty_git_dir() == first, "created once per process"


def test_every_git_spawn_here_stays_off_the_fork_path():
    """Asserted at the source, like `tests/test_engine.py` and
    `tests/test_claude_config_api.py`: the crash needs a resident libproj
    holding a live handle, which a test cannot arrange, so what is checkable is
    that no call in this module can take the fork path."""
    source = Path(gitignore.__file__).read_text(encoding="utf-8")
    spawns = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("run", "Popen")
        and getattr(node.func.value, "id", "") == "subprocess"
    ]
    assert len(spawns) >= 4, "the guard must not pass by matching nothing"
    for call in spawns:
        kwargs = {k.arg: k.value for k in call.keywords if k.arg}
        assert isinstance(kwargs.get("close_fds"), ast.Constant) and (
            kwargs["close_fds"].value is False), f"line {call.lineno}: close_fds"
        for forcing in ("start_new_session", "preexec_fn", "cwd", "pass_fds"):
            assert forcing not in kwargs, f"line {call.lineno}: {forcing} forces fork()"


def test_an_unusable_tempdir_degrades_instead_of_raising(monkeypatch):
    """Nothing in this module may raise at its callers.

    `_IgnoreOracle.__init__` and `_git_ignored` read None as "degrade to
    nothing ignored" and put no `except` around the call, so an OSError out of
    `mkdtemp` — a full disk, a read-only TMPDIR — would turn the loss of some
    dimming into a failed /api/fs/list for every un-inited directory carrying a
    .gitignore. It is also not a verdict about git, so it must not be cached.
    """
    monkeypatch.setattr(gitignore, "_EMPTY_GIT_DIR", None)
    monkeypatch.setattr(
        gitignore.tempfile, "mkdtemp",
        lambda **kw: (_ for _ in ()).throw(OSError(28, "No space left on device")))

    assert gitignore._empty_git_dir() is None
    assert gitignore._EMPTY_GIT_DIR is None


def test_a_walk_survives_a_gitignore_oracle_that_cannot_be_built(tmp_path, monkeypatch):
    """The same thing one layer up, through the route that actually reaches it.

    `/api/fs/walk` is the caller: it builds an `_IgnoreOracle` per directory,
    and a standalone-.gitignore tree is exactly the case that needs the scratch
    `.git`. (`/api/fs/list` takes the `_git_ignored` path and never asks for one
    — a route-level test written against it would pass with the guard removed,
    which is no test at all.)
    """
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")  # no repo here
    (tmp_path / "debug.log").write_text("l", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("k", encoding="utf-8")
    monkeypatch.setattr(gitignore, "_EMPTY_GIT_DIR", None)
    monkeypatch.setattr(
        gitignore.tempfile, "mkdtemp",
        lambda **kw: (_ for _ in ()).throw(OSError(28, "No space left on device")))

    r = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)})

    assert r.status_code == 200
    rels = {e["rel"] for e in r.json()["entries"]}
    assert "keep.txt" in rels
    # Pruning is what is lost when the oracle cannot be built, and losing it is
    # the DEGRADED behaviour this is asserting: the walk still answers.
    assert "debug.log" in rels
