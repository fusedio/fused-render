"""Writer backing duckdb/template.html's Save — applies a batch of edits,
deletes and inserts to a Parquet/CSV/TSV file (optionally .gz/.zst compressed)
and rewrites it atomically, keeping the same format and compression codec.

Flat files can't be edited in place, so the whole file is rewritten: load it
into an in-memory DuckDB table (whose `rowid` pseudo-column is the 0-based file
position — the same id reader.py hands the grid), apply every change, then COPY
the result to a temp file in the same directory and os.replace it over the
original. The batch is all-or-nothing: any bad cast aborts before the COPY, so
a rejected save leaves the file byte-for-byte untouched.

Row identity is file position (`row`), matching reader.py's `ids`. Edits and
deletes key off the ORIGINAL positions (rowids are assigned at load and don't
shift as we mutate); inserts append.

A DuckDB **database** file (.duckdb/.ddb) is the exception: it isn't rewritten
but edited in place, ATTACHed read-write and mutated by `rowid` inside one
transaction (like the SQLite writer). `table` names which relation to write;
views are rejected.

Called by fused.runPython with structured params:
  table:   "<table name>" (database files only; ignored for flat files)
  edits:   [{"row": <int>, "column": <name>, "value": <str|None>}, ...]
  deletes: [<int>, ...]
  inserts: [{<column>: <value>, ...}, ...]
Returns {"total_rows": <int>} — the row count after the batch.
"""
import os

import duckdb

# Base COPY options per logical format. CSV/TSV are written with a header; the
# reader reads them all-VARCHAR, so values round-trip as the exact text typed.
_COPY_OPTS = {
    ".parquet": ["FORMAT parquet"],
    ".csv": ["FORMAT csv", "HEADER"],
    ".tsv": ["FORMAT csv", "HEADER", "DELIMITER '\t'"],
}

# A trailing .gz/.zst suffix means the source was compressed; the rewrite keeps
# it compressed with the matching codec so the file's format never changes.
_COMPRESSION_SUFFIXES = (".gz", ".zst")
_COMPRESSION = {".gz": "gzip", ".zst": "zstd"}


def _logical_ext(file: str) -> str:
    """Format extension seen past a trailing compression suffix (kept in step
    with reader.py): 'a.csv.gz' -> '.csv'."""
    base, low = file, file.lower()
    for comp in _COMPRESSION_SUFFIXES:
        if low.endswith(comp):
            base = base[: -len(comp)]
            break
    return os.path.splitext(base)[1].lower()


def _compression(file: str) -> "str | None":
    low = file.lower()
    for comp in _COMPRESSION_SUFFIXES:
        if low.endswith(comp):
            return _COMPRESSION[comp]
    return None


def _copy_clause(file: str) -> str:
    """The `(FORMAT …, COMPRESSION …)` COPY options for `file`, appending the
    compression codec when the name carries a .gz/.zst suffix."""
    opts = list(_COPY_OPTS[_logical_ext(file)])
    codec = _compression(file)
    if codec:
        opts.append(f"COMPRESSION {codec}")
    return "(" + ", ".join(opts) + ")"


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _relation_for(file: str) -> str:
    """Read-only table-function for `file`. Kept in step with reader.py's
    relation_for (in-process helpers are self-contained — no sibling imports),
    so the writer loads each format exactly as the viewer showed it: CSV/TSV
    all-VARCHAR for exact text round-trip, Parquet with its real types."""
    lit = _quote_str(os.path.abspath(file))
    ext = _logical_ext(file)
    if ext == ".parquet":
        return f"read_parquet({lit})"
    if ext == ".tsv":
        return f"read_csv_auto({lit}, delim='\t', all_varchar=true)"
    return f"read_csv_auto({lit}, all_varchar=true)"


def _column_types(con) -> dict:
    """{column_name: duckdb_type_string} for the loaded table `t`."""
    return {r[0]: r[1] for r in con.execute("DESCRIBE t").fetchall()}


# DuckDB database files: edited transactionally in place by rowid, like the
# SQLite writer — not rewritten via COPY like a flat file.
_DB_EXTS = {".duckdb", ".ddb"}


def _cast_fragment(types, col, value):
    """SQL fragment + binds for a value cast to col's type (shared by both write
    paths). A JSON null -> SQL NULL; anything else is CAST from the grid's
    string, so a bad value raises before any row is touched."""
    if col not in types:
        raise ValueError(f"unknown column {col!r}")
    if value is None:
        return "NULL", []
    return f"CAST(? AS {types[col]})", [value]


def _write_database(file, table, edits, deletes, inserts):
    """Apply a batch to one table of a DuckDB database file, transactionally.
    ATTACH read-write, key every edit/delete by rowid, COMMIT once; any error
    rolls the whole batch back, so a rejected save leaves the db untouched.
    Only base tables are writable — a view (no rowid) is rejected up front."""
    if not table:
        raise ValueError("no table specified")
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH {_quote_str(os.path.abspath(file))} AS db")
        kind = con.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_catalog = 'db' AND table_name = ?", [table]).fetchone()
        if not kind or kind[0] != "BASE TABLE":
            raise ValueError(f"{table!r} is not an editable table")
        qtable = f"db.{_quote_ident(table)}"
        types = {r[0]: r[1] for r in con.execute(f"DESCRIBE {qtable}").fetchall()}

        con.execute("BEGIN TRANSACTION")
        try:
            for e in edits:
                frag, binds = _cast_fragment(types, e["column"], e.get("value"))
                con.execute(
                    f"UPDATE {qtable} SET {_quote_ident(e['column'])} = {frag} "
                    f"WHERE rowid = ?", binds + [int(e["row"])])
            if deletes:
                placeholders = ", ".join("?" for _ in deletes)
                con.execute(f"DELETE FROM {qtable} WHERE rowid IN ({placeholders})",
                            [int(d) for d in deletes])
            for row in inserts:
                cols, frags, binds = [], [], []
                for col, value in row.items():
                    frag, b = _cast_fragment(types, col, value)
                    cols.append(_quote_ident(col))
                    frags.append(frag)
                    binds.extend(b)
                if cols:
                    con.execute(
                        f"INSERT INTO {qtable} ({', '.join(cols)}) "
                        f"VALUES ({', '.join(frags)})", binds)
                else:
                    con.execute(f"INSERT INTO {qtable} DEFAULT VALUES")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

        total = con.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
        return {"total_rows": total}
    finally:
        con.close()


def main(file: str, table: str = "", edits: "list | None" = None,
         deletes: "list | None" = None, inserts: "list | None" = None) -> dict:
    edits = edits or []
    deletes = deletes or []
    inserts = inserts or []
    # Same fs gate as the reader: the COPY-to-temp + os.replace rewrite below
    # goes through the parent directory, so without this a chmod -w file would
    # be silently overwritten.
    if not os.access(file, os.W_OK):
        raise PermissionError(f"{file!r} is read-only")
    ext = _logical_ext(file)
    if ext in _DB_EXTS:
        # A DuckDB database file is edited in place, not rewritten.
        return _write_database(file, table, edits, deletes, inserts)
    if ext not in _COPY_OPTS:
        raise ValueError(f"{ext} files are read-only in the DuckDB grid")

    con = duckdb.connect(":memory:")
    try:
        # rowid on this base table == file position (insertion == scan order).
        con.execute(f"CREATE TABLE t AS SELECT * FROM {_relation_for(file)}")
        types = _column_types(con)

        def cast(col, value):
            return _cast_fragment(types, col, value)

        for e in edits:
            frag, binds = cast(e["column"], e.get("value"))
            con.execute(
                f"UPDATE t SET {_quote_ident(e['column'])} = {frag} WHERE rowid = ?",
                binds + [int(e["row"])],
            )

        if deletes:
            placeholders = ", ".join("?" for _ in deletes)
            con.execute(f"DELETE FROM t WHERE rowid IN ({placeholders})",
                        [int(d) for d in deletes])

        for row in inserts:
            cols, frags, binds = [], [], []
            for col, value in row.items():
                frag, b = cast(col, value)
                cols.append(_quote_ident(col))
                frags.append(frag)
                binds.extend(b)
            if cols:
                con.execute(
                    f"INSERT INTO t ({', '.join(cols)}) VALUES ({', '.join(frags)})",
                    binds,
                )
            else:
                con.execute("INSERT INTO t DEFAULT VALUES")

        # Atomic rewrite: COPY to a temp sibling, then replace. os.replace is
        # atomic within a filesystem, so a reader never sees a half-written file
        # and a crash mid-COPY leaves the original intact.
        tmp = f"{os.path.abspath(file)}.fused-tmp.{os.getpid()}"
        try:
            con.execute(f"COPY t TO {_quote_str(tmp)} {_copy_clause(file)}")
            os.replace(tmp, os.path.abspath(file))
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

        total = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        return {"total_rows": total}
    finally:
        con.close()
