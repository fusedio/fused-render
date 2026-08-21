"""Programmatic triggers for the workflow canvas: plan, arm, disarm, run.

The model — the store, arming, the fingerprint check, the queue and the tick —
is `fused_render/workflow_triggers.py`; this is the HTTP skin over it, and it is
the whole of the boundary the `workflow` template is allowed to reach (a
template imports nothing from `fused_render`, SPEC PY-15 / D166, so the panel
arms by POSTing here rather than by calling a Python helper beside it).

Two things live here rather than in the model, both because they need what only
this layer knows:

* **the mount refusal.** An armed workflow is an agent turned loose on this
  machine on a timer, and the bytes under the mounts dir come from a remote over
  FUSE. The workflow mode's own `condition.py` already refuses to OPEN a
  mount-backed document; arming one would route around that gate, and so would a
  watched folder that lives on a mount — a sweep of a wedged rclone-NFS mount is
  the pattern those gates were written for. The mounts registry lives above the
  model, so the check belongs on this side of the import.
* **the D3 header guard.** Every POST here either authorizes unattended code
  execution or revokes it. Reads are unguarded like every other read endpoint.
"""
import os

from fastapi import APIRouter, Body, Header

from fused_render import workflow_triggers
from fused_render.server.common import _error, _require_fused

router = APIRouter()


def _mount_refusal(path: str) -> str:
    """A sentence, or `""`. Imported per call, not at module scope: binding the
    name at import would freeze it past the mounts registry's own seams, which
    is the same reason the peer gates resolve it late."""
    from fused_render.shell.mounts import is_mount_backed

    try:
        if is_mount_backed(os.path.abspath(os.path.expanduser(path))):
            return ("%s is on a remote mount, and unattended work must not run "
                    "against one." % path)
    except Exception:  # noqa: BLE001 — cannot tell reads as "refuse" (CT-12)
        return "%s could not be checked against this machine's mounts." % path
    return ""


def _path_of(body: dict) -> tuple[str, str]:
    path = body.get("path")
    if not isinstance(path, str) or not path.strip():
        return "", "path: required"
    return os.path.abspath(os.path.expanduser(path.strip())), ""


@router.get("/api/workflow-triggers")
def api_workflow_triggers(path: str = ""):
    """Every armed (and every once-armed) workflow, or the state of one.

    Disarmed rows are included on purpose: `needs_rearm` with its reason on it is
    how somebody finds out that adding a node stopped their workflow running, and
    a row that vanished would have said nothing.
    """
    if path:
        resolved = os.path.abspath(os.path.expanduser(path))
        return {"workflow": workflow_triggers.get(resolved),
                "defaults": _defaults()}
    return {"workflows": workflow_triggers.list_workflows(),
            "defaults": _defaults()}


def _defaults() -> dict:
    """The model's bounds, so the panel states the real numbers rather than a
    second copy of them that can drift."""
    return {
        "max_runs_per_hour": workflow_triggers.DEFAULT_RUNS_PER_HOUR,
        "error_limit": workflow_triggers.DEFAULT_ERROR_LIMIT,
        "queue_max": workflow_triggers.QUEUE_MAX,
        "poll_interval_s": workflow_triggers.POLL_INTERVAL_S,
        "kinds": list(workflow_triggers.KINDS),
    }


@router.get("/api/workflow-triggers/events")
def api_workflow_trigger_events():
    """What armed workflows did that nobody has been told about yet. Same shape
    and same reasoning as `/api/schedule/events`: unattended work needs a surface
    that finds the user, not one they have to think to visit."""
    return {"events": workflow_triggers.undelivered_events()}


@router.post("/api/workflow-triggers/events/ack")
def api_workflow_trigger_events_ack(body: dict = Body(...),
                                    x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    event_id = body.get("id")
    if not isinstance(event_id, int) or isinstance(event_id, bool):
        return _error("id: expected an integer event id", status=400)
    return {"delivered": workflow_triggers.ack_events(event_id)}


@router.post("/api/workflow-triggers/plan")
def api_workflow_trigger_plan(body: dict = Body(...),
                              x_fused: str | None = Header(default=None)):
    """What arming this document would authorize.

    GUARDED even though it changes nothing, and this is the one read here that
    is: it compiles a path the caller names, which spawns `fused app serve`
    discovery over that folder. A read endpoint that does work on an arbitrary
    path is not the same kind of read as one that lists a store.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path, error = _path_of(body)
    if error:
        return _error(error, status=400)
    refusal = _mount_refusal(path)
    if refusal:
        return _error(refusal, status=400)
    return workflow_triggers.plan(path)


@router.post("/api/workflow-triggers/arm")
def api_workflow_trigger_arm(body: dict = Body(...),
                             x_fused: str | None = Header(default=None)):
    """Record that a person approved THIS tool set for this workflow.

    `tools` is the list the caller showed the human and it is required — the
    model re-compiles and refuses if the two differ, which is what makes "the
    dialog shows the tool list" a guarantee rather than a convention.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path, error = _path_of(body)
    if error:
        return _error(error, status=400)
    refusal = _mount_refusal(path)
    if refusal:
        return _error(refusal, status=400)

    triggers = body.get("triggers")
    # Every watched folder gets the same refusal the document does. A workflow
    # on local disk watching a folder on a wedged mount is the same unattended
    # FUSE read by another name.
    if isinstance(triggers, list):
        for trigger in triggers:
            if isinstance(trigger, dict) and trigger.get("folder"):
                refusal = _mount_refusal(str(trigger["folder"]))
                if refusal:
                    return _error(refusal, status=400)

    return workflow_triggers.arm(
        path,
        tools=body.get("tools"),
        triggers=triggers,
        max_runs_per_hour=body.get("max_runs_per_hour"),
        error_limit=body.get("error_limit"),
        model=str(body.get("model") or ""),
    )


@router.post("/api/workflow-triggers/disarm")
def api_workflow_trigger_disarm(body: dict = Body(...),
                                x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path, error = _path_of(body)
    if error:
        return _error(error, status=400)
    if body.get("forget"):
        return workflow_triggers.forget(path)
    return workflow_triggers.disarm(path)


@router.post("/api/workflow-triggers/run")
def api_workflow_trigger_run(body: dict = Body(...),
                             x_fused: str | None = Header(default=None)):
    """Queue one run of an ARMED workflow with a payload of the caller's choosing.

    Deliberately goes through the queue rather than starting anything: it is the
    same path a trigger takes, so it gets the same serialization, the same rate
    cap and the same fingerprint check. A person who wants to run a workflow
    right now, armed or not, uses the panel's Run button — that click is its own
    approval (WC-4a) and does not come through here.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path, error = _path_of(body)
    if error:
        return _error(error, status=400)
    payload = body.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _error("payload: expected a JSON object", status=400)
    return workflow_triggers.enqueue(
        path, payload, source=workflow_triggers.SOURCE_MANUAL)
