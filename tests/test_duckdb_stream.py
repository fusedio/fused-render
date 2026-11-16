"""duckdb parquet streaming (D1486): reader.py on `_file=s3://…`.

When the explorer streams a bucket parquet, the grid page signs the `s3://`
address (`fused.s3.sign`) and hands reader.py the HTTPS URL, which DuckDB reads
with its httpfs extension. Run here against the local range bucket (tests/_range_bucket.py,
plain http on loopback, which httpfs reads like any URL) and compared, mode by
mode, with the same file read locally.

httpfs is not in the duckdb wheel; DuckDB installs it from its extension
repository. Where that cannot be reached (an offline or proxied runner) the
tests that need it are skipped rather than hung.
"""
import os
import subprocess
import sys

import duckdb
import pytest

import fused_render
from _range_bucket import bucket  # noqa: F401 - the fixture

DUCKDB_DIR = os.path.join(os.path.dirname(fused_render.__file__), "templates", "duckdb")
if DUCKDB_DIR not in sys.path:
    sys.path.insert(0, DUCKDB_DIR)

import reader  # noqa: E402 - path seeded above, as runPython does



def _httpfs_available() -> bool:
    try:
        duckdb.connect().execute("LOAD httpfs")
        return True
    except duckdb.Error:
        pass
    try:  # INSTALL has no timeout of its own; bound it here
        subprocess.run([sys.executable, "-c", "import duckdb; duckdb.execute('INSTALL httpfs')"],
                       timeout=90, check=True, capture_output=True)
        duckdb.connect().execute("LOAD httpfs")
        return True
    except (subprocess.SubprocessError, OSError, duckdb.Error):
        return False


needs_httpfs = pytest.mark.skipif(not _httpfs_available(),
                                  reason="DuckDB's httpfs extension cannot be installed here")


@pytest.fixture
def table(tmp_path, bucket):  # noqa: F811
    """A 200k-row, 20-row-group parquet, on disk and in the bucket (the URL
    stands in for the one fused.s3.sign returns)."""
    path = tmp_path / "t.parquet"
    duckdb.execute(
        f"COPY (SELECT i AS id, (i * 7919) % 1000 AS score, 'name-' || (i % 97) AS name, "
        f"repeat('x', 40) AS pad FROM range(200000) t(i)) "
        f"TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 10000)")
    data = path.read_bytes()
    bucket.objects["/data/t.parquet"] = data
    return str(path), data, bucket, bucket.url("data/t.parquet")


def _same(remote, local):
    for key in ("columns", "types", "rows", "ids", "total_rows", "positions"):
        assert remote.get(key) == local.get(key), key


@needs_httpfs
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
    local, _, _, url = table
    _same(reader.main(url, **kwargs), reader.main(local, **kwargs))


@needs_httpfs
def test_streamed_files_are_read_only(table):
    out = reader.main(table[3], mode="schema")
    assert out["editable"] is False and out["remote"] is True
    assert out["readonly_message"] == "S3"


@needs_httpfs
def test_a_page_reads_ranges_not_the_object(table):
    _, _, b, url = table
    before = len(b.log)
    reader.main(url, mode="page", offset=120_000, limit=100)
    ranged = [rng for _, rng, _ in b.log[before:] if rng]
    assert ranged, "httpfs read the object without Range requests"


@needs_httpfs
def test_a_scan_over_the_budget_is_refused(table, monkeypatch):
    url = table[3]
    monkeypatch.setattr(reader, "STREAM_SCAN_BUDGET", 10_000)
    with pytest.raises(reader.TooLargeToStream, match="Too large to stream.*Download"):
        reader.main(url, mode="positions", sort={"column": "score", "dir": "asc"})
    with pytest.raises(reader.TooLargeToStream):
        reader.main(url, mode="count", filters=[{"column": "id", "op": "<", "value": "5"}])
    # an unsorted, unfiltered page and the footer-only modes still work
    assert len(reader.main(url, mode="page", limit=10)["rows"]) == 10
    assert reader.main(url, mode="count")["total_rows"] == 200_000


def test_only_parquet_streams():
    with pytest.raises(ValueError, match="Only parquet files stream"):
        reader.main("https://fused-asset.s3.us-west-2.amazonaws.com/t.csv", mode="page")


def test_a_bare_s3_address_points_at_signing():
    with pytest.raises(ValueError, match="fused.s3.sign"):
        reader.main("s3://fused-asset/data/t.parquet", mode="page")


def test_relation_for_keeps_a_streamed_url_and_writer_never_signs():
    """relation_for passes a streamed URL through untouched instead of making
    it absolute; writer.py (its own `_relation_for`, reached only for an
    editable grid, which a streamed one never is) knows nothing of S3."""
    url = "https://fused-asset.s3.us-west-2.amazonaws.com/k.parquet"
    assert reader.relation_for(url) == f"read_parquet('{url}')"
    assert reader._is_streamed(url) and not reader._is_streamed("/tmp/t.parquet")
    with open(os.path.join(DUCKDB_DIR, "writer.py"), encoding="utf-8") as f:
        src = f.read()
    assert "fused_s3" not in src and "httpfs" not in src
    with open(os.path.join(DUCKDB_DIR, "reader.py"), encoding="utf-8") as f:
        assert "fused_s3" not in f.read()  # the page signs, not the reader
