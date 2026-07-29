import asyncio
import logging
import os
import time
import traceback
import uuid
import httpx
from fastapi import Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from fused_render import calls as shell_calls



logger = logging.getLogger(__name__)


def _forced_engine() -> str | None:
    """The process-level engine override, or None when unset (D69/D70 + §20).

    FUSED_RENDER_ENGINE forces the /api/run engine for the whole process:
    `builtin` never touches the `fused` package even if importable; `auto`
    opts in to it iff importable; `fused` demands it (a missing package is a
    startup error, not a silent fallback). **Unset returns None** — the
    engine then follows the persisted preference (shell/prefs.py, default
    builtin — D70 stands), re-read per request so the Preferences page's
    switch applies without a restart. Logged either way — engine choice
    changes the code contract, so it must never be silent.
    """
    raw = os.environ.get("FUSED_RENDER_ENGINE")
    if raw is None:
        logger.info(
            "execution engine: following the preference (~/.fused-render/prefs.json, "
            "default builtin); FUSED_RENDER_ENGINE overrides it for this process"
        )
        return None
    requested = raw.strip().lower()
    if requested not in ("auto", "fused", "builtin"):
        raise RuntimeError(
            f"FUSED_RENDER_ENGINE={requested!r} is not one of: auto, fused, builtin"
        )
    if requested == "builtin":
        logger.info("execution engine: builtin (forced by FUSED_RENDER_ENGINE)")
        return "builtin"
    try:
        from fused_render import engine as _engine

        ok = _engine.available()
    except ImportError:
        ok = False
    if ok:
        logger.info("execution engine: fused (forced by FUSED_RENDER_ENGINE)")
        return "fused"
    if requested == "fused":
        raise RuntimeError(
            "FUSED_RENDER_ENGINE=fused but the `fused` package is not importable; "
            "install it (pip install 'fused-render[fused]') or unset the override"
        )
    logger.info("execution engine: builtin (FUSED_RENDER_ENGINE=auto, `fused` not installed)")
    return "builtin"

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Guard for the mutating/executing POSTs. Read endpoints are already safe
    # cross-origin because the browser blocks a foreign page from reading our
    # response; but a POST can be fired blind (no-cors fetch) by any website,
    # with no way to read the reply. Requiring a custom request header forces a
    # CORS preflight, which fails cross-origin since we return no CORS headers —
    # so only our own same-origin pages get through. Not authentication (D3
    # stands): it only blocks blind cross-origin POSTs, nothing more.
    if x_fused != "1":
        return _error("missing or invalid X-Fused header", status=403)
    return None


# Shared keep-alive HTTP pool for the opt-in pooled /api/fs/raw proxy
# (TASK F). The pyramid/geotiff workers range-read a store's signed URL one
# ~64KB block at a time; a per-block urllib GET (and, before this, a 307
# they re-followed per block) pays a fresh TLS handshake every read — serial,
# multi-second cold. One AsyncClient with a connection pool lets those range
# reads reuse sockets to the store. Created at startup, closed at shutdown,
# stashed on app.state so api_fs_raw can await through it.
async def open_pooled_client(app):
    app.state.pooled_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0),
        limits=httpx.Limits(max_keepalive_connections=32,
                            max_connections=64),
    )

async def close_pooled_client(app):
    client = getattr(app.state, "pooled_client", None)
    if client is not None:
        await client.aclose()

async def unhandled_exception(request, exc):
    # A bare "Internal Server Error" with an empty body is undebuggable on
    # a DMG install: Finder-launched apps have no visible stderr, so the
    # traceback used to vanish (e.g. a right-click "Open with FusedRender"
    # that 500s on /render or /api/run leaves nothing to report). Put the
    # traceback in the response body (local single-user tool, D3 — the
    # only reader owns the machine) AND in the log file so a later
    # `Open app logs` gives the full story. Log with the request line so a
    # noisy log still pins the failure to a URL.
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # The id the middleware already stamped onto this request's call record
    # (it runs first, then re-raises past us). Echoing it here is what makes
    # `err_id` in the call log an actual join key rather than a dead field:
    # a screenshot of this 500 and the record in the log name each other.
    err_id = getattr(request.state, "fused_err_id", None)
    logger.error(
        "unhandled error on %s %s%s\n%s", request.method, request.url.path,
        f" [err_id {err_id}]" if err_id else "", tb
    )
    return _error(
        f"fused-render internal error on {request.method} "
        f"{request.url.path}"
        + (f" (err_id {err_id})" if err_id else "")
        + f":\n\n{tb}",
        status=500,
    )
# NOT IMPLEMENTED: detecting that the client hung up mid-request, which would
# let an abandoned run be recorded as `disconnected` instead of a served
# request (SPEC CL-5a's named gap). Both obvious approaches are dead ends
# under this app's shape, verified rather than assumed:
#
#   * From a ROUTE: `BaseHTTPMiddleware` (which `@app.middleware("http")`
#     builds) wraps the downstream `receive`, so `request.is_disconnected()`
#     inside a handler never observes `http.disconnect` — a watcher there
#     waits forever. Without the middleware in front it fires immediately,
#     which is what makes this easy to "verify" wrongly.
#   * From HERE: the middleware's own request CAN see the disconnect, but
#     `is_disconnected()` peeks by CONSUMING a message off the receive
#     channel (starlette.requests, an immediately-cancelled CancelScope
#     around `_receive()`). Polling it steals the `http.request` body message
#     the downstream route is waiting for, so every request with a body —
#     /api/run included — hangs. A body-less spike hides this completely.
#
# Doing it properly means converting this middleware to pure ASGI so it can
# tee the receive channel instead of racing the route for it. That is a
# change to the hottest path in the server and belongs in its own commit,
# not riding along with the call log. Meanwhile a supersession IS reported by
# the page (CL-5a), which covers the common slider case; a closed tab or
# reload still records as `ok`.

_LOG_SKIP_PREFIXES = ("/static/", "/template-assets/", "/template-shared/")

async def no_cache_and_log(request, call_next):
    # The app call log's single write point (calls.py, design §4.5). begin()
    # returns a record only for a request carrying runtime.js's
    # X-Fused-Page header — so the shell's own /api/fs/list, the conditions
    # probe, and every non-page caller are excluded by construction rather
    # than by an endpoint blocklist that would drift. Route handlers enrich
    # the same dict through request.state.fused_call; only finish() writes.
    call = shell_calls.begin(request)
    request.state.fused_call = call
    # App code changes between restarts and user files change on disk;
    # stale browser caches of shell/runtime JS cause confusing half-old UIs.
    # Also the browser request log (SPEC SV-3): one INFO line per request
    # with status + duration, so the log reconstructs the sequence of calls
    # a page made — the context you need to see *which* request 500'd and
    # what led to it. A 500 raised in a route escapes call_next; log the
    # request line before re-raising so the access trail stays complete
    # (the catch-all handler then logs the traceback).
    path = request.url.path
    logged = not path.startswith(_LOG_SKIP_PREFIXES)
    start = time.monotonic()
    try:
        response = await call_next(request)
    except asyncio.CancelledError:
        # The client went away mid-request — overwhelmingly a runPython
        # superseded by a newer call for the same .py (D114/RH-9) or a
        # closed tab. Recorded as its own outcome and kept out of every
        # latency statistic: a slider scrub would otherwise report dozens
        # of "slow" calls for what the user experienced as one request.
        if call is not None:
            shell_calls.finish(
                call, status=None, elapsed_ms=(time.monotonic() - start) * 1000,
                outcome="disconnected",
            )
        raise
    except Exception:
        if logged:
            dur = (time.monotonic() - start) * 1000
            logger.info("%s %s -> 500 (%.0f ms)", request.method, path, dur)
        # An unhandled exception escapes call_next: @app.exception_handler
        # runs in ServerErrorMiddleware, OUTSIDE user middleware, so this
        # except branch is the only place the record can be closed out.
        # Mint the correlation id here (we run before the handler) and stash
        # it so the handler can echo the same id into the 500 body.
        err_id = uuid.uuid4().hex[:12]
        request.state.fused_err_id = err_id
        if call is not None:
            shell_calls.finish(
                call, status=500, elapsed_ms=(time.monotonic() - start) * 1000,
                outcome="error", err_id=err_id,
            )
        raise
    if logged:
        dur = (time.monotonic() - start) * 1000
        logger.info(
            "%s %s -> %s (%.0f ms)", request.method, path, response.status_code, dur
        )
    if call is not None:
        shell_calls.finish(
            call,
            status=response.status_code,
            elapsed_ms=(time.monotonic() - start) * 1000,
            content_length=response.headers.get("content-length"),
        )
    response.headers["Cache-Control"] = "no-cache"
    return response


def get_start_dir(request: Request) -> str:
    return request.app.state.start_dir


def get_shell_path(request: Request) -> str:
    return request.app.state.shell_path
