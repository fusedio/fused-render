"""Tests for the `ignored` field on GET /api/fs/list and /api/fs/walk
(fused_render/server.py) — files matched by .gitignore inside a git work tree
are flagged so the shell can dim them. Non-repos, and installs without git,
degrade to `ignored: False` everywhere (dimming is a hint, never required)."""
import io
import os
import shutil
import subprocess
import sys
import time

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


# -- _IgnoreOracle deadline ---------------------------------------------------
#
# `_read_field` used to block forever on `self.proc.stdout.read1` with no
# timeout at all. A stalled check-ignore child (an index.lock held elsewhere,
# gc --auto, a degraded filesystem) would hang the request thread
# indefinitely. These are POSIX-only (select() on a pipe doesn't work on
# Windows), matching the class's own `_read_chunk` fallback.

import types

pytestmark_win = pytest.mark.skipif(
    sys.platform == "win32", reason="deadline uses select() on a pipe, POSIX only")


class _NullStdin:
    def write(self, data):
        pass

    def flush(self):
        pass

    def close(self):
        pass


@pytestmark_win
def test_ignored_gives_up_on_a_stalled_child_within_the_deadline(tmp_path, monkeypatch):
    _make_repo(tmp_path)
    oracle = _gi._IgnoreOracle(str(tmp_path))
    assert not oracle.broken
    real_proc = oracle.proc

    # Swap in a pipe whose write end is never written to: read1 (and the
    # select() guarding it) will block exactly as a stalled git would.
    read_fd, write_fd = os.pipe()
    oracle.proc = types.SimpleNamespace(
        stdin=_NullStdin(),
        stdout=os.fdopen(read_fd, "rb"),
        terminate=lambda: None,
    )
    monkeypatch.setattr(oracle, "DEADLINE_S", 0.2)

    start = time.monotonic()
    result = oracle.ignored(["keep.txt"])
    elapsed = time.monotonic() - start

    assert result == set()
    assert oracle.broken is True
    assert elapsed < 2.0  # bounded well under a real hang, generous for CI jitter

    os.close(write_fd)
    real_proc.kill()
    real_proc.wait(timeout=5)


@pytestmark_win
def test_a_broken_oracle_after_a_stall_answers_nothing_ignored_from_then_on(
        tmp_path, monkeypatch):
    _make_repo(tmp_path)
    oracle = _gi._IgnoreOracle(str(tmp_path))
    real_proc = oracle.proc
    read_fd, write_fd = os.pipe()
    oracle.proc = types.SimpleNamespace(
        stdin=_NullStdin(),
        stdout=os.fdopen(read_fd, "rb"),
        terminate=lambda: None,
    )
    monkeypatch.setattr(oracle, "DEADLINE_S", 0.2)
    oracle.ignored(["debug.log"])
    assert oracle.broken is True

    # Further calls must not try to read from the (now-closed) stream again —
    # they short-circuit on `self.broken`.
    assert oracle.ignored(["debug.log"]) == set()

    os.close(write_fd)
    real_proc.kill()
    real_proc.wait(timeout=5)


@pytestmark_win
def test_ignored_does_not_time_out_on_a_slow_but_progressing_stream(tmp_path):
    # A generous deadline must not fire just because a real (fast) call takes
    # a normal amount of time — no false positive on the happy path.
    _make_repo(tmp_path)
    oracle = _gi._IgnoreOracle(str(tmp_path))
    assert oracle.ignored(["debug.log", "keep.txt"]) == {"debug.log"}
    assert oracle.broken is False
    oracle.close()


@pytestmark_win
def test_ignored_survives_a_chunk_cycle_bigger_than_64kib(tmp_path):
    """One CHUNK (200 queries) of ordinary-length paths, echoed back in full
    by `check-ignore -v`, produces more than the 65536-byte `_READ_SIZE` of
    output for one write/read cycle, so this must complete in several
    `_read_chunk` calls, not one. `_read_chunk` reads the fd directly via
    `os.read` rather than through `self.proc.stdout`'s `BufferedReader`
    (see its docstring), so `select()` is authoritative regardless of how the
    kernel happens to buffer git's writes — this is a reliable, not a
    best-effort, regression test."""
    _make_repo(tmp_path)
    oracle = _gi._IgnoreOracle(str(tmp_path))
    # 200 paths of ~400 bytes each (git echoes the full path back for every
    # query under -v) totals well past 64 KiB for one write/read cycle.
    long_paths = [f"deep/{'x' * 380}/{i}.txt" for i in range(oracle.CHUNK)]
    assert sum(len(p) for p in long_paths) > 65536
    oracle.DEADLINE_S = 3.0  # generous — this must finish fast if correct
    result = oracle.ignored(long_paths + ["debug.log"])
    assert oracle.broken is False
    assert result == {"debug.log"}
    oracle.close()


class _ExplodingRead1(io.BufferedReader):
    """A real `BufferedReader` whose `read1` raises — used to prove
    `_read_chunk` never calls it. `io.BufferedReader` is a builtin type and
    cannot be monkeypatched directly (its methods are immutable), so this
    subclasses it instead; everything else (fileno, the underlying raw fd)
    behaves exactly like a real `Popen`-created stdout."""

    def read1(self, *a, **k):
        raise AssertionError("_read_chunk must not use read1()")


@pytestmark_win
def test_read_chunk_never_buffers_bytes_select_cannot_see():
    """`_read_chunk` must read the fd directly (`os.read`), never through
    `self.proc.stdout.read1` — the latter is a `BufferedReader` (no `bufsize`
    on the `Popen`) that can silently absorb more bytes into its OWN buffer
    than one read hands back, invisible to `select()`. If a read ever goes
    through it, a LATER read can land on that residue with `select` having no
    way to know bytes are already in hand: exactly the unbounded wait this
    method exists to remove. Proven directly: use a stdout whose `read1`
    raises, and show a real read still succeeds — it must therefore have
    gone through `os.read` instead."""
    oracle = _gi._IgnoreOracle.__new__(_gi._IgnoreOracle)
    oracle.broken = False
    oracle._buf = b""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"hello")
        oracle.proc = types.SimpleNamespace(
            stdout=_ExplodingRead1(io.FileIO(read_fd, "rb")))
        oracle.DEADLINE_S = 2.0
        oracle._deadline = time.monotonic() + oracle.DEADLINE_S
        assert oracle._read_chunk() == b"hello"
    finally:
        os.close(write_fd)
        oracle.proc.stdout.close()


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
