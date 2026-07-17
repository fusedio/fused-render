"""H3 hexagon preview reader for fused-render.

Reads a parquet file with the app's bundled duckdb, finds the H3 cell-index
column (by name, else by validating the uint64 bit pattern), and returns
cell ids as hex strings plus the attribute columns. Boundary polygons are
computed client-side with h3-js, so no H3 python dependency is needed.
"""

H3_NAMES = (
    "hex",
    "h3",
    "h3_index",
    "h3index",
    "h3_cell",
    "cell",
    "cell_id",
    "hex_id",
    "h3_id",
    "index",
)
MAX_CELLS = 80_000


def _looks_h3_int(v):
    # H3 cell index: mode field (bits 59-62) == 1, high bit 0
    try:
        v = int(v)
    except (TypeError, ValueError):
        return False
    return v > 0 and (v >> 63) == 0 and ((v >> 59) & 0xF) == 1


def _looks_h3_str(v):
    s = str(v).strip().lower()
    if not (8 <= len(s) <= 16):
        return False
    try:
        return _looks_h3_int(int(s, 16))
    except ValueError:
        return False


def main(file: str = "", h3_col: str = "", max_cells: int = MAX_CELLS):
    import os

    max_cells = int(max_cells)
    if not file:
        return {"error": "no file selected"}
    file = os.path.abspath(os.path.expanduser(file))
    if not os.path.isfile(file):
        return {"error": f"not a file: {file}"}

    import duckdb

    con = duckdb.connect()
    q = lambda sql: con.execute(sql).fetchall()
    src = file.replace("'", "''")
    try:
        schema = q(f"DESCRIBE SELECT * FROM '{src}'")
        total = q(f"SELECT count(*) FROM '{src}'")[0][0]
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not read parquet: {type(e).__name__}: {e}"}
    cols = [(s[0], s[1]) for s in schema]

    # ---- find the H3 column: explicit > name match > value sniff ----
    sample = q(f"SELECT * FROM '{src}' LIMIT 200")
    names = [c[0] for c in cols]

    def col_ok(i):
        vals = [r[i] for r in sample if r[i] is not None][:50]
        if not vals:
            return False
        good = sum(
            1 for v in vals if (_looks_h3_int(v) if not isinstance(v, str) else _looks_h3_str(v))
        )
        return good >= max(1, int(len(vals) * 0.9))

    cand = None
    if h3_col:
        if h3_col not in names:
            return {"error": f"column not found: {h3_col}", "columns": names}
        cand = h3_col
    else:
        for n in H3_NAMES:
            if n in [x.lower() for x in names]:
                i = [x.lower() for x in names].index(n)
                if col_ok(i):
                    cand = names[i]
                    break
        if cand is None:
            for i, n in enumerate(names):
                if col_ok(i):
                    cand = n
                    break
    if cand is None:
        return {
            "error": "no H3 index column detected — pick one manually",
            "columns": names,
            "no_h3": True,
        }

    ci = names.index(cand)
    is_str = isinstance(next((r[ci] for r in sample if r[ci] is not None), None), str)

    # ---- pull cells + attributes ----
    others = [n for n in names if n != cand]
    sel_h3 = f'lower(trim("{cand}"))' if is_str else f"format('{{:x}}', \"{cand}\"::UBIGINT)"
    sel = ", ".join([f"{sel_h3} AS __h3"] + [f'"{n}"' for n in others])
    rows = q(f"SELECT {sel} FROM '{src}' WHERE \"{cand}\" IS NOT NULL LIMIT {max_cells}")

    cells = [r[0] for r in rows]
    attrs = {}
    for j, n in enumerate(others):
        vals = []
        for r in rows:
            v = r[j + 1]
            if v is None:
                vals.append(None)
            elif isinstance(v, (int, float, bool)):
                f = float(v)
                vals.append(
                    None if f != f else (int(v) if isinstance(v, int) or isinstance(v, bool) else f)
                )
            else:
                vals.append(str(v)[:120])
        attrs[n] = vals

    return {
        "file": file,
        "file_size": os.path.getsize(file),
        "count": int(total),
        "shown": len(cells),
        "truncated": bool(total > len(cells)),
        "h3_col": cand,
        "columns": names,
        "schema": [{"name": c[0], "dtype": c[1]} for c in cols],
        "cells": cells,
        "attrs": attrs,
    }


try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass
