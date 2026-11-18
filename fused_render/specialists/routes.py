"""`/api/specialists*`: the core HTTP API (PLAN B.5 "Core").

Every route depends on `flag.require` (flag off: 404 `{"error": "not found"}`, as if the route did not exist).
POSTs need `X-Fused: 1` (PUT/PATCH/DELETE are preflighted by the browser anyway). Errors are `{"error": sentence}`:
400 a bad body, 404 an unknown specialist, 409 a conflict. Routes are plain `def`s (FastAPI's threadpool): they
read files and run git, never on the event loop.

Literal paths (`/policy`) are declared before `/{spec_id}` so the parameter route never swallows them; stream 2's
literal task-side paths are included before this router in server/app.py for the same reason.
"""
from __future__ import annotations

import functools
import os
import time

from fastapi import APIRouter, Body, Depends, Header, Query

from fused_render.server.common import _error, _require_fused
from fused_render.specialists import chat, flag, harness, history, launch, services, store
from fused_render.specialists import home as hm

router = APIRouter(dependencies=[Depends(flag.require)])
PREFIX = "/api/specialists"
SERVERS = ("specialist", "specialist_services")
NOTE_KINDS = ("gap", "correction", "convention", "suspected-bug", "behaviour", "case")
BASES = ("user-said", "verified", "guess")


class _NotFound(Exception):
    pass


def _handled(fn):
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _NotFound as e:
            return _error(str(e), 404)
        except KeyError:
            return _error("no such specialist", 404)
        except ValueError as e:
            return _error(str(e) or "bad request", 400)
    return wrap


def _spec(spec_id: str) -> store.Specialist:
    s = store.get(spec_id)
    if s is None:
        raise _NotFound("no such specialist")
    return s


def _body(body) -> dict:
    if not isinstance(body, dict):
        raise ValueError("the request body must be a JSON object")
    return body


def _working(s: store.Specialist) -> bool:
    return bool(store.live_runs(s.home))


def _status(s: store.Specialist, working: bool) -> str:
    if working:
        return "Working now"
    rows = [r for r in harness.rows(s.id, 5) if r.get("kind") == "nightly" and r.get("status") == "done"]
    if rows:
        return "Updated " + time.strftime("%b %d", time.localtime(rows[0].get("ended") or rows[0]["started"]))
    return "Ready"


def _row(s: store.Specialist) -> dict:
    w = _working(s)
    return {"id": s.id, "name": s.name, "subject": s.subject, "face": s.face, "status": _status(s, w), "working": w}


def _detail(s: store.Specialist) -> dict:
    return {"id": s.id, "name": s.name, "subject": s.subject, "face": s.face, "model": s.model, "effort": s.effort,
            "triggers": s.triggers, "home": s.home, "connections": s.connections, "created": s.created,
            "shipped": s.shipped, "status": _status(s, _working(s)),
            **({} if history.available() else {"history": history.STATUS_OFF})}


# ------------------------------------------------------------- literal paths ---
@router.get(PREFIX + "/policy")
@_handled
def specialists_policy(specialist: str = Query(""), kind: str = Query(""), session_id: str = Query(""),
                       tool: str = Query("")):
    return {"answer": services.policy(specialist, kind, session_id, tool)}


# ------------------------------------------------------------------- list ---
@router.get(PREFIX)
@_handled
def specialists_list():
    return [_row(s) for s in store.all()]


@router.post(PREFIX)
@_handled
def specialists_create(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard:
        return guard
    b = _body(body)
    try:
        s = store.create(str(b.get("name") or ""), str(b.get("subject") or ""), triggers=b.get("triggers"))
    except store.Invalid as e:
        msg = str(e)
        return _error(msg, 409 if "already exists" in msg else 400)
    return _detail(s)


@router.get(PREFIX + "/{spec_id}")
@_handled
def specialists_get(spec_id: str):
    return _detail(_spec(spec_id))


@router.patch(PREFIX + "/{spec_id}")
@_handled
def specialists_patch(spec_id: str, body: dict = Body(...)):
    _spec(spec_id)
    b = _body(body)
    fields = {k: b[k] for k in ("name", "subject", "face", "model", "effort", "triggers") if k in b}
    unknown = set(b) - set(fields)
    if unknown:
        return _error(f"unknown field(s): {', '.join(sorted(unknown))}", 400)
    try:
        s = store.update(spec_id, **fields)
    except store.Invalid as e:
        msg = str(e)
        return _error(msg, 409 if "already exists" in msg else 400)
    return _detail(s)


@router.delete(PREFIX + "/{spec_id}")
@_handled
def specialists_delete(spec_id: str):
    _spec(spec_id)
    store.delete(spec_id)
    return {"ok": True}


# ------------------------------------------------------------------- chat ---
@router.get(PREFIX + "/{spec_id}/chat")
@_handled
def specialists_chat(spec_id: str):
    _spec(spec_id)
    return chat.state(spec_id)


@router.post(PREFIX + "/{spec_id}/chat/clear")
@_handled
def specialists_chat_clear(spec_id: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard:
        return guard
    _spec(spec_id)
    return chat.clear(spec_id)


# ---------------------------------------------------------------- charter ---
def _charter_path(s) -> str:
    return os.path.join(s.home, "CLAUDE.md")


@router.get(PREFIX + "/{spec_id}/charter")
@_handled
def specialists_charter(spec_id: str):
    s = _spec(spec_id)
    return {"text": hm.read_text(_charter_path(s)), "path": _charter_path(s)}


@router.put(PREFIX + "/{spec_id}/charter")
@_handled
def specialists_charter_put(spec_id: str, body: dict = Body(...)):
    s = _spec(spec_id)
    text = _body(body).get("text")
    if not isinstance(text, str) or not text.strip():
        return _error("'text' must be a non-empty string", 400)
    if len(text) > hm.CHARTER_MAX:
        return _error(f"the charter is limited to {hm.CHARTER_MAX:,} characters", 400)
    hm.write_text(_charter_path(s), hm.with_imports(text))
    history.commit(s.id, "settings: charter")
    return {"text": hm.read_text(_charter_path(s)), "path": _charter_path(s)}


# ----------------------------------------------------------------- skills ---
def _skill_name(name: str) -> str:
    if not hm.skill_name_ok(name):
        raise ValueError("a skill name is lowercase letters, digits and dashes (at most 64)")
    return name


@router.get(PREFIX + "/{spec_id}/skills")
@_handled
def specialists_skills(spec_id: str):
    return {"skills": hm.skills(_spec(spec_id).home)}


@router.get(PREFIX + "/{spec_id}/skills/{name}")
@_handled
def specialists_skill(spec_id: str, name: str):
    s = _spec(spec_id)
    p = hm.skill_path(s.home, _skill_name(name))
    if not os.path.isfile(p):
        raise _NotFound("no such skill")
    return {"name": name, "text": hm.read_text(p)}


@router.put(PREFIX + "/{spec_id}/skills/{name}")
@_handled
def specialists_skill_put(spec_id: str, name: str, body: dict = Body(...)):
    s = _spec(spec_id)
    _skill_name(name)
    text = _body(body).get("text")
    if not isinstance(text, str) or not text.strip():
        return _error("'text' must be a non-empty string", 400)
    if len(text) > hm.SKILL_MAX:
        return _error(f"a skill is limited to {hm.SKILL_MAX:,} characters", 400)
    if not hm.frontmatter(text).get("description"):
        # Claude Code finds a skill by its frontmatter; give a bare text one, described by its first line.
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), name)
        text = hm.skill_text(name, first[:300], text)
    hm.write_text(hm.skill_path(s.home, name), text)
    history.commit(s.id, f"settings: skill {name}")
    return {"name": name, "text": hm.read_text(hm.skill_path(s.home, name))}


@router.delete(PREFIX + "/{spec_id}/skills/{name}")
@_handled
def specialists_skill_delete(spec_id: str, name: str):
    s = _spec(spec_id)
    p = hm.skill_path(s.home, _skill_name(name))
    if not os.path.isfile(p):
        raise _NotFound("no such skill")
    folder = os.path.dirname(p)
    for root, dirs, files in os.walk(folder, topdown=False):
        for f in files:
            os.remove(os.path.join(root, f))
        for d in dirs:
            os.rmdir(os.path.join(root, d))
    os.rmdir(folder)
    history.commit(s.id, f"settings: delete skill {name}")
    return {"ok": True}


# ----------------------------------------------------------------- people ---
@router.get(PREFIX + "/{spec_id}/people")
@_handled
def specialists_people(spec_id: str):
    s = _spec(spec_id)
    return {"rows": hm.people_rows(s.home), "path": os.path.join(s.home, "people.md")}


@router.put(PREFIX + "/{spec_id}/people")
@_handled
def specialists_people_put(spec_id: str, body: dict = Body(...)):
    s = _spec(spec_id)
    rows = _body(body).get("rows")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return _error("'rows' must be a list of objects", 400)
    hm.write_people(s.home, rows[:500])
    history.commit(s.id, "settings: people")
    return {"rows": hm.people_rows(s.home), "path": os.path.join(s.home, "people.md")}


# ------------------------------------------------------------ connections ---
@router.get(PREFIX + "/{spec_id}/connections")
@_handled
def specialists_connections(spec_id: str):
    return services.connections(_spec(spec_id))


@router.put(PREFIX + "/{spec_id}/connections")
@_handled
def specialists_connections_put(spec_id: str, body: dict = Body(...)):
    _spec(spec_id)
    b = _body(body)
    add, remove = b.get("add") or [], b.get("remove") or []
    if not isinstance(add, list) or not isinstance(remove, list):
        return _error("'add' and 'remove' must be lists", 400)
    services.set_allowed(spec_id, add=[str(x) for x in add], remove=[str(x) for x in remove])
    return services.connections(_spec(spec_id))


# ------------------------------------------------------------ history/runs ---
@router.get(PREFIX + "/{spec_id}/history")
@_handled
def specialists_history(spec_id: str, path: str = Query(""), limit: int = Query(50)):
    _spec(spec_id)
    if path and (os.path.isabs(path) or ".." in path.replace("\\", "/").split("/")):
        return _error("'path' must be relative to the specialist's home", 400)
    return {"commits": history.log(spec_id, path or None, max(1, min(int(limit), 500)))}


@router.get(PREFIX + "/{spec_id}/runs")
@_handled
def specialists_runs(spec_id: str, limit: int = Query(100)):
    _spec(spec_id)
    return {"runs": harness.rows(spec_id, max(1, min(int(limit), 300)))}


@router.get(PREFIX + "/{spec_id}/runs/{rid}")
@_handled
def specialists_run(spec_id: str, rid: str):
    _spec(spec_id)
    r = harness.row(spec_id, rid)
    if r is None:
        raise _NotFound("no such run")
    return r


# ------------------------------------------------------- session MCP door ---
def _session(spec_id: str, kind: str, token: str, server: str):
    s = _spec(spec_id)
    if server not in SERVERS:
        raise ValueError("unknown server")
    rec = launch.check_token(spec_id, kind, token)
    if rec is None:
        return s, None
    return s, rec


NOTE_TOOL_DEF = {
    "name": "note",
    "description": ("Send a note to your own inbox: something you got wrong, missed, a convention the user stated, "
                    "a suspected bug, or a case worth remembering. The nightly update weighs it. Never write files "
                    "under inbox/ yourself."),
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["kind", "claim", "basis"],
                    "properties": {
                        "kind": {"type": "string", "enum": list(NOTE_KINDS)},
                        "claim": {"type": "string", "maxLength": 300, "description": "one sentence"},
                        "evidence": {"type": "string", "maxLength": 4000},
                        "basis": {"type": "string", "enum": list(BASES)},
                        "entry": {"type": "string", "description": "the memory entry id (m-xxxx) it concerns"}}},
}


@router.get(PREFIX + "/{spec_id}/session-tools")
@_handled
def specialists_session_tools(spec_id: str, kind: str = Query(""), token: str = Query(""), server: str = Query("")):
    s, rec = _session(spec_id, kind, token, server)
    if rec is None:
        return _error("bad or expired session token", 403)
    if server == "specialist":
        return {"tools": [NOTE_TOOL_DEF] if kind in ("main", "breakout") else []}
    return {"tools": services.mcp_tools(s, kind)}


def _text(t: str, err: bool = False) -> dict:
    return {"content": [{"type": "text", "text": t}], "isError": err}


def _session_id_for(spec_id: str, kind: str, rec: dict) -> str:
    """The session a session MCP server belongs to: the breakout's recorded session, or main chat's current."""
    if kind == "breakout" and rec.get("breakout_id"):
        try:
            from fused_render.specialists import breakouts

            b = breakouts.get(spec_id, rec["breakout_id"])
            return str((b or {}).get("session_id") or "")
        except Exception:  # noqa: BLE001
            return ""
    if kind == "main":
        return chat.state(spec_id).get("current") or ""
    return ""


def _note(s, kind: str, rec: dict, session_id: str, args: dict) -> dict:
    k, claim, basis = args.get("kind"), str(args.get("claim") or "").strip(), args.get("basis")
    if k not in NOTE_KINDS:
        return _text(f"error: kind must be one of {', '.join(NOTE_KINDS)}", True)
    if basis not in BASES:
        return _text(f"error: basis must be one of {', '.join(BASES)}", True)
    if not claim:
        return _text("error: claim is required (one sentence)", True)
    try:
        from fused_render.specialists import inbox

        note = inbox.add(s.id, kind=k, claim=claim[:300], basis=basis, evidence=str(args.get("evidence") or "")[:4000],
                         entry=(str(args.get("entry")) if args.get("entry") else None),
                         source={"type": kind, "session_id": session_id or _session_id_for(s.id, kind, rec),
                                 "breakout_id": rec.get("breakout_id") or ""})
    except Exception as e:  # noqa: BLE001 — stream 3's inbox missing or refusing
        return _text(f"error: the note was not saved ({e})", True)
    return _text(f"Saved to your inbox ({note.get('id', '')}).")


@router.post(PREFIX + "/{spec_id}/session-tool")
@_handled
def specialists_session_tool(spec_id: str, body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard:
        return guard
    b = _body(body)
    kind, server, name = str(b.get("kind") or ""), str(b.get("server") or ""), str(b.get("name") or "")
    s, rec = _session(spec_id, kind, str(b.get("token") or ""), server)
    if rec is None:
        return _error("bad or expired session token", 403)
    args = b.get("args") if isinstance(b.get("args"), dict) else {}
    if server == "specialist":
        if name != "note" or kind not in ("main", "breakout"):
            return _text(f"error: no tool {name!r} here", True)
        return _note(s, kind, rec, str(b.get("session_id") or ""), args)
    return services.call(s, kind, name, args)
