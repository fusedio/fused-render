"""`/api/capture` — native screen, microphone and still capture (SPEC §44).

Route wiring only; everything real is `fused_render/capture/`. Shaped after
`/api/ai/transcribe` (routers/ai_runtime.py): the reply comes back with the
OUTPUT PATH already decided, so a page needs no second lookup and a page that
navigated away can still find what it recorded.

Guarded with `X-Fused` like every other mutating route (D3/D36). A capture is
not a read: it turns on the microphone and the screen.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Header

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
