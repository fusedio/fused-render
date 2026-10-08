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
# version-1 document. Versions 1 and 2 carry no coordinates; version 3 gives
# every widget an explicit top-left cell (x, y) on a 4-column grid; version 4
# is the same with half-cell units (an 8-unit grid, a 1x1 is 2 x 2 units). The
# client migrates older documents (3 doubles) and writes 4 on its next change;
# the server keeps whichever version it was given and never migrates. Version 4
# may carry an explicit `cols`/`rows` footprint in units (an edge-dragged size);
# without them the size's own dims apply.
VERSIONS = {1, 2, 3, 4}
MIN_UNITS = 2
MAX_WIDGET_UNIT_ROWS = 8
GRID_COLS = 4
MAX_ROWS = 64
GRID_UNITS = 8
MAX_UNIT_ROWS = 128
_DIMS = {"1x1": (1, 1), "2x1": (2, 1), "1x2": (1, 2), "2x2": (2, 2), "4x1": (4, 1)}
_UNIT_DIMS = {k: (c * 2, r * 2) for k, (c, r) in _DIMS.items()}
SOURCES = {"search", "build", "apps", "playground", "sessions", "recents", "tasks", "bots", "folder", "index", "app"}
SIZES = {"1x1", "2x1", "1x2", "2x2", "4x1"}
FORMATS = {"cards", "list", "icons", "board", "count", "live", "bar"}
APPS_SORTS = {"opened", "updated", "name"}


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Duplicated deliberately (see bookmarks.py): shell/ must not import server.
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "home_layout.json")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _clean(doc) -> dict | None:
    """Structurally cleaned copy of `doc` (unknown keys, sources, sizes, formats,
    version), or None if invalid. Version-3/4 x/y pass through when both are
    integers and are dropped otherwise; placement is _placement_ok's job."""
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
        if doc["version"] in (3, 4) and _is_int(w.get("x")) and _is_int(w.get("y")):
            item["x"], item["y"] = w["x"], w["y"]
        if doc["version"] == 4 and _is_int(w.get("cols")) and _is_int(w.get("rows")):
            item["cols"], item["rows"] = w["cols"], w["rows"]
        fid = w.get("folderId")
        if isinstance(fid, str):
            item["folderId"] = fid
        ap = w.get("appPath")
        if w["source"] == "app" and isinstance(ap, str) and 0 < len(ap) <= MAX_APP_PATH:
            item["appPath"] = ap
        sort = w.get("sort")
        if w["source"] == "apps" and sort in APPS_SORTS:
            item["sort"] = sort
        out.append(item)
    return {"version": doc["version"], "widgets": out}


def _placement_ok(clean: dict) -> bool:
    """Version-3/4 widgets all carry in-bounds, non-overlapping cells (version 3
    in cells, version 4 in half-cell units). PUT only: GET hands a stored
    document to the client, which repairs it on load."""
    if clean["version"] == 3:
        dims, cols, max_rows = _DIMS, GRID_COLS, MAX_ROWS
    elif clean["version"] == 4:
        dims, cols, max_rows = _UNIT_DIMS, GRID_UNITS, MAX_UNIT_ROWS
    else:
        return True
    taken: set[tuple[int, int]] = set()
    for w in clean["widgets"]:
        if "x" not in w:
            return False
        x, y = w["x"], w["y"]
        c, r = dims[w["size"]]
        if clean["version"] == 4 and "cols" in w:
            c, r = w["cols"], w["rows"]
            if not (MIN_UNITS <= c <= GRID_UNITS and MIN_UNITS <= r <= MAX_WIDGET_UNIT_ROWS):
                return False
        if x < 0 or x + c > cols or y < 0 or y + r > max_rows:
            return False
        cells = {(x + i, y + j) for i in range(c) for j in range(r)}
        if cells & taken:
            return False
        taken |= cells
    return True


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
    if clean is None or not _placement_ok(clean):
        return JSONResponse({"error": "invalid layout"}, status_code=400)
    storage.write_json(_path(), clean)
    return {"ok": True, "count": len(clean["widgets"])}
