"""`/api/experts/*`: the experts' HTTP API (docs/experts.md).

    GET  /api/experts                         [{"id","name","ask_me_about"}]          (expert_list)
    POST /api/experts/context {expert, run_id} memory.md as text + the skill files      (expert_context; records use)
    POST /api/experts/ask {expert, question, run_id}  an ask session's answer          (expert_ask; records use)
    GET  /api/experts/{id}/memory             the file, its size against the limit, who changed it last
    GET  /api/experts/{id}/sessions           the side-session rows, newest first, without transcripts
    GET  /api/experts/{id}/sessions/{sid}     one full row

Owned by the bots owner (bots/routes.OWNED_PREFIXES) and forwarded there. POSTs need `X-Fused: 1`. Errors are
`{"error": "<sentence>"}`: an unknown expert 404, a bad body 400, the bots workspace held elsewhere 409. `ask`
is a sync `def` (FastAPI's threadpool): the harness session it waits on never blocks the event loop.
"""
from __future__ import annotations

import functools
import json

from fastapi import APIRouter, Body, Header, Response

from fused_render.experts import learn, sessions, store
from fused_render.server.common import _error, _require_fused

router = APIRouter()
PREFIX = "/api/experts"
QUESTION_MAX = 4000


class _NotFound(Exception):
    pass


def _handled(fn):
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _NotFound as e:
            return _error(str(e), 404)
        except ValueError as e:
            from fused_render.bots import registry
            if isinstance(e, registry.WorkspaceBusy):
                return Response(json.dumps({"error": str(e), "reason": "workspace_busy", "holder": e.holder}),
                                status_code=409, media_type="application/json")
            return _error(str(e) or "bad request", 400)
    return wrap


def _expert(key):
    bot = store.find(key)
    if bot is None:
        raise _NotFound(f"no such expert: {store.line(key, 80) or '(none given)'}")
    return bot


def _body(body) -> dict:
    if not isinstance(body, dict):
        raise ValueError("the request body must be a JSON object")
    return body


@router.get(PREFIX)
@_handled
def experts_list():
    return [{"id": b.id, "name": store.name(b), "ask_me_about": store.manifest(b)["ask_me_about"]}
            for b in store.all_experts()]


@router.post(PREFIX + "/context")
@_handled
def expert_context(body=Body(None), x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    body = _body(body)
    bot = _expert(body.get("expert"))
    from fused_render.bots import registry
    registry.require_workspace()  # record_use writes into the expert's folder, as ask does
    learn.record_use(bot, body.get("run_id"))
    return {"id": bot.id, "name": store.name(bot), "memory": store.memory(bot), "memory_path": store.memory_path(bot),
            "skills": store.skills(bot)}


@router.post(PREFIX + "/ask")
@_handled
def expert_ask(body=Body(None), x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    body = _body(body)
    bot = _expert(body.get("expert"))
    question = str(body.get("question") or "").strip()
    if not question:
        raise ValueError("ask a question: `question` is empty")
    if len(question) > QUESTION_MAX:
        raise ValueError(f"the question is too long (at most {QUESTION_MAX} characters)")
    from fused_render.bots import registry
    registry.require_workspace()  # the ask writes a session row into the expert's folder
    learn.record_use(bot, body.get("run_id"))
    who = learn.caller(str(body.get("run_id") or ""))
    asker = " ".join(p for p in (f"a {who['backend']} task session" if who["backend"] else "",
                                 f"working in {who['target']}" if who["target"] else "") if p)
    row = sessions.ask(bot, question, asker, caller=who)
    if row.get("status") != "done":
        return _error(f"{store.name(bot)} could not answer: {row.get('error') or 'the session failed'}", 502)
    return {"id": bot.id, "name": store.name(bot), "answer": row.get("answer") or "", "session": row["id"]}


@router.get(PREFIX + "/{eid}/memory")
@_handled
def expert_memory(eid: str):
    bot = _expert(eid)
    text = store.memory(bot)
    lines, nbytes = len(text.splitlines()), len(text.encode("utf-8"))
    return {"text": text, "path": store.memory_path(bot), "lines": lines, "bytes": nbytes,
            "limit_lines": sessions.LIMIT_LINES, "limit_bytes": sessions.LIMIT_BYTES,
            "edited_by_hand": sessions.edited_by_hand(bot), "changes": sessions.changes(bot)}


@router.get(PREFIX + "/{eid}/sessions")
@_handled
def expert_sessions(eid: str):
    return {"sessions": sessions.rows(_expert(eid))}


@router.get(PREFIX + "/{eid}/sessions/{sid}")
@_handled
def expert_session(eid: str, sid: str):
    row = sessions.row(_expert(eid), sid)
    if row is None:
        return _error(f"no such session: {store.line(sid, 80)}", 404)
    return row
