"""Workbook engine behind xlsx/template.html — and the same engine, as tools,
for an agent (`mcp.toml` beside this file publishes them over MCP via
`fused app serve <this folder>`; the page reaches them through `main(action=…)`).

    sheets(file)                         every sheet: name, row count, typed columns
    page(file, sheet, offset, limit)     one page of rows (what the grid shows)
    describe(file, sheet, columns)       per-column statistics
    query(file, sql, limit)              read-only DuckDB SQL; every sheet is a table
    cells(file, ref, sheet)              an A1 range: value AND formula of each cell

Every tool answers from ONE materialized snapshot of the workbook per file
version (path, mtime, size): read once, formulas resolved once, then served
from memory. That is what makes an agent's "give me statistics of this" a
millisecond query instead of a re-parse per question.

Formula values. `data_only=True` gives the value Excel cached at its last
save. A workbook no Excel ever computed — built by openpyxl, or saved by the
`excel` template — has formulas but no cached values, so those cells are
evaluated with pycel, all of them in one pass, in a bounded subprocess
(`_pycel_worker.py`): building pycel's graph is not a bounded operation, and
this file runs IN-PROCESS in the server (D72, `executor.INPROCESS_HELPERS`).
The results are kept on disk too, so a fresh process (an MCP call) does not
pay for the evaluation again. A cell the worker cannot evaluate stays blank
and is counted in `unevaluated` — reported, never guessed.

Dates. pycel returns Excel day serials; a computed cell is shown as a date
when Excel would show one: its own number format is a date format, the whole
formula is one date-function call (`DATE(…)`, `TODAY()`, …), or it is a plain
`ref ± N` offset off a date cell. `TODAY()-A2` is a day count and stays a
number.

The first row of a sheet is its header; blank header cells get positional
names (`col3`), and repeated ones a suffix (`Amount_2`), so every column is
addressable — by the grid, and as a SQL identifier.
"""
import datetime
import decimal
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
from collections import OrderedDict

import openpyxl
from openpyxl.styles.numbers import is_date_format
from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries
from openpyxl.utils.datetime import from_excel

_PYCEL_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pycel_worker.py")
# One spawn evaluates every uncached formula in the workbook, and its result
# is cached per file version — so this bounds a one-time cost, not a per-page
# one.
_PYCEL_TIMEOUT = 20.0

# A snapshot holds the whole workbook in the server's memory, so it is only
# built for files up to this size. A bigger one is still paged — streamed,
# cached values only, the way this reader always read it — but the analysis
# tools refuse it by name rather than load it; the `excel` template converts
# big workbooks to Parquet for exactly that.
_SNAPSHOT_MAX_BYTES = 25 * 1024 * 1024

# Formula results survive the process here, keyed by file version (see
# _formula_cache_path). ~ is expanded per call, never at import.
_CACHE_DIR = os.path.join("~", ".fused-render", "cache", "xlsx")

# SQL runs in the server process, so it is bounded: wall clock (interrupted),
# memory, threads, and no filesystem or network at all.
_QUERY_TIMEOUT = 10.0
_QUERY_MAX_ROWS = 10_000
_CELLS_MAX = 10_000
_TOP_VALUES = 5

# Snapshots kept in memory, most recent last. The executor re-execs this
# module on every call, so the store lives on the openpyxl module (which does
# persist in sys.modules) — the pattern duckdb/reader.py uses for its
# connection.
_MEMO_KEY = "_fused_render_xlsx_snapshots"
_MEMO_MAX_CELLS = 4_000_000  # across every snapshot kept; the newest one always stays

_EXCEL_ERRORS = frozenset((
    "#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!",
    "#SPILL!", "#CALC!", "#GETTING_DATA",
))
# Whole-formula date calls (see module docstring, "Dates").
_DATE_FUNCS = ("DATE(", "TODAY(", "NOW(", "EDATE(", "EOMONTH(", "WORKDAY(", "WORKDAY.INTL(")
_OFFSET_RE = re.compile(
    r"^=?\s*(?:(\$?[A-Za-z]{1,3}\$?[0-9]+)\s*[+-]\s*[0-9.]+|[0-9.]+\s*\+\s*(\$?[A-Za-z]{1,3}\$?[0-9]+))\s*$"
)
# A formula using one of these changes value without the file changing, so
# its cached results are only good for the day they were computed.
_VOLATILE_RE = re.compile(r"\b(TODAY|NOW|RAND|RANDBETWEEN)\s*\(", re.IGNORECASE)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

class _Sheet:
    __slots__ = ("name", "grid", "formulas", "columns", "unevaluated", "_table")

    def __init__(self, name):
        self.name = name
        self.grid = []        # every row, header included; grid[r][c] = sheet cell (r+1, c+1)
        self.formulas = {}    # (r, c) -> formula text, e.g. "=A2*50"
        self.columns = []
        self.unevaluated = 0  # formula cells with no cached value that could not be computed
        self._table = None    # typed pyarrow table of the data rows, built on first SQL use

    @property
    def total_rows(self):
        return max(len(self.grid) - 1, 0)

    def value(self, r, c):
        row = self.grid[r] if r < len(self.grid) else ()
        return row[c] if c < len(row) else None

    def table(self):
        if self._table is None:
            self._table = _arrow_table(self)
        return self._table


class _Book:
    __slots__ = ("sheets", "volatile", "day", "cells")

    def __init__(self, sheets, volatile):
        self.sheets = sheets  # OrderedDict name -> _Sheet, in workbook order
        self.volatile = volatile
        self.day = datetime.date.today()
        self.cells = sum(len(row) for s in sheets.values() for row in s.grid)

    def sheet(self, name=""):
        if not self.sheets:
            raise ValueError("the workbook has no sheets")
        if not name:
            return next(iter(self.sheets.values()))
        if name in self.sheets:
            return self.sheets[name]
        raise ValueError(f"no sheet named {name!r}; sheets are {list(self.sheets)}")


class TooLarge(ValueError):
    pass


def _version(file):
    st = os.stat(file)
    return (os.path.realpath(file), st.st_mtime_ns, st.st_size)


def _memo():
    return openpyxl.__dict__.setdefault(_MEMO_KEY, (threading.Lock(), OrderedDict()))


def _book(file) -> _Book:
    """The snapshot for `file`'s current version, built at most once."""
    key = _version(file)
    if key[2] > _SNAPSHOT_MAX_BYTES:
        raise TooLarge(
            f"{os.path.basename(file)} is {key[2] / 2**20:.0f} MB; these tools load "
            f"workbooks up to {_SNAPSHOT_MAX_BYTES / 2**20:.0f} MB — open it with the "
            "excel template, which pages and queries big sheets through Parquet")
    lock, store = _memo()
    with lock:
        book = store.get(key)
        if book is not None and not (book.volatile and book.day != datetime.date.today()):
            store.move_to_end(key)
            return book
    book = _build(file, key)
    with lock:
        # Older versions of the same file are dead weight the moment it changes.
        for stale in [k for k in store if k[0] == key[0] and k != key]:
            del store[stale]
        store[key] = book
        while len(store) > 1 and sum(b.cells for b in store.values()) > _MEMO_MAX_CELLS:
            store.popitem(last=False)
    return book


def _build(file, key) -> _Book:
    sheets = OrderedDict()
    date_formatted = set()  # (sheet, r, c) formula cells whose number format is a date
    # Pass 1, data_only=False: constants, plus every formula's text and number
    # format. A workbook without formulas is complete after this pass.
    wb = openpyxl.load_workbook(file, read_only=True, data_only=False)
    try:
        epoch = wb.epoch
        for ws in wb.worksheets:
            sheet = sheets[ws.title] = _Sheet(ws.title)
            for r, cells in enumerate(ws.iter_rows()):
                row = []
                for c, cell in enumerate(cells):
                    v = cell.value
                    if isinstance(v, str) and v.startswith("="):
                        sheet.formulas[(r, c)] = v
                        if is_date_format(cell.number_format):
                            date_formatted.add((ws.title, r, c))
                        v = None
                    elif v is not None and not isinstance(v, str) and not _is_scalar(v):
                        v = str(v)  # an ArrayFormula / DataTableFormula object, etc.
                    row.append(v)
                sheet.grid.append(row)
    finally:
        wb.close()

    volatile = any(_VOLATILE_RE.search(f) for s in sheets.values() for f in s.formulas.values())
    pending = []
    if any(s.formulas for s in sheets.values()):
        # Pass 2, data_only=True: Excel's cached result for each formula cell.
        wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                sheet = sheets[ws.title]
                if not sheet.formulas:
                    continue
                for r, row in enumerate(ws.iter_rows(values_only=True)):
                    for c, v in enumerate(row):
                        if (r, c) in sheet.formulas and v is not None:
                            sheet.grid[r][c] = v
        finally:
            wb.close()
        pending = [(s.name, r, c) for s in sheets.values()
                   for (r, c) in s.formulas if s.grid[r][c] is None]

    if pending:
        computed = _formula_values(file, key, pending, volatile)
        for name, r, c in pending:
            sheet = sheets[name]
            if (name, r, c) not in computed:
                sheet.unevaluated += 1
                continue
            v = computed[(name, r, c)]
            if (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and _shows_as_date(sheet, r, c, (name, r, c) in date_formatted)):
                try:
                    v = from_excel(v, epoch)
                except (ValueError, OverflowError):
                    pass  # a serial outside Excel's calendar is shown as the number it is
            sheet.grid[r][c] = v

    for sheet in sheets.values():
        sheet.columns = _column_names(sheet.grid[0] if sheet.grid else [],
                                      max((len(row) for row in sheet.grid), default=0))
    return _Book(sheets, volatile)


def _is_scalar(v):
    return isinstance(v, (bool, int, float, decimal.Decimal,
                          datetime.datetime, datetime.date, datetime.time, datetime.timedelta))


def _column_names(header, width):
    """Header names, unique case-insensitively (SQL identifiers are)."""
    names, seen = [], set()
    for j in range(width):
        v = header[j] if j < len(header) else None
        base = _text(v).strip() if v is not None else ""
        base = base or f"col{j}"
        name, n = base, 1
        while name.lower() in seen:
            n += 1
            name = f"{base}_{n}"
        seen.add(name.lower())
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# formula evaluation
# ---------------------------------------------------------------------------

def _formula_cache_path(key, volatile):
    realpath, mtime_ns, size = key
    stem = hashlib.sha1(realpath.encode("utf-8", "surrogatepass")).hexdigest()[:20]
    day = f"-{datetime.date.today().isoformat()}" if volatile else ""
    return os.path.join(os.path.expanduser(_CACHE_DIR), stem, f"{mtime_ns}-{size}{day}.json")


def _formula_values(file, key, cells, volatile):
    """{(sheet, r, c): value} for the uncached formula cells, from the disk
    cache or one bounded worker run. A cell the worker could not evaluate is
    absent. A run that timed out or crashed is NOT cached, so a transiently
    slow machine gets another chance next time the workbook is opened."""
    path = _formula_cache_path(key, volatile)
    try:
        with open(path, encoding="utf-8") as fh:
            return {(s, r, c): v for s, r, c, v in json.load(fh)}
    except (OSError, ValueError):
        pass
    request = json.dumps({"file": file, "cells": [[s, r + 1, c + 1] for s, r, c in cells]})
    try:
        proc = subprocess.run(
            [sys.executable, _PYCEL_WORKER],
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PYCEL_TIMEOUT,
            close_fds=False,  # see executor.py's own subprocess call: avoids a
            # fork() + pthread_atfork crash when pyproj/PROJ is resident
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        results = json.loads(proc.stdout)
    except ValueError:
        return {}
    out = {(s, row - 1, col - 1): v for s, row, col, v in results}
    _write_cache(path, [[s, r, c, v] for (s, r, c), v in out.items()])
    return out


def _write_cache(path, rows):
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        for old in os.listdir(folder):  # only the current version is ever read again
            if old != os.path.basename(path):
                os.remove(os.path.join(folder, old))
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, allow_nan=False)
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that can't be written costs speed, not correctness


def _shows_as_date(sheet, r, c, date_formatted, depth=0):
    """Whether Excel would display the computed number at (r, c) as a date."""
    formula = sheet.formulas.get((r, c))
    if formula is None:
        return isinstance(sheet.value(r, c), (datetime.date, datetime.datetime))
    if date_formatted or _is_date_call(formula):
        return True
    m = _OFFSET_RE.match(formula.strip())
    if not m or depth:
        return False
    row, col = coordinate_to_tuple((m.group(1) or m.group(2)).replace("$", "").upper())
    return _shows_as_date(sheet, row - 1, col - 1, False, depth + 1)


def _is_date_call(formula):
    """True only when the WHOLE formula is one date-function call —
    `TODAY()-A2` starts like one but is a day count."""
    body = formula.lstrip("=").strip()
    upper = body.upper()
    for name in _DATE_FUNCS:
        if not upper.startswith(name):
            continue
        depth = 0
        for i in range(len(name) - 1, len(body)):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    return body[i + 1:].strip() == ""
        return False
    return False


# ---------------------------------------------------------------------------
# values out
# ---------------------------------------------------------------------------

def _jsonify(value):
    """A cell value as JSON: dates ISO, Decimal/bytes/timedelta as text."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "#NUM!"
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _text(value):
    v = _jsonify(value)
    return v if isinstance(v, str) else json.dumps(v)


def _column_kind(values):
    """number / integer / date / boolean / text / empty — what SQL sees.
    Error values (#DIV/0!, …) and blank strings don't decide a column's type;
    they read as NULL in a typed one, the way Excel's SUM skips them."""
    kinds = set()
    for v in values:
        if v is None or (isinstance(v, str) and (v in _EXCEL_ERRORS or not v.strip())):
            continue
        if isinstance(v, bool):
            kinds.add("boolean")
        elif isinstance(v, int):
            kinds.add("integer")
        elif isinstance(v, (float, decimal.Decimal)):
            kinds.add("number")
        elif isinstance(v, (datetime.datetime, datetime.date)):
            kinds.add("date")
        else:
            kinds.add("text")
        if len(kinds) > 1 and kinds != {"integer", "number"}:
            return "text"
    if not kinds:
        return "empty"
    return "number" if kinds == {"integer", "number"} else kinds.pop()


def _typed_column(values, kind):
    import pyarrow as pa

    if kind == "text":
        return pa.array([None if v is None else _text(v) for v in values], pa.string())
    vals = [None if v is None or (isinstance(v, str) and (v in _EXCEL_ERRORS or not v.strip()))
            else v for v in values]
    if kind == "integer" and all(v is None or -2**63 <= v < 2**63 for v in vals):
        return pa.array(vals, pa.int64())
    if kind in ("integer", "number"):
        return pa.array([None if v is None else float(v) for v in vals], pa.float64())
    if kind == "boolean":
        return pa.array(vals, pa.bool_())
    if kind == "date":
        return pa.array([v if v is None or isinstance(v, datetime.datetime)
                         else datetime.datetime(v.year, v.month, v.day) for v in vals],
                        pa.timestamp("us"))
    return pa.nulls(len(values), pa.string())  # empty


def _arrow_table(sheet):
    import pyarrow as pa

    data = sheet.grid[1:]
    arrays = []
    for c in range(len(sheet.columns)):
        values = [row[c] if c < len(row) else None for row in data]
        arrays.append(_typed_column(values, _column_kind(values)))
    return pa.table(arrays, names=sheet.columns)


def _schema(sheet):
    table = sheet.table()
    return [{"name": f.name, "type": str(f.type)} for f in table.schema]


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def sheets(file: str) -> dict:
    """List every sheet in the workbook with its row count and typed columns.
    Start here: the sheet names are the table names `query` accepts."""
    book = _book(file)
    return {"file": file, "sheets": [
        {"name": s.name, "rows": s.total_rows, "columns": _schema(s),
         "formulas": len(s.formulas), "unevaluated": s.unevaluated}
        for s in book.sheets.values()]}


def page(file: str, sheet: str = "", offset: int = 0, limit: int = 100) -> dict:
    """Rows `offset`..`offset+limit` of one sheet (the first sheet by default),
    as {column: value} objects, with formula cells showing their value."""
    offset, limit = max(int(offset), 0), max(int(limit), 0)
    try:
        book = _book(file)
    except TooLarge:
        return _streamed_page(file, sheet, offset, limit)
    s = book.sheet(sheet) if sheet in book.sheets else book.sheet()
    rows = [{col: _jsonify(s.value(r, c)) for c, col in enumerate(s.columns)}
            for r in range(1 + offset, min(1 + offset + limit, len(s.grid)))]
    return {
        "sheets": list(book.sheets),
        "sheet": s.name,
        "columns": s.columns,
        "rows": rows,
        "total_rows": s.total_rows,
        "unevaluated": s.unevaluated,
    }


def _streamed_page(file, sheet, offset, limit):
    """page() for a workbook too big to snapshot: one streaming pass that keeps
    only the requested rows. Formula cells show Excel's cached value."""
    wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
    try:
        names = wb.sheetnames
        active = sheet if sheet in names else (names[0] if names else "")
        columns, rows, total = [], [], 0
        if active:
            for i, raw in enumerate(wb[active].iter_rows(values_only=True)):
                if i == 0:
                    columns = _column_names(raw, len(raw))
                    continue
                total += 1
                if offset < total <= offset + limit:
                    rows.append({col: _jsonify(raw[j] if j < len(raw) else None)
                                 for j, col in enumerate(columns)})
    finally:
        wb.close()
    return {"sheets": names, "sheet": active, "columns": columns, "rows": rows,
            "total_rows": total, "unevaluated": 0}


def describe(file: str, sheet: str = "", columns: str = "") -> dict:
    """Summary statistics for each column of a sheet (the first by default):
    count, nulls, distinct; min/max/mean/median/std/sum for numbers; min/max
    for dates; the most common values for text. `columns` is an optional
    comma-separated subset."""
    import duckdb

    book = _book(file)
    s = book.sheet(sheet)
    table = s.table()
    wanted = [c.strip() for c in columns.split(",") if c.strip()] if columns else table.column_names
    missing = [c for c in wanted if c not in table.column_names]
    if missing:
        raise ValueError(f"no column(s) {missing} in sheet {s.name!r}; columns are {table.column_names}")

    con = _sandbox(duckdb, {"t": table})
    try:
        stats = []
        for name in wanted:
            q = _ident(name)
            kind = str(table.schema.field(name).type)
            aggs = [f"count({q})", f"count(*) - count({q})", f"count(DISTINCT {q})"]
            keys = ["count", "nulls", "distinct"]
            if kind in ("int64", "double"):
                aggs += [f"min({q})", f"max({q})", f"avg({q})", f"median({q})",
                         f"stddev_samp({q})", f"sum({q})"]
                keys += ["min", "max", "mean", "median", "std", "sum"]
            elif kind.startswith("timestamp"):
                aggs += [f"min({q})", f"max({q})"]
                keys += ["min", "max"]
            row = con.execute(f"SELECT {', '.join(aggs)} FROM t").fetchone()
            entry = {"column": name, "type": kind}
            entry.update({k: _jsonify(v) for k, v in zip(keys, row)})
            if kind == "string":
                entry["top"] = [
                    {"value": v, "count": n} for v, n in con.execute(
                        f"SELECT {q}, count(*) AS n FROM t WHERE {q} IS NOT NULL "
                        f"GROUP BY 1 ORDER BY n DESC, 1 LIMIT {_TOP_VALUES}").fetchall()]
            c = s.columns.index(name)
            errors = sum(1 for r in range(1, len(s.grid))
                         if isinstance(v := s.value(r, c), str) and v in _EXCEL_ERRORS)
            if errors:
                entry["errors"] = errors
            stats.append(entry)
    finally:
        con.close()
    return {"sheet": s.name, "rows": s.total_rows, "unevaluated": s.unevaluated, "columns": stats}


def query(file: str, sql: str, limit: int = 1000) -> dict:
    """Run read-only DuckDB SQL over the workbook. Every sheet is a table
    named after it (quote names with spaces: SELECT * FROM "Q1 sales");
    column names come from each sheet's header row — `sheets` lists them with
    their types. At most `limit` rows are returned; `truncated` says whether
    there were more."""
    import duckdb

    book = _book(file)
    limit = min(max(int(limit), 1), _QUERY_MAX_ROWS)
    con = _sandbox(duckdb, {s.name: s.table() for s in book.sheets.values()})
    timer = threading.Timer(_QUERY_TIMEOUT, con.interrupt)
    timer.start()
    try:
        cur = con.execute(sql)
        if cur.description is None:
            return {"columns": [], "types": [], "rows": [], "truncated": False}
        cols = [d[0] for d in cur.description]
        types = [str(d[1]) for d in cur.description]
        fetched = cur.fetchmany(limit + 1)
    except duckdb.InterruptException:
        raise TimeoutError(f"query exceeded {_QUERY_TIMEOUT:g}s") from None
    finally:
        timer.cancel()
        con.close()
    return {
        "columns": cols,
        "types": types,
        "rows": [[_jsonify(v) for v in row] for row in fetched[:limit]],
        "truncated": len(fetched) > limit,
    }


def cells(file: str, ref: str, sheet: str = "") -> dict:
    """The cells of an A1 reference ("B5", "A1:D10", or "Sheet 2!A1:C3") as
    rows of {ref, value, formula}. Rows are the sheet's own numbering, header
    included — the way the formulas themselves address cells."""
    book = _book(file)
    if "!" in ref:
        sheet, ref = ref.rsplit("!", 1)
        sheet = sheet.strip("'").replace("''", "'")
    s = book.sheet(sheet)
    min_col, min_row, max_col, max_row = range_boundaries(ref.replace("$", "").upper())
    if min_row is None or min_col is None:
        raise ValueError(f"{ref!r} is not a bounded A1 range")
    if (max_row - min_row + 1) * (max_col - min_col + 1) > _CELLS_MAX:
        raise ValueError(f"{ref!r} spans more than {_CELLS_MAX} cells; use query for bulk reads")
    rows = []
    for row in range(min_row, max_row + 1):
        rows.append([
            {"ref": f"{get_column_letter(col)}{row}",
             "value": _jsonify(s.value(row - 1, col - 1)),
             "formula": s.formulas.get((row - 1, col - 1))}
            for col in range(min_col, max_col + 1)])
    return {"sheet": s.name, "ref": ref, "cells": rows}


def _ident(name):
    return '"' + name.replace('"', '""') + '"'


def _sandbox(duckdb, tables):
    """An in-memory DuckDB that sees `tables` and nothing else: no files, no
    network, no extension installs, bounded memory and threads — then locked,
    so the SQL it runs can't turn any of that back on."""
    con = duckdb.connect(":memory:")
    try:
        for name, table in tables.items():
            con.register(name, table)
        con.execute("SET memory_limit='1GB'")
        con.execute("SET threads=2")
        con.execute("SET enable_external_access=false")
        con.execute("SET autoinstall_known_extensions=false")
        con.execute("SET autoload_known_extensions=false")
        con.execute("SET lock_configuration=true")
    except BaseException:
        con.close()
        raise
    return con


def main(file: str, sheet: str = "", offset: int = 0, limit: int = 0, action: str = "page",
         columns: str = "", sql: str = "", ref: str = "") -> dict:
    """/api/run entrypoint: `action` picks one of the tools above; `limit`
    0 means that tool's own default."""
    if action == "page":
        return page(file, sheet, offset, limit or 100)
    if action == "sheets":
        return sheets(file)
    if action == "describe":
        return describe(file, sheet, columns)
    if action == "query":
        return query(file, sql, limit or 1000)
    if action == "cells":
        return cells(file, ref, sheet)
    raise ValueError(f"unknown action {action!r}: one of page, sheets, describe, query, cells")
