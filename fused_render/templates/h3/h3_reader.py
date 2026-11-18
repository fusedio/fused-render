"""H3 hexagon preview reader for fused-render.

Reads a parquet file (a local path, or a streamed S3 object's signed https URL
read with DuckDB's httpfs), finds the H3 cell-index column with condition.py's
footer-first detection, and returns cell ids as hex strings plus attribute
columns. Boundary polygons are computed client-side with h3-js.

DuckDB fetches whole column chunks of the row groups a LIMIT touches, so a
streamed object reads only the H3 column and the colour column. Streamed reads
share one DuckDB per process (the folder's warm worker): httpfs, the footer
and the chunks already fetched stay loaded, so a colour change moves only the
new column.
"""
import os
import threading

import duckdb

from condition import connect, h3_column, ident, is_url

MAX_CELLS = 80_000

_stream_db = None
_stream_lock = threading.Lock()


def _cursor(file):
    global _stream_db
    if not is_url(file):
        return duckdb.connect()
    with _stream_lock:
        if _stream_db is None:
            _stream_db = connect(file)
        return _stream_db.cursor()


def _cell(v):
    if isinstance(v, (int, float)):
        return int(v) if isinstance(v, int) else (None if v != v else v)
    return None if v is None else str(v)[:120]


def main(file: str = "", h3_col: str = "", max_cells: int = MAX_CELLS, color_by: str = ""):
    if not file:
        return {"error": "no file selected"}
    if not is_url(file):
        file = os.path.abspath(os.path.expanduser(file))
        if not os.path.isfile(file):
            return {"error": f"not a file: {file}"}
    with _cursor(file) as con:
        return _read(con, file, h3_col, color_by, int(max_cells))


def _read(con, file, h3_col, color_by, max_cells):
    q = lambda sql: con.execute(sql, [file]).fetchall()
    try:
        types = dict(r[:2] for r in q("DESCRIBE SELECT * FROM read_parquet(?)"))
        total = q("SELECT count(*) FROM read_parquet(?)")[0][0]
        size = q("SELECT size FROM read_blob(?)")[0][0]
        cand = h3_col or h3_column(con, file)
    except duckdb.Error as e:
        return {"error": f"could not read parquet: {type(e).__name__}: {e}"}
    names = list(types)
    if cand not in types:
        if h3_col:
            return {"error": f"column not found: {h3_col}", "columns": names}
        return {"error": "no H3 index column detected — pick one manually",
                "columns": names, "no_h3": True}

    numeric = [n for n, t in types.items()
               if n != cand and t.endswith(("INT", "INTEGER", "FLOAT", "DOUBLE", "BOOLEAN"))]
    others = [n for n in names if n != cand]
    if is_url(file):
        others = [color_by if color_by in numeric else numeric[0]] if numeric else []
    h3 = (f"format('{{:x}}', {ident(cand)}::UBIGINT)" if "INT" in types[cand]
          else f"lower(trim({ident(cand)}::VARCHAR))")
    rows = q(f"SELECT {', '.join([h3] + [ident(n) for n in others])} FROM read_parquet(?)"
             f" WHERE {ident(cand)} IS NOT NULL LIMIT {max_cells}")

    return {
        "file_size": size,
        "count": int(total),
        "shown": len(rows),
        "truncated": bool(total > len(rows)),
        "h3_col": cand,
        "columns": names,
        "numeric": numeric,
        "schema": [{"name": n, "dtype": t} for n, t in types.items()],
        "cells": [r[0] for r in rows],
        "attrs": {n: [_cell(r[j]) for r in rows] for j, n in enumerate(others, 1)},
    }


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
