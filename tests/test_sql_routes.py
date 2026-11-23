"""The local SQL engine's routes and runner (SPEC §55): `/api/sql/session/...`
through a TestClient, over a real worker process. FUSED_RENDER_HOME is a
tmp dir per test and no extension is loaded (FUSED_RENDER_SQL_EXTENSIONS=""),
so nothing here needs the network."""
import asyncio
import io
import os
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
import pyarrow.ipc  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fused_render.server import create_app  # noqa: E402
from fused_render.server.routers import sql as sql_routes  # noqa: E402
from fused_render.sql import files as sql_files  # noqa: E402
from fused_render.sql import runner  # noqa: E402
from fused_render.udf import store  # noqa: E402

H = {"X-Fused": "1"}
WAIT_S = 60.0
FOREVER = "SELECT count(*) FROM range(1000000000) a, range(1000000000) b WHERE a.range + b.range < 0"
FINITE = "SELECT sum(x.range * y.range) AS s FROM range(20000) x, range(20000) y"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_SQL_EXTENSIONS", "")
    monkeypatch.setattr(runner, "_refused", False)
    runner.shutdown()
    yield home
    runner.shutdown()


@pytest.fixture
def client(home, tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path), lean=True))


@pytest.fixture
def started():
    """wait(session, query_id): returns once the worker has reported that
    statement started."""
    seen = set()
    cond = threading.Condition()

    def listener(sid, qid):
        with cond:
            seen.add((sid, qid))
            cond.notify_all()

    runner.started_listeners.append(listener)

    def wait(sid, qid):
        with cond:
            assert cond.wait_for(lambda: (sid, qid) in seen, timeout=WAIT_S), (sid, qid)

    yield wait
    runner.started_listeners.remove(listener)


def new_session(client, scope=""):
    r = client.post("/api/sql/session", json={"scope": scope}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["session"]


def q(client, sid, sql, query_id=None):
    body = {"sql": sql}
    if query_id is not None:
        body["query_id"] = query_id
    return client.post(f"/api/sql/session/{sid}/query", json=body, headers=H)


def table_of(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/vnd.apache.arrow.stream")
    return pa.ipc.open_stream(io.BytesIO(response.content)).read_all()


def in_background(fn):
    out = []
    t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    t.start()

    def result():
        t.join(WAIT_S)
        assert not t.is_alive(), "the request never answered"
        return out[0]

    return result


def arrow_bytes(table):
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return sink.getvalue()


# ------------------------------------------------------------------ the guard


def test_every_route_refuses_a_request_without_x_fused(client):
    sid = new_session(client)
    s = f"/api/sql/session/{sid}"
    calls = [
        ("post", "/api/sql/session", {"json": {}}),
        ("post", f"{s}/query", {"json": {"sql": "SELECT 1"}}),
        ("post", f"{s}/cancel", {"json": {"query_id": "x"}}),
        ("post", f"{s}/keepalive", {}),
        ("put", f"{s}/file/a.csv", {"content": b"a\n1\n"}),
        ("post", f"{s}/file/a.csv/link", {"json": {"handle": "udf:" + "0" * 64}}),
        ("get", f"{s}/file/a.csv", {}),
        ("delete", f"{s}/file/a.csv", {}),
        ("post", f"{s}/arrow/t?create=1", {"content": b""}),
        ("delete", s, {}),
    ]
    for method, url, kw in calls:
        r = getattr(client, method)(url, **kw)
        assert r.status_code == 403, (method, url, r.status_code)
    # Nothing above reached the engine: the session is still there and no
    # file was written.
    assert q(client, sid, "SELECT 1").status_code == 200
    assert not os.path.exists(os.path.join(runner.session_files_dir(sid), "a.csv"))


# ------------------------------------------------------------------- queries


def test_a_query_round_trips_as_an_arrow_stream_and_its_spool_is_removed(client):
    sid = new_session(client)
    assert q(client, sid, "CREATE TABLE t AS SELECT range AS x FROM range(4)").status_code == 200
    table = table_of(q(client, sid, "SELECT x, 'v' || x AS s FROM t ORDER BY x"))
    assert table.column("x").to_pylist() == [0, 1, 2, 3]
    assert table.column("s").to_pylist() == ["v0", "v1", "v2", "v3"]
    io_dir = runner.session_io_dir(sid)
    assert [n for n in os.listdir(io_dir) if n.startswith("out-")] == []


def test_a_statement_without_rows_answers_a_valid_stream(client):
    sid = new_session(client)
    table_of(q(client, sid, "SET VARIABLE v = 3"))
    assert table_of(q(client, sid, "SELECT getvariable('v') AS v")).column("v").to_pylist() == [3]


def test_an_sql_error_is_a_400_engine_error(client):
    sid = new_session(client)
    r = q(client, sid, "SELECT * FROM no_such_table")
    assert r.status_code == 400
    assert r.json()["code"] == "engine_error" and "no_such_table" in r.json()["message"]


def test_bad_arguments_and_unknown_sessions(client):
    sid = new_session(client)
    assert q(client, sid, "").json()["code"] == "invalid_args"
    assert q(client, sid, "SELECT 1", query_id="has space").json()["code"] == "invalid_args"
    r = client.post(f"/api/sql/session/{sid}/query", content=b"[1]", headers=H)
    assert r.status_code == 400 and r.json()["code"] == "invalid_args"
    r = client.post("/api/sql/session", json={"scope": "x" * 257}, headers=H)
    assert r.status_code == 400 and r.json()["code"] == "invalid_args"
    for bad in ("0" * 32, "not-a-session", "A" * 32):
        r = q(client, bad, "SELECT 1")
        assert r.status_code == 404 and r.json()["code"] == "not_found", bad
    r = client.post(f"/api/sql/session/{'0' * 32}/cancel", json={"query_id": "a"}, headers=H)
    assert r.status_code == 404


def test_a_query_id_already_running_in_the_session_is_refused(client, started):
    sid = new_session(client)
    first = in_background(lambda: q(client, sid, FOREVER, query_id="same"))
    started(sid, "same")
    r = q(client, sid, "SELECT 1", query_id="same")
    assert r.status_code == 400 and r.json()["code"] == "invalid_args"
    client.post(f"/api/sql/session/{sid}/cancel", json={"query_id": "same"}, headers=H)
    assert first().json()["code"] == "cancelled"


# -------------------------------------------------------------------- cancel


def test_cancel_stops_the_running_query_and_the_session_answers_the_next(client, started):
    sid = new_session(client)
    pending = in_background(lambda: q(client, sid, FOREVER, query_id="long"))
    started(sid, "long")
    r = client.post(f"/api/sql/session/{sid}/cancel", json={"query_id": "long"}, headers=H)
    assert r.status_code == 200 and r.json() == {}
    r = pending()
    assert r.status_code == 400 and r.json()["code"] == "cancelled"
    assert table_of(q(client, sid, "SELECT 5 AS v")).column("v").to_pylist() == [5]


def test_a_cancel_before_the_worker_exists_cancels_the_query_when_it_comes(client):
    sid = new_session(client)
    assert runner.worker_pid() is None
    client.post(f"/api/sql/session/{sid}/cancel", json={"query_id": "early"}, headers=H)
    r = q(client, sid, "CREATE TABLE never AS SELECT 1", query_id="early")
    assert r.status_code == 400 and r.json()["code"] == "cancelled"
    r = q(client, sid, "SELECT count(*) AS n FROM duckdb_tables() WHERE table_name = 'never'")
    assert table_of(r).column("n").to_pylist() == [0]


def test_a_cancel_before_the_query_with_a_running_worker_cancels_it_too(client):
    sid = new_session(client)
    assert q(client, sid, "SELECT 1").status_code == 200
    client.post(f"/api/sql/session/{sid}/cancel", json={"query_id": "early"}, headers=H)
    r = q(client, sid, "SELECT 1", query_id="early")
    assert r.status_code == 400 and r.json()["code"] == "cancelled"


def test_one_sessions_cancel_never_reaches_another_sessions_query(client, started):
    a = new_session(client)
    b = new_session(client)
    pending = in_background(lambda: q(client, b, FINITE, query_id="shared-id"))
    started(b, "shared-id")
    r = client.post(f"/api/sql/session/{a}/cancel", json={"query_id": "shared-id"}, headers=H)
    assert r.status_code == 200
    assert table_of(pending()).num_rows == 1


def test_run_until_disconnected_cancels_when_the_caller_leaves(monkeypatch):
    monkeypatch.setattr(sql_routes, "_DISCONNECT_POLL_S", 0.01)
    cancelled = []

    class Gone:
        async def is_disconnected(self):
            return True

    async def go():
        release = asyncio.Event()

        async def work():
            await release.wait()
            return "spool"

        def on_disconnect():
            cancelled.append(True)
            release.set()

        return await sql_routes.run_until_disconnected(Gone(), work(), on_disconnect)

    assert asyncio.run(go()) == (True, "spool")
    assert cancelled == [True]


# ------------------------------------------------------------- shared state


def test_tables_are_shared_between_sessions_and_temp_tables_are_not(client):
    a = new_session(client)
    b = new_session(client)
    assert q(client, a, "CREATE TABLE shared AS SELECT 1 AS v").status_code == 200
    assert q(client, a, "CREATE TEMP TABLE own AS SELECT 1 AS v").status_code == 200
    assert table_of(q(client, b, "SELECT v FROM shared")).column("v").to_pylist() == [1]
    r = q(client, b, "SELECT v FROM own")
    assert r.status_code == 400 and r.json()["code"] == "engine_error"


def test_deleting_a_session_closes_only_that_session(client, started):
    a = new_session(client)
    b = new_session(client)
    assert q(client, a, "CREATE TABLE kept AS SELECT 1 AS v").status_code == 200
    pending = in_background(lambda: q(client, a, FOREVER, query_id="long"))
    started(a, "long")
    assert client.delete(f"/api/sql/session/{a}", headers=H).json() == {}
    assert pending().json()["code"] == "cancelled"
    assert q(client, a, "SELECT 1").status_code == 404
    assert client.delete(f"/api/sql/session/{a}", headers=H).status_code == 404
    assert table_of(q(client, b, "SELECT v FROM kept")).column("v").to_pylist() == [1]


# --------------------------------------------------------------------- files


def test_a_file_put_is_queried_read_back_and_deleted(client):
    sid = new_session(client)
    base = f"/api/sql/session/{sid}/file"
    csv = b"a,b\n1,2\n3,4\n"
    assert client.put(f"{base}/data.csv", content=csv, headers=H).json() == {}
    assert table_of(q(client, sid, "SELECT sum(b) AS s FROM 'data.csv'")).column("s").to_pylist() == [6]
    r = client.get(f"{base}/data.csv", headers=H)
    assert r.status_code == 200 and r.content == csv
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"].startswith("sandbox")
    # Replacing it replaces the name, never writes the old file in place.
    old = os.path.join(runner.session_files_dir(sid), "data.csv")
    old_ino = os.stat(old).st_ino
    assert client.put(f"{base}/data.csv", content=b"a,b\n5,6\n", headers=H).status_code == 200
    if os.name != "nt":
        assert os.stat(old).st_ino != old_ino
    assert table_of(q(client, sid, "SELECT sum(b) AS s FROM 'data.csv'")).column("s").to_pylist() == [6]
    assert client.delete(f"{base}/data.csv", headers=H).json() == {}
    assert client.get(f"{base}/data.csv", headers=H).status_code == 404
    assert client.delete(f"{base}/data.csv", headers=H).json()["code"] == "not_found"


def test_a_file_sql_writes_can_be_read_back(client):
    sid = new_session(client)
    assert q(client, sid, "COPY (SELECT 9 AS v) TO 'out.parquet'").status_code == 200
    r = client.get(f"/api/sql/session/{sid}/file/out.parquet", headers=H)
    assert r.status_code == 200
    assert pq.read_table(io.BytesIO(r.content)).column("v").to_pylist() == [9]
    # Another session sees the same files.
    other = new_session(client)
    assert client.get(f"/api/sql/session/{other}/file/out.parquet", headers=H).status_code == 200


@pytest.mark.parametrize("name", ["%2E%2E", "%2E", "a%2Fb", "x" * 129, "a%20b", "%2E%2E%2Fx"])
def test_bad_file_names_are_refused_and_nothing_is_written(client, name):
    sid = new_session(client)
    files_dir = runner.session_files_dir(sid)
    before = sorted(os.listdir(os.path.dirname(files_dir)))
    r = client.put(f"/api/sql/session/{sid}/file/{name}", content=b"x", headers=H)
    assert r.status_code == 400 and r.json()["code"] == "invalid_args", (name, r.status_code, r.text)
    for method in ("get", "delete"):
        r = getattr(client, method)(f"/api/sql/session/{sid}/file/{name}", headers=H)
        assert r.status_code == 400, (method, name)
    assert os.listdir(files_dir) == []
    assert sorted(os.listdir(os.path.dirname(files_dir))) == before


def test_name_validation():
    for good in ("a", "a.csv", "A-b_c.parquet", "..a", "x" * 128):
        assert sql_files.check_name(good) == good
    for bad in ("", ".", "..", "a/b", "a\\b", "x" * 129, "a b", "ä", "a\0b"):
        with pytest.raises(sql_files.BadName):
            sql_files.check_name(bad)


# ---------------------------------------------------------------------- links


def _store_entry(key, data):
    _token, path = store.new_body_path()
    with open(path, "wb") as fh:
        fh.write(data)
    store.keep(path, {"content_type": "text/csv", "size": len(data)}, key)
    body, _meta = store.open_result(key)
    return body


def test_a_linked_result_keeps_its_bytes_after_the_store_replaces_and_drops_it(client):
    key = "ab" * 32
    original = b"a\n1\n2\n"
    body = _store_entry(key, original)
    sid = new_session(client)
    r = client.post(f"/api/sql/session/{sid}/file/linked.csv/link",
                    json={"handle": f"udf:{key}"}, headers=H)
    assert r.status_code == 200, r.text
    linked = os.path.join(runner.session_files_dir(sid), "linked.csv")
    if os.name != "nt":
        assert os.stat(linked).st_ino == os.stat(body).st_ino
    assert table_of(q(client, sid, "SELECT sum(a) AS s FROM 'linked.csv'")).column("s").to_pylist() == [3]
    # SQL cannot write the store's bytes through the name.
    if os.name != "nt" and os.geteuid() != 0:
        assert q(client, sid, "COPY (SELECT 99 AS a) TO 'linked.csv' (HEADER, USE_TMP_FILE false)").status_code == 400
    _store_entry(key, b"a\n100\n")
    os.remove(store.open_result(key)[0])
    r = client.get(f"/api/sql/session/{sid}/file/linked.csv", headers=H)
    assert r.status_code == 200 and r.content == original
    assert table_of(q(client, sid, "SELECT sum(a) AS s FROM 'linked.csv'")).column("s").to_pylist() == [3]


def test_link_errors(client):
    sid = new_session(client)
    url = f"/api/sql/session/{sid}/file/x.csv/link"
    r = client.post(url, json={"handle": "udf:" + "cd" * 32}, headers=H)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    for bad in ("udf:", "udf:ABC", "nope:abc", "udf:abc/../x", 5, None):
        r = client.post(url, json={"handle": bad}, headers=H)
        assert r.status_code == 400 and r.json()["code"] == "invalid_args", bad
    r = client.post(url, json={"handle": "idx:abc"}, headers=H)
    assert r.status_code == 501 and r.json()["code"] == "unsupported"
    assert os.listdir(runner.session_files_dir(sid)) == []


# ---------------------------------------------------------------------- arrow


def test_an_arrow_stream_creates_and_appends_to_a_table(client):
    sid = new_session(client)
    data = arrow_bytes(pa.table({"n": [1, 2], "s": ["a", "b"]}))
    url = f"/api/sql/session/{sid}/arrow/items"
    assert client.post(f"{url}?create=1", content=data, headers=H).json() == {}
    assert client.post(f"{url}?create=0", content=data, headers=H).json() == {}
    assert table_of(q(client, sid, "SELECT sum(n) AS t FROM items")).column("t").to_pylist() == [6]
    assert client.post(f"{url}?create=1", content=data, headers=H).status_code == 200
    assert table_of(q(client, sid, "SELECT sum(n) AS t FROM items")).column("t").to_pylist() == [3]
    assert client.post(f"/api/sql/session/{sid}/arrow/1bad?create=1", content=data,
                       headers=H).json()["code"] == "invalid_args"
    assert client.post(f"{url}?create=2", content=data, headers=H).json()["code"] == "invalid_args"
    r = client.post(f"{url}?create=1", content=b"not arrow", headers=H)
    assert r.status_code == 400 and r.json()["code"] == "engine_error"
    assert [n for n in os.listdir(runner.session_io_dir(sid)) if n.startswith("in-")] == []


# ---------------------------------------------------------------- confinement


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_text('{outside}')",
    "SELECT * FROM read_csv('{outside}')",
    "SELECT * FROM read_text('../outside.txt')",
    "SELECT * FROM read_text('{etc}')",
    "COPY (SELECT 1) TO '{home}/written.csv'",
    "ATTACH '{home}/attached.db'",
    "SELECT * FROM read_text('http://127.0.0.1:{port}/x')",
    "SET enable_external_access=true",
    "SET allowed_directories=['{home}']",
])
def test_sql_cannot_reach_outside_files_from_any_session(client, home, sql):
    outside = home / "outside.txt"
    home.mkdir(parents=True, exist_ok=True)
    outside.write_text("secret")
    etc = os.path.join(os.path.abspath(os.sep), "etc", "passwd") if os.name != "nt" else str(outside)
    stmt = sql.format(outside=outside.resolve(), home=home.resolve(), etc=etc, port=9)
    sessions = (new_session(client), new_session(client))
    before = set(os.listdir(home))
    for sid in sessions:
        r = q(client, sid, stmt)
        assert r.status_code == 400 and r.json()["code"] == "engine_error", (stmt, r.text)
    assert set(os.listdir(home)) == before


def test_reading_inside_files_by_absolute_path_works(client):
    sid = new_session(client)
    client.put(f"/api/sql/session/{sid}/file/in.csv", content=b"a\n4\n", headers=H)
    path = os.path.join(os.path.realpath(runner.session_files_dir(sid)), "in.csv")
    assert table_of(q(client, sid, f"SELECT a FROM read_csv('{path}')")).column("a").to_pylist() == [4]


# ------------------------------------------------------------------ lifetime


def test_a_worker_crash_fails_the_query_and_forgets_every_session(client, started):
    sid = new_session(client)
    pending = in_background(lambda: q(client, sid, FOREVER, query_id="long"))
    started(sid, "long")
    proc = runner._ENGINES[""].proc
    old_dir = runner.engine_dir()
    assert proc.pid != os.getpid()
    proc.kill()
    r = pending()
    assert r.status_code == 400 and r.json()["code"] == "engine_error"
    # Published after the cleanup: once the query has failed, the session
    # is unknown and the directory is gone.
    assert q(client, sid, "SELECT 1").status_code == 404
    assert not os.path.exists(old_dir)
    assert not os.path.exists(old_dir + ".lock")
    fresh = new_session(client)
    assert table_of(q(client, fresh, "SELECT 1 AS v")).column("v").to_pylist() == [1]
    assert runner._ENGINES[""].proc is not proc
    assert runner.engine_dir() != old_dir


def test_shutdown_leaves_no_worker_and_no_directory(client):
    sid = new_session(client)
    assert q(client, sid, "SELECT 1").status_code == 200
    proc = runner._ENGINES[""].proc
    engine_path = runner.engine_dir()
    runner.shutdown()
    assert proc.wait(WAIT_S) is not None
    assert not os.path.exists(engine_path)
    assert q(client, sid, "SELECT 1").status_code == 404


def test_shutdown_fails_a_waiting_query_with_engine_error(client, started):
    sid = new_session(client)
    pending = in_background(lambda: q(client, sid, FOREVER, query_id="long"))
    started(sid, "long")
    runner.shutdown()
    r = pending()
    assert r.status_code == 400 and r.json()["code"] == "engine_error"


def test_refuse_new_refuses_sessions_and_workers(client):
    sid = new_session(client)
    runner.refuse_new()
    r = client.post("/api/sql/session", json={}, headers=H)
    assert r.status_code == 503 and r.json()["code"] == "engine_error"
    # The engine exists but its worker never started: it is not started now.
    r = q(client, sid, "SELECT 1")
    assert r.status_code == 503 and r.json()["code"] == "engine_error"
    assert runner.worker_pid() is None


def test_without_duckdb_a_session_is_unsupported(client, monkeypatch):
    monkeypatch.setattr(runner, "available", lambda: False)
    r = client.post("/api/sql/session", json={}, headers=H)
    assert r.status_code == 501 and r.json()["code"] == "unsupported"


def test_the_idle_reaper_ends_an_untouched_engine_only(client, started):
    sid = new_session(client)
    assert q(client, sid, "SELECT 1").status_code == 200
    proc = runner._ENGINES[""].proc
    engine_path = runner.engine_dir()
    assert runner.reap_once() is False
    # Something waiting keeps it, however old.
    pending = in_background(lambda: q(client, sid, FOREVER, query_id="long"))
    started(sid, "long")
    assert runner.reap_once(now=time.monotonic() + runner.IDLE_S + 60) is False
    client.post(f"/api/sql/session/{sid}/cancel", json={"query_id": "long"}, headers=H)
    assert pending().json()["code"] == "cancelled"
    assert runner.reap_once(now=time.monotonic() + runner.IDLE_S + 60) is True
    assert proc.wait(WAIT_S) is not None
    assert not os.path.exists(engine_path)
    assert q(client, sid, "SELECT 1").status_code == 404


LOCK_HOLDER = """
import sys
from fused_render import _platform
fh = open(sys.argv[1], "a+")
assert _platform.flock(fh, blocking=False)
print("locked", flush=True)
sys.stdin.read()
"""


def test_the_sweep_removes_dead_owners_and_keeps_live_ones(client, home):
    root = runner.root()
    os.makedirs(root, exist_ok=True)

    def fake_instance(name, lock=True):
        os.makedirs(os.path.join(root, name, "files"))
        if lock:
            open(os.path.join(root, name + ".lock"), "w").close()

    fake_instance("1-dead")
    fake_instance("2-nolock", lock=False)
    fake_instance("3-live")
    own = new_session(client)
    own_dir = runner.engine_dir()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, os.path.join(root, "3-live.lock")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=repo)
    try:
        assert holder.stdout.readline().strip() == "locked"
        removed = runner.sweep()
        assert sorted(removed) == ["1-dead", "2-nolock"]
        assert not os.path.exists(os.path.join(root, "1-dead"))
        assert not os.path.exists(os.path.join(root, "1-dead.lock"))
        assert not os.path.exists(os.path.join(root, "2-nolock"))
        assert os.path.isdir(os.path.join(root, "3-live"))
        assert os.path.isdir(own_dir)
    finally:
        holder.stdin.close()
        holder.wait(WAIT_S)
    # Its owner gone, the live one's directory goes too.
    assert runner.sweep() == ["3-live"]
    assert os.path.isdir(own_dir)
    assert q(client, own, "SELECT 1").status_code == 200
# ------------------------------------------------------------- engine scopes


def test_sessions_of_one_scope_share_an_engine_and_scopes_are_apart(client):
    a1 = new_session(client, "a")
    a2 = new_session(client, "a")
    b = new_session(client, "b")
    default = new_session(client)
    assert q(client, a1, "CREATE TABLE shared AS SELECT 1 AS v").status_code == 200
    assert table_of(q(client, a2, "SELECT v FROM shared")).column("v").to_pylist() == [
        1
    ]
    for other in (b, default):
        r = q(client, other, "SELECT v FROM shared")
        assert r.status_code == 400 and r.json()["code"] == "engine_error"
    # One worker, one directory per scope; the empty scope is the default engine.
    pids = {runner.worker_pid(s) for s in ("a", "b", "")}
    assert None not in pids and len(pids) == 3
    dirs = {runner.engine_dir(s) for s in ("a", "b", "")}
    assert len(dirs) == 3 and all(os.path.isdir(d) for d in dirs)
    assert runner.engine_dir("never") is None and runner.worker_pid("never") is None
    assert runner.session_files_dir(a1) == runner.session_files_dir(a2)
    assert runner.session_files_dir(a1) != runner.session_files_dir(b)
    # A file put in one scope is not in another.
    assert (
        client.put(
            f"/api/sql/session/{a1}/file/f.csv", content=b"a\n1\n", headers=H
        ).status_code
        == 200
    )
    assert client.get(f"/api/sql/session/{a2}/file/f.csv", headers=H).status_code == 200
    assert client.get(f"/api/sql/session/{b}/file/f.csv", headers=H).status_code == 404
    # No scope given = the default engine.
    r = client.post("/api/sql/session", json={}, headers=H)
    assert runner._ENGINES[""].sessions.keys() >= {default, r.json()["session"]}


def test_a_crash_ends_only_its_scopes_engine(client, started):
    a = new_session(client, "a")
    b = new_session(client, "b")
    assert q(client, b, "CREATE TABLE kept AS SELECT 7 AS v").status_code == 200
    pending = in_background(lambda: q(client, a, FOREVER, query_id="long"))
    started(a, "long")
    dir_a = runner.engine_dir("a")
    runner._ENGINES["a"].proc.kill()
    assert pending().json()["code"] == "engine_error"
    assert q(client, a, "SELECT 1").status_code == 404
    assert not os.path.exists(dir_a) and not os.path.exists(dir_a + ".lock")
    assert table_of(q(client, b, "SELECT v FROM kept")).column("v").to_pylist() == [7]
    again = new_session(client, "a")
    assert table_of(q(client, again, "SELECT 1 AS v")).column("v").to_pylist() == [1]


def test_the_reaper_ends_each_engine_on_its_own_clock_and_keepalive_resets_it(client):
    a = new_session(client, "a")
    b = new_session(client, "b")
    for sid in (a, b):
        assert q(client, sid, "SELECT 1").status_code == 200
    proc_a = runner._ENGINES["a"].proc
    dir_a, dir_b = runner.engine_dir("a"), runner.engine_dir("b")
    old = time.monotonic() - runner.IDLE_S - 60
    runner._ENGINES["a"].last_used = old
    runner._ENGINES["b"].last_used = old
    r = client.post(f"/api/sql/session/{b}/keepalive", headers=H)
    assert r.status_code == 200 and r.json() == {}
    assert runner.reap_once() is True
    assert proc_a.wait(WAIT_S) is not None
    assert not os.path.exists(dir_a) and os.path.isdir(dir_b)
    assert q(client, a, "SELECT 1").status_code == 404
    assert q(client, b, "SELECT 1").status_code == 200
    assert runner.reap_once() is False


def test_keepalive_of_an_unknown_session_is_404(client):
    for bad in ("0" * 32, "not-a-session"):
        r = client.post(f"/api/sql/session/{bad}/keepalive", headers=H)
        assert r.status_code == 404 and r.json()["code"] == "not_found"
    sid = new_session(client)
    assert client.delete(f"/api/sql/session/{sid}", headers=H).status_code == 200
    assert (
        client.post(f"/api/sql/session/{sid}/keepalive", headers=H).status_code == 404
    )


def test_the_number_of_live_engines_is_bounded(client, monkeypatch):
    monkeypatch.setattr(runner, "MAX_ENGINES", 2)
    new_session(client, "a")
    new_session(client, "b")
    r = client.post("/api/sql/session", json={"scope": "c"}, headers=H)
    assert r.status_code == 503 and r.json()["code"] == "engine_error"
    assert "too many" in r.json()["message"]
    assert runner.engine_dir("c") is None
    # An existing scope still takes sessions, and a freed slot is reusable.
    new_session(client, "a")
    runner.reap_once(now=time.monotonic() + runner.IDLE_S + 60)
    assert (
        client.post("/api/sql/session", json={"scope": "c"}, headers=H).status_code
        == 200
    )


def test_shutdown_and_refuse_new_cover_every_engine_and_sweep_spares_them(client):
    a = new_session(client, "a")
    b = new_session(client)
    for sid in (a, b):
        assert q(client, sid, "SELECT 1").status_code == 200
    procs = [runner._ENGINES[s].proc for s in ("a", "")]
    dirs = [runner.engine_dir(s) for s in ("a", "")]
    assert runner.sweep() == []
    assert all(os.path.isdir(d) for d in dirs)
    runner.refuse_new()
    assert (
        client.post("/api/sql/session", json={"scope": "a"}, headers=H).status_code
        == 503
    )
    assert (
        client.post("/api/sql/session", json={"scope": "z"}, headers=H).status_code
        == 503
    )
    runner.shutdown()
    for proc in procs:
        assert proc.wait(WAIT_S) is not None
    assert not any(os.path.exists(d) for d in dirs)
    assert (
        q(client, a, "SELECT 1").status_code == 404
        and q(client, b, "SELECT 1").status_code == 404
    )


# -------------------------------------------------------------- size ceilings


def test_an_upload_past_the_ceiling_is_413_too_large_and_leaves_nothing(
    client, monkeypatch
):
    monkeypatch.setenv("FUSED_RENDER_SQL_MAX_UPLOAD_BYTES", "16")
    sid = new_session(client)
    files_dir, io_dir = runner.session_files_dir(sid), runner.session_io_dir(sid)
    url = f"/api/sql/session/{sid}/file/big.bin"
    r = client.put(url, content=b"x" * 17, headers=H)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    # A body with no declared length is cut off as it streams.
    r = client.put(url, content=iter([b"x" * 10, b"x" * 10]), headers=H)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert os.listdir(files_dir) == [] and os.listdir(io_dir) == []
    assert client.put(url, content=b"x" * 16, headers=H).status_code == 200
    assert os.path.getsize(os.path.join(files_dir, "big.bin")) == 16
    # A refused replacement keeps what was there.
    assert client.put(url, content=b"y" * 17, headers=H).status_code == 413
    assert os.path.getsize(os.path.join(files_dir, "big.bin")) == 16
    assert os.listdir(io_dir) == []


def test_an_arrow_upload_past_the_ceiling_is_413_too_large(client, monkeypatch):
    sid = new_session(client)
    data = arrow_bytes(pa.table({"n": list(range(100))}))
    monkeypatch.setenv("FUSED_RENDER_SQL_MAX_UPLOAD_BYTES", str(len(data) - 1))
    url = f"/api/sql/session/{sid}/arrow/items?create=1"
    r = client.post(url, content=data, headers=H)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    r = client.post(url, content=iter([data[:50], data[50:]]), headers=H)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert os.listdir(runner.session_io_dir(sid)) == []
    r = q(
        client,
        sid,
        "SELECT count(*) AS n FROM duckdb_tables() WHERE table_name = 'items'",
    )
    assert table_of(r).column("n").to_pylist() == [0]
    monkeypatch.setenv("FUSED_RENDER_SQL_MAX_UPLOAD_BYTES", str(len(data)))
    assert client.post(url, content=data, headers=H).status_code == 200


def test_a_result_past_the_ceiling_is_413_too_large_and_its_spool_is_removed(
    client, monkeypatch
):
    sid = new_session(client)
    monkeypatch.setenv("FUSED_RENDER_SQL_MAX_RESULT_BYTES", "2000")
    r = q(client, sid, "SELECT range AS v FROM range(100000)")
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert os.listdir(runner.session_io_dir(sid)) == []
    # The session and the engine go on, and a small result still passes.
    assert table_of(q(client, sid, "SELECT 1 AS v")).column("v").to_pylist() == [1]
    monkeypatch.delenv("FUSED_RENDER_SQL_MAX_RESULT_BYTES")
    assert (
        table_of(q(client, sid, "SELECT range AS v FROM range(100000)")).num_rows
        == 100000
    )


def test_the_limits_default_to_1_gib_and_256_mib_and_ignore_nonsense(monkeypatch):
    for name in (
        "FUSED_RENDER_SQL_MAX_UPLOAD_BYTES",
        "FUSED_RENDER_SQL_MAX_RESULT_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)
    assert runner.max_upload_bytes() == 1 << 30
    assert runner.max_result_bytes() == 256 << 20
    monkeypatch.setenv("FUSED_RENDER_SQL_MAX_UPLOAD_BYTES", "junk")
    monkeypatch.setenv("FUSED_RENDER_SQL_MAX_RESULT_BYTES", "-5")
    assert runner.max_upload_bytes() == 1 << 30
    assert runner.max_result_bytes() == 256 << 20
    monkeypatch.setenv("FUSED_RENDER_SQL_MAX_UPLOAD_BYTES", "123")
    assert runner.max_upload_bytes() == 123


# -------------------------------------------------------------------- logging


def test_nothing_at_info_or_above_names_the_sql_or_a_file(client, caplog):
    import logging

    caplog.set_level(logging.INFO)
    sid = new_session(client)
    marker = "marker_zq_table"
    assert (
        client.put(
            f"/api/sql/session/{sid}/file/{marker}.csv", content=b"a\n1\n", headers=H
        ).status_code
        == 200
    )
    assert (
        q(
            client, sid, f"CREATE TABLE {marker} AS SELECT a FROM '{marker}.csv'"
        ).status_code
        == 200
    )
    assert q(client, sid, f"SELECT * FROM no_such_{marker}").status_code == 400
    runner.shutdown()
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("fused_render"))
    assert marker not in ours and "/file/" in ours


@pytest.mark.parametrize(
    "version, ok",
    [("1.1.3", False), ("1.2.0", True), ("1.5.6", True), ("1.10.0", True),
     ("2.0.0.dev12", True), ("0.10.2", False), ("garbage", False)],
)
def test_the_engine_needs_a_duckdb_that_has_the_lockdown(monkeypatch, version, ok):
    from importlib import metadata

    monkeypatch.setattr(metadata, "version", lambda name: version)
    assert runner.available() is ok


def test_an_older_duckdb_is_unsupported_not_a_failed_first_query(client, monkeypatch):
    from importlib import metadata

    monkeypatch.setattr(metadata, "version", lambda name: "1.1.3")
    r = client.post("/api/sql/session", json={}, headers=H)
    assert r.status_code == 501 and r.json()["code"] == "unsupported"
    assert runner.worker_pid() is None
