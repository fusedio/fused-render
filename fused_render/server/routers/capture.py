"""`/api/capture` — native screen, microphone and still capture (SPEC §45).

Route wiring only; everything real is `fused_render/capture/`. Shaped after
`/api/ai/transcribe` (routers/ai_runtime.py): the reply comes back with the
OUTPUT PATH already decided, so a page needs no second lookup and a page that
navigated away can still find what it recorded.

Guarded with `X-Fused` like every other mutating route (D3/D36). A capture is
not a read: it turns on the microphone and the screen. The one exception is the
chunk WebSocket at the bottom — a browser cannot put a header on a handshake —
which is guarded by the per-recording token from its own start reply plus an
`Origin` check instead.
"""

from __future__ import annotations

import asyncio

from fastapi import (APIRouter, Body, Header, Response, WebSocket,
                     WebSocketDisconnect)

from fused_render import capture
from fused_render.server.common import _error, _require_fused

router = APIRouter()


@router.get("/api/capture")
def api_capture_list():
    """What this machine can capture, plus every recording running right now.

    One GET rather than a `/sources` beside a `/list`: a page opening a recorder
    UI wants both in the same paint, and the two are one screenful of state.
    Unguarded, like the other read-only routes — and `sources()` never prompts,
    which is what makes that safe (see `capture._darwin.probe`).
    """
    return {"sources": capture.sources(), "active": capture.active()}


@router.post("/api/capture/start")
def api_capture_start(body: dict = Body(...),
                      x_fused: str | None = Header(default=None)):
    """Begin a recording. `mode` is "screen" or "audio"."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    mode = body.get("mode") or "screen"
    try:
        return capture.start(mode, body)
    except capture.CaptureError as e:
        return _error(str(e), status=400)
    except capture.Unsupported as e:
        # 409, not 400: the request was fine, the machine is not — the same
        # split `/api/ai/*` makes between "you asked wrong" and "there is no
        # runner here" (AI-10).
        return _error(str(e), status=409)
    except Exception as e:                      # noqa: BLE001 - never a traceback
        return _error(f"{e.__class__.__name__}: {e}".rstrip(": "), status=500)


@router.post("/api/capture/{cid}/stop")
def api_capture_stop(cid: str, x_fused: str | None = Header(default=None)):
    """End a recording and keep the file. Resolves with it."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        return capture.stop(cid)
    except capture.CaptureError as e:
        return _error(str(e), status=404)
    except Exception as e:                      # noqa: BLE001
        return _error(f"{e.__class__.__name__}: {e}".rstrip(": "), status=500)


@router.post("/api/capture/{cid}/cancel")
def api_capture_cancel(cid: str, x_fused: str | None = Header(default=None)):
    """End a recording and DELETE the file — the ✕'s meaning, made explicit."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        return capture.stop(cid, discard=True)
    except capture.CaptureError as e:
        return _error(str(e), status=404)
    except Exception as e:                      # noqa: BLE001
        return _error(f"{e.__class__.__name__}: {e}".rstrip(": "), status=500)


@router.post("/api/capture/screenshot")
def api_capture_screenshot(body: dict = Body(...),
                           x_fused: str | None = Header(default=None)):
    """One frame, now. No job row — it is milliseconds, not minutes."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        return capture.screenshot(body)
    except capture.CaptureError as e:
        return _error(str(e), status=400)
    except capture.Unsupported as e:
        return _error(str(e), status=409)
    except Exception as e:                      # noqa: BLE001
        return _error(f"{e.__class__.__name__}: {e}".rstrip(": "), status=500)


@router.post("/api/capture/shot-region")
def api_capture_shot_region(body: dict = Body(...),
                            x_fused: str | None = Header(default=None)):
    """The pixels under a browser-measured screen rect, as a PNG body.

    The shell's export capture (SPEC AF-11): `{rect: [x, y, w, h], dpr}` in
    the browser's own screen units, bytes back — no file in `<home>/recordings`
    and no `fused.capture` surface, because the caller is the shell baking a
    thumbnail into a `.fused`, not a page keeping a still. Errors are the
    ordinary JSON `_error` shape, so a caller branches on `res.ok` alone.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        png = capture.shot_region(body)
    except capture.CaptureError as e:
        return _error(str(e), status=400)
    except capture.Unsupported as e:
        return _error(str(e), status=409)
    except Exception as e:                      # noqa: BLE001
        return _error(f"{e.__class__.__name__}: {e}".rstrip(": "), status=500)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.websocket("/api/capture/{cid}/stream")
async def api_capture_stream(cid: str, ws: WebSocket):
    """The chunk feed for a recording the PAGE encodes (Windows, Linux).

    On those platforms `MediaRecorder` produces the bytes and this appends them
    to the file the start reply already named (see `capture/_sink.py` for why
    the encoder is the page's there and native on macOS). A WebSocket rather
    than repeated POSTs for the reason D74 gives for `/api/fs/events`: a chunk
    every second for up to four hours is a connection to hold open, not four
    thousand requests through a six-per-origin HTTP pool.

    **Guarded by the token from the start reply, not by `X-Fused`** — a browser
    cannot set headers on a WebSocket handshake. The token is per-recording,
    single-use and never in a URL a page would link, and `Origin` is checked
    the way the header guard checks it, so a page on another origin cannot
    write into this server's files even knowing an id.
    """
    await ws.accept()
    origin = ws.headers.get("origin")
    if origin and not _same_origin(ws, origin):
        await ws.close(code=1008, reason="cross-origin stream refused")
        return
    try:
        sink = capture.attach_stream(cid, ws.query_params.get("token"))
    except capture.CaptureError as e:
        # 1008 (policy violation) with the reason on it: the page's `onclose`
        # is the only thing it gets, so the sentence has to travel there.
        await ws.close(code=1008, reason=str(e)[:120])
        return
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("text") == "eos":
                # The page is about to send its stop request, on a DIFFERENT
                # connection. Frames on this one are ordered, so a reply here
                # proves every chunk before it was already appended — without
                # this the stop could close the file first and drop the tail.
                await ws.send_text("flushed")
                continue
            chunk = message.get("bytes")
            if chunk:
                # Off the event loop: this is a disk write on the path of every
                # open socket in the server, and one slow fsync must not stall
                # the file watcher or a render.
                await asyncio.to_thread(sink.write, chunk)
                if sink.done:
                    # The server ended this recording underneath us (the cap,
                    # or the manager's ✕). Closing tells the page to stop
                    # encoding into nothing.
                    break
    except WebSocketDisconnect:
        pass
    finally:
        # CLOSE EXPLICITLY. Returning from a websocket endpoint does not close
        # the socket, and this handler leaves the loop on its own (the byte
        # ceiling, a recording the server ended) as well as on a disconnect —
        # without this the page sits with an open socket and no `onclose`, so it
        # keeps encoding into a file nothing is reading.
        try:
            await ws.close()
        except RuntimeError:
            pass                     # the peer had already gone
        # The socket closing IS an ending when the page did not stop first —
        # a reload takes the encoder with it, and the file is kept.
        await asyncio.to_thread(capture.detach_stream, cid)


def _same_origin(ws: WebSocket, origin: str) -> bool:
    """Is `origin` this server? Compared by host and port, not by string.

    A page is served from `127.0.0.1:<port>` and may reach the socket through
    `localhost:<port>`; both are this server and neither is another site. An
    `Origin` with no port is the scheme's default rather than "any port" —
    treating it as a wildcard would let a page on `http://localhost` attach to a
    server on some other port.
    """
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        return False
    theirs = parsed.port
    if theirs is None:
        theirs = 443 if parsed.scheme == "https" else 80
    return theirs == ws.url.port
