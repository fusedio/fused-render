"""A tiny in-memory registry for the contribution loop's long operations
(contract §12.3): a proposal draft, a send, a review, an inbox accept. The
HTTP route starts one and answers `{ok, job}`; the page polls
`GET /api/contrib/job?id=` until it is done or failed.

One daemon thread per job. Jobs live in this process only: a server restart
forgets them, and the page simply starts again. Finished jobs are dropped an
hour after they end.

ERRORS ARE PLAIN. A job function that wants to refuse with a sentence the user
should read raises `PlainError("...")`; that sentence becomes the job's
`message`. Any other exception is logged with its traceback and surfaces as
the generic "Something went wrong. Try again." — raw exception text (git's
stderr, a path, a traceback) never reaches the page.

DEDUPE. By default one job per (kind, app_dir) runs at a time: a second start
while the first is running returns the first's id, so a double click or two
open pages never run the same operation twice.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable

logger = logging.getLogger(__name__)

GENERIC_MESSAGE = "Something went wrong. Try again."
KEEP_AFTER_END_S = 3600.0


class PlainError(Exception):
    """Raised by a job function to fail with a user-readable sentence."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _prune_locked(now: float) -> None:
    stale = [
        jid
        for jid, j in _jobs.items()
        if j["ended_at"] is not None and now - j["ended_at"] > KEEP_AFTER_END_S
    ]
    for jid in stale:
        del _jobs[jid]


def _public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "kind", "app_dir", "status", "result",
                                 "message", "started_at", "ended_at")}


def _run(job_id: str, fn: Callable[[], dict]) -> None:
    status, result, message = "done", None, None
    try:
        result = fn()
        if result is not None and not isinstance(result, dict):
            result = {"value": result}
    except PlainError as e:
        status, message = "failed", e.message or GENERIC_MESSAGE
    except Exception:
        job = _jobs.get(job_id) or {}
        logger.exception("contrib job %s (%s) failed", job_id, job.get("kind"))
        status, message = "failed", GENERIC_MESSAGE
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(status=status, result=result, message=message,
                       ended_at=time.time())


def start(kind: str, app_dir: str, fn: Callable[[], dict], *,
          dedupe: bool = True) -> str:
    """Run `fn` on a daemon thread and return its job id. With `dedupe`, a
    running job of the same kind for the same app is returned instead."""
    now = time.time()
    with _lock:
        _prune_locked(now)
        if dedupe:
            for j in _jobs.values():
                if (j["kind"] == kind and j["app_dir"] == app_dir
                        and j["status"] == "running"):
                    return j["id"]
        job_id = uuid.uuid4().hex[:16]
        _jobs[job_id] = {
            "id": job_id, "kind": kind, "app_dir": app_dir,
            "status": "running", "result": None, "message": None,
            "started_at": now, "ended_at": None,
        }
    threading.Thread(target=_run, args=(job_id, fn), daemon=True,
                     name=f"contrib-job-{kind}").start()
    return job_id


def get(job_id: str) -> dict | None:
    with _lock:
        _prune_locked(time.time())
        job = _jobs.get(job_id)
        return _public(job) if job is not None else None


def latest(kind: str, app_dir: str) -> dict | None:
    """The most recently started job of `kind` for `app_dir`, if still kept."""
    with _lock:
        _prune_locked(time.time())
        best = None
        for j in _jobs.values():
            if j["kind"] == kind and j["app_dir"] == app_dir:
                if best is None or j["started_at"] >= best["started_at"]:
                    best = j
        return _public(best) if best is not None else None


def wait(job_id: str, timeout: float = 30.0) -> dict | None:
    """Block until the job ends or `timeout` passes (tests + simple callers)."""
    deadline = time.time() + timeout
    while True:
        job = get(job_id)
        if job is None or job["status"] != "running" or time.time() >= deadline:
            return job
        time.sleep(0.02)
