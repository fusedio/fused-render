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
from fused_render.index.store import (
    load_dir_cache,
    partition_files,
    read_manifest,
)


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


def _package(root):
    app = root / "Cool.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text("x", encoding="utf-8")
    return app


def test_a_package_directory_is_recorded_but_not_descended(tmp_path):
    """The walk treats macOS packages as opaque leaves — one entry, no contents
    (WALK_LEAF_DIR_SUFFIXES) — and the scan had no counterpart, so the index was
    full of Cool.app/Contents/... paths the walk would never emit.

    Dropping the package from the descent list is not the fix on its own: a
    dirs.parquet row exists only for a SCANNED directory, so the package would
    vanish from the corpus entirely and break parity the other way. It is
    recorded, with no file rows, and not listed."""
    app = _package(tmp_path)
    rules, guard = IgnoreRules([]), _guard(tmp_path)
    assert scan_dir_once(str(tmp_path), {}, rules, guard)[2] == [str(app)]
    kind, payload, subs = scan_dir_once(str(app), {}, rules, guard)
    assert kind == "s"           # recorded: one dirs row
    assert payload[1] == []      # no file rows from inside the package
    assert payload[4] == 0
    assert subs == []            # and nothing below it is queued


def _repo(root, name="proj"):
    """A repo whose .git holds the loose files git actually puts there plus a
    subdirectory, so "recorded but not listed" is distinguishable from "pruned as
    an ignored tree" (an ignore rule prunes the subdir but NOT the loose files)."""
    proj = root / name
    git = proj / ".git"
    (git / "objects" / "ab").mkdir(parents=True)
    (git / "objects" / "ab" / "cdef").write_text("blob", encoding="utf-8")
    for loose in ("HEAD", "config", "index"):
        (git / loose).write_text("x", encoding="utf-8")
    (proj / "main.py").write_text("print()", encoding="utf-8")
    return proj, git


def test_dot_git_is_recorded_as_a_leaf_and_never_listed(tmp_path):
    """Repo-ness has to be a queryable index fact (routers/git_repos.py reads
    these rows instead of stat-ing every indexed directory), and it has to cost
    one row: NOT the ~15 loose files directly inside `.git`, which is exactly what
    an ignore rule would have left behind, and certainly not the object
    database."""
    proj, git = _repo(tmp_path)
    rules, guard = IgnoreRules([]), _guard(tmp_path)

    # the repo's own scan offers .git onward — that is how the row gets made
    kind, payload, subs = scan_dir_once(str(proj), {}, rules, guard)
    assert kind == "s"
    assert str(git) in subs
    assert [r[2] for r in payload[1]] == ["main.py"]

    # and .git itself is one row with nothing in it and nothing below it
    kind, payload, subs = scan_dir_once(str(git), {}, rules, guard)
    assert kind == "s"           # recorded
    assert payload[1] == []      # no HEAD/config/index rows
    assert payload[4] == 0
    assert subs == []            # objects/ never queued


def test_a_user_ignore_entry_cannot_delete_the_dot_git_row(tmp_path):
    """An ignore entry buys a scan two things: no descent and no row. For a leaf
    dir the first is already true, so all it can still do is delete the row that
    IS the repo-detection fact — silently emptying the homepage's Repos tab to
    save one stat. `.git` shipped in the default ignore list once, so old saved
    configs really do name it."""
    proj, git = _repo(tmp_path)
    rules, guard = IgnoreRules([".git"]), _guard(tmp_path)
    assert str(git) in scan_dir_once(str(proj), {}, rules, guard)[2]
    # ... and it is still opaque: kept, not descended
    assert scan_dir_once(str(git), {}, rules, guard)[2] == []


def test_an_ignored_dot_git_row_SURVIVES_an_incremental_rescan(tmp_path):
    """The data-loss sequence, end to end: a full rescan writes the `.git` rows,
    then an incremental pass over a config that NAMES `.git` must not purge them.

    keep_subdirs alone was not enough. The cache filter (load_dir_cache) decides
    what an incremental pass carries forward and the journal gate decides what it
    re-adds; with the leaf exemption in only the walk gate, the rows the first scan
    wrote were dropped by the second — so a user whose Repos tab worked lost it on
    the next scan. Worse than a stale list: actively purged."""
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _repo(src, "proj")
    cfg = _cfg(tmp_path, ignore=["node_modules", ".git"])

    _run(cfg, str(src))
    git_dir = str(src / "proj" / ".git")
    dirs = pq.read_table(cfg.dirs_parquet).column("dir").to_pylist()
    assert git_dir in dirs, "the full rescan should record the leaf row"

    # ...and the incremental pass must keep it. Both gates are exercised: the
    # cache filter (whether it is carried forward) and the journal/walk gate
    # (whether it is re-added).
    _run(cfg, str(src), run_name="run2")
    dirs2 = pq.read_table(cfg.dirs_parquet).column("dir").to_pylist()
    assert git_dir in dirs2, "an incremental pass purged the .git row"


def test_load_dir_cache_keeps_an_ignored_leaf_but_drops_a_real_ignored_tree(tmp_path):
    """The cache filter is the sharpest edge: a row missing here is a row the next
    compaction deletes. Leaf exempt, ancestors still vetoing."""
    import pyarrow.parquet as pqmod

    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _repo(src, "proj")
    cfg = _cfg(tmp_path, ignore=["node_modules", ".git"])
    _run(cfg, str(src))

    cache = load_dir_cache(cfg, str(src), pqmod)
    assert str(src / "proj" / ".git") in cache          # leaf: exempt
    assert str(src / "node_modules") not in cache       # ordinary ignore: gone


def test_keep_subdirs_still_honors_ignores_for_non_leaf_dirs(tmp_path):
    """The leaf override is narrow — it decides the verdict on the leaf dir
    ITSELF and changes nothing else, including for a repo sitting inside an
    ignored tree (the walk never reaches its parent to offer it)."""
    guard = _guard(tmp_path)
    rules = IgnoreRules([".git", "node_modules"])
    assert keep_subdirs([str(tmp_path / "node_modules")], rules, guard) == []
    # SKIP_DIRS and the mount guard keep their veto over a leaf dir too
    assert keep_subdirs(["/dev"], rules, guard) == []
    blocked = MountGuard(mounts_dir=str(tmp_path / "m"))
    assert keep_subdirs([str(tmp_path / "m" / "s3" / ".git")], rules, blocked) == []


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


def test_a_run_and_the_walk_agree_about_a_package_directory(tmp_path):
    """Corpus/walk parity: the two sources are meant to be interchangeable, so
    the package appears in both as exactly one leaf entry."""
    from fused_render.index.query import search_under
    from fused_render.server.walk import _walk_bfs

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    app = _package(src)
    cfg = _cfg(tmp_path)
    run_dir = _run(cfg, str(src))
    assert _summary(run_dir)["msg"] == "complete", _summary(run_dir).get("error")

    dirs = pq.read_table(cfg.dirs_parquet).column("dir").to_pylist()
    assert str(app) in dirs
    assert not any(d.startswith(str(app) + "/") for d in dirs)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    assert pq.read_table(part).column("path").to_pylist() == [str(src / "a.txt")]

    corpus = {e["rel"] for e in search_under(cfg, str(src))["entries"]}
    walked = {e["rel"] for e in _walk_bfs(str(src), True) if isinstance(e, dict)}
    assert corpus == walked == {"a.txt", "Cool.app"}


def test_the_fsevents_path_does_not_walk_into_a_package(tmp_path, monkeypatch):
    """The leaf rule has to hold on BOTH scan paths, and the journal one does not
    arrive by descent: it visits whatever directories the OS names, and what the
    OS names inside a package is always a descendant (an app update writes
    Cool.app/Contents/..., Photos writes Foo.photoslibrary/database) — never the
    package itself. A final-component test therefore passes those straight
    through and re-fills the index with the very rows the walk-driven path
    excludes. They are not self-correcting either: _run_fsevents keeps every
    cached dir it did not visit, so once present they survive every later run.

    The hint here is exactly what the journal reports after something writes
    inside the package."""
    from fused_render.index import fsevents

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    app = _package(src)
    cfg = _cfg(tmp_path)
    assert _summary(_run(cfg, str(src)))["msg"] == "complete"

    # Second run takes the journal fast path, with the package's INNER dir
    # reported as changed.
    monkeypatch.setattr(
        fsevents, "hint",
        lambda _cfg, _root: ([], [str(app / "Contents")]))
    run_dir = _run(cfg, str(src), run_name="run2")
    assert _summary(run_dir)["msg"] == "complete", _summary(run_dir).get("error")
    # the run really took the journal path — otherwise this proves nothing
    assert any(e.get("msg", "").startswith("scanning (fsevents")
               for e in _events(run_dir))

    dirs = pq.read_table(cfg.dirs_parquet).column("dir").to_pylist()
    assert str(app) in dirs                                    # still recorded
    assert not any(d.startswith(str(app) + "/") for d in dirs)  # nothing below
    paths = []
    for part in partition_files(cfg):
        paths += pq.read_table(part).column("path").to_pylist()
    assert not any(p.startswith(str(app) + "/") for p in paths), \
        "package internals entered the index through the fsevents path"


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


def test_a_rescan_picks_up_a_new_file(tmp_path, monkeypatch):
    """The mtime-driven incremental path: `sub`'s mtime moved, so it is rescanned
    and the new file lands.

    The FSEvents fast path is pinned OFF, and that is load-bearing rather than
    tidiness. FSEvents is a machine-global, ASYNCHRONOUS journal: with the journal
    live, run 2 takes the fast path and visits only what the OS has already
    reported — so if the report for `sub` has not landed yet, every cached row is
    carried forward and this assertion fails with no bug present. Measured: 4 runs
    in 10 under `-n auto` once neighbouring tests in this file started writing
    enough files to make the journal lag. The fsevents path has its own tests
    (test_the_fsevents_path_does_not_walk_into_a_package), which pin `hint` for the
    same reason in the other direction."""
    from fused_render.index import fsevents

    monkeypatch.setattr(fsevents, "hint", lambda *a, **k: None)
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


def test_a_missing_applied_fingerprint_forces_a_full_rescan(tmp_path):
    """An index with no `ignore_applied.json` must NOT be reconciled
    incrementally, and this is not a theoretical tidiness point.

    Absent used to count as "no change", which was sound while every rule only
    ever REMOVED rows: dropping an ignore pattern is self-purging through the
    filtered cache. A rule that ADDS rows breaks it — a `.git` row appears only by
    visiting the repo directory, and an incremental scan skips exactly that
    directory because its mtime is unchanged. The run would then stamp the new
    fingerprint over an index that never grew the rows, and /api/git-repos, which
    trusts that stamp, would report zero repositories forever.
    """
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    _run(cfg, str(src))
    # a repo appears, and the fingerprint is lost (an index predating the file)
    (src / "proj" / ".git").mkdir(parents=True)
    os.remove(cfg.applied_ignore_json)

    run_dir = _run(cfg, str(src), run_name="run2")
    msgs = [e.get("msg") for e in _events(run_dir)]
    assert "no applied rules fingerprint - full rescan" in msgs
    assert "scanning (full)" in msgs
    dirs = pq.read_table(cfg.dirs_parquet).column("dir").to_pylist()
    assert str(src / "proj" / ".git") in dirs


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
