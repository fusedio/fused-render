"""`/api/experts/*` — the experts' HTTP API (docs/experts.md §8).

A FastAPI router included from fused_render/server/app.py. Reads are plain
GETs; every POST needs `X-Fused: 1` (`server.common._require_fused`). Errors
are `{"error": "<sentence>"}`: an unknown expert -> 404, `ValueError` /
`RuntimeError` -> 400, the bots workspace held by another fused-render -> 409,
no engine for a consult -> 409 with `reason: "unavailable"`.

Every endpoint that may call a model (consult, consolidate, capture) is a sync
`def`, so FastAPI runs it on its threadpool and the event loop never waits on
one. The expert modules are imported lazily and reached through their module
(`_mod("consult").consult(...)`), so the server imports this router without
pulling in the bots' Chrome code and tests can monkeypatch a module function.
"""
from __future__ import annotations

import functools
import importlib
import json
import logging
import os
import re

from fastapi import APIRouter, Body, Header, Response

from fused_render.server.common import _error, _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()

PREFIX = "/api/experts"
ENGINE_IDS = ("claude", "codex", "copilot")
ENGINE_CHOICES = ("auto",) + ENGINE_IDS
CONSULTS_LIMIT = 100
#: A run id as `agent._start` mints it (`YYYYmmdd-HHMMSS-<hex>`), loosely: a
#: name that cannot climb out of the runs root.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


def _mod(name: str):
    return importlib.import_module("fused_render.experts." + name)


class _NotFound(Exception):
    pass


class _Unavailable(Exception):
    pass


def _handled(fn):
    """`bots/routes._handled`, for this router: an unknown expert -> 404,
    WorkspaceBusy -> 409, model.Unavailable -> 409 `unavailable`,
    ValueError / RuntimeError -> 400."""
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _NotFound as e:
            return _error(str(e) or "no such expert", 404)
        except _Unavailable as e:
            return Response(json.dumps({"error": str(e), "reason": "unavailable"}),
                            status_code=409, media_type="application/json")
        except Exception as e:  # noqa: BLE001 — classified below, re-raised otherwise
            from fused_render.bots import registry
            if isinstance(e, registry.WorkspaceBusy):
                return Response(json.dumps({"error": str(e), "reason": "workspace_busy",
                                            "holder": e.holder}),
                                status_code=409, media_type="application/json")
            unavailable = getattr(_mod("model"), "Unavailable", None)
            if unavailable is not None and isinstance(e, unavailable):
                return Response(json.dumps({"error": str(e) or "no engine is available for this expert",
                                            "reason": "unavailable"}),
                                status_code=409, media_type="application/json")
            if isinstance(e, (ValueError, RuntimeError)):
                return _error(str(e) or e.__class__.__name__, 400)
            raise
    return wrap


def _expert(eid):
    bot = _mod("store").find(eid)
    if bot is None:
        raise _NotFound(f"no such expert: {eid}")
    return bot


def _writable():
    """The bots workspace gate (`bots/routes._ws`): a home without the
    workspace claim is read-only for bot state."""
    from fused_render.bots import registry
    registry.require_workspace()


def _body_dict(body) -> dict:
    if not isinstance(body, dict):
        raise ValueError("the request body must be a JSON object")
    return body


def _name(bot) -> str:
    return str((getattr(bot, "meta", None) or {}).get("name") or bot.id)


# ------------------------------------------------------------------ listing ---
@router.get(PREFIX)
@_handled
def experts_list():
    store, model = _mod("store"), _mod("model")
    out = []
    for bot in store.all_experts():
        man = store.manifest(bot)
        try:
            ok, reason = model.available(bot)
        except Exception as e:  # noqa: BLE001 — one expert's engine check must not hide the rest
            ok, reason = False, str(e)
        out.append({"id": bot.id, "name": _name(bot), "ask_me_about": man.get("ask_me_about") or "",
                    "engine": man.get("engine") or "auto", "model": man.get("model") or "",
                    "available": bool(ok), "reason": "" if ok else (reason or "")})
    return out


# ------------------------------------------------------------------ consult ---
def _agent():
    from fused_render.claude_agent import agent_module
    return agent_module()


def caller_for_run(run_id) -> dict:
    """`{"run_id","session_id","backend","target","canvas"}` for the agent run
    `run_id` names, as far as its run dir says (docs/experts.md §4 consult).
    `target` is the folder the session works in, `canvas` the canvas clone it
    is inside ("" when none). Never raises: an unknown run is a caller with
    only its run id."""
    caller = {"run_id": str(run_id or ""), "session_id": "", "backend": "", "target": "", "canvas": ""}
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id) or run_id in (".", ".."):
        return caller
    try:
        agent = _agent()
        run_dir = os.path.join(agent.RUNS, run_id)
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        if not isinstance(meta, dict):
            return caller
        file = str(meta.get("file") or "")
        caller["backend"] = str(meta.get("backend") or "claude")
        try:
            sid = agent._run_own_session(run_dir, meta)
        except Exception:  # noqa: BLE001
            sid = ""
        caller["session_id"] = str(sid or meta.get("session_id") or meta.get("resumed_from") or "")
        if file:
            caller["target"] = file if os.path.isdir(file) else os.path.dirname(file)
            caller["canvas"] = canvas_clone(file)
    except Exception:  # noqa: BLE001 — a caller we cannot describe still gets its answer
        logger.debug("experts: could not read run %s", run_id, exc_info=True)
    return caller


def canvas_clone(path) -> str:
    """The canvas clone `path` sits inside, or "" (`canvases._clone_for_session`,
    which also follows a moved clone's old path)."""
    try:
        from fused_render import canvases
        found = canvases._clone_for_session(path)
    except Exception:  # noqa: BLE001
        return ""
    return str(found[2]) if found else ""


@router.post(PREFIX + "/consult")
@_handled
def experts_consult(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = _body_dict(body)
    key, question = body.get("expert"), body.get("question")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("expert must name an expert (its id or name)")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    context = body.get("context") or ""
    if not isinstance(context, str):
        raise ValueError("context must be a string")
    run_id = body.get("run_id") or ""
    if not isinstance(run_id, str):
        raise ValueError("run_id must be a string")
    bot = _expert(key.strip())
    ok, reason = _mod("model").available(bot)
    if not ok:
        raise _Unavailable(reason or "no engine is available for this expert")
    res = _mod("consult").consult(bot, question.strip(), context, caller_for_run(run_id))
    return {k: res.get(k) for k in ("id", "answer", "refs", "used", "unsure")}


# ------------------------------------------------------------------- memory ---
@router.get(PREFIX + "/{eid}/memory")
@_handled
def experts_memory(eid: str):
    bot = _expert(eid)
    memory = _mod("memory")
    return {"items": memory.items(bot), "index": memory.index_text(bot)}


@router.post(PREFIX + "/{eid}/memory/forget")
@_handled
def experts_memory_forget(eid: str, body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = _body_dict(body)
    bot = _expert(eid)
    target = body.get("id")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("id must name a memory item (its id or its text)")
    _writable()
    return {"removed": int(_mod("memory").forget(bot, target.strip()))}


@router.get(PREFIX + "/{eid}/observations")
@_handled
def experts_observations(eid: str):
    bot = _expert(eid)
    obs = _mod("observations")
    return {"pending": obs.pending(bot), "due": bool(obs.due(bot))}


@router.get(PREFIX + "/{eid}/consults")
@_handled
def experts_consults(eid: str):
    bot = _expert(eid)
    rows = list(_mod("consult").log(bot, limit=CONSULTS_LIMIT) or [])
    rows.sort(key=lambda r: (r.get("ts") or 0) if isinstance(r, dict) else 0, reverse=True)
    return {"consults": rows[:CONSULTS_LIMIT]}


# ------------------------------------------------------------------- checks ---
@router.get(PREFIX + "/{eid}/checks")
@_handled
def experts_checks(eid: str):
    bot = _expert(eid)
    gate = _mod("gate")
    return {"latest": gate.latest(bot), "status": gate.status(bot), "questions": len(gate.questions(bot))}


@router.post(PREFIX + "/{eid}/checks/run")
@_handled
def experts_checks_run(eid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bot = _expert(eid)
    gate = _mod("gate")
    if not gate.questions(bot):
        raise ValueError("this expert has no questions to check (questions.md is empty or missing)")
    ok, reason = _mod("model").available(bot)
    if not ok:
        raise _Unavailable(reason or "no engine is available for this expert")
    _writable()
    return {"run_id": gate.start(bot)}


# ---------------------------------------------------------------- proposals ---
@router.get(PREFIX + "/{eid}/proposals")
@_handled
def experts_proposals(eid: str):
    bot = _expert(eid)
    return {"proposals": _mod("proposals").listing(bot)}


@router.post(PREFIX + "/{eid}/consolidate")
@_handled
def experts_consolidate(eid: str, body: dict | None = Body(default=None),
                        x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = _body_dict(body if body is not None else {})
    bot = _expert(eid)
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise ValueError("force must be true or false")
    _writable()
    result = dict(_mod("consolidate").run(bot, force=force) or {})
    result["proposed"] = propose(bot)
    return result


def propose(bot) -> list:
    """`proposals.candidates(bot)`, all of them, as ONE proposal (one draft
    call and one gate before/after, however many lessons are due; `prepare`
    marks them proposed), then its card (`cards.show`, which declines while
    the bot is busy; `cards.tick` shows it later). Returns the new proposal
    ids (none or one). Shared with the scheduler."""
    proposals, cards = _mod("proposals"), _mod("cards")
    items = list(proposals.candidates(bot) or [])
    if not items:
        return []
    prop = proposals.prepare(bot, items)
    if not isinstance(prop, dict) or not prop.get("id"):
        return []
    try:
        cards.show(bot, prop)
    except Exception:  # noqa: BLE001 — the proposal is saved; cards.tick retries the card
        logger.warning("experts: could not show proposal %s", prop.get("id"), exc_info=True)
    return [prop["id"]]


# ------------------------------------------------------------------ capture ---
@router.post(PREFIX + "/{eid}/capture")
@_handled
def experts_capture(eid: str, body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = _body_dict(body)
    sid = body.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("session_id must name a task session")
    bot = _expert(eid)
    capture = _mod("capture")
    session = capture.session_by_id(sid.strip())
    if session is None:
        raise _NotFound(f"no task session {sid.strip()} with a transcript")
    ok, reason = _mod("model").available(bot)
    if not ok:
        raise _Unavailable(reason or "no engine is available for this expert")
    _writable()
    return {"observations": capture.capture_session(bot, session)}


# ----------------------------------------------------------------- settings ---
@router.post(PREFIX + "/{eid}/settings")
@_handled
def experts_settings(eid: str, body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = _body_dict(body)
    bot = _expert(eid)
    engine, model = body.get("engine"), body.get("model")
    if engine is not None:
        if engine not in ENGINE_CHOICES:
            raise ValueError("engine must be one of " + ", ".join(ENGINE_CHOICES))
        if engine != "auto" and not _enabled(engine):
            raise ValueError(f"{_label(engine)} is turned off in Preferences")
    if model is not None and not isinstance(model, str):
        raise ValueError("model must be a string (empty for the default)")
    _writable()
    store = _mod("store")
    with bot.lock:
        raw = bot.meta.get("expert")
        raw = dict(raw) if isinstance(raw, dict) else {}
        if engine is not None:
            raw["engine"] = engine
        if model is not None:
            raw["model"] = model.strip()
        bot.meta["expert"] = raw
        bot.save()
    return store.manifest(bot)


def _backends():
    from fused_render.agent_backends import registry
    return registry


def _enabled(bid: str) -> bool:
    try:
        return bool(_backends().is_enabled(bid))
    except Exception:  # noqa: BLE001
        return False


def _label(bid: str) -> str:
    try:
        return _backends().get(bid).label
    except Exception:  # noqa: BLE001
        return bid


@router.get(PREFIX + "/{eid}/engines")
@_handled
def experts_engines(eid: str):
    _expert(eid)
    backends = _backends()
    out = []
    for bid in ENGINE_IDS:
        b = backends.get(bid)
        health = None
        if getattr(b, "health_cached", None) is not None:
            try:
                health = b.health_cached()
            except Exception:  # noqa: BLE001 — the cached snapshot or nothing, never a probe
                health = None
        try:
            models = b.effective_models(health)
        except Exception:  # noqa: BLE001
            models = b.models
        out.append({"id": bid, "label": b.label, "enabled": _enabled(bid),
                    "models": [{"id": m.id, "label": m.label} for m in models],
                    "default_model": getattr(b, "default_model", "") or ""})
    return out
