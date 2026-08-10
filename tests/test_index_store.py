"""The on-disk index store: shard sinks, the directory reuse cache, and the
duckdb compaction that merges shards into path-sorted partitions.

See fused_render/index/specs/index-store.md.
"""
import json
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index.config import IndexConfig
from fused_render.index.store import (
    Sink,
    applied_ignore_sig,
    compact,
    load_dir_cache,
    read_manifest,
    save_applied_ignore,
)


def _cfg(tmp_path, **over):
    return IndexConfig(dir=str(tmp_path / "ix"), **over)


def _scanned(sig, rows, total, mtime_ns, n_subdirs):
    return (sig, rows, total, mtime_ns, n_subdirs)


def _row(path, size=10, mtime=100.0):
    d, name = path.rsplit("/", 1)
    ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
    return (path, d, name, ext, size, mtime)


def _shard(tmp_path, cfg, entries, keep=()):
    """Write one batch of scan output into a fresh shards dir."""
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    for d, payload in entries:
        sink.add(d, "s", payload)
    for d, n in keep:
        sink.add(d, "u", n)
    sink.close()
    return shards


# -- Sink ---------------------------------------------------------------------

def test_sink_writes_file_dir_and_keep_shards(tmp_path):
    cfg = _cfg(tmp_path)
    shards = _shard(
        tmp_path, cfg,
        [("/a", _scanned("sig-a", [_row("/a/one.txt")], 10, 111, 1))],
        keep=[("/a/sub", 3)],
    )
    names = sorted(os.listdir(shards))
    assert any(n.startswith("shard-") for n in names)
    assert any(n.startswith("_dirs-") for n in names)
    assert any(n.startswith("_keep-") for n in names)
    files = pq.read_table(os.path.join(shards, [n for n in names if n.startswith("shard-")][0]))
    assert files.column("path").to_pylist() == ["/a/one.txt"]
    assert files.column("ext").to_pylist() == ["txt"]
    kept = pq.read_table(os.path.join(shards, [n for n in names if n.startswith("_keep-")][0]))
    assert kept.column("dir").to_pylist() == ["/a/sub"]


def test_sink_counts_reused_files_from_unchanged_dirs(tmp_path):
    cfg = _cfg(tmp_path)
    sink = Sink(str(tmp_path / "s"), "t", pa, pq, cfg.shard_rows)
    os.makedirs(tmp_path / "s", exist_ok=True)
    sink.add("/a", "s", _scanned("sig", [_row("/a/x.txt")], 10, 1, 0))
    sink.add("/b", "u", 7)
    assert (sink.dirs, sink.files, sink.reused, sink.udirs) == (2, 1, 7, 1)


# -- the directory reuse cache ------------------------------------------------

def _write_dirs(cfg, rows, with_mtime=True):
    os.makedirs(cfg.dir, exist_ok=True)
    cols = {"dir": [], "sig": [], "n_files": [], "total_size": [], "n_subdirs": []}
    if with_mtime:
        cols["mtime_ns"] = []
    for d, mtime_ns, n_files, n_subdirs in rows:
        cols["dir"].append(d)
        cols["sig"].append("s")
        cols["n_files"].append(n_files)
        cols["total_size"].append(0)
        cols["n_subdirs"].append(n_subdirs)
        if with_mtime:
            cols["mtime_ns"].append(mtime_ns)
    pq.write_table(pa.table(cols), cfg.dirs_parquet)


def test_load_dir_cache_scopes_to_the_root_subtree(tmp_path):
    cfg = _cfg(tmp_path)
    _write_dirs(cfg, [("/r", 1, 2, 1), ("/r/sub", 2, 1, 0), ("/other", 3, 1, 0)])
    cache = load_dir_cache(cfg, "/r", pq)
    assert sorted(cache) == ["/r", "/r/sub"]
    assert cache["/r"] == (1, 2, 1)


def test_load_dir_cache_drops_ignored_subtrees(tmp_path):
    """Filtering the cache is what makes a newly-ignored folder self-purging:
    it never reaches the keep list, so compaction drops its file rows."""
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    _write_dirs(cfg, [("/r", 1, 0, 1), ("/r/node_modules", 2, 5, 0),
                      ("/r/node_modules/pkg", 3, 9, 0)])
    assert sorted(load_dir_cache(cfg, "/r", pq)) == ["/r"]


def test_load_dir_cache_is_empty_without_mtime_ns(tmp_path):
    cfg = _cfg(tmp_path)
    _write_dirs(cfg, [("/r", 0, 1, 0)], with_mtime=False)
    assert load_dir_cache(cfg, "/r", pq) == {}


def test_load_dir_cache_is_empty_without_a_dirs_file(tmp_path):
    assert load_dir_cache(_cfg(tmp_path), "/r", pq) == {}


# -- compaction ---------------------------------------------------------------

def test_compact_writes_sorted_partitions_and_a_manifest(tmp_path):
    cfg = _cfg(tmp_path)
    shards = _shard(tmp_path, cfg, [
        ("/r/b", _scanned("s", [_row("/r/b/2.txt"), _row("/r/b/1.txt")], 20, 2, 0)),
        ("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 1)),
    ])
    summary = compact(cfg, "/r", shards, pa, pq)
    assert summary["rows"] == 3
    m = read_manifest(cfg)
    assert m["last_root"] == "/r"
    part = os.path.join(cfg.files_dir, m["partitions"][0]["file"])
    assert pq.read_table(part).column("path").to_pylist() == [
        "/r/a.txt", "/r/b/1.txt", "/r/b/2.txt"]
    assert m["partitions"][0]["min"] == "/r/a.txt"
    assert m["partitions"][0]["max"] == "/r/b/2.txt"
    # the shards dir is cleaned up once its rows are in the index
    assert not os.path.isdir(shards)


def test_compact_dedupes_by_path_keeping_the_newest_mtime(tmp_path):
    cfg = _cfg(tmp_path)
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt", size=1, mtime=100.0)], 1, 1, 0))]),
        pa, pq)
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt", size=2, mtime=200.0)], 2, 2, 0))]),
        pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    t = pq.read_table(part)
    assert t.num_rows == 1
    assert t.column("size").to_pylist() == [2]


def test_compact_keeps_rows_outside_the_scan_root(tmp_path):
    """Multi-root indexes work: `outside` preserves trees other than this
    run's root, so scanning ~/Documents after ~/code keeps both."""
    cfg = _cfg(tmp_path)
    compact(cfg, "/one", _shard(tmp_path, cfg, [
        ("/one", _scanned("s", [_row("/one/a.txt")], 10, 1, 0))]), pa, pq)
    compact(cfg, "/two", _shard(tmp_path, cfg, [
        ("/two", _scanned("s", [_row("/two/b.txt")], 10, 1, 0))]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    assert pq.read_table(part).column("path").to_pylist() == ["/one/a.txt", "/two/b.txt"]


def test_compact_carries_kept_dirs_forward_and_drops_unmentioned_ones(tmp_path):
    cfg = _cfg(tmp_path)
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 2)),
        ("/r/keep", _scanned("s", [_row("/r/keep/k.txt")], 10, 2, 0)),
        ("/r/gone", _scanned("s", [_row("/r/gone/g.txt")], 10, 3, 0)),
    ]), pa, pq)
    # second run: /r rescanned, /r/keep carried forward, /r/gone in neither
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt")], 10, 9, 1))],
        keep=[("/r/keep", 1)]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    assert pq.read_table(part).column("path").to_pylist() == [
        "/r/a.txt", "/r/keep/k.txt"]
    assert summary["removed_dirs"] == 1


def test_compact_skips_the_rewrite_when_nothing_changed(tmp_path):
    cfg = _cfg(tmp_path)
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 0))]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    before = os.stat(part).st_mtime_ns
    shards = str(tmp_path / "run2" / "shards")
    os.makedirs(shards)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    sink.add("/r", "u", 1)
    sink.close()
    summary = compact(cfg, "/r", shards, pa, pq)
    assert summary["skipped_rewrite"] is True
    assert os.stat(part).st_mtime_ns == before


def test_compact_reports_root_totals(tmp_path):
    cfg = _cfg(tmp_path)
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt", size=5), _row("/r/b.txt", size=7)],
                        12, 1, 0))]), pa, pq)
    assert summary["root_files"] == 2
    assert summary["root_size"] == 12
    assert summary["root_dirs"] == 1


def test_compact_emits_phase_events_when_given_a_sink(tmp_path):
    cfg = _cfg(tmp_path)
    seen = []
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 0))]), pa, pq,
        emit=lambda **ev: seen.append(ev))
    assert any(e.get("msg") == "writing index" for e in seen)


# -- readability while a scan is compacting -----------------------------------

def test_a_compaction_writes_a_new_generation_beside_the_old_one(tmp_path):
    """A rescan must never make the index unreadable. Partitions are named per
    generation and the manifest is swapped atomically last, so a reader either
    sees the whole old set or the whole new one — never a half-written mix."""
    cfg = _cfg(tmp_path)
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 0))]), pa, pq)
    first = read_manifest(cfg)
    first_files = [p["file"] for p in first["partitions"]]
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt"), _row("/r/b.txt")], 20, 2, 0))]),
        pa, pq)
    second = read_manifest(cfg)
    assert second["generation"] > first["generation"]
    assert [p["file"] for p in second["partitions"]] != first_files
    # a reader that read the OLD manifest a moment before the swap can still
    # open every file it named
    for name in first_files:
        assert os.path.exists(os.path.join(cfg.files_dir, name))


def test_a_third_compaction_reclaims_the_generation_before_last(tmp_path):
    cfg = _cfg(tmp_path)
    names = []
    for i in range(3):
        compact(cfg, "/r", _shard(tmp_path, cfg, [
            ("/r", _scanned("s", [_row(f"/r/a{i}.txt")], 10, i + 1, 0))],
            ), pa, pq)
        names.append([p["file"] for p in read_manifest(cfg)["partitions"]])
    live = set(os.listdir(cfg.files_dir))
    assert set(names[2]) <= live      # current generation
    assert set(names[1]) <= live      # the one a live reader may still hold
    assert not (set(names[0]) & live)  # older than that: reclaimed


def test_compact_reads_the_previous_index_through_the_manifest(tmp_path):
    """A stray parquet left in the files dir must not become index rows: the
    manifest, not a glob, says what the index IS."""
    cfg = _cfg(tmp_path)
    compact(cfg, "/one", _shard(tmp_path, cfg, [
        ("/one", _scanned("s", [_row("/one/a.txt")], 10, 1, 0))]), pa, pq)
    stray = os.path.join(cfg.files_dir, "part-99999-99999.parquet")
    file_schema, _ = __import__("fused_render.index.store", fromlist=["schemas"]).schemas(pa)
    pq.write_table(pa.table({k: [v] for k, v in zip(
        file_schema.names, ["/one/ghost.txt", "/one", "ghost.txt", "txt", 1, 1.0])},
        schema=file_schema), stray)
    compact(cfg, "/two", _shard(tmp_path, cfg, [
        ("/two", _scanned("s", [_row("/two/b.txt")], 10, 1, 0))]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    assert pq.read_table(part).column("path").to_pylist() == ["/one/a.txt", "/two/b.txt"]


# -- the applied-ignore fingerprint -------------------------------------------

def test_applied_ignore_sig_round_trips(tmp_path):
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    assert applied_ignore_sig(cfg, "/r") is None  # nothing built yet
    save_applied_ignore(cfg, "/r")
    assert applied_ignore_sig(cfg, "/r") == cfg.rules.sig()
    assert json.load(open(cfg.applied_ignore_json))["patterns"] == ["node_modules"]


def test_applied_ignore_sig_is_per_root(tmp_path):
    """A single global sig was stamped by whichever root full-rescanned
    first, after which every other root's scan looked already-reconciled and
    kept its stale cache — re-included folders stayed permanently missing.
    Each root now records the rules ITS slice was built under."""
    old = _cfg(tmp_path, ignore=["node_modules"])
    save_applied_ignore(old, "/a")
    new = _cfg(tmp_path, ignore=["node_modules", "target"])
    save_applied_ignore(new, "/b")
    assert applied_ignore_sig(new, "/a") == old.rules.sig()  # /a still stale
    assert applied_ignore_sig(new, "/b") == new.rules.sig()
    # the rootless form answers "does ANY root differ?" for needs_rescan
    assert applied_ignore_sig(new) != new.rules.sig()
    save_applied_ignore(new, "/a")
    assert applied_ignore_sig(new) == new.rules.sig()


def test_applied_ignore_sig_reads_the_pre_per_root_format(tmp_path):
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    os.makedirs(cfg.dir, exist_ok=True)
    with open(cfg.applied_ignore_json, "w") as f:
        json.dump({"sig": "old-global-sig", "patterns": []}, f)
    assert applied_ignore_sig(cfg, "/anything") == "old-global-sig"
    assert applied_ignore_sig(cfg) == "old-global-sig"


def test_migrating_the_old_format_keeps_the_other_roots_stale(tmp_path):
    """Stamping ONE root must not migrate the old global sig away from the
    others. Dropping it made them report None, which the router's
    `(sig or current) != current` staleness test reads as up-to-date — so a
    rules edit never rescanned them and their slices stayed built under the
    old rules forever."""
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    os.makedirs(cfg.dir, exist_ok=True)
    with open(cfg.applied_ignore_json, "w") as f:
        json.dump({"sig": "old-global-sig", "patterns": []}, f)
    save_applied_ignore(cfg, "/a")
    assert applied_ignore_sig(cfg, "/a") == cfg.rules.sig()
    # /b was never stamped individually, so the pre-migration sig is still the
    # only thing known about it — and it differs from the current rules.
    assert applied_ignore_sig(cfg, "/b") == "old-global-sig"
    # ...until /b is stamped in its own right, which retires the fallback.
    save_applied_ignore(cfg, "/b")
    assert applied_ignore_sig(cfg, "/b") == cfg.rules.sig()


def test_a_root_stamped_without_a_legacy_file_stays_unknown(tmp_path):
    """No legacy sig to inherit: an unstamped root is genuinely unknown (an
    index predating the feature), which is the safe-incrementally answer."""
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    save_applied_ignore(cfg, "/a")
    assert applied_ignore_sig(cfg, "/b") is None


def test_compact_never_deletes_a_like_metachar_sibling(tmp_path):
    """`_` matches any char in LIKE: a scan of /x/proj_a must not silently
    drop /x/proj-a's rows — the `outside` predicate escapes LIKE metachars."""
    cfg = _cfg(tmp_path)
    compact(cfg, "/x/proj-a", _shard(tmp_path, cfg, [
        ("/x/proj-a", _scanned("s", [], 0, 1, 1)),
        ("/x/proj-a/sub", _scanned("s", [_row("/x/proj-a/sub/keep.txt")], 10, 1, 0)),
    ]), pa, pq)
    compact(cfg, "/x/proj_a", _shard(tmp_path, cfg, [
        ("/x/proj_a", _scanned("s", [_row("/x/proj_a/new.txt")], 10, 1, 0)),
    ]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    paths = pq.read_table(part).column("path").to_pylist()
    assert "/x/proj-a/sub/keep.txt" in paths
    assert "/x/proj_a/new.txt" in paths


def test_compact_serializes_behind_the_store_lock(tmp_path):
    """Two concurrent compactions both read the same manifest generation,
    write identically-named partitions, and the losing root's rows vanish
    from whichever manifest lands last. Writers therefore serialize on
    store_lock, manifest read included."""
    import threading
    import time as _time

    from fused_render.index.store import store_lock

    cfg = _cfg(tmp_path)
    shards = _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 0))])
    finished = threading.Event()

    def run():
        compact(cfg, "/r", shards, pa, pq)
        finished.set()

    with store_lock(cfg):
        t = threading.Thread(target=run)
        t.start()
        assert not finished.wait(0.4), "compact ran while the lock was held"
    assert finished.wait(30), "compact never ran after the lock was released"
    t.join()
    assert read_manifest(cfg)["rows"] == 1


def test_compact_aborts_at_the_lock_when_its_run_was_cancelled(tmp_path):
    """Delete Index cancels the run and takes the lock; a compaction arriving
    afterwards must abort instead of rebuilding the store the user just
    emptied (its docstring promised this; only the walk used to check)."""
    cfg = _cfg(tmp_path)
    shards = _shard(tmp_path, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 0))])
    flag = tmp_path / "cancel"
    flag.write_text("", encoding="utf-8")
    out = compact(cfg, "/r", shards, pa, pq, cancel_flag=str(flag))
    assert out is None
    assert read_manifest(cfg) is None
