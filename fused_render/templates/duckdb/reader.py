"""Reader backing duckdb/template.html — one tabular viewer/editor for every
flat file DuckDB can read: Parquet, CSV/TSV and JSON/JSONL.

DuckDB does the COUNT(*) and LIMIT/OFFSET paging *in-engine*, so paging a huge
CSV never loads the whole file into memory. Reads are non-mutating (the file
table-functions can't write); edits go through the sibling writer.py.

Row identity for editing is **file position** — the 0-based row index in the
file's natural scan order, which DuckDB reads deterministically. The reader
returns a parallel `ids` list (ids[i] is the absolute position of rows[i]) so
the grid can key edits/deletes by position without colliding with a real
column named, say, "id".

CSV/TSV are read as all-VARCHAR: delimited text has no real types, so keeping
every cell a string means editing round-trips exactly (a zip code "01234"
can't silently become the integer 1234 on rewrite). Parquet/JSON keep their
real types.

Returns {columns, rows, ids, total_rows, editable}. `editable` is false for
JSON — rewriting a flattened JSON grid would corrupt nested structure, so the
JSON grid is view-only. `tables`/`table` are absent (single-relation files);
the grid hides its relation selector when they're missing.
"""
import datetime
import decimal
import os

import duckdb

MAX_LIMIT = 1000

# Extensions the grid can safely edit (rewrite in place). JSON is read-only.
_EDITABLE_EXTS = {".parquet", ".csv", ".tsv"}


def _jsonify(value):
    """Coerce a DuckDB cell value into something json.dumps can encode.
    Recurses into lists/structs so a nested column is fully JSON-safe."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    return str(value)


def _quote_str(s: str) -> str:
    """Single-quote a SQL string literal (file path), doubling embedded quotes."""
    return "'" + s.replace("'", "''") + "'"


def relation_for(file: str) -> str:
    """The read-only table-function that reads `file` by extension. Shared with
    writer.py so read and write agree on how each format is parsed."""
    lit = _quote_str(os.path.abspath(file))
    ext = os.path.splitext(file)[1].lower()
    if ext == ".parquet":
        return f"read_parquet({lit})"
    if ext == ".tsv":
        return f"read_csv_auto({lit}, delim='\t', all_varchar=true)"
    if ext == ".csv":
        return f"read_csv_auto({lit}, all_varchar=true)"
    # .json / .jsonl / .ndjson
    return f"read_json_auto({lit})"


def main(file: str, offset: int = 0, limit: int = 100) -> dict:
    # Clamp so a hostile/negative limit can't turn LIMIT ? into an unbounded
    # fetch, and a negative offset can't error out mid-query.
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    ext = os.path.splitext(file)[1].lower()
    relation = relation_for(file)

    con = duckdb.connect(":memory:")
    try:
        total_rows = con.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0]
        cur = con.execute(f"SELECT * FROM {relation} LIMIT ? OFFSET ?", [limit, offset])
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = []
        for raw in cur.fetchall():
            rows.append({columns[j] if j < len(columns) else f"col{j}": _jsonify(v)
                         for j, v in enumerate(raw)})
        editable = ext in _EDITABLE_EXTS
        return {
            "columns": columns,
            "rows": rows,
            # Absolute file position of each returned row — the edit/delete key.
            "ids": list(range(offset, offset + len(rows))),
            "total_rows": total_rows,
            "editable": editable,
            "readonly_message": "" if editable else "JSON",
            "readonly_tooltip": "" if editable else (
                "Read-only. JSON is flattened into columns for viewing; writing "
                "that back would lose the original nested structure."),
        }
    finally:
        con.close()
