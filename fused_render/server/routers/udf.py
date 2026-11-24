"""The doors of `fused.workbench.udf()` (SPEC §54.2): the `/api/udf/ws`
socket a page runs UDFs over, and `GET /api/udf/results/<token>` for a
result's bytes. The work is `fused_render.udf.service`.

A socket, not a POST per run: a UDF run can last minutes (a batch job, an
hour), and WebKit's six connections per host are shared by every window of
the app (the reason `/api/run/ws` exists). Sockets do not count toward them.

Socket protocol (text frames, JSON):
  page -> {op: "run", id, ref, params?, format?, cache?, confirmed?, tile?}
          {op: "schema", id, ref}
          {op: "cancel", id}
  server -> the frames service.run / service.schema send, each with the
          request's `id`. A page that leaves does not cancel its runs: they
          finish and are saved for the next open.

Guarded like `/api/run/ws`: the Origin must be this server (`ws_origin_ok`),
since a socket has no CORS preflight. The LAN listener refuses sockets, so a
LAN peer never reaches it.

`GET /api/udf/saved` sizes the saved results; `DELETE /api/udf/saved`
(guarded) clears them all — the Preferences → Debugging row.

The results route is a plain GET so an `<img>` or `fetch` can use the URL.
Its token is unguessable (a run key or a random token). A result is served
with a sandboxing CSP, so UDF-made HTML opened at that URL directly, or framed
without a sandbox, runs in an opaque origin and never as this app.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from fused_render.server.common import _require_fused, ws_origin_ok
from fused_render.udf import service, store

logger = logging.getLogger(__name__)

router = APIRouter()

_RESULT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox allow-scripts allow-popups "
                               "allow-popups-to-escape-sandbox allow-downloads",
}


@router.websocket("/api/udf/ws")
async def api_udf_ws(ws: WebSocket):
    if not ws_origin_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    send_lock = asyncio.Lock()
    state = {"open": True}
    tasks: set[asyncio.Task] = set()
    cancels: dict[str, asyncio.Event] = {}

    async def send(frame: dict) -> None:
        if not state["open"]:
            return
        try:
            async with send_lock:
                await ws.send_text(json.dumps(frame, default=str))
        except Exception:  # noqa: BLE001 — the page went away mid-send
            state["open"] = False

    async def handle(msg: dict) -> None:
        op = msg.get("op")
        rid = msg.get("id")
        if op == "run":
            event = asyncio.Event()
            cancels[str(rid)] = event
            try:
                await service.run(msg, send, event)
            finally:
                cancels.pop(str(rid), None)
        elif op == "schema":
            await service.schema(msg, send)
        else:
            await send({"id": rid, "type": "error", "code": "bad_ref",
                        "error": "unknown op"})

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                msg = None
            if not isinstance(msg, dict):
                await send({"id": None, "type": "error", "code": "bad_ref",
                            "error": "message must be a JSON object"})
                continue
            if msg.get("op") == "cancel":
                event = cancels.get(str(msg.get("id")))
                if event is not None:
                    event.set()
                continue
            task = asyncio.create_task(handle(msg))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except WebSocketDisconnect:
        pass
    finally:
        state["open"] = False
        # The page left: its runs are NOT cancelled. A run that finishes is
        # saved, so the next open of the same UDF shows it at once (clicking
        # through nodes must not throw away runs nearly done). Only an
        # explicit {op: "cancel"} stops one.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@router.get("/api/udf/results/{token}")
def api_udf_result(token: str):
    found = store.open_result(token)
    if found is None:
        return JSONResponse({"error": "no such result (it may have expired)"}, status_code=404)
    path, meta = found
    media = meta.get("content_type") or "application/octet-stream"
    return FileResponse(path, media_type=media, headers=_RESULT_HEADERS)


@router.get("/api/udf/saved")
def api_udf_saved_stats():
    """How much the saved UDF results take: `{entries, bytes}` (SPEC §54.3,
    the Preferences → Debugging row)."""
    return store.stats()


@router.delete("/api/udf/saved")
def api_udf_saved_clear(x_fused: str | None = Header(default=None)):
    """Delete every saved UDF result: `{cleared: {entries, bytes}}`. The next
    open of any UDF runs it fresh. Guarded (D3): it deletes files."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return {"cleared": store.clear()}

