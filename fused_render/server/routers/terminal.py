"""`/api/terminal` — the status-bar terminal's session routes and byte stream.

`POST /api/terminal` creates a pty session (fused_render/pty_session.py) and
returns its id; `GET /api/terminal` lists live sessions; `DELETE
/api/terminal/{sid}` kills one; `WS /api/terminal/{sid}/stream` attaches to
one, replaying its scrollback before streaming live bytes both ways — a page
reload rejoins the same shell rather than losing it.

Shaped after `fs_read.py`'s `/api/fs/events` for the accept/pump/drain shape,
but the framing differs: fs/events is JSON-only (a change feed), this socket
is a raw byte pipe plus a side channel of JSON control messages, so client and
server frames are typed by their WebSocket frame kind rather than by a field
inside one shape:
  * client -> server: a BINARY frame is keystrokes/paste, written straight to
    the pty; a TEXT frame is a JSON control message, today only
    `{"resize": [rows, cols]}`.
  * server -> client: a BINARY frame is the shell's output; a TEXT frame is
    JSON, today only `{"exit": code}` sent once, when the child dies.

Windows: every route returns 501 with a plain reason, and the chip that would
reach them is hidden client-side (see PLAN-status-bar-terminal.md's
Decisions) — a disabled control, not one that fails when clicked.

NOT on the LAN forward allowlist (fused_render/lan.py) — a paired LAN peer's
attach gets a 1008 close. See the comment there for why that is a deliberate
UX/scope call, not a security boundary.

`REGISTRY` is read off the `pty_session` module (not imported by name) so a
test can swap in a scratch registry via `monkeypatch.setattr(pty_session,
"REGISTRY", ...)` without touching this module's globals.
"""
from __future__ import annotations

import asyncio
import json
import os
from queue import Empty

from fastapi import APIRouter, Body, Header, WebSocket, WebSocketDisconnect

from fused_render import pty_session
from fused_render.server.common import _error, _require_fused

router = APIRouter()

_UNSUPPORTED = "terminal is not supported on this platform (Windows is out of scope)"


def _windows() -> bool:
    return os.name == "nt"


@router.post("/api/terminal")
def api_terminal_create(body: dict = Body(default={}),
                        x_fused: str | None = Header(default=None)):
    if _windows():
        return _error(_UNSUPPORTED, status=501)
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    cwd = (body or {}).get("cwd") or None
    try:
        session = pty_session.REGISTRY.create(cwd=cwd)
    except pty_session.SessionLimitError as e:
        return _error(str(e), status=409)
    except RuntimeError as e:
        return _error(str(e), status=501)
    return {"id": session.id}


@router.get("/api/terminal")
def api_terminal_list():
    if _windows():
        return _error(_UNSUPPORTED, status=501)
    return {"sessions": [
        {"id": s.id, "alive": s.alive, "exitCode": s.exit_code}
        for s in pty_session.REGISTRY.list()
    ]}


@router.delete("/api/terminal/{sid}")
def api_terminal_delete(sid: str, x_fused: str | None = Header(default=None)):
    if _windows():
        return _error(_UNSUPPORTED, status=501)
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not pty_session.REGISTRY.kill(sid):
        return _error("no such terminal session", status=404)
    return {"ok": True}


@router.websocket("/api/terminal/{sid}/stream")
async def api_terminal_stream(ws: WebSocket, sid: str):
    if _windows():
        await ws.close(code=1008)
        return
    session = pty_session.REGISTRY.get(sid)
    if session is None:
        # Reject the handshake outright — no accept() — so a stale id closes
        # immediately rather than opening a socket with nothing behind it.
        await ws.close(code=1008)
        return

    await ws.accept()
    # Replay scrollback first so a reattach repaints the screen before any
    # new output arrives; a plain empty bytes frame for a session with none
    # yet is harmless (xterm.js writes zero bytes and moves on).
    await ws.send_bytes(session.scrollback())
    if not session.alive:
        await ws.send_text(json.dumps({"exit": session.exit_code}))

    out_queue = session.subscribe()

    async def pump_output():
        # A plain `await asyncio.to_thread(out_queue.get)` would block a
        # thread-pool worker on the underlying blocking call forever whenever
        # nothing more ever arrives (a disconnect with the shell still
        # alive). Cancelling THIS task then does not stop that worker
        # thread — `Future.cancel()` is a no-op once a work item is already
        # running — so on cleanup asyncio's default-executor shutdown
        # (`loop.shutdown_default_executor`, run whenever the loop backing
        # this test/portal closes) blocks forever waiting for a worker that
        # will never return, hanging the whole process. Polling with a short
        # timeout instead means a cancelled task's current `to_thread` call
        # returns (Empty) well within one tick, so cancellation actually
        # takes effect and no thread outlives this task.
        while True:
            try:
                kind, payload = await asyncio.to_thread(out_queue.get, timeout=0.2)
            except Empty:
                continue
            if kind == "data":
                await ws.send_bytes(payload)
            else:  # "exit"
                await ws.send_text(json.dumps({"exit": payload}))
                return

    pumper = asyncio.create_task(pump_output())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                session.write(data)
                continue
            text = msg.get("text")
            if text is None:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                continue
            resize = control.get("resize")
            if isinstance(resize, list) and len(resize) == 2:
                rows, cols = resize
                session.resize(int(rows), int(cols))
    except WebSocketDisconnect:
        pass
    finally:
        pumper.cancel()
        session.unsubscribe(out_queue)
