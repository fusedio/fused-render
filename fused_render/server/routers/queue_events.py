"""POST /api/tasks/queue/event — the project queue's ear on its own processes.

The queue moves on EVENTS, never on a poll (design.md, "No polls in queue
logic"). Three of those events are only ever known inside a process the server
spawned and does not otherwise talk to:

* `turn_ended` — the CLI wrote a `result` row, so the turn that owned this
  folder is over. Seen by `session_host.py`, which is already tailing
  `out.jsonl` every tick for its own reap timer.
* `exited`     — the CLI process is gone (its own exit, or the host's idle
  reap). Seen by the same loop, on its way out.
* `card_raised` — a permission card was parked, so the task is waiting on a
  human and is no longer the one *running*. Seen by `permission_server.py` the
  instant it writes the request file, long before the page polls it.

Those two modules are TEMPLATES: stdlib only, no `fused_render` import (SPEC
PY-15), spawned as separate processes. HTTP is the only wire they have back
here, hence this endpoint rather than a function call.

The posts are unconditional and this endpoint is the flag gate: with
`project_queue_enabled` off it answers `{"ok": true, "ignored": true}` and
touches nothing, so the flag-off behaviour is byte-for-byte what shipped in
main plus one ignored request. It also never 500s on a manager error — the
caller is a fire-and-forget daemon thread that cannot act on a failure, and a
traceback in the log is worth more than a status code nobody reads.
"""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Body, Header
from fastapi.concurrency import run_in_threadpool

from fused_render import project_queue, queue_manager
from fused_render.server.common import _error, _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()

# The three the transport sends. An unknown kind is a 400 and not a shrug: it
# means a template and this file have drifted apart, which is a bug worth
# seeing rather than an event worth dropping.
KINDS = ("turn_ended", "exited", "card_raised")


def _task_key(run_id: str) -> str:
    """The Tasks page key for a run, read off the run dir.

    The posting process knows its run id for certain (it IS the directory it
    writes into) and the session id only if `meta.json` was readable when it
    looked, so the body's `session_id` is an optimisation and this is the
    authority. Same three spellings every other reader uses: the id `_start`
    MINTED for a new chat, the one a resume was asked to continue, and — for a
    run dir that names neither yet — `project_queue.run_sessions`, which also
    asks the live registry by pid.

    `""` for anything unreadable; the caller turns that into a 400 rather than
    guessing which task an event belonged to.
    """
    if not run_id or project_queue.bad_id(run_id):
        return ""
    agent = project_queue.agent_module()
    if agent is None:
        return ""
    run_dir = os.path.join(str(agent.RUNS), run_id)
    try:
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception:  # noqa: BLE001 — an unreadable run has no key, not an error
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    key = str(meta.get("session_id") or "") or str(meta.get("resumed_from") or "")
    if key:
        return key
    try:
        sessions = project_queue.run_sessions(agent, run_dir, meta)
    except Exception:  # noqa: BLE001 — same
        return ""
    # Sorted only so a run that answers to two ids resolves the same way twice;
    # in practice this branch sees at most one.
    return sorted(sessions)[0] if sessions else ""


def _dispatch(kind: str, task_key: str, run_id: str, code) -> bool:
    manager = queue_manager.get()
    if kind == "turn_ended":
        manager.turn_ended(task_key, run_id)
    elif kind == "exited":
        manager.exited(task_key, run_id, code)
    else:
        manager.card_raised(task_key, run_id)
    return True


@router.post("/api/tasks/queue/event")
async def api_tasks_queue_event(payload: dict | None = Body(default=None),
                                x_fused: str | None = Header(default=None)):
    """One queue event from a process we spawned.

    Body: `{kind, run_id, session_id?, request_id?, code?}`. X-Fused guarded
    like every other mutating POST (D3) — this one moves who owns a working
    tree, which is not something a blind cross-origin POST may do.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = payload if isinstance(payload, dict) else {}

    kind = str(body.get("kind") or "")
    # Checked BEFORE the flag: a kind nobody handles is a drift bug whether or
    # not the queue is switched on, and "ignored" would hide it until someone
    # turned the flag on months later.
    if kind not in KINDS:
        return _error("unknown queue event kind: %r" % (kind,), status=400)

    if not await run_in_threadpool(project_queue.enabled):
        return {"ok": True, "ignored": True}

    run_id = str(body.get("run_id") or "")
    task_key = str(body.get("session_id") or "")
    if not task_key:
        task_key = await run_in_threadpool(_task_key, run_id)
    if not task_key:
        return _error("no task key for run %r" % (run_id,), status=400)

    code = body.get("code")
    if code is not None:
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None

    try:
        # In a threadpool: every manager event takes the one queue lock and
        # persists the index, and blocking the event loop on a disk write would
        # stall every other request the page has in flight beside this one.
        await run_in_threadpool(_dispatch, kind, task_key, run_id, code)
    except Exception:  # noqa: BLE001 — see the module docstring
        logger.warning("queue event %s for %s (run %s) failed", kind, task_key,
                       run_id, exc_info=True)
        return {"ok": False}
    return {"ok": True}
