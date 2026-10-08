"""POST /api/run — run a page's `.py` (`runPython`) on the selected engine.

A plain run door. The Claude chat used to come through here too, with a
claude-only folder gate around it; it has its own in-process router now
(`server/routers/claude_agent.py`, gate in `claude_agent/gate.py`).

`/api/run/ws` is the same door over a per-page WebSocket (see `api_run_ws`);
both call `run_request`, so there is one body of run logic.
"""
import asyncio
import json
import logging
import time
import traceback
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, Body, Header, Request, Response, WebSocket, WebSocketDisconnect

from fused_render import calls as shell_calls
from fused_render import jobs as shell_jobs
from fused_render.server.common import _require_fused, resolve_py, ws_origin_ok
from fused_render.executor import dumps_result, run_python
from fused_render.server import git_status
from fused_render.shell import prefs as shell_prefs

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/run")
async def api_run(request: Request, body: dict = Body(...),
                  x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    status, content = await run_request(
        body, getattr(request.state, "fused_call", None))
    # dumps_result, not JSONResponse (see run_request): the in-process
    # executor path already serialized the payload (it has to, to validate
    # it), so this reuses that string instead of encoding a multi-MB result
    # a second time. The bytes are identical to JSONResponse's for every
    # other result — and for resolve_py's error, which run_request hands back
    # as that JSONResponse's own rendered body.
    return Response(content=content, status_code=status, media_type="application/json")


async def run_request(body: dict, call: dict | None) -> tuple[int, str]:
    """Run one request's `.py`; return ``(status, json_body)``.

    The whole of /api/run minus the transport, shared by the POST route and
    the `/api/run/ws` socket so the two cannot drift. ``call`` is the
    in-flight call record (calls.py) to enrich, or None."""
    py = body.get("py")
    html = body.get("html")
    params = body.get("params") or {}

    resolved, resolve_error = resolve_py(py, html)
    if resolve_error is not None:
        return resolve_error.status_code, resolve_error.body.decode("utf-8")

    # Engine dispatch (D69/§20): both paths return the same wire shape
    # ({ok, result, error:{type,message,traceback}, stdout} — the fused
    # engine adds stderr/duration_ms), so pages never see which ran.
    # Resolved per request: the Preferences switch applies to the next
    # run, no restart (a set FUSED_RENDER_ENGINE pins it instead).
    engine_used = shell_prefs.effective_engine()
    if engine_used == "fused":
        from fused_render import engine as _engine

        work = _engine.run_python(resolved, params)
    else:
        # The built-in executor blocks on a subprocess; keep the event
        # loop free (the endpoint is async now for the engine's sake).
        work = asyncio.to_thread(run_python, resolved, params)
    result = await work
    # A script that ran may have written anything, anywhere — including
    # through the git template's stage/unstage/commit (templates/git/ops.py),
    # which runs on this same subprocess-per-call path and so cannot reach
    # into THIS process's `git_status` cache itself (see that cache's own
    # `invalidate_status_cache` docstring). Dropping it here, unconditionally,
    # is the in-process mirror of `noteFsChanged()` (static/runtime.js) firing
    # on every runPython completion — success or failure, since a script that
    # wrote three files and then raised still changed the disk, and the same
    # is true of a git op that partially applied before erroring. Without
    # this, a listing fetched moments before the run — and still within
    # `_STATUS_CACHE_TTL_S` — would replay its now-stale answer to the very
    # refresh Job B's dir-watch fix triggers right after this call returns.
    git_status.invalidate_status_cache()
    # Hand the run's detail to the in-flight call record (calls.py): the
    # resolved .py, the params, the engine, and — on failure — the
    # traceback and output tails a user has since clicked away from. The
    # handler enriches; the middleware (or, for the socket, `api_run_ws`)
    # writes. (Whether an HTTP client hung up mid-run is decided by the
    # middleware — a route CANNOT see it; the NOT IMPLEMENTED note above
    # `no_cache_and_log` says why. The socket can, see `api_run_ws`.)
    shell_calls.enrich_run(
        call, resolved=resolved, params=params, engine=engine_used, result=result,
    )
    # Tell the runtime which absolute file actually ran so it can watch it
    # for auto-reload (LR-2). Set on failed runs too, so a broken py that
    # gets fixed still triggers a reload.
    result["resolved_py"] = resolved
    return 200, dumps_result(result)


# ---- /api/run over a WebSocket ----------------------------------------------
#
# WHY: WebKit allows 6 HTTP/1.1 connections per host:port, and that pool is
# shared by EVERY native WKWebView window of the app (measured 2026-10-08).
# A POST /api/run holds one of the six for the whole Python run — up to 60 s —
# so two or three slow runs plus the long-polls pin the pool and every window
# stalls: the same starvation /api/fs/events left SSE over (D74). WebSockets
# do NOT count toward that cap. So runtime.js's runPython carries its runs
# over one socket per page, and falls back to the POST above when the socket
# cannot open (a LAN peer — LanApp 1008s every socket but /api/fs/events —
# an older server, a proxy).
#
# Protocol: the page sends `{id, py, html, params, headers}` text frames; each
# answer is `{id, status, ...}` where `...` is EXACTLY the JSON body the POST
# returns for the same request (resolve_py's 400 body included — sent as a
# reply carrying its `status`, never as a socket close, so one bad call cannot
# take down the page's other runs). `headers` carries what the POST sends as
# request headers (X-Fused-Page/-Target/-Call/-Supersedes, from runtime.js
# callHeaders): the browser WebSocket API cannot set headers.
#
# Payload size: uvicorn's `ws_max_size` (16 MiB default, uvicorn/config.py) is
# handed to websockets' ServerProtocol as `max_size`, which bounds INCOMING
# messages only — what this handler sends is uncapped server-side, so large
# results are fine. Requests are small (a path + params); runtime.js routes
# a call whose message could exceed the inbound cap to the POST instead,
# before sending. WebKit documents no receive cap on a message (not measured).
#
# Parity with the HTTP path: `@app.middleware("http")` only wraps `http`
# scopes, so none of `no_cache_and_log`'s per-request work happens for a
# socket. This handler repeats it per run: the call record
# (shell_calls.begin_from/finish), the `x-fused-source` ambient source
# (shell_jobs.ambient_source) and the access-log line, as `WS /api/run`.

_WS_ROUTE = "/api/run"


@router.websocket("/api/run/ws")
async def api_run_ws(ws: WebSocket):
    # Cross-site WebSocket hijacking guard. The POST's X-Fused header works
    # only because it forces a CORS preflight a foreign page fails; a socket
    # has no preflight, so without this ANY website could open
    # ws://127.0.0.1:<port>/api/run/ws and execute Python. Closing before
    # accept answers the handshake with a 403.
    if not ws_origin_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()

    # One frame at a time on the wire: concurrent runs finish in any order
    # and each sends its own reply.
    send_lock = asyncio.Lock()
    state = {"open": True}
    tasks: set[asyncio.Task] = set()

    async def reply(text: str) -> bool:
        if not state["open"]:
            return False
        try:
            async with send_lock:
                await ws.send_text(text)
            return True
        except Exception:  # noqa: BLE001 — the page went away mid-send
            state["open"] = False
            return False

    async def handle(text: str) -> None:
        start = time.monotonic()
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            await reply(json.dumps({"id": None, "status": 400,
                                    "error": "message must be a JSON object"}))
            return
        rid = msg.get("id")
        raw_headers = msg.get("headers")
        headers = ({str(k).lower(): str(v) for k, v in raw_headers.items()}
                   if isinstance(raw_headers, dict) else {})
        call = shell_calls.begin_from(_WS_ROUTE, "WS", headers)
        raw_source = headers.get("x-fused-source")
        try:
            with shell_jobs.ambient_source(unquote(raw_source) if raw_source else ""):
                status, content = await run_request(msg, call)
        except asyncio.CancelledError:
            # Server shutdown cancelling the socket's runs — the middleware's
            # CancelledError branch, same outcome.
            shell_calls.finish(call, status=None,
                               elapsed_ms=(time.monotonic() - start) * 1000,
                               outcome="disconnected")
            raise
        except Exception as exc:  # noqa: BLE001 — mirrors unhandled_exception
            # What `no_cache_and_log` + `unhandled_exception` do for a route
            # that raises: access line, err_id minted, traceback logged, and a
            # 500 body carrying the traceback (D3 — the reader owns the box).
            err_id = uuid.uuid4().hex[:12]
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            dur = (time.monotonic() - start) * 1000
            logger.info("WS %s -> 500 (%.0f ms)", _WS_ROUTE, dur)
            logger.error("unhandled error on WS %s [err_id %s]\n%s", _WS_ROUTE, err_id, tb)
            shell_calls.finish(call, status=500, elapsed_ms=dur, outcome="error",
                               err_id=err_id)
            await reply(json.dumps({
                "id": rid, "status": 500,
                "error": f"fused-render internal error on WS {_WS_ROUTE} "
                         f"(err_id {err_id}):\n\n{tb}",
            }))
            return
        # Splice `id`/`status` into the already-rendered body rather than
        # re-encoding it: dumps_result exists so a multi-MB result is not
        # serialized twice. Every body here is a JSON object.
        head = '{"id":%s,"status":%d' % (json.dumps(rid), status)
        sent = await reply(head + ("," + content[1:] if content != "{}" else "}"))
        dur = (time.monotonic() - start) * 1000
        logger.info("WS %s -> %s (%.0f ms)", _WS_ROUTE, status, dur)
        # Unlike the POST (the NOT IMPLEMENTED note above `no_cache_and_log`),
        # a socket KNOWS its page left: a reply that could not be delivered is
        # recorded `disconnected`, not `ok`. `content_length` stands in for
        # the POST's Content-Length header (only read when enrich left
        # result_bytes unset — an error body).
        shell_calls.finish(
            call, status=status, elapsed_ms=dur,
            outcome=None if sent else "disconnected",
            content_length=(str(len(content.encode("utf-8")))
                            if call is not None and call.get("result_bytes") is None
                            else None),
        )

    try:
        # The receive loop is also how a hang-up is learned (api_fs_events'
        # pattern); each run is its own task so a page's runPythons stay
        # concurrent, exactly as parallel POSTs were.
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                continue
            task = asyncio.create_task(handle(text))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except WebSocketDisconnect:
        pass
    finally:
        state["open"] = False
        # NOT cancelled on hang-up: the POST path never cancels a run whose
        # client left (aborting the fetch does not reach the handler, the run
        # completes — runtime.js's superseded-reporting note), and a half-run
        # script that wrote some of its files is worse than a finished one.
        # Awaited so the runs stay owned, and recorded, until they finish —
        # even if this handler itself was cancelled (the gather does not pass
        # that on), same as a POST's run. `handle`'s CancelledError branch is
        # for the event loop's own teardown cancelling every task.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
