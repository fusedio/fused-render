"""Gate for the H3 template (SPEC CT-12).

`method(path)` returns True only when the parquet actually carries an H3
cell-index column, so the template stops showing for every parquet
unconditionally. The detection mirrors `h3_reader.py` — a name match against
the known H3 column names, else a uint64/hex-string bit-pattern sniff — so the
gate agrees with what the reader would pick once opened.

Self-contained on purpose: the condition module is loaded standalone (not part
of a package), so it cannot rely on importing `h3_reader`. Fails closed — any
read error means "cannot prove it's H3" and the template is dropped quietly.
"""

H3_NAMES = ("hex", "h3", "h3_index", "h3index", "h3_cell", "cell", "cell_id",
            "hex_id", "h3_id", "index")


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


def method(path: str) -> bool:
    import os

    path = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isfile(path):
        return False

    try:
        import duckdb
        con = duckdb.connect()
        src = path.replace("'", "''")
        schema = con.execute(f"DESCRIBE SELECT * FROM '{src}'").fetchall()
        names = [s[0] for s in schema]
        sample = con.execute(f"SELECT * FROM '{src}' LIMIT 200").fetchall()
    except Exception:  # noqa: BLE001 — unreadable/not-parquet: fail closed, quietly
        return False

    def col_ok(i):
        vals = [r[i] for r in sample if r[i] is not None][:50]
        if not vals:
            return False
        good = sum(1 for v in vals
                   if (_looks_h3_str(v) if isinstance(v, str) else _looks_h3_int(v)))
        return good >= max(1, int(len(vals) * 0.9))

    lower = [n.lower() for n in names]
    # name match first (cheap, matches reader precedence), still validated by values
    for n in H3_NAMES:
        if n in lower and col_ok(lower.index(n)):
            return True
    # value sniff any remaining column
    return any(col_ok(i) for i in range(len(names)))
