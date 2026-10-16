"""The on-disk index store: shard sinks, the directory reuse cache, and the
duckdb compaction that merges shards into path-sorted partitions.

See fused_render/index/specs/index-store.md.
"""
import json
import os
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index import store as store_mod
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


def test_compact_stores_the_absolute_path_depth_on_both_tables(tmp_path):
    """`depth` is denormalised out of the path so the corpus query can order by
    a stored int32 instead of counting slashes per row (store.schemas)."""
    cfg = _cfg(tmp_path)
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r/b", _scanned("s", [_row("/r/b/1.txt")], 10, 2, 0)),
        ("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 1)),
    ]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    t = pq.read_table(part)
    assert dict(zip(t.column("path").to_pylist(),
                    t.column("depth").to_pylist())) == {
        "/r/a.txt": 2, "/r/b/1.txt": 3}
    d = pq.read_table(cfg.dirs_parquet)
    assert dict(zip(d.column("dir").to_pylist(),
                    d.column("depth").to_pylist())) == {"/r": 1, "/r/b": 2}


def test_compact_backfills_depth_onto_a_pre_depth_index(tmp_path):
    """Additive schema evolution, like mtime_ns and n_subdirs before it: an
    index already on disk has one fewer column than the shards it is unioned
    with, and a positional UNION ALL would fail rather than merge."""
    cfg = _cfg(tmp_path)
    compact(cfg, "/one", _shard(tmp_path, cfg, [
        ("/one", _scanned("s", [_row("/one/a.txt")], 10, 1, 0))]), pa, pq)
    for fp in [os.path.join(cfg.files_dir, p["file"])
               for p in read_manifest(cfg)["partitions"]] + [cfg.dirs_parquet]:
        pq.write_table(pq.read_table(fp).drop(["depth"]), fp)
    compact(cfg, "/two", _shard(tmp_path, cfg, [
        ("/two", _scanned("s", [_row("/two/b/c.txt")], 10, 1, 0))]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    t = pq.read_table(part)
    assert dict(zip(t.column("path").to_pylist(),
                    t.column("depth").to_pylist())) == {
        "/one/a.txt": 2, "/two/b/c.txt": 3}
    assert sorted(pq.read_table(cfg.dirs_parquet).column("depth").to_pylist()) == [1, 1]


@pytest.mark.parametrize("awkward", [
    "Dave's stuff",
    "brack[et]s",
    "star*dir",
])
def test_compact_survives_a_store_path_with_sql_or_glob_metachars(tmp_path, awkward):
    """The store dir is the USER's path (FUSED_RENDER_HOME under their home),
    so it can hold anything a filename can. An apostrophe closed the SQL
    literal and made every compaction a syntax error — the index could never
    build at all. A '[' silently matched no files, because DuckDB's glob has
    no escape (verified on 1.5.5): the fix is to list the shard files
    explicitly rather than hand DuckDB a pattern built from a real path."""
    home = tmp_path / awkward
    home.mkdir()
    cfg = _cfg(home)
    shards = _shard(home, cfg, [
        ("/r", _scanned("s", [_row("/r/a.txt"), _row("/r/b.txt")], 20, 1, 0))])
    summary = compact(cfg, "/r", shards, pa, pq)
    assert summary["rows"] == 2
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    assert pq.read_table(part).column("path").to_pylist() == ["/r/a.txt", "/r/b.txt"]
    # a second compaction reads the first one's partitions back
    assert compact(cfg, "/r", _shard(home, cfg, [
        ("/r", _scanned("s2", [_row("/r/a.txt")], 10, 2, 0))]), pa, pq)["rows"] == 1


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


def test_compact_emits_progress_across_multiple_partitions(tmp_path):
    """A compaction spanning several partitions must keep emitting through
    the whole partition-write loop, not just once at the start and once at
    the end — that gap is what the liveness watchdog reads as a dead worker
    during a real multi-partition merge (specs/index-store.md §4)."""
    cfg = _cfg(tmp_path, part_rows=5)
    rows = [_row(f"/r/f{i}.txt") for i in range(23)]
    seen = []
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", rows, 23, 1, 0))]), pa, pq,
        emit=lambda **ev: seen.append(ev))
    n_parts = len(read_manifest(cfg)["partitions"])
    assert n_parts >= 4
    phase_msgs = [e["msg"] for e in seen if e.get("type") == "phase"]
    # one distinguishable message per partition, on top of "writing index"
    # and "writing signatures"
    assert len(phase_msgs) >= n_parts + 2


def test_compaction_progress_keeps_the_watchdog_from_reporting_abandoned(tmp_path):
    """Reproduces the bug directly, on a fake clock standing in for a
    compaction slow enough that two emits alone (the old "writing index" /
    "writing signatures" phases) would span past ABANDONED_RUN_S, while
    `spec.json` — backdated once and never touched again — proves the fix
    does not depend on anything else in the run directory moving."""
    from fused_render.index.scan import _emit
    from fused_render.index import runner

    cfg = _cfg(tmp_path, part_rows=5)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events_path = run_dir / "events.jsonl"
    spec = run_dir / "spec.json"
    spec.write_text("{}")
    old = time.time() - runner.ABANDONED_RUN_S - 60
    os.utime(spec, (old, old))

    ev = open(events_path, "a")
    step = runner.ABANDONED_RUN_S - 5
    clock = [old]
    ticks = []

    def emit(**kw):
        clock[0] += step
        _emit(ev, **kw)
        os.utime(events_path, (clock[0], clock[0]))
        ticks.append(clock[0])

    rows = [_row(f"/r/f{i}.txt") for i in range(23)]
    shards = _shard(tmp_path, cfg, [("/r", _scanned("s", rows, 23, 1, 0))])
    compact(cfg, "/r", shards, pa, pq, emit=emit)
    ev.close()

    n_parts = len(read_manifest(cfg)["partitions"])
    assert len(ticks) >= n_parts + 2
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert all(g < runner.ABANDONED_RUN_S for g in gaps)
    # two emits alone, at this cadence, would have spanned past the threshold
    assert ticks[-1] - ticks[0] > runner.ABANDONED_RUN_S
    assert runner._looks_abandoned(
        str(run_dir), clock[0], runner.ABANDONED_RUN_S) is False


def test_the_blocking_merge_statement_itself_heartbeats(tmp_path, monkeypatch):
    """The per-partition heartbeat (see the test above) only covers the
    COPY loop. The dominant cost on a large merge is the single blocking
    `CREATE TEMP TABLE merged AS ...` statement that runs BEFORE that loop —
    with nothing touching the run directory for as long as that statement
    takes, a merge slower than ABANDONED_RUN_S would read as a dead worker
    with no heartbeat at all during it."""
    monkeypatch.setattr(store_mod, "_MERGE_HEARTBEAT_S", 0.02)

    real_connect = store_mod.background_connect

    class _SlowDuringMerge:
        """Proxies a real duckdb connection, only slowing the one statement
        under test — everything else in compaction runs at normal speed."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if "CREATE TEMP TABLE merged" in sql:
                time.sleep(0.2)
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(store_mod, "background_connect",
                        lambda **kw: _SlowDuringMerge(real_connect(**kw)))

    cfg = _cfg(tmp_path, part_rows=5)
    rows = [_row(f"/r/f{i}.txt") for i in range(23)]
    seen = []
    compact(cfg, "/r", _shard(tmp_path, cfg, [
        ("/r", _scanned("s", rows, 23, 1, 0))]), pa, pq,
        emit=lambda **ev: seen.append(ev))
    phase_msgs = [e["msg"] for e in seen if e.get("type") == "phase"]
    merge_heartbeats = [m for m in phase_msgs if "merging" in m]
    # 0.2s of blocking work at a 0.02s heartbeat interval must land more than
    # one tick — a single heartbeat could just be the ordinary "writing
    # index" phase logged before the statement, not a heartbeat DURING it.
    assert len(merge_heartbeats) >= 2


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
        file_schema.names,
        ["/one/ghost.txt", "/one", "ghost.txt", "txt", 1, 1.0, 2])},
        schema=file_schema), stray)
    compact(cfg, "/two", _shard(tmp_path, cfg, [
        ("/two", _scanned("s", [_row("/two/b.txt")], 10, 1, 0))]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    assert pq.read_table(part).column("path").to_pylist() == ["/one/a.txt", "/two/b.txt"]


def test_delete_store_evicts_the_schema_cache_for_this_store(tmp_path):
    """Review finding I / D880: `delete_store` removes the manifest, so the
    next compaction's `generation` starts back at 1 — the same
    `query._cached_src_cols` key an earlier life of this same store dir
    could already have populated at generation 1. Left uncached across the
    delete, a rebuilt store's real schema could be shadowed by that stale
    entry. Pin that `delete_store` evicts every cache entry for `cfg.dir`
    and leaves an unrelated store's entries alone."""
    from fused_render.index import query as query_mod
    from fused_render.index.store import delete_store

    cfg = _cfg(tmp_path)
    other_dir = str(tmp_path / "other-ix")
    query_mod._src_cols_cache[(cfg.dir, 1, "dirs")] = {"stale_col"}
    query_mod._src_cols_cache[(cfg.dir, 1, "files")] = {"stale_col"}
    query_mod._src_cols_cache[(other_dir, 1, "dirs")] = {"unrelated_col"}

    delete_store(cfg)

    assert (cfg.dir, 1, "dirs") not in query_mod._src_cols_cache
    assert (cfg.dir, 1, "files") not in query_mod._src_cols_cache
    assert query_mod._src_cols_cache[(other_dir, 1, "dirs")] == {"unrelated_col"}


def test_delete_store_on_an_empty_store_still_evicts_the_cache(tmp_path):
    """`delete_store` on a store that was never built is a documented no-op
    for the files it tries to unlink — but it must still run the cache
    eviction unconditionally, since a cache entry can exist for a `cfg.dir`
    whose on-disk store was already gone (e.g. deleted a second time)."""
    from fused_render.index import query as query_mod
    from fused_render.index.store import delete_store

    cfg = _cfg(tmp_path)
    query_mod._src_cols_cache[(cfg.dir, 1, "dirs")] = {"stale_col"}

    delete_store(cfg)  # no files on disk at all

    assert (cfg.dir, 1, "dirs") not in query_mod._src_cols_cache


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


# -- partial merge: a small changed-dir set must not rewrite the whole store --

def _build_ten_dir_store(tmp_path, name="ix"):
    """10 dirs (/r/d00 .. /r/d09), 5 files each, part_rows=5 -> exactly one
    partition per directory: partition i's whole content is dir di's files.
    That 1:1 mapping is what makes it easy to assert precisely which
    partitions a later merge touched and which it left untouched."""
    cfg = _cfg(tmp_path / name, part_rows=5)
    entries = [
        (f"/r/d{i:02d}",
         _scanned(f"sig{i}", [_row(f"/r/d{i:02d}/f{j}.txt") for j in range(5)],
                  50, i + 1, 0))
        for i in range(10)
    ]
    compact(cfg, "/r", _shard(tmp_path / name, cfg, entries), pa, pq)
    return cfg


def _all_rows(cfg):
    """Every (path, dir, name, ext, size, mtime, depth) tuple currently in the
    store, from EVERY partition file the manifest names — the ground truth an
    equivalence check compares, independent of which files happen to hold
    which rows."""
    m = read_manifest(cfg)
    files = [os.path.join(cfg.files_dir, p["file"]) for p in m["partitions"]]
    if not files:
        return set()
    t = pq.read_table(files, columns=["path", "dir", "name", "ext", "size",
                                       "mtime", "depth"])
    return set(zip(*(t.column(c).to_pylist() for c in t.column_names)))


def test_partial_merge_is_equivalent_to_a_full_rewrite(tmp_path):
    """The single most important test in this round: for the SAME inputs, the
    merge path a small changed-dir set takes must produce a store with the
    exact same rows as the full-rewrite path would, not merely a faster one.

    Built by running the identical update against two copies of the same
    fixture store: one left to pick whatever path `compact` chooses on its
    own (expected: the merge path, since only 1 of 10 dirs changed), the
    other with `_plan_partial_merge` forced to answer "rewrite everything" so
    it is provably the full-rewrite path. Same row set, same dirs.parquet,
    same summary totals is the equivalence this round is about."""
    cfg_a = _build_ten_dir_store(tmp_path, "a")  # picks its own path
    cfg_b = _build_ten_dir_store(tmp_path, "b")  # forced full rewrite

    update = [("/r/d05", _scanned("sig5-changed",
                                   [_row("/r/d05/f0.txt", size=999)],
                                   999, 100, 0))]
    keep = [(f"/r/d{i:02d}", 5) for i in range(10) if i != 5]

    summary_a = compact(cfg_a, "/r",
                        _shard(tmp_path / "a", cfg_a, update, keep=keep), pa, pq)

    def _force_full(*args, **kwargs):
        return None  # "no partial plan" -> the full-rewrite path

    import fused_render.index.store as store_mod
    orig = store_mod._plan_partial_merge
    store_mod._plan_partial_merge = _force_full
    try:
        summary_b = compact(cfg_b, "/r",
                            _shard(tmp_path / "b", cfg_b, update, keep=keep),
                            pa, pq)
    finally:
        store_mod._plan_partial_merge = orig

    assert summary_a["merged_rewrite"] is True
    assert summary_b["merged_rewrite"] is False
    assert _all_rows(cfg_a) == _all_rows(cfg_b)
    assert pq.read_table(cfg_a.dirs_parquet).to_pydict() == \
        pq.read_table(cfg_b.dirs_parquet).to_pydict()
    for key in ("rows", "root_files", "root_size", "root_dirs",
               "changed_dirs", "added_dirs", "removed_dirs"):
        assert summary_a[key] == summary_b[key], key


def test_partial_merge_leaves_untouched_partitions_byte_identical(tmp_path):
    cfg = _build_ten_dir_store(tmp_path)
    before = read_manifest(cfg)["partitions"]
    before_stat = {p["file"]: os.stat(os.path.join(cfg.files_dir, p["file"])).st_ino
                  for p in before}
    before_mtime = {p["file"]: os.stat(os.path.join(cfg.files_dir, p["file"])).st_mtime_ns
                    for p in before}

    update = [("/r/d05", _scanned("sig5-changed",
                                   [_row("/r/d05/f0.txt", size=999)],
                                   999, 100, 0))]
    keep = [(f"/r/d{i:02d}", 5) for i in range(10) if i != 5]
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, update, keep=keep), pa, pq)
    assert summary["merged_rewrite"] is True

    after = read_manifest(cfg)["partitions"]
    # every partition file NOT holding d05's rows kept its exact filename and
    # on-disk bytes (mtime unchanged) -- it was never opened for writing
    untouched_files = {p["file"] for p in after} & set(before_stat)
    assert len(untouched_files) == 9
    for f in untouched_files:
        assert os.stat(os.path.join(cfg.files_dir, f)).st_mtime_ns == before_mtime[f]


def test_partial_merge_handles_a_dir_whose_rows_all_disappear(tmp_path):
    cfg = _build_ten_dir_store(tmp_path)
    # d05 is dropped entirely: not scanned, not in the keep list
    keep = [(f"/r/d{i:02d}", 5) for i in range(10) if i != 5]
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, [], keep=keep), pa, pq)
    assert summary["removed_dirs"] == 1
    rows = _all_rows(cfg)
    assert not any(p[1] == "/r/d05" for p in rows)
    assert len(rows) == 45


def test_partial_merge_handles_a_new_dir_in_the_gap_between_two_partitions(tmp_path):
    """A brand new directory ('/r/d05b') has no rows in ANY old partition, so
    the per-partition affected test alone would mark nothing affected -- and
    its path sorts strictly between d05's and d06's old partitions, so a
    naive [min,max]-overlap test could miss it too. The merge range must
    still be widened to include it (store.py's load-bearing invariant: no row
    a scan produced may be silently dropped)."""
    cfg = _build_ten_dir_store(tmp_path)
    keep = [(f"/r/d{i:02d}", 5) for i in range(10)]  # every old dir unchanged
    new_dir = [("/r/d05b", _scanned("new", [_row("/r/d05b/f0.txt")], 10, 1, 0))]
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, new_dir, keep=keep), pa, pq)
    rows = _all_rows(cfg)
    assert ("/r/d05b/f0.txt", "/r/d05b", "f0.txt", "txt", 10, 100.0, 3) in rows
    assert len(rows) == 51  # the original 50 plus the new one


def test_partial_merge_falls_back_to_full_rewrite_when_every_partition_is_touched(tmp_path):
    cfg = _build_ten_dir_store(tmp_path)
    entries = [
        (f"/r/d{i:02d}", _scanned(f"sig{i}b",
                                  [_row(f"/r/d{i:02d}/f0.txt", size=999)],
                                  999, 200, 0))
        for i in range(10)
    ]
    # every dir rescanned, none kept -> every partition is affected
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, entries), pa, pq)
    assert summary["merged_rewrite"] is False


def test_partial_merge_migrates_a_store_with_a_pre_depth_partition(tmp_path):
    """A partition written before the `depth` column existed must not be
    carried forward next to new ones: a mixed-schema manifest breaks readers.
    Any such partition forces the full rewrite, which writes `depth` everywhere."""
    cfg = _build_ten_dir_store(tmp_path)
    old = read_manifest(cfg)["partitions"][7]
    path = os.path.join(cfg.files_dir, old["file"])
    t = pq.read_table(path)
    pq.write_table(t.drop(["depth"]), path)

    update = [("/r/d02", _scanned("sig2-changed",
                                   [_row("/r/d02/f0.txt", size=999)],
                                   999, 100, 0))]
    keep = [(f"/r/d{i:02d}", 5) for i in range(10) if i != 2]
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, update, keep=keep), pa, pq)
    assert summary["merged_rewrite"] is False
    parts = read_manifest(cfg)["partitions"]
    for p in parts:
        assert "depth" in pq.read_schema(
            os.path.join(cfg.files_dir, p["file"])).names
    rows = _all_rows(cfg)
    assert len(rows) == 50 - 5 + 1
    assert len({r[0] for r in rows}) == len(rows)


def test_partial_merge_at_the_boundary_one_partition_short_of_everything(tmp_path):
    """9 of 10 dirs change; exactly 1 stays untouched -- the boundary on the
    side where a merge still has something to save."""
    cfg = _build_ten_dir_store(tmp_path)
    entries = [
        (f"/r/d{i:02d}", _scanned(f"sig{i}b",
                                  [_row(f"/r/d{i:02d}/f0.txt", size=999)],
                                  999, 200, 0))
        for i in range(9)
    ]
    keep = [("/r/d09", 5)]
    before_file = read_manifest(cfg)["partitions"][9]["file"]  # d09's partition
    before_mtime = os.stat(os.path.join(cfg.files_dir, before_file)).st_mtime_ns
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, entries, keep=keep), pa, pq)
    assert summary["merged_rewrite"] is True
    after = read_manifest(cfg)["partitions"]
    # d09's own partition survives untouched (same file, same bytes); the
    # other 9 dirs' single-row updates (9 rows) get re-chunked at part_rows=5
    # into 2 fresh partitions -- 3 partitions total, not the original 10.
    assert any(p["file"] == before_file for p in after)
    assert os.stat(os.path.join(cfg.files_dir, before_file)).st_mtime_ns == before_mtime
    assert len(after) == 3


# -- store_lock: a wait that can be cancelled and that heartbeats ----------------

def _hold_lock(cfg):
    """Hold the store lock from a second open file description, so the
    lock under test contends with it the way another worker's would."""
    import fcntl

    os.makedirs(cfg.dir, exist_ok=True)
    f = open(os.path.join(cfg.dir, ".store.lock"), "a")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def test_uncontended_store_lock_works_and_releases(tmp_path):
    cfg = _cfg(tmp_path)
    with store_mod.store_lock(cfg):
        pass
    with store_mod.store_lock(cfg):  # would block forever if never released
        pass


def test_compact_waiting_on_the_store_lock_returns_none_when_cancelled(tmp_path):
    """A cancelled run used to notice its flag only AFTER acquiring the lock,
    holding its whole scan's memory while it waited behind a long merge."""
    import threading

    cfg = _cfg(tmp_path)
    shards = _shard(tmp_path, cfg, [("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 0))])
    flag = str(tmp_path / "cancel")
    held = _hold_lock(cfg)
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=compact(cfg, "/r", shards, pa, pq, cancel_flag=flag)))
    try:
        t.start()
        time.sleep(0.4)
        assert t.is_alive()  # still waiting on the held lock
        open(flag, "w").close()
        t.join(timeout=2.5)
        assert not t.is_alive(), "a cancelled waiter must give up promptly"
        assert out == {"r": None}
    finally:
        held.close()
        t.join(timeout=5)


def test_a_waiting_store_lock_heartbeats(tmp_path, monkeypatch):
    """A waiting worker emits nothing otherwise, so the liveness watchdog
    (runner.ABANDONED_RUN_S) would call it dead while it is merely queued."""
    import threading

    monkeypatch.setattr(store_mod, "_LOCK_WAIT_HEARTBEAT_S", 0.1)
    cfg = _cfg(tmp_path)
    beats = []
    held = _hold_lock(cfg)
    got = []

    def waiter():
        with store_mod.store_lock(cfg, on_wait=lambda: beats.append(1)):
            got.append(True)

    t = threading.Thread(target=waiter)
    t.start()
    try:
        time.sleep(0.8)
        assert len(beats) >= 2
        assert not got
    finally:
        held.close()  # releases the flock
        t.join(timeout=5)
    assert got == [True]


def test_compact_reports_a_waiting_phase_while_queued(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(store_mod, "_LOCK_WAIT_HEARTBEAT_S", 0.1)
    cfg = _cfg(tmp_path)
    shards = _shard(tmp_path, cfg, [("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 0))])
    events = []
    held = _hold_lock(cfg)
    t = threading.Thread(target=lambda: compact(
        cfg, "/r", shards, pa, pq, emit=lambda **kw: events.append(kw)))
    t.start()
    try:
        time.sleep(0.6)
    finally:
        held.close()
        t.join(timeout=10)
    assert {"type": "phase", "msg": "waiting for another scan to finish"} in events


# -- load_dir_cache filters in Arrow before it materializes ----------------------

def _write_dirs_rg(cfg, rows, row_group_size=3, with_n_subdirs=True):
    """dirs.parquet sorted by dir, in small row groups so the statistics
    pushdown has several groups to skip across."""
    os.makedirs(cfg.dir, exist_ok=True)
    rows = sorted(rows)
    cols = {"dir": [r[0] for r in rows], "n_files": [r[2] for r in rows],
            "mtime_ns": [r[1] for r in rows]}
    if with_n_subdirs:
        cols["n_subdirs"] = [r[3] for r in rows]
    pq.write_table(pa.table(cols), cfg.dirs_parquet, row_group_size=row_group_size)


_TRAPS = [
    ("/a", 1, 1, 1), ("/r", 2, 2, 3), ("/r/a", 3, 4, 2), ("/r/a/x", 4, 5, 0),
    ("/r/a/x/y", 5, 6, 0), ("/r/a-b", 6, 7, 0), ("/r/a.b", 7, 8, 0),
    ("/r/ab", 8, 9, 0), ("/r/b", 9, 10, 0), ("/z", 10, 11, 0),
]


def test_load_dir_cache_returns_exactly_the_root_and_its_descendants(tmp_path):
    """`/r/a-b`, `/r/a.b` and `/r/ab` sort inside the `[root, root+'0')` range
    the row-group filter admits; the mask must remove them."""
    cfg = _cfg(tmp_path)
    _write_dirs_rg(cfg, _TRAPS)
    assert load_dir_cache(cfg, "/r/a", pq) == {
        "/r/a": (3, 4, 2), "/r/a/x": (4, 5, 0), "/r/a/x/y": (5, 6, 0)}


def test_load_dir_cache_with_a_trailing_slash_root_gives_the_same_answer(tmp_path):
    cfg = _cfg(tmp_path)
    _write_dirs_rg(cfg, _TRAPS)
    assert load_dir_cache(cfg, "/r/a/", pq) == load_dir_cache(cfg, "/r/a", pq)
    assert "/r/a" in load_dir_cache(cfg, "/r/a/", pq)


def test_load_dir_cache_of_the_filesystem_root_returns_everything(tmp_path):
    cfg = _cfg(tmp_path)
    _write_dirs_rg(cfg, _TRAPS)
    assert sorted(load_dir_cache(cfg, "/", pq)) == sorted(r[0] for r in _TRAPS)


def test_load_dir_cache_without_n_subdirs_reports_minus_one(tmp_path):
    cfg = _cfg(tmp_path)
    _write_dirs_rg(cfg, _TRAPS, with_n_subdirs=False)
    got = load_dir_cache(cfg, "/r/a", pq)
    assert sorted(got) == ["/r/a", "/r/a/x", "/r/a/x/y"]
    assert all(v[2] == -1 for v in got.values())
    assert got["/r/a/x"] == (4, 5, -1)


def test_load_dir_cache_still_drops_an_ignored_descendant(tmp_path):
    cfg = _cfg(tmp_path, ignore=["x"])
    _write_dirs_rg(cfg, _TRAPS)
    assert sorted(load_dir_cache(cfg, "/r/a", pq)) == ["/r/a"]



# -- compaction DuckDB memory cap --------------------------------------------------

def _fake_sysconf(monkeypatch, total_bytes):
    page = 4096

    def sysconf(name):
        if name == "SC_PAGE_SIZE":
            return page
        if name == "SC_PHYS_PAGES":
            return total_bytes // page
        raise ValueError(name)

    monkeypatch.setattr(os, "sysconf", sysconf)


@pytest.mark.parametrize("gb,expect", [(4, 512), (8, 1024), (16, 2048), (64, 2048)])
def test_compaction_memory_clamps_an_eighth_of_ram(monkeypatch, gb, expect):
    _fake_sysconf(monkeypatch, gb * 1024 ** 3)
    assert store_mod.compaction_memory_mb() == expect


def test_compaction_memory_with_unknown_ram_is_a_gigabyte(monkeypatch):
    def boom(name):
        raise ValueError(name)

    monkeypatch.setattr(os, "sysconf", boom)
    assert store_mod.compaction_memory_mb() == 1024


def test_background_connect_caps_memory_and_spills_to_the_temp_dir(
        tmp_path, monkeypatch):
    import duckdb

    _fake_sysconf(monkeypatch, 8 * 1024 ** 3)  # 8 GB -> 1024 MB
    tmp = tmp_path / "t"
    con = store_mod.background_connect(temp_dir=str(tmp))
    ref = duckdb.connect()
    ref.execute("SET memory_limit = '1024MB'")
    assert (con.execute("SELECT current_setting('memory_limit')").fetchone()
            == ref.execute("SELECT current_setting('memory_limit')").fetchone())
    assert con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(tmp)
    assert tmp.is_dir()


def test_compact_gives_the_merge_a_temp_directory_under_the_store(
        tmp_path, monkeypatch):
    seen = {}
    real = store_mod.background_connect

    def spy(temp_dir=None):
        seen["temp_dir"] = temp_dir
        return real(temp_dir=temp_dir)

    monkeypatch.setattr(store_mod, "background_connect", spy)
    cfg = _cfg(tmp_path)
    shards = _shard(tmp_path, cfg, [("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 0))])
    compact(cfg, "/r", shards, pa, pq)
    assert seen["temp_dir"] == os.path.join(cfg.dir, "duckdb-tmp")


# -- per-partition plan: non-contiguous rewrites over small partitions ---------

def _hashes(cfg):
    import hashlib
    out = {}
    for p in read_manifest(cfg)["partitions"]:
        with open(os.path.join(cfg.files_dir, p["file"]), "rb") as f:
            out[p["file"]] = (hashlib.sha256(f.read()).hexdigest(),
                              os.stat(f.name).st_mtime_ns)
    return out


def _update_dirs(tmp_path, cfg, changed, n_dirs=10, root="/r"):
    entries = [(f"/r/d{i:02d}",
                _scanned(f"sig{i}-new", [_row(f"/r/d{i:02d}/f0.txt", size=999)],
                         999, 500, 0)) for i in changed]
    keep = [(f"/r/d{i:02d}", 5) for i in range(n_dirs) if i not in changed]
    return compact(cfg, root, _shard(tmp_path, cfg, entries, keep=keep), pa, pq)


def test_two_far_apart_folders_rewrite_only_their_own_partitions(tmp_path):
    cfg = _build_ten_dir_store(tmp_path)
    before = _hashes(cfg)
    names = list(before)
    summary = _update_dirs(tmp_path, cfg, [0, 9])
    assert summary["merged_rewrite"] is True
    assert summary["rewritten_partitions"] == 2
    after = _hashes(cfg)
    middle = names[1:9]
    for f in middle:
        assert after[f] == before[f]  # same filename, same bytes, same mtime
    assert names[0] not in after and names[9] not in after
    assert len(set(after) - set(before)) == 2


def test_new_folder_in_a_gap_rewrites_only_its_owner_partition(tmp_path):
    cfg = _build_ten_dir_store(tmp_path)
    before = _hashes(cfg)
    keep = [(f"/r/d{i:02d}", 5) for i in range(10)]
    new_dir = [("/r/d05b", _scanned("new", [_row("/r/d05b/f0.txt")], 10, 1, 0))]
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, new_dir, keep=keep), pa, pq)
    assert summary["rewritten_partitions"] == 1
    after = _hashes(cfg)
    assert len(set(before) - set(after)) == 1
    # 5 old rows + 1 new at part_rows=5 re-split into 2 fresh files
    assert len(set(after) - set(before)) == 2
    assert ("/r/d05b/f0.txt", "/r/d05b", "f0.txt", "txt", 10, 100.0, 3) in _all_rows(cfg)
    assert len(_all_rows(cfg)) == 51


def test_deleting_a_whole_partition_drops_it_from_the_manifest(tmp_path):
    cfg = _build_ten_dir_store(tmp_path)
    keep = [(f"/r/d{i:02d}", 5) for i in range(10) if i != 5]
    summary = compact(cfg, "/r", _shard(tmp_path, cfg, [], keep=keep), pa, pq)
    m = read_manifest(cfg)
    assert len(m["partitions"]) == 9
    assert m["rows"] == 45 == summary["rows"]
    assert all(p["rows"] > 0 for p in m["partitions"])
    for p in m["partitions"]:
        assert pq.read_metadata(os.path.join(cfg.files_dir, p["file"])).num_rows > 0
    assert len(_all_rows(cfg)) == 45


def test_an_oversized_old_partition_is_resplit_when_touched(tmp_path):
    big = _cfg(tmp_path / "big", part_rows=500)
    entries = [(f"/r/d{i}", _scanned(f"s{i}",
                                     [_row(f"/r/d{i}/f{j:03d}.txt") for j in range(300)],
                                     3000, i + 1, 0)) for i in range(4)]
    compact(big, "/r", _shard(tmp_path / "big", big, entries), pa, pq)
    before = _hashes(big)
    assert len(before) == 3  # 1200 rows at 500 per partition
    small = IndexConfig(dir=big.dir, part_rows=50)
    upd = [("/r/d0", _scanned("s0b", [_row(f"/r/d0/f{j:03d}.txt", size=7)
                                      for j in range(300)], 2100, 99, 0))]
    keep = [(f"/r/d{i}", 300) for i in range(1, 4)]
    summary = compact(small, "/r", _shard(tmp_path / "big", small, upd, keep=keep),
                      pa, pq)
    assert summary["merged_rewrite"] is True
    after = _hashes(small)
    assert len(set(after) & set(before)) == 2  # untouched ones carried forward
    m = read_manifest(small)
    assert max(p["rows"] for p in m["partitions"] if p["file"] not in before) <= 50
    import duckdb
    files = [os.path.join(small.files_dir, p["file"]) for p in m["partitions"]]
    n, nd = duckdb.connect().execute(
        "SELECT count(*), count(DISTINCT path) FROM read_parquet(?)", [files]).fetchone()
    assert n == nd == 1200


def test_manifest_stays_sorted_and_non_overlapping(tmp_path):
    cfg = _build_ten_dir_store(tmp_path)
    _update_dirs(tmp_path, cfg, [0, 9])
    parts = read_manifest(cfg)["partitions"]
    for a, b in zip(parts, parts[1:]):
        assert a["max"] < b["min"]


def test_two_rewritten_runs_get_distinct_filenames(tmp_path):
    cfg = _build_ten_dir_store(tmp_path)
    _update_dirs(tmp_path, cfg, [0, 9])
    names = [p["file"] for p in read_manifest(cfg)["partitions"]]
    assert len(names) == len(set(names)) == 10
    on_disk = set(os.listdir(cfg.files_dir))
    assert set(names) <= on_disk


def test_planning_skips_partitions_outside_the_scan_root(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, part_rows=5)
    entries = [(f"/{top}/d{i}", _scanned(f"s{top}{i}",
                                         [_row(f"/{top}/d{i}/f{j}.txt") for j in range(5)],
                                         50, i + 1, 0))
               for top in ("a", "z") for i in range(3)]
    compact(cfg, "/", _shard(tmp_path, cfg, entries), pa, pq)
    z_files = [p["file"] for p in read_manifest(cfg)["partitions"]
               if p["min"].startswith("/z/")]
    assert len(z_files) == 3
    seen = []
    real = store_mod.background_connect

    class Spy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            seen.append(sql)
            return self._con.execute(sql, *a, **k)

        def __getattr__(self, n):
            return getattr(self._con, n)

    monkeypatch.setattr(store_mod, "background_connect",
                        lambda *a, **k: Spy(real(*a, **k)))
    upd = [("/a/d1", _scanned("new", [_row("/a/d1/f0.txt", size=5)], 5, 9, 0))]
    keep = [("/a/d0", 5), ("/a/d2", 5)] + [(f"/z/d{i}", 5) for i in range(3)]
    compact(cfg, "/a", _shard(tmp_path, cfg, upd, keep=keep), pa, pq)
    probes = [s for s in seen if s.startswith("SELECT count(*) FROM read_parquet")
              and " WHERE NOT (" in s and "OR " in s.split(" WHERE NOT (")[1]]
    assert probes, "expected affected-count probes"
    bad = [s for s in probes if any(z in s for z in z_files)]
    assert not bad, (z_files, bad[:2], len(probes))
# -- low-impact resource knobs --------------------------------------------------

@pytest.mark.parametrize("cpus,normal,low", [(64, 4, 2), (8, 2, 2), (4, 1, 1)])
def test_compaction_threads_low_impact_caps_at_two(monkeypatch, cpus, normal, low):
    monkeypatch.setattr(os, "cpu_count", lambda: cpus)
    assert store_mod.compaction_threads() == normal
    assert store_mod.compaction_threads(low_impact=True) == low


@pytest.mark.parametrize("gb,normal,low", [(4, 512, 512), (8, 1024, 768),
                                           (64, 2048, 768)])
def test_compaction_memory_low_impact_caps_at_768(monkeypatch, gb, normal, low):
    _fake_sysconf(monkeypatch, gb * 1024 ** 3)
    assert store_mod.compaction_memory_mb() == normal
    assert store_mod.compaction_memory_mb(low_impact=True) == low


def test_background_connect_low_impact_applies_the_caps(monkeypatch):
    import duckdb

    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    _fake_sysconf(monkeypatch, 64 * 1024 ** 3)
    con = store_mod.background_connect(low_impact=True)
    assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 2
    ref = duckdb.connect()
    ref.execute("SET memory_limit = '768MB'")
    assert (con.execute("SELECT current_setting('memory_limit')").fetchone()
            == ref.execute("SELECT current_setting('memory_limit')").fetchone())


def test_compact_threads_low_impact_into_background_connect(tmp_path, monkeypatch):
    seen = []
    real = store_mod.background_connect

    def spy(**kw):
        seen.append(kw.get("low_impact", False))
        return real(**kw)

    monkeypatch.setattr(store_mod, "background_connect", spy)
    cfg = _cfg(tmp_path)
    compact(cfg, "/r", _shard(tmp_path, cfg,
                              [("/r", _scanned("s", [_row("/r/a.txt")], 10, 1, 0))]),
            pa, pq, low_impact=True)
    assert seen == [True]
