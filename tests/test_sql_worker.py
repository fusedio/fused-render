"""The local SQL engine's worker process (`fused_render/sql/_worker.py`, SPEC
§55.5), run for real: the ready handshake, the protocol, per-session order
and cancellation, the lockdown, what can be imported from `files/`, and that
stdin EOF ends the process. No extension is loaded
(FUSED_RENDER_SQL_EXTENSIONS=""), so nothing here needs the network."""
import json
import os
import queue
import subprocess
import sys
import threading

import pytest

pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
import pyarrow.ipc  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAIT_S = 60.0
# Long enough that only an interrupt ends it.
FOREVER = "SELECT count(*) FROM range(1000000000) a, range(1000000000) b WHERE a.range + b.range < 0"


class Worker:
    def __init__(self, tmp_path, env_extra=None, make_dirs=True):
        self.dir = tmp_path / "engine"
        self.files = self.dir / "files"
        self.io = self.dir / "io"
        if make_dirs:
            for d in (self.files, self.dir / "tmp", self.io):
                d.mkdir(parents=True, exist_ok=True)
        else:
            self.dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, FUSED_RENDER_SQL_DIR=str(self.dir),
                   FUSED_RENDER_SQL_EXTENSIONS="",
                   PYTHONPATH=os.pathsep.join(p for p in (REPO, os.environ.get("PYTHONPATH")) if p))
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-P", "-m", "fused_render.sql._worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(self.dir), env=env)
        self.events: queue.Queue = queue.Queue()
        self.by_id: dict[str, list[dict]] = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.events.put(json.loads(line))
        self.events.put(None)

    def next_event(self, pred):
        """The next event `pred` accepts (earlier ones are kept by id)."""
        while True:
            for evs in self.by_id.values():
                for ev in evs:
                    if pred(ev) and not ev.get("_seen"):
                        ev["_seen"] = True
                        return ev
            ev = self.events.get(timeout=WAIT_S)
            assert ev is not None, "the worker exited"
            self.by_id.setdefault(str(ev.get("id")), []).append(ev)

    def ready(self):
        return self.next_event(lambda e: e.get("event") == "ready")

    def started(self, rid):
        return self.next_event(lambda e: e.get("id") == rid and e.get("event") == "started")

    def done(self, rid):
        return self.next_event(lambda e: e.get("id") == rid and e.get("event") == "done")

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def query(self, rid, session, sql):
        out = str(self.io / f"{rid.replace(':', '_')}.arrows")
        self.send({"op": "query", "id": rid, "session": session, "sql": sql, "out": out})
        return out

    def run(self, rid, session, sql):
        out = self.query(rid, session, sql)
        done = self.done(rid)
        return done, out

    def stop(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(30)


@pytest.fixture
def worker(tmp_path):
    w = Worker(tmp_path)
    assert w.ready()["ok"] is True
    yield w
    w.stop()


def read_table(path):
    with open(path, "rb") as fh:
        return pa.ipc.open_stream(fh).read_all()


def test_ready_reports_the_version_and_no_extensions_when_none_are_asked_for(tmp_path):
    w = Worker(tmp_path)
    try:
        ready = w.ready()
        assert ready["ok"] is True
        assert ready["version"]
        assert ready["extensions"] == []
    finally:
        w.stop()


def test_a_directory_that_is_no_engine_directory_answers_ready_false_and_exits(tmp_path):
    w = Worker(tmp_path, make_dirs=False)
    ready = w.ready()
    assert ready["ok"] is False and ready["error"]
    assert w.proc.wait(WAIT_S) != 0


def test_a_query_answers_an_arrow_stream_in_io(worker):
    done, _ = worker.run("s1:a", "s1", "CREATE TABLE t AS SELECT range AS x FROM range(5)")
    assert done["ok"] is True
    done, out = worker.run("s1:b", "s1", "SELECT x, x * 2 AS y FROM t ORDER BY x")
    assert done["ok"] is True and done["size"] == os.path.getsize(out)
    table = read_table(out)
    assert table.column_names == ["x", "y"]
    assert table.column("y").to_pylist() == [0, 2, 4, 6, 8]
    assert not os.path.exists(out + ".part")


def test_a_statement_without_rows_still_answers_a_valid_stream(worker):
    done, out = worker.run("s1:a", "s1", "SET VARIABLE v = 1")
    assert done["ok"] is True
    read_table(out)  # a valid stream, whatever its schema


def test_an_error_while_rows_stream_is_a_clean_engine_error_with_no_spool(worker):
    done, out = worker.run(
        "s1:a", "s1",
        "SELECT CASE WHEN range > 300000 THEN error('boom') ELSE range END FROM range(1000000)")
    assert done["ok"] is False and done["code"] == "engine_error"
    assert "boom" in done["message"]
    assert not os.path.exists(out) and not os.path.exists(out + ".part")


def test_an_insert_reads_an_arrow_stream_into_a_table(worker):
    path = worker.io / "in.arrows"
    table = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    with open(path, "wb") as fh, pa.ipc.new_stream(fh, table.schema) as w:
        w.write_table(table)
    worker.send({"op": "insert", "id": "s1:i1", "session": "s1", "table": 'we"ird',
                 "create": True, "path": str(path)})
    assert worker.done("s1:i1")["ok"] is True
    worker.send({"op": "insert", "id": "s1:i2", "session": "s1", "table": 'we"ird',
                 "create": False, "path": str(path)})
    assert worker.done("s1:i2")["ok"] is True
    done, out = worker.run("s1:q", "s1", 'SELECT count(*) AS n FROM "we""ird"')
    assert read_table(out).column("n").to_pylist() == [6]


def test_cancel_interrupts_the_running_statement_and_the_session_goes_on(worker):
    worker.query("s1:long", "s1", FOREVER)
    worker.started("s1:long")
    worker.send({"op": "cancel", "id": "s1:long"})
    done = worker.done("s1:long")
    assert done["ok"] is False and done["code"] == "cancelled"
    done, out = worker.run("s1:next", "s1", "SELECT 42 AS v")
    assert read_table(out).column("v").to_pylist() == [42]


def test_a_cancel_sent_before_the_statement_answers_cancelled_without_running(worker):
    worker.send({"op": "cancel", "id": "s1:early"})
    done, out = worker.run("s1:early", "s1", "CREATE TABLE should_not_exist AS SELECT 1")
    assert done["code"] == "cancelled"
    assert not any(e.get("event") == "started" for e in worker.by_id.get("s1:early", []))
    done, out = worker.run("s1:check", "s1",
                           "SELECT count(*) AS n FROM duckdb_tables() WHERE table_name = 'should_not_exist'")
    assert read_table(out).column("n").to_pylist() == [0]


def test_a_late_cancel_of_an_ended_id_does_not_cancel_its_reuse(worker):
    done, _ = worker.run("s1:q", "s1", "SELECT 1")
    assert done["ok"] is True
    worker.send({"op": "cancel", "id": "s1:q"})
    done, _ = worker.run("s1:q", "s1", "SELECT 2")
    assert done["ok"] is True


def test_a_queued_statement_can_be_cancelled_behind_a_running_one(worker):
    worker.query("s1:long", "s1", FOREVER)
    worker.started("s1:long")
    worker.query("s1:queued", "s1", "SELECT 1")
    worker.send({"op": "cancel", "id": "s1:queued"})
    worker.send({"op": "cancel", "id": "s1:long"})
    assert worker.done("s1:long")["code"] == "cancelled"
    assert worker.done("s1:queued")["code"] == "cancelled"


def test_cancelling_one_session_leaves_another_sessions_statement_running(worker):
    # B is finite; it must finish on its own while A, started at the same
    # time on another cursor, is interrupted.
    worker.query("a:long", "a", FOREVER)
    worker.query("b:sum", "b",
                 "SELECT sum(x.range * y.range) AS s FROM range(20000) x, range(20000) y")
    worker.started("a:long")
    worker.started("b:sum")
    worker.send({"op": "cancel", "id": "a:long"})
    assert worker.done("a:long")["code"] == "cancelled"
    done = worker.done("b:sum")
    assert done["ok"] is True


def test_sessions_share_tables_but_not_temp_tables(worker):
    assert worker.run("a:1", "a", "CREATE TABLE shared AS SELECT 1 AS v")[0]["ok"]
    assert worker.run("a:2", "a", "CREATE TEMP TABLE mine AS SELECT 1 AS v")[0]["ok"]
    done, out = worker.run("b:1", "b", "SELECT v FROM shared")
    assert read_table(out).column("v").to_pylist() == [1]
    done, _ = worker.run("b:2", "b", "SELECT v FROM mine")
    assert done["code"] == "engine_error"


def test_close_interrupts_the_running_statement_and_cancels_the_queue(worker):
    worker.query("s1:long", "s1", FOREVER)
    worker.started("s1:long")
    worker.query("s1:queued", "s1", "SELECT 1")
    worker.send({"op": "close", "session": "s1"})
    assert worker.done("s1:long")["code"] == "cancelled"
    assert worker.done("s1:queued")["code"] == "cancelled"
    # Another session is untouched.
    assert worker.run("s2:q", "s2", "SELECT 1")[0]["ok"] is True


def test_stdin_eof_while_a_statement_runs_ends_the_process(worker):
    worker.query("s1:long", "s1", FOREVER)
    worker.started("s1:long")
    worker.proc.stdin.close()
    # A liveness bound for the failing case, not a timing claim.
    assert worker.proc.wait(WAIT_S) == 0


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_text('{outside}')",
    "SELECT * FROM read_text('../outside.txt')",
    "SELECT * FROM read_csv('{outside}')",
    "SELECT * FROM glob('{parent}/*')",
    "COPY (SELECT 1) TO '{parent}/written.csv'",
    "COPY (SELECT 1) TO '../written2.csv'",
    "ATTACH '{parent}/attached.db'",
    "EXPORT DATABASE '{parent}/exported'",
    "SELECT * FROM read_text('file://{outside}')",
    "SELECT * FROM read_text('http://127.0.0.1:9/x')",
    "SELECT * FROM read_text('s3://bucket/key')",
    "SELECT * FROM read_text('/vsicurl/http://127.0.0.1:9/x')",
    "SET enable_external_access=true",
    "SET allowed_directories=['/']",
    "SET lock_configuration=false",
    "LOAD 'evil.duckdb_extension'",
])
def test_the_lockdown_refuses_everything_outside_files_in_every_session(worker, sql):
    parent = str(worker.dir.resolve())
    outside = os.path.join(parent, "outside.txt")
    with open(outside, "w") as fh:
        fh.write("secret")
    (worker.files / "evil.duckdb_extension").write_bytes(b"\0" * 64)
    before = set(os.listdir(parent))
    stmt = sql.format(outside=outside, parent=parent)
    # The first session and a second one (another cursor of the same
    # connection) are both confined.
    for i, session in enumerate(("a", "b")):
        done, _ = worker.run(f"{session}:{i}", session, stmt)
        assert done["ok"] is False and done["code"] == "engine_error", done
    assert set(os.listdir(parent)) == before


def test_reading_and_writing_inside_files_works(worker):
    (worker.files / "data.csv").write_text("a,b\n1,2\n3,4\n")
    done, out = worker.run("s:1", "s", "SELECT sum(b) AS s FROM 'data.csv'")
    assert read_table(out).column("s").to_pylist() == [6]
    assert worker.run("s:2", "s", "COPY (SELECT 7 AS v) TO 'out.parquet'")[0]["ok"] is True
    assert (worker.files / "out.parquet").exists()
    done, out = worker.run("t:1", "t", f"SELECT v FROM read_parquet('{worker.files.resolve()}/out.parquet')")
    assert read_table(out).column("v").to_pylist() == [7]


def test_the_secret_manager_works_behind_the_lockdown_and_keeps_nothing_on_disk(worker):
    # A remote read starts DuckDB's secret manager. Started only after the
    # lockdown, it fails on its default folder in the user's home, and so
    # does every remote read from then on; this is that failure without the
    # network.
    done, out = worker.run("s:1", "s", "SELECT count(*) AS n FROM duckdb_secrets()")
    assert done["ok"] is True, done
    assert read_table(out).column("n").to_pylist() == [0]
    assert worker.run("s:2", "s", "CREATE SECRET mem (TYPE http, BEARER_TOKEN 'x')")[0]["ok"] is True
    done, _ = worker.run("s:3", "s", "CREATE PERSISTENT SECRET disk (TYPE http, BEARER_TOKEN 'x')")
    assert done["ok"] is False and done["code"] == "engine_error"
    assert os.listdir(worker.dir / "tmp") == []


def test_python_files_in_files_are_never_imported(worker, tmp_path):
    marker = tmp_path / "imported.marker"
    planted = f"open({str(marker)!r}, 'w').write('imported')\n"
    for name in ("numpy.py", "pandas.py", "pyarrow_hotfix.py", "fsspec.py", "polars.py"):
        (worker.files / name).write_text(planted)
    (worker.files / "data.csv").write_text("a\n1\n")
    # Statements and an insert: the paths on which duckdb and pyarrow import
    # optional modules lazily.
    assert worker.run("s:1", "s", "SELECT * FROM 'data.csv'")[0]["ok"] is True
    path = worker.io / "in.arrows"
    table = pa.table({"a": [1]})
    with open(path, "wb") as fh, pa.ipc.new_stream(fh, table.schema) as w:
        w.write_table(table)
    worker.send({"op": "insert", "id": "s:2", "session": "s", "table": "t",
                 "create": True, "path": str(path)})
    assert worker.done("s:2")["ok"] is True
    assert worker.run("s:3", "s", "SELECT * FROM t")[0]["ok"] is True
    assert not marker.exists()


def _machine_zone():
    import duckdb

    con = duckdb.connect()
    try:
        return con.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    finally:
        con.close()


def test_the_time_zone_is_the_machines_by_default(worker):
    expected = _machine_zone()
    done, out = worker.run("s1:tz", "s1", "SELECT current_setting('TimeZone') AS tz")
    assert done["ok"] is True
    assert read_table(out).column("tz").to_pylist() == [expected]
    # A cursor of another session sees the same (the setting is global).
    done, out = worker.run("s2:tz", "s2", "SELECT current_setting('TimeZone') AS tz")
    assert read_table(out).column("tz").to_pylist() == [expected]


def test_an_empty_time_zone_variable_leaves_the_machines_zone(tmp_path):
    w = Worker(tmp_path / "none", env_extra={"FUSED_RENDER_SQL_TIMEZONE": ""})
    try:
        assert w.ready()["ok"] is True
        done, out = w.run("s1:tz", "s1", "SELECT current_setting('TimeZone') AS tz")
        assert read_table(out).column("tz").to_pylist() == [_machine_zone()]
    finally:
        w.stop()


def test_the_time_zone_variable_pins_the_zone(tmp_path):
    w = Worker(
        tmp_path / "ny", env_extra={"FUSED_RENDER_SQL_TIMEZONE": "America/New_York"}
    )
    try:
        assert w.ready()["ok"] is True
        done, out = w.run("s1:tz", "s1", "SELECT current_setting('TimeZone') AS tz")
        assert read_table(out).column("tz").to_pylist() == ["America/New_York"]
    finally:
        w.stop()


def test_a_result_past_max_bytes_answers_too_large_and_leaves_no_spool(worker):
    out = str(worker.io / "big.arrows")
    worker.send(
        {
            "op": "query",
            "id": "s1:big",
            "session": "s1",
            "out": out,
            "max_bytes": 2000,
            "sql": "SELECT range AS v FROM range(1000000)",
        }
    )
    done = worker.done("s1:big")
    assert done["ok"] is False and done["code"] == "too_large"
    assert not os.path.exists(out) and not os.path.exists(out + ".part")
    # The session goes on; a result within the limit passes.
    out = str(worker.io / "small.arrows")
    worker.send(
        {
            "op": "query",
            "id": "s1:small",
            "session": "s1",
            "out": out,
            "max_bytes": 1 << 20,
            "sql": "SELECT 1 AS v",
        }
    )
    assert worker.done("s1:small")["ok"] is True
    assert read_table(out).column("v").to_pylist() == [1]
