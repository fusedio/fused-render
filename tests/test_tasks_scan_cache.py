"""The scan cache FILE — `tasks-scan.json`, the two transcript caches carried
across server processes (routers/tasks `load_scan_cache` / `save_scan_cache`).

The listing's cost is reading every transcript from byte zero once per process.
The file carries the offsets over, so a launch reads only what each transcript
grew by. These tests pin: a saved cache makes the next process read nothing for
an unchanged file; growth is read from the saved offset; a shrunk file is
re-read from zero; a corrupt or foreign file is ignored, not raised on; vanished
paths are not written; and listings write at most once per save window.
"""

import builtins
import json
import os

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


OLD = 1_700_000_000


def _transcript(projects_dir, sid="sess-a"):
    # ISO stamps, as Claude writes them: a record with no readable timestamp
    # leaves the head INCOMPLETE, and an incomplete head is deliberately not
    # persisted (tasks_store.export_heads).
    path = _write_transcript(projects_dir, sid, "/home/me/proj", [
        _user("pull today's news", "2023-11-14T22:13:20Z"),
        _assistant("done", "2023-11-14T22:13:30Z"),
        _ai_title("Pull today's news"),
    ])
    # Old enough that liveness does not tail it: `_live` reads the last 16KB
    # of any transcript touched in the last 90s, every time, by design.
    os.utime(path, (OLD, OLD))
    return path


def _opens_of(monkeypatch, path):
    opened = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        if str(file) == str(path):
            opened.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    return opened


def _new_process():
    """What a relaunch is to the caches: everything in RAM gone, the file kept."""
    tasks_mod.reset_cache()


def test_warm_writes_the_file_and_the_next_process_reads_nothing(
        projects_dir, state_dir, monkeypatch):
    path = _transcript(projects_dir)
    tasks_mod.warm()
    cache = state_dir / tasks_mod.SCAN_CACHE_FILE
    assert cache.exists()
    data = json.loads(cache.read_text())
    assert data["version"] == 1
    assert data["scan"][str(path)]["count"] == 1
    assert data["scan"][str(path)]["title"] == "Pull today's news"
    assert str(path) in data["head"]

    _new_process()
    assert str(path) not in tasks_mod._SCAN
    opened = _opens_of(monkeypatch, path)
    tasks_mod.warm()
    rows = tasks_mod._task_rows()

    assert [r["key"] for r in rows] == ["sess-a"]
    assert rows[0]["title"] == "Pull today's news"
    assert rows[0]["project"] == "/home/me/proj", "the head came from the file too"
    assert opened == [], "an unchanged transcript is stats only after a cached launch"


def test_a_transcript_that_grew_between_processes_is_read_from_its_offset(
        projects_dir, monkeypatch):
    path = _transcript(projects_dir)
    tasks_mod.warm()
    offset = tasks_mod._SCAN[str(path)]["offset"]
    with path.open("a") as f:
        f.write('{"type":"user","uuid":"sess-a-9","sessionId":"sess-a",'
                '"cwd":"/home/me/proj","timestamp":"2023-11-14T22:14:00Z",'
                '"message":{"role":"user","content":"and tomorrow"}}\n')
    os.utime(path, (OLD, OLD))

    _new_process()
    tasks_mod.load_scan_cache()
    seen = []
    real_open = builtins.open

    def spying_open(file, *args, **kwargs):
        f = real_open(file, *args, **kwargs)
        if str(file) == str(path):
            real_seek = f.seek

            def seek(pos, *a):
                seen.append(pos)
                return real_seek(pos, *a)
            f.seek = seek
        return f
    monkeypatch.setattr(builtins, "open", spying_open)
    rows = tasks_mod._task_rows()

    assert rows[0]["message_count"] == 2
    assert seen and seen[0] == offset, "read from where the last process stopped"


def test_a_transcript_that_shrank_between_processes_is_read_from_zero(projects_dir):
    path = _transcript(projects_dir)
    tasks_mod.warm()
    assert tasks_mod._SCAN[str(path)]["count"] == 1
    # Replaced by a shorter file: one prompt, no ai-title.
    path.write_text(json.dumps({
        "type": "user", "uuid": "sess-a-0", "sessionId": "sess-a",
        "cwd": "/home/me/proj", "timestamp": "2023-11-14T22:13:20Z",
        "message": {"role": "user", "content": "fresh start"}}) + "\n")
    os.utime(path, (OLD, OLD))

    _new_process()
    tasks_mod.load_scan_cache()
    rows = tasks_mod._task_rows()

    assert rows[0]["message_count"] == 1
    assert rows[0]["title"] == "fresh start"


@pytest.mark.parametrize("body", [
    "not json{",
    json.dumps({"version": 99, "scan": {}, "head": {}}),
    json.dumps({"version": 1, "scan": "nope", "head": []}),
    json.dumps([1, 2, 3]),
])
def test_a_corrupt_or_foreign_file_is_ignored_not_raised_on(projects_dir, state_dir, body):
    path = _transcript(projects_dir)
    (state_dir / tasks_mod.SCAN_CACHE_FILE).write_text(body)

    assert tasks_mod.load_scan_cache() == 0
    rows = tasks_mod._task_rows()
    assert rows[0]["title"] == "Pull today's news"


def test_a_record_of_the_wrong_shape_is_dropped_alone(projects_dir, state_dir):
    good = _transcript(projects_dir, "sess-a")
    bad = _transcript(projects_dir, "sess-b")
    tasks_mod.warm()
    cache = state_dir / tasks_mod.SCAN_CACHE_FILE
    data = json.loads(cache.read_text())
    data["scan"][str(bad)] = {"offset": "zero", "size": 1}
    cache.write_text(json.dumps(data))

    _new_process()
    assert tasks_mod.load_scan_cache() == 1
    assert str(good) in tasks_mod._SCAN
    assert str(bad) not in tasks_mod._SCAN
    rows = tasks_mod._task_rows()
    assert sorted(r["key"] for r in rows) == ["sess-a", "sess-b"], "the bad one is simply re-read"


def test_a_vanished_transcript_is_not_written(projects_dir, state_dir):
    path = _transcript(projects_dir)
    tasks_mod.warm()
    path.unlink()

    tasks_mod.save_scan_cache()

    data = json.loads((state_dir / tasks_mod.SCAN_CACHE_FILE).read_text())
    assert data["scan"] == {}
    assert data["head"] == {}


def test_listings_write_at_most_once_per_window(projects_dir, state_dir, monkeypatch):
    path = _transcript(projects_dir)
    cache = state_dir / tasks_mod.SCAN_CACHE_FILE
    writes = []
    real = tasks_mod.storage.write_json
    # Only THIS file's writes: the desk (current_apps) writes its own store
    # through the same helper on every listing.
    monkeypatch.setattr(tasks_mod.storage, "write_json",
                        lambda p, d: (p == str(cache) and writes.append(p), real(p, d)))

    tasks_mod.load_scan_cache()  # a process that has looked at the file may write it
    tasks_mod._task_rows()  # cold: read bytes → dirty → first write
    assert len(writes) == 1
    tasks_mod._task_rows()  # nothing read: not dirty → no write
    assert len(writes) == 1
    with path.open("a") as f:
        f.write('{"type":"user","uuid":"sess-a-9","sessionId":"sess-a",'
                '"cwd":"/home/me/proj","timestamp":"2023-11-14T22:14:00Z",'
                '"message":{"role":"user","content":"and tomorrow"}}\n')
    os.utime(path, (OLD, OLD))
    tasks_mod._task_rows()  # read bytes, but inside the window → held
    assert len(writes) == 1
    monkeypatch.setattr(tasks_mod, "_SCAN_SAVED_AT",
                        tasks_mod._SCAN_SAVED_AT - tasks_mod.SCAN_CACHE_SAVE_EVERY_S - 1)
    tasks_mod._task_rows()  # still dirty, window over → written
    assert len(writes) == 2
    assert json.loads(cache.read_text())["scan"][str(path)]["count"] == 2


def test_an_unwritable_state_dir_costs_nothing_but_the_cache(projects_dir, monkeypatch):
    _transcript(projects_dir)
    monkeypatch.setattr(tasks_mod, "_scan_cache_path",
                        lambda: "/nonexistent-root/nope/tasks-scan.json")
    tasks_mod.warm()  # must not raise
    assert [r["key"] for r in tasks_mod._task_rows()] == ["sess-a"]


def test_a_transcript_rewritten_to_the_same_size_is_read_again(projects_dir):
    """Size alone used to be the hit test, and a compaction that lands on the
    same byte count kept the old tail for the life of the process — and, once
    records outlive the process, for good. mtime is part of the key now."""
    path = _transcript(projects_dir)
    tasks_mod.warm()
    before = path.read_bytes()
    assert tasks_mod._SCAN[str(path)]["title"] == "Pull today's news"
    # Same length, one letter different in the ai-title.
    after = before.replace(b"Pull today's news", b"Pull today's newz")
    assert len(after) == len(before)
    path.write_bytes(after)
    os.utime(path, (OLD + 60, OLD + 60))

    _new_process()
    tasks_mod.load_scan_cache()
    rows = tasks_mod._task_rows()

    assert rows[0]["title"] == "Pull today's newz"


def test_an_incomplete_head_is_not_persisted(projects_dir, state_dir):
    """A head with no prompt yet is retried in-process only when the file grows;
    a restart used to give it a fresh parse. Persisting it would end that."""
    good = _transcript(projects_dir, "sess-a")
    empty = projects_dir / "-encoded-sess-b"
    empty.mkdir()
    bare = empty / "sess-b.jsonl"
    bare.write_text('{"type":"summary","summary":"nothing here"}\n')
    os.utime(bare, (OLD, OLD))
    tasks_mod.warm()

    data = json.loads((state_dir / tasks_mod.SCAN_CACHE_FILE).read_text())
    assert str(good) in data["head"]
    assert str(bare) not in data["head"], "no prompt, no timestamp: left for the next process"


def test_an_older_listing_never_overwrites_the_desk_after_a_newer_one(projects_dir, monkeypatch):
    """Two listings can leave the lock in one order and reach the desk in the
    other. `observe` prunes what it does not see, so the older set must lose."""
    from fused_render import current_apps
    _transcript(projects_dir)
    seen = []
    monkeypatch.setattr(current_apps, "observe", lambda rows: seen.append(len(rows)))
    tasks_mod._task_rows()
    assert seen == [1]
    # Pretend a newer build already spoke: an older ticket must stay quiet.
    monkeypatch.setattr(tasks_mod, "_OBSERVED_SEQ", tasks_mod._BUILD_SEQ + 5)
    tasks_mod._task_rows()
    assert seen == [1], "the stale ticket did not reach the desk"


def test_a_save_before_the_load_leaves_the_file_alone(projects_dir, state_dir):
    """Shutdown can beat the warm thread. A process that never read the file
    has empty caches, and writing them would throw away every offset the last
    process saved (Bugbot, #1081)."""
    _transcript(projects_dir)
    tasks_mod.warm()
    cache = state_dir / tasks_mod.SCAN_CACHE_FILE
    before = cache.read_text()
    assert json.loads(before)["scan"]

    _new_process()
    tasks_mod.save_scan_cache()  # the shutdown handler, before any load
    assert cache.read_text() == before

    tasks_mod.load_scan_cache()
    tasks_mod.save_scan_cache()  # after a load, saving is fair again
    assert json.loads(cache.read_text())["scan"]


def test_a_head_comes_back_only_for_an_unchanged_file(projects_dir, state_dir):
    """`head` keys on size alone, so a same-size rewrite kept a stale cwd for
    the life of a process and a restart healed it. The file must not take that
    away: a head is imported only when size AND mtime still match the scan
    record saved beside it (Bugbot, #1081)."""
    path = _transcript(projects_dir)
    tasks_mod.warm()
    assert tasks_store.head(str(path))[0] == "/home/me/proj"
    before = path.read_bytes()
    # Same length, different folder — nine characters each.
    after = before.replace(b"/home/me/proj", b"/home/me/othr")
    assert len(after) == len(before)
    path.write_bytes(after)
    os.utime(path, (OLD + 60, OLD + 60))

    _new_process()
    tasks_mod.load_scan_cache()
    assert str(path) not in tasks_store._HEAD_CACHE, "stale head left for a fresh parse"
    rows = tasks_mod._task_rows()
    assert rows[0]["project"] == "/home/me/othr"
