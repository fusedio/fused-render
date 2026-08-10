"""Reading the index: the lookup pattern grammar, partition pruning, and
stats. See fused_render/index/specs/query.md.

The raw `sql` action OpenIndex exposed is deliberately absent — arbitrary
duckdb from a client is an arbitrary read/write surface behind an HTTP route.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index import query as index_query
from fused_render.index.config import IndexConfig
from fused_render.index.query import lookup, pattern_for, prune, stats
from fused_render.index.store import Sink, compact


def _cfg(tmp_path):
    return IndexConfig(dir=str(tmp_path / "ix"))


def _index(tmp_path, root, paths, sizes=None, mtimes=None):
    """Build a real index over `paths` (absolute, already canonical)."""
    cfg = _cfg(tmp_path)
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    by_dir = {}
    for i, p in enumerate(paths):
        d, name = p.rsplit("/", 1)
        ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
        size = (sizes or {}).get(p, 10)
        mtime = (mtimes or {}).get(p, 100.0 + i)
        by_dir.setdefault(d, []).append((p, d, name, ext, size, mtime))
    for d, rows in by_dir.items():
        sink.add(d, "s", ("sig", rows, sum(r[4] for r in rows), 1, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


# -- the pattern grammar -------------------------------------------------------

@pytest.mark.parametrize("q,expected_like,expected_prefix", [
    ("app", "%app%", ""),                       # substring anywhere
    # `*` is the wildcard; unanchored queries also get the leading `%`
    ("*.parquet", "%%.parquet%", ""),
    ("/Users/me/code", "/Users/me/code%", "/Users/me/code"),   # anchored
    ("/Users/me/*.py", "/Users/me/%.py%", "/Users/me/"),       # prune to the literal lead-in
])
def test_pattern_translation(q, expected_like, expected_prefix):
    like, prefix = pattern_for(q)
    assert (like, prefix) == (expected_like, expected_prefix)


def test_pattern_escapes_like_metacharacters():
    like, _ = pattern_for("a_b%c")
    assert like == "%a\\_b\\%c%"


def test_pattern_expands_and_anchors_a_tilde():
    like, prefix = pattern_for("~/Doc")
    assert prefix == os.path.expanduser("~/Doc")
    assert like.startswith(os.path.expanduser("~/Doc"))


# -- partition pruning ---------------------------------------------------------

def test_prune_keeps_only_overlapping_partitions():
    parts = [{"min": "/a/1", "max": "/a/9"},
             {"min": "/b/1", "max": "/b/9"},
             {"min": "/c/1", "max": "/c/9"}]
    assert prune(parts, "/b") == [parts[1]]
    assert prune(parts, "") == parts


def test_prune_drops_a_partition_with_no_range():
    assert prune([{"min": None, "max": None}], "/a") == []


# -- lookup --------------------------------------------------------------------

def test_lookup_matches_a_substring_anywhere_in_the_path(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/alpha.txt", "/r/sub/beta.md"])
    out = lookup(cfg, "beta")
    assert [r["path"] for r in out["rows"]] == ["/r/sub/beta.md"]
    assert out["total"] == 1


def test_lookup_with_no_query_returns_everything(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.txt"])
    assert lookup(cfg, "")["total"] == 2


def test_lookup_honours_limit_and_offset(tmp_path):
    cfg = _index(tmp_path, "/r", [f"/r/f{i}.txt" for i in range(5)])
    out = lookup(cfg, "", limit=2, offset=1, sort="path")
    assert [r["name"] for r in out["rows"]] == ["f1.txt", "f2.txt"]
    assert out["total"] == 5


def test_lookup_sort_is_an_allowlist(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/b.txt", "/r/a.txt"])
    assert [r["name"] for r in lookup(cfg, "", sort="path")["rows"]] == ["a.txt", "b.txt"]
    # anything unknown falls back to mtime rather than reaching the SQL
    assert lookup(cfg, "", sort="; DROP TABLE files")["rows"]


def test_lookup_reports_pruning_telemetry(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    out = lookup(cfg, "/r/a")
    assert out["scanned_partitions"] == 1
    assert out["of_partitions"] == 1


def test_lookup_prunes_partitions_an_anchored_query_cannot_hit(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    out = lookup(cfg, "/zzz")
    assert out["rows"] == [] and out["scanned_partitions"] == 0


def test_lookup_escapes_quotes_in_the_query(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/it's.txt"])
    assert lookup(cfg, "it's")["total"] == 1


def test_lookup_on_an_empty_index_is_not_an_error(tmp_path):
    out = lookup(_cfg(tmp_path), "anything")
    assert out["empty"] is True
    assert out["rows"] == []


# -- stats ---------------------------------------------------------------------

def test_stats_totals_and_extension_breakdown(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.txt", "/r/c.bin"],
                 sizes={"/r/a.txt": 1, "/r/b.txt": 2, "/r/c.bin": 100})
    out = stats(cfg)
    assert out["rows"] == 3
    assert out["total_size"] == 103
    assert out["last_root"] == "/r"
    assert out["types"][0] == {"ext": "bin", "n": 1, "size": 100}


def test_stats_is_scoped_to_one_subtree(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    compact_cfg = _index(tmp_path, "/other", ["/other/b.txt"])
    assert stats(compact_cfg, root="/r")["rows"] == 1
    assert stats(compact_cfg, root="/other")["rows"] == 1


def test_stats_on_an_empty_index(tmp_path):
    out = stats(_cfg(tmp_path))
    assert out["empty"] is True
    assert out["location"] == str(tmp_path / "ix")


def test_there_is_no_raw_sql_surface():
    """The `sql` action was fine inside a trusted local page and is not fine
    behind an HTTP route: duckdb with unrestricted SQL reads and writes
    anything the user's account can."""
    assert not hasattr(index_query, "sql")
    assert "sql" not in dir(index_query)


def test_stats_does_not_count_a_lookalike_underscore_sibling(tmp_path):
    """`_` matches any char in LIKE: stats for /x/my_dir must not include
    /x/my-dir's rows (the subtree prefix is escaped)."""
    _index(tmp_path, "/x/my_dir", ["/x/my_dir/real.txt"])
    cfg = _index(tmp_path, "/x/my-dir", ["/x/my-dir/fake.txt"])
    assert stats(cfg, root="/x/my_dir")["rows"] == 1
