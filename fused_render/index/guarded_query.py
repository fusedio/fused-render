"""User SQL against the index, confined to reading it.

`query.py` builds its SQL from escaped literals and a fixed allowlist. This
module does the opposite — it runs the caller's own statement — so the whole
file is the two guards that make that safe, and they are independent:

1. **The statement-type gate**, before anything executes. This is load-bearing,
   not defence in depth: behind a fully locked configuration an in-memory
   `INSERT` / `CREATE TABLE` / `DELETE` still succeeds, so the only place a
   write can be refused is before it runs. `duckdb.extract_statements` gives
   the type — and note that `PRAGMA database_list` parses as
   `StatementType.SELECT`, so a naive "SELECT only" gate would admit PRAGMA and
   CALL anyway while claiming not to. They are therefore admitted explicitly,
   alongside DESCRIBE and EXPLAIN; everything else is refused.

2. **The DuckDB lockdown**, in this exact order, once per connection:
   `allowed_directories` → `enable_external_access=false` →
   `lock_configuration=true`. The order matters and so does all three:
   `allowed_directories` ALONE CONFINES NOTHING — it is a carve-out from
   `enable_external_access=false`, not a restriction — and without the lock a
   statement can simply widen the allowlist again. Afterwards the lazy
   `read_parquet` views over the index still resolve (they are inside the
   allowed directory) and every path outside it is a permission error.

Deliberately NOT a function blocklist. There are ~40 file-touching functions
among ~2 900 built-ins and that ratio moves every release; a list would be
stale on the next upgrade, while the confinement covers the ones nobody
enumerated.

The scope of what this can reach is the same scope `/api/run` already has (it
executes arbitrary local Python), so this is not a new capability — it is a
narrower one, and the guards exist so a mistyped or model-written statement
cannot write to the index or read outside it.

See specs/query.md §5.
"""
import os
import threading

from fused_render.index.config import IndexConfig
from fused_render.index.store import parquet_src, partition_files

# Rows a single query may return, whatever the caller asks for — the same cap
# `query.lookup` uses, so the two read surfaces cannot disagree about how much
# a client is handed.
MAX_LIMIT = 5_000

# Wall clock a statement gets before `con.interrupt()` stops it. A cross join
# over 600k rows is trivial to type and impossible to bound by inspection, and
# the panel that calls this is a text box, so a timeout is the only backstop.
TIMEOUT_S = 10.0


def _statement_types():
    """The read-only statement types, resolved at call time.

    Named off `duckdb.StatementType` rather than written as integers: the
    numeric values are DuckDB internals, and a release that renumbered them
    would silently turn this allowlist into a different one."""
    import duckdb

    t = duckdb.StatementType
    # SELECT covers WITH, DESCRIBE and PRAGMA as duckdb parses them. CALL and
    # EXPLAIN are their own types. Everything absent here — INSERT, UPDATE,
    # DELETE, CREATE, DROP, ALTER, COPY, ATTACH, DETACH, LOAD, SET, TRANSACTION
    # — is refused.
    return {t.SELECT, t.CALL, t.EXPLAIN}


# The typed empty stand-ins used when the index has no partitions yet, so a
# query written against a built index fails on nothing but its own logic.
# MIRRORS store.schemas — int32 → INTEGER, int64 → BIGINT, float64 → DOUBLE —
# and tests/test_index_guarded_query.py DESCRIBEs both views against it, which
# is what stops the two drifting.
_EMPTY_FILES = ("SELECT CAST(NULL AS VARCHAR) AS path, CAST(NULL AS VARCHAR) AS dir, "
                "CAST(NULL AS VARCHAR) AS name, CAST(NULL AS VARCHAR) AS ext, "
                "CAST(NULL AS BIGINT) AS size, CAST(NULL AS DOUBLE) AS mtime, "
                "CAST(NULL AS INTEGER) AS depth WHERE false")
_EMPTY_DIRS = ("SELECT CAST(NULL AS VARCHAR) AS dir, CAST(NULL AS VARCHAR) AS sig, "
               "CAST(NULL AS INTEGER) AS n_files, CAST(NULL AS BIGINT) AS total_size, "
               "CAST(NULL AS BIGINT) AS mtime_ns, CAST(NULL AS INTEGER) AS n_subdirs, "
               "CAST(NULL AS INTEGER) AS depth WHERE false")


def _one_read_statement(sql: str) -> str:
    """`sql` stripped of its trailing semicolon, or ValueError.

    Refuses an empty input, more than one statement, anything that does not
    parse, and any statement type that is not a read."""
    import duckdb

    body = (sql or "").strip().rstrip(";").strip()
    if not body:
        raise ValueError("no SQL statement was given")
    try:
        statements = duckdb.extract_statements(body)
    except duckdb.Error as e:
        raise ValueError(str(e)) from e
    if len(statements) != 1:
        # One statement only. A batch is how a read-looking prefix smuggles a
        # write past a reader skimming the box, and there is no result shape for
        # more than one anyway.
        raise ValueError("send exactly one statement")
    allowed = _statement_types()
    if statements[0].type not in allowed:
        raise ValueError(
            "only read-only statements are allowed here "
            "(SELECT, WITH, PRAGMA, CALL, DESCRIBE, EXPLAIN)")
    return body


def _connect(cfg: IndexConfig):
    """An in-memory connection holding `files` and `dirs`, then locked down."""
    import duckdb

    con = duckdb.connect()
    files = parquet_src(partition_files(cfg)) or _EMPTY_FILES
    dirs = (parquet_src([cfg.dirs_parquet])
            if os.path.exists(cfg.dirs_parquet) else None) or _EMPTY_DIRS
    # LAZY views, not tables. Copying the rows in first was the obvious shape
    # and is strictly slower: measured at 300k rows, 14.9ms to materialise a
    # table against 6.8ms for the view — the parquet is already columnar and
    # already the format DuckDB wants.
    con.execute(f"CREATE VIEW files AS SELECT * FROM {files}"
                if files.startswith("read_parquet")
                else f"CREATE VIEW files AS {files}")
    con.execute(f"CREATE VIEW dirs AS SELECT * FROM {dirs}"
                if dirs.startswith("read_parquet")
                else f"CREATE VIEW dirs AS {dirs}")
    # The order is the guard (see the module docstring). Set while external
    # access is still on, then withdraw it, then lock so nothing can restore it.
    quoted = cfg.dir.replace("'", "''")
    con.execute(f"SET allowed_directories=['{quoted}']")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    return con


def _jsonable(v):
    """A cell as JSON can carry it. The user picks the columns, so anything
    duckdb can produce may arrive — dates, decimals, blobs, intervals."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def run_guarded(cfg: IndexConfig, sql: str, limit: int = 200) -> dict:
    """Run one read-only statement against the index; `{columns, rows, truncated}`.

    Raises ValueError for anything the statement-type gate refuses (the caller
    turns that into a 400). A statement that parses but cannot run — a bad
    column, a path outside the index — surfaces duckdb's own exception."""
    body = _one_read_statement(sql)
    cap = max(0, min(int(limit), MAX_LIMIT))
    con = _connect(cfg)
    # A statement that would return the whole index must not be MATERIALISED in
    # full just to be trimmed afterwards, so the cap goes into the SQL. PRAGMA
    # parses as SELECT but is not a subquery-able expression, so a wrap that
    # does not parse falls back to the bare statement and the fetch cap — those
    # statements answer in tens of rows by nature.
    import duckdb

    timer = threading.Timer(TIMEOUT_S, con.interrupt)
    timer.start()
    try:
        try:
            cur = con.execute(
                f"SELECT * FROM ({body}) AS _guarded LIMIT {cap + 1}")
        except (duckdb.ParserException, duckdb.BinderException):
            cur = con.execute(body)
        rows = cur.fetchmany(cap + 1)
        columns = [d[0] for d in (cur.description or [])]
    finally:
        timer.cancel()
        con.close()
    return {"columns": columns,
            "rows": [[_jsonable(v) for v in r] for r in rows[:cap]],
            "truncated": len(rows) > cap}
