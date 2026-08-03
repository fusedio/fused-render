"""The OS clipboard bridge: /api/clipboard/files.

Thin transport over `fused_render.shell.pasteboard` — the routes hold no
platform knowledge at all, only the wire contract:

  GET  -> {paths: [abs…], token: str, supported: bool}
  POST {paths: [abs…]} -> {token: str, supported: bool}

`supported: false` is a 200, not an error: a machine with no clipboard bridge
(no pyobjc, no xclip, a hardened sandbox) is a normal machine, and the
frontend polls this on every return to the app — turning that into a failed
request would mean console noise forever on those machines.

The POST carries the X-Fused guard because it mutates state outside the
browser's reach; the GET doesn't, for the same reason the other read routes
don't (a foreign page can fire a request but can't read the reply).
"""
from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.server.common import _error, _require_fused
from fused_render.shell import pasteboard

router = APIRouter()


@router.get("/api/clipboard/files")
def clipboard_read():
    paths, token, supported = pasteboard.read_files()
    return JSONResponse({"paths": paths, "token": token, "supported": supported})


@router.post("/api/clipboard/files")
def clipboard_write(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    paths = body.get("paths")
    if not isinstance(paths, list):
        return _error("'paths' must be a list of absolute filesystem paths")
    try:
        token, supported = pasteboard.write_files(paths)
    except ValueError as e:
        # A relative or non-string path is a caller bug, not a platform
        # limitation — 400 it rather than hiding it behind supported:false.
        return _error(str(e))
    return JSONResponse({"token": token, "supported": supported})
