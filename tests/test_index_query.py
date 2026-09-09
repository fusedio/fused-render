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
from fused_render.index.ignore import norm
from fused_render.index.query import _glob_to_regex, prune, resolve_query, stats
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


# -- glob translation -----------------------------------------------------------

@pytest.mark.parametrize("pattern,rel,expected", [
    ("*.csv", "report.csv", True),
    ("*.csv", "a/report.csv", False),
    ("**/*.csv", "report.csv", True),
    ("**/*.csv", "a/b/report.csv", True),
    ("*/*.csv", "a/report.csv", True),
    ("*/*.csv", "report.csv", False),
    ("*/*.csv", "a/b/report.csv", False),
    ("a/b/*.c", "a/b/x.c", True),
    ("a/b/*.c", "a/b/c/x.c", False),
    ("draft*", "draft1.txt", True),
    ("draft*", "a/draft1.txt", False),
    ("report?.csv", "report1.csv", False),  # ? is a literal character
    ("report?.csv", "report?.csv", True),
    ("[abc].csv", "[abc].csv", True),
    ("[abc].csv", "a.csv", False),
])
def test_glob_to_regex_full_match_semantics(pattern, rel, expected):
    import re as _re

    regex = _glob_to_regex(pattern.lower())
    assert bool(_re.fullmatch(regex[1:-1], rel.lower())) is expected


# -- query resolution ------------------------------------------------------------
#
# The behaviour table from the spec, turned into tests: every row names a
# typed string, the base it resolves against, and (via `mode`/`pattern`) how
# far it reaches. `_home` below is a real directory tree on disk — base
# resolution walks the filesystem, so a fake string root would silently
# short-circuit every case that depends on a segment actually existing.

@pytest.fixture()
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "a" / "b").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(home) if p in ("~", "~/") else p)
    # `resolve_query` returns every base through `norm()` (forward slashes
    # only, the canonical form everything in the index stores and compares
    # paths as) — normalized here too, so the comparison isn't just re-
    # asserting `str(home)`'s own platform-native spelling back at itself.
    return norm(str(home))


def test_resolve_bare_substring_query_is_any_depth_at_the_box_root():
    out = resolve_query("/box", ".csv")
    assert out == {"base": "/box", "pattern": ".csv", "mode": "substring"}
    out = resolve_query("/box", "report")
    assert out == {"base": "/box", "pattern": "report", "mode": "substring"}


def test_resolve_star_with_no_slash_gets_the_implicit_any_depth_prefix():
    out = resolve_query("/box", "*.csv")
    assert out == {"base": "/box", "pattern": "**/*.csv", "mode": "glob"}
    out = resolve_query("/box", "draft*")
    assert out == {"base": "/box", "pattern": "**/draft*", "mode": "glob"}


def test_resolve_leading_slash_anchors_at_depth_one():
    out = resolve_query("/box", "/*.csv")
    assert out == {"base": "/box", "pattern": "*.csv", "mode": "glob"}


def test_resolve_one_slash_inside_reaches_exactly_two():
    out = resolve_query("/box", "*/*.csv")
    assert out == {"base": "/box", "pattern": "*/*.csv", "mode": "glob"}


def test_resolve_explicit_any_depth_form_is_unchanged():
    out = resolve_query("/box", "**/*.csv")
    assert out == {"base": "/box", "pattern": "**/*.csv", "mode": "glob"}


def test_resolve_tilde_star_escapes_to_home_at_depth_one(_home):
    out = resolve_query("/box", "~/*.csv")
    assert out == {"base": _home, "pattern": "*.csv", "mode": "glob"}


def test_resolve_tilde_path_walks_to_the_deepest_real_directory(_home):
    out = resolve_query("/box", "~/a/b/*.c")
    assert out == {"base": _home + "/a/b", "pattern": "*.c", "mode": "glob"}


def test_resolve_tilde_path_stops_at_the_first_glob_segment(_home):
    out = resolve_query("/box", "~/a/*/b.csv")
    assert out == {"base": _home + "/a", "pattern": "*/b.csv", "mode": "glob"}


def test_resolve_a_missing_named_folder_widens_instead_of_failing(_home):
    out = resolve_query("/box", "~/nope/x.csv")
    assert out == {"base": _home, "pattern": "nope/x.csv", "mode": "substring"}


def test_resolve_absolute_path_walks_the_filesystem(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    out = resolve_query("/box", f"{etc}/*/x.conf")
    assert out == {"base": norm(str(etc)), "pattern": "*/x.conf", "mode": "glob"}


def test_resolve_windows_drive_letter_path_walks_the_filesystem(monkeypatch):
    """A raw typed string can start with a drive letter instead of `/` (a
    pasted or typed Windows absolute path) — unambiguously absolute, unlike a
    bare leading `/`, so there is no depth-1-anchor fallback to consider.

    Exercised against a faked directory tree (`os.path.isdir` monkeypatched)
    rather than a real one: a Windows drive letter has no counterpart on the
    POSIX filesystem this suite otherwise runs against, real or via tmp_path,
    so this is the only way to run `_walk_from`'s directory-existence walk
    over one on any platform."""
    real_dirs = {"C:/Users", "C:/Users/example"}
    monkeypatch.setattr(os.path, "isdir", lambda p: p in real_dirs)
    out = resolve_query("/box", "C:\\Users\\example\\*.conf")
    # The implicit `**/` decision reads the RAW typed string looking for `/`
    # specifically (spec: "a slash is the only thing that limits depth") —
    # an all-backslash Windows path has none, so it widens to any depth under
    # the resolved base exactly like a slash-free POSIX query does.
    assert out == {"base": "C:/Users/example", "pattern": "**/*.conf",
                   "mode": "glob"}


def test_resolve_windows_drive_letter_path_with_forward_slashes(monkeypatch):
    """The same absolute-path recognition fires whichever separator the
    drive-letter string uses past the colon — `C:/` is as legitimate a
    Windows spelling as `C:\\`."""
    real_dirs = {"C:/Users", "C:/Users/example"}
    monkeypatch.setattr(os.path, "isdir", lambda p: p in real_dirs)
    out = resolve_query("/box", "C:/Users/example/*.conf")
    assert out == {"base": "C:/Users/example", "pattern": "*.conf",
                   "mode": "glob"}


def test_resolve_bare_windows_drive_root_backslash_normalizes_to_slash_form():
    """A drive letter with nothing after the separator (`C:\\`, the whole
    drive) walks no segments at all — `_walk_from`'s own bare-root collapse
    (`rest` empty) leaves `base` as the bare `"C:"` `_BARE_DRIVE` matches,
    which has to come back out as `"C:/"`: canonical_root() (index/runner.py)
    stores this drive under `"C:/"`, never the bare `"C:"` spelling."""
    out = resolve_query("/box", "C:\\")
    assert out == {"base": "C:/", "pattern": "", "mode": "substring"}


def test_resolve_bare_windows_drive_root_forward_slash_normalizes_to_slash_form():
    """The forward-slash spelling of the same bare drive root."""
    out = resolve_query("/box", "C:/")
    assert out == {"base": "C:/", "pattern": "", "mode": "substring"}


def test_resolve_relative_dotdot_walks_up_the_filesystem(_home):
    """A bare relative query with a `..` segment escapes the box's own root
    the same way `~` and a leading `/` already do — it does not stay
    anchored at `root` matching the literal string `..` against an index
    that never stores that segment."""
    box = _home + "/a/b"
    out = resolve_query(box, "../*.c")
    assert out == {"base": _home + "/a", "pattern": "*.c", "mode": "glob"}


def test_resolve_relative_dotdot_can_walk_back_to_where_it_started(_home):
    box = _home + "/a/b"
    out = resolve_query(box, "../../a/b/*.c")
    assert out == {"base": box, "pattern": "*.c", "mode": "glob"}


def test_resolve_relative_dotdot_to_a_missing_folder_widens_instead_of_failing(_home):
    box = _home + "/a/b"
    out = resolve_query(box, "../nope/x.csv")
    assert out == {"base": _home + "/a", "pattern": "nope/x.csv",
                   "mode": "substring"}


def test_resolve_relative_dotdot_clamps_at_the_filesystem_root(monkeypatch):
    """A run of `..` longer than the tree is deep keeps landing on real
    directories the whole way (`/..` is `/`, same as `cd`), so the walk
    never manufactures a fictitious base — it just stops climbing once it
    is at the root, same as every other consumed segment."""
    real_dirs = {"/", "/etc"}
    monkeypatch.setattr(
        os.path, "isdir",
        lambda p: os.path.normpath(p) in real_dirs)
    out = resolve_query("/", "../../../etc/*.conf")
    assert out == {"base": "/etc", "pattern": "*.conf", "mode": "glob"}


def test_resolve_leading_slash_with_no_real_directory_stays_anchored():
    """The disambiguation's other branch: a leading `/` whose first segment
    is not a real directory (here, none of `/nonexistent-xyz` exists) is read
    as the depth-1 anchor, not an absolute path."""
    out = resolve_query("/box", "/nonexistent-xyz/*.csv")
    assert out == {"base": "/box", "pattern": "nonexistent-xyz/*.csv",
                   "mode": "glob"}


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
