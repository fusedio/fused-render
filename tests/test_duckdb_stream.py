"""duckdb parquet streaming (D1486): reader.py on `_file=s3://…`.

The grid's reader gets an `s3://` address when the explorer streams a bucket
parquet, and reads it through `_s3_fs` — an fsspec filesystem over
`shared/remote_range.RemoteBuffer` registered on DuckDB — instead of a
downloaded copy. Run here against the local range bucket (tests/_range_bucket.py)
and compared, mode by mode, with the same file read locally: the streamed grid
must be the local grid, read-only, and a page must read a small part of the file.
"""
import os
import sys

import duckdb
import pytest

import fused_render
from _range_bucket import bucket  # noqa: F401 - the fixture

DUCKDB_DIR = os.path.join(os.path.dirname(fused_render.__file__), "templates", "duckdb")
if DUCKDB_DIR not in sys.path:
    sys.path.insert(0, DUCKDB_DIR)

import reader  # noqa: E402 - path seeded above, as runPython does
import _s3_fs  # noqa: E402

ADDR = "s3://fused-asset/data/t.parquet"


@pytest.fixture
def table(tmp_path, bucket):  # noqa: F811
    """A 200k-row, 20-row-group parquet, on disk and in the bucket."""
    path = tmp_path / "t.parquet"
    duckdb.execute(
        f"COPY (SELECT i AS id, (i * 7919) % 1000 AS score, 'name-' || (i % 97) AS name, "
        f"repeat('x', 40) AS pad FROM range(200000) t(i)) "
        f"TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 10000)")
    data = path.read_bytes()
    bucket.objects["/data/t.parquet"] = data
    return str(path), data, bucket


def _same(remote, local):
    for key in ("columns", "types", "rows", "ids", "total_rows", "positions"):
        assert remote.get(key) == local.get(key), key


@pytest.mark.parametrize("kwargs", [
    {"mode": "schema"},
    {"mode": "count"},
    {"mode": "full"},
    {"mode": "page", "offset": 150_000, "limit": 50},
    {"mode": "page", "columns": ["score", "name"]},
    {"mode": "positions", "sort": {"column": "score", "dir": "desc"}, "limit": 30},
    {"mode": "page", "sort": {"column": "name", "dir": "asc"}, "limit": 20},
    {"mode": "count", "filters": [{"column": "score", "op": ">", "value": "990"}]},
    {"mode": "page", "filters": [{"column": "name", "op": "contains", "value": "-9"}]},
    {"mode": "page", "positions": [5, 123_456, 199_999]},
])
def test_streamed_grid_matches_the_local_grid(table, kwargs):
    local, _, _ = table
    remote = reader.main(ADDR, **kwargs)
    _same(remote, reader.main(local, **kwargs))


def test_streamed_files_are_read_only(table):
    out = reader.main(ADDR, mode="schema")
    assert out["editable"] is False and out["remote"] is True
    assert out["readonly_message"] == "S3"


def test_a_page_reads_a_small_part_of_the_file(table, monkeypatch):
    _, data, b = table
    fs_seen = []
    real = _s3_fs.register

    def spy(con):
        fs = real(con)
        fs_seen.append(fs)
        return fs

    monkeypatch.setattr(_s3_fs, "register", spy)
    reader.main(ADDR, mode="page", offset=120_000, limit=100)
    fetched = fs_seen[0].stats()["bytes_fetched"]
    assert 0 < fetched < len(data) / 4, (fetched, len(data))


def test_a_scan_over_the_budget_is_refused(table, monkeypatch):
    monkeypatch.setattr(reader, "STREAM_SCAN_BUDGET", 10_000)
    with pytest.raises(reader.TooLargeToStream, match="Too large to stream.*Download"):
        reader.main(ADDR, mode="positions", sort={"column": "score", "dir": "asc"})
    with pytest.raises(reader.TooLargeToStream):
        reader.main(ADDR, mode="count", filters=[{"column": "id", "op": "<", "value": "5"}])
    # an unsorted, unfiltered page and the footer-only modes still work
    assert len(reader.main(ADDR, mode="page", limit=10)["rows"]) == 10
    assert reader.main(ADDR, mode="count")["total_rows"] == 200_000


def test_only_parquet_streams(bucket):  # noqa: F811
    with pytest.raises(ValueError, match="Only parquet files stream"):
        reader.main("s3://fused-asset/data/t.csv", mode="page")


def test_the_filesystem_is_read_only_and_never_lists(table):
    fs = _s3_fs.RemoteFS()
    p = _s3_fs.duckdb_path(ADDR)
    assert p == "fusedrange://fused-asset/data/t.parquet"
    assert fs.glob(p + "*") == ["fused-asset/data/t.parquet*"]  # a literal, not a pattern
    assert fs.info(p)["size"] == len(table[1])
    with pytest.raises(PermissionError):
        fs.open(p, "wb")
    assert _s3_fs.is_streamed(p) and not _s3_fs.is_streamed("/tmp/t.parquet")


def test_writer_never_sees_a_streamed_path():
    """relation_for (shared with writer.py) passes a streamed path through
    untouched instead of making it absolute, and writer.py needs no fsspec."""
    assert reader.relation_for("fusedrange://b/k.parquet") == \
        "read_parquet('fusedrange://b/k.parquet')"
    src = open(os.path.join(DUCKDB_DIR, "writer.py"), encoding="utf-8").read()
    assert "fsspec" not in src and "_s3_fs" not in src
