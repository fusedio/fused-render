"""Tests for the layout template's reader.py — parquet metadata + physical
byte layout (row-group / column-chunk level), built on pyarrow.

Skipped when pyarrow isn't installed.
"""
import importlib.util
import os

import pytest

pytest.importorskip("pyarrow")
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402


def _load(name):
    path = os.path.join(os.path.dirname(__file__), "..", "fused_render",
                        "templates", "layout", name)
    spec = importlib.util.spec_from_file_location(f"layout_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reader = _load("reader.py")


@pytest.fixture
def pq_file(tmp_path):
    """A 4-row, 2-column, 2-row-group parquet with typed columns + stats."""
    t = pa.table({
        "id": pa.array([1, 2, 3, 4], pa.int32()),
        "name": pa.array(["a", "bb", "ccc", "d"]),
    })
    p = tmp_path / "sample.parquet"
    pq.write_table(t, str(p), row_group_size=2, compression="snappy")
    return str(p)


# ---------------------------------------------------------------- file summary

def test_file_summary(pq_file):
    out = reader.main(pq_file)
    f = out["file"]
    assert f["num_rows"] == 4
    assert f["num_row_groups"] == 2
    assert f["num_columns"] == 2
    assert f["size"] == os.path.getsize(pq_file)
    assert f["serialized_size"] > 0
    assert "snappy" in [c.lower() for c in f["compression"]]
    assert f["created_by"]  # pyarrow stamps a creator


# ---------------------------------------------------------------- schema

def test_schema_columns(pq_file):
    schema = reader.main(pq_file)["schema"]
    by_name = {c["name"]: c for c in schema}
    assert by_name["id"]["physical_type"] == "INT32"
    assert by_name["name"]["physical_type"] == "BYTE_ARRAY"
    # the string column carries a UTF8/String logical or converted type
    n = by_name["name"]
    assert "STRING" in str(n["logical_type"]).upper() or n["converted_type"] == "UTF8"


# ---------------------------------------------------------------- row groups

def test_row_groups_and_chunks(pq_file):
    rgs = reader.main(pq_file)["row_groups"]
    assert len(rgs) == 2
    rg0 = rgs[0]
    assert rg0["index"] == 0
    assert rg0["num_rows"] == 2
    assert len(rg0["columns"]) == 2

    # Every column chunk's byte range is self-consistent: end = start + bytes.
    for col in rg0["columns"]:
        assert col["end"] == col["start"] + col["compressed_size"]
        assert col["compressed_size"] > 0

    # The very first chunk begins right after the 4-byte PAR1 header.
    first = rg0["columns"][0]
    assert first["start"] == 4


def test_column_statistics(pq_file):
    rgs = reader.main(pq_file)["row_groups"]
    id_col = next(c for c in rgs[0]["columns"] if c["path"] == "id")
    assert id_col["stats"]["min"] == 1
    assert id_col["stats"]["max"] == 2
    assert id_col["stats"]["nulls"] == 0


# ---------------------------------------------------------------- layout boxes

def test_layout_header_and_footer(pq_file):
    out = reader.main(pq_file)
    layout = out["layout"]
    header = layout[0]
    assert header["kind"] == "header"
    assert header["start"] == 0 and header["bytes"] == 4 and header["end"] == 4

    footer = layout[-1]
    assert footer["kind"] == "footer"
    # the footer's magic ends exactly at end-of-file
    assert footer["end"] == out["file"]["size"]
    assert footer["start"] < footer["end"]


def test_layout_row_group_regions(pq_file):
    layout = reader.main(pq_file)["layout"]
    rgs = [r for r in layout if r["kind"] == "row_group"]
    assert len(rgs) == 2
    assert rgs[0]["index"] == 0
    # each row-group region lists its column chunks with byte ranges
    for col in rgs[0]["columns"]:
        assert col["end"] == col["start"] + col["bytes"]


# ---------------------------------------------------------------- json safety

def test_output_is_json_serializable(pq_file):
    import json
    json.dumps(reader.main(pq_file))  # must not raise
