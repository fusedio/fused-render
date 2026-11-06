"""`POST /api/phone/tasks/new-app` — the one route the Fused Render iOS app has
of its own (docs/bots.md §14, D1468): the grid's "+", "describe an app and the
computer names, scaffolds and builds it as a task". Everything else the phone
draws is the desktop's own routes, allow-listed by the LAN listener (lan.py
`_DOORS`) and rendered by the desktop's own React pages (frontend/src/lan/
main.tsx). This stays because the desktop has no naming call of its own: the
hero names an app client-side through the AI relay, and the phone makes that
one call server-side so a slow Wi-Fi hop never shows a half-made app.

Included from fused_render/server/app.py. `X-Fused-Device` is the pairing
record's device id lan.py stamps (a client-sent one is dropped); on loopback
the header is optional and the device is "". Errors are `{"error":
"<sentence>"}`: 400 for a bad body, 409 with the desktop's own sentence when
the name is taken, 403 without `X-Fused: 1`.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import Response

from fused_render import tasks_store
from fused_render.server.common import _error, _require_fused

router = APIRouter()
logger = logging.getLogger(__name__)

PREFIX = "/api/phone/tasks"
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # lan.py's pairing record ids


def _device(x_fused_device) -> str:
    """The paired device's id lan.py stamped; "" on loopback (or for anything that is not an id)."""
    v = str(x_fused_device or "").strip()
    return v if _DEVICE_RE.match(v) else ""


def _tilde(path) -> str:
    p = str(path or "")
    home = os.path.expanduser("~")
    if home and p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return "~" if p and p == home else p


def _body(resp):
    """A desktop handler's answer as a `(status, dict)` pair: a plain dict is 200, a
    JSONResponse carries its own status and JSON body."""
    if isinstance(resp, Response):
        try:
            data = json.loads(bytes(resp.body) or b"{}")
        except (TypeError, ValueError):
            data = {}
        return resp.status_code, data if isinstance(data, dict) else {"result": data}
    return 200, resp if isinstance(resp, dict) else {"result": resp}


def _guarded(fn):
    """Run a desktop handler for the phone: its `HTTPException` (the apps
    router's own 400/409 and their sentences) becomes `{"error": sentence}`
    with the same status, and anything else is handed through as it came."""
    try:
        return fn()
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            detail = detail.get("error") or json.dumps(detail)
        return _error(str(detail), exc.status_code)


#: The naming prompt the desktop hero sends to the relay (apps/builder/HomeHero.tsx
#: NAME_SYSTEM_PROMPT), here so the phone makes one call. Keep the two the same.
NAME_NONE = "none"
NAME_MODEL = "haiku"
NAME_SYSTEM_PROMPT = (
    "You name software projects. Given a description of an app, reply with a "
    "short folder name for it in kebab-case: 2-3 lowercase words joined by "
    "hyphens, letters and digits only, at most 4 words when 3 cannot say it.\n"
    "Rules:\n"
    "- Lead with the subject (what it is about), then what it does: "
    "\"expense-tracker\", not \"track-expenses\".\n"
    "- Use words from the description or plain words for what the app does. "
    "Never invent brand words or puns.\n"
    "- No filler: never app, tool, my, simple, new, basic, project, helper, "
    "manager (unless the thing IS a manager), or the user's name.\n"
    "- If the description names the app itself, use that name, kebab-cased.\n"
    "- If the text is not a description of an app to build, reply with "
    "exactly: " + NAME_NONE + "\n"
    "Examples:\n"
    "\"a page that shows my monthly spending by category\" -> spending-by-category\n"
    "\"something to track which plants I watered and when\" -> plant-watering-log\n"
    "\"a kanban board for my reading list, call it Shelf\" -> shelf\n"
    "\"hello, what can you do?\" -> " + NAME_NONE + "\n"
    "Reply with ONLY the name — no quotes, no prose, no explanation."
)
_KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NAME_TIMEOUT_S = 12.0


def _kebab(text: str) -> str:
    words = [w for w in re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-").split("-") if w]
    return "-".join(words[:4])


async def _suggest_name(prompt: str) -> str:
    """A kebab-case folder name for the app `prompt` describes, through the AI
    relay on haiku (the hero's own call, server-side), or "" when the relay is
    down, slow, or answers prose. The caller then falls back to a dated slug."""
    try:
        from fused_render.server import ai as ai_mod
        resp = await asyncio.wait_for(
            ai_mod._ai_relay({"prompt": prompt, "systemPrompt": NAME_SYSTEM_PROMPT, "model": NAME_MODEL}),
            _NAME_TIMEOUT_S)
        status, data = _body(resp)
        if status != 200:
            return ""
        text = str(((data.get("result") or {}).get("text")) or "").strip().lower().rstrip(". ")
    except Exception:  # noqa: BLE001 — naming is best effort; the slug below is the answer then
        logger.debug("phone new-app: naming failed", exc_info=True)
        return ""
    if not text or text == NAME_NONE or not _KEBAB_RE.match(text):
        return ""
    return _kebab(text)


def _dated_slug() -> str:
    return "app-" + _dt.datetime.now().strftime("%Y%m%d-%H%M")


def _free_name(name: str) -> str:
    """`name`, or `name-2`, `name-3`, … — the first the workspace does not hold.
    Only for a name the phone did NOT type: a typed one that is taken is a 409
    with the desktop's sentence, so the person can pick another."""
    from fused_render.shell.seed import fused_dir
    root = os.path.join(fused_dir(), "app")
    if not os.path.exists(os.path.join(root, name)):
        return name
    for i in range(2, 1000):
        if not os.path.exists(os.path.join(root, f"{name}-{i}")):
            return f"{name}-{i}"
    return f"{name}-{os.urandom(3).hex()}"


@router.post(PREFIX + "/new-app")
async def phone_new_app(body: dict = Body(...), x_fused: str | None = Header(default=None),
                        x_fused_device: str | None = Header(default=None)):
    """Describe an app: `{prompt, name?}` -> `{key, entry_id, task_id, app: {name,
    path, url}}`. With no `name` the relay names it (`_suggest_name`), else a
    dated slug. Then the desktop's own `api_new_app`: copy the starter, git
    init, stamp the app id, start the build task on the entry html with the
    default agent. 409 with the desktop's sentence when the name is taken."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("prompt: required", 400)
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        return _error("name must be a string", 400)
    typed = (name or "").strip()
    suggested = "" if typed else (await _suggest_name(prompt) or _dated_slug())
    # Everything below touches the disk and the listing, and `_new_app` waits
    # for the task's send to resolve (up to 20 s): all of it off the event loop.
    return await asyncio.to_thread(_new_app_sync, typed or suggested, bool(typed), prompt, _device(x_fused_device))


def _new_app_sync(name: str, typed: bool, prompt: str, device: str):
    from fused_render.server.routers import apps
    base = name
    if not typed:
        name = _free_name(base)
    err = apps._app_name_error(name)
    if err is not None:
        return _error(err, 400)
    # The build task asks before anything risky ("prompt"), whatever the
    # scheduler's default for a scheduled message is, and is stamped phone.
    body = {"name": name, "prompt": prompt, "backend": ""}
    resp = _guarded(lambda: apps._new_app(body, permission_mode="prompt", origin="phone"))
    status, data = _body(resp)
    if status == 409 and not typed:
        # A name nobody typed was taken between the check and the copy (two
        # quick taps in one minute): the next free one, once.
        body["name"] = name = _free_name(base)
        resp = _guarded(lambda: apps._new_app(body, permission_mode="prompt", origin="phone"))
        status, data = _body(resp)
    if status != 200:
        return resp
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    entry_id = str(task.get("id") or "")
    session = str(task.get("claude_session_id") or "")
    key = session or (tasks_store.pending_key(entry_id) if entry_id else "")
    path = str(data.get("path") or "")
    out = {"key": key, "entry_id": entry_id,
           "app": {"name": name, "path": _tilde(path),
                   "url": "/render?path=" + _q(str(data.get("entry_html") or ""))}}
    if data.get("task_error"):
        out["task_error"] = str(data["task_error"])
    logger.info("phone new app %r (%s) from device %r", name, key, device)
    return out


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="/")
