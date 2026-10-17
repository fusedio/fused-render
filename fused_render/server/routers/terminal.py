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
import time

from fastapi import APIRouter, Body, Header, WebSocket, WebSocketDisconnect

from fused_render import claude_cmd_log, pty_session
from fused_render.server.common import _error, _require_fused

router = APIRouter()

#: Most output bytes `pump_output` merges into one WS frame.
_MERGE_CAP = 256 * 1024
#: Output arriving closer together than this counts as a flood, and
#: `pump_output` then waits `_FLOOD_TICK_S` for more before sending.
_FLOOD_GAP_S = 0.01
_FLOOD_TICK_S = 0.004

@router.post("/api/terminal")
def api_terminal_create(body: dict = Body(default={}),
                        x_fused: str | None = Header(default=None)):
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
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    chat = claude_cmd_log.chat_of(sid)
    if chat is None or not claude_cmd_log.command_ids(chat):
        return _error("no such terminal session", status=404)
    return {"ok": True, "stopped": len(claude_cmd_log.stop(chat))}


@router.delete("/api/terminal/{sid}")
def api_terminal_delete(sid: str, x_fused: str | None = Header(default=None)):
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
    scrollback, alive, exit_code, out_queue = session.attach(asyncio.get_running_loop())
    # Replay scrollback first so a reattach repaints the screen before any
    # new output arrives; a plain empty bytes frame for a session with none
    # yet is harmless (xterm.js writes zero bytes and moves on).
    await ws.send_bytes(scrollback)
    if not alive:
        await ws.send_text(json.dumps({"exit": exit_code}))

    async def pump_output():
        # `out_queue` is an asyncio.Queue the reader thread feeds through
        # `loop.call_soon_threadsafe` (see `PtySession.attach`), so this is a
        # plain `await` on the event loop. It used to be `await
        # asyncio.to_thread(out_queue.get, timeout=0.2)` on a blocking
        # SimpleQueue: that parked one default-executor worker per attached
        # terminal for the terminal's whole life (the rest of the server
        # shares that pool), and the 0.2 s timeout existed only because a
        # worker blocked in an un-timed `get` can never be cancelled —
        # `Future.cancel()` is a no-op once the work item runs, so the
        # default-executor shutdown at loop close would hang forever on it.
        # An asyncio queue `get` is natively cancellable, so that hazard is
        # gone and so is the poll.
        last_send = 0.0
        while True:
            kind, payload = await out_queue.get()
            if kind != "data":  # "exit"
                await ws.send_text(json.dumps({"exit": payload}))
                return
            # Merge whatever else is already queued into this one frame. A
            # flooding program makes the reader enqueue ~one item per read
            # (a pty read returns 1-4 KB on macOS however big the buffer);
            # a WS frame per item costs a send, a JS message event and an
            # xterm `write` each. Capped so one frame stays a sane size and
            # the loop still yields to the receive side between sends.
            # Order is never changed: an "exit" met while draining is held
            # until the merged data has been sent.
            chunks = [payload]
            size = len(payload)
            exited = False
            code = None
            # A consumer that keeps up finds the queue empty every time and
            # merges nothing, so while output is arriving back to back (the
            # previous frame went out < _FLOOD_GAP_S ago) wait one tick for
            # the reader to get ahead. A lone keystroke echo has no recent
            # frame, skips the wait and pays no latency.
            waited = time.monotonic() - last_send >= _FLOOD_GAP_S
            while size < _MERGE_CAP:
                try:
                    kind, payload = out_queue.get_nowait()
                except asyncio.QueueEmpty:
                    if waited:
                        break
                    waited = True
                    await asyncio.sleep(_FLOOD_TICK_S)
                    continue
                if kind != "data":
                    exited, code = True, payload
                    break
                chunks.append(payload)
                size += len(payload)
            await ws.send_bytes(b"".join(chunks) if len(chunks) > 1 else chunks[0])
            last_send = time.monotonic()
            if exited:
                await ws.send_text(json.dumps({"exit": code}))
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
