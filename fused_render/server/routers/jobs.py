"""The background-job registry's HTTP surface (`fused_render/jobs.py`).

Four routes, and the split between them is who is talking:

  POST /api/jobs               a REPORTER says where its work is up to
  GET  /api/jobs               the SHELL asks what to draw
  POST /api/jobs/{id}/cancel   the SHELL asks a reporter to stop
  POST /api/jobs/{id}/dismiss  the SHELL closes a finished row
  POST /api/jobs/clear         ...or all of them at once

The reporter's POST answers with the stored record, which is what makes cancel
work without a second channel: the reporter learns `cancel_requested` in the
reply to the tick it was going to send anyway (SPEC BG-4). A reporter that only
ever posts and never reads the reply still works — it just cannot be cancelled
from the manager.

Reads are unguarded and writes carry the X-Fused header, exactly as everywhere
else: a blind cross-origin POST is the thing the header stops, and the job list
is not a secret to any page already running on this origin.
"""

import time
from urllib.parse import unquote

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render import jobs as jobs_mod
from fused_render.server.common import _error, _require_fused

router = APIRouter()


@router.get("/api/jobs")
def api_jobs_list():
    """Every live record, oldest first.

    `now` rides along so the client measures age against the SERVER's clock.
    The two clocks are the same machine's, but they are not the same reading —
    a client subtracting its own `Date.now()` from a server timestamp shows a
    job as stalled (or as finishing in the future) whenever the browser tab has
    been throttled or the timestamps crossed a suspend.
    """
    now = time.time()
    return {"jobs": jobs_mod.list_jobs(now=now), "now": now}


@router.post("/api/jobs")
def api_jobs_report(body: dict = Body(...), x_fused: str | None = Header(default=None),
                    x_fused_page: str | None = Header(default=None),
                    x_fused_worker: str | None = Header(default=None)):
    """One progress report. Creates the record on the first tick, updates it after.

    The page is taken from the X-Fused-Page header rather than the body: it is
    the attribution header every runtime call already carries (calls.py), so a
    report is attributed by the same rule as the call log and a reporter cannot
    accidentally claim a different page by typing one into its body.

    **A model worker reports here too** (SPEC §40) — it is the process doing the
    downloading, so it is the only one that knows the byte counts. Its rows live
    under the reserved `sys:` prefix that pages may not write, so the endpoint
    has to tell a worker from a page: `X-Fused-Worker` carries the token this
    server generated and passed into that worker's environment, and only an
    exact match against a LIVE worker's token unlocks the prefix. Not a secret
    shared with anything else, and gone the moment the worker stops.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # Imported at call time: `jobs.py` must not import anything under `ai/`, and
    # the supervisor holds the only list of live tokens.
    from fused_render.ai import supervisor

    is_worker = supervisor.is_worker_token(x_fused_worker or "")
    # runtime.js sends the path encodeURIComponent'd, like every other
    # X-Fused-* path header; unquote is the identity for a raw ASCII path, so a
    # curl or a test that sends one plain is unaffected.
    page = unquote(x_fused_page) if x_fused_page else ""
    try:
        return jobs_mod.upsert(body, page=page, server=is_worker)
    except jobs_mod.JobError as e:
        return _error(str(e))


@router.post("/api/jobs/clear")
def api_jobs_clear(x_fused: str | None = Header(default=None)):
    """Dismiss every finished (or stalled) row. Live work is left alone."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return {"cleared": jobs_mod.clear_finished()}


@router.post("/api/jobs/{job_id}/cancel")
def api_jobs_cancel(job_id: str, x_fused: str | None = Header(default=None)):
    """Flag a running job for cancellation; its reporter acts on it.

    404 when there is no such record — the row the user clicked has aged out or
    was never there, and answering 200 would leave the manager showing a
    Cancel that quietly did nothing.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        job_id = jobs_mod.clean_id(job_id)
    except jobs_mod.JobError as e:
        return _error(str(e))
    record = jobs_mod.request_cancel(job_id)
    if record is None:
        return _error(f"no such job: {job_id}", status=404)
    return record


@router.post("/api/jobs/{job_id}/dismiss")
def api_jobs_dismiss(job_id: str, x_fused: str | None = Header(default=None)):
    """Close a finished (or stalled) row. A live one is refused (409) — cancel it."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        job_id = jobs_mod.clean_id(job_id)
    except jobs_mod.JobError as e:
        return _error(str(e))
    if not jobs_mod.dismiss(job_id):
        return JSONResponse(
            {"error": f"{job_id} is still being reported on — cancel it instead "
                      "of dismissing it, or it has already gone"},
            status_code=409,
        )
    return {"dismissed": job_id}
