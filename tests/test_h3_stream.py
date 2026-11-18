import os
import sys

import duckdb
import pytest

import fused_render
from _range_bucket import bucket  # noqa: F401
from test_duckdb_stream import needs_httpfs

H3_DIR = os.path.join(os.path.dirname(fused_render.__file__), "templates", "h3")
if H3_DIR not in sys.path:
    sys.path.insert(0, H3_DIR)

import condition  # noqa: E402
import h3_reader  # noqa: E402

H3_BASE = 622236750694711295


@pytest.fixture
def put(tmp_path, bucket):  # noqa: F811
    def put(name, sql):
        path = tmp_path / name
        duckdb.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 50000)")
        bucket.objects["/" + name] = path.read_bytes()
        return str(path), bucket.url(name)
    return put


@needs_httpfs
@pytest.mark.parametrize("color_by", ["", "b"])
def test_streamed_read_matches_local_and_reads_ranges(put, bucket, color_by):  # noqa: F811
    local, url = put("h3.parquet", f"SELECT ({H3_BASE} + i)::UBIGINT AS hex, (i * 0.5)::DOUBLE AS a, "
                                   f"i % 7 AS b, repeat('x', 40) AS pad FROM range(200000) t(i)")
    before = len(bucket.log)
    streamed = h3_reader.main(url, max_cells=1000, color_by=color_by)
    log = bucket.log[before:]
    expected = h3_reader.main(local, max_cells=1000)
    col = color_by or "a"
    assert streamed["attrs"] == {col: expected.pop("attrs")[col]}
    assert {k: v for k, v in streamed.items() if k != "attrs"} == expected
    gets = [rng for _, rng, _ in log if rng != "HEAD"]
    assert gets and all(gets)
    assert bucket.sent["/h3.parquet"] < len(bucket.objects["/h3.parquet"]) / 2


@needs_httpfs
def test_gate_takes_a_url(put, bucket):  # noqa: F811
    _, h3 = put("h3.parquet", f"SELECT printf('%x', {H3_BASE} + range) AS cell_id FROM range(1000)")
    _, plain = put("plain.parquet", "SELECT range AS a, 'v' || range AS b FROM range(1000)")
    assert condition.main(h3) is True
    assert condition.main(plain) is False
    assert condition.main(bucket.url("missing.parquet")) is False
