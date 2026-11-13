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
import os
import secrets
import threading

from fastapi import APIRouter, Body, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from fused_render import health, jobs
from fused_render.server.common import _error, _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()

# An outage record is a dozen scalar fields plus a short latency list; 64 KB is
# two orders of magnitude of headroom and still keeps a runaway client from
# writing megabytes per beacon into a file the bundle ships.
MAX_OUTAGE_BODY = 64 * 1024

_NO_STORE = {"Cache-Control": "no-store"}

# Where the diagnostics job row opens: Preferences scrolled to the row. One of
# `jobs._ORIGIN_BY_ROUTE`'s shell routes, so the dock navigates instead of
# treating it as an fs path.
DIAGNOSTICS_PAGE = "/preferences?s=diagnostics"

# How often a running diagnostics build re-reports; well under
# `jobs.STALE_AFTER_S` so the dock never calls it stalled.
HEARTBEAT_S = 10.0


@router.get("/api/health")
async def api_health(request: Request):
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

    The paired loopback origin may read the response cross-origin: the shell
    probes the OTHER loopback name (a window on `127.0.0.1:<p>` probes
    `localhost:<p>` and vice versa), because WebKit caps connections per host
    and every window's long-polls fill the page's own pool, so a probe on the
    same host queues behind them and reads as "server down". Only that exact
    pair (same scheme, same port the request arrived on) is allowed, since the
    body carries pid and version. No preflight route: the probe is a plain GET
    with no custom headers.
    """
    headers = dict(_NO_STORE)
    origin = request.headers.get("origin")
    port = request.url.port
    if origin is not None and port is not None and origin in (
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    ):
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return JSONResponse(health.snapshot(), headers=headers)


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


@router.post("/api/health/visibility")
async def api_health_visibility(request: Request, x_fused: str | None = Header(default=None)):
    """One page-side visibility report (`visibilitychange`, focus/blur, a
    requestAnimationFrame stall while visible), appended to `visibility.jsonl`
    and echoed to the app log. Same shape and limits as the outage record;
    guarded by `X-Fused` since it is sent with `fetch` keepalive, never a
    beacon."""
    denied = _require_fused(x_fused)
    if denied is not None:
        return denied
    raw = await request.body()
    if len(raw) > MAX_OUTAGE_BODY:
        return _error("visibility record too large", status=413)
    try:
        body = json.loads(raw or b"null")
    except (ValueError, UnicodeDecodeError):
        return _error("visibility record is not JSON")
    if not isinstance(body, dict):
        return _error("visibility record must be a JSON object")
    try:
        await run_in_threadpool(health.record_visibility, body)
    except OSError as exc:
        logger.warning("could not record visibility: %s", exc)
        return _error(f"could not record visibility: {exc}", status=500)
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
    """Write the diagnostics zip (default `~/Downloads`) and return its path.

    Sync `def`: copying logs and zipping is disk work and belongs on the
    threadpool, not the event loop. Body is optional — `{since_s}` narrows
    the window; omitted means `build_bundle`'s own default (the larger of a
    day and since this server booted).

    `X-Fused` guarded like every other writing POST: unlike the outage
    beacon, this one drops a zip in the user's Downloads, and a blind
    cross-origin POST must not be able to do that.
    """
    if (err := _require_fused(x_fused)):
        return err

    since_s = _since_from_body(body)
    # `system_log` (macOS `log show`) is the one slow step, 15-45 s; the
    # Preferences button sends false unless its checkbox is on.
    system_log = bool(body.get("system_log")) if isinstance(body, dict) else False
    # Mirrored into the job registry so the build shows in the bottom
    # Activity dock: the user can leave Preferences while it runs (the
    # system log alone is 15-45 s) and still see it finish. The row links
    # back to the Diagnostics row, which keeps the result in a module store.
    job_id = f"{jobs.SERVER_ID_PREFIX}diagnostics:{secrets.token_hex(4)}"

    def report(**fields) -> None:
        try:
            jobs.upsert({"id": job_id, "kind": "task", **fields},
                        page=DIAGNOSTICS_PAGE, origin="Diagnostics", server=True)
        except Exception:  # noqa: BLE001 — reporting must never break the work
            logger.debug("could not report the diagnostics job")

    report(title="Saving diagnostics", state=jobs.RUNNING,
           detail="Reading the system log…" if system_log else "Collecting…")
    # `build_bundle` only ticks between phases, and `log show` alone can run
    # 45 s — past `jobs.STALE_AFTER_S`, which would paint a working build as
    # "no longer reporting". A bare re-report every few seconds keeps it fresh.
    finished = threading.Event()

    def heartbeat() -> None:
        while not finished.wait(HEARTBEAT_S):
            report()

    try:
        threading.Thread(target=heartbeat, name="diagnostics-heartbeat", daemon=True).start()
    except RuntimeError:  # best-effort: no thread means a stale row, not no bundle
        logger.debug("could not start the diagnostics heartbeat")
    try:
        from fused_render import diagnostics

        path = diagnostics.build_bundle(
            since_s=since_s, system_log=system_log,
            progress=lambda msg: report(detail=msg),
        )
    except Exception as exc:  # noqa: BLE001 — the user clicked a button; say why
        finished.set()
        logger.exception("diagnostics bundle failed")
        report(title="Diagnostics failed", state="error", detail="", message=str(exc))
        return _error(f"could not write diagnostics bundle: {exc}", status=500)
    finished.set()
    report(title="Diagnostics saved", state="done", detail=os.path.basename(path))
    out = {"path": path}
    try:
        out["bytes"] = os.path.getsize(path)
    except OSError:
        pass
    return out
