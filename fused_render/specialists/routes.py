"""`/api/specialists*`: the Bots page's routes and the session MCP servers' door.

Every route depends on `flag.require` (flag off: the unknown-path 404). Writes need `X-Fused: 1`. Errors are
`{"error": sentence}`: 400 a bad body, 404 an unknown specialist, 409 a taken name or a busy specialist. Routes are
plain `def`s (FastAPI's threadpool): they read files and run git, never on the event loop.
"""
from __future__ import annotations

import functools
import os

from fastapi import APIRouter, Body, Depends, Header, Query
from fastapi.responses import JSONResponse

from fused_render.server.common import _error, _require_fused
from fused_render.specialists import chat, flag, history, launch, notes, services, store, update
from fused_render.specialists import home as hm

router = APIRouter(dependencies=[Depends(flag.require)])
P = "/api/specialists"
FIELDS = ("name", "about", "folder", "face")


def _handled(fn):
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError:
            return _error("no such specialist", 404)
        except (store.Taken, update.Busy) as e:
            return _error(str(e), 409)
        except ValueError as e:
            return _error(str(e), 400)
    return wrap


def _spec(spec_id: str) -> store.Specialist:
    s = store.get(spec_id)
    if s is None:
        raise KeyError(spec_id)
    return s


def _fields(body) -> dict:
    if not isinstance(body, dict) or set(body) - set(FIELDS):
        raise ValueError(f"the body is an object with any of {', '.join(FIELDS)}")
    return body


def _row(s: store.Specialist) -> dict:
    st = chat.load(s.home)
    last = chat.last(s.home, st["main"])
    return {**s.manifest(), "home": s.home, "working": bool(store.live_runs(s.home)),
            "last": {"text": last["text"], "at": last["at"]} if last else None,
            "new": bool(last and last["role"] == "assistant" and last["at"] > st["seen"])}


@router.get(P)
@_handled
def specialists_list():
    return [_row(s) for s in store.all()]


@router.post(P)
@_handled
def specialists_create(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    if guard := _require_fused(x_fused):
        return guard
    f = _fields(body)
    return _row(store.create(str(f.get("name") or ""), str(f.get("about") or ""), str(f.get("folder") or ""),
                             f.get("face")))


@router.patch(P + "/{spec_id}")
@_handled
def specialists_patch(spec_id: str, body: dict = Body(...), x_fused: str | None = Header(default=None)):
    if guard := _require_fused(x_fused):
        return guard
    return _row(store.update(spec_id, **_fields(body)))


@router.delete(P + "/{spec_id}")
@_handled
def specialists_delete(spec_id: str, x_fused: str | None = Header(default=None)):
    if guard := _require_fused(x_fused):
        return guard
    store.delete(spec_id)
    return {"ok": True}


@router.get(P + "/{spec_id}/chat")
@_handled
def specialists_chat(spec_id: str):
    return chat.view(_spec(spec_id).home)


@router.post(P + "/{spec_id}/chat/clear")
@_handled
def specialists_chat_clear(spec_id: str, x_fused: str | None = Header(default=None)):
    if guard := _require_fused(x_fused):
        return guard
    return chat.clear(_spec(spec_id).home)


@router.post(P + "/{spec_id}/seen")
@_handled
def specialists_seen(spec_id: str, x_fused: str | None = Header(default=None)):
    if guard := _require_fused(x_fused):
        return guard
    chat.mark_seen(_spec(spec_id).home)
    return {"ok": True}


@router.get(P + "/{spec_id}/memory")
@_handled
def specialists_memory(spec_id: str):
    return {"text": hm.read_text(os.path.join(_spec(spec_id).home, "memory.md"))}


@router.get(P + "/{spec_id}/skills")
@_handled
def specialists_skills(spec_id: str):
    return {"skills": hm.skills(_spec(spec_id).home)}


@router.get(P + "/{spec_id}/notes")
@_handled
def specialists_notes(spec_id: str):
    return {"notes": notes.list(_spec(spec_id))}


@router.delete(P + "/{spec_id}/notes/{name}")
@_handled
def specialists_note_delete(spec_id: str, name: str, x_fused: str | None = Header(default=None)):
    if guard := _require_fused(x_fused):
        return guard
    return {"ok": True} if notes.delete(_spec(spec_id), name) else _error("no such note", 404)


@router.get(P + "/{spec_id}/changes")
@_handled
def specialists_changes(spec_id: str):
    home = _spec(spec_id).home
    history.commit_edits(home)
    return {"changes": history.changes(home)}


@router.get(P + "/{spec_id}/changes/{sha}")
@_handled
def specialists_change(spec_id: str, sha: str):
    text = history.diff(_spec(spec_id).home, sha)
    return _error("no such change", 404) if text is None else {"diff": text}


@router.post(P + "/{spec_id}/update")
@_handled
def specialists_update(spec_id: str, x_fused: str | None = Header(default=None)):
    if guard := _require_fused(x_fused):
        return guard
    started = update.start(_spec(spec_id))
    return JSONResponse({"started": started}, status_code=202 if started else 200)


# ------------------------------------------------------- session MCP door ---
def _session(spec_id: str, token: str | None, server: str):
    s = _spec(spec_id)
    if not launch.check_token(spec_id, token or ""):
        return s, _error("bad or expired session token", 403)
    if server not in services.SERVERS:
        raise ValueError("unknown server")
    return s, None


@router.get(P + "/{spec_id}/session-tools")
@_handled
def specialists_session_tools(spec_id: str, server: str = Query(""),
                               x_specialist_token: str | None = Header(default=None)):
    s, refused = _session(spec_id, x_specialist_token, server)
    return refused or {"tools": services.mcp_tools(s, server)}


@router.post(P + "/{spec_id}/session-tool")
@_handled
def specialists_session_tool(spec_id: str, body: dict = Body(...), x_fused: str | None = Header(default=None),
                             x_specialist_token: str | None = Header(default=None)):
    if guard := _require_fused(x_fused):
        return guard
    server = str(body.get("server") or "")
    s, refused = _session(spec_id, x_specialist_token, server)
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    return refused or services.call(s, server, str(body.get("name") or ""), args)
