"""Tests for the sqlite template's rowid-based editing: reader.py returns
rowids + an `editable` flag, writer.py applies a batch transactionally."""
import importlib.util
import os
import sqlite3

import pytest


def _load(name):
    path = os.path.join(os.path.dirname(__file__), "..", "fused_render",
                        "templates", "sqlite", name)
    spec = importlib.util.spec_from_file_location(f"sqlite_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reader = _load("reader.py")
writer = _load("writer.py")


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "s.db"
    sc = sqlite3.connect(str(p))
    sc.execute("CREATE TABLE people(name TEXT, age INTEGER)")
    sc.executemany("INSERT INTO people VALUES(?,?)", [(f"p{i}", i) for i in range(5)])
    sc.execute("CREATE VIEW adults AS SELECT * FROM people WHERE age >= 2")
    sc.commit()
    sc.close()
    return str(p)


def _all(db):
    sc = sqlite3.connect(db)
    rows = sc.execute("SELECT rowid, name, age FROM people ORDER BY rowid").fetchall()
    sc.close()
    return rows


# ------------------------------------------------------------------ reader

def test_reader_returns_rowids_and_editable(db):
    out = reader.main(db, table="people")
    assert out["editable"] is True
    assert out["columns"] == ["name", "age"]
    assert out["ids"] == [1, 2, 3, 4, 5]           # sqlite rowids are 1-based
    assert out["rows"][0] == {"name": "p0", "age": 0}
    # the rowid alias must not leak into the visible columns
    assert reader._RID not in out["columns"]


def test_view_is_not_editable(db):
    out = reader.main(db, table="adults")
    assert out["editable"] is False
    assert out["ids"] == []
    assert out["total_rows"] == 3                   # still viewable
    assert "view" in out["readonly_message"].lower()
    assert out["readonly_tooltip"]                  # non-empty explanation


def test_reader_returns_declared_types(db):
    # Declared SQLite affinities label the grid's column headers.
    assert reader.main(db, table="people")["types"] == {"name": "TEXT", "age": "INTEGER"}


def test_editable_table_has_no_readonly_message(db):
    out = reader.main(db, table="people")
    assert out["readonly_message"] == "" and out["readonly_tooltip"] == ""


# ------------------------------------------------------------- sort / filter

def test_sort_desc_keeps_rowids(db):
    # Sorting by age desc puts p4 (age 4) first; its rowid is still 5, so edits
    # key correctly while the grid is sorted.
    out = reader.main(db, table="people", sort={"column": "age", "dir": "desc"})
    assert out["rows"][0] == {"name": "p4", "age": 4}
    assert out["ids"][0] == 5


def test_filter_gte_narrows_count(db):
    out = reader.main(db, table="people", filters=[{"column": "age", "op": ">=", "value": "2"}])
    assert out["total_rows"] == 3
    assert out["ids"] == [3, 4, 5]


def test_filter_contains_matches_substring(db):
    out = reader.main(db, table="people", filters=[{"column": "name", "op": "contains", "value": "3"}])
    assert out["ids"] == [4]
    assert out["rows"][0]["name"] == "p3"


def test_unknown_filter_column_is_ignored(db):
    out = reader.main(db, table="people", filters=[{"column": "nope", "op": "=", "value": "x"}])
    assert out["total_rows"] == 5


# ------------------------------------------------------------------ writer

def test_edit_delete_insert(db):
    writer.main(db, table="people",
                edits=[{"row": 1, "column": "name", "value": "EDITED"}],
                deletes=[2, 3],
                inserts=[{"name": "new", "age": 99}])
    rows = [(r[1], r[2]) for r in _all(db)]
    assert ("EDITED", 0) in rows
    assert ("p1", 1) not in rows and ("p2", 2) not in rows   # deleted rowids 2,3
    assert ("new", 99) in rows
    assert len(rows) == 5 - 2 + 1


def test_string_value_gets_integer_affinity(db):
    writer.main(db, table="people", edits=[{"row": 1, "column": "age", "value": "42"}])
    sc = sqlite3.connect(db)
    val = sc.execute("SELECT age FROM people WHERE rowid = 1").fetchone()[0]
    sc.close()
    assert val == 42 and isinstance(val, int)       # affinity coerced "42" -> 42


def test_unknown_column_rejected_and_rolls_back(db):
    before = _all(db)
    with pytest.raises(ValueError):
        writer.main(db, table="people",
                    edits=[{"row": 1, "column": "name", "value": "ok"},
                           {"row": 2, "column": "nope", "value": "x"}])
    assert _all(db) == before                        # whole batch rolled back


def test_writing_a_view_is_rejected(db):
    with pytest.raises(ValueError):
        writer.main(db, table="adults", edits=[{"row": 1, "column": "age", "value": "5"}])


def test_null_value(db):
    writer.main(db, table="people", edits=[{"row": 1, "column": "name", "value": None}])
    sc = sqlite3.connect(db)
    val = sc.execute("SELECT name FROM people WHERE rowid = 1").fetchone()[0]
    sc.close()
    assert val is None
