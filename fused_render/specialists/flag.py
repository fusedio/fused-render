"""The `specialists` feature flag.

On only when env `FUSED_RENDER_SPECIALISTS` is "1", or when it is unset/other and prefs.json stores
`specialists_enabled: true` (same shape as `breakout_enabled`). The env var wins both ways so tests and dev runs
can force either state without touching the user's prefs.
"""
from __future__ import annotations

import os

from fastapi import HTTPException

ENV = "FUSED_RENDER_SPECIALISTS"


def on() -> bool:
    """True when specialists are enabled. Never raises: a broken prefs file reads as off."""
    env = os.environ.get(ENV)
    if env == "1":
        return True
    if env == "0":
        return False
    try:
        from fused_render.shell import prefs

        return prefs.read_prefs().get("specialists_enabled") is True
    except Exception:  # noqa: BLE001
        return False


#: What an unknown path answers on main (Starlette's own 404): the flag-off answer must be indistinguishable.
NOT_FOUND = {"detail": "Not Found"}
PREFIX = "/api/specialists"


class Off(HTTPException):
    """Raised by `require`: exactly the 404 an unknown route gives."""

    def __init__(self) -> None:
        super().__init__(status_code=404, detail=NOT_FOUND["detail"])


async def off_handler(request, exc):  # noqa: ARG001 — FastAPI's handler signature
    from fastapi.responses import JSONResponse

    return JSONResponse(NOT_FOUND, status_code=404)


async def off_middleware(request, call_next):
    """With the flag off, every `/api/specialists*` request answers the unknown-path 404 BEFORE routing, so a
    malformed body (which FastAPI would reject with a 422 before any dependency ran) or a bad method looks
    exactly like main. Other paths pass through at the cost of one string test."""
    path = request.url.path
    if (path == PREFIX or path.startswith(PREFIX + "/") or path.startswith(PREFIX + "?")) and not on():
        from fastapi.responses import JSONResponse

        return JSONResponse(NOT_FOUND, status_code=404)
    return await call_next(request)


def require() -> None:
    """FastAPI dependency for every `/api/specialists*` route: with the flag off the routes do not exist, so they
    answer exactly what an unknown route answers (404), not a 403 that would advertise the feature."""
    if not on():
        raise Off()
