"""Cooperative cancellation for a rank request that has outlived its client.

`api_index_rank` (server/routers/index.py) dispatches the actual query onto a
worker thread via `asyncio.to_thread`, and `asyncio.to_thread` CANNOT KILL
THAT THREAD — Python gives no way to preempt one. So cancelling has to be
cooperative from inside the thread, and it needs BOTH pieces below, because
neither alone is enough:

  * `check()` — a flag the query thread polls at cheap, frequent points
    between phases (query.py's `pass_over`: before the `execute`, after the
    `fetchall`, before `rank_entries`). This is what makes the thread
    actually RETURN.
  * `cancel()` also calls `con.interrupt()` on the bound duckdb connection —
    the same mechanism `guarded_query.py` already arms from a
    `threading.Timer` for the SQL panel's timeout. This is what unblocks a
    `con.execute()` that is already running when cancellation arrives; a
    `check()` between phases cannot reach INTO a call already in flight.

`bind`/`cancel` ordering matters: a token cancelled BEFORE `bind` (the
abandoned-before-the-connect-even-finished case) must still stop the query,
so `bind` interrupts immediately if the flag is already set, under the same
lock `cancel()` uses — otherwise a fast abort races the connect and is lost.

`unbind`/`cancel` ordering matters just as much at the OTHER end: the
caller's `finally` must call `unbind()` before `con.close()`, so a `cancel()`
racing the very end of the query (the disconnect watcher firing just as the
query thread is already returning) finds no connection to `interrupt()`
rather than one that is closed, or about to be.
"""
import asyncio
import threading
from contextlib import asynccontextmanager


class Cancelled(Exception):
    """Raised by `CancelToken.check()`, and by `search_ranked` when an
    interrupted duckdb call is attributable to this token.

    NOT an error: a cancelled rank is a client that stopped waiting, which is
    normal operation for a per-keystroke request. Callers must not log this
    the way a real failure is logged (see routers/index.py's `api_index_rank`,
    which uses `logger.debug` here for the same reason it already does for the
    candidate-cap line)."""


class CancelToken:
    """One per rank request. `cancel()` is called from the disconnect-watcher
    task (a different thread than the query); `bind()`/`check()` run on the
    query thread itself."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._con = None

    def bind(self, con) -> None:
        """Register the live connection, immediately after `duckdb.connect()`."""
        with self._lock:
            self._con = con
            already_cancelled = self._cancelled
        # Outside the lock: `interrupt()` itself does its own cross-thread
        # synchronization inside duckdb, and holding this lock across it would
        # only widen the window `cancel()` blocks in for no reason.
        if already_cancelled:
            con.interrupt()

    def unbind(self) -> None:
        """Detach the connection, so a `cancel()` that arrives after this
        point does not call `interrupt()` on it.

        The caller (`search_ranked`) must call this in its `finally`, BEFORE
        `con.close()` — the same ordering `guarded_query.py` already uses for
        its own connection-owning `threading.Timer` (`timer.cancel()` before
        `close()`, not after). Without it, `_watch_disconnect`
        (server/routers/index.py) calling `cancel()` after the query thread
        has already returned and closed its connection makes `interrupt()`
        land on an already-closed `duckdb.DuckDBPyConnection`, which raises
        `duckdb.ConnectionException` — on a task nothing ever awaits again
        once the route only `.cancel()`s it, so a perfectly normal client
        disconnect surfaces as an unhandled "Task exception was never
        retrieved" warning instead of nothing at all.

        `cancelled` itself is untouched: `check()` must keep raising for a
        token cancelled before this call, connection or no connection."""
        with self._lock:
            self._con = None

    def cancel(self) -> None:
        """Safe to call from any thread — cross-thread is exactly what
        `duckdb.Connection.interrupt()` exists for."""
        with self._lock:
            self._cancelled = True
            con = self._con
        if con is not None:
            con.interrupt()

    def check(self) -> None:
        """Raise `Cancelled` if `cancel()` has been called."""
        if self._cancelled:
            raise Cancelled()

    @property
    def cancelled(self) -> bool:
        return self._cancelled


# How often the disconnect watcher polls `request.is_disconnected()` while a
# cancellable request is in flight on a worker thread. `is_disconnected()` is
# the only signal an async route that is ALSO doing real work off the event
# loop has for "the client gave up" — the same tradeoff `api_fs_walk` already
# makes (server/routers/fs_read.py's WALK_DISCONNECT_CHECK_EVERY) — and a
# `receive`-driven variant is not worth the added complexity here.
DISCONNECT_POLL_S = 0.1


async def _watch_disconnect(request, token: "CancelToken") -> None:
    """Cancel `token` the moment `request` disconnects; otherwise run forever
    until the caller cancels THIS task (`cancellable`'s `finally`, once the
    work finished on its own)."""
    while True:
        if await request.is_disconnected():
            token.cancel()
            return
        await asyncio.sleep(DISCONNECT_POLL_S)


@asynccontextmanager
async def cancellable(request):
    """`async with cancellable(request) as token:` — a `CancelToken` bound to
    `request`'s lifetime for the duration of the block.

    Wraps the `CancelToken()` / disconnect-watcher-task boilerplate every
    cancellable route otherwise repeats: a fast typist fires and abandons a
    request on every keystroke, and without this the abandoned ones ran to
    completion on a worker thread nothing was still waiting on. The route
    still does its own `await` on that worker thread either way —
    cancellation makes the wait take milliseconds instead of seconds, it does
    not make the `await` itself skippable — and still catches `Cancelled`
    itself, because only the route knows what to log and what response an
    abandoned request gets.

    The watch task is cancelled when the block exits, whether normally or via
    an exception — a `token` outliving its route would poll
    `is_disconnected()` forever on a request nobody is waiting on."""
    token = CancelToken()
    watch = asyncio.create_task(_watch_disconnect(request, token))
    try:
        yield token
    finally:
        watch.cancel()
