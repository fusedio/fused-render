"""Liveness probe, the client outage record, and the diagnostics bundle (SPEC §50).

Three routes over `fused_render.health` / `fused_render.diagnostics`:

* `GET /api/health` — the probe the shell's disconnect banner polls. Tiny and
  dependency-free so "the probe failed" means the server is down or wedged,
  not that some unrelated lock was held (see the handler's docstring).
* `POST /api/health/outage` — one outage the shell observed, appended to
  `outages.jsonl`. Sent with `navigator.sendBeacon` (or `fetch` keepalive) as
  the tab recovers or unloads, so it can carry NO custom headers.
* `POST /api/diagnostics` + `GET /api/diagnostics/plan` — the Preferences
  "Save diagnostics" button: preview what would be collected, then write the
  zip.

`diagnostics` is imported inside the handlers, not at module top: it is the
heaviest of the three (a log/crash-report walk plus zip writing) and only the
two diagnostics routes need it, and a broken import there must not take the
liveness probe — the one route that has to work when other things don't —
down with it.
"""
import json
import logging

from fastapi import APIRouter, Body, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from fused_render import health
from fused_render.server.common import _error, _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()

# An outage record is a dozen scalar fields plus a short latency list; 64 KB is
# two orders of magnitude of headroom and still keeps a runaway client from
# writing megabytes per beacon into a file the bundle ships.
MAX_OUTAGE_BODY = 64 * 1024

_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/api/health")
async def api_health():
    """Boot id, pid, uptime, version, server clock — nothing else.

    `async def` on purpose: a sync route runs on Starlette's 40-thread pool,
    which `/api/config`, `/api/tasks` and the long-polls share. When that pool
    is saturated a sync probe queues behind them and the banner reads "server
    down" for a server that is merely busy — exactly the false alarm this
    probe exists to tell apart (D1). On the event loop it answers as long as
    the loop itself is alive.

    Not `/api/config`, which the banner used to poll: that route is sync, takes
    the update manager's RLock and can fork for the Full Disk Access check, so
    its latency measures those, not liveness. `health.snapshot()` takes no
    locks and touches no disk.
    """
    return JSONResponse(health.snapshot(), headers=_NO_STORE)


@router.post("/api/health/outage")
async def api_health_outage(request: Request):
    """Append one client-observed outage to `outages.jsonl`.

    No `X-Fused` guard, deliberately: `navigator.sendBeacon` cannot set
    headers, and a beacon is the only send that survives the tab unloading
    mid-outage. The blast radius of a blind cross-origin POST here is one
    line in a bounded, size-capped local log whose keys `record_outage`
    filters to `OUTAGE_FIELDS` — not worth losing the record over.

    The body is parsed by hand rather than through a pydantic model: a beacon
    Blob's content type is whatever the client set (or `text/plain`), and a
    validation 422 on a diagnostics side channel would just drop the record.
    """
    raw = await request.body()
    if len(raw) > MAX_OUTAGE_BODY:
        return _error("outage record too large", status=413)
    try:
        body = json.loads(raw or b"null")
    except (ValueError, UnicodeDecodeError):
        return _error("outage record is not JSON")
    if not isinstance(body, dict):
        return _error("outage record must be a JSON object")
    try:
        # A small locked append; off the loop anyway so a slow disk can't
        # stall the very probe that would report it.
        await run_in_threadpool(health.record_outage, body)
    except OSError as exc:
        logger.warning("could not record outage: %s", exc)
        return _error(f"could not record outage: {exc}", status=500)
    return {"ok": True}


def _since_from_body(body) -> float | None:
    if not isinstance(body, dict):
        return None
    since = body.get("since_s")
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        return None
    return float(since)


@router.get("/api/diagnostics/plan")
def api_diagnostics_plan(since_s: float | None = None):
    """What a bundle would hold right now: file count, bytes, window start,
    crash reports — so the button can say "N files, M MB" before writing.
    Sync: it walks the log home (threadpool)."""
    try:
        from fused_render import diagnostics

        return diagnostics.summarize_plan(since_s=since_s)
    except Exception as exc:  # noqa: BLE001 — surface, never 500 with no message
        logger.exception("diagnostics plan failed")
        return _error(f"could not plan diagnostics bundle: {exc}", status=500)


@router.post("/api/diagnostics")
def api_diagnostics(
    body: dict | None = Body(default=None),
    x_fused: str | None = Header(default=None),
):
    """Write the diagnostics zip (default `~/Desktop`) and return its path.

    Sync `def`: copying logs and zipping is disk work and belongs on the
    threadpool, not the event loop. Body is optional — `{since_s}` narrows
    the window; omitted means `build_bundle`'s own default (the larger of a
    day and since this server booted).

    `X-Fused` guarded like every other writing POST: unlike the outage
    beacon, this one drops a zip on the user's Desktop, and a blind
    cross-origin POST must not be able to do that.
    """
    import os

    if (err := _require_fused(x_fused)):
        return err

    since_s = _since_from_body(body)
    try:
        from fused_render import diagnostics

        path = diagnostics.build_bundle(since_s=since_s)
    except Exception as exc:  # noqa: BLE001 — the user clicked a button; say why
        logger.exception("diagnostics bundle failed")
        return _error(f"could not write diagnostics bundle: {exc}", status=500)
    out = {"path": path}
    try:
        out["bytes"] = os.path.getsize(path)
    except OSError:
        pass
    return out
