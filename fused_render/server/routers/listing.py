"""/api/contrib/listing* — Publish to the Marketplace (docs/CONTRIB_LOOP.md
§18.6). Registered in `server/app.py` next to the contrib router.

THREE ROUTES:
  * `GET /api/contrib/listing?path=` → `{ok, listing, state, sentence,
    actions, job}` — the ListingStrip. `listing` is `listing.view` (the last
    read of GitHub + local git; never waits on GitHub). When that read is
    stale (`listing.stale`: older than `AUTO_TTL_S`, or `STATUS_TTL_S`
    while a request is open on the index; none at all; or `fresh=1`) a
    refresh job is
    started (`listing-check`, one per app) and its id answered as `job`: the
    page polls `GET /api/contrib/job?id=` and GETs again when it ends — a
    named wait (§16.14), not a background loop.
  * `GET /api/contrib/listing-draft?path=` → the Publish dialog's prefill,
    `{ok, slug, name, description, category, categories, tags, icon,
    preview, listed}`.
  * `POST /api/contrib/listing-action {path, action, confirm?, args?}` —
    the same body and gate as `/api/contrib/action`: X-Fused, a workspace
    app folder, the Marketplace switch, an own app with a GitHub repo; the
    buttons are re-derived (`actions_listing.decide` over `listing.view`)
    and an id not offered right now is refused `not-offered`; a button with
    a `confirm` sentence needs `confirm: true` (refusal reason `confirm`,
    the sentence as message). Publish, Publish update, Remove and Check
    again are jobs (`{ok, job}`, polled on `GET /api/contrib/job`, whose
    `result` is `{ok, pr: {number, url}, listing}` — `{listing}` for Check
    again); Confirm answers `{ok, listing}`; Fix with Claude answers
    `{ok, open, task, entry_html}` — `open` is the URL the page navigates
    to. Publish now runs ownsync's publish as a job; Open App Doctor and
    Sign in answer `{ok, dialog}` (the page opens them itself).

Refusals are 200s `{ok: false, reason, message}` with plain sentences, like
every contrib route (`routers/contrib.py` — its gate helpers are reused).
Sync defs: they shell out to git and gh, kept off the event loop.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Header

from fused_render.server.routers.contrib import (
    _Target, _app_dir, _disabled, _failure, _job, _refuse, _safely, _text,
)
from fused_render.server.common import _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()


def _listing():
    from fused_render.contrib import listing
    return listing


def _decide_mod():
    from fused_render.contrib import actions_listing
    return actions_listing


def _target(path, *, read: bool = False) -> _Target | dict:
    """The gate both GETs and the POST share, after X-Fused: a workspace app
    folder, the switch on, an own app with a repo. A GET reads the store
    only (`classify.ensure` may ask GitHub who the user is, and a page GET
    never waits on GitHub); the POST classifies like `/api/contrib/action`."""
    app_dir = _app_dir(path)
    if app_dir is None:
        return _refuse("not-an-app", "That isn't an app in your Fused folder.")
    if not _listing()._enabled():
        return _disabled()
    try:
        if read:
            from fused_render.contrib import store
            record = store.get(app_dir)
        else:
            from fused_render.contrib import classify
            record = classify.ensure(app_dir)
    except Exception as e:  # noqa: BLE001
        return _failure(e, "classify")
    if not isinstance(record, dict):
        return _refuse("unknown", "Can't tell where this app came from yet. Try again in a moment.")
    if record.get("kind") != "own":
        return _refuse("not-allowed", "Only an app of your own can go in the Community.")
    if not record.get("repo"):
        return _refuse("no-repo", "Publish this app to GitHub first; the Community lists "
                                  "apps that are on GitHub.")
    return _Target(app_dir, record)


def _signed_in() -> bool:
    from fused_render.contrib import refresh
    gh_ = _safely("identity", refresh.github_identity) or {}
    return bool(gh_.get("signed_in"))


def _running_check(app_dir: str) -> dict | None:
    from fused_render.contrib import jobs, store
    job = jobs.latest("listing-check", store.key(app_dir))
    return job if job and job.get("status") == "running" else None


def _answer(app_dir: str, *, job: str | None = None) -> dict:
    L = _listing()
    lst = _safely("listing view", L.view, app_dir) or {}
    running = job or (_running_check(app_dir) or {}).get("id")
    decided = _decide_mod().decide(lst, signed_in=_signed_in(), refreshing=bool(running))
    return {"ok": True, "listing": lst, "state": decided["state"],
            "sentence": decided["sentence"], "actions": decided["actions"],
            "job": running}


@router.get("/api/contrib/listing")
def api_contrib_listing(path: str = "", fresh: bool = False):
    """`{ok, listing, state, sentence, actions, job}` (see the docstring).
    Starts the refresh job when the last read is stale (or `fresh=1`) and
    someone is signed in — never waits for it: the answer is the cached
    read plus the job's id."""
    t = _target(path, read=True)
    if not isinstance(t, _Target):
        return t
    L = _listing()
    job = None
    if _signed_in() and (fresh or _safely("stale", L.stale, t.app_dir)):
        job = _safely("refresh", L.start_refresh, t.app_dir)
    return _answer(t.app_dir, job=job)


@router.get("/api/contrib/listing-draft")
def api_contrib_listing_draft(path: str = ""):
    t = _target(path, read=True)
    if not isinstance(t, _Target):
        return t
    try:
        return {"ok": True, **_listing().draft(t.app_dir)}
    except Exception as e:  # noqa: BLE001
        return _failure(e, "listing draft")


# -- actions ---------------------------------------------------------------------


def _details(args: dict) -> dict:
    keys = ("slug", "name", "description", "category", "tags", "icon", "preview", "version")
    return {k: args[k] for k in keys if k in args}


def _no_app_id(t: _Target) -> dict | None:
    """The listing shape's refusal (200 `{ok: false, reason: "app-id"}`),
    answered before any job starts, when the entry page has no
    `fused-app-id` — the Community lists apps by it (the job would refuse
    the same, later)."""
    L = _listing()
    return None if L._app_id_slug(t.app_dir) else _refuse("app-id", L.APP_ID_MISSING)


def _publish(t: _Target, args) -> dict:
    return _no_app_id(t) or _job("listing-publish", t.app_dir, _listing().publish, t.app_dir,
                                 _details(args))


def _publish_update(t: _Target, _args) -> dict:
    return _no_app_id(t) or _job("listing-update", t.app_dir, _listing().publish_update,
                                 t.app_dir)


def _check(t: _Target, _args) -> dict:
    return {"ok": True, "job": _listing().start_refresh(t.app_dir)}


def _confirm(t: _Target, _args) -> dict:
    return _listing().confirm_powers(t.app_dir)


def _fix(t: _Target, _args) -> dict:
    return _listing().fix_with_claude(t.app_dir)


def _publish_now(t: _Target, _args) -> dict:
    from fused_render.contrib import ownsync
    return _job("action-publish_now", t.app_dir, ownsync.publish_now, t.app_dir)


_DISPATCH = {
    "publish-market": _publish,
    "publish-update": _publish_update,
    "remove-market": lambda t, a: _job("listing-remove", t.app_dir,
                                       _listing().remove, t.app_dir),
    "confirm-powers": _confirm,
    "fix-with-claude": _fix,
    "check-listing": _check,
    "publish-now": _publish_now,
    "open-doctor": lambda t, a: {"ok": True, "dialog": "doctor"},
    "sign-in": lambda t, a: {"ok": True, "dialog": "sign-in"},
}


@router.post("/api/contrib/listing-action")
def api_contrib_listing_action(body: dict = Body(...),
                               x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not isinstance(body, dict):
        body = {}
    t = _target(body.get("path"))
    if not isinstance(t, _Target):
        return t
    action = _text(body, "action")
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    handler = _DISPATCH.get(action)
    if handler is None:
        return _refuse("unknown-action", "That isn't something fused can do here.")
    offered = {a["id"]: a for a in _answer(t.app_dir)["actions"]}
    button = offered.get(action)
    if button is None:
        return _refuse("not-offered", "That isn't available right now.")
    if button.get("confirm") and body.get("confirm") is not True:
        return _refuse("confirm", button["confirm"])
    try:
        out = handler(t, args)
    except Exception as e:  # noqa: BLE001
        return _failure(e, f"listing action {action}")
    return out if isinstance(out, dict) and "ok" in out else {"ok": True, **(out or {})}
