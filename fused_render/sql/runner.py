"""The local SQL engine in the server process (SPEC §55.4).

One engine for the whole app: one worker process (`_worker.py`), one
in-memory DuckDB database, one directory. A session is a caller's own
connection to that database (a cursor in the worker): permanent tables and
registered files are shared by every session; temp tables, variables and
cancellation are the session's own. A session's statements run in order;
different sessions run concurrently.

An engine incarnation is created by the first `create_session` and its
worker is spawned lazily, by the first query or insert. The engine ends when
its worker dies, after IDLE_S untouched with nothing waiting, or on
`shutdown()`; every session id is then unknown, the directory is removed,
and requests still waiting fail with `WorkerGone`. The next `create_session`
starts a new incarnation.

Directory: `<FUSED_RENDER_HOME or ~/.fused-render>/cache/sql/<instance>/`
with `files/` (the worker's working directory and the only local directory
its SQL can reach), `tmp/` (DuckDB's temp_directory) and `io/` (bodies on
their way in, result spools on their way out). Beside it, `<instance>.lock`
is held (an flock, `_platform.flock`) for the incarnation's lifetime and is
taken BEFORE the directory exists, so `sweep()` (run at server startup, safe
at any time) can tell a live owner's directory from a dead one's: a lock it
can take belongs to nobody.

This module never imports duckdb or pyarrow: the engine only runs in the
worker.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

IDLE_S = 20 * 60.0
READY_TIMEOUT_S = 180.0
_STOP_WAIT_S = 2.0
_REAP_EVERY_S = 60.0
_PRE_CANCEL_MAX = 1024

SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
QUERY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

_LOCK = threading.Lock()
_ENGINE: _Engine | None = None
_refused = False
_reaper: threading.Thread | None = None
# Engines marked ended whose worker and directory are still being removed.
_ENDING: set[_Engine] = set()

#: Called as fn(session, query_id) when the worker reports a statement has
#: started (from the reader thread; a listener must not block).
started_listeners: list[Callable[[str, str], None]] = []


class Unavailable(Exception):
    """No engine can start: the app is quitting, or the engine failed to."""


class NotInstalled(Unavailable):
    """duckdb or pyarrow is not installed."""


class NoSession(LookupError):
    """No such session (never created, deleted, or its engine ended)."""


class WorkerGone(Exception):
    """The worker stopped before answering."""


class QueryFailed(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def root() -> str:
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, "cache", "sql")


def available() -> bool:
    return (importlib.util.find_spec("duckdb") is not None
            and importlib.util.find_spec("pyarrow") is not None)


class _Waiter:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.future: asyncio.Future = loop.create_future()

    def deliver(self, event: dict | None, error: Exception | None) -> None:
        def apply():
            if self.future.done():
                return
            if error is not None:
                self.future.set_exception(error)
            else:
                self.future.set_result(event)

        try:
            self.loop.call_soon_threadsafe(apply)
        except RuntimeError:  # the loop is closed: nobody is waiting any more
            pass


class _Session:
    def __init__(self, scope: str):
        self.scope = scope
        self.inflight: set[str] = set()


class _Engine:
    def __init__(self, instance: str, lock_handle):
        self.instance = instance
        self.dir = os.path.join(root(), instance)
        self.lock_path = self.dir + ".lock"
        self.lock_handle = lock_handle
        self.files = os.path.join(self.dir, "files")
        self.tmp = os.path.join(self.dir, "tmp")
        self.io = os.path.join(self.dir, "io")
        self.sessions: dict[str, _Session] = {}
        self.waiters: dict[str, _Waiter] = {}
        self.pre_cancelled: dict[str, None] = {}
        self.proc: subprocess.Popen | None = None
        self.ready = threading.Event()
        self.ready_event: dict | None = None
        self.spawn_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.discarded = False
        self.ended = threading.Event()
        self.last_used = time.monotonic()

    # -- the worker -------------------------------------------------------

    def send(self, obj: dict) -> None:
        line = json.dumps(obj) + "\n"
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise WorkerGone("the SQL engine is not running")
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise WorkerGone("the SQL engine stopped") from exc

    def _read(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("event")
                if kind == "ready":
                    self.ready_event = event
                    self.ready.set()
                    continue
                rid = str(event.get("id"))
                if kind == "started":
                    sid, _, qid = rid.partition(":")
                    for listener in list(started_listeners):
                        try:
                            listener(sid, qid)
                        except Exception:  # noqa: BLE001 — must not kill the reader
                            logger.exception("sql runner: started listener failed")
                    continue
                if kind != "done":
                    continue
                with _LOCK:
                    waiter = self.waiters.pop(rid, None)
                if waiter is not None:
                    waiter.deliver(event, None)
        except (OSError, ValueError):
            pass
        self.ready.set()
        _discard(self, "the SQL engine stopped")

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    logger.debug("sql worker: %s", line[:500])
        except (OSError, ValueError):
            pass

    def stop_process(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(_STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(_STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                pass

    def remove_files(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        _release_lock(self.lock_handle, self.lock_path)
        self.lock_handle = None


def _release_lock(handle, path: str) -> None:
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass
    try:
        os.remove(path)
    except OSError:
        pass


def _open_lock(path: str):
    """Open `path` (never truncating) and lock it without waiting; the open
    handle, or None when another holder has it."""
    from fused_render import _platform

    handle = open(path, "a+")  # noqa: SIM115 — held for the owner's lifetime
    try:
        if _platform.flock(handle, blocking=False):
            return handle
    except OSError:
        pass
    handle.close()
    return None


def _new_engine() -> _Engine:
    base = root()
    os.makedirs(base, exist_ok=True)
    instance = f"{os.getpid()}-{secrets.token_hex(6)}"
    lock_path = os.path.join(base, instance + ".lock")
    # The lock first: a sibling's sweep that sees the directory must find it
    # held.
    handle = _open_lock(lock_path)
    if handle is None:
        raise Unavailable("could not take the SQL engine's directory lock")
    engine = _Engine(instance, handle)
    try:
        for d in (engine.files, engine.tmp, engine.io):
            os.makedirs(d, exist_ok=True)
    except OSError as exc:
        engine.remove_files()
        raise Unavailable(f"could not create the SQL engine's directory: {exc}") from exc
    return engine


def _mark_discarded(engine: _Engine) -> bool:
    """Caller holds _LOCK. Makes every session id of `engine` unknown; False
    when another caller already did (and finishes the job)."""
    global _ENGINE
    if engine.discarded:
        return False
    engine.discarded = True
    _ENDING.add(engine)
    if _ENGINE is engine:
        _ENGINE = None
    engine.sessions.clear()
    return True


def _discard(engine: _Engine, reason: str, marked: bool = False) -> None:
    """End `engine` (worker death, idle, shutdown, a failed start). The
    session ids become unknown first, then the worker is stopped and the
    directory removed, and only then do the requests still waiting fail."""
    if not marked:
        with _LOCK:
            if not _mark_discarded(engine):
                return
    engine.stop_process()
    engine.remove_files()
    with _LOCK:
        waiters = list(engine.waiters.values())
        engine.waiters.clear()
    for waiter in waiters:
        waiter.deliver(None, WorkerGone(reason))
    with _LOCK:
        _ENDING.discard(engine)
    engine.ended.set()
    logger.info("sql runner: engine %s ended (%s)", engine.instance, reason)


def _spawn(engine: _Engine) -> None:
    """Start the worker and wait for its `ready`. Blocking: called off the
    event loop."""
    with engine.spawn_lock:
        if engine.ready.is_set() and engine.proc is not None and not engine.discarded:
            if engine.ready_event and engine.ready_event.get("ok"):
                return
            raise WorkerGone("the SQL engine is not running")
        with _LOCK:
            if engine.discarded:
                raise WorkerGone("the SQL engine stopped")
            if _refused:
                raise Unavailable("fused-render is quitting")
            env = dict(os.environ, FUSED_RENDER_SQL_DIR=engine.dir)
            # A source checkout run without installing: its root, so the
            # worker imports this fused_render whatever its working directory
            # (-P keeps that directory off sys.path). An installed package
            # needs nothing (site-packages must not go ahead of the stdlib).
            pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if os.path.basename(pkg_parent) not in ("site-packages", "dist-packages"):
                env["PYTHONPATH"] = os.pathsep.join(
                    p for p in (pkg_parent, os.environ.get("PYTHONPATH")) if p)
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-P", "-m", "fused_render.sql._worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    cwd=engine.dir, env=env,
                )
            except OSError as exc:
                failed = exc
                proc = None
            else:
                failed = None
                engine.proc = proc
        if proc is None:
            _discard(engine, "the SQL engine could not start")
            raise Unavailable(f"could not start the SQL engine: {failed}")
        logger.info("sql runner: started the engine worker (pid %s)", proc.pid)
        threading.Thread(target=engine._read, args=(proc,), daemon=True,
                         name="sql-engine-reader").start()
        threading.Thread(target=engine._drain_stderr, args=(proc,), daemon=True,
                         name="sql-engine-err").start()
        engine.ready.wait(READY_TIMEOUT_S)
        event = engine.ready_event
        if not event or not event.get("ok"):
            error = (event or {}).get("error") or "the SQL engine did not start"
            _discard(engine, f"start failed: {error}")
            raise Unavailable(f"the SQL engine did not start: {error}")
    _start_reaper()


# -- sessions ---------------------------------------------------------------


def _engine_of(sid: str) -> _Engine:
    """The live engine holding session `sid`, or NoSession. Caller holds _LOCK."""
    if not isinstance(sid, str) or not SESSION_RE.match(sid):
        raise NoSession("no such session")
    engine = _ENGINE
    if engine is None or engine.discarded or sid not in engine.sessions:
        raise NoSession("no such session")
    engine.last_used = time.monotonic()
    return engine


def create_session(scope: str = "") -> str:
    """A new session id on the engine (created when there is none). The
    worker is not started here."""
    global _ENGINE
    if not available():
        raise NotInstalled("duckdb and pyarrow are needed for local SQL")
    with _LOCK:
        if _refused:
            raise Unavailable("fused-render is quitting")
        engine = _ENGINE
        if engine is None or engine.discarded:
            engine = _new_engine()
            _ENGINE = engine
        sid = secrets.token_hex(16)
        engine.sessions[sid] = _Session(scope or "")
        engine.last_used = time.monotonic()
    _start_reaper()
    return sid


def session_files_dir(sid: str) -> str:
    with _LOCK:
        return _engine_of(sid).files


def session_io_dir(sid: str) -> str:
    with _LOCK:
        return _engine_of(sid).io


def io_path(sid: str) -> str:
    """A fresh path in the engine's `io/` for a body on its way in."""
    with _LOCK:
        engine = _engine_of(sid)
    return os.path.join(engine.io, f"in-{secrets.token_hex(12)}.part")


def worker_pid() -> int | None:
    """The live worker's pid, or None."""
    with _LOCK:
        engine = _ENGINE
        proc = engine.proc if engine is not None else None
    return proc.pid if proc is not None else None


def engine_dir() -> str | None:
    with _LOCK:
        return _ENGINE.dir if _ENGINE is not None else None


async def _request(sid: str, rid: str, op: dict) -> dict:
    """Send `op` for session `sid` (starting the worker if needed) and return
    its `done` event."""
    with _LOCK:
        engine = _engine_of(sid)
    await asyncio.to_thread(_spawn, engine)
    waiter = _Waiter(asyncio.get_running_loop())
    session = None
    with engine.write_lock:
        with _LOCK:
            engine = _engine_of(sid)
            session = engine.sessions[sid]
            if rid in engine.pre_cancelled:
                del engine.pre_cancelled[rid]
                raise QueryFailed("cancelled", "cancelled")
            if rid in session.inflight:
                raise QueryFailed("invalid_args", "this query_id is already running")
            session.inflight.add(rid)
            engine.waiters[rid] = waiter
        try:
            engine.send(op)
        except WorkerGone:
            with _LOCK:
                engine.waiters.pop(rid, None)
                session.inflight.discard(rid)
            raise
    try:
        return await waiter.future
    finally:
        with _LOCK:
            engine.waiters.pop(rid, None)
            session.inflight.discard(rid)
            engine.last_used = time.monotonic()


def _check_done(event: dict) -> None:
    if event.get("ok"):
        return
    code = event.get("code") if event.get("code") in ("cancelled", "engine_error") else "engine_error"
    raise QueryFailed(code, str(event.get("message") or code))


async def query(sid: str, sql: str, query_id: str | None = None) -> str:
    """Run `sql` on session `sid`; the path of the spooled Arrow IPC stream
    (the caller removes it). Raises NoSession, Unavailable, WorkerGone or
    QueryFailed."""
    qid = query_id or secrets.token_hex(8)
    if not QUERY_ID_RE.match(qid):
        raise QueryFailed("invalid_args", "query_id is 1-128 of A-Z a-z 0-9 . _ -")
    rid = f"{sid}:{qid}"
    with _LOCK:
        engine = _engine_of(sid)
    out = os.path.join(engine.io, f"out-{secrets.token_hex(12)}.arrows")
    event = await _request(sid, rid, {"op": "query", "id": rid, "session": sid,
                                      "sql": sql, "out": out})
    _check_done(event)
    return out


async def insert(sid: str, table: str, create: bool, path: str) -> None:
    """Load the Arrow IPC stream at `path` into `table` (created or
    replaced when `create`, appended to otherwise)."""
    if not TABLE_RE.match(table or ""):
        raise QueryFailed("invalid_args", "not a valid table name")
    rid = f"{sid}:insert-{secrets.token_hex(8)}"
    event = await _request(sid, rid, {"op": "insert", "id": rid, "session": sid,
                                      "table": table, "create": bool(create),
                                      "path": path})
    _check_done(event)


def cancel(sid: str, query_id: str) -> None:
    """Stop `query_id` of session `sid` (best effort): interrupted while it
    runs, answered `cancelled` without running when it has not started."""
    rid = f"{sid}:{query_id}"
    with _engine_write(sid) as engine:
        if engine is None:
            return
        with _LOCK:
            # With a worker, the worker decides (running: interrupted; not
            # started: cancelled when its turn comes; already ended: nothing).
            # Without one nothing has run yet, so it is remembered here.
            sent = engine.proc is not None
            if not sent:
                engine.pre_cancelled[rid] = None
                while len(engine.pre_cancelled) > _PRE_CANCEL_MAX:
                    engine.pre_cancelled.pop(next(iter(engine.pre_cancelled)))
        if sent:
            try:
                engine.send({"op": "cancel", "id": rid})
            except WorkerGone:
                pass


class _engine_write:
    """`with _engine_write(sid) as engine:` the session's engine (None when
    unknown) with its write lock held, so nothing is sent for the session
    in between."""

    def __init__(self, sid: str):
        self.sid = sid
        self.engine: _Engine | None = None

    def __enter__(self) -> _Engine | None:
        with _LOCK:
            try:
                engine = _engine_of(self.sid)
            except NoSession:
                return None
        engine.write_lock.acquire()
        self.engine = engine
        return engine

    def __exit__(self, *exc) -> None:
        if self.engine is not None:
            self.engine.write_lock.release()


def delete_session(sid: str) -> None:
    """Close session `sid`: what runs there is interrupted, what waits
    answers `cancelled`. The engine and its shared state stay. NoSession
    when unknown."""
    with _engine_write(sid) as engine:
        if engine is None:
            raise NoSession("no such session")
        with _LOCK:
            engine.sessions.pop(sid, None)
            started = engine.proc is not None
            prefix = sid + ":"
            for rid in [r for r in engine.pre_cancelled if r.startswith(prefix)]:
                del engine.pre_cancelled[rid]
        if started:
            try:
                engine.send({"op": "close", "session": sid})
            except WorkerGone:
                pass


# -- lifetime ---------------------------------------------------------------


def reap_once(now: float | None = None) -> bool:
    """One pass of the idle reaper: ends the engine when nothing touched it
    for IDLE_S and nothing is waiting on it. True when it did."""
    with _LOCK:
        engine = _ENGINE
        if engine is None or engine.waiters:
            return False
        if (time.monotonic() if now is None else now) - engine.last_used <= IDLE_S:
            return False
        _mark_discarded(engine)
    _discard(engine, "idle", marked=True)
    return True


def _reap() -> None:
    while True:
        time.sleep(_REAP_EVERY_S)
        try:
            reap_once()
        except Exception:  # noqa: BLE001 — the reaper must keep going
            logger.exception("sql runner: reaping failed")


def _start_reaper() -> None:
    global _reaper
    with _LOCK:
        if _reaper is not None:
            return
        _reaper = threading.Thread(target=_reap, daemon=True, name="sql-engine-reaper")
        _reaper.start()


def refuse_new() -> None:
    """Quit's first step: no engine or worker starts after this."""
    global _refused
    with _LOCK:
        _refused = True


def shutdown() -> None:
    """End the engine (quit, server shutdown): the worker's stdin closed,
    killed after a short wait, the directory removed. Waiting requests fail."""
    with _LOCK:
        engine = _ENGINE
    if engine is not None:
        _discard(engine, "shutdown")
    # An engine another thread is already ending (its worker died a moment
    # ago) is waited for too, so nothing is left once this returns.
    with _LOCK:
        ending = list(_ENDING)
    for other in ending:
        other.ended.wait(_STOP_WAIT_S * 3)


def sweep() -> list[str]:
    """Remove the engine directories whose owner is gone. Two servers can
    share one home, so only a directory whose lock can be taken (nobody
    holds it) or that has no lock file is removed; this process's own engine
    is skipped by name. Returns the instances removed."""
    base = root()
    try:
        names = os.listdir(base)
    except OSError:
        return []
    with _LOCK:
        own = _ENGINE.instance if _ENGINE is not None else None
    instances = {n[:-len(".lock")] if n.endswith(".lock") else n for n in names}
    removed = []
    for instance in sorted(instances):
        if not instance or instance == own:
            continue
        path = os.path.join(base, instance)
        lock_path = path + ".lock"
        if os.path.exists(lock_path):
            handle = _open_lock(lock_path)
            if handle is None:
                continue  # a live owner
            shutil.rmtree(path, ignore_errors=True)
            _release_lock(handle, lock_path)
        elif os.path.isdir(path):
            # The owner takes its lock before making the directory and
            # removes it only after the directory, so this is a leftover.
            shutil.rmtree(path, ignore_errors=True)
        else:
            continue
        removed.append(instance)
    if removed:
        logger.info("sql runner: swept %d engine directories of dead servers", len(removed))
    return removed
