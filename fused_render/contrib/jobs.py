"""A tiny in-memory registry for the contribution loop's long operations
(contract §16.9): a proposal draft, a send, a review, an inbox accept. The
HTTP route starts one and answers `{ok, job}`; the page polls
`GET /api/contrib/job?id=` until it is done or failed.

One daemon thread per job. Jobs live in this process only: a server restart
forgets them, and the page simply starts again. Finished jobs are dropped an
hour after they end.

ERRORS ARE PLAIN. A job function that wants to refuse with a sentence the user
should read raises `PlainError("...", reason=...)`; that sentence becomes the
job's `message`, and the optional `reason` (a code the page branches on, e.g.
"signed-out" to run sign-in first; `gh.GitHubError` carries one) its
`reason`. Any other exception is logged with its traceback and surfaces as
the generic "Something went wrong. Try again." — raw exception text (git's
stderr, a path, a traceback) never reaches the page.

PROGRESS. A long job names the step it is on with `set_step(job_id, key)`
(a short key the page maps to words, e.g. "fork", "push"); `step` is in the
public dict. The function learns its own id from `current_id()`, which
answers inside the job's own thread only.

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
    """Raised by a job function to fail with a user-readable sentence, and
    optionally a `reason` code for the page to branch on."""

    def __init__(self, message: str, reason: str | None = None):
        super().__init__(message)
        self.message = message
        self.reason = reason


_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_local = threading.local()


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
                                 "message", "reason", "step", "started_at",
                                 "ended_at")}


def _run(job_id: str, fn: Callable[[], dict]) -> None:
    status, result, message, reason = "done", None, None, None
    _local.job_id = job_id
    try:
        result = fn()
        if result is not None and not isinstance(result, dict):
            result = {"value": result}
        # A job that finished but has something to say (the page shows it
        # as a toast): "Sent, GitHub hasn't caught up yet".
        if isinstance(result, dict) and isinstance(result.get("message"), str):
            message = result["message"] or None
    except PlainError as e:
        status, message = "failed", e.message or GENERIC_MESSAGE
        reason = getattr(e, "reason", None)
    except Exception:
        with _lock:
            kind = (_jobs.get(job_id) or {}).get("kind")
        logger.exception("contrib job %s (%s) failed", job_id, kind)
        status, message = "failed", GENERIC_MESSAGE
    finally:
        _local.job_id = None
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(status=status, result=result, message=message,
                       reason=reason, ended_at=time.time())


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
            "reason": None, "step": None, "started_at": now, "ended_at": None,
        }
    threading.Thread(target=_run, args=(job_id, fn), daemon=True,
                     name=f"contrib-job-{kind}").start()
    return job_id


def current_id() -> str | None:
    """The id of the job running on THIS thread, else None."""
    return getattr(_local, "job_id", None)


def set_step(job_id: str | None, key: str | None) -> None:
    """Name the step a running job is on (a no-op for an unknown id)."""
    if not job_id:
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is not None and job["status"] == "running":
            job["step"] = key


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
