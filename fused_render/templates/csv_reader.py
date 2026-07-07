# /// script
# dependencies = ["pandas"]
# ///
"""Reader backing csv_template.html. Returns a JSON-safe page of rows.

Mirrors parquet_reader.py's contract and cell-stringifying approach, but reads
delimited text via pandas. The full file is read once to get an honest
total_rows and to slice the requested page; only the page is returned, so the
payload stays small even for large files (per ARCHITECTURE.md §7).
"""
import datetime
import decimal
import os

import fused
import pandas as pd


def _jsonify(value):
    """Coerce a pandas/numpy scalar into something json.dumps can encode."""
    # NaN/NaT/None → null. pd.isna handles float nan, pd.NaT, None uniformly;
    # it only accepts scalars here (cells), so no array-ambiguity risk.
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, decimal.Decimal):
        return str(value)
    # pandas Timestamp subclasses datetime, so this covers it too.
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    # numpy scalars (int64/float64/bool_) expose .item() → native Python.
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            return str(value)
    return value


@fused.udf
def main(file: str, offset: int = 0, limit: int = 100) -> dict:
    # .tsv is tab-delimited; for everything else default to comma (pandas'
    # own default). Explicit sep keeps the fast C parser — sniffing forces the
    # slower Python engine.
    sep = "\t" if os.path.splitext(file)[1].lower() == ".tsv" else ","
    df = pd.read_csv(file, sep=sep)
    total_rows = len(df)
    page = df.iloc[offset : offset + limit]
    columns = [str(c) for c in df.columns]
    rows = [{str(k): _jsonify(v) for k, v in rec.items()} for rec in page.to_dict("records")]
    return {"columns": columns, "rows": rows, "total_rows": total_rows}
