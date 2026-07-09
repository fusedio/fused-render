"""Writer backing sqlite/template.html's Save — applies a batch of edits,
deletes and inserts to one table of a SQLite database, in place.

Unlike the flat-file writer, a SQLite database is edited transactionally: open
read-write, apply every change by `rowid` (the id reader.py returns), and
commit once. Any error rolls the whole batch back, so a rejected save leaves
the database exactly as it was — all-or-nothing, same guarantee as the
file-rewrite path.

Only ordinary rowid tables are writable (reader.py's `editable` gate); a name
that isn't such a table is rejected before any write. Column names from the
client are validated against the table's real columns, so a quoted identifier
can never smuggle in SQL.

Called by fused.runPython with structured params:
  table:   "<table name>"
  edits:   [{"row": <rowid>, "column": <name>, "value": <scalar|None>}, ...]
  deletes: [<rowid>, ...]
  inserts: [{<column>: <value>, ...}, ...]
Returns {"total_rows": <int>} — the table's row count after the batch.
"""
import os
import sqlite3
import urllib.request


def _connect_rw(file):
    """Open `file` read-write (default mode). Percent-encode the path so a name
    with '?'/'#' can't be misread as URI syntax."""
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(file)) + "?mode=rw"
    return sqlite3.connect(uri, uri=True)


def _quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def _real_columns(conn, table):
    """The table's actual column names — the allowlist every client-supplied
    column is checked against, and confirmation the name is a real table."""
    rows = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    return [r[1] for r in rows]  # (cid, name, type, ...)


def _check_column(col, valid):
    if col not in valid:
        raise ValueError(f"unknown column {col!r}")
    return _quote_ident(col)


def main(file: str, table: str = "", edits: "list | None" = None,
         deletes: "list | None" = None, inserts: "list | None" = None) -> dict:
    edits = edits or []
    deletes = deletes or []
    inserts = inserts or []
    if not table:
        raise ValueError("no table specified")

    conn = _connect_rw(file)
    try:
        # A view has no columns via table_info AND can't be written; either way
        # an empty column set means "not an editable table".
        valid = _real_columns(conn, table)
        obj = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
            (table,),
        ).fetchone()
        if not valid or not obj or obj[0] != "table":
            raise ValueError(f"{table!r} is not an editable table")
        qtable = _quote_ident(table)
        # Same gate as the reader: a WITHOUT ROWID table has no rowid to key
        # edits by, so no part of a batch (not even inserts) may touch it.
        try:
            conn.execute(f"SELECT rowid FROM {qtable} LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            raise ValueError(f"{table!r} is not an editable table (no rowid)")

        # One transaction: sqlite3 opens one implicitly before the first DML and
        # commits below; any exception falls through to rollback.
        for e in edits:
            qcol = _check_column(e["column"], valid)
            conn.execute(
                f"UPDATE {qtable} SET {qcol} = ? WHERE rowid = ?",
                (e.get("value"), int(e["row"])),
            )

        if deletes:
            placeholders = ", ".join("?" for _ in deletes)
            conn.execute(f"DELETE FROM {qtable} WHERE rowid IN ({placeholders})",
                         [int(d) for d in deletes])

        for row in inserts:
            cols = [_check_column(c, valid) for c in row]
            if cols:
                placeholders = ", ".join("?" for _ in cols)
                conn.execute(
                    f"INSERT INTO {qtable} ({', '.join(cols)}) VALUES ({placeholders})",
                    list(row.values()),
                )
            else:
                conn.execute(f"INSERT INTO {qtable} DEFAULT VALUES")

        conn.commit()
        total = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
        return {"total_rows": total}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
