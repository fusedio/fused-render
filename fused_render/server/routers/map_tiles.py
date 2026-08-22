"""Map tiles through the stable server origin (docs/MAP_ENGINE_SERVER_DESIGN.md).

The browser never learns the tile daemon's ephemeral port or token: /describe
answers are rewritten to server-relative /api/map/... paths, and every request
here is proxied to the one child map_engine manages. A child that died or
wedged is restarted and the request retried once — the healing that makes the
tile URL a page holds survive the daemon's whole lifecycle, which the old
client-side re-describe hack never could.

Only the POSTs that reach the child's executing side carry the D3 X-Fused
guard; the tile/job GETs are read-only and same-origin like every other read.
"""
import asyncio
import contextlib
import http.client
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import quote

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import JSONResponse, Response

from fused_render.server import map_engine
from fused_render.server.common import _error, _require_fused

router = APIRouter()

#: A describe against a cold remote source legitimately takes minutes (the old
#: map_render._post allowed the same 300s).
DESCRIBE_TIMEOUT_S = 300.0
#: Long enough for a slow remote tile render, short enough that a wedged child
#: is eventually healed rather than parked on forever.
PROXY_TIMEOUT_S = 120.0

# What the daemon's responses carry that the page needs; mirrors proxy.py's
# forwarded set, minus the range fields the daemon never emits and the length
# Response recomputes from the buffered body.
_PROXY_HEADERS = ("content-type", "cache-control")

#: The browser abandoned the request mid-proxy; there is nobody to answer.
_GONE = object()


def _child_url(child, path: str) -> str:
    separator = "&" if "?" in path else "?"
    return (f"http://127.0.0.1:{child.port}{path}{separator}"
            f"t={quote(child.token, safe='')}")


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


async def _proxy(child, request: Request, path: str):
    """Forward one request to the child. None when the child cannot be reached
    at all — the healing trigger; an HTTP error from a live child is an answer.

    A browser that pans/zooms away resolves _client_hangup, which closes the
    child connection: that frees the fetch thread here immediately and shows
    the daemon a hung-up socket it uses to cancel the render. The old sync
    urllib proxy parked a threadpool thread for up to PROXY_TIMEOUT_S per
    abandoned tile — a viewport of those saturated the browser's six
    connections per origin and blocked every other tab."""
    connection = http.client.HTTPConnection("127.0.0.1", child.port,
                                            timeout=PROXY_TIMEOUT_S)
    separator = "&" if "?" in path else "?"
    target = f"{path}{separator}t={quote(child.token, safe='')}"

    def fetch():
        headers = {}
        rng = request.headers.get("range")
        if rng:
            headers["Range"] = rng
        body = b"" if request.method == "POST" else None
        connection.request(request.method, target, body=body, headers=headers)
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
    except OSError:
        return None
    finally:
        hangup_task.cancel()
        connection.close()
    out = {k: v for k, v in answer.headers.items()
           if k.lower() in _PROXY_HEADERS}
    return Response(content=payload, status_code=answer.status, headers=out)


async def _forward(request: Request, path: str):
    child = map_engine.current()
    if child is None:
        return _error("the map engine is not running; describe the layer again",
                      status=409)
    response = await _proxy(child, request, path)
    if response is None:
        child = await asyncio.to_thread(map_engine.restart, failed=child)
        response = await _proxy(child, request, path)
    if response is _GONE:
        return Response(status_code=204)
    if response is None:
        return _error("the map engine did not answer, even after a restart",
                      status=502)
    return response


@router.post("/api/map/ensure")
def api_map_ensure(payload: dict = Body(...),
                   x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    try:
        child = map_engine.ensure(
            python=str(payload.get("python") or ""),
            daemon=str(payload.get("daemon") or ""),
            cache=str(payload.get("cache") or ""),
            version=str(payload.get("version") or ""),
        )
    except map_engine.MapEngineError as error:
        return _error(str(error), status=400)
    return {"ok": True, "version": child.version, "pid": child.pid}


@router.post("/api/map/forget")
def api_map_forget(payload: dict = Body(...),
                   x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    source = str(payload.get("source") or "")
    if source:
        map_engine.forget(source)
    return {"ok": True}


@router.post("/api/map/describe")
def api_map_describe(payload: dict = Body(...),
                     x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    child = map_engine.current()
    if child is None:
        return _error("the map engine is not running; POST /api/map/ensure first",
                      status=409)
    result = _describe(child, payload)
    if result is None:
        child = map_engine.restart(failed=child)
        result = _describe(child, payload)
    if result is None:
        return _error("the map engine did not answer, even after a restart",
                      status=502)
    status, descriptor = result
    return JSONResponse(_rewrite(descriptor, child, payload), status_code=status)


def _describe(child, payload: dict):
    req = urllib.request.Request(
        _child_url(child, "/describe"),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DESCRIBE_TIMEOUT_S) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        e.close()
        try:
            return e.code, json.loads(detail)
        except ValueError:
            return e.code, {"status": "error", "message": detail}
    except (OSError, ValueError):
        return None


def _rewrite(descriptor, child, request: dict):
    """Swap the child's ephemeral-port URLs for stable /api/map/... paths, and
    remember the request so a restart can re-register the source."""
    data = descriptor.get("data") if isinstance(descriptor, dict) else None
    if not isinstance(data, dict):
        return descriptor
    base = f"http://127.0.0.1:{child.port}/"
    for key in ("tile_url", "vtile_url", "job_url", "optimize_url"):
        url = data.get(key)
        if isinstance(url, str) and url.startswith(base):
            data[key] = "/api/map/" + url[len(base):].split("?", 1)[0]
    source_id = data.get("source_id")
    if descriptor.get("status") == "ok" and source_id:
        map_engine.remember(str(source_id), request)
    return descriptor


@router.get("/api/map/tiles/{source}/{z}/{x}/{y}.png")
async def api_map_tile(source: str, z: int, x: int, y: int, request: Request):
    return await _forward(request, f"/tiles/{source}/{z}/{x}/{y}.png")


@router.get("/api/map/vtiles/{source}/{z}/{x}/{y}.pbf")
async def api_map_vtile(source: str, z: int, x: int, y: int, request: Request):
    return await _forward(request, f"/vtiles/{source}/{z}/{x}/{y}.pbf")


@router.get("/api/map/jobs/{source}")
async def api_map_job(source: str, request: Request):
    return await _forward(request, f"/jobs/{source}")


@router.post("/api/map/optimize/{source}")
async def api_map_optimize(source: str, request: Request,
                           x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    return await _forward(request, f"/optimize/{source}")
