"""/api/bots — the Bots sub-app's HTTP surface (wiring only; the store is
`fused_render/bots.py`).

Consumers: the /bots page (frontend/src/apps/bots) and the bot's own MCP server
(templates/claude/bot_server.py), which reaches memory and tasks through here
rather than touching files itself, so every write goes through one process's
lock and one validation.

Every mutation carries the D3 X-Fused guard. Handlers are sync `def`: the
task-start route waits (up to `_SENT_WAIT_S`) for the spawn to report a run id,
and FastAPI runs sync handlers in its threadpool.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render import bots as store
from fused_render.server.common import _error, _require_fused
from fused_render.server.routers.apps import (
    _VALID_SESSION_EFFORTS,
    _app_name_error,
    _create_app_task,
    _scaffold_app,
    _session_choice_error,
)
from fused_render.shell.prefs import VALID_DEFAULT_MODELS

router = APIRouter()

# How many `-N` suffixes a `create_app` name tries before giving up — the same
# bound as HomeHero's createAppUnderFreeName.
_FREE_NAME_TRIES = 20


def _choice_errors(body: dict) -> str | None:
    """model/effort membership, for the fields PRESENT in `body` — the same
    vocabulary (and the same message) as POST /api/apps/new, so a bot's picker
    and the composer's cannot drift."""
    for field, allowed in (("model", VALID_DEFAULT_MODELS),
                           ("effort", _VALID_SESSION_EFFORTS)):
        if field in body:
            value = "" if body[field] is None else body[field]
            err = _session_choice_error(field, value, allowed)
            if err is not None:
                return err
    return None


def _no_bot(slug: str) -> JSONResponse:
    return _error(f"no bot {slug!r}", status=404)


@router.get("/api/bots")
def api_list_bots():
    return {"root": store.bots_root(), "bots": store.list_bots()}


@router.post("/api/bots")
def api_create_bot(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    err = _choice_errors(body)
    if err is not None:
        return _error(err)
    try:
        bot = store.create_bot(body)
    except ValueError as exc:
        return _error(str(exc))
    return {"bot": bot}


@router.get("/api/bots/{slug}")
def api_get_bot(slug: str):
    bot = store.get_bot(slug)
    if bot is None:
        return _no_bot(slug)
    return {"bot": bot, "memory": store.read_memory(slug) or "",
            "tasks": store.list_tasks(slug) or []}


@router.put("/api/bots/{slug}")
def api_update_bot(slug: str, body: dict = Body(...),
                   x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    err = _choice_errors(body)
    if err is not None:
        return _error(err)
    try:
        bot = store.update_bot(slug, body)
    except ValueError as exc:
        return _error(str(exc))
    if bot is None:
        return _no_bot(slug)
    return {"bot": bot}


@router.delete("/api/bots/{slug}")
def api_delete_bot(slug: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not store.delete_bot(slug):
        return _no_bot(slug)
    return {"ok": True}


@router.get("/api/bots/{slug}/memory")
def api_get_memory(slug: str):
    content = store.read_memory(slug)
    if content is None:
        return _no_bot(slug)
    return {"content": content}


@router.put("/api/bots/{slug}/memory")
def api_put_memory(slug: str, body: dict = Body(...),
                   x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    content = body.get("content")
    if not isinstance(content, str):
        return _error("'content' must be a string")
    if not store.write_memory(slug, content):
        return _no_bot(slug)
    return {"ok": True}


@router.get("/api/bots/{slug}/tasks")
def api_list_tasks(slug: str):
    tasks = store.list_tasks(slug)
    if tasks is None:
        return _no_bot(slug)
    return {"tasks": tasks}


@router.get("/api/bots/{slug}/apps")
def api_list_apps(slug: str):
    # Per-bot in the URL for the MCP server's convenience (it only knows its
    # slug); the listing itself is the workspace's, the same for every bot.
    if store.get_bot(slug) is None:
        return _no_bot(slug)
    return {"apps": store.list_apps()}


@router.post("/api/bots/{slug}/tasks")
def api_start_task(slug: str, body: dict = Body(...),
                   x_fused: str | None = Header(default=None)):
    """Start a builder task for the bot: kind "new" scaffolds an app through
    POST /api/apps/new's own path (`_scaffold_app`) with the framed spec as
    its prompt; kind "edit" starts a task on an existing app's entry page
    (`_create_app_task`, what the app page's task row uses). Either way the
    task is appended to the bot's tasks.json and returned with live status."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bot = store.get_bot(slug)
    if bot is None:
        return _no_bot(slug)

    kind = body.get("kind")
    if kind not in ("new", "edit"):
        return _error("'kind' must be 'new' or 'edit'")
    spec = body.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        return _error("'spec' must be a non-empty string")
    model = body.get("model") or ""
    effort = body.get("effort") or ""
    err = _choice_errors({"model": model, "effort": effort})
    if err is not None:
        return _error(err)

    # The framing line tells the builder whose words these are, so a spec
    # written in the bot's voice ("the user wants…") reads as a brief, not as
    # the user talking to it directly.
    verb = "build" if kind == "new" else "edit"
    prompt = (f"Task from bot '{bot['name']}': {verb} this Fused app to the "
              f"following spec.\n\n{spec.strip()}")

    if kind == "new":
        name = body.get("name")
        name_err = _app_name_error(name)
        if name_err is not None:
            return _error(name_err)
        name = str(name).strip()
        result = None
        for i in range(1, _FREE_NAME_TRIES + 1):
            attempt = name if i == 1 else f"{name}-{i}"
            result = _scaffold_app(attempt, prompt, model, effort)
            if isinstance(result, JSONResponse) and result.status_code == 409:
                continue
            break
        if isinstance(result, JSONResponse):
            return result
        entry = result.get("task")
        if not entry:
            # The app exists; only the task failed. Say both, so the bot can
            # tell the user where the empty app is instead of retrying into a
            # `-2` copy.
            return _error(f"created app at {result['path']} but its task did "
                          f"not start: {result.get('task_error') or 'unknown'}",
                          status=500)
        app_path = result["path"]
        entry_html = result["entry_html"]
    else:
        path = body.get("path")
        if not isinstance(path, str) or not path.strip():
            return _error("'path' must be a non-empty string")
        resolved = store.resolve_app(path.strip())
        if resolved is None:
            return _error(f"no app at {path!r}", status=404)
        app_path, entry_html = resolved
        entry, task_error = _create_app_task(entry_html, prompt, model, effort,
                                             permission_mode="auto")
        if entry is None:
            return _error(task_error or "task did not start", status=500)

    record = store.task_record(entry=entry, kind=kind,
                               app_name=os.path.basename(app_path),
                               app_path=app_path, entry_html=entry_html,
                               spec=spec.strip())
    store.append_task(slug, record)
    # Live fields straight from the entry we already hold — it was read back
    # after the send resolved — instead of a second list_entries() scan.
    return {"task": store.with_live(record, [entry])}
