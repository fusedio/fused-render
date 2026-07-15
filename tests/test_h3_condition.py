"""The H3 template's condition.py gate (SPEC CT-12).

The gate is footer-first for remote mounts: parquet column stats decide the
common case with no data pages read, and only stats gaps fall back to a
narrowly-projected sample. These tests pin the detection behavior over real
parquet files written by duckdb (which emits footer stats).
"""

import importlib.util
import os

import pytest

pytest.importorskip("duckdb")
import duckdb  # noqa: E402

CONDITION = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "h3", "condition.py"
)

# First cell of the res-0 H3 grid — any offset within a small range keeps the
# mode bits (59-62 == 1) intact, so `base + range(n)` yields valid-looking cells.
H3_BASE = 622236750694711295


@pytest.fixture(scope="module")
def main():
    spec = importlib.util.spec_from_file_location("h3_condition", CONDITION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


@pytest.fixture()
def parquet(tmp_path):
    con = duckdb.connect()

    def make(sql, name="t.parquet"):
        p = str(tmp_path / name)
        con.execute(f"COPY ({sql}) TO '{p}' (FORMAT PARQUET)")
        return p

    return make


def test_h3_uint_column_named_hex(main, parquet):
    p = parquet(f"SELECT ({H3_BASE} + range)::UBIGINT AS hex, range AS x FROM range(1000)")
    assert main(p) is True


def test_h3_hex_string_column(main, parquet):
    p = parquet(f"SELECT printf('%x', {H3_BASE} + range) AS cell_id FROM range(1000)")
    assert main(p) is True


def test_h3_values_under_unknown_name_sniffed(main, parquet):
    # Name not in H3_NAMES: the value sniff (footer stats) still finds it.
    p = parquet(f"SELECT ({H3_BASE} + range)::UBIGINT AS weird_col FROM range(1000)")
    assert main(p) is True


def test_plain_parquet_rejected(main, parquet):
    p = parquet("SELECT range AS a, 'v' || range AS b FROM range(1000)")
    assert main(p) is False


def test_h3_named_column_with_non_h3_values_rejected(main, parquet):
    # A column NAMED like H3 but holding small ints must not pass: the gate
    # validates values (footer min/max then a sample), never trusts the name.
    p = parquet("SELECT range AS h3_index FROM range(1000)")
    assert main(p) is False


def test_h3_named_column_with_outlier_still_accepted(main, parquet):
    # A sentinel 0 hides the H3 pattern from min/max, but the reader accepts
    # >=90% H3 values — for an H3-NAMED column the gate must sample past the
    # refuting stats and agree with the reader.
    p = parquet(
        f"SELECT CASE WHEN range = 3 THEN 0 ELSE {H3_BASE} + range END::UBIGINT AS hex"
        " FROM range(1000)"
    )
    assert main(p) is True


def test_unknown_name_with_outlier_stays_footer_only(main, parquet):
    # Same data under an unknown name is refuted by stats alone — no sample
    # is paid for arbitrary columns (that's every plain parquet's columns).
    p = parquet(
        f"SELECT CASE WHEN range = 3 THEN 0 ELSE {H3_BASE} + range END::UBIGINT AS weird_col"
        " FROM range(1000)"
    )
    assert main(p) is False


def test_empty_parquet_rejected(main, parquet):
    p = parquet("SELECT 1::BIGINT AS hex WHERE false")
    assert main(p) is False


def test_missing_file_rejected(main, tmp_path):
    assert main(str(tmp_path / "nope.parquet")) is False


def test_non_parquet_bytes_fail_closed(main, tmp_path):
    p = tmp_path / "bad.parquet"
    p.write_text("not parquet at all")
    assert main(str(p)) is False
