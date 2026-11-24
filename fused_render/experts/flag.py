"""The `experts` switch: `prefs.experts_enabled()` (the stored pref, or env `FUSED_RENDER_EXPERTS`).

Off, every `/api/experts*` request answers the unknown-path 404 before routing, so a malformed body (which
FastAPI would reject with a 422 first) or a bad method looks exactly like main.
"""
from __future__ import annotations

from fastapi.responses import JSONResponse

from fused_render.shell.prefs import experts_enabled as on

PREFIX = "/api/experts"


async def off_middleware(request, call_next):
    path = request.url.path
    if (path == PREFIX or path.startswith(PREFIX + "/")) and not on():
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)
