"""GET/PUT /api/home/layout — the Home widget grid at ~/.fused-render/home_layout.json.

Whole-document, last-write-wins (same posture as bookmarks.py). GET `exists`
distinguishes an absent/corrupt file from a saved one so the frontend can fall
back to its default layout. PUT validates the shape and drops unknown keys.
"""
import os

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

router = APIRouter()

MAX_WIDGETS = 48
MAX_APP_PATH = 4096
# Version 1 predates the search widget; the client prepends one when it loads a
# version-1 document and stamps 2. The server keeps whichever version it was given.
VERSIONS = {1, 2}
SOURCES = {"search", "build", "apps", "playground", "sessions", "recents", "tasks", "bots", "folder", "index", "app", "spacer"}
SIZES = {"1x1", "2x1", "1x2", "2x2", "4x1"}
FORMATS = {"cards", "list", "icons", "board", "count", "live", "bar", "blank"}


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Duplicated deliberately (see bookmarks.py): shell/ must not import server.
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "home_layout.json")


def _clean(doc) -> dict | None:
    """Validated copy of `doc` with unknown keys dropped, or None if invalid."""
    if not isinstance(doc, dict) or doc.get("version") not in VERSIONS:
        return None
    widgets = doc.get("widgets")
    if not isinstance(widgets, list) or len(widgets) > MAX_WIDGETS:
        return None
    out = []
    for w in widgets:
        if not isinstance(w, dict):
            return None
        if not isinstance(w.get("id"), str):
            return None
        if w.get("source") not in SOURCES or not isinstance(w.get("source"), str):
            return None
        if w.get("size") not in SIZES or w.get("format") not in FORMATS:
            return None
        item = {k: w[k] for k in ("id", "source", "size", "format")}
        fid = w.get("folderId")
        if isinstance(fid, str):
            item["folderId"] = fid
        ap = w.get("appPath")
        if w["source"] == "app" and isinstance(ap, str) and 0 < len(ap) <= MAX_APP_PATH:
            item["appPath"] = ap
        out.append(item)
    return {"version": doc["version"], "widgets": out}


@router.get("/api/home/layout")
def get_home_layout():
    layout = _clean(storage.read_json(_path()))
    if layout is None:
        return {"exists": False, "layout": None}
    return {"exists": True, "layout": layout}


@router.put("/api/home/layout")
def put_home_layout(
    layout: dict = Body(...), x_fused: str | None = Header(default=None)
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    clean = _clean(layout)
    if clean is None:
        return JSONResponse({"error": "invalid layout"}, status_code=400)
    storage.write_json(_path(), clean)
    return {"ok": True, "count": len(clean["widgets"])}
