"""Gate for the H3 template (SPEC CT-12).

`main(path)` returns True only when the parquet actually carries an H3
cell-index column, so the template stops showing for every parquet
unconditionally. The detection mirrors `h3_reader.py` — a name match against
the known H3 column names, else a uint64/hex-string bit-pattern sniff — so the
gate agrees with what the reader would pick once opened.

Remote mounts make I/O the whole cost here, so the gate is footer-first:

1. `parquet_metadata()` reads only the footer — column names, physical types,
   and per-row-group min/max stats. A column whose every row group has stats
   and whose min AND max both carry the H3 bit pattern is decided True with
   zero data pages read; stats present but non-H3 decide it False the same
   way. Writers almost always emit stats, so the common case never touches
   row data.
2. Only columns with a stats gap fall back to sampling, and the sample
   projects just those columns (`SELECT "col", … LIMIT 128`) instead of a
   `SELECT *` — projection pushdown means one column's pages from the first
   row group, not a full row group of every column.

Only INT64 and BYTE_ARRAY columns can hold an H3 index (uint64 / hex string),
so everything else — and nested columns, which the value sniff could never
match anyway — is skipped outright.

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


def main(path: str) -> bool:
    import os

    path = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isfile(path):
        return False

    try:
        import duckdb
        con = duckdb.connect()
        for setting, env_name in (
            ("extension_directory", "FUSED_RENDER_DUCKDB_EXTENSION_DIR"),
            ("temp_directory", "FUSED_RENDER_DUCKDB_TEMP_DIR"),
        ):
            value = os.environ.get(env_name)
            if value:
                os.makedirs(value, exist_ok=True)
                con.execute(f"SET {setting} = ?", [value])
        src = path.replace("'", "''")
        # Footer-only read: names, physical types, per-row-group min/max.
        meta = con.execute(
            "SELECT path_in_schema, type, stats_min_value, stats_max_value"
            f" FROM parquet_metadata('{src}')"
        ).fetchall()
    except Exception:  # noqa: BLE001 — unreadable/not-parquet: fail closed, quietly
        return False

    # Aggregate the per-row-group rows into per-column footer verdicts. Only
    # types that can hold an H3 index matter, and a nested column (dotted
    # path) can't be projected by bare name — the whole-row sniff could never
    # match its struct values either, so skipping it changes nothing.
    cols = {}  # name -> {"str": bool, "stats": [(min, max), ...], "gap": bool}
    for name, ptype, smin, smax in meta:
        if ptype not in ("INT64", "BYTE_ARRAY") or "." in name:
            continue
        c = cols.setdefault(
            name, {"str": ptype == "BYTE_ARRAY", "stats": [], "gap": False}
        )
        if smin is None or smax is None:
            c["gap"] = True  # this row group can't be judged from the footer
        else:
            c["stats"].append((smin, smax))

    def looks(c, v):
        return _looks_h3_str(v) if c["str"] else _looks_h3_int(v)

    # Known H3 names first, mirroring the reader's precedence.
    ordered = sorted(cols, key=lambda n: (n.lower() not in H3_NAMES, n))
    unresolved = []
    for name in ordered:
        c = cols[name]
        if c["stats"] and not c["gap"]:
            # Full footer coverage: min/max of every row group decide it.
            if all(looks(c, lo) and looks(c, hi) for lo, hi in c["stats"]):
                return True
            # Stats refuted — but the reader accepts a column at >=90% H3
            # values, which a single outlier (a sentinel 0, a stray junk id)
            # would hide from min/max. For a column NAMED like H3 that
            # mismatch is likely real data the reader would render, so pay
            # for a sample; unknown names stay footer-only (that's every
            # column of every plain parquet — the case this gate must keep
            # cheap on remote mounts).
            if name.lower() in H3_NAMES:
                unresolved.append(name)
        else:
            unresolved.append(name)

    if not unresolved:
        return False

    # A stats gap somewhere: sample just those columns, narrowly projected.
    try:
        sel = ", ".join('"%s"' % n.replace('"', '""') for n in unresolved)
        sample = con.execute(f"SELECT {sel} FROM '{src}' LIMIT 128").fetchall()
    except Exception:  # noqa: BLE001 — fail closed, quietly
        return False

    def col_ok(i):
        vals = [r[i] for r in sample if r[i] is not None][:50]
        if not vals:
            return False
        good = sum(1 for v in vals
                   if (_looks_h3_str(v) if isinstance(v, str) else _looks_h3_int(v)))
        return good >= max(1, int(len(vals) * 0.9))

    return any(col_ok(i) for i in range(len(unresolved)))
