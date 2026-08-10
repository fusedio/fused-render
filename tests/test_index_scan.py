"""The scan itself: one-directory scanning, the mtime reuse rule, and a whole
run end to end (in-process — the detached spawn is tested separately).

See fused_render/index/specs/scan.md and scan-incremental.md.
"""
import json
import os
import time

import pyarrow.parquet as pq
import pytest

from fused_render.index.config import IndexConfig
from fused_render.index.ignore import IgnoreRules, MountGuard
from fused_render.index.scan import keep_subdirs, run_scan, scan_dir_once
from fused_render.index.store import load_dir_cache, read_manifest


def _cfg(tmp_path, **over):
    return IndexConfig(dir=str(tmp_path / "ix"), **over)


def _guard(tmp_path):
    return MountGuard(mounts_dir=str(tmp_path / "nowhere-mounts"))


def _tree(root):
    (root / "a.txt").write_text("aa", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.md").write_text("b", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x", encoding="utf-8")


# -- scan_dir_once -------------------------------------------------------------

def test_scan_dir_once_returns_rows_and_subdirs(tmp_path):
    _tree(tmp_path)
    rules = IgnoreRules([])
    kind, payload, subs = scan_dir_once(str(tmp_path), {}, rules, _guard(tmp_path))
    sig, rows, total, mtime_ns, n_subdirs = payload
    assert kind == "s"
    assert [r[2] for r in rows] == ["a.txt"]
    assert rows[0][3] == "txt"  # ext lowercased, dot stripped
    assert total == 2
    assert sorted(subs) == sorted([str(tmp_path / "node_modules"), str(tmp_path / "sub")])
    assert n_subdirs == 2


def test_scan_dir_once_prunes_ignored_subdirs(tmp_path):
    _tree(tmp_path)
    kind, payload, subs = scan_dir_once(
        str(tmp_path), {}, IgnoreRules(["node_modules"]), _guard(tmp_path))
    assert subs == [str(tmp_path / "sub")]
    assert payload[4] == 1  # n_subdirs is the POST-prune count


def test_scan_dir_once_never_follows_symlinks(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.txt").write_text("x", encoding="utf-8")
    os.symlink(tmp_path / "real", tmp_path / "link")
    os.symlink(tmp_path / "real" / "f.txt", tmp_path / "linkf")
    kind, payload, subs = scan_dir_once(
        str(tmp_path), {}, IgnoreRules([]), _guard(tmp_path))
    assert subs == [str(tmp_path / "real")]
    assert payload[1] == []  # the file symlink is not a file row either


def test_unchanged_leaf_costs_one_stat(tmp_path):
    """The reuse rule: an unchanged leaf directory is stat'd and nothing
    else — no scandir, no per-file stat, no rewritten rows."""
    d = tmp_path / "leaf"
    d.mkdir()
    (d / "f.txt").write_text("x", encoding="utf-8")
    mtime_ns = os.stat(d).st_mtime_ns
    cache = {str(d): (mtime_ns, 1, 0)}
    real_scandir = os.scandir
    calls = []

    def watched(p=".", *a, **k):
        calls.append(str(p))
        return real_scandir(p, *a, **k)

    try:
        os.scandir = watched
        kind, payload, subs = scan_dir_once(
            str(d), cache, IgnoreRules([]), _guard(tmp_path))
    finally:
        os.scandir = real_scandir
    assert (kind, payload, subs) == ("u", 1, [])
    assert calls == []


def test_unchanged_non_leaf_recurses_but_reuses_its_rows(tmp_path):
    d = tmp_path / "parent"
    (d / "child").mkdir(parents=True)
    (d / "f.txt").write_text("x", encoding="utf-8")
    cache = {str(d): (os.stat(d).st_mtime_ns, 1, 1)}
    kind, payload, subs = scan_dir_once(
        str(d), cache, IgnoreRules([]), _guard(tmp_path))
    assert kind == "u"
    assert payload == 1              # cached file count carried forward
    assert subs == [str(d / "child")]  # still recursed into


def test_changed_directory_is_rescanned(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "f.txt").write_text("x", encoding="utf-8")
    cache = {str(d): (os.stat(d).st_mtime_ns + 1, 1, 0)}
    kind, _payload, _subs = scan_dir_once(
        str(d), cache, IgnoreRules([]), _guard(tmp_path))
    assert kind == "s"


def test_unknown_subdir_count_forces_one_rescan(tmp_path):
    """n_subdirs == -1 is a pre-upgrade cache row: rescan once so the count
    backfills, or a folder re-included by an ignore edit stays invisible."""
    d = tmp_path / "p"
    d.mkdir()
    cache = {str(d): (os.stat(d).st_mtime_ns, 0, -1)}
    kind, _p, _s = scan_dir_once(str(d), cache, IgnoreRules([]), _guard(tmp_path))
    assert kind == "s"


def test_unreadable_directory_is_skipped_not_fatal(tmp_path):
    kind, payload, subs = scan_dir_once(
        str(tmp_path / "gone"), {}, IgnoreRules([]), _guard(tmp_path))
    assert (kind, payload, subs) == (None, None, [])


def test_keep_subdirs_drops_skip_dirs_ignored_and_mount_paths(tmp_path):
    guard = MountGuard(mounts_dir=str(tmp_path / "mounts"))
    subs = [str(tmp_path / "ok"), str(tmp_path / "mounts" / "s3"),
            str(tmp_path / "node_modules"), "/proc"]
    assert keep_subdirs(subs, IgnoreRules(["node_modules"]), guard) == [
        str(tmp_path / "ok")]


# -- a whole run ---------------------------------------------------------------

def _run(cfg, root, full=False, run_name="run"):
    run_dir = os.path.join(cfg.runs_dir, run_name)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        json.dump({"root": root, "full": full, "started": 0,
                   "config": cfg.to_dict()}, f)
    run_scan(run_dir)
    return run_dir


def _events(run_dir):
    with open(os.path.join(run_dir, "events.jsonl")) as f:
        return [json.loads(line) for line in f if line.strip()]


def _summary(run_dir):
    for ev in reversed(_events(run_dir)):
        if ev.get("type") == "run_end":
            return ev
    raise AssertionError("no run_end event")


def test_a_run_indexes_the_tree_and_skips_ignored_folders(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    run_dir = _run(cfg, str(src))
    end = _summary(run_dir)
    assert end["msg"] == "complete", end.get("error")
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    names = pq.read_table(part).column("path").to_pylist()
    assert names == [str(src / "a.txt"), str(src / "sub" / "b.md")]


def test_a_second_run_carries_unchanged_directories_forward(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    _run(cfg, str(src))
    run_dir = _run(cfg, str(src), run_name="run2")
    end = _summary(run_dir)
    assert end["summary"]["unchanged_dirs"] >= 2   # nothing touched the tree
    assert end["summary"]["reused_files"] >= 2
    assert any(e.get("msg", "").startswith("scanning (incremental")
               or e.get("msg", "").startswith("scanning (fsevents")
               for e in _events(run_dir))


def test_a_rescan_picks_up_a_new_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    _run(cfg, str(src))
    (src / "sub" / "new.txt").write_text("n", encoding="utf-8")
    cfg2 = _cfg(tmp_path, ignore=["node_modules"])
    run_dir = _run(cfg2, str(src), run_name="run2")
    assert _summary(run_dir)["msg"] == "complete"
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    assert str(src / "sub" / "new.txt") in pq.read_table(part).column("path").to_pylist()


def test_changing_the_ignore_rules_forces_a_full_rescan(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    _run(cfg, str(src))
    wider = _cfg(tmp_path, ignore=["node_modules", "sub"])
    run_dir = _run(wider, str(src), run_name="run2")
    assert any(e.get("msg") == "ignore rules changed - full rescan"
               for e in _events(run_dir))
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    # the newly-ignored folder's rows are gone without a manual purge
    assert pq.read_table(part).column("path").to_pylist() == [str(src / "a.txt")]


def test_a_cancelled_run_leaves_the_index_untouched(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    cfg = _cfg(tmp_path)
    run_dir = os.path.join(cfg.runs_dir, "cancelled")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        json.dump({"root": str(src), "full": False, "started": 0,
                   "config": cfg.to_dict()}, f)
    open(os.path.join(run_dir, "cancel"), "w").close()
    run_scan(run_dir)
    assert _summary(run_dir)["msg"] == "cancelled"
    assert read_manifest(cfg) is None  # no compaction happened


def test_a_failing_run_terminates_the_event_log(tmp_path):
    """A crash must still close the log, or the poller waits forever."""
    src = tmp_path / "src"
    src.mkdir()
    cfg = _cfg(tmp_path)
    run_dir = os.path.join(cfg.runs_dir, "boom")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        json.dump({"root": str(src), "full": False, "started": 0,
                   "config": cfg.to_dict()}, f)
    import fused_render.index.scan as scan_mod

    def boom(*a, **k):
        raise RuntimeError("compaction exploded")

    orig = scan_mod.compact
    scan_mod.compact = boom
    try:
        run_scan(run_dir)
    finally:
        scan_mod.compact = orig
    end = _summary(run_dir)
    assert end["msg"] == "failed"
    assert "compaction exploded" in end["error"]


def test_threaded_scan_never_drops_entries_from_a_slow_worker(tmp_path, monkeypatch):
    """Regression: `pending` was counted per-submit, so a fast worker could
    hit 0 and latch `done` (never cleared) while later dirs were still
    unsubmitted; a quiet 0.2s in the drain loop then closed the sink while a
    slow worker was still producing, silently dropping its rows."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from fused_render.index import scan as scan_mod

    slow_started = threading.Event()

    def fake_scan_dir_once(d, cache, rules, guard, devs, root_dev):
        if d == "/fast":
            return None, None, []  # finishes instantly, produces nothing
        slow_started.set()
        time.sleep(0.6)  # longer than the drain loop's 0.2s quiet window
        return "s", ("sig", [(d + "/late.txt", d, "late.txt", "txt", 1, 1.0)],
                     1, 1, 0), []

    class InlineFirstPool:
        """Runs the first submit inline (the fast worker beating the submit
        loop, made deterministic); later submits go to real threads."""
        def __init__(self):
            self.real = ThreadPoolExecutor(max_workers=2)
            self.first = True

        def submit(self, fn, *a):
            if self.first:
                self.first = False
                fn(*a)
                return None
            return self.real.submit(fn, *a)

    got = []

    class RecordingSink:
        def add(self, d, kind, payload):
            got.append(d)

    monkeypatch.setattr(scan_mod, "scan_dir_once", fake_scan_dir_once)
    monkeypatch.setattr(scan_mod, "_child_progress", lambda *a, **k: None)
    monkeypatch.setitem(scan_mod._CHILD, "sink", RecordingSink())
    monkeypatch.setitem(scan_mod._CHILD, "cache", {})
    monkeypatch.setitem(scan_mod._CHILD, "pool", InlineFirstPool())
    monkeypatch.setitem(scan_mod._CHILD, "rules", None)
    monkeypatch.setitem(scan_mod._CHILD, "guard", None)
    monkeypatch.setitem(scan_mod._CHILD, "devs", set())
    monkeypatch.setitem(scan_mod._CHILD, "root_dev", None)
    monkeypatch.setitem(scan_mod._CHILD, "cancel_flag",
                        str(tmp_path / "cancel"))

    scan_mod._scan_dirs_threaded(["/fast", "/slow"])
    assert slow_started.is_set()
    assert "/slow" in got
