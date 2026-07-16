"""Reader backing duckdb/template.html — one tabular viewer/editor for every
flat file DuckDB can read: Parquet, CSV/TSV and JSON/JSONL, including their
gzip/zstd-compressed forms (data.csv.gz, data.json.zst, …) which DuckDB's
read_*_auto scans decompress transparently.

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
JSON grid is view-only.

Flat files are single-relation, so `tables`/`table` are absent and the grid
hides its relation selector. A DuckDB **database** file (.duckdb/.ddb) is the
exception: it holds many tables/views, so the reader ATTACHes it, returns the
full `tables` list plus the selected `table`, and keys edits by DuckDB's real
`rowid` (edited in place by writer.py, not rewritten). Views have no rowid and
stay read-only.
"""
import datetime
import decimal
import os

import duckdb

MAX_LIMIT = 1000

# Extensions the grid can safely edit (rewrite in place). JSON is read-only.
_EDITABLE_EXTS = {".parquet", ".csv", ".tsv"}

# DuckDB database files: multi-table, edited in place by rowid (not rewritten
# like a flat file). Handled by a separate ATTACH-based path below.
_DB_EXTS = {".duckdb", ".ddb"}

# Compression suffixes DuckDB transparently decodes; a file's *logical* format
# is the extension underneath (data.csv.gz reads as CSV). read_*_auto detect the
# codec from the .gz/.zst suffix, so no explicit COMPRESSION arg is needed here.
_COMPRESSION_SUFFIXES = (".gz", ".zst")


def _logical_ext(file: str) -> str:
    """Format extension, seeing past a trailing compression suffix:
    'a.csv.gz' -> '.csv', 'a.parquet' -> '.parquet'."""
    base = file
    low = base.lower()
    for comp in _COMPRESSION_SUFFIXES:
        if low.endswith(comp):
            base = base[: -len(comp)]
            break
    return os.path.splitext(base)[1].lower()

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


def _rowgroup_prune(con, scan: str, sort: dict, types: dict, need: int) -> str:
    """WHERE clause restricting a sorted, unfiltered positions scan to the row
    groups that can possibly hold the top `need` rows, or "" when pruning
    can't be proven safe. Parquet's footer carries per-row-group min/max stats
    for every column; DuckDB's own top-N pushdown is reactive (it reads groups
    in file order and only skips ones beaten by rows already seen), which on a
    remote file still pays a range read per group. Deciding up front from
    parquet_metadata() moves no extra bytes — the footer is already parsed.

    Soundness (desc; asc is the mirror): rank groups by stats_min descending
    and accumulate their non-null row counts until >= need; that last group's
    stats_min is a proven lower bound L on the need-th largest value (at least
    `need` rows are >= L). Any group with stats_max < L can hold no qualifying
    row. Cumulative-row-count alone is NOT sufficient — a huge group with one
    large value would wrongly evict a group full of runners-up.

    Bails to "" (full scan — never worse than today) whenever the guarantee
    doesn't hold: any group missing min/max/null-count stats, a window deep
    enough to reach NULLs (they sort last, invisible to value stats), a
    nested column (stats don't cast to the column type), or nothing pruned."""
    col, desc_ = sort["column"], str(sort["dir"]).lower() == "desc"
    coltype = types[col]
    rows = con.execute(
        f"SELECT row_group_id, row_group_num_rows,"
        f" TRY_CAST(stats_min_value AS {coltype}),"
        f" TRY_CAST(stats_max_value AS {coltype}), stats_null_count"
        f" FROM parquet_metadata(?) WHERE path_in_schema = ?"
        f" ORDER BY row_group_id", [scan, col]).fetchall()
    # One row per row group, ids contiguous from 0 — anything else (nested
    # column with several leaves, exotic writer) breaks the cumulative
    # file_row_number offsets below.
    if [r[0] for r in rows] != list(range(len(rows))) or not rows:
        return ""
    if any(r[2] is None or r[3] is None or r[4] is None for r in rows):
        return ""
    nonnull = [r[1] - r[4] for r in rows]
    if need >= sum(nonnull):
        return ""  # window reaches the NULL tail — stats can't bound it
    ranked = sorted(range(len(rows)),
                    key=lambda i: rows[i][2 if desc_ else 3], reverse=desc_)
    acc, bound = 0, rows[ranked[0]][2 if desc_ else 3]
    for i in ranked:
        acc += nonnull[i]
        bound = rows[i][2 if desc_ else 3]
        if acc >= need:
            break
    if desc_:
        keep = {r[0] for r in rows if r[3] >= bound}
    else:
        keep = {r[0] for r in rows if r[2] <= bound}
    if len(keep) == len(rows):
        return ""
    # Row groups are contiguous in file order, so each keeps a
    # [start, start+rows) file_row_number range; merge adjacent ones.
    ranges, start = [], 0
    for gid, nrows, *_ in rows:
        if gid in keep:
            if ranges and ranges[-1][1] == start:
                ranges[-1][1] = start + nrows
            else:
                ranges.append([start, start + nrows])
        start += nrows
    clause = " OR ".join(
        f"file_row_number BETWEEN {a} AND {b - 1}" for a, b in ranges)
    return f" WHERE ({clause})"


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


# ------------------------------------------------------------- remote files
# When the page marks the file remote (the shell's /api/fs/stat says its
# bytes come from a mount), it passes `source_url` — the app's own
# /api/fs/raw URL for the file — and parquet is scanned over ranged HTTP
# instead of local file I/O. Reading a remote-backed mount like a local file
# breaks down under an analytical read pattern: DuckDB fans out concurrent
# range reads, each small kernel NFS READ stalls behind a multi-MB remote
# fetch, and macOS's 1s NFS timeout drops the whole mount ("server
# connections interrupted"). Over HTTP a slow read is just slow. This reader
# knows nothing beyond the URL it was handed; anything that fails on it falls
# back to the plain file path.


def _configure_connection(con):
    for setting, env_name in (
        ("extension_directory", "FUSED_RENDER_DUCKDB_EXTENSION_DIR"),
        ("temp_directory", "FUSED_RENDER_DUCKDB_TEMP_DIR"),
    ):
        path = os.environ.get(env_name)
        if path:
            os.makedirs(path, exist_ok=True)
            con.execute(f"SET {setting} = ?", [path])
    return con


def _http_connection():
    """A DuckDB connection for reading mounted parquet over HTTP, kept alive
    across reader runs. DuckDB's external file cache holds the byte ranges a
    query fetched, so re-filtering the same file is served from memory instead
    of re-downloading row groups — but the cache dies with its database
    instance, and the executor re-execs this module per call. Stash the
    connection on the *duckdb* module (which does survive in sys.modules of
    the server process) and hand out cursors. Racing calls may both create
    one; last-stashed wins and the loser is GC'd — harmless, so no lock."""
    # Versioned stash key: bumping it (settings change) simply strands the old
    # connection for GC and builds a fresh one — no stale-config connection can
    # outlive a code update in a long-running server.
    key = "_fused_render_http_con_v3"
    con = getattr(duckdb, key, None)
    if con is None:
        con = _configure_connection(duckdb.connect(":memory:"))
        con.execute("INSTALL httpfs; LOAD httpfs")
        con.execute("PRAGMA enable_object_cache=true")
        # The object cache alone revalidates cached parquet metadata against
        # the remote's etag/last-modified — and presigned store URLs are
        # GET-signed, so the validation HEAD 403s and every run re-fetches
        # and re-parses the footer (~700KB, several ranged reads). This
        # setting caches metadata by URL with no revalidation, which fits
        # this connection's traffic: mount-backed objects are effectively
        # immutable, and the URLs themselves rotate (presigned links are
        # re-minted every ~30min), naturally bounding staleness.
        con.execute("SET parquet_metadata_cache=true")
        # Remote scans block worker threads in HTTP reads, and the default
        # pool (one per core) lets a single fat-column query starve every
        # concurrent one — the grid's parallel per-column loads would all
        # converge to the slowest column (measured: 32 threads -> the light
        # columns land in ~5s while an 18MB column takes 13s; default pool ->
        # everything takes 13s). IO-bound, so way past core count is fine.
        con.execute("SET threads=32")
        setattr(duckdb, key, con)
    return con.cursor()


def relation_for(file: str, file_row_number: bool = False) -> str:
    """The read-only table-function that reads `file` by extension. Shared with
    writer.py so read and write agree on how each format is parsed. `file` may
    be an http(s) URL (the mounted-parquet fast path), which is passed through
    verbatim; the extension logic reads the same either way.

    `file_row_number` (parquet only) asks read_parquet to expose its
    `file_row_number` pseudo-column — the row's physical position in the file.
    It's ignored for the other formats, which have no such pseudo-column."""
    is_url = file.startswith(("http://", "https://"))
    lit = _quote_str(file if is_url else os.path.abspath(file))
    ext = _logical_ext(file)
    if ext == ".parquet":
        opt = ", file_row_number=true" if file_row_number else ""
        return f"read_parquet({lit}{opt})"
    if ext == ".tsv":
        return f"read_csv_auto({lit}, delim='\t', all_varchar=true)"
    if ext == ".csv":
        return f"read_csv_auto({lit}, all_varchar=true)"
    # .json / .jsonl / .ndjson (optionally .gz/.zst — DuckDB auto-decompresses)
    return f"read_json_auto({lit})"


def _page_sql(scan: str, relation: str, where: str, order: str,
              projection: str = "*") -> str:
    """The paging SELECT, which must also yield each row's physical file position
    as `_POS` (the writer's edit key) even after WHERE/ORDER reshuffle the page.
    `scan` is whatever relation_for reads — the file path, or the http URL on
    the mounted-parquet fast path. `projection` narrows the selected columns
    (already quoted, comma-joined); filter/sort columns need not be in it —
    WHERE/ORDER BY bind against the scan, not the select list.

    For parquet the position comes from read_parquet's `file_row_number`
    pseudo-column. Crucially, that leaves predicate + row-group pushdown intact:
    a filtered/sorted page prunes to the matching row groups, and a narrow
    projection reads only those columns' bytes — what the grid's per-column
    parallel loading over a remote file relies on. The obvious alternative —
    numbering rows with a `row_number() OVER ()` window — is a blocking
    operator that sits between the filter and the scan, defeating both
    pushdowns and forcing DuckDB to read row groups that provably hold no
    match. CSV/JSON have no such pseudo-column and can't prune anyway, so they
    keep the streaming window (LIMIT still pushes through it).

    A user sort always gets the physical position appended as a tiebreaker.
    Without it, ties at a page boundary make the LIMIT/OFFSET window itself
    nondeterministic under parallel execution — and the grid's per-column
    batches are *separate queries*, so two batches could resolve the same page
    to different row sets, leaving id-merged cells unfilled (rendered as fake
    NULLs). No sort needs none: DuckDB's default preserve_insertion_order
    already makes the unordered window deterministic."""
    if _logical_ext(scan) == ".parquet":
        rel = relation_for(scan, file_row_number=True)
        proj = "* EXCLUDE (file_row_number)" if projection == "*" else projection
        tie = f"{order}, file_row_number" if order else ""
        return (f"SELECT file_row_number AS {_POS}, {proj} "
                f"FROM {rel}{where}{tie} LIMIT ? OFFSET ?")
    proj = "*" if projection == "*" else f"{_POS}, {projection}"
    tie = f"{order}, {_POS}" if order else ""
    return (f"SELECT {proj} FROM (SELECT (row_number() OVER () - 1) AS {_POS}, * "
            f"FROM {relation}){where}{tie} LIMIT ? OFFSET ?")


def _db_tables(con):
    """(names, {name: type}) for the attached `db` — base tables and views,
    sorted by name so the relation selector is stable."""
    rows = con.execute(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_catalog = 'db' ORDER BY table_name").fetchall()
    return [r[0] for r in rows], {r[0]: r[1] for r in rows}


def _read_database(file, table, offset, limit, sort, filters, mode="full"):
    """Page one relation of a DuckDB database file. Unlike the flat-file path,
    row identity is DuckDB's real `rowid` pseudo-column (the key writer.py edits
    by), and nothing is rewritten. Views have no rowid, so they're view-only —
    the same rule the SQLite grid applies.

    `mode` gates the count/page split identically to the flat-file path (see
    main): "count" returns only {total_rows}, "page" the page with total_rows
    None, "full" both."""
    con = _configure_connection(duckdb.connect(":memory:"))
    try:
        con.execute(f"ATTACH {_quote_str(os.path.abspath(file))} AS db (READ_ONLY)")
        tables, kinds = _db_tables(con)
        if not tables:
            if mode == "count":
                return {"total_rows": 0}
            return {"columns": [], "types": {}, "rows": [], "ids": [],
                    "total_rows": None, "editable": False, "tables": [], "table": "",
                    "readonly_message": "", "readonly_tooltip": ""}
        # An unknown/stale table param falls back to the first real table
        # rather than erroring the whole view.
        sel = table if table in tables else tables[0]
        is_view = kinds[sel] != "BASE TABLE"
        relation = f"db.{_quote_ident(sel)}"

        types = {r[0]: r[1] for r in
                 con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()}
        where, wbinds = _build_where(filters, types)
        if mode == "count":
            total = con.execute(
                f"SELECT COUNT(*) FROM {relation}{where}", wbinds).fetchone()[0]
            return {"total_rows": total}
        order = _build_order(sort, types)
        # "page" defers the count (the grid fetches it in a second call); "full"
        # returns it inline.
        total_rows = con.execute(
            f"SELECT COUNT(*) FROM {relation}{where}", wbinds).fetchone()[0] \
            if mode == "full" else None

        # Base tables carry rowid (the edit key); a view has none, so it pages
        # without ids and stays read-only.
        pos = "rowid AS " + _POS if not is_view else f"CAST(NULL AS BIGINT) AS {_POS}"
        paged = (f"SELECT {pos}, * FROM {relation}{where}{order} LIMIT ? OFFSET ?")
        cur = con.execute(paged, wbinds + [limit, offset])
        desc = [d[0] for d in cur.description] if cur.description else []
        columns = desc[1:]
        rows, ids = [], []
        for raw in cur.fetchall():
            if not is_view:
                ids.append(raw[0])
            rows.append({columns[j] if j < len(columns) else f"col{j}": _jsonify(v)
                         for j, v in enumerate(raw[1:])})
        return {
            "columns": columns,
            "types": types,
            "rows": rows,
            "ids": ids,
            "total_rows": total_rows,
            "editable": not is_view,
            "tables": tables,
            "table": sel,
            "readonly_message": "" if not is_view else "View",
            "readonly_tooltip": "" if not is_view else (
                "Read-only. A view has no stored rows of its own to edit — "
                "change the underlying table instead."),
        }
    finally:
        con.close()


def _fs_gate(file, out):
    """FS gate over either path's verdict: a chmod -w file beats a
    content-level "editable" (the writer refuses it too — see writer.py).
    Content-level read-only reasons (view, JSON) keep their own message."""
    if out["editable"] and not os.access(file, os.W_OK):
        out.update(
            editable=False,
            readonly_message="Read-only",
            readonly_tooltip="The file is read-only — its permissions don't "
                             "allow writing, so it can't be edited here.")
    return out


def main(file: str, table: str = "", offset: int = 0, limit: int = 100,
         sort: "dict | None" = None, filters: "list | None" = None,
         mode: str = "full", source_url: str = "",
         columns: "list | None" = None,
         positions: "list | None" = None) -> dict:
    # Clamp so a hostile/negative limit can't turn LIMIT ? into an unbounded
    # fetch, and a negative offset can't error out mid-query.
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    ext = _logical_ext(file)
    if ext in _DB_EXTS:
        out = _read_database(file, table, offset, limit, sort, filters, mode)
        # "count" returns only {total_rows} — no editability verdict to gate.
        return out if mode == "count" else _fs_gate(file, out)

    # Remote fast path: scan the URL the page handed us on the shared
    # connection, for every flat format (.duckdb/.ddb already returned above).
    # Reading a mount-backed file over the serve — parquet, CSV/TSV or JSON —
    # keeps DuckDB's concurrent range reads off the NFS mount, whose 1s RPC
    # timeout drops the whole mount under an analytical scan. CSV/JSON can't
    # prune like parquet and re-scan the file per page, but the serve's shared
    # VFS cache (mounts.SERVE_VFS_OPT) makes the repeat reads local-disk-cheap,
    # so slow-but-safe beats fast-but-fatal. Any failure — httpfs unavailable,
    # the URL gone stale, a network error mid-query — falls back to the path.
    if source_url.startswith(("http://", "https://")):
        try:
            cur = _http_connection()
        except Exception:
            cur = None
        if cur is not None:
            try:
                return _read_flat(file, source_url, cur, ext, offset, limit,
                                  sort, filters, mode, columns, positions)
            except duckdb.Error:
                pass
            finally:
                cur.close()

    con = _configure_connection(duckdb.connect(":memory:"))
    try:
        # Reuse parsed parquet/CSV metadata across the queries in *this* call
        # (DESCRIBE, COUNT, page all touch the same footer). It can't cache
        # across calls — the executor re-execs this module and never keeps it in
        # sys.modules — so this only saves within a single reader run.
        con.execute("PRAGMA enable_object_cache=true")
        return _read_flat(file, file, con, ext, offset, limit, sort, filters,
                          mode, columns, positions)
    finally:
        con.close()


def _read_flat(file: str, scan: str, con, ext: str, offset: int, limit: int,
               sort, filters, mode: str, columns: "list | None" = None,
               positions: "list | None" = None) -> dict:
    """Page a flat file on an open connection/cursor. `scan` is what DuckDB
    reads (the file path, or its http URL on the mounted fast path); `file`
    stays the real path for the FS-level editability gate.

    Sorted/filtered pages over parquet load in two phases: mode="positions"
    resolves the page window to file positions (one query pays the sort/filter
    scan), then each of the grid's parallel column batches passes those back
    as `positions` and becomes a row-group-pruned point read — no per-batch
    re-sort, and every batch provably resolves the same row set. `positions`
    is authoritative: sort/filters/offset/limit are ignored alongside it."""
    relation = relation_for(scan)
    # Column types (BIGINT, VARCHAR, DOUBLE, …) so the grid can label each
    # header. DESCRIBE reports the relation's schema without scanning rows,
    # and drives which filters are valid and how they cast.
    types = {r[0]: r[1] for r in
             con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()}
    where, wbinds = _build_where(filters, types)

    # "schema" answers from the footer alone — no row bytes moved. The grid
    # uses it over a remote file to paint the full header + skeleton cells
    # immediately, then fills columns in from parallel batched page calls.
    if mode == "schema":
        editable = ext in _EDITABLE_EXTS
        # Compressed bytes per column (first row group — the one a first page
        # reads). The grid packs cheap columns into shared page calls and
        # gives expensive ones their own, so batches finish together instead
        # of small columns waiting on a fat one. Best-effort: parquet-only,
        # and the footer is already parsed so this moves no extra bytes.
        col_sizes = {}
        if ext == ".parquet":
            try:
                col_sizes = {r[0]: r[1] for r in con.execute(
                    "SELECT path_in_schema, SUM(total_compressed_size) "
                    "FROM parquet_metadata(?) WHERE row_group_id = 0 "
                    "GROUP BY path_in_schema", [scan]).fetchall()}
            except duckdb.Error:
                pass
        return _fs_gate(file, {
            "columns": list(types), "types": types, "rows": [], "ids": [],
            "col_sizes": col_sizes,
            "total_rows": None, "editable": editable,
            "readonly_message": "" if editable else "JSON",
            "readonly_tooltip": "" if editable else (
                "Read-only. JSON is flattened into columns for viewing; "
                "writing that back would lose the original nested structure."),
        })

    # The filtered count scans every candidate row group, so over a remote
    # mount it dominates. The grid fetches it in a separate "count" call and
    # renders the page first ("… rows"), so "page" skips it (total_rows None)
    # and "count" returns only the total. "full" (default) keeps both inline.
    if mode == "count":
        total = con.execute(
            f"SELECT COUNT(*) FROM {relation}{where}", wbinds).fetchone()[0]
        return {"total_rows": total}
    order = _build_order(sort, types)

    # Phase one of the two-phase load: just the page window's file positions,
    # ordered. Scans only the sort/filter columns (Top-N late-materializes),
    # with the position tiebreaker keeping ties deterministic.
    if mode == "positions":
        if ext != ".parquet":
            raise ValueError("positions mode requires a parquet file")
        rel = relation_for(scan, file_row_number=True)
        tie = f"{order}, file_row_number" if order else ""
        prune = where
        if order and not where:
            # Unfiltered sort: statically prune to the row groups that can
            # hold the window. Best-effort — any surprise (unorderable stats
            # values, metadata query failure) keeps the full scan.
            try:
                prune = _rowgroup_prune(con, scan, sort, types, offset + limit)
            except (duckdb.Error, TypeError):
                prune = ""
        cur = con.execute(
            f"SELECT file_row_number FROM {rel}{prune}{tie} LIMIT ? OFFSET ?",
            wbinds + [limit, offset])
        return {"positions": [r[0] for r in cur.fetchall()]}

    total_rows = con.execute(
        f"SELECT COUNT(*) FROM {relation}{where}", wbinds).fetchone()[0] \
        if mode == "full" else None

    # Optional projection: page only the named (schema-checked) columns. The
    # grid's parallel batched loads use this — each call moves only its
    # batch's column bytes.
    wanted = [c for c in (columns or []) if c in types]
    projection = ", ".join(_quote_ident(c) for c in wanted) if wanted else "*"

    if positions is not None:
        # Phase two: fetch this batch's columns for exactly those rows. The
        # IN list prunes to the row groups holding them; int() keeps a hostile
        # positions payload from reaching the SQL, and IN (-1) is the legal
        # spelling of "no rows" (positions are never negative).
        if ext != ".parquet":
            raise ValueError("positions require a parquet file")
        rel = relation_for(scan, file_row_number=True)
        proj = ("* EXCLUDE (file_row_number)" if projection == "*"
                else projection)
        in_list = ",".join(str(int(p)) for p in positions[:MAX_LIMIT]) or "-1"
        cur = con.execute(f"SELECT file_row_number AS {_POS}, {proj} "
                          f"FROM {rel} WHERE file_row_number IN ({in_list})")
    else:
        cur = con.execute(_page_sql(scan, relation, where, order, projection),
                          wbinds + [limit, offset])
    desc = [d[0] for d in cur.description] if cur.description else []
    columns = desc[1:]                      # drop the leading _POS column
    rows, ids = [], []
    for raw in cur.fetchall():
        ids.append(raw[0])
        rows.append({columns[j] if j < len(columns) else f"col{j}": _jsonify(v)
                     for j, v in enumerate(raw[1:])})
    editable = ext in _EDITABLE_EXTS
    return _fs_gate(file, {
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
    })
