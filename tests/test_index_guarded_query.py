"""User SQL against the index, confined. See index/specs/query.md §5.

Two independent guards, and the tests are split along them because they fail
differently: the statement-type gate refuses before anything executes, and the
DuckDB lockdown refuses paths at execution. Neither is belt-and-braces — after
`lock_configuration=true` an in-memory INSERT still succeeds, and
`allowed_directories` on its own confines nothing at all.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index import guarded_query
from fused_render.index.config import IndexConfig
from fused_render.index.guarded_query import MAX_LIMIT, run_guarded
from fused_render.index.store import Sink, compact, schemas


def _cfg(tmp_path):
    return IndexConfig(dir=str(tmp_path / "ix"))


def _index(tmp_path, root="/r", paths=("/r/a.txt", "/r/b.md"), sizes=None):
    cfg = _cfg(tmp_path)
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    by_dir = {}
    for i, p in enumerate(paths):
        d, name = p.rsplit("/", 1)
        ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
        size = (sizes or {}).get(p, 10)
        by_dir.setdefault(d, []).append((p, d, name, ext, size, 100.0 + i))
    for d, rows in by_dir.items():
        sink.add(d, "s", ("sig", rows, sum(r[4] for r in rows), 1, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


# -- the read path works -------------------------------------------------------

def test_a_select_over_files_answers(tmp_path):
    out = run_guarded(_index(tmp_path), "SELECT name, size FROM files ORDER BY name")
    assert out["columns"] == ["name", "size"]
    assert out["rows"] == [["a.txt", 10], ["b.md", 10]]
    assert out["truncated"] is False


def test_a_select_over_dirs_answers(tmp_path):
    out = run_guarded(_index(tmp_path), "SELECT dir FROM dirs")
    assert out["rows"] == [["/r"]]


def test_the_two_views_carry_the_stored_schemas(tmp_path):
    """The empty-index views are written out by hand, so they can drift from
    store.schemas. DESCRIBE is the check that they haven't."""
    file_schema, dir_schema = schemas(pa)
    cfg = _index(tmp_path)
    for table, schema in (("files", file_schema), ("dirs", dir_schema)):
        got = [r[0] for r in run_guarded(cfg, f"DESCRIBE {table}")["rows"]]
        assert got == list(schema.names)


def test_an_empty_index_answers_with_no_rows_rather_than_an_error(tmp_path):
    cfg = _cfg(tmp_path)
    assert run_guarded(cfg, "SELECT count(*) FROM files")["rows"] == [[0]]
    assert run_guarded(cfg, "SELECT count(*) FROM dirs")["rows"] == [[0]]
    # …and the empty views still carry the real column names, so a query
    # written against a built index does not fail differently on an empty one.
    file_schema, dir_schema = schemas(pa)
    for table, schema in (("files", file_schema), ("dirs", dir_schema)):
        got = [r[0] for r in run_guarded(cfg, f"DESCRIBE {table}")["rows"]]
        assert got == list(schema.names)


def test_the_files_view_reads_only_the_manifests_partitions(tmp_path):
    """A glob of the files dir would read the PREVIOUS generation too — the
    store leaves it on disk for readers still holding the old manifest — and
    silently double every count."""
    cfg = _index(tmp_path, paths=("/r/a.txt",))
    _index(tmp_path, paths=("/r/a.txt", "/r/b.txt"))  # second generation
    on_disk = len([f for f in os.listdir(cfg.files_dir) if f.endswith(".parquet")])
    assert on_disk > 1  # the previous generation is still there
    assert run_guarded(cfg, "SELECT count(*) FROM files")["rows"] == [[2]]


# -- the statement-type gate ---------------------------------------------------

@pytest.mark.parametrize("sql", [
    "INSERT INTO files VALUES ('/x', '/', 'x', '', 1, 1.0, 1)",
    "DELETE FROM files",
    "UPDATE files SET size = 0",
    "CREATE TABLE t AS SELECT 1",
    "CREATE VIEW v AS SELECT 1",
    "DROP VIEW files",
    "COPY files TO '/tmp/fused-guarded-should-not-exist.csv'",
    "ATTACH '/tmp/fused-guarded.db' AS other",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET enable_external_access=true",
    "RESET allowed_directories",
])
def test_a_non_read_statement_is_refused_before_it_runs(tmp_path, sql):
    """The gate is LOAD-BEARING, not defence in depth: `lock_configuration`
    stops the settings changing, and stops nothing else — an in-memory INSERT
    or CREATE TABLE still succeeds behind it."""
    with pytest.raises(ValueError, match="read-only"):
        run_guarded(_index(tmp_path), sql)


def test_a_copy_that_was_refused_wrote_nothing(tmp_path):
    out = tmp_path / "out.csv"
    with pytest.raises(ValueError):
        run_guarded(_index(tmp_path), f"COPY files TO '{out}'")
    assert not out.exists()


def test_more_than_one_statement_is_refused(tmp_path):
    with pytest.raises(ValueError, match="one statement"):
        run_guarded(_index(tmp_path), "SELECT 1; DROP VIEW files")


def test_an_empty_statement_is_refused(tmp_path):
    for sql in ("", "   ", "-- nothing\n"):
        with pytest.raises(ValueError):
            run_guarded(_index(tmp_path), sql)


def test_unparseable_sql_is_an_error_not_a_crash(tmp_path):
    with pytest.raises(ValueError):
        run_guarded(_index(tmp_path), "SELEKT * FROM files")


def test_the_read_only_statement_types_are_allowed(tmp_path):
    """PRAGMA and CALL are named explicitly because they are already reachable:
    duckdb parses `PRAGMA database_list` as StatementType.SELECT, so a gate that
    admitted only SELECT would admit them anyway while claiming not to."""
    cfg = _index(tmp_path)
    for sql in ("PRAGMA database_list",
                "CALL pragma_version()",
                "DESCRIBE SELECT 1",
                "EXPLAIN SELECT 1",
                "WITH x AS (SELECT 1 c) SELECT c FROM x"):
        assert isinstance(run_guarded(cfg, sql)["rows"], list)


# -- the lockdown --------------------------------------------------------------

@pytest.mark.parametrize("fn", ["read_csv", "read_parquet", "read_text",
                                "read_blob"])
def test_a_file_outside_the_index_directory_cannot_be_read(tmp_path, fn):
    """`allowed_directories` alone confines NOTHING — it is a carve-out from
    `enable_external_access=false`, not a restriction — so this only holds
    because external access is off and the configuration is locked."""
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        run_guarded(_index(tmp_path), f"SELECT * FROM {fn}('{secret}')")
    assert "Permission Error" in str(exc.value)


def test_the_allowlist_cannot_be_widened_from_inside_a_query(tmp_path):
    with pytest.raises(ValueError, match="read-only"):
        run_guarded(_index(tmp_path), "SET allowed_directories=['/']")


def test_a_glob_outside_the_index_directory_is_refused(tmp_path):
    (tmp_path / "leak.parquet").write_bytes(b"not really")
    with pytest.raises(Exception):
        run_guarded(_index(tmp_path),
                    f"SELECT * FROM glob('{tmp_path}/*')")


# -- the row cap --------------------------------------------------------------

def test_the_row_cap_is_enforced_and_flagged(tmp_path):
    cfg = _index(tmp_path, paths=[f"/r/f{i}.txt" for i in range(20)])
    out = run_guarded(cfg, "SELECT path FROM files ORDER BY path", limit=5)
    assert len(out["rows"]) == 5
    assert out["truncated"] is True


def test_a_result_that_fits_is_not_flagged(tmp_path):
    cfg = _index(tmp_path, paths=[f"/r/f{i}.txt" for i in range(3)])
    out = run_guarded(cfg, "SELECT path FROM files", limit=5)
    assert len(out["rows"]) == 3
    assert out["truncated"] is False


def test_the_callers_limit_cannot_exceed_the_server_cap(tmp_path):
    cfg = _index(tmp_path, paths=[f"/r/f{i}.txt" for i in range(3)])
    out = run_guarded(cfg, "SELECT path FROM files", limit=MAX_LIMIT * 100)
    assert len(out["rows"]) == 3  # clamped, not honoured
    assert run_guarded(cfg, "SELECT path FROM files", limit=0)["rows"] == []


def test_the_users_own_limit_still_applies(tmp_path):
    cfg = _index(tmp_path, paths=[f"/r/f{i}.txt" for i in range(20)])
    out = run_guarded(cfg, "SELECT path FROM files LIMIT 2", limit=10)
    assert len(out["rows"]) == 2
    assert out["truncated"] is False


# -- the shape that leaves the process ----------------------------------------

def test_a_runaway_statement_is_interrupted(tmp_path, monkeypatch):
    """A cross join is trivial to type and impossible to bound by inspection,
    and the caller is a text box — so the timeout is the only backstop there is."""
    monkeypatch.setattr(guarded_query, "TIMEOUT_S", 0.05)
    with pytest.raises(Exception) as exc:
        run_guarded(_index(tmp_path),
                    "SELECT count(*) FROM range(100000000) a, range(1000) b")
    assert "interrupt" in str(exc.value).lower()


def test_values_that_json_cannot_hold_leave_as_strings(tmp_path):
    out = run_guarded(_index(tmp_path),
                      "SELECT DATE '2020-01-01' d, CAST(1 AS HUGEINT) h, "
                      "'x'::BLOB b")
    assert out["rows"] == [["2020-01-01", 1, "x"]]
