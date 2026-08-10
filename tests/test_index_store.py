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


# -- the applied-ignore fingerprint -------------------------------------------

def test_applied_ignore_sig_round_trips(tmp_path):
    cfg = _cfg(tmp_path, ignore=["node_modules"])
    assert applied_ignore_sig(cfg) is None  # nothing built yet
    save_applied_ignore(cfg)
    assert applied_ignore_sig(cfg) == cfg.rules.sig()
    assert json.load(open(cfg.applied_ignore_json))["patterns"] == ["node_modules"]
