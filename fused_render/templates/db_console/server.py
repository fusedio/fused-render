"""Persistent SQL-console daemon backing db_console/template.html.

The console talks to real databases (Postgres, MySQL, MSSQL, DuckDB, SQLite,
plus optional Snowflake/BigQuery) over SQLAlchemy. Those drivers don't live in
the fused-render interpreter, so — like the netcdf grid daemon — this file has
two lives: `main(action="ensure")` runs in the server's Python and only uses
the stdlib to spawn/reuse a long-lived localhost daemon, and `--serve` runs
under an interpreter with SQLAlchemy — the current one when it already imports,
else a uv venv (keyed by the dep-set hash) provisioned with the pure-python
drivers. Every data endpoint requires the per-daemon `?t=<TOKEN>`; the daemon
binds 127.0.0.1 only.

Connections are opaque: the browser only ever sees a `conn_id` (a sha256 of the
normalized URL) and a password-hidden display URL. File-backed databases
(SQLite/DuckDB opened via `{file}`) default to READ-ONLY; a UI "allow writes"
toggle first clears the RO-3 filesystem gate. Remote databases run SQL verbatim
— that's the product — and are simply labelled.
"""
# /// script
# dependencies = ["sqlalchemy>=2", "pg8000", "pymysql", "python-tds", "sqlalchemy-pytds", "duckdb", "duckdb_engine"]
# ///

import hashlib
import json
import os
import sys
import threading
import time

DAEMON_DEPS = ["sqlalchemy>=2", "pg8000", "pymysql", "python-tds",
               "sqlalchemy-pytds", "duckdb", "duckdb_engine"]

# venv keyed by the dep set, in a stable cache outside the FUSED_DBCONSOLE_HOME
# override so tests that redirect the state dir still reuse a provisioned venv.
_CACHE_ROOT = os.path.expanduser("~/.cache/fused-render-dbconsole")
DAEMON_VENV = os.path.join(
    _CACHE_ROOT, "venv-" + hashlib.sha256(",".join(DAEMON_DEPS).encode()).hexdigest()[:8])


def _home():
    return os.environ.get("FUSED_DBCONSOLE_HOME") or _CACHE_ROOT


def _state_path():
    return os.path.join(_home(), "daemon.json")


DAEMON_IDLE_EXIT_S = 30 * 60
ENGINE_IDLE_S = 10 * 60
QUERY_TIMEOUT_S = 60
PAGE_CAP = 1000
CELL_CAP = 4096
CURSORS_PER_CONN = 4
CURSOR_IDLE_S = 5 * 60

# Default pure-python driver per backend, injected when a pasted URL names none
# (a bare postgresql:// would otherwise reach for psycopg2, which isn't baked).
DEFAULT_DRIVER = {"postgresql": "pg8000", "mysql": "pymysql", "mssql": "pytds"}

# Optional dialects that are NOT baked; a missing one degrades to an install
# hint (never a hard failure) and the UI's install button targets these.
OPTIONAL_DRIVERS = {
    "snowflake": ("snowflake-sqlalchemy", "Snowflake needs `snowflake-sqlalchemy`."),
    "bigquery": ("sqlalchemy-bigquery", "BigQuery needs `sqlalchemy-bigquery`."),
}

# pip package behind each baked backend's driver, for the missing-driver hint.
BACKEND_PACKAGE = {"postgresql": "pg8000", "mysql": "pymysql",
                   "mssql": "sqlalchemy-pytds", "duckdb": "duckdb_engine"}


def _me():
    if "__file__" in globals():
        return os.path.abspath(__file__)
    return os.path.join(os.path.abspath(sys.path[0]), "server.py")


def _no_window_kwargs():
    """subprocess flags so a uv/daemon spawn never flashes a console window when
    the parent has no console (packaged app, pythonw). No-op off Windows."""
    import subprocess
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _venv_python(venv):
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        p = os.path.join(venv, *rel)
        if os.path.exists(p):
            return p
    return None


def _daemon_python():
    # Reuse the running interpreter when SQLAlchemy is already importable
    # (dev/test envs); normalize pythonw -> python so the version string matches
    # across the ensuring parent and the windowless daemon.
    import importlib.util
    if importlib.util.find_spec("sqlalchemy") is not None:
        exe = sys.executable
        if os.name == "nt" and os.path.basename(exe).lower() == "pythonw.exe":
            exe = os.path.join(os.path.dirname(exe), "python.exe")
        return exe
    vp = _venv_python(DAEMON_VENV)
    if vp:
        return vp
    import shutil
    import subprocess
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    if os.path.exists(uv):
        try:
            os.makedirs(os.path.dirname(DAEMON_VENV), exist_ok=True)
            nw = _no_window_kwargs()
            subprocess.run([uv, "venv", "--python", "3.12", DAEMON_VENV],
                           check=True, capture_output=True, timeout=180, **nw)
            target = _venv_python(DAEMON_VENV)
            subprocess.run([uv, "pip", "install", "-p", target] + DAEMON_DEPS,
                           check=True, capture_output=True, timeout=600, **nw)
            return target
        except Exception:
            import shutil as _sh
            _sh.rmtree(DAEMON_VENV, ignore_errors=True)
    return sys.executable


def _version():
    try:
        h = hashlib.sha256(open(_me(), "rb").read()).hexdigest()[:12]
    except OSError:
        h = "0"
    return h + "|" + _daemon_python()


def _alive(port, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version
    except Exception:
        return False


# ------------------------------------------------------------ descriptors
def _sqlite_url(path, readonly=True):
    import urllib.request
    if not readonly:
        return "sqlite:///" + os.path.abspath(path)
    uri = urllib.request.pathname2url(os.path.abspath(path))
    return f"sqlite:///file:{uri}?mode=ro&uri=true"


def _duckdb_url(path):
    return "duckdb:///" + os.path.abspath(path)


def _resolve_relative_db(url, base_dir):
    """Make a relative sqlite/duckdb file path in `url` absolute against the
    descriptor's directory, so a checked-in descriptor is portable. Other URLs
    (and already-absolute paths) pass through untouched."""
    import urllib.request
    for scheme, uri_form in (("sqlite:///file:", True), ("sqlite:///", False),
                             ("duckdb:///", False)):
        if not url.startswith(scheme):
            continue
        rest = url[len(scheme):]
        tail = ""
        if uri_form and "?" in rest:
            rest, tail = rest.split("?", 1)
            rest = urllib.request.url2pathname(rest)
            tail = "?" + tail
        elif "?" in rest:
            rest, tail = rest.split("?", 1)
            tail = "?" + tail
        if rest in (":memory:", "") or os.path.isabs(rest):
            return url
        ap = os.path.abspath(os.path.join(base_dir, rest))
        enc = urllib.request.pathname2url(ap) if uri_form else ap
        return scheme + enc + tail
    return url


def _load_dbconn(path):
    """Parse a `.dbconn` JSON descriptor. `url_env` names an env var the daemon
    reads so a checked-in descriptor stays credential-free; it wins over `url`."""
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    url = ""
    if spec.get("url_env"):
        url = os.environ.get(spec["url_env"], "")
    if not url:
        url = spec.get("url", "")
    if not url:
        raise ValueError(f"{os.path.basename(path)}: no url (or url_env is unset)")
    url = _resolve_relative_db(url, os.path.dirname(os.path.abspath(path)))
    return {"url": url, "name": spec.get("name") or os.path.basename(path),
            "options": spec.get("options") or {}, "file_backed": False}


def _resolve_file(path, readonly=True):
    """Resolve a `_file` (a `.dbconn`, or a `.sqlite`/`.duckdb` path) to a
    connection descriptor. File-backed databases are read-only by default."""
    ap = os.path.abspath(os.path.expanduser(path))
    low = ap.lower()
    if low.endswith(".dbconn"):
        return _load_dbconn(ap)
    if low.endswith((".sqlite", ".sqlite3", ".db")):
        return {"url": _sqlite_url(ap, readonly), "name": os.path.basename(ap),
                "options": {}, "file_backed": True, "file": ap}
    if low.endswith((".duckdb", ".ddb")):
        return {"url": _duckdb_url(ap), "name": os.path.basename(ap),
                "options": {}, "file_backed": True, "file": ap}
    raise ValueError(f"unsupported connection descriptor: {path}")


def main(action: str = "ensure", file: str = "", readonly: bool = True):
    if action == "resolve":
        return _resolve_file(file, readonly)

    import subprocess
    version = _version()
    state = _state_path()
    try:
        with open(state) as f:
            st = json.load(f)
        if _alive(st.get("port"), version):
            return {"port": st["port"], "token": st.get("token"), "reused": True}
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit?t={st.get('token', '')}",
                timeout=1).read()
        except Exception:
            pass
    except (OSError, ValueError):
        pass

    os.makedirs(_home(), exist_ok=True)
    log = os.path.join(_home(), "daemon.log")
    # Launch the daemon windowless: a console-subsystem python.exe spawned
    # DETACHED_PROCESS gets a fresh console window on Windows (the popup), so run
    # the venv's pythonw.exe (GUI subsystem, no console) with the winopen.py combo.
    exe = _daemon_python()
    if os.name == "nt":
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
    detach = ({"creationflags": subprocess.DETACHED_PROCESS
               | subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
              if os.name == "nt" else {"start_new_session": True})
    with open(log, "ab") as lf:
        subprocess.Popen([exe, _me(), "--serve"],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                         cwd=os.path.dirname(_me()), **detach)
    # generous: a first start imports SQLAlchemy cold from a freshly
    # provisioned venv; a reused daemon answers /ping within a tick or two.
    for _ in range(2400):
        time.sleep(0.05)
        try:
            with open(state) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), version):
                return {"port": st["port"], "token": st.get("token"), "reused": False}
        except (OSError, ValueError):
            continue
    return {"error": f"db-console daemon did not start — see {log}"}


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass


# ================================================================ helpers
# (importable by tests without pulling in the HTTP server)
def _normalize_url(url_str):
    from sqlalchemy.engine import make_url
    u = make_url(url_str)
    backend = u.get_backend_name()
    if "+" not in u.drivername and backend in DEFAULT_DRIVER:
        u = u.set(drivername=f"{backend}+{DEFAULT_DRIVER[backend]}")
    return u


def _conn_id(url_str):
    u = _normalize_url(url_str)
    return hashlib.sha256(u.render_as_string(hide_password=False).encode()).hexdigest()


def _display_url(url_str):
    return _normalize_url(url_str).render_as_string(hide_password=True)


def _missing_driver_payload(backend, detail=""):
    """The install-hint payload a connect returns when a backend's driver isn't
    importable. HTTP 200, never a hard failure."""
    pkg, hint = OPTIONAL_DRIVERS.get(
        backend, (BACKEND_PACKAGE.get(backend, backend), f"Install the `{backend}` driver."))
    return {"available": False, "dialect": backend, "missing": pkg,
            "hint": hint, "detail": detail}


def _jsonify(value):
    import datetime
    import decimal
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


def _cell(value):
    """Serialize a cell, truncating an oversized string with a marker the grid
    resolves through /cell."""
    out = _jsonify(value)
    if isinstance(out, str) and len(out) > CELL_CAP:
        return {"_trunc": len(out), "v": out[:CELL_CAP]}
    return out


def _type_name(v):
    import datetime
    import decimal
    if v is None:
        return ""
    if isinstance(v, bool):
        return "BOOLEAN"
    if isinstance(v, int):
        return "INTEGER"
    if isinstance(v, float):
        return "DOUBLE"
    if isinstance(v, (bytes, bytearray)):
        return "BLOB"
    if isinstance(v, decimal.Decimal):
        return "DECIMAL"
    if isinstance(v, datetime.datetime):
        return "TIMESTAMP"
    if isinstance(v, datetime.date):
        return "DATE"
    return "TEXT"


_COMPARE_OPS = {"=", "!=", ">", "<", ">=", "<="}
_NULL_OPS = {"is_null": "IS NULL", "not_null": "IS NOT NULL"}
_LIKE_OPS = {"contains": "%{}%", "starts": "{}%"}


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_where(preparer, filters, columns):
    """(sql, binds) for a browse-mode WHERE clause. Identifiers are quoted via
    the dialect's IdentifierPreparer; values are always bound. An unknown column
    or op is dropped, so a hostile filter can neither error nor inject SQL."""
    from sqlalchemy import bindparam
    clauses, binds = [], []
    for i, f in enumerate(filters or []):
        col, op = f.get("column"), f.get("op")
        if col not in columns:
            continue
        q = preparer.quote_identifier(col)
        key = f"f{i}"
        if op in _NULL_OPS:
            clauses.append(f"{q} {_NULL_OPS[op]}")
        elif op in _COMPARE_OPS:
            clauses.append(f"{q} {op} :{key}")
            binds.append(bindparam(key, f.get("value")))
        elif op in _LIKE_OPS:
            clauses.append(f"CAST({q} AS VARCHAR) LIKE :{key} ESCAPE '\\'")
            binds.append(bindparam(key, _LIKE_OPS[op].format(
                _like_escape(str(f.get("value") or "")))))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", binds


def _build_order(preparer, sort, columns):
    if not sort:
        return ""
    col = sort.get("column")
    direction = str(sort.get("dir", "")).lower()
    if col not in columns or direction not in ("asc", "desc"):
        return ""
    return f" ORDER BY {preparer.quote_identifier(col)} {direction.upper()}"


# ================================================================ daemon
def _serve():
    import secrets
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    import sqlalchemy as sa
    from sqlalchemy.exc import NoSuchModuleError

    TOKEN = secrets.token_urlsafe(32)
    VERSION = _version()
    last_hit = [time.time()]

    engines = {}          # conn_id -> engine record
    eng_lock = threading.Lock()
    schema_cache = {}     # conn_id -> schema dict
    cursors = {}          # (conn_id, query_id) -> open server-side cursor record
    running = {}          # (conn_id, query_id) -> raw connection of an in-flight query
    cur_lock = threading.Lock()

    def _build_engine(url_obj, options):
        return sa.create_engine(url_obj, pool_size=1, max_overflow=1,
                                pool_pre_ping=True, connect_args=dict(options or {}))

    def _get_engine(conn_id):
        with eng_lock:
            rec = engines.get(conn_id)
            if rec is None:
                return None
            if rec["engine"] is None:
                rec["engine"] = _build_engine(rec["url_obj"], rec["connect_args"])
            rec["last_used"] = time.time()
            return rec

    def _server_version(engine):
        vi = getattr(engine.dialect, "server_version_info", None)
        return ".".join(str(x) for x in vi) if vi else ""

    def do_connect(body):
        raw_url = body.get("url")
        file = body.get("file")
        allow_write = bool(body.get("allow_write"))
        options = {}
        file_path = None
        file_backed = False
        name = None
        if file:
            readonly_req = not allow_write
            desc = _resolve_file(file, readonly=readonly_req)
            raw_url = desc["url"]
            options = desc.get("options", {})
            name = desc.get("name")
            file_backed = desc.get("file_backed", False)
            file_path = desc.get("file")
        if not raw_url:
            return {"error": "no url or file given", "type": "bad_request"}

        u = _normalize_url(raw_url)
        backend = u.get_backend_name()
        conn_id = _conn_id(raw_url)
        readonly = False
        if file_backed:
            writable = bool(allow_write) and file_path and os.access(file_path, os.W_OK)
            readonly = not writable
            if backend == "duckdb":
                options = dict(options, read_only=readonly)

        try:
            engine = _build_engine(u, options)
            with engine.connect() as c:
                server_version = _server_version(engine)
                if backend == "postgresql":
                    server_version = str(c.exec_driver_sql("SHOW server_version").scalar() or server_version)
        except (ModuleNotFoundError, NoSuchModuleError) as e:
            return _missing_driver_payload(backend, str(e))
        except Exception as e:
            return {"error": str(e), "type": "connect_error", "dialect": backend}

        display = u.render_as_string(hide_password=True)
        with eng_lock:
            engines[conn_id] = {
                "url_obj": u, "connect_args": options, "display_url": display,
                "dialect": backend, "readonly": readonly, "file": file_path,
                "name": name or (u.database or backend), "engine": engine,
                "server_version": server_version, "last_used": time.time()}
        schema_cache.pop(conn_id, None)
        return {"conn_id": conn_id, "dialect": backend, "display_url": display,
                "server_version": server_version, "readonly": readonly,
                "name": name or (u.database or backend)}

    def do_conns():
        with eng_lock:
            out = [{"conn_id": cid, "display_url": r["display_url"],
                    "dialect": r["dialect"], "name": r["name"],
                    "readonly": r["readonly"]} for cid, r in engines.items()]
        return {"conns": out}

    def _estimate_rows(engine, backend, schema, table):
        try:
            with engine.connect() as c:
                if backend == "postgresql":
                    q = ("SELECT reltuples::bigint FROM pg_class c "
                         "JOIN pg_namespace n ON n.oid=c.relnamespace "
                         "WHERE c.relname=:t AND n.nspname=:s")
                    v = c.execute(sa.text(q), {"t": table, "s": schema or "public"}).scalar()
                    return int(v) if v is not None and v >= 0 else None
                if backend in ("sqlite", "duckdb"):
                    p = engine.dialect.identifier_preparer
                    rel = (p.quote_schema(schema) + "." if schema else "") + p.quote(table)
                    return int(c.exec_driver_sql(f"SELECT COUNT(*) FROM {rel}").scalar())
        except Exception:
            return None
        return None

    def do_schema(conn_id, refresh):
        rec = _get_engine(conn_id)
        if rec is None:
            return {"error": "unknown connection", "type": "no_conn"}
        if not refresh and conn_id in schema_cache:
            return schema_cache[conn_id]
        engine = rec["engine"]
        backend = rec["dialect"]
        insp = sa.inspect(engine)
        try:
            schemas = insp.get_schema_names()
        except Exception:
            schemas = []
        default_schema = insp.default_schema_name
        if default_schema and default_schema in schemas:
            schemas = [default_schema] + [s for s in schemas if s != default_schema]
        out = []
        for sch in (schemas or [None]):
            relations = []
            try:
                tables = [(t, "table") for t in insp.get_table_names(schema=sch)]
                views = [(v, "view") for v in insp.get_view_names(schema=sch)]
            except Exception:
                tables, views = [], []
            for name, kind in tables + views:
                cols = []
                pk = set()
                try:
                    pk = set(insp.get_pk_constraint(name, schema=sch).get("constrained_columns") or [])
                    for c in insp.get_columns(name, schema=sch):
                        cols.append({"name": c["name"], "type": str(c["type"]),
                                     "nullable": bool(c.get("nullable", True)),
                                     "pk": c["name"] in pk})
                except Exception:
                    pass
                rows = _estimate_rows(engine, backend, sch, name) if kind == "table" else None
                relations.append({"name": name, "kind": kind,
                                  "columns": cols, "rows": rows})
            out.append({"schema": sch, "relations": relations})
        result = {"schemas": out, "default_schema": default_schema,
                  "dialect": backend}
        schema_cache[conn_id] = result
        return result

    def _cancel_raw(backend, raw):
        try:
            dbconn = raw.driver_connection
            if backend in ("sqlite", "duckdb") and hasattr(dbconn, "interrupt"):
                dbconn.interrupt()
            elif hasattr(dbconn, "cancel"):
                dbconn.cancel()
            else:
                raw.close()
        except Exception:
            try:
                raw.close()
            except Exception:
                pass

    def _reap_cursors():
        now = time.time()
        with cur_lock:
            dead = [k for k, v in cursors.items() if now - v["last"] > CURSOR_IDLE_S]
            for k in dead:
                _close_cursor(cursors.pop(k))

    def _close_cursor(rec):
        try:
            rec["cur"].close()
        except Exception:
            pass
        try:
            rec["raw"].close()
        except Exception:
            pass

    def _stash_cursor(conn_id, query_id, rec):
        with cur_lock:
            cursors[(conn_id, query_id)] = rec
            owned = [k for k in cursors if k[0] == conn_id]
            if len(owned) > CURSORS_PER_CONN:
                owned.sort(key=lambda k: cursors[k]["last"])
                for k in owned[:-CURSORS_PER_CONN]:
                    _close_cursor(cursors.pop(k))

    def do_query(conn_id, body):
        _reap_cursors()
        rec = _get_engine(conn_id)
        if rec is None:
            return {"error": "unknown connection", "type": "no_conn"}
        sql = (body.get("sql") or "").strip()
        if not sql:
            return {"error": "empty query", "type": "bad_request"}
        limit = max(1, min(int(body.get("limit") or 100), PAGE_CAP))
        timeout_s = max(1, min(int(body.get("timeout_s") or QUERY_TIMEOUT_S), 600))
        query_id = str(body.get("query_id") or secrets.token_hex(8))
        backend = rec["dialect"]

        raw = rec["engine"].raw_connection()
        cur = raw.cursor()
        box = {}

        def work():
            try:
                cur.execute(sql)
                box["desc"] = cur.description
            except BaseException as e:  # noqa: BLE001 - surfaced to the client
                box["err"] = e

        with cur_lock:
            running[(conn_id, query_id)] = raw
        th = threading.Thread(target=work, daemon=True)
        t0 = time.time()
        th.start()
        th.join(timeout_s)
        with cur_lock:
            running.pop((conn_id, query_id), None)

        if th.is_alive():
            _cancel_raw(backend, raw)
            th.join(5)
            try:
                raw.close()
            except Exception:
                pass
            return {"cancelled": True, "type": "timeout",
                    "message": f"cancelled after {timeout_s}s", "query_id": query_id}
        if "err" in box:
            try:
                raw.close()
            except Exception:
                pass
            return {"error": str(box["err"]),
                    "type": box["err"].__class__.__name__, "query_id": query_id}

        duration_ms = int((time.time() - t0) * 1000)
        desc = box.get("desc")
        if not desc:
            rowcount = cur.rowcount
            try:
                raw.commit()
            except Exception:
                pass
            raw.close()
            return {"query_id": query_id, "columns": [], "types": [], "rows": [],
                    "more": False, "rowcount": int(rowcount), "duration_ms": duration_ms}

        columns = [d[0] for d in desc]
        page = cur.fetchmany(limit)
        more = len(page) == limit
        types = _infer_types(columns, page)
        _stash_cursor(conn_id, query_id, {"raw": raw, "cur": cur, "columns": columns,
                                          "last": time.time(), "page": page})
        return {"query_id": query_id, "columns": columns, "types": types,
                "rows": [[_cell(v) for v in row] for row in page], "more": more,
                "rowcount": len(page), "duration_ms": duration_ms}

    def _infer_types(columns, page):
        types = [""] * len(columns)
        for row in page:
            for j, v in enumerate(row):
                if not types[j] and v is not None:
                    types[j] = _type_name(v)
            if all(types):
                break
        return types

    def do_fetch(conn_id, query_id, n):
        _reap_cursors()
        with cur_lock:
            rec = cursors.get((conn_id, query_id))
        if rec is None:
            return {"error": "cursor expired", "type": "no_cursor"}
        page = rec["cur"].fetchmany(max(1, min(int(n or 100), PAGE_CAP)))
        rec["page"] = page
        rec["last"] = time.time()
        more = len(page) == max(1, min(int(n or 100), PAGE_CAP))
        return {"query_id": query_id, "columns": rec["columns"],
                "rows": [[_cell(v) for v in row] for row in page], "more": more,
                "rowcount": len(page)}

    def do_cell(conn_id, query_id, row, col):
        with cur_lock:
            rec = cursors.get((conn_id, query_id))
        if rec is None:
            return {"error": "cursor expired", "type": "no_cursor"}
        page = rec.get("page") or []
        if row < 0 or row >= len(page) or col < 0 or col >= len(page[row]):
            return {"error": "out of range", "type": "bad_request"}
        return {"value": _jsonify(page[row][col])}

    def do_cancel(conn_id, query_id):
        rec = _get_engine(conn_id)
        with cur_lock:
            raw = running.get((conn_id, query_id))
        if rec is not None and raw is not None:
            _cancel_raw(rec["dialect"], raw)
        with cur_lock:
            crec = cursors.pop((conn_id, query_id), None)
        if crec is not None:
            _close_cursor(crec)
        return {"cancelled": True, "query_id": query_id}

    def do_table(conn_id, q):
        rec = _get_engine(conn_id)
        if rec is None:
            return {"error": "unknown connection", "type": "no_conn"}
        engine = rec["engine"]
        schema = q.get("schema", [None])[0] or None
        name = q.get("name", [""])[0]
        offset = max(0, int(q.get("offset", ["0"])[0] or 0))
        limit = max(1, min(int(q.get("limit", ["100"])[0] or 100), PAGE_CAP))
        preparer = engine.dialect.identifier_preparer
        insp = sa.inspect(engine)
        columns = [c["name"] for c in insp.get_columns(name, schema=schema)]
        sort = json.loads(q.get("sort", ["null"])[0] or "null")
        filters = json.loads(q.get("filters", ["[]"])[0] or "[]")
        where, binds = _build_where(preparer, filters, columns)
        order = _build_order(preparer, sort, columns)
        rel = (preparer.quote_schema(schema) + "." if schema else "") + preparer.quote_identifier(name)
        stmt = sa.text(f"SELECT * FROM {rel}{where}{order} LIMIT :_lim OFFSET :_off")
        if binds:
            stmt = stmt.bindparams(*binds)
        stmt = stmt.bindparams(_lim=limit, _off=offset)
        with engine.connect() as c:
            total = c.execute(
                sa.text(f"SELECT COUNT(*) FROM {rel}{where}").bindparams(*binds)
                if binds else sa.text(f"SELECT COUNT(*) FROM {rel}{where}")).scalar()
            cur = c.execute(stmt)
            cols = list(cur.keys())
            rows = cur.fetchall()
        page = [list(r) for r in rows]
        return {"columns": cols, "types": _infer_types(cols, page),
                "rows": [[_cell(v) for v in r] for r in page],
                "total_rows": int(total), "offset": offset, "readonly": rec["readonly"]}

    def do_histogram(conn_id, q):
        rec = _get_engine(conn_id)
        if rec is None:
            return {"error": "unknown connection", "type": "no_conn"}
        backend = rec["dialect"]
        schema = q.get("schema", [None])[0] or None
        name = q.get("name", [""])[0]
        col = q.get("column", [""])[0]
        preparer = rec["engine"].dialect.identifier_preparer
        rel = (preparer.quote_schema(schema) + "." if schema else "") + preparer.quote_identifier(name)
        qc = preparer.quote_identifier(col)
        if backend in ("duckdb", "postgresql"):
            bucket = f"date_trunc('day', {qc})"
        elif backend == "sqlite":
            bucket = f"date({qc})"
        else:
            return {"available": False}
        try:
            with rec["engine"].connect() as c:
                rows = c.execute(sa.text(
                    f"SELECT {bucket} AS b, COUNT(*) AS n FROM {rel} "
                    f"WHERE {qc} IS NOT NULL GROUP BY b ORDER BY b LIMIT 500")).fetchall()
        except Exception as e:
            return {"available": False, "detail": str(e)}
        return {"available": True, "buckets": [
            {"start": _jsonify(r[0]), "count": int(r[1])} for r in rows]}

    def do_install_driver(name):
        if name not in OPTIONAL_DRIVERS:
            return {"ok": False, "error": f"unknown optional driver: {name}"}
        import shutil
        import subprocess
        pkg = OPTIONAL_DRIVERS[name][0]
        uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
        target = _venv_python(DAEMON_VENV) or sys.executable
        r = subprocess.run([uv, "pip", "install", "-p", target, pkg],
                           capture_output=True, text=True, timeout=600,
                           **_no_window_kwargs())
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr[-600:]}
        return {"ok": True, "installed": pkg}

    def _reap_engines():
        now = time.time()
        with eng_lock:
            for rec in engines.values():
                if rec["engine"] is not None and now - rec["last_used"] > ENGINE_IDLE_S:
                    rec["engine"].dispose()
                    rec["engine"] = None

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_OPTIONS(self):
            # CORS preflight for the browser's cross-origin JSON POSTs.
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _guard(self, u, q):
            if u.path == "/ping":
                return True
            if q.get("t", [""])[0] != TOKEN:
                self._send(403, {"error": "forbidden"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._guard(u, q):
                return
            try:
                if u.path == "/ping":
                    out = {"ok": True, "version": VERSION}
                elif u.path == "/quit":
                    self._send(200, {"ok": True})
                    threading.Thread(target=srv.shutdown, daemon=True).start()
                    return
                elif u.path == "/conns":
                    out = do_conns()
                elif u.path == "/schema":
                    out = do_schema(q.get("c", [""])[0], q.get("refresh", ["0"])[0] == "1")
                elif u.path == "/fetch":
                    out = do_fetch(q.get("c", [""])[0], q.get("query_id", [""])[0],
                                   q.get("n", ["100"])[0])
                elif u.path == "/cell":
                    out = do_cell(q.get("c", [""])[0], q.get("query_id", [""])[0],
                                  int(q.get("row", ["0"])[0]), int(q.get("col", ["0"])[0]))
                elif u.path == "/table":
                    out = do_table(q.get("c", [""])[0], q)
                elif u.path == "/histogram":
                    out = do_histogram(q.get("c", [""])[0], q)
                else:
                    self._send(404, {"error": "not found"})
                    return
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, {"error": str(e), "type": e.__class__.__name__})
                return
            self._send(200, out)

        def do_POST(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._guard(u, q):
                return
            try:
                body = self._body()
                if u.path == "/connect":
                    out = do_connect(body)
                elif u.path == "/query":
                    out = do_query(q.get("c", [""])[0], body)
                elif u.path == "/cancel":
                    out = do_cancel(q.get("c", [""])[0], str(body.get("query_id") or
                                    q.get("query_id", [""])[0]))
                elif u.path == "/install_driver":
                    out = do_install_driver(body.get("name", ""))
                else:
                    self._send(404, {"error": "not found"})
                    return
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, {"error": str(e), "type": e.__class__.__name__})
                return
            self._send(200, out)

        def _send(self, code, obj):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    os.makedirs(_home(), exist_ok=True)
    with open(_state_path(), "w") as fh:
        json.dump({"port": port, "token": TOKEN, "pid": os.getpid(),
                   "version": VERSION}, fh)

    def reaper():
        while True:
            time.sleep(60)
            _reap_engines()
            if time.time() - last_hit[0] > DAEMON_IDLE_EXIT_S:
                srv.shutdown()
                return
    threading.Thread(target=reaper, daemon=True).start()
    print(f"db-console daemon on 127.0.0.1:{port} (v{VERSION})", flush=True)
    srv.serve_forever()


if __name__ == "__main__" and "--serve" in sys.argv:
    _serve()
