"""Keeping the open folder's slice of the index fresh.

See fused_render/index/specs/scan-incremental.md §5. The whole feature is one
mtime comparison plus pacing: the trigger reuses the ordinary incremental scan,
so what is worth guarding here is the decision to fire it — every gate that
must refuse, in the order that makes the expensive ones unreachable.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index import freshness, runner
from fused_render.index.config import IndexConfig
from fused_render.index.freshness import (
    MIN_INTERVAL_S,
    QUIET_S,
    enclosing_root,
    indexed_mtime_ns,
    note_folder_opened,
)
from fused_render.index.store import Sink, compact

NS = 1_000_000_000


def _index(tmp_path, root, dirs):
    """A real index whose dirs.parquet holds `dirs` = {abs dir: mtime_ns}."""
    cfg = IndexConfig(dir=str(tmp_path / "ix"))
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    for d, mtime_ns in dirs.items():
        sink.add(d, "s", ("sig", [], 0, mtime_ns, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


@pytest.fixture()
def spawned(monkeypatch):
    """runner.start recorded instead of spawning a worker."""
    calls = []

    def fake_start(cfg, root, full=False):
        calls.append({"root": root, "full": full})
        return {"run_id": "r1", "root": root}

    monkeypatch.setattr(runner, "start", fake_start)
    # No mounts records anywhere near tmp_path, so the guard is a no-op here
    # except in the test that points it at one.
    monkeypatch.setattr(runner, "_mounts_dir", lambda: "/nonexistent-mounts")
    return calls


def _tree(tmp_path, rel):
    d = tmp_path / rel
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


# -- the staleness check -------------------------------------------------------

def test_the_indexed_mtime_of_a_recorded_directory_is_read_back(tmp_path):
    cfg = _index(tmp_path, "/r", {"/r": 500 * NS, "/r/sub": 700 * NS})
    assert indexed_mtime_ns(cfg, "/r/sub") == 700 * NS


def test_a_directory_the_index_never_recorded_reads_as_unknown(tmp_path):
    cfg = _index(tmp_path, "/r", {"/r": 500 * NS})
    assert indexed_mtime_ns(cfg, "/r/never-scanned") is None


def test_an_index_that_was_never_built_reads_as_unknown(tmp_path):
    assert indexed_mtime_ns(IndexConfig(dir=str(tmp_path / "ix")), "/r") is None


# -- which root a folder belongs to -------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/home/me/code/app", "/home/me/code"),
    ("/home/me/code", "/home/me/code"),
    ("/home/me/other", None),
    # segment-wise, so a sibling with the root as a name prefix is not inside it
    ("/home/me/code-old/app", None),
])
def test_enclosing_root_is_matched_segment_wise(path, expected):
    assert enclosing_root(["/home/me/code"], path) == expected


def test_the_deepest_configured_root_wins():
    """Roots may nest. The scan that answers for the folder soonest is the
    narrower one, and firing the outer root as well would scan its subtree
    twice."""
    roots = ["/a", "/a/b"]
    assert enclosing_root(roots, "/a/b/c") == "/a/b"


# -- the trigger --------------------------------------------------------------

def test_a_folder_whose_mtime_moved_since_the_scan_triggers_a_rescan(
        tmp_path, spawned):
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) == root
    assert spawned == [{"root": root, "full": False}]


def test_an_unchanged_folder_triggers_nothing(tmp_path, spawned):
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS,
                                 sub: os.stat(sub).st_mtime_ns})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) is None
    assert spawned == []


def test_a_folder_the_index_never_recorded_triggers_nothing(tmp_path, spawned):
    """An uncovered folder already falls back to the live walk (query.md §6),
    so a scan buys its search nothing — and treating "absent" as stale would
    make every folder outside the index a trigger."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) is None
    assert spawned == []


def test_an_unknown_recorded_mtime_triggers_nothing(tmp_path, spawned):
    """A partition written before mtime_ns existed reads 0 (store._compact_locked).
    Comparing against 0 would read as "stale forever" and fire on every open."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 0})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) is None
    assert spawned == []


def test_a_folder_that_changed_moments_ago_is_left_to_settle(tmp_path, spawned):
    """The quiet period. A build directory's mtime moves continuously, so it is
    never quiet and never triggers — which is what stops it queueing scan after
    scan."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    now = os.stat(sub).st_mtime + QUIET_S - 1
    assert note_folder_opened(cfg, sub, [root], now=now) is None
    assert spawned == []


def test_a_root_scanned_within_the_floor_is_not_rescanned(tmp_path, spawned):
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    runner._record_scan(cfg, root)
    assert runner.last_scan(cfg, root) is not None
    # `now` is in the future relative to the record just written, so express the
    # floor from the record itself.
    at = runner.last_scan(cfg, root) + MIN_INTERVAL_S - 1
    assert note_folder_opened(cfg, sub, [root], now=max(now, at)) is None
    assert spawned == []


def test_a_folder_outside_every_configured_root_triggers_nothing(
        tmp_path, spawned):
    root = _tree(tmp_path, "root")
    outside = _tree(tmp_path, "elsewhere")
    cfg = _index(tmp_path, root, {root: 1 * NS, outside: 1 * NS})
    now = os.stat(outside).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, outside, [root], now=now) is None
    assert spawned == []


def test_a_live_scan_of_the_root_is_not_joined_by_a_second_one(
        tmp_path, spawned, monkeypatch):
    """runner.start would join it, but only for an EXACT root-string match, and
    a triggered scan must not be the thing that discovers that. Refuse here."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    monkeypatch.setattr(runner, "active_run",
                        lambda cfg, r: {"run_id": "live", "root": r})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) is None
    assert spawned == []


def test_the_scan_it_starts_is_of_the_configured_root_not_the_open_folder(
        tmp_path, monkeypatch):
    """`scans.json` is keyed by root string and read by the startup debounce,
    so a per-folder root would both pollute it and defeat runner.start's
    exact-match join. Let the real runner.start record, and check the key."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    monkeypatch.setattr(runner, "_mounts_dir", lambda: "/nonexistent-mounts")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: _Spawned())
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) == root
    assert runner.last_scan(cfg, root) is not None
    assert runner.last_scan(cfg, sub) is None


class _Spawned:
    pid = 4242


def test_a_mount_backed_folder_is_refused_without_touching_the_kernel(
        tmp_path, spawned, monkeypatch):
    """os.stat on a wedged rclone mount blocks the request thread forever (this
    repo's documented mount-wedge class), so the guard has to come first — and
    it is pure string work against the mount records."""
    mounts = _tree(tmp_path, "home/mounts")
    root = _tree(tmp_path, "home")
    under = _tree(tmp_path, "home/mounts/bucket/data")
    cfg = _index(tmp_path, root, {root: 1 * NS, under: 1 * NS})
    monkeypatch.setattr(runner, "_mounts_dir", lambda: mounts)

    def boom(_p):
        raise AssertionError("stat reached a mount-backed path")

    monkeypatch.setattr(freshness.os, "stat", boom)
    now = 10 ** 10
    assert note_folder_opened(cfg, under, [root], now=now) is None
    assert spawned == []


def test_a_vanished_folder_is_not_an_error(tmp_path, spawned):
    root = _tree(tmp_path, "root")
    gone = os.path.join(root, "gone")
    cfg = _index(tmp_path, root, {root: 1 * NS, gone: 1 * NS})
    assert note_folder_opened(cfg, gone, [root], now=10 ** 10) is None
    assert spawned == []
