"""Tests for the `ignored` field on GET /api/fs/list and /api/fs/walk
(fused_render/server.py) — files matched by .gitignore inside a git work tree
are flagged so the shell can dim them. Non-repos, and installs without git,
degrade to `ignored: False` everywhere (dimming is a hint, never required)."""
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
    """Integration-level regression: one CHUNK (200 queries) of ordinary-
    length paths, echoed back in full by `check-ignore -v`, can exceed the
    65536 bytes `read1` asks for in a single call — `Popen`'s default
    `bufsize` gives `stdout` a 128 KiB internal buffer, so the extra bytes can
    land there, invisible to `select()` on the fd. Before the
    buffered-awareness fix, a read that landed on such a residue would wait
    on `select` for bytes already sitting in Python's own buffer, burn the
    whole deadline, and come back broken with the batch silently unfiltered.

    This exercises the real oracle end-to-end with a batch big enough to make
    that possible, but whether it actually LANDS on the buggy path depends on
    how the kernel pipe happens to buffer git's writes on this machine — it
    is not a reliable trigger by itself.
    `test_read_chunk_skips_select_while_the_buffer_may_still_hold_data` below
    pins the exact mechanism deterministically; this one just proves nothing
    broke in the real, non-mocked path for a batch of this size."""
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


@pytestmark_win
def test_read_chunk_skips_select_while_the_buffer_may_still_hold_data(monkeypatch):
    """Deterministic unit test of the exact heuristic `_read_chunk` uses,
    independent of real pipe/kernel buffering timing: a `read1` that returns
    a FULL `_READ1_SIZE` means the BufferedReader's own buffer may still hold
    more, so the very next read must be tried directly. If it instead went
    through `select()`, this would hang against a starved fd until
    DEADLINE_S and raise TimeoutError."""

    class _FakeStdout:
        def __init__(self, chunks, starved_fd):
            self._chunks = list(chunks)
            self._starved_fd = starved_fd
            self._drained_sentinel = False

        def fileno(self):
            return self._starved_fd

        def read1(self, n):
            # A real `read1` on a real BufferedReader drains the underlying
            # fd; this fake doesn't touch it at all, so the sentinel byte
            # written to make the FIRST select() see the fd ready would
            # otherwise sit there forever, making every later select() falsely
            # "ready" too and hiding exactly the bug this test exists to
            # catch. Drain it once, on the first call only.
            if not self._drained_sentinel:
                self._drained_sentinel = True
                os.read(self._starved_fd, 1)
            return self._chunks.pop(0) if self._chunks else b""

    oracle = _gi._IgnoreOracle.__new__(_gi._IgnoreOracle)
    oracle.broken = False
    oracle._buf = b""
    oracle._may_have_buffered = False
    read_fd, write_fd = os.pipe()
    try:
        # One real byte so the FIRST `select()` — legitimately needed, since
        # nothing is known yet — sees the fd ready immediately. `read1` is
        # faked separately below and never actually consumes it; it only
        # exists to satisfy `select`.
        os.write(write_fd, b"?")
        first = b"x" * oracle._READ1_SIZE          # a FULL read1
        second = b"y" * 10                         # residue: a short read
        oracle.proc = types.SimpleNamespace(
            stdout=_FakeStdout([first, second], read_fd))
        oracle.DEADLINE_S = 0.3
        oracle._deadline = time.monotonic() + oracle.DEADLINE_S

        got_first = oracle._read_chunk()
        assert got_first == first
        assert oracle._may_have_buffered is True

        start = time.monotonic()
        got_second = oracle._read_chunk()
        elapsed = time.monotonic() - start
        assert got_second == second
        assert elapsed < 0.1, "second read went through select() and stalled"
    finally:
        os.close(read_fd)
        os.close(write_fd)


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
