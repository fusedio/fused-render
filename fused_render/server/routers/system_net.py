"""The Monitor page's Network tab: what the server saw of its own requests, and
what each browser document measured about its own queueing (fused_render/netlog.py).

All `async def` on purpose. These endpoints report load; a sync handler would
queue in the very 40-thread pool whose saturation they exist to show, so the
page would go blind exactly when it is needed. Everything here is a cheap read
of in-memory state.

Reading any of them marks the server "watched" for 15 s, which is what makes
the middleware add `Server-Timing: ..., fr-watch` so documents start measuring
and beaconing (`POST /api/system/net/client`). The beacon is unguarded on
purpose: `navigator.sendBeacon` cannot set the X-Fused header. It carries only
numbers, is size-capped, and is kept out of the request ring.
"""
import json

from fastapi import APIRouter, Request, Response

from fused_render import netlog
from fused_render.server.common import _error

router = APIRouter()

_MAX_BEACON_BYTES = 256 * 1024


@router.get("/api/system/net/summary")
async def api_net_summary():
    netlog.get().touch_watch()
    return netlog.get().summary()


@router.get("/api/system/net/requests")
async def api_net_requests(since: int = 0, source: str = "", route: str = "",
                           status: str = "", min_ms: float | None = None,
                           open: int = 0, own: int = 0):
    log = netlog.get()
    log.touch_watch()
    return log.requests(since, source=source or None, route=route or None,
                        status=status or None, min_ms=min_ms,
                        open_only=bool(open), include_self=bool(own))


@router.get("/api/system/net/groups")
async def api_net_groups(window: float = 60.0):
    netlog.get().touch_watch()
    return {"window": window, "groups": netlog.get().groups(window)}


@router.get("/api/system/net/sockets")
async def api_net_sockets():
    netlog.get().touch_watch()
    return {"sockets": netlog.get().sockets()}


@router.post("/api/system/net/client")
async def api_net_client(request: Request):
    """One document's browser-side queue stats: `{page, routes: {route:
    {count, sum, max, samples}}}`. Content-Type is whatever sendBeacon sent
    (text/plain for a string), so the body is parsed by hand."""
    raw = await request.body()
    if len(raw) > _MAX_BEACON_BYTES:
        return _error("beacon too large", status=413)
    try:
        body = json.loads(raw)
    except ValueError:
        return _error("beacon must be JSON", status=400)
    if not isinstance(body, dict) or not isinstance(body.get("routes"), dict):
        return _error("beacon needs a `routes` object", status=400)
    page = body.get("page")
    source = netlog.source_name(page if isinstance(page, str) else None,
                                request.headers.get("referer"))
    netlog.get().client_report(source, body["routes"])
    return Response(status_code=204)
