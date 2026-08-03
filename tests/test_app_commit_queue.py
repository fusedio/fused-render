"""The debounced app committer (fused_render/app_commit_queue.py): editor
mutations mark an app dirty, one global asyncio worker turns each burst into
a single aggregated commit; without a running worker marks accumulate until
the next flush() (what lifespan-less test apps rely on).

Real git in tmp workspaces, same fixture shape as tests/test_app_git.py.
Debounce windows are monkeypatched tight so the async tests stay fast.
"""
import asyncio
import subprocess
import time

import pytest

from fused_render import app_commit_queue as q
from fused_render import app_git


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def fast_debounce(monkeypatch):
    monkeypatch.setattr(q, "_QUIET_S", 0.05)
    monkeypatch.setattr(q, "_MAX_AGE_S", 0.5)


def _log(app_dir):
    out = subprocess.run(["git", "-C", str(app_dir), "log", "--format=%s"],
                         capture_output=True, text=True)
    return out.stdout.strip().splitlines()


def _make_app(workspace, tag="local", name="demo"):
    d = workspace / tag / name
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html></html>")
    assert app_git.init_repo(str(d))
    return d


def _run(coro):
    asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_pending():
    yield
    with q._lock:
        q._pending.clear()


# --------------------------------------------------------- mark w/o worker

def test_marks_accumulate_until_flush_when_worker_not_running(workspace):
    d = _make_app(workspace)
    (d / "index.html").write_text("<html>v2</html>")
    q.mark(str(d / "index.html"), "Edit")
    assert _log(d) == ["New app from starter"]  # queued, not committed
    q.flush()
    assert _log(d)[0] == "Edit index.html"


def test_mark_is_a_noop_outside_app_dirs(workspace, tmp_path):
    repo = tmp_path / "userrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f.txt").write_text("x")
    q.mark(str(repo / "f.txt"), "Edit")
    assert _log(repo) == []


# ------------------------------------------------------------------ debounce

def test_burst_collapses_into_one_commit(workspace, fast_debounce):
    d = _make_app(workspace)

    async def go():
        q.start()
        for v in ("v2", "v3", "v4"):
            (d / "index.html").write_text(f"<html>{v}</html>")
            q.mark(str(d / "index.html"), "Edit")
        await asyncio.sleep(0.3)
        await q.stop()

    _run(go())
    subjects = _log(d)
    assert subjects == ["Edit index.html", "New app from starter"]
    assert (d / "index.html").read_text() == "<html>v4</html>"


def test_burst_message_aggregates_files(workspace, fast_debounce):
    d = _make_app(workspace)

    async def go():
        q.start()
        (d / "index.html").write_text("<html>v2</html>")
        q.mark(str(d / "index.html"), "Edit")
        (d / "style.css").write_text("body{}")
        q.mark(str(d / "style.css"), "Edit")
        await asyncio.sleep(0.3)
        await q.stop()

    _run(go())
    assert _log(d)[0] == "Edit index.html, style.css"


def test_two_apps_commit_independently(workspace, fast_debounce):
    a = _make_app(workspace, name="one")
    b = _make_app(workspace, name="two")

    async def go():
        q.start()
        (a / "index.html").write_text("<html>a</html>")
        q.mark(str(a / "index.html"), "Edit")
        (b / "index.html").write_text("<html>b</html>")
        q.mark(str(b / "index.html"), "Edit")
        await asyncio.sleep(0.3)
        await q.stop()

    _run(go())
    assert _log(a)[0] == "Edit index.html"
    assert _log(b)[0] == "Edit index.html"


def test_requeue_merges_with_pending_and_new_marks(workspace):
    # _requeue is what a cancelled-mid-batch worker uses to put entries
    # `_take_due` already popped back — it must merge with anything a mark()
    # added in the meantime, exactly like mark() itself does.
    d = _make_app(workspace)
    app_dir = str(d)
    q._requeue([(app_dir, ["Edit a.html"])])
    with q._lock:
        assert q._pending[app_dir]["labels"] == ["Edit a.html"]
    q.mark(str(d / "b.html"), "Edit")
    q._requeue([(app_dir, ["Edit a.html"])])  # same label again: no duplicate
    with q._lock:
        labels = q._pending[app_dir]["labels"]
    assert labels.count("Edit a.html") == 1
    assert "Edit b.html" in labels
    assert q._requeue([]) is None  # empty batch: a no-op, not a KeyError


def test_shutdown_requeues_uncommitted_apps_mid_batch(workspace, fast_debounce,
                                                       monkeypatch):
    # Two apps due in the same worker batch; make the FIRST app's commit()
    # slow so stop()'s cancellation lands while the SECOND is still sitting
    # in `remaining` (already popped from _pending by _take_due). Without
    # requeueing on CancelledError, that second app's change would vanish —
    # not committed, and no longer pending for stop()'s own flush() either.
    a = _make_app(workspace, name="one")
    b = _make_app(workspace, name="two")

    real_commit = app_git.commit
    order = []

    def slow_commit(app_dir, message):
        order.append(app_dir)
        if app_dir == str(a):
            time.sleep(0.4)
        return real_commit(app_dir, message)

    monkeypatch.setattr(app_git, "commit", slow_commit)

    async def go():
        q.start()
        (a / "index.html").write_text("<html>a</html>")
        q.mark(str(a / "index.html"), "Edit")
        (b / "index.html").write_text("<html>b</html>")
        q.mark(str(b / "index.html"), "Edit")
        # Let the debounce (0.05s) fire and the worker start committing `a`.
        await asyncio.sleep(0.15)
        assert order == [str(a)]  # confirms we're mid-batch, not done yet
        await q.stop()

    _run(go())
    # Both apps got their commit — `b` via the requeue -> stop()'s flush().
    assert _log(a)[0] == "Edit index.html"
    assert _log(b)[0] == "Edit index.html"


def test_stop_flushes_pending_before_the_debounce_expires(workspace,
                                                          monkeypatch):
    # Huge debounce: only the shutdown flush can produce this commit.
    monkeypatch.setattr(q, "_QUIET_S", 60.0)
    monkeypatch.setattr(q, "_MAX_AGE_S", 60.0)
    d = _make_app(workspace)

    async def go():
        q.start()
        (d / "index.html").write_text("<html>v2</html>")
        q.mark(str(d / "index.html"), "Edit")
        await q.stop()

    _run(go())
    assert _log(d)[0] == "Edit index.html"


def test_max_age_commits_while_saves_keep_streaming(workspace, monkeypatch):
    # Quiet period never satisfied (marks every 0.02s < 0.1s), so only the
    # max-age cap can land this commit before we stop marking.
    monkeypatch.setattr(q, "_QUIET_S", 0.1)
    monkeypatch.setattr(q, "_MAX_AGE_S", 0.25)
    d = _make_app(workspace)

    async def go():
        q.start()
        for i in range(25):
            (d / "index.html").write_text(f"<html>{i}</html>")
            q.mark(str(d / "index.html"), "Edit")
            await asyncio.sleep(0.02)
        committed = len(_log(d)) >= 2
        await q.stop()
        assert committed, "max-age cap never fired while marks streamed"

    _run(go())
    assert _log(d)[0] == "Edit index.html"


# ------------------------------------------------------------------- message

def test_message_shapes():
    assert q._message(["Edit index.html"]) == "Edit index.html"
    assert q._message(["Edit a", "Edit b"]) == "Edit a, b"
    assert q._message(["Edit a", "Delete b"]) == "Edit a, Delete b"
    assert q._message(["Edit a", "Edit b", "Edit c", "Edit d", "Edit e"]) == \
        "Edit a, b, c +2 more"
