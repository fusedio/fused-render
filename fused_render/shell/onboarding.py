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

`step` is the third field: the id of the wizard step the user last had open,
written by the shell on every step change. It is what lets the wizard RESUME —
after a server restart (the auto-show lands on that step, not the first) and
after a dismiss (Help › Setup wizard reopens where the user left). `complete`
clears it, so a finished user who reopens the wizard starts from the top;
`dismiss` keeps it. Server-side for the same reason as the flags.

`stages` is the fourth field, and the one behind the PROGRESS METER (the
sidebar's "Setup 60%" row and the wizard's top-bar pills): one record per
wizard step, `{status, meta, updated_at}`, `status` one of
`pending | partial | complete | n/a`. Each step of the wizard decides its own
status from the facts it already checks (shell/onboarding/progress.ts holds
the table) and POSTs it here whenever it changes; `meta` is whatever the step
wants to remember about how it got there (the version it found, the account,
the model ids it started) — free-form, for reference, never read back by a
rule. `n/a` is a step this machine does not have (Disk Access off macOS, Models
with nothing to offer) — it leaves the denominator.

The stored status is what the wizard last SAW. For the stages the server can
check cheaply it is overruled on every read by what is true now (`_observe`):
Full Disk Access from fda.py's TTL-cached probe, "first app" from the
workspace's local/ folder, Claude Code from claude_health's disk cache (never a
spawn). So a grant made from the Home strip, or an app built without the
wizard, moves the meter without the wizard being reopened.

`seed_for_existing_users` is the upgrade edge: "first time they open the app"
means a NEW user, and an existing install upgrading into this build has no
flag either. A workspace that already holds apps under <fused_dir>/local is
the evidence someone has been here; stamp it completed once, before the shell
ever asks.
"""

from __future__ import annotations

import json
import logging
import os
import sys
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

#: The wizard's step ids (frontend shell/onboarding/OnboardingWizard STEPS).
#: A closed set: prefs.json is shared state, and an unknown id is refused
#: rather than stored.
STEPS = ("about", "claude", "fda", "models", "app")

#: Stage statuses. `n/a` = the machine has no such step; it is not counted.
STATUSES = ("pending", "partial", "complete", "n/a")

#: Largest `meta` a stage write may carry, in JSON bytes — prefs.json is
#: shared state and a stage note is a note, not a log.
META_MAX_BYTES = 4096

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


def _stages(state: dict) -> dict:
    """The stored stage records, validated (an unknown id or a malformed record
    is dropped, not served), then overruled by what the server can see now."""
    raw = state.get("stages")
    out: dict = {}
    if isinstance(raw, dict):
        for sid, rec in raw.items():
            if sid not in STEPS or not isinstance(rec, dict):
                continue
            if rec.get("status") not in STATUSES:
                continue
            meta = rec.get("meta")
            out[sid] = {
                "status": rec["status"],
                "meta": meta if isinstance(meta, dict) else {},
                "updated_at": rec.get("updated_at"),
            }
    _observe(out)
    return out


def _observe(stages: dict) -> None:
    """Overrule the stored status with the truth where it is cheap to know.
    Mutates `stages` in place; a stage the server cannot see is left as the
    wizard wrote it. Never raises — a meter must not take /api/config down."""

    def put(sid: str, status: str, **meta: object) -> None:
        rec = stages.get(sid) or {"status": "pending", "meta": {}, "updated_at": None}
        if rec["status"] == status and all(rec["meta"].get(k) == v for k, v in meta.items()):
            stages.setdefault(sid, rec)
            return
        stages[sid] = {
            "status": status,
            "meta": {**rec["meta"], **meta, "observed": True},
            "updated_at": rec.get("updated_at"),
        }

    # Full Disk Access: fda.snapshot() is the strip's own verdict, TTL-cached.
    try:
        from fused_render.shell import fda as shell_fda

        fda = shell_fda.snapshot()
        if fda is not None:
            if fda.get("granted"):
                put("fda", "complete", granted=True)
            elif fda.get("pending_relaunch"):
                put("fda", "partial", pending_relaunch=True)
            elif stages.get("fda", {}).get("status") == "complete":
                # A grant this process no longer has (revoked) — back to pending.
                put("fda", "pending", granted=False)
        elif sys.platform != "darwin":
            put("fda", "n/a")
    except Exception:  # noqa: BLE001
        log.debug("onboarding: fda observe failed", exc_info=True)

    # First app: any folder under <fused_dir>/local IS an app the user has.
    try:
        from fused_render.shell.seed import fused_dir

        local = os.path.join(fused_dir(), "local")
        if os.path.isdir(local):
            with os.scandir(local) as it:
                count = sum(1 for e in it if e.is_dir() and not e.name.startswith("."))
            if count:
                put("app", "complete", app_count=count)
    except Exception:  # noqa: BLE001
        log.debug("onboarding: app observe failed", exc_info=True)

    # Claude Code: the health module's DISK CACHE only — a fresh measure spawns
    # processes, and /api/config is read on every page load. No cache, no say.
    try:
        from fused_render import claude_health

        h = claude_health.cached()
        if h is not None:
            runnable = bool(h.get("found")) and not h.get("broken")
            ok = (
                runnable
                and h.get("version") is not None
                and not h.get("outdated")
                and h.get("signed_in") is True
            )
            status = "complete" if ok else "partial" if runnable else "pending"
            acct = h.get("account") or {}
            put(
                "claude",
                status,
                version=h.get("version"),
                signed_in=h.get("signed_in"),
                account=acct.get("email") or acct.get("method"),
                on_shell_path=h.get("on_shell_path"),
            )
    except Exception:  # noqa: BLE001
        log.debug("onboarding: claude observe failed", exc_info=True)


def snapshot() -> dict:
    """The `onboarding` field of /api/config: `{completed_at, dismissed_at,
    step, stages, version}` — each timestamp epoch seconds or None, `step` the
    last open step id or None, `stages` the per-step progress records (module
    docstring). The shell auto-shows when BOTH timestamps are None."""
    force = os.environ.get(FORCE_ENV)
    state = _read()
    step = state.get("step") if state.get("step") in STEPS else None
    stages = _stages(state)
    if force == "1":
        # The override fakes the FLAGS, not the step: a dev server forced into
        # the wizard still resumes where it was, which is how this is smoked.
        return {"completed_at": None, "dismissed_at": None, "step": step, "stages": stages, "version": VERSION}
    if force == "0" and state.get("dismissed_at") is None:
        # Reads as dismissed without writing anything: the override is for
        # this process, not a decision the user made.
        state = {**state, "dismissed_at": time.time()}
    return {
        "completed_at": state.get("completed_at"),
        "dismissed_at": state.get("dismissed_at"),
        "step": step,
        "stages": stages,
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
    # Completion resets the resume point: reopening a FINISHED wizard from
    # Help starts at the top, a dismissed one resumes (see module docstring).
    _write({"completed_at": time.time(), "step": None})
    return snapshot()


@router.post("/api/onboarding/dismiss")
def api_onboarding_dismiss(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    _write({"dismissed_at": time.time()})
    return snapshot()


@router.post("/api/onboarding/step")
def api_onboarding_step(body: dict, x_fused: str | None = Header(default=None)):
    """Remember the step the user has open, so a restart or a reopen resumes
    there. Fire-and-forget from the shell; refuses ids it does not know."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    step = body.get("step") if isinstance(body, dict) else None
    if step not in STEPS:
        return JSONResponse({"error": f"unknown step {step!r}"}, status_code=400)
    _write({"step": step})
    return snapshot()


@router.post("/api/onboarding/stage")
def api_onboarding_stage(body: dict, x_fused: str | None = Header(default=None)):
    """A step reports its status — `{stage, status, meta?}`. The status is
    replaced; `meta` is MERGED over what the stage stored before (a step notes
    one more fact without restating the rest). Refuses ids and statuses it
    does not know, and a note over `META_MAX_BYTES`."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    stage = body.get("stage")
    status = body.get("status")
    meta = body.get("meta") or {}
    if stage not in STEPS:
        return JSONResponse({"error": f"unknown stage {stage!r}"}, status_code=400)
    if status not in STATUSES:
        return JSONResponse({"error": f"unknown status {status!r}"}, status_code=400)
    if not isinstance(meta, dict):
        return JSONResponse({"error": "meta must be an object"}, status_code=400)
    try:
        if len(json.dumps(meta)) > META_MAX_BYTES:
            return JSONResponse({"error": f"meta over {META_MAX_BYTES} bytes"}, status_code=400)
    except (TypeError, ValueError):
        return JSONResponse({"error": "meta must be JSON"}, status_code=400)
    state = _read()
    stages = dict(state["stages"]) if isinstance(state.get("stages"), dict) else {}
    prev = stages.get(stage) if isinstance(stages.get(stage), dict) else {}
    prev_meta = prev.get("meta") if isinstance(prev.get("meta"), dict) else {}
    stages[stage] = {"status": status, "meta": {**prev_meta, **meta}, "updated_at": time.time()}
    _write({"stages": stages})
    return snapshot()
