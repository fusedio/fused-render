"""/api/contrib/ladder and /api/contrib/publish-github — the Community tab's
one read and its one door (docs/CONTRIB_LOOP.md §19.2). Registered in
`server/app.py` next to the listing router; the logic is
`contrib/ladder.py`'s.

  * `GET /api/contrib/ladder?dir=` (`path=` too; `fresh=1` after an action
    re-asks visibility, App Doctor and the listing — still never waited for) → `{ok, rung, kind, mine,
    pending, remix_of, github: {installed, signed_in, checking, login, repo,
    visibility, unpushed, link, blocked}, listing, doctor: {clear, failing},
    flags: {sources, marketplace, github_link}}`. A read: no guard, never
    waits on GitHub. Not a workspace app folder → 200 `{ok: false, reason:
    "not-an-app", message}` like every contrib read.
  * `POST /api/contrib/publish-github {dir, name?, visibility?}` (X-Fused)
    → `github_setup`'s publish record plus `ok: true`; the page polls
    `GET /api/github/publish` as the Git panel does. A refusal is a 409
    `{ok: false, error, message, reason}` with one plain sentence.

Sync defs: both shell out to git (and the POST to gh), kept off the event
loop.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.server.common import _require_fused
from fused_render.server.routers.contrib import _app_dir, _refuse

logger = logging.getLogger(__name__)

router = APIRouter()

_GENERIC = "Something went wrong. Try again."


def _ladder():
    from fused_render.contrib import ladder
    return ladder


@router.get("/api/contrib/ladder")
def api_contrib_ladder(dir: str = "", path: str = "",  # noqa: A002 — the contract's name
                       fresh: bool = False):
    app_dir = _app_dir(dir or path)
    if app_dir is None:
        return _refuse("not-an-app", "That isn't an app in your Fused folder.")
    try:
        return _ladder().status(app_dir, fresh=fresh)
    except Exception:  # noqa: BLE001
        logger.exception("contrib ladder failed for %s", app_dir)
        return _refuse("failed", _GENERIC)


def _conflict(message: str, reason: str | None) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, "message": message,
                         "reason": reason or "refused"}, status_code=409)


@router.post("/api/contrib/publish-github")
def api_contrib_publish_github(body: dict = Body(...),
                               x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not isinstance(body, dict):
        body = {}
    app_dir = _app_dir(body.get("dir") or body.get("path"))
    if app_dir is None:
        return _conflict("That isn't an app in your Fused folder.", "not-an-app")
    from fused_render.contrib.jobs import PlainError

    name = body.get("name") if isinstance(body.get("name"), str) else None
    visibility = body.get("visibility") if isinstance(body.get("visibility"), str) else None
    try:
        record = _ladder().publish_github(app_dir, name, visibility)
    except PlainError as e:
        return _conflict(e.message, e.reason)
    except Exception:  # noqa: BLE001
        logger.exception("publish-github failed for %s", app_dir)
        return _conflict(_GENERIC, "failed")
    return {"ok": True, **(record if isinstance(record, dict) else {})}
