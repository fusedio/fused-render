"""Reader backing sqlite/template.html. Returns a JSON-safe page of one table.

The database is opened READ-ONLY (SQLite `mode=ro` URI) so browsing can never
mutate the user's file, and each call returns only the requested page of rows
plus the honest total row count — the same shape the xlsx reader uses, with
tables standing in for sheets.

For editing (via the sibling writer.py) each row is keyed by its SQLite
`rowid`, returned in a parallel `ids` list — ids[i] is the rowid of rows[i].
`editable` says whether the active object can be written: ordinary rowid tables
can; views and WITHOUT ROWID tables (no accessible `rowid`) are view-only.
"""
import os
import sqlite3
import urllib.request

# Hard cap on rows per call. The template only ever asks for 100, but this
# reader runs in-process (allowlisted as "bounded"), so an arbitrary /api/run
# request must not be able to pull an unbounded page — SQLite treats a negative
# LIMIT as "no cap", so we clamp rather than trust the caller.
MAX_LIMIT = 1000

# Alias for the rowid column so it can't collide with a real column literally
# named "rowid"; the grid reads row identity from `ids`, never from a column.
_RID = "__fused_rowid__"

# Filter operators the grid may request, grouped by how they build into SQL.
_COMPARE_OPS = {"=", "!=", ">", "<", ">=", "<="}
_NULL_OPS = {"is_null": "IS NULL", "not_null": "IS NOT NULL"}
_LIKE_OPS = {"contains": "%{}%", "starts": "{}%"}


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


def _editability(conn, active):
    """(editable, message, tooltip). Only an ordinary rowid table is writable:
    a view can't be written through, and a WITHOUT ROWID table has no `rowid` to
    key edits by (the SELECT below raises). When not editable, `message` is the
    short badge text and `tooltip` the hover explanation the grid shows.
    Determined by asking SQLite, not by guessing."""
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
        (active,),
    ).fetchone()
    if row and row[0] == "view":
        return (False, "View",
                "Read-only. This is a view, not a table — its rows are computed "
                "from other tables and can't be edited. Switch the table "
                "selector to a base table to make changes.")
    if not row or row[0] != "table":
        return (False, "Read-only", "This object can't be edited.")
    try:
        conn.execute(f"SELECT rowid FROM {_quote_ident(active)} LIMIT 1").fetchone()
        return (True, "", "")
    except sqlite3.OperationalError:
        return (False, "No rowid",
                "Read-only. This is a WITHOUT ROWID table — it has no rowid to "
                "identify rows by, so it can't be edited here.")


def _column_types(conn, active):
    """Map each column to its declared SQLite type (INTEGER, TEXT, …) for the
    header label. Views and expression columns often have no declared type;
    those come back as "" and the grid just omits the type there."""
    try:
        info = conn.execute(f"PRAGMA table_info({_quote_ident(active)})").fetchall()
        return {row[1]: (row[2] or "") for row in info}
    except sqlite3.OperationalError:
        return {}


def _like_escape(s):
    r"""Escape a substring so LIKE treats it literally: %, _ and the \ escape
    char itself are neutralised (paired with `ESCAPE '\'` on the clause)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_where(filters, columns):
    """(sql, binds) for the WHERE clause. Only filters whose column is a real
    column and whose op is allowed are used; an unknown column or op is dropped,
    so a hostile/garbled filter can neither error the query nor inject SQL.
    Values are always bound; SQLite applies the column's affinity to a bound
    comparison, so "2" compares numerically against an INTEGER column."""
    clauses, binds = [], []
    for f in filters or []:
        col, op = f.get("column"), f.get("op")
        if col not in columns:
            continue
        q = _quote_ident(col)
        if op in _NULL_OPS:
            clauses.append(f"{q} {_NULL_OPS[op]}")
        elif op in _COMPARE_OPS:
            clauses.append(f"{q} {op} ?")
            binds.append(f.get("value"))
        elif op in _LIKE_OPS:
            clauses.append(f"CAST({q} AS TEXT) LIKE ? ESCAPE '\\'")
            binds.append(_LIKE_OPS[op].format(_like_escape(str(f.get("value") or ""))))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", binds


def _build_order(sort, columns):
    """ORDER BY clause for a single {column, dir} sort, or "" when there's no
    sort / the column is unknown / the direction isn't asc|desc."""
    if not sort:
        return ""
    col = sort.get("column")
    direction = str(sort.get("dir", "")).lower()
    if col not in columns or direction not in ("asc", "desc"):
        return ""
    return f" ORDER BY {_quote_ident(col)} {direction.upper()}"


def _jsonify(value):
    """Coerce a SQLite cell value into something json.dumps can encode."""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value  # None / int / float / str are already JSON-safe


def main(file: str, table: str = "", offset: int = 0, limit: int = 100,
         sort: "dict | None" = None, filters: "list | None" = None) -> dict:
    # Clamp so a hostile/negative limit can't turn LIMIT ? into an unbounded
    # fetch, and a negative offset can't error out mid-query.
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    conn = _connect_ro(file)
    try:
        tables = _tables(conn)
        # Only ever query a name that actually exists (guards the identifier we
        # interpolate below); fall back to the first table.
        active = table if table in tables else (tables[0] if tables else "")

        columns = []
        types = {}
        rows = []
        ids = []
        total_rows = 0
        editable = False
        readonly_message = ""
        readonly_tooltip = ""
        if active:
            qname = _quote_ident(active)
            types = _column_types(conn, active)
            where, wbinds = _build_where(filters, types)
            order = _build_order(sort, types)
            # FS gate first: a chmod -w file beats any per-table verdict (the
            # writer refuses it too — see writer.py). Then the per-table gates.
            if not os.access(file, os.W_OK):
                editable, readonly_message, readonly_tooltip = (
                    False, "Read-only",
                    "The file is read-only — its permissions don't allow "
                    "writing, so it can't be edited here.")
            else:
                editable, readonly_message, readonly_tooltip = _editability(conn, active)
            # total_rows is the filtered count, so the grid pages within the filter.
            total_rows = conn.execute(
                f"SELECT COUNT(*) FROM {qname}{where}", wbinds).fetchone()[0]
            # Editable tables carry rowid as the first column; views/WITHOUT
            # ROWID tables have no usable rowid, so ids stay empty (no editing).
            select = f'SELECT rowid AS {_RID}, * FROM {qname}' if editable else f"SELECT * FROM {qname}"
            cur = conn.execute(f"{select}{where}{order} LIMIT ? OFFSET ?",
                               tuple(wbinds) + (limit, offset))
            desc = [d[0] for d in cur.description] if cur.description else []
            rid_first = editable and desc and desc[0] == _RID
            columns = desc[1:] if rid_first else desc
            for raw in cur.fetchall():
                if rid_first:
                    ids.append(raw[0])
                    values = raw[1:]
                else:
                    values = raw
                rows.append({columns[j] if j < len(columns) else f"col{j}": _jsonify(v)
                             for j, v in enumerate(values)})
        return {
            "tables": tables,
            "table": active,
            "columns": columns,
            "types": types,
            "rows": rows,
            "ids": ids,
            "total_rows": total_rows,
            "editable": editable,
            "readonly_message": readonly_message,
            "readonly_tooltip": readonly_tooltip,
        }
    finally:
        conn.close()
