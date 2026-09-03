"""First-run onboarding wizard state — the one flag behind "show the wizard?".

The wizard itself is the shell's (frontend/src/shell/onboarding/). This module
owns WHETHER it auto-shows, and it owns it server-side on purpose: the desktop
supervisor walks ports 1777..1787 and then an ephemeral one, and every port is
a different browser origin with a fresh localStorage — a flag kept there
would replay the wizard on the next port drift, a second browser, or a private
window. prefs.json (shell/storage) is the same file every other durable shell
preference lives in.

Two writes, kept distinct: `complete` (the user reached the end — created an
app or opened a showcase one) and `dismiss` ("Skip for now" / ✕). Both stop
the auto-show; only `complete` says onboarding happened. The distinction is
what lets a later "Setup" entry in the sidebar's Help menu reopen the wizard
without either write being touched, and what a future version bump could key
a re-show on (a dismissed user is a different audience from a completed one).

`seed_for_existing_users` is the upgrade edge: "first time they open the app"
means a NEW user, and an existing install upgrading into this build has no
flag either. A workspace that already holds apps under <fused_dir>/local is
the evidence someone has been here; stamp it completed once, before the shell
ever asks.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from fused_render.shell import prefs, storage

log = logging.getLogger(__name__)

router = APIRouter()

#: Bumping this does NOT re-show the wizard today (a dismissed/completed user
#: stays quiet); it is recorded so a later build CAN key a one-time re-show on
#: it without guessing which wizard the stored flag was about.
VERSION = 1

_KEY = "onboarding"

#: `FUSED_RENDER_ONBOARDING=1` forces the auto-show (state reads as fresh) so a
#: dev server can render the wizard without deleting prefs.json; `=0` forces
#: it off. Read per request: flipping it needs no restart.
FORCE_ENV = "FUSED_RENDER_ONBOARDING"


def _read() -> dict:
    data = prefs.read_prefs().get(_KEY)
    return data if isinstance(data, dict) else {}


def _write(patch: dict) -> dict:
    all_prefs = prefs.read_prefs()
    current = all_prefs.get(_KEY)
    state = dict(current) if isinstance(current, dict) else {}
    state.update(patch)
    state["version"] = VERSION
    all_prefs[_KEY] = state
    storage.write_json(prefs._path(), all_prefs)
    return state


def snapshot() -> dict:
    """The `onboarding` field of /api/config: `{completed_at, dismissed_at,
    version}` — each timestamp epoch seconds or None. The shell auto-shows
    when BOTH are None."""
    force = os.environ.get(FORCE_ENV)
    if force == "1":
        return {"completed_at": None, "dismissed_at": None, "version": VERSION}
    state = _read()
    if force == "0" and state.get("dismissed_at") is None:
        # Reads as dismissed without writing anything: the override is for
        # this process, not a decision the user made.
        state = {**state, "dismissed_at": time.time()}
    return {
        "completed_at": state.get("completed_at"),
        "dismissed_at": state.get("dismissed_at"),
        "version": VERSION,
    }


def seed_for_existing_users(fused_ws: str) -> None:
    """One-shot at startup: an install that already has apps under
    <fused_dir>/local predates this wizard — mark it completed so an upgrade
    never greets a returning user with a first-run screen. No-op once any
    flag is set; never raises (a startup chore, not a gate)."""
    try:
        state = _read()
        if state.get("completed_at") is not None or state.get("dismissed_at") is not None:
            return
        local = os.path.join(fused_ws, "local")
        if not os.path.isdir(local):
            return
        with os.scandir(local) as it:
            has_app = any(e.is_dir() and not e.name.startswith(".") for e in it)
        if has_app:
            _write({"completed_at": time.time(), "seeded": True})
            log.info("onboarding: existing workspace found, wizard marked completed")
    except Exception:  # noqa: BLE001 — startup chore, never fatal
        log.exception("onboarding: seed check failed (continuing)")


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to keep shell↛server
    # acyclic (see shell/bookmarks.py).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


@router.get("/api/onboarding")
def api_onboarding_get():
    return snapshot()


@router.post("/api/onboarding/complete")
def api_onboarding_complete(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    _write({"completed_at": time.time()})
    return snapshot()


@router.post("/api/onboarding/dismiss")
def api_onboarding_dismiss(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    _write({"dismissed_at": time.time()})
    return snapshot()
