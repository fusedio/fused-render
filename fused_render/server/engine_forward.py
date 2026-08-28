"""Forward one server request to a managed child engine (docs/ENGINE_HOST_DESIGN.md).

The layer between the server's HTTP surfaces and the child processes engine_host
supervises: it owns the per-child keep-alive connection pool, the heal-on-failure
retry, cancel-on-client-hangup, and the per-call budget. Both engine routers use
it — `routers/engines.py` for template daemons (`/api/engines/{id}/*`) and
`routers/app_engine.py` for the warm app worker (`/api/engine`) — so neither has
to reach into the other. Nothing here is template- or app-aware: the engine_id
and the proxied paths are opaque.
"""
import asyncio
import contextlib
import http.client
import socket
import threading
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response

from fused_render.server import engine_host
from fused_render.server.common import _error

#: A proxied POST (a template's describe against a cold remote source) can
#: legitimately take minutes; a GET (a tile) should heal a wedged child sooner.
POST_TIMEOUT_S = 300.0
GET_TIMEOUT_S = 120.0

# What the daemon's responses carry that the page needs; mirrors proxy.py's
# forwarded set, minus the range fields the daemon never emits and the length
# Response recomputes from the buffered body.
_PROXY_HEADERS = ("content-type", "cache-control")

#: The browser abandoned the request mid-proxy; there is nobody to answer.
_GONE = object()

#: The call outran its per-call budget. Unlike _GONE/None, the child is alive and
#: still running the call (a warm worker does not kill its own thread), so this is
#: NOT a heal trigger and the call is never retried — it becomes a 504.
_TIMEOUT = object()

# Per-child idle-connection pool. Children speak HTTP/1.1 keep-alive, so a
# connection is reused across calls instead of a fresh TCP connect+teardown per
# request. Keyed by child.uid (unique per spawn); _drop_pool, registered as an
# engine_host terminate hook, closes a dead child's connections so they don't
# leak on restart/idle-retire. A connection is returned to the pool only after a
# clean full read; a hung-up or errored one is discarded.
_POOL_MAX = 6
_idle_pools: dict[str, list] = {}
_pool_lock = threading.Lock()


def _checkout(uid: str):
    with _pool_lock:
        pool = _idle_pools.get(uid)
        return pool.pop() if pool else None


def _checkin(uid: str, conn) -> None:
    with _pool_lock:
        pool = _idle_pools.setdefault(uid, [])
        if len(pool) < _POOL_MAX:
            pool.append(conn)
            return
    conn.close()  # pool full: don't hoard connections


def _drop_pool(child) -> None:
    with _pool_lock:
        pool = _idle_pools.pop(child.uid, None)
    for conn in pool or ():
        with contextlib.suppress(Exception):
            conn.close()


engine_host.register_terminate_hook(_drop_pool)


async def _client_hangup(request: Request) -> None:
    """Resolves when the browser abandons the request. A persistent receive()
    is the one disconnect signal uvicorn actually delivers mid-request: its
    h11 protocol pauses reading once a request is complete, so a polled
    request.is_disconnected() never sees the closed socket (verified against
    a live daemon), while an awaited receive() resumes reading and gets the
    http.disconnect event."""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _proxy(child, request: Request, path: str, body: bytes,
                 call_timeout: float | None = None, at_most_once: bool = False):
    """Forward one request to the child. None when the request provably never
    reached a handler (the healing trigger); an HTTP error from a live child is
    an answer.

    `at_most_once` marks a call that runs user `main()` with side effects (the
    warm /call): it never rides a pooled keep-alive, and once the request is on
    the wire a failure is surfaced rather than retried, so `main()` runs at most
    once. Idempotent template traffic pools and retries freely.

    `call_timeout` is a per-call budget applied at the await level (warm app
    /call passes the ~60s /api/run budget). When it elapses the proxy stops
    waiting and returns `_TIMEOUT`: the child is left running (it does not kill
    its own thread, and concurrent calls on it must survive), so this neither
    heals nor retries — the socket timeout below stays the connection-level
    backstop, not the budget.

    A browser that pans/zooms away resolves _client_hangup, which closes the
    child connection: that frees the fetch thread here immediately and shows
    the daemon a hung-up socket it uses to cancel the render. The old sync
    urllib proxy parked a threadpool thread for up to the timeout per abandoned
    request — a viewport of those saturated the browser's six connections per
    origin and blocked every other tab."""
    timeout = POST_TIMEOUT_S if request.method == "POST" else GET_TIMEOUT_S
    separator = "&" if "?" in path else "?"
    target = f"{path}{separator}t={quote(child.token, safe='')}"
    idempotent = request.method in ("GET", "HEAD")

    # Pool keep-alives only for retry-safe traffic: an at-most-once call gets a
    # fresh connection so a stale pooled one can't force an ambiguous retry.
    for reused in ((False,) if at_most_once else (True, False)):
        connection = _checkout(child.uid) if reused else None
        if connection is None:
            if reused:
                continue  # nothing pooled — fall through to the fresh attempt
            connection = http.client.HTTPConnection("127.0.0.1", child.port,
                                                     timeout=timeout)

        sent = [False]

        def fetch(connection=connection, sent=sent):
            if connection.sock is not None:
                connection.sock.settimeout(timeout)  # a reused conn may carry another
            headers = {}
            rng = request.headers.get("range")
            if rng:
                headers["Range"] = rng
            content_type = request.headers.get("content-type")
            if content_type:
                headers["Content-Type"] = content_type
            payload = body if request.method == "POST" else None
            connection.request(request.method, target, body=payload, headers=headers)
            sent[0] = True  # on the wire: a failure past here may have run main()
            answer = connection.getresponse()
            return answer, answer.read()

        fetch_task = asyncio.ensure_future(asyncio.to_thread(fetch))
        hangup_task = asyncio.ensure_future(_client_hangup(request))
        try:
            await asyncio.wait(
                {fetch_task, hangup_task}, timeout=call_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not fetch_task.done():
                # The call is still running: the browser hung up, or (when a
                # call_timeout is set and neither task finished) it blew its
                # budget. Either way stop waiting and sever this socket so the
                # blocked recv frees — shutdown(), not close(): the response's
                # makefile() holds an io ref, so close() defers the real
                # closesocket and the blocked recv (and the daemon's socket)
                # would live on to the bitter end. The child keeps running its
                # thread; we neither heal nor retry.
                timed_out = not hangup_task.done()
                sock = connection.sock
                if sock is not None:
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)
                connection.close()
                with contextlib.suppress(Exception):
                    await fetch_task
                return _TIMEOUT if timed_out else _GONE
            answer, payload = fetch_task.result()
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            # An at-most-once call whose request was already on the wire may have
            # run main(): surface it rather than re-running a side-effecting call.
            # (A failure before it was sent means main() never ran — fall through
            # to a safe retry.)
            if at_most_once and sent[0]:
                return _error(f"the {child.engine_id} worker dropped the call "
                              "after it was sent; not retried, to avoid re-running "
                              "main()", status=502)
            # A pooled keep-alive the child dropped after its idle timeout raises
            # RemoteDisconnected *before* the request is handled — main() never
            # ran — so retry it on a fresh connection to the same, still-warm
            # child rather than declaring the child gone (which would restart it
            # and throw away its warm state). Idempotent GET/HEAD retry on any
            # failure.
            retryable = (idempotent
                         or isinstance(exc, http.client.RemoteDisconnected))
            if reused and retryable:
                continue  # stale pooled connection — retry with a fresh one
            return None
        finally:
            hangup_task.cancel()
        if at_most_once:
            connection.close()  # never pooled: a side-effecting call rides fresh
        else:
            _checkin(child.uid, connection)  # clean read: keep it warm for next time
        out = {k: v for k, v in answer.headers.items()
               if k.lower() in _PROXY_HEADERS}
        return Response(content=payload, status_code=answer.status, headers=out)
    return None


async def _forward(engine_id: str, request: Request, path: str, body: bytes,
                   call_timeout: float | None = None, at_most_once: bool = False):
    child = engine_host.current(engine_id)
    if child is None:
        return _error(
            f"the {engine_id} engine is not running; register the layer again",
            status=409)
    response = await _proxy(child, request, path, body, call_timeout, at_most_once)
    if response is _TIMEOUT:
        # The call outran its budget on a reachable child — the executor's own
        # answer to a slow run. Don't heal or retry (that would kill the still-
        # running worker and re-run its main); surface a timeout, worker intact.
        return _error(f"the {engine_id} call exceeded its "
                      f"{call_timeout:g}s budget", status=504)
    if response is None:
        try:
            child = await asyncio.to_thread(engine_host.restart, engine_id, child)
        except engine_host.EngineError:
            # The engine was torn down between current() and the restart (e.g.
            # app shutdown cleared it); report it as gone, not a 500.
            return _error(
                f"the {engine_id} engine is not running; register the layer again",
                status=409)
        response = await _proxy(child, request, path, body, call_timeout, at_most_once)
    if response is _GONE:
        return Response(status_code=204)
    if response is _TIMEOUT:
        return _error(f"the {engine_id} call exceeded its "
                      f"{call_timeout:g}s budget", status=504)
    if response is None:
        return _error(
            f"the {engine_id} engine did not answer, even after a restart",
            status=502)
    return response
