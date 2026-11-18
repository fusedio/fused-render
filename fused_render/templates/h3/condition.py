"""Gate for the H3 template (SPEC CT-12), and the H3 column detection
`h3_reader.py` imports, so the gate and the reader always pick the same column.

`main(path)` takes a local path or a streamed S3 object's signed https URL
(read with DuckDB's httpfs) and returns True only when the parquet carries an
H3 cell-index column. Detection is footer-first, since I/O is the whole cost:

1. `parquet_metadata()` reads only the footer. Only INT64 (uint64) and
   BYTE_ARRAY (hex string) top-level columns can hold an H3 index. A column
   whose every row group has min/max stats that both carry the H3 bit pattern
   is decided with no data pages read; a column refuted by its stats is
   dropped, unless it is NAMED like H3 (a sentinel 0 can hide real H3 data
   from min/max).
2. Columns with a stats gap, and refuted H3-named ones, are sampled with a
   projection of just those columns, accepted at >=90% H3 values.

Self-contained: the server loads this file standalone, so it cannot import a
sibling. Fails closed: any read error means "cannot prove it's H3".
"""

H3_NAMES = ("hex", "h3", "h3_index", "h3index", "h3_cell", "cell", "cell_id",
            "hex_id", "h3_id", "index")


def _looks_h3_int(v):
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


def is_url(src):
    return src.startswith(("https://", "http://"))


def connect(src):
    import duckdb

    con = duckdb.connect()
    if is_url(src):
        try:
            con.execute("LOAD httpfs")
        except duckdb.Error:
            con.execute("INSTALL httpfs")
            con.execute("LOAD httpfs")
    return con


def h3_column(con, src):
    cols = {}
    for name, ptype, lo, hi in con.execute(
            "SELECT path_in_schema, type, stats_min_value, stats_max_value"
            " FROM parquet_metadata(?)", [src]).fetchall():
        if ptype not in ("INT64", "BYTE_ARRAY") or "." in name:
            continue
        looks = _looks_h3_str if ptype == "BYTE_ARRAY" else _looks_h3_int
        seen = cols.get(name, True)
        cols[name] = None if None in (lo, hi, seen) else seen and looks(lo) and looks(hi)

    def rank(n):
        return H3_NAMES.index(n.lower()) if n.lower() in H3_NAMES else len(H3_NAMES)

    found, pending = None, []
    for name in sorted(cols, key=rank):
        if cols[name]:
            found = name
            break
        if cols[name] is None or rank(name) < len(H3_NAMES):
            pending.append(name)
    if not pending:
        return found

    sel = ", ".join('"%s"' % n.replace('"', '""') for n in pending)
    sample = con.execute(f"SELECT {sel} FROM read_parquet(?) LIMIT 128", [src]).fetchall()
    for i, name in enumerate(pending):
        vals = [r[i] for r in sample if r[i] is not None][:50]
        good = sum(_looks_h3_str(v) if isinstance(v, str) else _looks_h3_int(v) for v in vals)
        if vals and good >= max(1, int(len(vals) * 0.9)):
            return name
    return found


def main(path: str) -> bool:
    import os

    try:
        if not is_url(path):
            path = os.path.abspath(os.path.expanduser(path))
        return h3_column(connect(path), path) is not None
    except Exception:  # noqa: BLE001
        return False
