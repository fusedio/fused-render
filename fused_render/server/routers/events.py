"""`WS /api/events`: the one socket a document holds for every live fact.

Text frames, JSON. The client never sends a timer-driven message; the server
never sends anything a client did not subscribe to, except `hello` and
`ping`.

    ← {"t":"hello","boot_id":"…","version":"0.6.28","pid":4242,"topics":{…}}

    → {"t":"sub","id":7,"topic":"tasks.listing","params":{…},"since":1183}
    ← {"t":"snap","id":7,"gen":1190,"body":{…the GET's body…}}
    ← {"t":"delta","id":7,"gen":1191,"body":{…}}      // topics that offer deltas
    ← {"t":"err","id":7,"status":400,"error":"…"}     // the GET's refusal
    → {"t":"unsub","id":7}
    ← {"t":"ping"}                                    // every 15 s of silence

`boot_id` changes when the server restarts: every generation is then
meaningless and the client resubscribes everything. `since` on a resubscribe
lets a delta topic catch a reconnecting client up with one frame; a topic
without a ring ignores it and answers a snapshot.

ORIGIN-CHECKED (`ws_origin_ok`, D10): a socket has no CORS preflight, so any
website the user visits could open one to loopback; the Origin the browser
stamps on the handshake is the guard. Closing before accept answers 403.

ACCESS-LOG PARITY (`/api/run/ws`'s rule): a `WS /api/events sub <topic>` line
per subscribe, so the calls view still sees who asked, and a call record when
the subscriber is a page (its `page` param stands in for `X-Fused-Page`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from fused_render import __version__
from fused_render import calls as shell_calls
from fused_render.server.common import ws_origin_ok
from fused_render.server.events import BOOT_ID, TopicError, bus

logger = logging.getLogger("fused_render")
router = APIRouter()

_WS_ROUTE = "/api/events"


@router.websocket("/api/events")
async def api_events(ws: WebSocket):
    if not ws_origin_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    conn = bus.connect(ws.send_text)
    try:
        await conn.send_now({
            "t": "hello", "boot_id": BOOT_ID, "version": __version__,
            "pid": os.getpid(), "topics": bus.catalog(),
        })
        while True:
            frame = await ws.receive()
            if frame["type"] == "websocket.disconnect":
                break
            try:
                msg = json.loads(frame.get("text") or frame.get("bytes") or "")
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("t")
            sid = msg.get("id")
            if kind == "sub":
                if not isinstance(sid, (str, int)) or isinstance(sid, bool):
                    continue
                if sid in conn.subs:
                    bus.unsubscribe(conn.subs[sid])
                topic = str(msg.get("topic") or "")
                params = msg.get("params")
                since = msg.get("since")
                try:
                    await bus.subscribe(conn, sid, topic, params if isinstance(params, dict) else {},
                                        since if isinstance(since, int) else None)
                except TopicError as exc:
                    await conn.send_now({"t": "err", "id": sid, "status": exc.status, "error": str(exc)})
                    continue
                except Exception:  # noqa: BLE001 — the GET would have 500ed
                    logger.exception("events: subscribe %s %r failed", topic, params)
                    await conn.send_now({"t": "err", "id": sid, "status": 500, "error": "internal error"})
                    continue
                logger.info("WS %s sub %s", _WS_ROUTE, topic)
                page = params.get("page") if isinstance(params, dict) else None
                if isinstance(page, str) and page:
                    call = shell_calls.begin_from(_WS_ROUTE, "WS", {"x-fused-page": page})
                    shell_calls.finish(call, status=200, elapsed_ms=0.0)
            elif kind == "unsub":
                sub = conn.subs.get(sid)
                if sub is not None:
                    bus.unsubscribe(sub)
            # Anything else (a client `ping`, an unknown frame) is ignored.
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    finally:
        bus.disconnect(conn)
