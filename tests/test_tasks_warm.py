"""`tasks.warm()` — the boot-time read that makes the first listing fast.

The process starts with the transcript caches empty, and the first
`_task_rows` reads every transcript from byte zero inside whichever request
asked first. `warm` does that read once, on a thread of its own, at startup.
These tests pin the contract: a warm listing reads nothing, a failing warm
raises nothing, and the endpoints keep answering while a warm is under way.
"""

import builtins
import os
import threading

import pytest

from fused_render import tasks_store
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod
from tests.test_tasks_api import _already_using, _ai_title, _assistant, _user, _write_transcript


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(tasks_store, "PROJECTS_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "STATE_DIR", str(d))
    _already_using(d)
    return d


@pytest.fixture(autouse=True)
def _clear_caches():
    tasks_mod.reset_cache()
    yield
    tasks_mod.reset_cache()


def _transcript(projects_dir, sid="sess-a"):
    return _write_transcript(projects_dir, sid, "/home/me/proj", [
        _user("pull today's news", 1_700_000_000.0),
        _assistant("done", 1_700_000_010.0),
        _ai_title("Pull today's news"),
    ])


def _opens_of(monkeypatch, path):
    """Count how many times `path` is opened for reading."""
    opened = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        if str(file) == str(path):
            opened.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    return opened


def test_warm_fills_the_scan_cache(projects_dir):
    path = _transcript(projects_dir)
    assert str(path) not in tasks_mod._SCAN

    tasks_mod.warm()

    rec = tasks_mod._SCAN[str(path)]
    assert rec["count"] == 1
    assert rec["title"] == "Pull today's news"


def test_a_listing_after_warm_does_not_reopen_an_unchanged_transcript(
        projects_dir, monkeypatch):
    path = _transcript(projects_dir)
    # Old enough that liveness does not tail it either: `_live` reads the last
    # 16KB of any transcript touched in the last 90s, on purpose, every time.
    os.utime(path, (1_700_000_000, 1_700_000_000))
    tasks_mod.warm()

    opened = _opens_of(monkeypatch, path)
    rows = tasks_mod._task_rows()

    assert [r["key"] for r in rows] == ["sess-a"]
    assert opened == [], "a warm listing is stats only; the bytes were read at warm"


def test_a_transcript_that_grew_after_warm_is_read_from_its_offset(
        projects_dir, monkeypatch):
    path = _transcript(projects_dir)
    tasks_mod.warm()
    before = tasks_mod._SCAN[str(path)]["offset"]
    with path.open("a") as f:
        f.write('{"type":"user","uuid":"sess-a-9","sessionId":"sess-a",'
                '"cwd":"/home/me/proj","timestamp":"2023-11-14T22:14:00Z",'
                '"message":{"role":"user","content":"and tomorrow"}}\n')

    rows = tasks_mod._task_rows()

    assert rows[0]["message_count"] == 2
    assert tasks_mod._SCAN[str(path)]["offset"] > before


def test_warm_swallows_a_failing_listing(monkeypatch):
    def boom(only=None):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(tasks_mod, "_build_task_rows", boom)

    tasks_mod.warm()  # must not raise: a cold first request is the only cost


def test_a_request_during_warm_waits_for_that_scan_rather_than_starting_another(
        projects_dir, monkeypatch):
    """Two cold callers must not both read every transcript. The lock makes the
    second wait for the first, and the first's cache is what it then reads."""
    path = _transcript(projects_dir)
    entered = threading.Event()
    release = threading.Event()
    real_build = tasks_mod._build_task_rows
    calls = []

    def slow_build(only=None):
        calls.append(threading.current_thread().name)
        entered.set()
        release.wait(5)
        return real_build(only)
    monkeypatch.setattr(tasks_mod, "_build_task_rows", slow_build)

    warm = threading.Thread(target=tasks_mod.warm, name="warm")
    warm.start()
    assert entered.wait(5)
    # The "request" lands while warm is still inside the scan.
    result = {}
    req = threading.Thread(
        target=lambda: result.setdefault("rows", tasks_mod._task_rows()), name="req")
    req.start()
    req.join(0.2)
    assert req.is_alive(), "the request must block on the lock, not run alongside"
    assert calls == ["warm"]

    release.set()
    warm.join(5)
    req.join(5)
    assert [r["key"] for r in result["rows"]] == ["sess-a"]
    assert calls == ["warm", "req"], "the request ran only after warm let go"
    assert str(path) in tasks_mod._SCAN
