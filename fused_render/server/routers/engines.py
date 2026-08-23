"""Managed template engines through the stable server origin (docs/ENGINE_HOST_DESIGN.md).

The browser never learns a daemon's ephemeral port or token: a template rewrites
its own descriptor URLs to /api/engines/{engine_id}/proxy/... paths, and every
request here is proxied to the child engine_host manages for that id. A child
that died or wedged is restarted and the request retried once — the healing that
makes a proxied URL a page holds survive the daemon's whole lifecycle, which the
old client-side re-describe hack never could.

The control plane (ensure/reinit/forget) and every proxied POST carry the D3
X-Fused guard because they reach the child's executing side; proxied GETs are
read-only and same-origin like every other read. Nothing here is template-aware:
the engine_id and the proxied paths are opaque.
"""
import asyncio
import contextlib
import http.client
import json
import socket
import threading
from urllib.parse import quote

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import Response

from fused_render.server import engine_host
from fused_render.server.common import _error, _require_fused

router = APIRouter()

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


async def _proxy(child, request: Request, path: str, body: bytes):
    """Forward one request to the child. None when the child cannot be reached
    at all — the healing trigger; an HTTP error from a live child is an answer.

    A browser that pans/zooms away resolves _client_hangup, which closes the
    child connection: that frees the fetch thread here immediately and shows
    the daemon a hung-up socket it uses to cancel the render. The old sync
    urllib proxy parked a threadpool thread for up to the timeout per abandoned
    request — a viewport of those saturated the browser's six connections per
    origin and blocked every other tab."""
    timeout = POST_TIMEOUT_S if request.method == "POST" else GET_TIMEOUT_S
    separator = "&" if "?" in path else "?"
    target = f"{path}{separator}t={quote(child.token, safe='')}"

    # Try a pooled connection first; if a reused one fails (a keep-alive the
    # child dropped), retry once on a fresh one before declaring the child gone.
    for reused in (True, False):
        connection = _checkout(child.uid) if reused else None
        if connection is None:
            if reused:
                continue  # nothing pooled — fall through to the fresh attempt
            connection = http.client.HTTPConnection("127.0.0.1", child.port,
                                                     timeout=timeout)

        def fetch(connection=connection):
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
            answer = connection.getresponse()
            return answer, answer.read()

        fetch_task = asyncio.ensure_future(asyncio.to_thread(fetch))
        hangup_task = asyncio.ensure_future(_client_hangup(request))
        try:
            await asyncio.wait(
                {fetch_task, hangup_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if not fetch_task.done():
                # shutdown(), not close(): the response's makefile() holds an io
                # ref, so close() defers the real closesocket and the blocked
                # recv (and the daemon's socket) would live on to the bitter end.
                sock = connection.sock
                if sock is not None:
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)
                connection.close()
                with contextlib.suppress(Exception):
                    await fetch_task
                return _GONE
            answer, payload = fetch_task.result()
        except (OSError, http.client.HTTPException):
            connection.close()
            # Only replay a stale pooled connection for an idempotent request: a
            # POST may already have run main() before the keep-alive dropped, so
            # re-sending it could double-execute a side-effecting call.
            if reused and request.method in ("GET", "HEAD"):
                continue  # stale pooled connection — retry with a fresh one
            return None
        finally:
            hangup_task.cancel()
        _checkin(child.uid, connection)  # clean read: keep it warm for the next call
        out = {k: v for k, v in answer.headers.items()
               if k.lower() in _PROXY_HEADERS}
        return Response(content=payload, status_code=answer.status, headers=out)
    return None


async def _forward(engine_id: str, request: Request, path: str, body: bytes):
    child = engine_host.current(engine_id)
    if child is None:
        return _error(
            f"the {engine_id} engine is not running; register the layer again",
            status=409)
    response = await _proxy(child, request, path, body)
    if response is None:
        try:
            child = await asyncio.to_thread(engine_host.restart, engine_id, child)
        except engine_host.EngineError:
            # The engine was torn down between current() and the restart (e.g.
            # app shutdown cleared it); report it as gone, not a 500.
            return _error(
                f"the {engine_id} engine is not running; register the layer again",
                status=409)
        response = await _proxy(child, request, path, body)
    if response is _GONE:
        return Response(status_code=204)
    if response is None:
        return _error(
            f"the {engine_id} engine did not answer, even after a restart",
            status=502)
    return response


@router.post("/api/engines/{engine_id}/ensure")
def api_engine_ensure(engine_id: str, payload: dict = Body(...),
                      x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    try:
        child = engine_host.ensure(
            engine_id=engine_id,
            python=str(payload.get("python") or ""),
            daemon=str(payload.get("daemon") or ""),
            cache=str(payload.get("cache") or ""),
            version=str(payload.get("version") or ""),
        )
    except engine_host.EngineError as error:
        return _error(str(error), status=400)
    return {"ok": True, "version": child.version, "pid": child.pid}


@router.post("/api/engines/{engine_id}/reinit")
def api_engine_reinit(engine_id: str, payload: dict = Body(...),
                      x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    key = str(payload.get("key") or "")
    path = str(payload.get("path") or "")
    request = payload.get("payload")
    if not key or not path or not isinstance(request, dict):
        return _error("reinit needs key, path and payload", status=400)
    engine_host.reinit(engine_id, key, path, request)
    return {"ok": True}


@router.post("/api/engines/{engine_id}/forget")
def api_engine_forget(engine_id: str, payload: dict = Body(...),
                      x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    key = str(payload.get("key") or "")
    if key:
        engine_host.forget(engine_id, key)
    return {"ok": True}


@router.api_route(
    "/api/engines/{engine_id}/proxy/{path:path}",
    methods=["GET", "HEAD", "POST"],
)
async def api_engine_proxy(engine_id: str, path: str, request: Request,
                           x_fused: str | None = Header(default=None),
                           x_engine_reinit: str | None = Header(default=None)):
    # /ping is the daemon's private liveness path (engine_host probes it directly
    # with the token); it is never a page resource, so it is not proxied.
    if path == "ping":
        return _error("not found", status=404)
    # The path is opaque and forwarded verbatim, but no segment may climb out of
    # the namespace the child serves — a correctness guard for every engine.
    if ".." in path.split("/") or "\\" in path:
        return _error("not found", status=404)
    if request.method == "POST" and (error := _require_fused(x_fused)) is not None:
        return error
    body = await request.body() if request.method == "POST" else b""
    response = await _forward(engine_id, request, "/" + path, body)
    # A POST the caller marks replayable (X-Engine-Reinit: <key>) that the child
    # accepted is recorded here, atomically with the request — so a restart
    # re-runs it and the registration can never be lost to a separate call. The
    # body is opaque; the host only re-POSTs it.
    if (request.method == "POST" and x_engine_reinit
            and isinstance(response, Response)
            and 200 <= response.status_code < 300 and response.status_code != 204):
        with contextlib.suppress(ValueError):
            engine_host.reinit(engine_id, x_engine_reinit, "/" + path,
                               json.loads(body or b"{}"))
    return response
