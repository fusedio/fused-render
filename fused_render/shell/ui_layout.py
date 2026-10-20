"""GET/PUT /api/ui-layout — the shell's durable layout at ~/.fused-render/ui_layout.json.

The layout the user arranges and would miss across a relaunch: the sidebar's
width and collapse, the Bookmarks / Current apps section collapses, the
explorer side panel (hidden / width / maximized), each folder's last view
query (`viewState`, path → "?view=…"), the Tasks page's view, calendar range,
board lanes and collapsed groups, and the recently picked paths.

All of it used to sit in localStorage. D1386 moved it here: localStorage is
per ORIGIN, and the desktop app walks ports 1777..1787 then an ephemeral one
(shell/onboarding.py) — every relaunch that lands on another port is a new
origin with empty storage, so the arrangement silently reset. The rule since:
browser storage holds signals, caches and per-tab hand-offs only; anything a
user would miss across a relaunch lives behind an API.

Same posture as home_layout.py: ONE whole document, last-write-wins (the
client hydrates once per document at boot, then writes the whole doc on each
gesture). GET's `exists` tells an absent or corrupt file from a saved one so
the client can fall back to its defaults and run its one-shot import of the
old localStorage keys. PUT validates every known key, DROPS unknown ones
(a newer client's extra key must not break an older server), and answers 400
on a known key with a bad value. Every sub-object is optional.
"""
import os
import re

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

router = APIRouter()

VERSION = 1
SIDEBAR_MIN_W = 180
SIDEBAR_MAX_W = 400
MAX_VIEW_STATE = 500
MAX_PATH = 4096
MAX_QUERY = 512
MAX_RECENT_PATHS = 8
_IDENT_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Duplicated deliberately (see bookmarks.py): shell/ must not import server.
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "ui_layout.json")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_key(v) -> bool:
    return isinstance(v, str) and 0 < len(v) <= MAX_PATH


class _Invalid(Exception):
    pass


def _check(ok: bool) -> None:
    if not ok:
        raise _Invalid


def _clean_tasks(t) -> dict:
    _check(isinstance(t, dict))
    out: dict = {}
    for key in ("view", "calRange"):
        if key in t:
            v = t[key]
            _check(isinstance(v, str) and bool(_IDENT_RE.fullmatch(v)))
            out[key] = v
    return out


def _clean(doc) -> dict:
    """Validated copy of `doc` with unknown keys dropped; raises `_Invalid`
    on a wrong version or any known key carrying a bad value."""
    _check(isinstance(doc, dict) and _is_int(doc.get("v")) and doc["v"] == VERSION)
    out: dict = {"v": VERSION}
    if "sidebar" in doc:
        sb = doc["sidebar"]
        _check(isinstance(sb, dict))
        _check(_is_int(sb.get("width")) and SIDEBAR_MIN_W <= sb["width"] <= SIDEBAR_MAX_W)
        _check(isinstance(sb.get("collapsed"), bool))
        out["sidebar"] = {"width": sb["width"], "collapsed": sb["collapsed"]}
    for key in ("bookmarksCollapsed", "currentAppsCollapsed"):
        if key in doc:
            _check(isinstance(doc[key], bool))
            out[key] = doc[key]
    if "explorerSide" in doc:
        es = doc["explorerSide"]
        _check(isinstance(es, dict))
        side: dict = {}
        for key in ("hidden", "maximized"):
            if key in es:
                _check(isinstance(es[key], bool))
                side[key] = es[key]
        if "width" in es:
            w = es["width"]
            # null is a real value — "no width chosen, use the default" —
            # and round-trips as null rather than being dropped.
            _check(w is None or (_is_int(w) and w >= 0))
            side["width"] = w
        out["explorerSide"] = side
    if "viewState" in doc:
        vs = doc["viewState"]
        _check(isinstance(vs, dict) and len(vs) <= MAX_VIEW_STATE)
        for k, v in vs.items():
            _check(_is_key(k))
            # A query string or nothing: the explorer appends it to a /view/
            # url verbatim, so anything else would be a url fragment we let
            # a client smuggle into navigation.
            _check(isinstance(v, str) and len(v) <= MAX_QUERY
                   and (v == "" or v.startswith("?")))
        out["viewState"] = dict(vs)
    if "tasks" in doc:
        out["tasks"] = _clean_tasks(doc["tasks"])
    if "recentPaths" in doc:
        rp = doc["recentPaths"]
        _check(isinstance(rp, list) and len(rp) <= MAX_RECENT_PATHS)
        _check(all(_is_key(p) for p in rp))
        out["recentPaths"] = list(rp)
    return out


def read_layout() -> dict | None:
    """The stored document, validated, or None when absent or invalid."""
    try:
        return _clean(storage.read_json(_path()))
    except _Invalid:
        return None


@router.get("/api/ui-layout")
def get_ui_layout():
    layout = read_layout()
    if layout is None:
        return {"exists": False, "layout": None}
    return {"exists": True, "layout": layout}


@router.put("/api/ui-layout")
def put_ui_layout(layout=Body(default=None), x_fused: str | None = Header(default=None)):
    # Untyped body: a non-object is our 400, not FastAPI's 422.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        clean = _clean(layout)
    except _Invalid:
        return JSONResponse({"error": "invalid layout"}, status_code=400)
    storage.write_json(_path(), clean)
    return {"ok": True}
