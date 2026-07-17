"""Tests for the duckdb template's reader.py + writer.py (parquet/csv/tsv/json).

Skipped when duckdb isn't installed. (It's now a core dependency, but the base
test env may still lack it; guard so the suite degrades gracefully.)
"""
import importlib.util
import json
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


# -------------------------------------------- page SQL / pushdown-safe paging

def test_parquet_page_sql_uses_file_row_number(parquet_file):
    # The parquet page must get its physical-position key from read_parquet's
    # file_row_number pseudo-column (which preserves predicate + row-group
    # pushdown), NOT a row_number() window (which blocks both). EXCLUDE drops the
    # pseudo-column from the visible page so it isn't rendered as a data column.
    rel = reader.relation_for(parquet_file)
    sql = reader._page_sql(parquet_file, rel, "", "")
    assert "file_row_number=true" in reader.relation_for(parquet_file, file_row_number=True)
    assert "file_row_number AS" in sql
    assert "EXCLUDE (file_row_number)" in sql
    assert "row_number() OVER" not in sql


def test_parquet_unsorted_page_sql_prunes_by_file_row_number_range(parquet_file):
    # The common first-page case (no sort, no filter) expresses its window as a
    # file_row_number range predicate so DuckDB prunes to the covering row
    # groups instead of reading ~threads groups ahead before LIMIT halts it.
    rel = reader.relation_for(parquet_file)
    sql = reader._page_sql(parquet_file, rel, "", "", offset=100, limit=50)
    assert "file_row_number >= 100 AND file_row_number < 150" in sql
    assert "LIMIT ? OFFSET ?" not in sql          # window is the predicate now
    # A sort or filter must NOT prune by physical position (it no longer tracks
    # logical page order) — those keep the LIMIT/OFFSET window.
    sorted_sql = reader._page_sql(parquet_file, rel, "", ' ORDER BY "id" ASC',
                                  offset=100, limit=50)
    assert "file_row_number >= 100" not in sorted_sql
    assert "LIMIT ? OFFSET ?" in sorted_sql
    filtered_sql = reader._page_sql(parquet_file, rel, ' WHERE "id" > 5', "",
                                    offset=100, limit=50)
    assert "file_row_number >= 100" not in filtered_sql
    assert "LIMIT ? OFFSET ?" in filtered_sql


def test_page_branch_keys_on_scan_ext_not_file_ext(tmp_path):
    # The natural-order pruning branch must be chosen by the SCAN's logical ext
    # (what _page_sql itself branches on), not the file's. They can diverge — a
    # source_url whose splitext-visible extension isn't .parquet while the local
    # file is. If the branch keyed on the file ext it would take the no-bind
    # range path while _page_sql emits a LIMIT ? OFFSET ? query -> a bind-count
    # error. Here scan is a real .json (non-parquet ext) with the file ext
    # forced to .parquet: the robust LIMIT/OFFSET path must run and read it.
    jp = _make(tmp_path, "s.json",
               "SELECT range AS id, 'n'||range AS name FROM range(5)")
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA enable_object_cache=true")
    try:
        out = reader._read_flat(jp, jp, con, ".parquet", 0, 3,
                                None, None, "page")
    finally:
        con.close()
    assert out["ids"] == [0, 1, 2]
    assert [r["id"] for r in out["rows"]] == [0, 1, 2]


def _direct_page_positions(path, offset, limit):
    """Reference window: the plain LIMIT/OFFSET natural-order scan the pruning
    path replaces. Its rows/positions must match exactly."""
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT file_row_number FROM read_parquet('{path}', "
        f"file_row_number=true) LIMIT {limit} OFFSET {offset}").fetchall()
    con.close()
    return [r[0] for r in rows]


def test_pruned_page_matches_limit_offset_scan(grouped_parquet):
    # Offsets landing in the first, a middle, and the last row group, plus one
    # past EOF (empty). Each pruned page must return exactly the rows/positions
    # the LIMIT/OFFSET scan would.
    for offset in (0, 100, 5000, 9950, 10000, 12000):
        out = reader.main(grouped_parquet, mode="page", offset=offset, limit=50)
        expected = _direct_page_positions(grouped_parquet, offset, 50)
        assert out["ids"] == expected, offset
        assert [r["seq"] for r in out["rows"]] == expected, offset


def test_pruned_page_over_http_preserves_file_order(grouped_parquet):
    # The pruned first page has no ORDER BY — it relies on
    # preserve_insertion_order to return rows in file_row_number order. Prove
    # that holds on the remote path this fix actually targets: the shared
    # _http_connection runs SET threads=32, where an unordered parallel scan
    # could otherwise interleave row groups. Windows straddling the 2048-row
    # group boundary exercise the multi-group case.
    srv, hits = _serve_dir(os.path.dirname(grouped_parquet))
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/g.parquet"
        try:
            reader.main(grouped_parquet, mode="page", limit=1, source_url=url)
        except Exception:
            pytest.skip("duckdb httpfs extension unavailable")
        for offset in (0, 2000, 2048, 4096, 6000):
            out = reader.main(grouped_parquet, mode="page", offset=offset,
                              limit=100, source_url=url)
            expected = list(range(offset, offset + 100))
            assert out["ids"] == expected, offset
            assert [r["seq"] for r in out["rows"]] == expected, offset
    finally:
        srv.shutdown()


def test_pruned_page_projection_matches_scan(grouped_parquet):
    # A narrow projection on the pruned path still keys rows by physical
    # position and returns exactly the window's rows.
    out = reader.main(grouped_parquet, mode="page", columns=["seq"],
                      offset=4096, limit=10)
    assert out["columns"] == ["seq"]
    assert out["ids"] == list(range(4096, 4106))
    assert [r["seq"] for r in out["rows"]] == list(range(4096, 4106))


def test_csv_page_sql_keeps_window(csv_file):
    # CSV/JSON can't prune, and have no file_row_number, so they keep the
    # streaming row_number() window.
    rel = reader.relation_for(csv_file)
    sql = reader._page_sql(csv_file, rel, "", "")
    assert "row_number() OVER" in sql
    assert "file_row_number" not in sql


def test_parquet_filter_sort_positions_via_file_row_number(parquet_file):
    # End-to-end through the file_row_number path: a filtered + sorted page still
    # returns the physical file positions the writer edits by.
    out = reader.main(parquet_file,
                      filters=[{"column": "id", "op": ">=", "value": "200"}],
                      sort={"column": "id", "dir": "desc"})
    assert out["rows"][0]["id"] == 249
    assert out["ids"][0] == 249                   # physical position, sorted desc
    assert "file_row_number" not in out["columns"]  # pseudo-column not leaked


def test_sorted_page_sql_appends_position_tiebreaker(parquet_file, csv_file):
    # A user sort with tied values would otherwise make the LIMIT/OFFSET window
    # itself nondeterministic — fatal for the parallel per-column batches,
    # which are separate queries that must resolve the same page to the same
    # rows (a batch's unmatched ids merge to nothing and render as fake NULLs).
    order = ' ORDER BY "name" ASC'
    psql = reader._page_sql(parquet_file, reader.relation_for(parquet_file), "", order)
    assert f'{order}, file_row_number LIMIT' in psql
    csql = reader._page_sql(csv_file, reader.relation_for(csv_file), "", order)
    assert f'{order}, {reader._POS} LIMIT' in csql
    # No sort needs no tiebreaker: the unordered window is already
    # deterministic under DuckDB's default preserve_insertion_order.
    assert "ORDER BY" not in reader._page_sql(
        parquet_file, reader.relation_for(parquet_file), "", "")


def test_sorted_ties_resolve_to_same_rows_across_column_batches(tmp_path):
    # Regression for the batched-load window: every row shares one sort value,
    # so the whole page is one big tie group. Two batch calls projecting
    # different columns must land on identical ids.
    p = _make(tmp_path, "t.parquet",
              "SELECT range AS id, 0 AS tie, 'n'||range AS name FROM range(250)")
    a = reader.main(p, mode="page", columns=["id"],
                    sort={"column": "tie", "dir": "asc"}, offset=100, limit=50)
    b = reader.main(p, mode="page", columns=["name"],
                    sort={"column": "tie", "dir": "asc"}, offset=100, limit=50)
    assert a["ids"] == b["ids"] == list(range(100, 150))
    assert b["rows"][0]["name"] == "n100"


# ----------------------------------------- two-phase load (positions mode)

def test_positions_mode_resolves_sorted_filtered_window(parquet_file):
    # Phase one: the page window as file positions, with sort + filter + offset
    # applied exactly as a direct page call would.
    pos = reader.main(parquet_file, mode="positions",
                      sort={"column": "id", "dir": "desc"},
                      filters=[{"column": "id", "op": "<", "value": 200}],
                      offset=10, limit=5)
    assert pos == {"positions": [189, 188, 187, 186, 185]}
    page = reader.main(parquet_file, mode="page",
                       sort={"column": "id", "dir": "desc"},
                       filters=[{"column": "id", "op": "<", "value": 200}],
                       offset=10, limit=5)
    assert pos["positions"] == page["ids"]


def test_positions_mode_pins_ties(tmp_path):
    # All rows tie on the sort value; the position tiebreaker must make the
    # window deterministic (same guarantee the one-phase page SQL gives).
    p = _make(tmp_path, "t.parquet",
              "SELECT range AS id, 0 AS tie FROM range(250)")
    pos = reader.main(p, mode="positions",
                      sort={"column": "tie", "dir": "asc"}, offset=100, limit=50)
    assert pos["positions"] == list(range(100, 150))


# ------------------------------------------- row-group pruning (positions)

@pytest.fixture
def grouped_parquet(tmp_path):
    """5 row groups of 2048 rows (DuckDB clamps ROW_GROUP_SIZE below 2048 up,
    so this is the smallest multi-group file it writes). `seq` correlates
    with file order (footer stats prune it); `scat` is hash-scattered so
    every group spans the whole value range (provably unprunable); `seq_n`
    is seq with a NULL tail."""
    p = tmp_path / "g.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT range AS seq, (range * 37) % 10000 AS scat, "
        "CASE WHEN range >= 9000 THEN NULL ELSE range END AS seq_n "
        f"FROM range(10000)) TO '{p}' (FORMAT parquet, ROW_GROUP_SIZE 2048)")
    con.close()
    return str(p)


def _unpruned_positions(path, col, d, offset, limit):
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT file_row_number FROM read_parquet('{path}', "
        f"file_row_number=true) ORDER BY {col} {d}, file_row_number "
        f"LIMIT {limit} OFFSET {offset}").fetchall()
    con.close()
    return [r[0] for r in rows]


def test_pruned_positions_match_full_scan(grouped_parquet):
    for col in ("seq", "scat", "seq_n"):
        for d in ("asc", "desc"):
            for offset in (0, 2500, 9500):
                got = reader.main(grouped_parquet, mode="positions",
                                  sort={"column": col, "dir": d},
                                  offset=offset, limit=50)["positions"]
                assert got == _unpruned_positions(
                    grouped_parquet, col, d, offset, 50), (col, d, offset)


def test_prune_restricts_correlated_column(grouped_parquet):
    con = duckdb.connect()
    types = {r[0]: r[1] for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{grouped_parquet}')").fetchall()}
    clause = reader._rowgroup_prune(
        con, grouped_parquet, {"column": "seq", "dir": "desc"}, types, 100)
    # top 100 of a file-ordered column live in exactly the last group
    assert clause == " WHERE (file_row_number BETWEEN 8192 AND 9999)"


def test_prune_bails_on_scattered_and_null_tail(grouped_parquet):
    con = duckdb.connect()
    types = {r[0]: r[1] for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{grouped_parquet}')").fetchall()}
    # scattered: every group's [min,max] covers the range -> nothing provable
    assert reader._rowgroup_prune(
        con, grouped_parquet, {"column": "scat", "dir": "desc"}, types, 100) == ""
    # window deep enough to reach the NULL tail (9000 non-null rows)
    assert reader._rowgroup_prune(
        con, grouped_parquet, {"column": "seq_n", "dir": "asc"}, types, 9500) == ""


def test_page_with_positions_fetches_exactly_those_rows(parquet_file):
    # Phase two: a column batch hands the resolved positions back and gets
    # exactly those rows — sort/filters/offset/limit are ignored alongside
    # them (positions are authoritative; re-filtering could drop rows the
    # window already committed to).
    out = reader.main(parquet_file, mode="page", columns=["name"],
                      positions=[42, 7, 199],
                      filters=[{"column": "id", "op": "<", "value": 5}],
                      offset=90, limit=2)
    assert sorted(out["ids"]) == [7, 42, 199]
    by_id = dict(zip(out["ids"], out["rows"]))
    assert by_id[42] == {"name": "n42"}
    assert out["columns"] == ["name"]


def test_page_with_empty_positions_is_empty(parquet_file):
    # A filter that matches nothing resolves to zero positions; the batch
    # calls still answer cleanly with an empty page.
    out = reader.main(parquet_file, mode="page", columns=["name"], positions=[])
    assert out["ids"] == [] and out["rows"] == []


def test_two_phase_equals_single_sorted_page(parquet_file):
    # End-to-end equivalence: positions + per-batch fetch reassembles the very
    # rows the one-query sorted page returns.
    kw = dict(sort={"column": "name", "dir": "asc"}, offset=20, limit=10)
    direct = reader.main(parquet_file, mode="page", **kw)
    pos = reader.main(parquet_file, mode="positions", **kw)["positions"]
    merged = {}
    for cols in (["id"], ["name"]):
        batch = reader.main(parquet_file, mode="page", columns=cols,
                            positions=pos)
        for rid, row in zip(batch["ids"], batch["rows"]):
            merged.setdefault(rid, {}).update(row)
    assert [merged[p] for p in pos] == direct["rows"]


def test_positions_requires_parquet(csv_file):
    # CSV/JSON pages are a single call — no batches, so no two-phase. The
    # numbered-window subquery has no prunable position column anyway.
    with pytest.raises(ValueError):
        reader.main(csv_file, mode="positions", sort={"column": "id", "dir": "asc"})
    with pytest.raises(ValueError):
        reader.main(csv_file, mode="page", positions=[1, 2])


# --------------------------------------------------- deferred count (mode)

def test_page_mode_defers_count(parquet_file):
    # "page" returns the rows but leaves total_rows None — the grid fetches the
    # count separately so the page can render before the (heavy) count finishes.
    out = reader.main(parquet_file, mode="page")
    assert out["total_rows"] is None
    assert len(out["rows"]) == 100
    assert out["ids"] == list(range(100))
    assert out["editable"] is True


def test_count_mode_returns_total_only(parquet_file):
    out = reader.main(parquet_file, mode="count")
    assert out == {"total_rows": 250}


def test_count_mode_respects_filter(parquet_file):
    out = reader.main(parquet_file, mode="count",
                      filters=[{"column": "id", "op": ">=", "value": "200"}])
    assert out == {"total_rows": 50}


def test_full_mode_is_default_and_inlines_count(parquet_file):
    # The default (no mode) still returns the count inline, so existing callers
    # and the .py test suite are unaffected.
    assert reader.main(parquet_file)["total_rows"] == 250


def test_duckdb_count_mode(duckdb_db):
    assert reader.main(duckdb_db, table="actor", mode="count") == {"total_rows": 5}


def test_duckdb_page_mode_defers_count(duckdb_db):
    out = reader.main(duckdb_db, table="actor", mode="page")
    assert out["total_rows"] is None
    assert out["ids"] == [0, 1, 2, 3, 4]
    assert out["editable"] is True


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


# ---------------------------------------------------- fs-level read-only file

@pytest.fixture
def readonly_parquet(parquet_file):
    os.chmod(parquet_file, 0o444)
    yield parquet_file
    os.chmod(parquet_file, 0o644)  # so tmp_path cleanup works


def test_reader_readonly_file_not_editable(readonly_parquet):
    out = reader.main(readonly_parquet)
    assert out["editable"] is False
    assert out["total_rows"] == 250                  # still viewable
    assert "read-only" in out["readonly_message"].lower()
    assert out["readonly_tooltip"]


def test_writer_refuses_readonly_file(readonly_parquet):
    before = os.stat(readonly_parquet).st_size
    with pytest.raises(PermissionError):
        writer.main(readonly_parquet,
                    edits=[{"row": 0, "column": "name", "value": "x"}])
    assert os.stat(readonly_parquet).st_size == before  # bytes untouched



# ---------------------------------------------------- remote source_url path
# The page passes source_url (the app's /api/fs/raw URL) when the shell marks
# the file remote; the reader scans that URL instead of the local path, and
# falls back to the path on any failure. The reader itself knows nothing about
# mounts — just "bytes are also available here".


def _require_httpfs():
    """Skip the calling test when the httpfs extension can't be loaded (e.g.
    no network access to extensions.duckdb.org from this CI environment).

    reader.main() itself never raises on a missing httpfs — it's designed to
    fall back to a plain local read (see reader.py's `_http_connection`
    docstring) — so probing it directly here, instead of wrapping the
    reader.main() call in try/except, is what actually catches this case
    rather than silently exercising the fallback path and failing later on
    the "reader never hit the URL" assertion."""
    con = duckdb.connect(":memory:")
    try:
        con.execute("LOAD httpfs")
    except duckdb.Error:
        pytest.skip("duckdb httpfs extension unavailable")
    finally:
        con.close()


def _serve_dir(directory):
    """A local HTTP server over `directory` that records request paths."""
    import functools
    import http.server
    import threading

    hits = []

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            return super().do_GET()

        def do_HEAD(self):
            hits.append(self.path)
            return super().do_HEAD()

    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(H, directory=directory))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


def test_source_url_reads_over_http(parquet_file):
    _require_httpfs()
    srv, hits = _serve_dir(os.path.dirname(parquet_file))
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/s.parquet"
        out = reader.main(parquet_file, limit=5, source_url=url)
        assert [r["id"] for r in out["rows"]] == [0, 1, 2, 3, 4]
        assert any("s.parquet" in h for h in hits), "reader never hit the URL"
    finally:
        srv.shutdown()


def test_http_connection_persists_across_reads(parquet_file):
    # The shared connection is stashed on the duckdb module so it outlives a
    # single reader run; with parquet_metadata_cache=true set once at build,
    # the SECOND open in a server session reuses the parsed footer instead of
    # re-downloading/re-parsing it (the ~7s cold DESCRIBE is paid only once).
    _require_httpfs()
    srv, hits = _serve_dir(os.path.dirname(parquet_file))
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/s.parquet"
        reader.main(parquet_file, limit=5, source_url=url)
        con1 = getattr(duckdb, "_fused_render_http_con_v3", None)
        assert con1 is not None                   # stashed for reuse
        reader.main(parquet_file, limit=5, source_url=url)
        con2 = getattr(duckdb, "_fused_render_http_con_v3", None)
        assert con2 is con1                        # same connection -> warm cache
    finally:
        srv.shutdown()


def test_source_url_falls_back_when_dead(parquet_file):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    out = reader.main(parquet_file, limit=5,
                      source_url=f"http://127.0.0.1:{dead}/s.parquet")
    assert [r["id"] for r in out["rows"]] == [0, 1, 2, 3, 4]
    assert out["total_rows"] == 250


def test_source_url_ignored_unless_http(parquet_file):
    # A non-URL value must not be handed to DuckDB as a scan target.
    out = reader.main(parquet_file, limit=3, source_url="garbage; DROP TABLE x")
    assert len(out["rows"]) == 3


def test_source_url_reads_csv_over_http(csv_file):
    # CSV/TSV/JSON take the URL too, not just parquet: a mount-backed file is
    # scanned over the serve where hammering the NFS mount with the scan risks
    # dropping the whole mount. Reading the whole file over HTTP is slow-but-
    # safe (and the serve's shared VFS cache makes the repeat reads cheap).
    _require_httpfs()
    srv, hits = _serve_dir(os.path.dirname(csv_file))
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/s.csv"
        out = reader.main(csv_file, limit=3, source_url=url)
        assert out["rows"][0]["zip"] == "00000"   # all-varchar preserved
        assert any("s.csv" in h for h in hits), "reader never hit the URL"
    finally:
        srv.shutdown()


def test_source_url_reads_json_over_http(tmp_path):
    _require_httpfs()
    p = _make(tmp_path, "s.json",
              "SELECT range AS id, 'n'||range AS name FROM range(5)")
    srv, hits = _serve_dir(os.path.dirname(p))
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/s.json"
        out = reader.main(p, limit=3, source_url=url)
        assert [r["id"] for r in out["rows"]] == [0, 1, 2]
        assert out["editable"] is False           # JSON stays view-only
        assert any("s.json" in h for h in hits), "reader never hit the URL"
    finally:
        srv.shutdown()


def test_source_url_csv_falls_back_when_dead(csv_file):
    # A dead serve URL must not error the CSV read — fall back to the path.
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    out = reader.main(csv_file, limit=3,
                      source_url=f"http://127.0.0.1:{dead}/s.csv")
    assert out["rows"][0]["zip"] == "00000"
    assert out["total_rows"] == 250


# ------------------------------------- schema mode / per-column projection


def test_schema_mode_returns_no_rows(parquet_file):
    out = reader.main(parquet_file, mode="schema")
    assert out["columns"] == ["id", "name"]
    assert out["types"] == {"id": "BIGINT", "name": "VARCHAR"}
    assert out["rows"] == [] and out["ids"] == []
    assert out["total_rows"] is None
    assert out["editable"] is True


def test_schema_mode_reports_parquet_column_sizes(parquet_file):
    # The grid packs columns into byte-budgeted batches from these: compressed
    # first-row-group bytes per column, straight from the footer.
    out = reader.main(parquet_file, mode="schema")
    assert set(out["col_sizes"]) == {"id", "name"}
    assert all(isinstance(v, int) and v > 0 for v in out["col_sizes"].values())


def test_schema_mode_col_sizes_empty_for_csv(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("a,b\n1,x\n2,y\n")
    out = reader.main(str(p), mode="schema")
    assert out["col_sizes"] == {}


def test_page_with_columns_projects(parquet_file):
    out = reader.main(parquet_file, mode="page", columns=["name"], limit=3)
    assert out["columns"] == ["name"]
    assert out["rows"] == [{"name": "n0"}, {"name": "n1"}, {"name": "n2"}]
    assert out["ids"] == [0, 1, 2]  # position key still present


def test_columns_projection_respects_filter_and_sort(parquet_file):
    # Filter/sort columns need not be in the projection; ids must still be the
    # physical positions so parallel per-column pages align by id.
    out = reader.main(parquet_file, mode="page", columns=["name"],
                      filters=[{"column": "id", "op": ">=", "value": "200"}],
                      sort={"column": "id", "dir": "desc"}, limit=3)
    assert out["ids"] == [249, 248, 247]
    assert out["rows"][0] == {"name": "n249"}


def test_unknown_columns_fall_back_to_all(parquet_file):
    out = reader.main(parquet_file, mode="page", columns=["nope"], limit=2)
    assert out["columns"] == ["id", "name"]


def test_columns_projection_on_csv(csv_file):
    # The CSV window path also supports projection (unused by the grid today,
    # but the param must not corrupt the row_number position key).
    out = reader.main(csv_file, mode="page", columns=["zip"], limit=2)
    assert out["columns"] == ["zip"]
    assert out["ids"] == [0, 1]
    assert out["rows"][0] == {"zip": "00000"}
