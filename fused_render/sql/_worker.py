"""The local SQL engine's worker process (SPEC §55.5).

Started by runner.py as ``[sys.executable, -P, -m, fused_render.sql._worker]``
with ``FUSED_RENDER_SQL_DIR=<instance dir>`` and its working directory set to
that instance directory. One in-memory DuckDB database for the process; a
session is one cursor of it, owned by one thread that runs the session's
statements in order.

Startup, in this order:
  1. the protocol's stdout is taken (a dup of fd 1), and fd 1 is pointed at
     stderr, so nothing a library prints from C reaches the protocol;
  2. duckdb and pyarrow are imported, then every relative or working-
     directory entry is removed from `sys.path` and the process changes into
     `files/` (a file registered as `numpy.py` must never be importable);
  3. `duckdb.connect(":memory:")`, the extensions in FUSED_RENDER_SQL_EXTENSIONS
     (comma-separated; unset = httpfs,spatial,h3; empty = none) loaded
     best-effort, then settings (including `TimeZone` when
     FUSED_RENDER_SQL_TIMEZONE is set and non-empty; otherwise the engine
     keeps its own default, the machine's zone), then the lockdown:
     `allowed_directories=[files, tmp, https://]` ->
     `enable_external_access=false` -> `lock_configuration=true`;
  4. a self-check: a file in the instance directory, outside `files/` and
     `tmp/`, must not be readable. If any lockdown step fails or the file is
     readable, the worker answers `ready` with `ok: false` and exits.

Protocol: JSON lines. Bodies never travel on the pipe; they are files in
`io/`.

  -> {"event": "ready", "ok": true, "version", "extensions"}
     | {"event": "ready", "ok": false, "error"}
  {"op": "query", "id", "session", "sql", "out", "max_bytes"?}
  {"op": "insert", "id", "session", "table", "create", "path"}
      -> {"id", "event": "started"}       the statement is about to run
      -> {"id", "event": "done", "ok": true, "size"}
         (`out` holds an Arrow IPC stream; written to `out.part`, then renamed)
      or {"id", "event": "done", "ok": false,
          "code": "engine_error" | "cancelled" | "too_large", "message"}
         (`too_large`: the stream passed `max_bytes`; writing stopped and
         `out` was removed)
  {"op": "cancel", "id"}       interrupts `id` while it runs; an id not yet
                               started answers `cancelled` when its turn comes
  {"op": "close", "session"}   interrupts what runs there, pending statements
                               answer `cancelled`, the cursor is closed
  stdin EOF                    everything is interrupted and the process exits
"""
from __future__ import annotations

import collections
import json
import os
import queue
import secrets
import sys
import threading

_BATCH_ROWS = 100_000
_DEFAULT_EXTENSIONS = "httpfs,spatial,h3"
_COMMUNITY = {"h3"}
_REMEMBERED = 4096
_INTERRUPT_EVERY_S = 0.02

class _TooLarge(Exception):
    """A query's Arrow stream passed the size the server allows."""


_PROTO = None
_SEND_LOCK = threading.Lock()
_LOCK = threading.Lock()
_CON = None
_SESSIONS: dict[str, _Session] = {}
# Cancels for ids not seen yet, and ids that already ended (a late cancel of
# one must not cancel a later statement reusing its id). Both bounded.
_PRE_CANCELLED: collections.OrderedDict[str, None] = collections.OrderedDict()
_ENDED: collections.OrderedDict[str, None] = collections.OrderedDict()


def send(obj: dict) -> None:
    line = json.dumps(obj, default=str)
    with _SEND_LOCK:
        _PROTO.write(line + "\n")
        _PROTO.flush()


def _remember(store: collections.OrderedDict, key: str) -> None:
    store[key] = None
    store.move_to_end(key)
    while len(store) > _REMEMBERED:
        store.popitem(last=False)


def _quote_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.queued: set[str] = set()
        self.cancelled: set[str] = set()
        self.running: str | None = None
        self.closed = False
        self.cursor = _CON.cursor()
        self.thread = threading.Thread(target=self._loop, daemon=True,
                                       name=f"sql-session-{sid[:12]}")
        self.thread.start()

    def interrupt_while_running(self, rid: str) -> None:
        """Interrupt `rid` until it is no longer the running statement. An
        interrupt reaching the cursor before DuckDB has entered the statement
        is dropped, so one is not enough; the check under _LOCK keeps every
        interrupt off the next statement."""
        stop = threading.Event()
        while True:
            with _LOCK:
                if self.running != rid:
                    return
                self.cursor.interrupt()
            stop.wait(_INTERRUPT_EVERY_S)

    def _loop(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                break
            rid = item["id"]
            with _LOCK:
                self.queued.discard(rid)
                skip = self.closed or rid in self.cancelled
                if not skip:
                    self.running = rid
            if skip:
                self._end(rid, {"id": rid, "event": "done", "ok": False,
                                "code": "cancelled", "message": "cancelled"})
                continue
            send({"id": rid, "event": "started"})
            try:
                if item["op"] == "query":
                    size = self._query(item["sql"], item["out"], rid,
                                       item.get("max_bytes"))
                else:
                    size = self._insert(item["table"], bool(item.get("create")),
                                        item["path"], rid)
                done = {"id": rid, "event": "done", "ok": True, "size": size}
            except Exception as exc:  # noqa: BLE001 — every failure is an answer
                with _LOCK:
                    cancelled = rid in self.cancelled or self.closed
                if cancelled:
                    code, message = "cancelled", "cancelled"
                elif isinstance(exc, _TooLarge):
                    code, message = "too_large", str(exc)
                else:
                    code, message = "engine_error", str(exc) or type(exc).__name__
                done = {"id": rid, "event": "done", "ok": False,
                        "code": code, "message": message}
            with _LOCK:
                self.running = None
            self._end(rid, done)
        try:
            self.cursor.close()
        except Exception:  # noqa: BLE001
            pass

    def _end(self, rid: str, done: dict) -> None:
        with _LOCK:
            self.cancelled.discard(rid)
            _remember(_ENDED, rid)
        send(done)

    def _check_cancel(self, rid: str) -> None:
        with _LOCK:
            if rid in self.cancelled or self.closed:
                raise InterruptedError("cancelled")

    def _query(self, sql: str, out: str, rid: str, max_bytes: int | None = None) -> int:
        import pyarrow as pa
        import pyarrow.ipc  # noqa: F401

        part = out + ".part"
        limit = max_bytes if isinstance(max_bytes, int) and max_bytes > 0 else None
        too_large = _TooLarge(f"the result is larger than {limit} bytes")
        try:
            self._check_cancel(rid)
            cur = self.cursor.execute(sql)
            if cur.description is None:
                reader = None
                schema = pa.schema([])
            else:
                reader = (cur.to_arrow_reader(_BATCH_ROWS) if hasattr(cur, "to_arrow_reader")
                          else cur.fetch_record_batch(_BATCH_ROWS))
                schema = reader.schema
            with open(part, "wb") as fh, pa.ipc.new_stream(fh, schema) as writer:
                if reader is not None:
                    for batch in reader:
                        self._check_cancel(rid)
                        writer.write_batch(batch)
                        if limit is not None and fh.tell() > limit:
                            raise too_large
            if limit is not None and os.path.getsize(part) > limit:
                raise too_large
            self._check_cancel(rid)
            os.replace(part, out)
            return os.path.getsize(out)
        except BaseException:
            for path in (part, out):
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise

    def _insert(self, table: str, create: bool, path: str, rid: str) -> int:
        import pyarrow as pa
        import pyarrow.ipc  # noqa: F401

        self._check_cancel(rid)
        view = f"__fused_sql_in_{secrets.token_hex(8)}"
        with open(path, "rb") as fh:
            reader = pa.ipc.open_stream(fh)
            self.cursor.register(view, reader)
            try:
                if create:
                    self.cursor.execute(
                        f"CREATE OR REPLACE TABLE {_quote_ident(table)} AS SELECT * FROM {view}")
                else:
                    self.cursor.execute(
                        f"INSERT INTO {_quote_ident(table)} SELECT * FROM {view}")
            finally:
                try:
                    self.cursor.unregister(view)
                except Exception:  # noqa: BLE001
                    pass
        return 0


def _session(sid: str) -> _Session:
    with _LOCK:
        s = _SESSIONS.get(sid)
    if s is None:
        s = _Session(sid)
        with _LOCK:
            _SESSIONS[sid] = s
    return s


def _submit(req: dict) -> None:
    rid = str(req.get("id"))
    sid = str(req.get("session"))
    s = _session(sid)
    with _LOCK:
        _ENDED.pop(rid, None)
        s.queued.add(rid)
        if rid in _PRE_CANCELLED:
            del _PRE_CANCELLED[rid]
            s.cancelled.add(rid)
    s.queue.put(dict(req, id=rid))


def _cancel(rid: str) -> None:
    target = None
    with _LOCK:
        for s in _SESSIONS.values():
            if s.running == rid:
                s.cancelled.add(rid)
                target = s
                break
            if rid in s.queued:
                s.cancelled.add(rid)
                return
        if target is None:
            if rid not in _ENDED:
                _remember(_PRE_CANCELLED, rid)
            return
    threading.Thread(target=target.interrupt_while_running, args=(rid,),
                     daemon=True, name="sql-cancel").start()


def _close(sid: str) -> None:
    with _LOCK:
        s = _SESSIONS.pop(sid, None)
        if s is None:
            return
        s.closed = True
        running = s.running
    if running is not None:
        threading.Thread(target=s.interrupt_while_running, args=(running,),
                         daemon=True, name="sql-close").start()
    s.queue.put(None)


def _load_extensions(con) -> list[str]:
    raw = os.environ.get("FUSED_RENDER_SQL_EXTENSIONS")
    names = [n.strip() for n in (_DEFAULT_EXTENSIONS if raw is None else raw).split(",")]
    loaded = []
    for name in names:
        if not name or not name.replace("_", "").isalnum():
            continue
        try:
            try:
                con.execute(f"LOAD {name}")
            except Exception:  # noqa: BLE001 — not installed yet
                con.execute(f"INSTALL {name} FROM community" if name in _COMMUNITY
                            else f"INSTALL {name}")
                con.execute(f"LOAD {name}")
            loaded.append(name)
        except Exception as exc:  # noqa: BLE001 — best-effort
            print(f"sql worker: extension {name} unavailable: {exc}", file=sys.stderr)
    return loaded


def _start(instance: str):
    """Connect, load, configure, lock down and self-check. Returns
    (connection, extensions) or raises."""
    import duckdb

    files = os.path.realpath(os.path.join(instance, "files"))
    tmp = os.path.realpath(os.path.join(instance, "tmp"))
    con = duckdb.connect(":memory:")
    # Secrets stay in memory, and this must be said before anything uses the
    # secret manager: httpfs starts it on the first remote read, and after
    # the lockdown its default folder (in the user's home) is out of reach,
    # which would fail that read and every one after it.
    for setting in ("SET GLOBAL allow_persistent_secrets=false",
                    f"SET GLOBAL secret_directory={_quote_literal(tmp)}"):
        try:
            con.execute(setting)
        except Exception as exc:  # noqa: BLE001 — best-effort
            print(f"sql worker: {setting} failed: {exc}", file=sys.stderr)
    extensions = _load_extensions(con)
    try:
        # Start the secret manager now, while it still can.
        con.execute("SELECT count(*) FROM duckdb_secrets()").fetchall()
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"sql worker: starting the secret manager failed: {exc}", file=sys.stderr)
    # GLOBAL: a plain SET of a client setting stays on this connection and a
    # cursor would not see it.
    settings = [f"SET GLOBAL temp_directory={_quote_literal(tmp)}",
                "SET GLOBAL python_enable_replacements=false",
                "SET GLOBAL autoinstall_known_extensions=false",
                "SET GLOBAL autoload_known_extensions=false"]
    # A page's SQL sees the machine's zone unless the variable pins one for
    # fixed results.
    zone = (os.environ.get("FUSED_RENDER_SQL_TIMEZONE") or "").strip()
    if zone:
        settings.append(f"SET GLOBAL TimeZone={_quote_literal(zone)}")
    for setting in settings:
        try:
            con.execute(setting)
        except Exception as exc:  # noqa: BLE001 — best-effort
            print(f"sql worker: {setting} failed: {exc}", file=sys.stderr)
    # The lockdown. The order is the guard: allowed_directories alone confines
    # nothing (it is a carve-out from enable_external_access=false), and
    # without the lock a statement could widen it again.
    allowed = ", ".join(_quote_literal(d) for d in (files + os.sep, tmp + os.sep, "https://"))
    con.execute(f"SET allowed_directories=[{allowed}]")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    # Fail closed: a file outside files/ and tmp/ must be unreadable.
    probe = os.path.join(os.path.realpath(instance), f"probe-{secrets.token_hex(6)}.txt")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("probe")
    try:
        try:
            con.execute(f"SELECT * FROM read_text({_quote_literal(probe)})").fetchall()
        except Exception:  # noqa: BLE001 — refused, as it must be
            pass
        else:
            raise RuntimeError("the lockdown self-check read a file outside files/")
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return con, extensions


def _scrub_sys_path(cwd: str) -> None:
    cwd_real = os.path.realpath(cwd)
    kept = []
    for entry in sys.path:
        if not entry or entry == "." or not os.path.isabs(entry):
            continue
        if os.path.realpath(entry) == cwd_real:
            continue
        kept.append(entry)
    sys.path[:] = kept


def _read_stdin() -> None:
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if not isinstance(req, dict):
            continue
        op = req.get("op")
        if op in ("query", "insert"):
            _submit(req)
        elif op == "cancel":
            _cancel(str(req.get("id")))
        elif op == "close":
            _close(str(req.get("session")))


def main() -> None:
    global _PROTO, _CON
    # 1. The protocol's own stdout, then fd 1 -> stderr, so a print from C
    # (GDAL in spatial) can never corrupt an answer.
    _PROTO = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    instance = os.environ.get("FUSED_RENDER_SQL_DIR") or ""
    try:
        # 2. Everything imported before files/ becomes the working directory
        # and before the path is scrubbed of it.
        import duckdb  # noqa: F401
        import pyarrow  # noqa: F401
        import pyarrow.ipc  # noqa: F401

        if not instance or not os.path.isdir(os.path.join(instance, "files")):
            raise RuntimeError("FUSED_RENDER_SQL_DIR is not an engine directory")
        _scrub_sys_path(os.getcwd())
        os.chdir(os.path.join(instance, "files"))
        _scrub_sys_path(os.getcwd())
        # 3. + 4.
        _CON, extensions = _start(instance)
    except Exception as exc:  # noqa: BLE001
        send({"event": "ready", "ok": False, "error": str(exc) or type(exc).__name__})
        os._exit(3)
    send({"event": "ready", "ok": True, "version": duckdb.__version__,
          "extensions": extensions})
    try:
        _read_stdin()
    finally:
        # stdin closed: the server is gone or stopping us. Nothing may
        # outlive it.
        try:
            _CON.interrupt()
        except Exception:  # noqa: BLE001
            pass
        with _LOCK:
            sessions = list(_SESSIONS.values())
        for s in sessions:
            try:
                s.cursor.interrupt()
            except Exception:  # noqa: BLE001
                pass
        os._exit(0)


if __name__ == "__main__":
    main()
