"""The warm UDF workers: one per Fused environment (SPEC §54.4).

`fused._env` is process-wide, so each environment gets its own worker
(`_worker.py`), started on the first run there and kept up: the SDK import
and its per-process setup are paid once. A worker is replaced when the
signed-in account changes (`canvases._listing_account`, the hashed `sub`),
stopped after IDLE_S without a run, and stopped on quit (`shutdown`, a rung of
app.py's `_stop_children`).

`await run(env, request)` sends one request and returns its final event;
`on_event` hears every event before it (`started` with the UDF's kind). A
worker that dies fails every run waiting on it; the next run starts a fresh
one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

IDLE_S = 20 * 60.0
_STOP_WAIT_S = 2.0
FINAL_EVENTS = ("done", "needs_confirmation", "schema")

_LOCK = threading.Lock()
_WORKERS: dict[str, _Worker] = {}
_refused = False
_reaper: threading.Thread | None = None


class RunnerUnavailable(Exception):
    """No worker can be started (an external CLI override, quitting)."""


class WorkerGone(Exception):
    """The worker stopped before answering."""


class _Waiter:
    def __init__(self, loop: asyncio.AbstractEventLoop, on_event: Callable[[dict], None] | None):
        self.loop = loop
        self.future: asyncio.Future = loop.create_future()
        self.on_event = on_event


class _Worker:
    def __init__(self, env: str, account: str | None, proc: subprocess.Popen):
        self.env = env
        self.account = account
        self.proc = proc
        self.waiters: dict[str, _Waiter] = {}
        self.write_lock = threading.Lock()
        self.last_used = time.monotonic()
        self.reader = threading.Thread(target=self._read, daemon=True,
                                       name=f"udf-worker-{env}")
        self.reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True,
                         name=f"udf-worker-{env}-err").start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, obj: dict) -> None:
        line = json.dumps(obj) + "\n"
        with self.write_lock:
            if self.proc.stdin is None:
                raise WorkerGone("the UDF worker is not running")
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise WorkerGone("the UDF worker stopped") from exc

    def _deliver(self, waiter: _Waiter, event: dict | None, error: Exception | None) -> None:
        def apply():
            if waiter.future.done():
                return
            if error is not None:
                waiter.future.set_exception(error)
            elif event.get("event") in FINAL_EVENTS:
                waiter.future.set_result(event)
            elif waiter.on_event is not None:
                try:
                    waiter.on_event(event)
                except Exception:  # noqa: BLE001 — a listener must not kill the reader
                    logger.exception("udf runner: event listener failed")

        try:
            waiter.loop.call_soon_threadsafe(apply)
        except RuntimeError:  # the loop is closed: nobody is waiting any more
            pass

    def _read(self) -> None:
        stream = self.proc.stdout
        try:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                rid = str(event.get("id"))
                with _LOCK:
                    waiter = self.waiters.get(rid)
                    if waiter is not None and event.get("event") in FINAL_EVENTS:
                        self.waiters.pop(rid, None)
                if waiter is not None:
                    self._deliver(waiter, event, None)
        except (OSError, ValueError):
            pass
        with _LOCK:
            waiters = list(self.waiters.values())
            self.waiters.clear()
            if _WORKERS.get(self.env) is self:
                _WORKERS.pop(self.env, None)
        for waiter in waiters:
            self._deliver(waiter, None, WorkerGone("the UDF worker stopped"))

    def _drain_stderr(self) -> None:
        # The SDK's prints and warnings: kept in the server log, at debug.
        try:
            for line in self.proc.stderr:
                line = line.rstrip()
                if line:
                    logger.debug("udf worker %s: %s", self.env, line[:500])
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(_STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(_STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                pass


def _spawn(env: str, account: str | None) -> _Worker:
    from fused_render import canvases
    from fused_render.fusedcli import fused_cli

    cli = fused_cli()
    if cli is None or cli.external:
        raise RunnerUnavailable("running a UDF needs the bundled fused SDK")
    proc = subprocess.Popen(
        [sys.executable, "-m", "fused_render.udf._worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=canvases._cli_env(cli, env),
    )
    logger.info("udf runner: started a worker for %s (pid %s)", env, proc.pid)
    return _Worker(env, account, proc)


def _worker_for(env: str) -> _Worker:
    from fused_render import canvases

    account = canvases._listing_account(env)
    stale = None
    with _LOCK:
        if _refused:
            raise RunnerUnavailable("fused-render is quitting")
        worker = _WORKERS.get(env)
        if worker is not None and worker.alive() and worker.account == account:
            worker.last_used = time.monotonic()
            return worker
        if worker is not None:
            # Dead, or signed in as someone else since it started: its SDK
            # holds the old login.
            stale = _WORKERS.pop(env)
        worker = _spawn(env, account)
        _WORKERS[env] = worker
    if stale is not None:
        threading.Thread(target=stale.stop, daemon=True).start()
    _start_reaper()
    return worker


async def run(env: str, request: dict, on_event: Callable[[dict], None] | None = None) -> dict:
    """Send `request` (it carries its own `id`) to `env`'s worker and return
    its final event. Raises RunnerUnavailable or WorkerGone."""
    worker = _worker_for(env)
    waiter = _Waiter(asyncio.get_running_loop(), on_event)
    rid = str(request["id"])
    with _LOCK:
        worker.waiters[rid] = waiter
    try:
        worker.send(request)
        return await waiter.future
    finally:
        with _LOCK:
            worker.waiters.pop(rid, None)
            worker.last_used = time.monotonic()


def cancel(env: str, rid: str) -> None:
    """Ask `env`'s worker to stop run `rid` (best effort)."""
    with _LOCK:
        worker = _WORKERS.get(env)
    if worker is not None:
        try:
            worker.send({"op": "cancel", "id": rid})
        except WorkerGone:
            pass


def _reap() -> None:
    while True:
        time.sleep(60.0)
        idle = []
        with _LOCK:
            now = time.monotonic()
            for env, worker in list(_WORKERS.items()):
                if not worker.waiters and now - worker.last_used > IDLE_S:
                    idle.append(_WORKERS.pop(env))
        for worker in idle:
            logger.info("udf runner: stopping the idle %s worker", worker.env)
            worker.stop()


def _start_reaper() -> None:
    global _reaper
    with _LOCK:
        if _reaper is not None:
            return
        _reaper = threading.Thread(target=_reap, daemon=True, name="udf-worker-reaper")
        _reaper.start()


def refuse_new() -> None:
    """Quit's first step: no worker starts after this."""
    global _refused
    with _LOCK:
        _refused = True


def shutdown() -> None:
    """Stop every worker (quit, server shutdown). Waiting runs fail."""
    with _LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.stop()
