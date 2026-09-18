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
from fused_render.index.kinds import Column, IndexKind, register
from fused_render.index.store import (
    Sink,
    applied_ignore_sig,
    compact,
    load_dir_cache,
    read_manifest,
    save_applied_ignore,
    schemas,
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


def test_sink_add_tolerates_a_plugin_row_missing_a_declared_column(tmp_path):
    """Review finding 6: `extract`'s own contract says "return a dict of
    declared columns", but it is untrusted plugin code from the writer's
    point of view — the same "a plugin cannot take the scan down" rule
    `scan.py`'s own `extract` call site now honors (finding 5). Before the
    fix, the generic branch did `r[name].append(fr[name])`, so a row
    missing even one of the kind's declared keys raised `KeyError` and
    aborted the whole shard, not just that one malformed row."""
    kind = IndexKind(
        name="_test_missing_key",
        columns=(Column("name", "string"), Column("extra", "string")),
        extract=lambda path, st: None,
        text_column="name",
    )
    register(kind, replace=True)
    shards = str(tmp_path / "s")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, IndexConfig(dir=str(tmp_path / "ix")).shard_rows,
                kind="_test_missing_key")
    # "b" is missing the declared "extra" column entirely.
    sink.add("/r", "s", ("sig", [{"name": "a", "extra": "x"}, {"name": "b"}],
                         0, 1, 0))
    sink._flush_files()
    names = [n for n in os.listdir(shards) if n.startswith("shard-")]
    t = pq.read_table(os.path.join(shards, names[0]))
    assert t.column("name").to_pylist() == ["a", "b"]
    assert t.column("extra").to_pylist() == ["x", None]


def test_sink_add_tolerates_a_plugin_row_that_is_not_a_dict_at_all(tmp_path):
    """Bugbot finding against a7aef9472: `scan.py`'s own `extract` call site
    only guards against the row being an EXCEPTION or `None` — a plugin
    that returns some other truthy, non-dict value (a bare string, a list,
    an int) sails through that `if row is not None:` check unchanged and
    lands here as `fr`. The generic branch's `fr.get(name)` then raised
    `AttributeError` (a `str`/`list`/`int` has no `.get`), which is not
    caught anywhere between here and the scan's own top-level handler, so
    one malformed row aborted the entire run — not just degraded, unlike
    the sibling missing-key case above."""
    kind = IndexKind(
        name="_test_non_dict_row",
        columns=(Column("name", "string"), Column("extra", "string")),
        extract=lambda path, st: None,
        text_column="name",
    )
    register(kind, replace=True)
    shards = str(tmp_path / "s")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, IndexConfig(dir=str(tmp_path / "ix")).shard_rows,
                kind="_test_non_dict_row")
    # "not-a-dict-at-all" is exactly the shape a plugin bug (or a plugin
    # written against a different `IndexKind.extract` contract) can return
    # without ever raising — it is not `None`, so scan.py's own guard lets
    # it through.
    sink.add("/r", "s", ("sig", [{"name": "a", "extra": "x"}, "not-a-dict-at-all"],
                         0, 1, 0))
    sink._flush_files()
    names = [n for n in os.listdir(shards) if n.startswith("shard-")]
    t = pq.read_table(os.path.join(shards, names[0]))
    assert t.column("name").to_pylist() == ["a", None]
    assert t.column("extra").to_pylist() == ["x", None]


def _register_widgets_kind():
    kind = IndexKind(
        name="_test_widgets",
        columns=(Column("name", "string"), Column("count", "int64")),
        extract=lambda path, st: None,
        text_column="name",
    )
    register(kind, replace=True)
    return kind


def test_schemas_default_kind_is_byte_identical_to_files():
    file_schema, dir_schema = schemas(pa)
    file_schema_explicit, dir_schema_explicit = schemas(pa, "files")
    assert file_schema == file_schema_explicit
    assert dir_schema == dir_schema_explicit
    assert file_schema.names == ["path", "dir", "name", "ext", "size", "mtime", "depth"]


def test_schemas_for_a_registered_kind_uses_its_pa_schema():
    _register_widgets_kind()
    file_schema, dir_schema = schemas(pa, "_test_widgets")
    assert file_schema.names == ["name", "count"]
    assert file_schema.field("count").type == pa.int64()
    # dirs bookkeeping is kind-agnostic: every kind gets the same dirs table.
    assert dir_schema.names == schemas(pa)[1].names


def test_sink_for_a_registered_kind_writes_dict_rows_by_column_name(tmp_path):
    _register_widgets_kind()
    shards = str(tmp_path / "s")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, 200_000, kind="_test_widgets")
    sink.add("/a", "s", ("sig", [{"name": "one", "count": 3}], 0, 1, 0))
    sink.close()
    names = os.listdir(shards)
    shard = pq.read_table(os.path.join(shards, [n for n in names if n.startswith("shard-")][0]))
    assert shard.column("name").to_pylist() == ["one"]
    assert shard.column("count").to_pylist() == [3]
    assert sink.files == 1


def test_sink_default_kind_is_byte_identical_to_files(tmp_path):
    shards = str(tmp_path / "s")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, 200_000)
    assert sink.kind == "files"
    assert sink.file_schema.names == ["path", "dir", "name", "ext", "size", "mtime", "depth"]


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
    pytest.param(
        "star*dir",
        marks=pytest.mark.skipif(
            os.name == "nt",
            reason="`*` is one of NTFS's categorically-illegal filename "
                   "characters (< > : \" / \\ | ? *) — `home.mkdir()` below "
                   "cannot create this directory on Windows at all, so there "
                   "is no code fix and nothing to skip AROUND: the SQL/glob "
                   "escaping this test exists to prove (store.parquet_src, "
                   "shard_files) is still exercised end to end by the other "
                   "two params, whose characters (apostrophe, brackets) are "
                   "legal on NTFS."),
    ),
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


# -- compaction for a registered (non-"files") kind --------------------------

def _register_notes_kind(name="_test_notes", recency=True):
    kind = IndexKind(
        name=name,
        columns=(Column("title", "string"), Column("path", "string"),
                  Column("rank", "int64")),
        extract=lambda path, st: None,
        text_column="title",
        identity_column="path",
        recency_column="rank" if recency else None,
    )
    register(kind, replace=True)
    return kind


def _shard_kind(tmp_path, cfg, kind_name, entries):
    """Like `_shard`, but for a Sink writing a non-"files" kind's dict rows."""
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows, kind=kind_name)
    for d, payload in entries:
        sink.add(d, "s", payload)
    sink.close()
    return shards


def test_compact_dedupes_a_registered_kind_by_identity_and_recency_columns(tmp_path):
    _register_notes_kind()
    cfg = _cfg(tmp_path, kind="_test_notes")
    shards = _shard_kind(tmp_path, cfg, "_test_notes", [
        ("/r", ("sig", [
            {"title": "old", "path": "/r/a.md", "rank": 1},
            {"title": "new", "path": "/r/a.md", "rank": 2},
        ], 0, 1, 0)),
    ])
    summary = compact(cfg, "/r", shards, pa, pq)
    assert summary["rows"] == 1
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    t = pq.read_table(part)
    assert t.column("title").to_pylist() == ["new"]
    assert t.column("rank").to_pylist() == [2]


def test_compact_reports_zero_root_size_for_a_kind_without_a_size_column(tmp_path):
    _register_notes_kind()
    cfg = _cfg(tmp_path, kind="_test_notes")
    shards = _shard_kind(tmp_path, cfg, "_test_notes", [
        ("/r", ("sig", [{"title": "a", "path": "/r/a.md", "rank": 1},
                        {"title": "b", "path": "/r/b.md", "rank": 1}], 0, 1, 0)),
    ])
    summary = compact(cfg, "/r", shards, pa, pq)
    assert summary["root_files"] == 2
    assert summary["root_size"] == 0
    assert summary["root_dirs"] == 1


def test_compact_keeps_a_registered_kinds_rows_outside_the_scan_root(tmp_path):
    """A registered kind has no denormalized `dir` column of its own (unlike
    "files"): compaction must derive the containing directory from the
    identity column to decide what "outside this scan's root" means."""
    _register_notes_kind()
    cfg = _cfg(tmp_path, kind="_test_notes")
    compact(cfg, "/one", _shard_kind(tmp_path, cfg, "_test_notes", [
        ("/one", ("sig", [{"title": "a", "path": "/one/a.md", "rank": 1}],
                  0, 1, 0))]), pa, pq)
    compact(cfg, "/two", _shard_kind(tmp_path, cfg, "_test_notes", [
        ("/two", ("sig", [{"title": "b", "path": "/two/b.md", "rank": 1}],
                  0, 1, 0))]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    t = pq.read_table(part)
    assert sorted(t.column("path").to_pylist()) == ["/one/a.md", "/two/b.md"]


def test_compact_dedupes_a_kind_without_a_recency_column_deterministically(tmp_path):
    _register_notes_kind(name="_test_notes_no_recency", recency=False)
    cfg = _cfg(tmp_path, kind="_test_notes_no_recency")
    shards = _shard_kind(tmp_path, cfg, "_test_notes_no_recency", [
        ("/r", ("sig", [{"title": "x", "path": "/r/a.md", "rank": 1},
                        {"title": "y", "path": "/r/a.md", "rank": 2}], 0, 1, 0)),
    ])
    summary = compact(cfg, "/r", shards, pa, pq)
    assert summary["rows"] == 1


def _register_dir_identity_kind(name="_test_dir_identity"):
    """Mirrors `apps_kind.py`'s shape: `identity_column`'s value is the
    FOLDER the row was extracted from (`identity_is_dir=True`), not a file
    inside it — the distinction review finding 2 says `_dir_expr` got
    backwards for "apps"."""
    kind = IndexKind(
        name=name,
        columns=(Column("name", "string"), Column("path", "string")),
        extract=lambda path, st: None,
        text_column="name",
        identity_column="path",
        identity_is_dir=True,
    )
    register(kind, replace=True)
    return kind


def test_compact_keeps_a_dir_identity_kinds_row_across_an_incremental_rescan(tmp_path):
    """Repro from review finding 2: the apps kind's row identity IS the app's
    own folder (`app_dict`'s `path` is the folder's realpath). Rescanning the
    workspace root with that folder reused ("u") must not drop its row —
    `_dir_expr` used to strip a trailing path segment off the identity value
    unconditionally, answering the folder's PARENT instead of the folder
    itself, so it never matched the `_keep` table's literal folder path."""
    _register_dir_identity_kind()
    cfg = _cfg(tmp_path, kind="_test_dir_identity")

    # Full scan: one app row, identified by its own folder.
    compact(cfg, "/ws", _shard_kind(tmp_path, cfg, "_test_dir_identity", [
        ("/ws", ("sig", [{"name": "app", "path": "/ws/app"}], 0, 1, 1)),
        ("/ws/app", ("sig2", [], 0, 1, 0)),
    ]), pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    assert pq.read_table(part).column("path").to_pylist() == ["/ws/app"]

    # Incremental rescan: the workspace root is revisited, but the app's own
    # folder is unchanged and reused ("u") rather than rewalked.
    shards = str(tmp_path / "run2" / "shards")
    os.makedirs(shards)
    sink = Sink(shards, "t2", pa, pq, cfg.shard_rows, kind="_test_dir_identity")
    sink.add("/ws", "s", ("sig", [], 0, 1, 1))
    sink.add("/ws/app", "u", 1)
    sink.close()
    compact(cfg, "/ws", shards, pa, pq)
    part = os.path.join(cfg.files_dir, read_manifest(cfg)["partitions"][0]["file"])
    assert pq.read_table(part).column("path").to_pylist() == ["/ws/app"], (
        "the app's row must survive an incremental scan that reuses its folder")


def test_compact_raises_for_a_kind_without_an_identity_column(tmp_path):
    kind = IndexKind(
        name="_test_no_identity_compact",
        columns=(Column("title", "string"),),
        extract=lambda path, st: None,
        text_column="title",
    )
    register(kind, replace=True)
    cfg = _cfg(tmp_path, kind="_test_no_identity_compact")
    shards = _shard_kind(tmp_path, cfg, "_test_no_identity_compact", [
        ("/r", ("sig", [{"title": "x"}], 0, 1, 0))])
    with pytest.raises(ValueError):
        compact(cfg, "/r", shards, pa, pq)


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
                        lambda: _SlowDuringMerge(real_connect()))

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


# -- the Windows store lock ---------------------------------------------------

class _FakeMsvcrt:
    """msvcrt's locking() surface: LK_NBLCK raises OSError while the lock is
    held by someone else, LK_LOCK blocks internally and then gives up."""

    LK_LOCK = 0
    LK_NBLCK = 1

    def __init__(self, fails: int):
        self.fails = fails
        self.modes: list = []

    def locking(self, fd, mode, nbytes):
        self.modes.append(mode)
        if len(self.modes) <= self.fails:
            raise OSError(36, "Resource deadlock avoided")


def test_the_windows_lock_waits_instead_of_giving_up(monkeypatch):
    """msvcrt's LK_LOCK retries for ~10 seconds and then RAISES. A compaction
    holds this lock for a whole DuckDB merge — far longer on a real home index
    — so a second root's compact, or a Delete Index, would fail outright
    instead of waiting. Poll LK_NBLCK with no deadline, like flock."""
    fake = _FakeMsvcrt(fails=3)
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    store_mod._acquire_nt(fake, 7)
    assert len(fake.modes) == 4  # three refusals, then the acquire
    assert set(fake.modes) == {_FakeMsvcrt.LK_NBLCK}  # never the 10s LK_LOCK


def test_the_windows_lock_returns_as_soon_as_it_is_free(monkeypatch):
    fake = _FakeMsvcrt(fails=0)
    monkeypatch.setattr(store_mod.time, "sleep",
                        lambda s: pytest.fail("slept before even trying"))
    store_mod._acquire_nt(fake, 7)
    assert fake.modes == [_FakeMsvcrt.LK_NBLCK]


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
