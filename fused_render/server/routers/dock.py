"""`/dock` and `/api/dock*` — the menu-bar Dock over HTTP (fused_render/dock.py).

The tray page (`/dock`, a second Vite page: frontend/dock.html →
shell-dist/dock.html, hosted by menubar_dock.py in a transparent panel under
the status item) reads and writes through these; a plain browser tab on
`/dock` works too, with an HTML context menu instead of the native one.

    GET  /dock                              the tray page
    GET  /api/dock                       -> {kind, pinned, recent, tilesize}
    POST /api/dock/open  {kind, id|path} -> {ok, native: true}                 the app opened it
                                            {ok, native: false, view: <path>}  navigate there yourself
    POST /api/dock/home                  -> {ok, native: true} | {ok, native: false, view: "/"}
    POST /api/dock/reveal {path}         -> {ok}                `open -R` (apps only)
    POST /api/dock/pin    {path, pinned} -> {ok, pinned}        app pins (dock.json)
    POST /api/dock/pin    {id, pinned}   -> {ok, id, pinned}    bot pins (bot.json)
    POST /api/dock/order  {paths}        -> {ok, pinned}        the pinned apps' new left-to-right order
    POST /api/dock/size   {tilesize}     -> {ok, tilesize}      clamped to 16..128, persisted

`native` is whether the macOS app took the action through
`window_policy.native_hooks` ("dock_open", "show_home"; app.py installs them
beside the window manager); under `fused-render serve`, or in a browser tab,
it did not, and `view` is the shell path to go to instead.

Same conventions as the other routers: reads are plain GETs, every POST
needs `X-Fused: 1`, and a bad request is `{"error": "<sentence>"}` with a 400.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Body, Header
from fastapi.responses import FileResponse, PlainTextResponse

from fused_render import dock, window_policy
from fused_render.server.common import STATIC_DIR, _error, _require_fused

router = APIRouter()


@router.get("/dock")
def dock_page():
    page = os.path.join(STATIC_DIR, "shell-dist", "dock.html")
    if not os.path.isfile(page):
        return PlainTextResponse(
            "dock page not built (frontend/dock.html → shell-dist/dock.html); "
            "run: cd frontend && npm run build", status_code=503)
    return FileResponse(page, headers={"Cache-Control": "no-store"})


def _guarded_body(body, x_fused):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard, {}
    return None, (body if isinstance(body, dict) else {})


@router.get("/api/dock")
def dock_get():
    out = dock.entries()
    out["tilesize"] = dock.tilesize()
    return out


@router.post("/api/dock/open")
def dock_open(body: dict = Body(default=None), x_fused: str | None = Header(default=None)):
    guard, body = _guarded_body(body, x_fused)
    if guard is not None:
        return guard
    kind = body.get("kind")
    if kind == "bot":
        bid = str(body.get("id") or "")
        if not dock.bot_exists(bid):
            return _error(f"no such bot {bid}" if bid else "no bot id given", 400)
        key, view = bid, dock.bot_view_path(bid)
    elif kind in ("app", "appfile"):
        row = dock.row_for_path(body.get("path"))
        if row is None:
            return _error("not an app this machine knows", 400)
        # The real path, so a window already showing the app (keyed on the
        # path the shell opened it with) is found and raised, not doubled.
        kind, key, view = row["kind"], row["path"], row["url"]
    else:
        return _error('kind must be "bot", "app" or "appfile"', 400)
    hook = window_policy.native_hooks.get("dock_open")
    if hook is not None:
        hook(kind, key)
        return {"ok": True, "native": True}
    return {"ok": True, "native": False, "view": view}


@router.post("/api/dock/home")
def dock_home(body: dict = Body(default=None), x_fused: str | None = Header(default=None)):
    guard, _body = _guarded_body(body, x_fused)
    if guard is not None:
        return guard
    hook = window_policy.native_hooks.get("show_home")
    if hook is not None:
        hook()
        return {"ok": True, "native": True}
    return {"ok": True, "native": False, "view": "/"}


@router.post("/api/dock/reveal")
def dock_reveal(body: dict = Body(default=None), x_fused: str | None = Header(default=None)):
    guard, body = _guarded_body(body, x_fused)
    if guard is not None:
        return guard
    try:
        dock.reveal(body.get("path"))
    except ValueError as e:
        return _error(str(e) or "bad request", 400)
    return {"ok": True}


@router.post("/api/dock/pin")
def dock_pin(body: dict = Body(default=None), x_fused: str | None = Header(default=None)):
    guard, body = _guarded_body(body, x_fused)
    if guard is not None:
        return guard
    pinned = bool(body.get("pinned"))
    if "id" in body:
        bid = str(body.get("id") or "")
        if not dock.bot_exists(bid):
            return _error(f"no such bot {bid}" if bid else "no bot id given", 400)
        try:
            value = dock.set_bot_pinned(bid, pinned)
        except ValueError as e:
            return _error(str(e) or "bad request", 400)
        return {"ok": True, "id": bid, "pinned": value}
    try:
        pins = dock.set_pinned(body.get("path"), pinned)
    except ValueError as e:
        return _error(str(e) or "bad request", 400)
    return {"ok": True, "pinned": pins}


@router.post("/api/dock/order")
def dock_order(body: dict = Body(default=None), x_fused: str | None = Header(default=None)):
    guard, body = _guarded_body(body, x_fused)
    if guard is not None:
        return guard
    try:
        pins = dock.set_order(body.get("paths"))
    except ValueError as e:
        return _error(str(e) or "bad request", 400)
    return {"ok": True, "pinned": pins}


@router.post("/api/dock/size")
def dock_size(body: dict = Body(default=None), x_fused: str | None = Header(default=None)):
    guard, body = _guarded_body(body, x_fused)
    if guard is not None:
        return guard
    try:
        n = dock.set_tilesize(body.get("tilesize"))
    except ValueError as e:
        return _error(str(e) or "bad request", 400)
    return {"ok": True, "tilesize": n}
