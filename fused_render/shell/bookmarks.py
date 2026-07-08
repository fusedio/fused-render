"""GET/PUT /api/bookmarks — the bookmark tree at ~/.fused-render/bookmarks.json.

Server-side successor to the old localStorage store (DECISIONS D21 anticipated
this move as a "trivial export"). Whole-file, last-write-wins: the tree is tiny
(a handful of items, one level of folders) so there is no partial-update API —
the shell PUTs the entire tree on each mutation.

The GET `exists` flag is load-bearing: it is false only until the file is first
written, letting the shell run its one-time localStorage import exactly once
(a user who later deletes every bookmark leaves an existing `[]` file, so the
old localStorage data is never re-imported). See frontend lib/bookmarks.ts.
"""
import os

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

router = APIRouter()


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused (a custom header forces a CORS
    # preflight that fails cross-origin, blocking a blind foreign PUT).
    # Duplicated deliberately: shell/ must not import server (no server<->shell
    # cycle — the router is imported the other way in create_app).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "bookmarks.json")


@router.get("/api/bookmarks")
def get_bookmarks():
    data = storage.read_json(_path())
    # Absent or corrupt (not a list) -> report not-yet-written so the shell may
    # import from localStorage; a valid file (even []) reports exists=true.
    if not isinstance(data, list):
        return {"exists": False, "bookmarks": []}
    return {"exists": True, "bookmarks": data}


@router.put("/api/bookmarks")
def put_bookmarks(
    bookmarks: list = Body(...), x_fused: str | None = Header(default=None)
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    storage.write_json(_path(), bookmarks)
    return {"ok": True, "count": len(bookmarks)}
