"""`/api/terminal` — the status-bar terminal's session routes and byte stream.

`POST /api/terminal` creates a pty session (fused_render/pty_session.py) and
returns its id; `GET /api/terminal` lists live sessions; `POST
/api/terminal/{sid}/input` writes a string straight into the pty without an
attached stream socket; `DELETE /api/terminal/{sid}` kills one; `WS
/api/terminal/{sid}/stream` attaches to one, replaying its scrollback before
streaming live bytes both ways — a page reload rejoins the same shell rather
than losing it.

Shaped after `fs_read.py`'s `/api/fs/events` for the accept/pump/drain shape,
but the framing differs: fs/events is JSON-only (a change feed), this socket
is a raw byte pipe plus a side channel of JSON control messages, so client and
server frames are typed by their WebSocket frame kind rather than by a field
inside one shape:
  * client -> server: a BINARY frame is keystrokes/paste, written straight to
    the pty; a TEXT frame is a JSON control message, today only
    `{"resize": [rows, cols]}`.
  * server -> client: a BINARY frame is the shell's output; a TEXT frame is
    JSON, today only `{"exit": code}` sent once, when the child dies (or
    `{"exit": null}` immediately, for an id the registry no longer knows).

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


@router.post("/api/terminal/{sid}/input")
def api_terminal_input(sid: str, body: dict = Body(default={}),
                       x_fused: str | None = Header(default=None)):
    # A way to type into a session with no attached stream socket — the
    # "open in terminal / run a command" flow (EntryActionsMenu, fused.terminal.run)
    # sends this right after creating a session, before the drawer's
    # WebSocket has necessarily attached, so it cannot depend on a live
    # `stream` connection the way keystrokes typed into an open drawer do.
    if _windows():
        return _error(_UNSUPPORTED, status=501)
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    data = (body or {}).get("data")
    if not isinstance(data, str):
        return _error("'data' must be a string", status=400)
    session = pty_session.REGISTRY.get(sid)
    if session is None or not session.alive:
        return _error("no such terminal session", status=404)
    session.write(data.encode())
    return {"ok": True}


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
        # An id the registry doesn't know — never created, or reaped by a
        # server restart's `shutdown_all` — gets the same `{"exit": null}`
        # frame a session that dies normally sends, not a bare reject: the
        # client's `TerminalSession` treats every close it didn't ask for as
        # a transient blip and reconnects with backoff, which would retry
        # this same dead id forever. Accepting first and sending the exit
        # frame routes it through the client's ordinary exit handling
        # instead (drop the cached id, mint a fresh shell next open).
        await ws.accept()
        await ws.send_text(json.dumps({"exit": None}))
        await ws.close(code=1008)
        return

    await ws.accept()
    # `attach()` snapshots scrollback, the alive/exit_code pair, and
    # registers the subscriber queue all under one lock hold (PtySession's
    # own docstring explains why: three separate lock acquisitions here,
    # with awaits between them, would lose output the reader thread produces
    # in that window — and, worse, could miss the child's exit entirely if
    # it died during the gap).
    scrollback, alive, exit_code, out_queue = session.attach()
    # Replay scrollback first so a reattach repaints the screen before any
    # new output arrives; a plain empty bytes frame for a session with none
    # yet is harmless (xterm.js writes zero bytes and moves on).
    await ws.send_bytes(scrollback)
    if not alive:
        await ws.send_text(json.dumps({"exit": exit_code}))

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
                try:
                    rows, cols = int(rows), int(cols)
                except (TypeError, ValueError):
                    # Malformed input is ignored, not fatal — same intent as
                    # the JSONDecodeError guard above it. `int()` on a
                    # non-numeric value (or None) would otherwise raise
                    # straight out of this handler, which `except
                    # WebSocketDisconnect` does not catch, tearing down an
                    # otherwise healthy terminal over a single bad control
                    # frame.
                    continue
                session.resize(rows, cols)
    except WebSocketDisconnect:
        pass
    finally:
        pumper.cancel()
        session.unsubscribe(out_queue)
