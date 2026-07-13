"""Data ops for the Tableau viewer.

The browser never sees raw data at scale: every source (.csv/.tsv/.parquet/
.xlsx) is converted once into a typed Parquet cache, schema is inferred there
(dimension vs measure, date detection), and each chart is a single DuckDB
GROUP BY over the cache — shelves in, small aggregated table out. That
mirrors Tableau's own VizQL model: the UI state compiles to a query, the
database aggregates, the client only renders the result.

Actions (dispatched via the `action` param):
  boot           — home + workbooks dir + saved workbooks
  listdir        — directory listing for the in-app file browser
  open_data      — build/reuse the parquet cache, return schema + row count
  query          — shelves + filters -> aggregated records for the chart
  rows           — windowed raw-data preview (with the same filters applied)
  filter_domain  — distinct values (dimension) or min/max (measure/date)
  save_workbook  — write a *.tviz.json workbook file
  save_workbook_as — write to a user-chosen directory + name
  load_workbook  — read a workbook file back
  import_tableau — best-effort convert Tableau files into a workbook or data
                   source: .twb/.twbx (workbook XML, zipped), .tds/.tdsx
                   (datasource XML, zipped). .hyper extracts open directly as
                   data sources (queried through the Tableau Hyper API).
  export         — re-run a query and write csv/parquet into the exports dir
  log            — record a client-side event
"""

import json
import os
import re
import sqlite3
import time

# State lives under the user home dir, never inside the installed template
# package (same layout as pdf_studio): saved workbooks are primary content
# (data/); parquet caches, exports, and the event log are regenerable (cache/).
DATA_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "data", "tableau"))
CACHE_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "cache", "tableau"))
WORKBOOKS = os.path.join(DATA_ROOT, "workbooks")
EXPORTS = os.path.join(CACHE_ROOT, "exports")
SOURCES = os.path.join(CACHE_ROOT, "sources")
DB = os.path.join(CACHE_ROOT, "events.db")

DATA_EXTS = (".csv", ".tsv", ".parquet", ".xlsx", ".hyper")
TABLEAU_EXTS = (".twb", ".twbx", ".tds", ".tdsx")
WORKBOOK_EXT = ".tviz.json"
MAX_CHART_ROWS = 5_000       # aggregated rows sent to the chart
MAX_DOMAIN_VALUES = 300      # distinct values sent to a filter dropdown

AGGS = {"sum": "SUM", "avg": "AVG", "median": "MEDIAN", "min": "MIN", "max": "MAX",
        "count": "COUNT", "countd": "COUNT(DISTINCT"}
GRAINS = ("year", "quarter", "month", "week", "day")


def _db():
    os.makedirs(CACHE_ROOT, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute(
        """CREATE TABLE IF NOT EXISTS events (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             ts REAL NOT NULL,
             kind TEXT NOT NULL,
             detail TEXT NOT NULL DEFAULT ''
           )"""
    )
    return con


def _log(kind, detail):
    con = _db()
    with con:
        con.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
            (time.time(), str(kind), json.dumps(detail) if not isinstance(detail, str) else detail),
        )
    con.close()


def _safe_name(name, default):
    name = re.sub(r"[^\w.\- ()]+", "_", os.path.basename(str(name or default)).strip())
    return name or default


# ---------- duckdb helpers ----------

def _duck(excel_ext=False):
    import duckdb

    con = duckdb.connect()
    if excel_ext:
        con.execute("INSTALL excel; LOAD excel;")
    return con


def _q(s):
    """Quote a string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def _qi(name):
    """Quote an identifier (column names have spaces and dashes)."""
    return '"' + str(name).replace('"', '""') + '"'


def _json_val(v):
    if v is None:
        return None
    if isinstance(v, (int, float, bool, str)):
        return v
    import datetime
    import decimal

    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return str(v)


# ---------- parquet cache + schema ----------

def _cache_dir(file):
    import hashlib

    h = hashlib.sha1(os.path.abspath(file).encode()).hexdigest()[:16]
    return os.path.join(SOURCES, f"{h}-{int(os.path.getmtime(file) * 1000)}")


def _clean_stale(keep_dir):
    if not os.path.isdir(SOURCES):
        return
    prefix = os.path.basename(keep_dir).split("-")[0]
    for n in os.listdir(SOURCES):
        p = os.path.join(SOURCES, n)
        if n.startswith(prefix + "-") and p != keep_dir:
            import shutil

            shutil.rmtree(p, ignore_errors=True)


def _source_sql(file):
    ext = os.path.splitext(file)[1].lower()
    if ext == ".csv":
        return f"SELECT * FROM read_csv({_q(file)}, header=true, sample_size=-1)", False
    if ext == ".tsv":
        return f"SELECT * FROM read_csv({_q(file)}, header=true, delim='\t', sample_size=-1)", False
    if ext == ".parquet":
        return f"SELECT * FROM read_parquet({_q(file)})", False
    if ext == ".xlsx":
        return f"SELECT * FROM read_xlsx({_q(file)}, header=true)", True
    raise ValueError(f"unsupported file type {ext!r}")


def _hyper_to_csv(file, dest):
    """Dump the first table of a .hyper extract to csv (the Hyper API runs
    Tableau's own database engine, so this works for any extract version)."""
    try:
        from tableauhyperapi import Connection, CreateMode, HyperProcess, Telemetry
    except ImportError:
        raise ValueError(
            "Reading .hyper extracts needs the Tableau Hyper API — "
            "run: uv pip install tableauhyperapi"
        )
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp:
        with Connection(hp.endpoint, database=file, create_mode=CreateMode.NONE) as con:
            tables = []
            for schema in con.catalog.get_schema_names():
                tables += con.catalog.get_table_names(schema)
            tables = [t for t in tables if t.name.unescaped != "$TableauMetadata"]
            if not tables:
                raise ValueError("the .hyper extract contains no tables")
            con.execute_command(
                f"COPY (SELECT * FROM {tables[0]}) TO {_q(dest)} WITH (FORMAT CSV, HEADER)"
            )


def _classify(duck_type):
    t = duck_type.upper()
    if t in ("DATE",) or t.startswith("TIMESTAMP"):
        return "date", "dimension"
    if t == "BOOLEAN":
        return "bool", "dimension"
    if t in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT",
             "USMALLINT", "UINTEGER", "UBIGINT", "FLOAT", "DOUBLE") or t.startswith("DECIMAL"):
        return "number", "measure"
    return "string", "dimension"


def _ensure_cache(file):
    """Build (or reuse) the typed parquet cache + schema for a data source."""
    file = os.path.abspath(file)
    d = _cache_dir(file)
    meta_path = os.path.join(d, "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            return json.load(f)
    _clean_stale(d)
    os.makedirs(d, exist_ok=True)
    if file.lower().endswith(".hyper"):
        staged = os.path.join(d, "hyper.csv")
        _hyper_to_csv(file, staged)
        src, needs_excel = _source_sql(staged)
    else:
        src, needs_excel = _source_sql(file)
    con = _duck(excel_ext=needs_excel)
    pq = os.path.join(d, "data.parquet")
    con.execute(f"COPY ({src}) TO {_q(pq)} (FORMAT PARQUET)")
    info = con.execute(f"DESCRIBE SELECT * FROM read_parquet({_q(pq)})").fetchall()
    nrows = con.execute(f"SELECT count(*) FROM read_parquet({_q(pq)})").fetchone()[0]
    fields = []
    for name, duck_type, *_ in info:
        dtype, role = _classify(duck_type)
        fields.append({"name": name, "dtype": dtype, "role": role})
    meta = {"file": file, "mtime": os.path.getmtime(file), "parquet": "data.parquet",
            "nrows": nrows, "fields": fields}
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return meta


def _parquet(meta):
    return os.path.join(_cache_dir(meta["file"]), meta["parquet"])


def _field(meta, name):
    for fld in meta["fields"]:
        if fld["name"] == name:
            return fld
    raise ValueError(f"unknown field {name!r}")


# ---------- query building ----------

def _filters_sql(meta, filters):
    where, params = [], []
    for f in filters or []:
        fld = _field(meta, f["field"])
        col = _qi(fld["name"])
        kind = f.get("type")
        if kind == "in":
            vals = f.get("values") or []
            if not vals:
                continue
            if None in vals or "" in vals:
                vals = [v for v in vals if v not in (None, "")]
                clause = f"({col} IS NULL OR CAST({col} AS VARCHAR) = ''"
                if vals:
                    clause += f" OR CAST({col} AS VARCHAR) IN ({', '.join('?' * len(vals))})"
                    params.extend(str(v) for v in vals)
                where.append(clause + ")")
            else:
                where.append(f"CAST({col} AS VARCHAR) IN ({', '.join('?' * len(vals))})")
                params.extend(str(v) for v in vals)
        elif kind == "range":
            lo, hi = f.get("min"), f.get("max")
            cast = f"CAST({col} AS TIMESTAMP)" if fld["dtype"] == "date" else col
            if lo not in (None, ""):
                where.append(f"{cast} >= ?")
                params.append(lo)
            if hi not in (None, ""):
                where.append(f"{cast} <= ?")
                params.append(hi)
        elif kind == "contains":
            q = str(f.get("q", "")).strip()
            if q:
                esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where.append(f"CAST({col} AS VARCHAR) ILIKE ? ESCAPE '\\'")
                params.append(f"%{esc}%")
    return (" WHERE " + " AND ".join(where)) if where else "", params


def _dim_expr(fld, grain):
    col = _qi(fld["name"])
    if fld["dtype"] == "date" and grain in GRAINS:
        return f"date_trunc('{grain}', {col})"
    return col


def _agg_expr(fld, agg):
    col = _qi(fld["name"])
    if agg == "countd":
        return f"COUNT(DISTINCT {col})"
    if agg == "count":
        return f"COUNT({col})"
    fn = AGGS.get(agg, "SUM")
    return f"{fn}({col})"


def _query(file, spec):
    """spec: {dims: [{field, grain?, as}], measures: [{field, agg, as}],
             filters: [...], sort: {by, dir}?, limit?}"""
    meta = _ensure_cache(file)
    dims = spec.get("dims") or []
    measures = spec.get("measures") or []
    if not dims and not measures:
        return {"records": [], "truncated": False}
    sel, aliases = [], []
    for d in dims:
        fld = _field(meta, d["field"])
        alias = d.get("as") or fld["name"]
        sel.append(f"{_dim_expr(fld, d.get('grain'))} AS {_qi(alias)}")
        aliases.append(alias)
    for m in measures:
        fld = _field(meta, m["field"])
        alias = m.get("as") or fld["name"]
        sel.append(f"{_agg_expr(fld, m.get('agg', 'sum'))} AS {_qi(alias)}")
        aliases.append(alias)
    where, params = _filters_sql(meta, spec.get("filters"))
    sql = f"SELECT {', '.join(sel)} FROM read_parquet({_q(_parquet(meta))}){where}"
    if dims:
        sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims)))
    sort = spec.get("sort") or {}
    if sort.get("by") in aliases:
        sql += f" ORDER BY {_qi(sort['by'])} {'DESC' if sort.get('dir') == 'desc' else 'ASC'} NULLS LAST"
    elif dims:
        sql += " ORDER BY " + ", ".join(str(i + 1) for i in range(len(dims)))
    limit = min(int(spec.get("limit") or MAX_CHART_ROWS), MAX_CHART_ROWS)
    sql += f" LIMIT {limit + 1}"
    con = _duck()
    rows = con.execute(sql, params).fetchall()
    truncated = len(rows) > limit
    records = [
        {alias: _json_val(v) for alias, v in zip(aliases, row)}
        for row in rows[:limit]
    ]
    return {"records": records, "truncated": truncated}


def _rows(file, offset, limit, filters):
    """Raw-data preview window with the sheet's filters applied."""
    meta = _ensure_cache(file)
    where, params = _filters_sql(meta, filters)
    pq = _q(_parquet(meta))
    con = _duck()
    matched = con.execute(f"SELECT count(*) FROM read_parquet({pq}){where}", params).fetchone()[0]
    cur = con.execute(
        f"SELECT * FROM read_parquet({pq}){where} LIMIT ? OFFSET ?",
        params + [int(limit), int(offset)],
    )
    rows = [[_json_val(v) for v in row] for row in cur.fetchall()]
    return {"columns": [f["name"] for f in meta["fields"]], "rows": rows,
            "matched": matched, "total": meta["nrows"]}


def _filter_domain(file, field):
    meta = _ensure_cache(file)
    fld = _field(meta, field)
    col = _qi(fld["name"])
    pq = _q(_parquet(meta))
    con = _duck()
    if fld["role"] == "measure" or fld["dtype"] == "date":
        lo, hi = con.execute(f"SELECT min({col}), max({col}) FROM read_parquet({pq})").fetchone()
        return {"kind": "range", "min": _json_val(lo), "max": _json_val(hi)}
    total = con.execute(f"SELECT count(DISTINCT {col}) FROM read_parquet({pq})").fetchone()[0]
    vals = con.execute(
        f"SELECT CAST({col} AS VARCHAR) AS v, count(*) AS n FROM read_parquet({pq}) "
        f"GROUP BY 1 ORDER BY n DESC, v LIMIT {MAX_DOMAIN_VALUES}"
    ).fetchall()
    return {"kind": "values", "total": total,
            "values": [{"v": "" if v is None else v, "n": n} for v, n in vals]}


# ---------- workbooks ----------

def _save_workbook(file, data):
    file = os.path.abspath(file)
    if not file.endswith(WORKBOOK_EXT):
        raise ValueError(f"workbook files must end with {WORKBOOK_EXT}")
    os.makedirs(os.path.dirname(file), exist_ok=True)
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    _log("save_workbook", {"file": file})
    return {"file": file.replace(os.sep, "/"), "mtime": os.path.getmtime(file)}


def _save_workbook_as(directory, name, data):
    name = _safe_name(name, "workbook")
    name = re.sub(r"(\.tviz)?(\.json)?$", "", name, flags=re.I) + WORKBOOK_EXT
    base = os.path.abspath(os.path.expanduser(directory)) if directory else WORKBOOKS
    return _save_workbook(os.path.join(base, name), data)


def _load_workbook(file):
    file = os.path.abspath(file)
    with open(file, encoding="utf-8") as f:
        data = json.load(f)
    _log("load_workbook", {"file": file})
    return {"file": file.replace(os.sep, "/"), "workbook": data}


# ---------- tableau .twb import ----------

# .twb shelf tokens: [Datasource].[sum:Sales:qk], [Datasource].[yr:Order Date:ok],
# [Datasource].[Region] — an aggregate or date-grain prefix, or a bare field.
_TWB_AGG = {"sum": "sum", "avg": "avg", "mdn": "median", "min": "min", "max": "max",
            "cnt": "count", "ctd": "countd"}
_TWB_GRAIN = {"yr": "year", "qr": "quarter", "mn": "month", "wk": "week", "dy": "day",
              "tyr": "year", "tqr": "quarter", "tmn": "month", "twk": "week", "tdy": "day"}
_TWB_MARK = {"Automatic": "auto", "Bar": "bar", "Line": "line", "Area": "area",
             "Square": "heatmap", "Circle": "scatter", "Shape": "scatter",
             "Pie": "pie", "Text": "table"}
_TWB_FIELD = re.compile(r"\[[^\]]*\]\.\[([^\]]*)\]")


def _twb_pill(token, meta):
    parts = token.split(":")
    name = parts[1] if len(parts) >= 2 else parts[0]
    try:
        fld = _field(meta, name)
    except ValueError:
        return None
    if len(parts) >= 2 and parts[0] in _TWB_AGG:
        return {"field": name, "agg": _TWB_AGG[parts[0]]}
    if len(parts) >= 2 and parts[0] in _TWB_GRAIN:
        return {"field": name, "grain": _TWB_GRAIN[parts[0]]}
    if fld["role"] == "measure":
        return {"field": name, "agg": "sum"}
    if fld["dtype"] == "date":
        return {"field": name, "grain": "year"}
    return {"field": name}


def _twb_data_file(root, twb_dir):
    for conn in root.iter("connection"):
        fn = conn.get("filename") or conn.get("dbname") or ""  # hyper uses dbname
        if not fn.lower().endswith(DATA_EXTS):
            continue
        d = conn.get("directory") or "."
        cands = [fn] if os.path.isabs(fn) else [os.path.join(twb_dir, d, fn),
                                                os.path.join(twb_dir, fn)]
        for cand in cands:
            if os.path.exists(cand):
                return os.path.abspath(cand)
        # zip layouts vary (Data/…/file) — fall back to a name search
        base = os.path.basename(fn)
        for dirpath, _, names in os.walk(twb_dir):
            if base in names:
                return os.path.abspath(os.path.join(dirpath, base))
        raise ValueError(
            f"The workbook's data source “{fn}” was not found next to the file. "
            f"Place the data file in the same folder and reopen."
        )
    raise ValueError("No file-based data source found (only local csv/tsv/xlsx/"
                     "parquet/hyper connections are supported).")


def _import_twb(file):
    import xml.etree.ElementTree as ET

    file = os.path.abspath(file)
    root = ET.parse(file).getroot()
    src = _twb_data_file(root, os.path.dirname(file))
    meta = _ensure_cache(src)

    sheets = []
    for ws in root.iter("worksheet"):
        table = ws.find("table")
        if table is None:
            continue
        pills = lambda text: [p for t in _TWB_FIELD.findall(text or "")
                              if (p := _twb_pill(t, meta))]
        cols = pills(table.findtext("cols"))
        rows = pills(table.findtext("rows"))
        mark = table.find(".//pane/mark")
        chart = _TWB_MARK.get(mark.get("class") if mark is not None else "", "auto")
        color = []
        enc = table.find(".//pane/encodings/color")
        if enc is not None:
            color = pills(enc.get("column", ""))[:1]
        # a text encoding (label) is the sheet's measure when the shelves have none
        if not any("agg" in p for p in cols + rows):
            enc = table.find(".//pane/encodings/text")
            if enc is not None:
                rows += pills(enc.get("column", ""))[:1]
        sheets.append({"name": ws.get("name") or f"Sheet {len(sheets) + 1}",
                       "chart": chart, "cols": cols, "rows": rows, "color": color,
                       "filters": [], "sortDir": ""})
    if not sheets:
        sheets = [{"name": "Sheet 1", "chart": "auto", "cols": [], "rows": [],
                   "color": [], "filters": [], "sortDir": ""}]
    wb = {"version": 1,
          "name": re.sub(r"\.twb$", "", os.path.basename(file), flags=re.I),
          "source": src.replace(os.sep, "/"), "sheets": sheets, "active": 0}
    _log("import_twb", {"file": file, "source": src, "sheets": len(sheets)})
    return {"workbook": wb}


def _extract_tableau_zip(file):
    """Unpack a .twbx/.tdsx into a cached directory (keyed like the data cache)."""
    import zipfile

    d = _cache_dir(file)
    if not os.path.isdir(d) or not os.listdir(d):
        _clean_stale(d)
        os.makedirs(d, exist_ok=True)
        with zipfile.ZipFile(file) as z:
            z.extractall(d)
    return d


def _find_by_ext(directory, ext):
    for dirpath, _, names in os.walk(directory):
        for n in names:
            if n.lower().endswith(ext):
                return os.path.join(dirpath, n)
    return None


def _import_tds(file):
    import xml.etree.ElementTree as ET

    file = os.path.abspath(file)
    root = ET.parse(file).getroot()
    src = _twb_data_file(root, os.path.dirname(file))
    _log("import_tds", {"file": file, "source": src})
    return {"source": src.replace(os.sep, "/")}


def _import_tableau(file):
    file = os.path.abspath(file)
    ext = os.path.splitext(file)[1].lower()
    if ext == ".twb":
        return _import_twb(file)
    if ext == ".tds":
        return _import_tds(file)
    if ext in (".twbx", ".tdsx"):
        d = _extract_tableau_zip(file)
        inner = _find_by_ext(d, ".twb" if ext == ".twbx" else ".tds")
        if not inner:
            raise ValueError(f"no {'.twb' if ext == '.twbx' else '.tds'} found inside {os.path.basename(file)}")
        out = _import_twb(inner) if ext == ".twbx" else _import_tds(inner)
        if "workbook" in out:
            out["workbook"]["name"] = re.sub(r"\.twbx$", "", os.path.basename(file), flags=re.I)
        return out
    raise ValueError(f"unsupported Tableau file type {ext!r}")


# ---------- file browsing / boot ----------

def _listdir(path):
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        path = os.path.dirname(path) or "/"
    # forward slashes on every platform: the browser's crumb/join logic is "/"-based
    parent = (os.path.dirname(path) or path).replace(os.sep, "/")  # dirname(root) == root
    path = path.replace(os.sep, "/")
    dirs, files = [], []
    try:
        names = os.listdir(path)
    except OSError as e:
        return {"error": str(e), "path": path, "parent": parent, "dirs": [], "files": []}
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                dirs.append(name)
            elif name.lower().endswith(WORKBOOK_EXT):
                files.append({"name": name, "size": os.path.getsize(full), "kind": "workbook"})
            elif name.lower().endswith(TABLEAU_EXTS):
                files.append({"name": name, "size": os.path.getsize(full), "kind": "tableau"})
            elif name.lower().endswith(DATA_EXTS):
                files.append({"name": name, "size": os.path.getsize(full), "kind": "data"})
        except OSError:
            continue
    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())
    return {"path": path, "parent": parent, "dirs": dirs, "files": files}


def _boot():
    workbooks = []
    if os.path.isdir(WORKBOOKS):
        for n in sorted(os.listdir(WORKBOOKS)):
            if n.endswith(WORKBOOK_EXT):
                full = os.path.join(WORKBOOKS, n)
                workbooks.append({"file": full.replace(os.sep, "/"),
                                  "name": n[: -len(WORKBOOK_EXT)],
                                  "mtime": os.path.getmtime(full)})
    workbooks.sort(key=lambda w: -w["mtime"])
    return {"home": os.path.expanduser("~").replace(os.sep, "/"),
            "workbooks_dir": WORKBOOKS.replace(os.sep, "/"),
            "workbooks": workbooks}


# ---------- export ----------

def _export(kind, name, file, spec):
    os.makedirs(EXPORTS, exist_ok=True)
    base = _safe_name(name, "export")
    base = re.sub(r"\.(csv|parquet)$", "", base, flags=re.I)
    out = _query(file, spec)
    records = out["records"]
    if not records:
        raise ValueError("nothing to export — the chart has no data")
    cols = list(records[0].keys())
    if kind == "csv":
        import csv

        dest = os.path.join(EXPORTS, base + ".csv")
        with open(dest, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in records:
                w.writerow([r[c] for c in cols])
    elif kind == "parquet":
        con = _duck()
        con.execute("CREATE TABLE t (" + ", ".join(f"{_qi(c)} VARCHAR" for c in cols) + ")")
        con.executemany(
            "INSERT INTO t VALUES (" + ", ".join("?" * len(cols)) + ")",
            [[None if r[c] is None else str(r[c]) for c in cols] for r in records],
        )
        dest = os.path.join(EXPORTS, base + ".parquet")
        con.execute(f"COPY t TO {_q(dest)} (FORMAT PARQUET)")
    else:
        raise ValueError(f"unknown export kind {kind!r}")
    _log("export", {"kind": kind, "dest": dest, "rows": len(records)})
    return {"file": dest.replace(os.sep, "/"), "name": os.path.basename(dest)}


# Bare main() (no @fused.udf): the builtin executor calls main() directly —
# a udf wrapper hides the signature and hangs on hosted auth. The fused
# engine's compat bridge accepts a bare main() too, so this runs under both.
def main(
    action: str = "boot",
    file: str = "",
    data: str = "",
    name: str = "",
    directory: str = "",
    kind: str = "",
    field: str = "",
    spec: str = "",
    filters: str = "",
    detail: str = "",
    offset: int = 0,
    limit: int = 200,
):
    if action == "boot":
        return _boot()
    if action == "listdir":
        return _listdir(file)
    if action == "open_data":
        meta = _ensure_cache(file)
        _log("open_data", {"file": meta["file"], "nrows": meta["nrows"]})
        return {"file": meta["file"].replace(os.sep, "/"), "nrows": meta["nrows"],
                "fields": meta["fields"]}
    if action == "query":
        return _query(file, json.loads(spec))
    if action == "rows":
        return _rows(file, offset, min(limit, 1000), json.loads(filters) if filters else None)
    if action == "filter_domain":
        return _filter_domain(file, field)
    if action == "save_workbook":
        return _save_workbook(file, json.loads(data))
    if action == "save_workbook_as":
        return _save_workbook_as(directory, name, json.loads(data))
    if action == "load_workbook":
        return _load_workbook(file)
    if action == "import_tableau":
        return _import_tableau(file)
    if action == "export":
        return _export(kind, name, file, json.loads(spec))
    if action == "log":
        _log(kind or "event", detail)
        return {"ok": True}
    raise ValueError(f"unknown action {action!r}")
