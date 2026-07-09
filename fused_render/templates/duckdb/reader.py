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

Returns {columns, types, rows, ids, total_rows, editable}. `types` maps each
column to its DuckDB type name (BIGINT, VARCHAR, …) for the header label.
`editable` is false for
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

# Physical-position column injected into the page query. row_number() over the
# raw scan is the 0-based file position — the same key the writer's rowid uses —
# so it stays attached to each row even after WHERE/ORDER BY reshuffle the page.
_POS = "__fused_pos__"

# Filter operators the grid may request, grouped by how they build into SQL.
_COMPARE_OPS = {"=", "!=", ">", "<", ">=", "<="}
_NULL_OPS = {"is_null": "IS NULL", "not_null": "IS NOT NULL"}
_LIKE_OPS = {"contains": "%{}%", "starts": "{}%"}


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


def _quote_ident(name: str) -> str:
    """Double-quote a column identifier, doubling embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def _like_escape(s: str) -> str:
    r"""Escape a substring so LIKE treats it literally: %, _ and the \ escape
    char itself are neutralised (paired with `ESCAPE '\'` on the clause)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_where(filters, types):
    """(sql, binds) for the WHERE clause. Only filters whose column exists in the
    schema and whose op is allowed are used — an unknown column or op is dropped,
    so a hostile/garbled filter can neither error the query nor inject SQL.
    Values are bound (never interpolated) and comparisons cast to the column's
    own type so "200" compares numerically against a BIGINT column."""
    clauses, binds = [], []
    for f in filters or []:
        col, op = f.get("column"), f.get("op")
        if col not in types:
            continue
        q = _quote_ident(col)
        if op in _NULL_OPS:
            clauses.append(f"{q} {_NULL_OPS[op]}")
        elif op in _COMPARE_OPS:
            clauses.append(f"{q} {op} CAST(? AS {types[col]})")
            binds.append(f.get("value"))
        elif op in _LIKE_OPS:
            clauses.append(f"CAST({q} AS VARCHAR) LIKE ? ESCAPE '\\'")
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


def main(file: str, offset: int = 0, limit: int = 100,
         sort: "dict | None" = None, filters: "list | None" = None) -> dict:
    # Clamp so a hostile/negative limit can't turn LIMIT ? into an unbounded
    # fetch, and a negative offset can't error out mid-query.
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    ext = os.path.splitext(file)[1].lower()
    relation = relation_for(file)

    con = duckdb.connect(":memory:")
    try:
        # Column types (BIGINT, VARCHAR, DOUBLE, …) so the grid can label each
        # header. DESCRIBE reports the relation's schema without scanning rows,
        # and drives which filters are valid and how they cast.
        types = {r[0]: r[1] for r in
                 con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()}
        where, wbinds = _build_where(filters, types)
        order = _build_order(sort, types)

        # total_rows is the filtered count, so the grid pages within the filter.
        total_rows = con.execute(
            f"SELECT COUNT(*) FROM {relation}{where}", wbinds).fetchone()[0]

        # Number every row by physical position *before* filtering/sorting (the
        # window sits in the inner scan), then page the reshuffled result. ids
        # therefore stay the file positions the writer edits by, sorted or not.
        paged = (f"SELECT * FROM (SELECT (row_number() OVER () - 1) AS {_POS}, * "
                 f"FROM {relation}){where}{order} LIMIT ? OFFSET ?")
        cur = con.execute(paged, wbinds + [limit, offset])
        desc = [d[0] for d in cur.description] if cur.description else []
        columns = desc[1:]                      # drop the leading _POS column
        rows, ids = [], []
        for raw in cur.fetchall():
            ids.append(raw[0])
            rows.append({columns[j] if j < len(columns) else f"col{j}": _jsonify(v)
                         for j, v in enumerate(raw[1:])})
        editable = ext in _EDITABLE_EXTS
        return {
            "columns": columns,
            "types": types,
            "rows": rows,
            # Absolute file position of each returned row — the edit/delete key.
            "ids": ids,
            "total_rows": total_rows,
            "editable": editable,
            "readonly_message": "" if editable else "JSON",
            "readonly_tooltip": "" if editable else (
                "Read-only. JSON is flattened into columns for viewing; writing "
                "that back would lose the original nested structure."),
        }
    finally:
        con.close()
