"""Tests for the duckdb template's reader.py + writer.py (parquet/csv/tsv/json).

Skipped when duckdb isn't installed. (It's now a core dependency, but the base
test env may still lack it; guard so the suite degrades gracefully.)
"""
import importlib.util
import os

import pytest

pytest.importorskip("duckdb")
import duckdb  # noqa: E402


def _load(name):
    path = os.path.join(os.path.dirname(__file__), "..", "fused_render",
                        "templates", "duckdb", name)
    spec = importlib.util.spec_from_file_location(f"duckdb_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reader = _load("reader.py")
writer = _load("writer.py")


# ---------------------------------------------------------------- fixtures

def _make(tmp_path, name, sql):
    p = tmp_path / name
    fmt = {"parquet": "(FORMAT parquet)", "csv": "(FORMAT csv, HEADER)",
           "tsv": "(FORMAT csv, HEADER, DELIMITER '\t')",
           "json": "(FORMAT json, ARRAY true)"}[name.rsplit(".", 1)[1]]
    con = duckdb.connect()
    con.execute(f"COPY ({sql}) TO '{p}' {fmt}")
    con.close()
    return str(p)


@pytest.fixture
def parquet_file(tmp_path):
    return _make(tmp_path, "s.parquet",
                 "SELECT range AS id, 'n'||range AS name FROM range(250)")


@pytest.fixture
def csv_file(tmp_path):
    # Leading-zero value that must NOT become an integer on read/rewrite.
    return _make(tmp_path, "s.csv",
                 "SELECT range AS id, printf('%05d', range) AS zip FROM range(250)")


def _make_compressed(tmp_path, name, sql, opts):
    """Write a gzip/zstd-compressed csv/tsv/json via DuckDB COPY, matching what
    the grid must read back through the same auto-decompressing scan."""
    p = tmp_path / name
    con = duckdb.connect()
    con.execute(f"COPY ({sql}) TO '{p}' {opts}")
    con.close()
    return str(p)


@pytest.fixture
def csv_gz_file(tmp_path):
    return _make_compressed(
        tmp_path, "s.csv.gz",
        "SELECT range AS id, printf('%05d', range) AS zip FROM range(250)",
        "(FORMAT csv, HEADER, COMPRESSION gzip)")


@pytest.fixture
def duckdb_db(tmp_path):
    """A .duckdb database file with two base tables and a view — the multi-
    relation, edit-in-place case (vs the single-relation flat files above)."""
    p = tmp_path / "s.duckdb"
    con = duckdb.connect(str(p))
    con.execute("CREATE TABLE actor(first_name TEXT, last_name TEXT)")
    con.executemany("INSERT INTO actor VALUES (?, ?)",
                    [(f"F{i}", f"L{i}") for i in range(5)])
    con.execute("CREATE TABLE film(title TEXT)")
    con.execute("INSERT INTO film VALUES ('x')")
    con.execute("CREATE VIEW adults AS SELECT * FROM actor")
    con.close()
    return str(p)


# ------------------------------------------------------------------ reader

def test_parquet_shape(parquet_file):
    out = reader.main(parquet_file)
    assert out["columns"] == ["id", "name"]
    assert out["total_rows"] == 250
    assert len(out["rows"]) == 100
    assert out["rows"][0] == {"id": 0, "name": "n0"}
    assert out["ids"] == list(range(100))  # file positions
    assert out["editable"] is True


def test_types_reported(parquet_file):
    out = reader.main(parquet_file)
    # DuckDB infers real types from parquet; the grid labels headers with these.
    assert out["types"] == {"id": "BIGINT", "name": "VARCHAR"}


def test_csv_types_are_all_varchar(csv_file):
    # CSV is read all_varchar, so every column reports VARCHAR.
    assert set(reader.main(csv_file)["types"].values()) == {"VARCHAR"}


def test_offset_ids_are_absolute_positions(parquet_file):
    out = reader.main(parquet_file, offset=100, limit=10)
    assert out["ids"] == list(range(100, 110))
    assert out["rows"][0]["id"] == 100


# ------------------------------------------------------------- sort / filter

def test_sort_desc_ids_track_physical_position(parquet_file):
    # Sorting by name descending puts 'n99' first (largest string). Its id — and
    # thus its physical file position — is 99, so ids[0] must be 99, not 0.
    # This is what keeps edits correct while the grid is sorted.
    out = reader.main(parquet_file, sort={"column": "name", "dir": "desc"})
    assert out["rows"][0]["name"] == "n99"
    assert out["ids"][0] == 99
    assert out["rows"][0]["id"] == 99


def test_filter_equals_returns_matching_row(parquet_file):
    out = reader.main(parquet_file, filters=[{"column": "id", "op": "=", "value": "5"}])
    assert out["total_rows"] == 1
    assert out["ids"] == [5]
    assert out["rows"][0] == {"id": 5, "name": "n5"}


def test_filter_gte_narrows_count(parquet_file):
    out = reader.main(parquet_file, filters=[{"column": "id", "op": ">=", "value": "200"}])
    assert out["total_rows"] == 50
    assert len(out["rows"]) == 50
    assert min(r["id"] for r in out["rows"]) == 200


def test_filter_contains_matches_substring(parquet_file):
    # names containing "99" across range(250): n99 and n199.
    out = reader.main(parquet_file, filters=[{"column": "name", "op": "contains", "value": "99"}])
    assert out["total_rows"] == 2
    assert {r["name"] for r in out["rows"]} == {"n99", "n199"}


def test_filter_and_sort_stay_editable(parquet_file):
    out = reader.main(parquet_file, filters=[{"column": "id", "op": ">=", "value": "200"}],
                      sort={"column": "id", "dir": "desc"})
    assert out["editable"] is True
    assert out["rows"][0]["id"] == 249        # sorted desc within the filter
    assert out["ids"][0] == 249               # physical position preserved


def test_unknown_filter_column_is_ignored(parquet_file):
    # A column that isn't in the schema must not build into the WHERE clause
    # (no SQL error, no injection) — it's simply dropped.
    out = reader.main(parquet_file, filters=[{"column": "nope", "op": "=", "value": "x"}])
    assert out["total_rows"] == 250


def test_multiple_filters_are_anded(parquet_file):
    # Two conditions on the same column form a range (the grid's multi-filter
    # builder relies on the reader ANDing every condition together).
    out = reader.main(parquet_file, filters=[
        {"column": "id", "op": ">=", "value": "100"},
        {"column": "id", "op": "<", "value": "103"},
    ])
    assert out["total_rows"] == 3
    assert out["ids"] == [100, 101, 102]


def test_csv_read_as_text(csv_file):
    out = reader.main(csv_file)
    # all_varchar: the leading zero survives (string, not int 0).
    assert out["rows"][0]["zip"] == "00000"
    assert out["rows"][5]["zip"] == "00005"
    assert out["editable"] is True


def test_json_is_read_only(tmp_path):
    p = _make(tmp_path, "s.json", "SELECT range AS id, 'n'||range AS name FROM range(3)")
    out = reader.main(p)
    assert out["total_rows"] == 3
    assert out["editable"] is False
    assert out["readonly_message"] == "JSON"
    assert "read-only" in out["readonly_tooltip"].lower()


# ----------------------------------------------------- compressed variants

def test_csv_gz_reads_as_editable_text(csv_gz_file):
    # A gzip-compressed CSV is the same tabular data behind a compression
    # suffix — DuckDB auto-decompresses, so it reads all-VARCHAR and stays
    # editable just like a plain .csv.
    out = reader.main(csv_gz_file)
    assert out["total_rows"] == 250
    assert out["rows"][0]["zip"] == "00000"       # leading zero preserved
    assert set(out["types"].values()) == {"VARCHAR"}
    assert out["editable"] is True


def test_csv_zst_reads(tmp_path):
    p = _make_compressed(
        tmp_path, "s.csv.zst",
        "SELECT range AS id, printf('%05d', range) AS zip FROM range(10)",
        "(FORMAT csv, HEADER, COMPRESSION zstd)")
    out = reader.main(p)
    assert out["total_rows"] == 10
    assert out["editable"] is True


def test_json_gz_is_read_only(tmp_path):
    # JSON stays view-only whether or not it's compressed.
    p = _make_compressed(
        tmp_path, "s.json.gz", "SELECT range AS id FROM range(3)",
        "(FORMAT json, ARRAY true, COMPRESSION gzip)")
    out = reader.main(p)
    assert out["total_rows"] == 3
    assert out["editable"] is False
    assert out["readonly_message"] == "JSON"


def test_csv_gz_edit_round_trips_compressed(csv_gz_file):
    # Editing a compressed CSV rewrites it still-compressed; the leading-zero
    # text of untouched rows survives and the new value reads back exactly.
    writer.main(csv_gz_file, edits=[{"row": 1, "column": "zip", "value": "07001"}])
    out = reader.main(csv_gz_file)
    assert out["rows"][1]["zip"] == "07001"
    assert out["rows"][0]["zip"] == "00000"
    # still gzip on disk (magic bytes 1f 8b), not a plain-text CSV.
    with open(csv_gz_file, "rb") as f:
        assert f.read(2) == b"\x1f\x8b"


def test_limit_clamped(tmp_path):
    p = _make(tmp_path, "big.parquet", f"SELECT range AS id FROM range({reader.MAX_LIMIT + 500})")
    out = reader.main(p, limit=10 ** 9)
    assert len(out["rows"]) == reader.MAX_LIMIT


# --------------------------------------------------- .duckdb database reader

def test_duckdb_lists_tables_and_defaults_to_first(duckdb_db):
    out = reader.main(duckdb_db)
    # Base tables and the view are all selectable; sorted by name.
    assert out["tables"] == ["actor", "adults", "film"]
    assert out["table"] == "actor"               # first table, no param
    assert out["columns"] == ["first_name", "last_name"]
    assert out["ids"] == [0, 1, 2, 3, 4]         # duckdb rowids (0-based)
    assert out["rows"][0] == {"first_name": "F0", "last_name": "L0"}
    assert out["editable"] is True


def test_duckdb_table_param_selects_relation(duckdb_db):
    out = reader.main(duckdb_db, table="film")
    assert out["table"] == "film"
    assert out["total_rows"] == 1
    assert out["rows"][0] == {"title": "x"}


def test_duckdb_view_is_read_only(duckdb_db):
    out = reader.main(duckdb_db, table="adults")
    assert out["editable"] is False
    assert out["ids"] == []
    assert out["total_rows"] == 5                 # still viewable
    assert "view" in out["readonly_message"].lower()
    assert out["readonly_tooltip"]


def test_duckdb_sort_desc_keeps_rowids(duckdb_db):
    out = reader.main(duckdb_db, table="actor",
                      sort={"column": "first_name", "dir": "desc"})
    assert out["rows"][0]["first_name"] == "F4"
    assert out["ids"][0] == 4                     # rowid tracks physical row


def test_duckdb_filter_narrows(duckdb_db):
    out = reader.main(duckdb_db, table="actor",
                      filters=[{"column": "first_name", "op": "contains", "value": "3"}])
    assert out["ids"] == [3]
    assert out["rows"][0]["first_name"] == "F3"


def test_duckdb_unknown_table_falls_back_to_first(duckdb_db):
    # A stale/garbled table param must not error — fall back to a real one.
    out = reader.main(duckdb_db, table="does_not_exist")
    assert out["table"] == "actor"


# --------------------------------------------------- .duckdb database writer

def _db_rows(path, table="actor"):
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{path}' AS db (READ_ONLY)")
        return con.execute(f"SELECT rowid, * FROM db.{table} ORDER BY rowid").fetchall()
    finally:
        con.close()


def test_duckdb_edit_delete_insert(duckdb_db):
    writer.main(duckdb_db, table="actor",
                edits=[{"row": 0, "column": "first_name", "value": "EDITED"}],
                deletes=[1, 2],
                inserts=[{"first_name": "new", "last_name": "row"}])
    vals = [(r[1], r[2]) for r in _db_rows(duckdb_db)]
    assert ("EDITED", "L0") in vals
    assert ("F1", "L1") not in vals and ("F2", "L2") not in vals
    assert ("new", "row") in vals
    assert len(vals) == 5 - 2 + 1


def test_duckdb_edit_casts_and_null(duckdb_db):
    writer.main(duckdb_db, table="actor",
                edits=[{"row": 0, "column": "last_name", "value": None}])
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{duckdb_db}' AS db (READ_ONLY)")
    val = con.execute("SELECT last_name FROM db.actor WHERE rowid = 0").fetchone()[0]
    con.close()
    assert val is None


def test_duckdb_writer_rejects_view(duckdb_db):
    with pytest.raises(ValueError):
        writer.main(duckdb_db, table="adults",
                    edits=[{"row": 0, "column": "first_name", "value": "x"}])


def test_duckdb_writer_rolls_back_on_bad_cast(tmp_path):
    p = tmp_path / "n.duckdb"
    con = duckdb.connect(str(p))
    con.execute("CREATE TABLE t(id INTEGER)")
    con.execute("INSERT INTO t VALUES (1), (2)")
    con.close()
    before = _db_rows(str(p), "t")
    with pytest.raises(Exception):
        writer.main(str(p), table="t",
                    edits=[{"row": 0, "column": "id", "value": "not-a-number"}])
    assert _db_rows(str(p), "t") == before        # transaction rolled back


# ------------------------------------------------------------------ writer

def _rows(path):
    con = duckdb.connect(":memory:")
    try:
        rel = f"read_parquet('{path}')" if path.endswith(".parquet") else f"read_csv_auto('{path}', all_varchar=true)"
        return con.execute(f"SELECT * FROM {rel} ORDER BY 1").fetchall()
    finally:
        con.close()


def test_parquet_edit_delete_insert(parquet_file):
    writer.main(parquet_file,
                edits=[{"row": 2, "column": "name", "value": "EDITED"}],
                deletes=[0, 1],
                inserts=[{"id": 999, "name": "added"}])
    con = duckdb.connect(":memory:")
    rows = con.execute(f"SELECT id, name FROM read_parquet('{parquet_file}')").fetchall()
    con.close()
    ids = {r[0] for r in rows}
    assert 0 not in ids and 1 not in ids       # deleted
    assert (2, "EDITED") in rows               # edited
    assert (999, "added") in rows              # inserted
    assert len(rows) == 250 - 2 + 1


def test_parquet_edit_casts_to_column_type(parquet_file):
    # "42" is a string from the grid; it must land as the integer 42.
    writer.main(parquet_file, edits=[{"row": 0, "column": "id", "value": "42"}])
    con = duckdb.connect(":memory:")
    val = con.execute(f"SELECT id FROM read_parquet('{parquet_file}') WHERE name = 'n0'").fetchone()[0]
    con.close()
    assert val == 42 and isinstance(val, int)


def test_bad_cast_aborts_and_leaves_file_untouched(parquet_file):
    before = _rows(parquet_file)
    with pytest.raises(Exception):
        writer.main(parquet_file, edits=[{"row": 0, "column": "id", "value": "not-a-number"}])
    assert _rows(parquet_file) == before          # atomic: nothing written
    assert not any(f.endswith(f".fused-tmp.{os.getpid()}")
                   for f in os.listdir(os.path.dirname(parquet_file)))  # temp cleaned


def test_csv_edit_preserves_text(csv_file):
    writer.main(csv_file, edits=[{"row": 1, "column": "zip", "value": "07001"}])
    out = reader.main(csv_file)
    assert out["rows"][1]["zip"] == "07001"       # leading zero kept
    assert out["rows"][0]["zip"] == "00000"       # untouched rows still text


def test_null_value_writes_null(parquet_file):
    writer.main(parquet_file, edits=[{"row": 0, "column": "name", "value": None}])
    con = duckdb.connect(":memory:")
    val = con.execute(f"SELECT name FROM read_parquet('{parquet_file}') WHERE id = 0").fetchone()[0]
    con.close()
    assert val is None


def test_empty_string_is_distinct_from_null(parquet_file):
    # An empty string round-trips as "" — it must NOT collapse to NULL. The grid
    # keeps a cleared cell ("") and an explicit "Set Null" (NULL) distinct, and
    # relies on the writer preserving that difference.
    writer.main(parquet_file, edits=[{"row": 0, "column": "name", "value": ""}])
    con = duckdb.connect(":memory:")
    val = con.execute(f"SELECT name FROM read_parquet('{parquet_file}') WHERE id = 0").fetchone()[0]
    con.close()
    assert val == "" and val is not None


def test_writer_rejects_readonly_format(tmp_path):
    p = _make(tmp_path, "s.json", "SELECT 1 AS id")
    with pytest.raises(ValueError):
        writer.main(p, edits=[{"row": 0, "column": "id", "value": "2"}])
