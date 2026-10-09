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

from fused_render import claude_cmd_log, pty_session
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
    # Optional fitted terminal size; invalid/missing keeps the old no-ioctl path.
    size = pty_session.valid_winsize((body or {}).get("rows"), (body or {}).get("cols"))
    rows, cols = size if size is not None else (None, None)
    try:
        session = pty_session.REGISTRY.create(cwd=cwd, rows=rows, cols=cols)
    except pty_session.SessionLimitError as e:
        return _error(str(e), status=409)
    except RuntimeError as e:
        return _error(str(e), status=501)
    return {"id": session.id}


@router.get("/api/terminal")
def api_terminal_list():
    if _windows():
        return _error(_UNSUPPORTED, status=501)
    # `shell` is what the client labels a tab with when it has no better name
    # and `cwd` the tooltip's directory (live once the shell reports it);
    # `foreground`/`lastCommand`/`lastExit`/`lastActivity` are additive — see
    # PtySession.snapshot.
    reg = pty_session.REGISTRY
    focused = reg.focused_id
    return {"sessions": [{**s.snapshot(), "focused": s.id == focused}
                         for s in reg.list()] + _claude_entries(focused)}


def _claude_entries(focused) -> list:
    """The read-only "claude" tabs: one per chat that has run a Bash-tool
    command (D1327). Always `alive` — a log view never exits — with `running`
    saying whether a command is executing right now. A tab disappears once the
    user dismisses it (DELETE) or Claude's turn ends, until a newer command."""
    out = []
    for e in claude_cmd_log.list_chats():
        cmds = claude_cmd_log.commands(e["chat"])
        last = cmds[-1] if cmds else None
        sid = claude_cmd_log.session_id(e["chat"])
        out.append({
            "id": sid, "kind": "claude", "chat": e["chat"], "alive": True,
            "exitCode": None, "shell": "claude", "cwd": None, "foreground": None,
            "lastCommand": claude_cmd_log.readable_command(last.raw) if last else None,
            "lastExit": last.exit_code if last else None,
            "lastActivity": e["lastActivity"], "running": e["running"],
            "focused": sid == focused})
    return out


@router.put("/api/terminal/focus")
def api_terminal_focus(body: dict = Body(default={}),
                       x_fused: str | None = Header(default=None)):
    """Remember which terminal tab the user has in front of them, so Claude's
    `terminal_read()` with no id (and `terminal_list`'s `focused`) can answer
    "which terminal do you mean". The drawer reports it on every tab change;
    `{"id": null}` clears it (drawer closed / last tab gone)."""
    if _windows():
        return _error(_UNSUPPORTED, status=501)
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    sid = (body or {}).get("id")
    if sid is not None and not isinstance(sid, str):
        return _error("'id' must be a string or null", status=400)
    pty_session.REGISTRY.focused_id = sid or None
    return {"ok": True}


@router.get("/api/terminal/{sid}/text")
def api_terminal_text(sid: str, lines: int = pty_session.DEFAULT_TEXT_LINES):
    """The terminal as a human sees it (VT-emulated, not raw bytes) plus the
    metadata of the list entry. Unguarded like the list: a read."""
    if _windows():
        return _error(_UNSUPPORTED, status=501)
    lines = max(1, min(lines, pty_session.MAX_TEXT_LINES))
    chat = claude_cmd_log.chat_of(sid)
    if chat is not None:
        entry = next((e for e in _claude_entries(None) if e["chat"] == chat), None)
        if entry is None:
            return _error("no such terminal session", status=404)
        return {**entry, "text": claude_cmd_log.render_text(chat, lines)}
    session = pty_session.REGISTRY.get(sid)
    if session is None:
        return _error("no such terminal session", status=404)
    return {**session.snapshot(), "text": session.text(lines)}


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
    # Refuse to type into a pane that isn't sitting at the shell's own
    # prompt: an "open in terminal / run a command" request landing on an
    # existing, reattached session (TerminalDrawer.tsx) has no idea what's
    # currently running there, and `cd ... && cmd\r` going to vim, a REPL, or
    # a dev server instead of the shell is silent data loss / a bogus
    # command. `shell_is_foreground()` (pty_session.py) is the actual check;
    # `wait_shell_foreground()` gives it a grace period first — a freshly
    # created session's `tcgetpgrp` reads back stale/empty until the child's
    # own `setsid()` lands, and an interactive shell's rc files can briefly
    # run a foreground job of their own — before this turns a real "no" into
    # a 409 the client shows a line for instead of blindly writing the bytes.
    if not session.wait_shell_foreground(timeout=1.5):
        return _error("terminal is busy", status=409)
    session.write(data.encode())
    return {"ok": True}


@router.post("/api/terminal/{sid}/stop")
def api_terminal_stop(sid: str, x_fused: str | None = Header(default=None)):
    """Stop the commands a chat is running right now (the Claude tab's Stop
    button). Kills the wrapper's process tree, not a pgid: see claude_cmd_log."""
    if _windows():
        return _error(_UNSUPPORTED, status=501)
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    chat = claude_cmd_log.chat_of(sid)
    if chat is None or not claude_cmd_log.command_ids(chat):
        return _error("no such terminal session", status=404)
    return {"ok": True, "stopped": len(claude_cmd_log.stop(chat))}


@router.delete("/api/terminal/{sid}")
def api_terminal_delete(sid: str, x_fused: str | None = Header(default=None)):
    if _windows():
        return _error(_UNSUPPORTED, status=501)
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if sid.startswith(claude_cmd_log.PREFIX):
        chat = claude_cmd_log.chat_of(sid)
        if chat is None or not claude_cmd_log.command_ids(chat):
            return _error("no such terminal session", status=404)
        if any(c.running for c in claude_cmd_log.commands(chat)):
            return _error("stop the running command first", status=409)
        claude_cmd_log.mark(chat, claude_cmd_log.DISMISSED)
        return {"ok": True}
    if not pty_session.REGISTRY.kill(sid):
        return _error("no such terminal session", status=404)
    return {"ok": True}


async def _stream_claude(ws: WebSocket, chat: str) -> None:
    """The read-only Claude tab: same frames as a pty stream (binary output,
    text `{"exit"}`) but backed by the wrapper's log, polled. Replay is just the
    first poll of a fresh reader; client input is read and dropped."""
    await ws.accept()
    if not claude_cmd_log.command_ids(chat):
        await ws.send_text(json.dumps({"exit": None}))
        await ws.close(code=1008)
        return
    reader = claude_cmd_log.Stream(chat)
    await ws.send_bytes(await asyncio.to_thread(reader.poll))

    async def pump():
        while True:
            await asyncio.sleep(0.2)
            data = await asyncio.to_thread(reader.poll)
            if data:
                await ws.send_bytes(data)

    pumper = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        pumper.cancel()


@router.websocket("/api/terminal/{sid}/stream")
async def api_terminal_stream(ws: WebSocket, sid: str):
    if _windows():
        await ws.close(code=1008)
        return
    chat = claude_cmd_log.chat_of(sid)
    if chat is not None:
        await _stream_claude(ws, chat)
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
            # A syntactically valid but non-object control frame (`5`,
            # `[1, 2]`, `"resize"`) has no `.get` — ignored here rather than
            # tearing down the socket with an AttributeError the same way the
            # JSONDecodeError guard above it is.
            if not isinstance(control, dict):
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
                # `struct.pack`'s "HHHH" format (PtySession.resize) rejects
                # anything outside an unsigned 16-bit int with `struct.error`
                # — ignored here rather than raised, same as the malformed
                # cases above. (`PtySession.resize` also catches it directly,
                # as a second line of defense for any other caller.)
                if not (1 <= rows <= 65535 and 1 <= cols <= 65535):
                    continue
                session.resize(rows, cols)
    except WebSocketDisconnect:
        pass
    finally:
        pumper.cancel()
        session.unsubscribe(out_queue)
