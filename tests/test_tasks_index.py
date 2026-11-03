"""tasks_index: one sqlite file per derivation stamp, pruned."""
import os
import threading
import time

import pytest

from fused_render import tasks_index, tasks_store

A = "a" * 16
B = "b" * 16


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tasks_index, "_fingerprint", lambda: A)
    monkeypatch.setattr(tasks_index, "_pruned", False)
    monkeypatch.setattr(tasks_index, "_enabled", False)
    tasks_index._saved.clear()
    saved_heads = dict(tasks_store._HEAD_CACHE)
    yield tmp_path
    tasks_store._HEAD_CACHE.clear()
    tasks_store._HEAD_CACHE.update(saved_heads)
    tasks_index._saved.clear()


def _transcript(tmp_path, name):
    p = tmp_path / name
    p.write_text("x" * 10)
    size = os.stat(p).st_size
    tasks_store._HEAD_CACHE[str(p)] = (size, "/cwd", 1.0, "prompt", None, None, True)
    return str(p), {"size": size}


def test_path_is_keyed_by_fingerprint(state, monkeypatch):
    assert "tasks-index-aaaa" in tasks_index.path()
    monkeypatch.setattr(tasks_index, "_fingerprint", lambda: B)
    assert "tasks-index-bbbb" in tasks_index.path()


def test_two_stamps_do_not_evict_each_other(state, monkeypatch):
    pa, sa = _transcript(state, "a.jsonl")
    pb, sb = _transcript(state, "b.jsonl")
    tasks_index.seed({}, threading.Lock())  # enables saving
    assert tasks_index.save({pa: sa}, threading.Lock()) == 1

    monkeypatch.setattr(tasks_index, "_fingerprint", lambda: B)
    tasks_index._saved.clear()
    tasks_index.seed({}, threading.Lock())
    assert tasks_index.save({pb: sb}, threading.Lock()) == 1

    monkeypatch.setattr(tasks_index, "_fingerprint", lambda: A)
    tasks_index._saved.clear()
    cache = {}
    hits, total = tasks_index.seed(cache, threading.Lock())
    assert hits >= 1 and pa in cache and pb not in cache


def _fake(tmp_path, name, age_days):
    f = tmp_path / name
    f.write_text("x")
    t = time.time() - age_days * 86400
    os.utime(f, (t, t))
    return f


def test_prune_keeps_newest_and_removes_legacy(state):
    tasks_index.seed({}, threading.Lock())  # creates the current file
    cur = os.path.basename(tasks_index.path())
    fakes = [_fake(state, f"tasks-index-{i:016x}.sqlite", i) for i in range(1, 7)]
    old = _fake(state, "tasks-index-ffffffffffffffff.sqlite", 30)
    legacy = _fake(state, "tasks-index.sqlite", 1)
    legacy_wal = _fake(state, "tasks-index.sqlite-wal", 1)
    tasks_index._pruned = False
    tasks_index._prune()
    left = {p.name for p in state.iterdir() if p.name.startswith("tasks-index")}
    assert cur in left
    assert not legacy.exists() and not legacy_wal.exists()
    assert not old.exists()
    kept = [f for f in fakes if f.exists()]
    assert len(kept) == 4 and kept == fakes[:4]


def test_prune_never_raises(state, monkeypatch):
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(state / "missing"))
    tasks_index._pruned = False
    assert tasks_index._prune() is None
