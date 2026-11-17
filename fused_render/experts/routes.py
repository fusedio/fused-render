"""`/api/experts/*` — the experts' HTTP API (docs/experts.md §8).

A FastAPI router included from fused_render/server/app.py. Reads are plain
GETs; every POST needs `X-Fused: 1` (`server.common._require_fused`). Errors
are `{"error": "<sentence>"}`: an unknown expert -> 404, `ValueError` /
`RuntimeError` -> 400, the bots workspace held by another fused-render -> 409,
no engine for a consult -> 409 with `reason: "unavailable"`.

Every endpoint that may call a model (consult, capture) is a sync
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
                    "available": bool(ok), "reason": "" if ok else (reason or ""),
                    "unread": _mod("knowledge").unread(bot)})
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


def _run_roster(run_id):
    """The roster recorded in `<run dir>/experts.json` for `run_id`, or None when there is none to check."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id) or run_id in (".", ".."):
        return None
    try:
        path = os.path.join(_agent().RUNS, run_id, "experts.json")
    except Exception:  # noqa: BLE001
        return None
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []  # a roster we cannot read offers nothing
    return data if isinstance(data, list) else []


def offered_to_run(bot, run_id) -> bool:
    """Whether `bot` is in the roster the session `run_id` was offered (`<run dir>/experts.json`): by `id` when
    the entries carry one, else by name (case-insensitive). True when the run recorded no roster file."""
    roster = _run_roster(run_id)
    if roster is None:
        return True
    name = _name(bot).strip().casefold()
    for e in roster:
        if not isinstance(e, dict):
            continue
        if e.get("id"):
            if str(e["id"]) == str(bot.id):
                return True
        elif str(e.get("name") or "").strip().casefold() == name:
            return True
    return False


#: The refusal when a session that pre-allowed `expert_consult` asks an expert whose reads are no longer fenced.
UNFENCED_NOW = ("this expert's engine changed since this session started and its reads are no longer limited to "
                "its folders (Codex), so a consult now needs your approval: start a new session to consult it")


def pre_allowed_for_run(run_id) -> bool:
    """Whether the session `run_id` was started with `expert_consult` pre-allowed: its recorded roster is
    non-empty and every entry was fenced then (`auto_allow`, agent `_expert_auto_allow`). Such a session's
    consults run with no card, so the route re-checks the expert's engine now (`model.fenced`)."""
    roster = _run_roster(run_id)
    if not roster:
        return False
    try:
        agent = _agent()
        return bool(agent._expert_auto_allow([e for e in map(agent._expert_entry, roster) if e]))
    except Exception:  # noqa: BLE001
        return all(isinstance(e, dict) and e.get("auto_allow") is True for e in roster)


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
    if run_id and not offered_to_run(bot, run_id):
        return _error("that expert is not offered to this session", 403)
    ok, reason = _mod("model").available(bot)
    if not ok:
        raise _Unavailable(reason or "no engine is available for this expert")
    if run_id and not _mod("model").fenced(bot) and pre_allowed_for_run(run_id):
        # The session pre-allowed consults when every expert was fenced; this one no longer is (its engine is
        # Codex now). A consult with no card would let it read outside its folders: refuse instead.
        return _error(UNFENCED_NOW, 403)
    res = _mod("consult").consult(bot, question.strip(), context, caller_for_run(run_id))
    return {k: res.get(k) for k in ("id", "answer", "refs", "used", "unsure")}


# ------------------------------------------------------------------- memory ---
@router.get(PREFIX + "/{eid}/memory")
@_handled
def experts_memory(eid: str):
    bot = _expert(eid)
    memory = _mod("memory")
    def row(i):
        seen = i.get("contradicted_sessions")
        return dict(i, indexed=bool(memory.indexed(i)), contradicted=memory.contradicted(i),
                    contradicted_sessions=[str(x) for x in seen] if isinstance(seen, list) else [],
                    cited=memory.cited(i), last_used=i.get("last_used") or None, uses=memory.uses(i))
    items = [row(i) for i in memory.items(bot)]
    budget = _mod("store").manifest(bot)["memory"]
    return {"items": items, "budget": {"limit": budget["limit"], "tidy_at": budget["tidy_at"], "total": len(items),
                                        # The tidy summary is gone: what a tidy did is in the activity log.
                                        "last_tidy": None}}


# ----------------------------------------------------------------- activity ---
@router.get(PREFIX + "/{eid}/activity")
@_handled
def experts_activity(eid: str, limit: int = 100, before: str | None = None, quiet: bool = False):
    bot = _expert(eid)
    knowledge = _mod("knowledge")
    st = _mod("consolidate").status(bot)
    return {"items": knowledge.activity(bot, limit=limit, before=before, quiet=quiet),
            "unread": knowledge.unread(bot), "waiting": int(st.get("waiting") or 0),
            "learning": {"running": bool(st.get("running")), "stage": st.get("stage") or ""}}


@router.post(PREFIX + "/{eid}/activity/seen")
@_handled
def experts_activity_seen(eid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bot = _expert(eid)
    _writable()
    _mod("knowledge").seen(bot)
    return {}


@router.post(PREFIX + "/{eid}/activity/{aid}/undo")
@_handled
def experts_activity_undo(eid: str, aid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bot = _expert(eid)
    _writable()
    knowledge = _mod("knowledge")
    try:
        return {"ok": True, "item": knowledge.undo(bot, aid)}
    except knowledge.Refused as e:
        return Response(json.dumps({"detail": str(e)}), status_code=409, media_type="application/json")


@router.post(PREFIX + "/{eid}/learn")
@_handled
def experts_learn(eid: str, body: dict | None = Body(default=None), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = _body_dict(body if body is not None else {})
    bot = _expert(eid)
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise ValueError("force must be true or false")
    ok, reason = _mod("model").available(bot)
    if not ok:
        raise _Unavailable(reason or "no engine is available for this expert")
    _writable()
    return {"started": bool(_mod("scheduler").poke(bot, force=force))}


@router.get(PREFIX + "/{eid}/consults")
@_handled
def experts_consults(eid: str):
    bot = _expert(eid)
    rows = list(_mod("consult").log(bot, limit=CONSULTS_LIMIT) or [])
    rows.sort(key=lambda r: (r.get("ts") or 0) if isinstance(r, dict) else 0, reverse=True)
    return {"consults": rows[:CONSULTS_LIMIT]}


@router.get(PREFIX + "/{eid}/usage")
@_handled
def experts_usage(eid: str, days: int = 7):
    """Tokens per stage and per day over the last `days` days (`usage.summary`, no model call)."""
    return _mod("usage").summary(_expert(eid), days=days)


@router.get(PREFIX + "/{eid}/skills/health")
@_handled
def experts_skills_health(eid: str):
    bot = _expert(eid)
    return _mod("seed").skills_health(bot)


@router.post(PREFIX + "/{eid}/skills/{name}/restore")
@_handled
def experts_skill_restore(eid: str, name: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bot = _expert(eid)
    seed = _mod("seed")
    if seed.shipped_skill_path(bot, name) is None:
        raise _NotFound("no such shipped skill")
    _writable()
    if not seed.restore_shipped_skill(bot, name):
        raise ValueError("the skill could not be restored")
    return {"restored": True}


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
@router.get(PREFIX + "/{eid}/settings")
@_handled
def experts_settings_get(eid: str):
    return _mod("store").manifest(_expert(eid))


@router.post(PREFIX + "/{eid}/settings")
@_handled
def experts_settings(eid: str, body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = _body_dict(body)
    bot = _expert(eid)
    engine, model, learning = body.get("engine"), body.get("model"), body.get("learning")
    if engine is not None:
        if engine not in ENGINE_CHOICES:
            raise ValueError("engine must be one of " + ", ".join(ENGINE_CHOICES))
        if engine != "auto" and not _enabled(engine):
            raise ValueError(f"{_label(engine)} is turned off in Preferences")
    if model is not None and not isinstance(model, str):
        raise ValueError("model must be a string (empty for the default)")
    if learning is not None and not isinstance(learning, bool):
        raise ValueError("learning must be true or false")
    _writable()
    store = _mod("store")
    with bot.lock:
        raw = bot.meta.get("expert")
        raw = dict(raw) if isinstance(raw, dict) else {}
        if engine is not None:
            raw["engine"] = engine
        if model is not None:
            raw["model"] = model.strip()
        if learning is not None:
            raw["learning"] = learning
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
    bot = _expert(eid)
    backends = _backends()
    xmodel = _mod("model")
    claude_default = ""
    try:
        provider, model = xmodel.provider_for(bot)
        if provider == "claude":
            claude_default = model
    except Exception:  # noqa: BLE001 — Claude unavailable (or not the engine): its own default below
        pass
    if not claude_default:
        # What provider_for would use with Claude chosen: the manifest's model, else the bot's (if Claude takes
        # it), else Claude's default.
        man = _mod("store").manifest(bot)
        meta_model = str((getattr(bot, "meta", None) or {}).get("model") or "").strip()
        claude_default = (man.get("model") or "").strip() or (
            meta_model if meta_model and xmodel._claude_model_ok(meta_model, backends) else "")
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
        default = getattr(b, "default_model", "") or ""
        if bid == "claude" and claude_default:
            default = claude_default
        out.append({"id": bid, "label": b.label, "enabled": _enabled(bid),
                    "models": [{"id": m.id, "label": m.label} for m in models],
                    "default_model": default, "note": xmodel.ENGINE_NOTES.get(bid, "")})
    return out
