"""Tests for the db_console template daemon (fused_render/templates/db_console).

Unit tests import server.py directly and touch only pure helpers (no network,
no SQLAlchemy engine). Daemon integration tests spawn the real daemon via
`main(action="ensure")` against LOCAL SQLite/DuckDB fixtures, hit its HTTP
endpoints, and always tear it down with /quit — never leaking a daemon, and
redirecting its state dir into tmp_path via FUSED_DBCONSOLE_HOME.

The daemon provisions its own uv venv (SQLAlchemy + pure-python drivers) on the
first spawn on this machine, so the integration fixture allows a generous
timeout; the venv is cached across runs, so later runs are fast.
"""
import importlib.util
import json
import os
import stat
import sqlite3
import urllib.error
import urllib.request

import pytest

_SERVER = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       "fused_render", "templates", "db_console", "server.py")


def _load_server():
    spec = importlib.util.spec_from_file_location("db_console_server", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server = _load_server()


# ============================================================ unit: pure
def test_jsonify_coercions():
    import datetime
    import decimal
    assert server._jsonify(None) is None
    assert server._jsonify(3) == 3
    assert server._jsonify(b"\x00\xff") == "00ff"
    assert server._jsonify(decimal.Decimal("1.50")) == "1.50"
    assert server._jsonify(datetime.date(2021, 1, 2)) == "2021-01-02"
    nested = server._jsonify({"a": [decimal.Decimal("2"), b"\x01"]})
    assert nested == {"a": ["2", "01"]}


def test_cell_truncation_marker():
    small = server._cell("hello")
    assert small == "hello"
    big = server._cell("x" * (server.CELL_CAP + 50))
    assert isinstance(big, dict) and big["_trunc"] == server.CELL_CAP + 50
    assert len(big["v"]) == server.CELL_CAP


def test_dbconn_parsing_plain(tmp_path):
    p = tmp_path / "c.dbconn"
    p.write_text(json.dumps({"name": "prod", "url": "postgresql://u:secret@h/db"}),
                 encoding="utf-8")
    desc = server._load_dbconn(str(p))
    assert desc["name"] == "prod"
    assert desc["url"] == "postgresql://u:secret@h/db"


def test_dbconn_url_env_wins(tmp_path, monkeypatch):
    p = tmp_path / "c.dbconn"
    p.write_text(json.dumps({"url": "postgresql://fallback/db",
                             "url_env": "MY_DB_URL"}), encoding="utf-8")
    monkeypatch.setenv("MY_DB_URL", "mysql://u:pw@h/live")
    assert server._load_dbconn(str(p))["url"] == "mysql://u:pw@h/live"
    monkeypatch.delenv("MY_DB_URL")
    # url_env unset -> falls back to the checked-in url.
    assert server._load_dbconn(str(p))["url"] == "postgresql://fallback/db"


def test_dbconn_relative_sqlite_is_resolved(tmp_path):
    (tmp_path / "demo.sqlite").write_bytes(b"")
    p = tmp_path / "c.dbconn"
    p.write_text(json.dumps({"url": "sqlite:///./demo.sqlite"}), encoding="utf-8")
    url = server._load_dbconn(str(p))["url"]
    assert os.path.abspath(str(tmp_path / "demo.sqlite")).replace("\\", "/") in url.replace("\\", "/")


def test_resolve_file_synthesizes_urls(tmp_path):
    sq = tmp_path / "a.sqlite"
    sq.write_bytes(b"")
    ro = server._resolve_file(str(sq), readonly=True)
    assert ro["file_backed"] and "mode=ro" in ro["url"]
    rw = server._resolve_file(str(sq), readonly=False)
    assert "mode=ro" not in rw["url"] and rw["url"].startswith("sqlite:///")
    dk = server._resolve_file(str(tmp_path / "a.duckdb"))
    assert dk["url"].startswith("duckdb:///")
    with pytest.raises(ValueError):
        server._resolve_file(str(tmp_path / "a.txt"))


def test_url_normalization_injects_default_driver():
    pytest.importorskip("sqlalchemy")
    u = server._normalize_url("postgresql://u:pw@h:5432/db")
    assert u.drivername == "postgresql+pg8000"
    # an explicit driver is left alone
    assert server._normalize_url("mysql+pymysql://h/db").drivername == "mysql+pymysql"


def test_conn_id_stable_and_credential_free():
    pytest.importorskip("sqlalchemy")
    a = server._conn_id("postgresql://u:pw@h/db")
    b = server._conn_id("postgresql+pg8000://u:pw@h/db")
    assert a == b and len(a) == 64  # normalization makes the two identical
    diff = server._conn_id("postgresql://u:other@h/db")
    assert diff != a  # different credentials -> different id


def test_display_url_hides_password():
    pytest.importorskip("sqlalchemy")
    disp = server._display_url("postgresql://u:supersecret@h/db")
    assert "supersecret" not in disp and "***" in disp


def test_missing_driver_payload():
    p = server._missing_driver_payload("snowflake")
    assert p["available"] is False
    assert p["missing"] == "snowflake-sqlalchemy"
    assert p["dialect"] == "snowflake"
    # an unknown backend still degrades gracefully with a generic hint.
    assert server._missing_driver_payload("weird")["missing"] == "weird"


def test_filter_and_sort_build_quoted_identifiers():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    prep = sqlite_dialect.dialect().identifier_preparer
    cols = ["name", "year"]
    where, binds = server._build_where(prep, [
        {"column": "name", "op": "=", "value": "x"},
        {"column": "year", "op": ">", "value": 2000},
        {"column": "evil; DROP", "op": "=", "value": "1"},   # unknown col -> dropped
        {"column": "name", "op": "bogus", "value": "1"},     # bad op -> dropped
    ], cols)
    assert '"name" = :f0' in where and '"year" > :f1' in where
    assert "evil" not in where and "bogus" not in where
    assert len(binds) == 2

    assert server._build_order(prep, {"column": "year", "dir": "desc"}, cols) == ' ORDER BY "year" DESC'
    assert server._build_order(prep, {"column": "nope", "dir": "asc"}, cols) == ""
    assert server._build_order(prep, {"column": "year", "dir": "sideways"}, cols) == ""


def test_venv_python_cross_platform(tmp_path):
    # Windows layout
    win = tmp_path / "win"
    (win / "Scripts").mkdir(parents=True)
    (win / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    assert server._venv_python(str(win)).endswith(os.path.join("Scripts", "python.exe"))
    # POSIX layout
    posix = tmp_path / "posix"
    (posix / "bin").mkdir(parents=True)
    (posix / "bin" / "python").write_text("", encoding="utf-8")
    assert server._venv_python(str(posix)).endswith(os.path.join("bin", "python"))
    assert server._venv_python(str(tmp_path / "missing")) is None


# ============================================================ integration
def _make_sqlite(path):
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT);"
        "CREATE VIEW artist_names AS SELECT name FROM artists;")
    con.executemany("INSERT INTO artists VALUES (?,?,?)",
                    [(i, f"artist{i}", "US") for i in range(1, 6)])
    con.commit()
    con.close()


class _Client:
    def __init__(self, port, token):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def _url(self, path):
        sep = "&" if "?" in path else "?"
        return f"{self.base}{path}{sep}t={self.token}"

    def get(self, path, with_token=True):
        url = self._url(path) if with_token else f"{self.base}{path}"
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)

    def post(self, path, body, with_token=True):
        url = self._url(path) if with_token else f"{self.base}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    home = tmp_path_factory.mktemp("dbconsole_home")
    os.environ["FUSED_DBCONSOLE_HOME"] = str(home)
    if os.path.exists(server._state_path()):
        os.remove(server._state_path())
    info = server.main(action="ensure")
    assert "error" not in info, info
    client = _Client(info["port"], info["token"])
    yield client
    try:
        client.get("/quit")
    except Exception:
        pass
    os.environ.pop("FUSED_DBCONSOLE_HOME", None)


@pytest.fixture()
def sqlite_conn(tmp_path, daemon):
    path = str(tmp_path / "music.sqlite")
    _make_sqlite(path)
    res = daemon.post("/connect", {"file": path})
    assert res.get("conn_id"), res
    assert res["readonly"] is True and res["dialect"] == "sqlite"
    return daemon, res["conn_id"], path


def test_token_required(daemon, tmp_path):
    path = str(tmp_path / "guard.sqlite")
    _make_sqlite(path)
    res = daemon.post("/connect", {"file": path})
    cid = res["conn_id"]
    with pytest.raises(urllib.error.HTTPError) as exc:
        daemon.get(f"/schema?c={cid}", with_token=False)
    assert exc.value.code == 403
    # /ping needs no token.
    assert daemon.get("/ping", with_token=False)["ok"] is True


def test_schema_lists_tables_and_views(sqlite_conn):
    daemon, cid, _ = sqlite_conn
    data = daemon.get(f"/schema?c={cid}")
    rels = {r["name"]: r for s in data["schemas"] for r in s["relations"]}
    assert "artists" in rels and rels["artists"]["kind"] == "table"
    assert "artist_names" in rels and rels["artist_names"]["kind"] == "view"
    cols = {c["name"]: c for c in rels["artists"]["columns"]}
    assert cols["id"]["pk"] is True and cols["name"]["nullable"] is False
    assert rels["artists"]["rows"] == 5


def test_query_fetch_and_pagination(sqlite_conn):
    daemon, cid, _ = sqlite_conn
    data = daemon.post(f"/query?c={cid}",
                       {"sql": "SELECT id, name FROM artists ORDER BY id", "limit": 2})
    assert data["columns"] == ["id", "name"]
    assert len(data["rows"]) == 2 and data["more"] is True
    assert data["rows"][0][1] == "artist1"
    more = daemon.get(f"/fetch?c={cid}&query_id={data['query_id']}&n=2")
    assert [r[0] for r in more["rows"]] == [3, 4]


def test_query_error_is_typed_not_traceback(sqlite_conn):
    daemon, cid, _ = sqlite_conn
    data = daemon.post(f"/query?c={cid}", {"sql": "SELECT * FROM nope"})
    assert "error" in data and data.get("query_id")
    assert "columns" not in data or not data.get("columns")


def test_cancel_returns_cleanly(sqlite_conn):
    daemon, cid, _ = sqlite_conn
    res = daemon.post(f"/cancel?c={cid}", {"query_id": "does-not-exist"})
    assert res["cancelled"] is True


def test_readonly_gate_on_readonly_file(daemon, tmp_path):
    path = str(tmp_path / "locked.sqlite")
    _make_sqlite(path)
    os.chmod(path, stat.S_IREAD)  # drop write -> os.access(W_OK) is False
    try:
        # even asking for writes, the RO-3 fs gate keeps it read-only.
        res = daemon.post("/connect", {"file": path, "allow_write": True})
        assert res["readonly"] is True
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def test_writable_file_allows_writes(daemon, tmp_path):
    path = str(tmp_path / "rw.sqlite")
    _make_sqlite(path)
    res = daemon.post("/connect", {"file": path, "allow_write": True})
    assert res["readonly"] is False


def test_duckdb_connect_and_query(daemon, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    path = str(tmp_path / "demo.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
    con.execute("INSERT INTO t VALUES (1,'x'), (2,'y')")
    con.close()
    res = daemon.post("/connect", {"file": path})
    assert res["dialect"] == "duckdb" and res["readonly"] is True
    cid = res["conn_id"]
    data = daemon.post(f"/query?c={cid}", {"sql": "SELECT a, b FROM t ORDER BY a"})
    assert data["rows"] == [[1, "x"], [2, "y"]]


def test_dbconn_descriptor_roundtrip(daemon, tmp_path):
    sq = str(tmp_path / "d.sqlite")
    _make_sqlite(sq)
    dbconn = tmp_path / "conn.dbconn"
    dbconn.write_text(json.dumps(
        {"name": "via-descriptor",
         "url": f"sqlite:///file:{sq.replace(os.sep, '/')}?mode=ro&uri=true"}),
        encoding="utf-8")
    res = daemon.post("/connect", {"file": str(dbconn)})
    assert res.get("conn_id"), res
    data = daemon.get(f"/schema?c={res['conn_id']}")
    names = {r["name"] for s in data["schemas"] for r in s["relations"]}
    assert "artists" in names
