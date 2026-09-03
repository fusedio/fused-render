"""Reading the index: partition pruning and stats. See
fused_render/index/specs/query.md.

The raw `sql` action OpenIndex exposed is deliberately absent from THIS module —
arbitrary duckdb from a client is an arbitrary read/write surface. The confined
one lives in `guarded_query.py` (tests/test_index_guarded_query.py), which is
why the assertion below is about `query.py` specifically.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index import query as index_query
from fused_render.index.cancel import CancelToken, Cancelled
from fused_render.index.config import IndexConfig
from fused_render.index.query import prune, stats
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


# -- partition pruning ---------------------------------------------------------

def test_prune_keeps_only_overlapping_partitions():
    parts = [{"min": "/a/1", "max": "/a/9"},
             {"min": "/b/1", "max": "/b/9"},
             {"min": "/c/1", "max": "/c/9"}]
    assert prune(parts, "/b") == [parts[1]]
    assert prune(parts, "") == parts


def test_prune_drops_a_partition_with_no_range():
    assert prune([{"min": None, "max": None}], "/a") == []


def test_prune_is_case_insensitive_like_the_match_it_gates():
    """The match is ILIKE, so the prune that gates it has to fold case too —
    otherwise /users/... rules out every /Users/... partition byte-wise and
    the anchored query returns nothing while the unanchored one finds it.
    The folded bounds are their own aggregate: byte order and folded order
    disagree, so lower(min) is NOT the folded minimum."""
    parts = [{"min": "/Users/a", "max": "/Users/z",
              "min_lower": "/users/a", "max_lower": "/users/z"}]
    assert prune(parts, "/Users/me") == parts
    assert prune(parts, "/users/me") == parts
    assert prune(parts, "/USERS/me") == parts
    assert prune(parts, "/etc/me") == []


def test_prune_falls_back_to_the_byte_test_without_folded_bounds():
    """A manifest written before the folded bounds keeps exactly the old
    behaviour — the status quo for data already on disk, not a new hole.
    Every compaction rewrites the manifest, so this heals on the next scan."""
    parts = [{"min": "/Users/a", "max": "/Users/z"}]
    assert prune(parts, "/Users/me") == parts
    assert prune(parts, "/users/me") == []
    assert prune(parts, "/zzz") == []


# -- stats ---------------------------------------------------------------------

def test_stats_totals_without_breakdown_by_default(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.txt", "/r/c.bin"],
                 sizes={"/r/a.txt": 1, "/r/b.txt": 2, "/r/c.bin": 100})
    out = stats(cfg)
    assert out["rows"] == 3
    assert out["total_size"] == 103
    assert out["last_root"] == "/r"
    assert out["types"] == []


def test_stats_extension_breakdown_when_asked(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.txt", "/r/c.bin"],
                 sizes={"/r/a.txt": 1, "/r/b.txt": 2, "/r/c.bin": 100})
    out = stats(cfg, breakdown=True)
    assert out["rows"] == 3
    assert out["total_size"] == 103
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
    anything the user's account can. The guarded replacement is a separate
    module on purpose, so this assertion keeps meaning what it meant."""
    assert not hasattr(index_query, "sql")
    assert "sql" not in dir(index_query)


def test_stats_does_not_count_a_lookalike_underscore_sibling(tmp_path):
    """`_` matches any char in LIKE: stats for /x/my_dir must not include
    /x/my-dir's rows (the subtree prefix is escaped)."""
    _index(tmp_path, "/x/my_dir", ["/x/my_dir/real.txt"])
    cfg = _index(tmp_path, "/x/my-dir", ["/x/my-dir/fake.txt"])
    assert stats(cfg, root="/x/my_dir")["rows"] == 1


# -- cancellation: a `token` handed to stats -----------------------------------
#
# `stats` follows `search_ranked`'s contract exactly (see
# tests/test_index_search.py's "cancellation: a `token` handed to
# search_ranked" section for the full case list against the reference
# implementation) — bind right after connect, `token.check()` before each
# real query, an InterruptException attributed to this token becomes
# `Cancelled`. These are scoped to stats's own wrinkle: two possible query
# sites (`n_dirs`, and `by_ext` only when `breakdown=True`).

def test_stats_an_uncancelled_token_changes_nothing(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.bin"],
                sizes={"/r/a.txt": 1, "/r/b.bin": 2})
    token = CancelToken()
    assert stats(cfg, breakdown=True, token=token) == stats(cfg, breakdown=True)


def test_stats_a_token_cancelled_before_the_call_raises_promptly(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        stats(cfg, token=token)


def test_stats_an_interrupt_during_the_breakdown_becomes_cancelled(tmp_path, monkeypatch):
    import duckdb as duckdb_module

    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.bin"])
    token = CancelToken()

    class _SpyConnection:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if "GROUP BY" in sql:
                token.cancel()
                raise duckdb_module.InterruptException("simulated cross-thread interrupt")
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = duckdb_module.connect
    monkeypatch.setattr(duckdb_module, "connect",
                        lambda *a, **kw: _SpyConnection(real_connect(*a, **kw)))
    with pytest.raises(Cancelled):
        stats(cfg, breakdown=True, token=token)


def test_stats_an_interrupt_with_no_token_is_not_swallowed(tmp_path, monkeypatch):
    import duckdb as duckdb_module

    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.bin"])

    class _SpyConnection:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if "GROUP BY" in sql:
                raise duckdb_module.InterruptException("simulated, not this token's doing")
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = duckdb_module.connect
    monkeypatch.setattr(duckdb_module, "connect",
                        lambda *a, **kw: _SpyConnection(real_connect(*a, **kw)))
    with pytest.raises(duckdb_module.InterruptException):
        stats(cfg, breakdown=True)


def test_stats_a_cancel_after_return_does_not_touch_the_closed_connection(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    token = CancelToken()
    stats(cfg, token=token)
    # Must not raise.
    token.cancel()
    assert token.cancelled is True
