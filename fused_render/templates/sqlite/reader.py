"""Reader backing sqlite/template.html. Returns a JSON-safe page of one table.

The database is opened READ-ONLY (SQLite `mode=ro` URI) so browsing can never
mutate the user's file, and each call returns only the requested page of rows
plus the honest total row count — the same shape the xlsx reader uses, with
tables standing in for sheets.
"""
import os
import sqlite3
import urllib.request


def _connect_ro(file):
    """Open `file` read-only. Percent-encode the path so a name containing
    '?' or '#' can't be misread as URI query/fragment syntax."""
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(file)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _tables(conn):
    """User tables and views, excluding SQLite's internal sqlite_* objects."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "ORDER BY name"
    )
    return [row[0] for row in cur.fetchall()]


def _quote_ident(name):
    """Quote a SQLite identifier — double it up so an embedded quote can't break
    out. Only ever called on names that came from sqlite_master."""
    return '"' + name.replace('"', '""') + '"'


def _jsonify(value):
    """Coerce a SQLite cell value into something json.dumps can encode."""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value  # None / int / float / str are already JSON-safe


def main(file: str, table: str = "", offset: int = 0, limit: int = 100) -> dict:
    conn = _connect_ro(file)
    try:
        tables = _tables(conn)
        # Only ever query a name that actually exists (guards the identifier we
        # interpolate below); fall back to the first table.
        active = table if table in tables else (tables[0] if tables else "")

        columns = []
        rows = []
        total_rows = 0
        if active:
            qname = _quote_ident(active)
            total_rows = conn.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]
            cur = conn.execute(f"SELECT * FROM {qname} LIMIT ? OFFSET ?", (limit, offset))
            columns = [d[0] for d in cur.description] if cur.description else []
            for raw in cur.fetchall():
                rows.append({columns[j] if j < len(columns) else f"col{j}": _jsonify(v)
                             for j, v in enumerate(raw)})
        return {
            "tables": tables,
            "table": active,
            "columns": columns,
            "rows": rows,
            "total_rows": total_rows,
        }
    finally:
        conn.close()
