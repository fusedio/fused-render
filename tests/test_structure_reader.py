"""Tests for the structure template's reader.py — parquet metadata + physical
byte layout (row-group / column-chunk level), built on pyarrow.

Two calls under test: the compact overview main(file) and the per-row-group
detail main(file, row_group=i).

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
                        "templates", "structure", name)
    spec = importlib.util.spec_from_file_location(f"structure_{name}", path)
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


@pytest.fixture
def blob_file(tmp_path):
    """One row group whose string column's min/max statistics are far longer
    than the 64-char cap the reader applies."""
    long_a = "a" * 500
    long_b = "b" * 500
    t = pa.table({"blob": pa.array([long_a, long_b])})
    p = tmp_path / "blob.parquet"
    pq.write_table(t, str(p), compression="snappy")
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


# ------------------------------------------------- row groups (overview shape)

def test_row_groups_are_compact_arrays(pq_file):
    out = reader.main(pq_file)
    rgs = out["row_groups"]
    assert len(rgs) == 2
    rg0 = rgs[0]
    assert rg0["index"] == 0
    assert rg0["num_rows"] == 2
    assert rg0["total_byte_size"] > 0

    # Numeric arrays instead of per-chunk objects: one entry per leaf column.
    assert "columns" not in rg0
    n = out["file"]["num_columns"]
    for rg in rgs:
        assert len(rg["chunk_bytes"]) == n
        assert len(rg["chunk_starts"]) == n
        assert all(isinstance(v, int) for v in rg["chunk_bytes"])
        assert all(isinstance(v, int) for v in rg["chunk_starts"])
        assert all(v > 0 for v in rg["chunk_bytes"])
        assert rg["compressed_size"] == sum(rg["chunk_bytes"])

    # The very first chunk begins right after the 4-byte PAR1 header, and
    # chunks are laid out in ascending on-disk order within a row group.
    assert rg0["chunk_starts"][0] == 4
    assert rg0["chunk_starts"] == sorted(rg0["chunk_starts"])


def test_overview_arrays_match_detail(pq_file):
    out = reader.main(pq_file)
    # Arrays are in schema leaf order, so schema[j] describes column j.
    paths = [c["path"] for c in out["schema"]]
    for rg in out["row_groups"]:
        detail = reader.main(pq_file, row_group=rg["index"])
        assert [c["path"] for c in detail["columns"]] == paths
        assert rg["chunk_bytes"] == [c["compressed_size"] for c in detail["columns"]]
        assert rg["chunk_starts"] == [c["start"] for c in detail["columns"]]
        assert rg["compressed_size"] == detail["compressed_size"]


# ------------------------------------------------------- row group detail call

def test_row_group_detail(pq_file):
    out = reader.main(pq_file, row_group=1)
    assert out["index"] == 1
    assert out["num_rows"] == 2
    assert len(out["columns"]) == 2
    # The detail is the only place per-chunk objects live, and it carries the
    # full physical description of each chunk.
    for col in out["columns"]:
        assert col["end"] == col["start"] + col["compressed_size"]
        assert col["compressed_size"] > 0
        assert col["uncompressed_size"] > 0
        assert col["encodings"]
        assert col["compression"]
        assert col["num_values"] == 2
    # A detail response is just that row group — no file/schema/layout keys.
    assert "layout" not in out and "schema" not in out


def test_row_group_detail_accepts_numeric_string(pq_file):
    # Params that round-trip through the URL arrive as strings.
    assert reader.main(pq_file, row_group="0")["index"] == 0


@pytest.mark.parametrize("bad", [2, 99, -1])
def test_row_group_out_of_range(pq_file, bad):
    with pytest.raises(ValueError, match="out of range"):
        reader.main(pq_file, row_group=bad)


def test_row_group_not_an_integer(pq_file):
    with pytest.raises(ValueError, match="integer"):
        reader.main(pq_file, row_group="nope")


def test_column_statistics(pq_file):
    cols = reader.main(pq_file, row_group=0)["columns"]
    id_col = next(c for c in cols if c["path"] == "id")
    assert id_col["stats"]["min"] == 1
    assert id_col["stats"]["max"] == 2
    assert id_col["stats"]["nulls"] == 0


def test_statistics_strings_are_truncated(blob_file):
    cols = reader.main(blob_file, row_group=0)["columns"]
    stats = cols[0]["stats"]
    for key in ("min", "max"):
        assert len(stats[key]) == 65  # 64 chars + the truncation marker
        assert stats[key].endswith("…")
    assert stats["min"].startswith("a")
    assert stats["max"].startswith("b")


def test_short_statistics_are_left_alone(pq_file):
    cols = reader.main(pq_file, row_group=0)["columns"]
    name_col = next(c for c in cols if c["path"] == "name")
    assert name_col["stats"]["min"] == "a"
    assert name_col["stats"]["max"] == "bb"


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
    out = reader.main(pq_file)
    regions = [r for r in out["layout"] if r["kind"] == "row_group"]
    assert len(regions) == 2
    for region, rg in zip(regions, out["row_groups"]):
        assert region["index"] == rg["index"]
        assert region["num_rows"] == rg["num_rows"]
        assert region["bytes"] == rg["compressed_size"]
        # per-column offsets are not duplicated here — the template derives
        # column boxes from the row group's arrays plus schema[j].path
        assert "columns" not in region


# ---------------------------------------------------------------- json safety

def test_output_is_json_serializable(pq_file):
    import json
    json.dumps(reader.main(pq_file))  # must not raise
    json.dumps(reader.main(pq_file, row_group=0))  # must not raise


# ------------------------------------------------------------- param binding

def test_row_group_is_optional_through_bind_params(pq_file):
    """The overview call ships no row_group at all; bind_params must accept
    that and leave the default in place (fused.runPython binds by name)."""
    from fused_render._binding import bind_params

    assert bind_params(reader.main, {"file": pq_file}) == {"file": pq_file}
    bound = bind_params(reader.main, {"file": pq_file, "row_group": 1})
    assert bound["row_group"] == 1
    assert reader.main(**bound)["index"] == 1
