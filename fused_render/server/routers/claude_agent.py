"""POST /api/claude/agent, /api/claude/app-entry, /api/claude/artifacts — the
chat's backend, in process.

Until 0.6.5 the React chat reached `templates/claude/agent.py` through
`POST /api/run`: one fresh Python interpreter per call, including the ~400 ms
poll every open chat runs. That was the single largest source of interpreter
churn the server had (`fused_render/claude_agent/__init__.py` has the numbers).
These routes run the SAME `main()` on the one in-process module instance
(`claude_agent.agent_module()`), so the port is a transport change and the
answers are the bytes `/api/run` carried in its `result` field.

ONE DISPATCH ENDPOINT, an explicit allowlist — the `claude_config.py` precedent.
`ACTIONS` names the nineteen actions `agent.main` dispatches and the handler
each reaches. The allowlist is the gate (an unknown action is a 400 before
anything is bound or run); the call itself goes through `agent.main`, not the
handler, because `main` is where the string-shaped wire params become handler
arguments — "1"/"0" flags to bools, `has_pane`'s three states, `native` to
`app_reads`, `queue` to `inbox`, `enrich`/`deltas` read in opposite directions,
the missing-`file` refusals. Re-deriving that here would be a second copy of
the one table that decides what a param means, and the first drift would
change a chat's behaviour with no test on either side noticing.

BINDING IS `_binding.bind_params`, the binder `/api/run`'s child used, not
`inspect.signature(...).bind`. It DROPS keys `main` does not take, and the
page's body always carries some: `_file` (the queue target the gate reads),
`queue_claim` (the admission token). A strict bind would 400 every send. It
also str-coerces like the child did, so a value reaches `main` exactly as it
did through `/api/run`.

Per request, in `/api/run`'s order: X-Fused guard (D3); allowlist; bind;
`gate._folder_busy` for start/send (a refusal is a 200 `{"error": ...}`, the
string the composer shows verbatim, with nothing else run — as before); the
handler on `pool.pool_for(action)` under its `pool.budget`; `gate._file_owner`;
`git_status.invalidate_status_cache()`; the call-log enrich. A handler's own
`{"error": ...}` is a 200 like any answer. An exception is a 500 and a blown
budget a 504, both shaped like `/api/run`'s error object so the page's
AgentError mapping is unchanged — and BOTH still run `_file_owner`, because a
start that raised is exactly the case that must give back the placeholder its
gate minted (see `gate._file_owner`).
"""
from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import logging
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType
from urllib.parse import unquote

from fastapi import (APIRouter, Body, Header, Request, Response, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import JSONResponse

from fused_render import calls as shell_calls
from fused_render import jobs as shell_jobs
from fused_render import netlog
from fused_render import claude_agent, claude_remote
from fused_render import tasks_watch
from fused_render._binding import ParamError, bind_params
from fused_render.claude_agent import gate, pool
from fused_render.executor import dumps_result
from fused_render.server import git_status
from fused_render.server.common import _error, _require_fused, ws_origin_ok

logger = logging.getLogger(__name__)
router = APIRouter()

#: action -> the `agent.py` handler `main` dispatches it to. The keys are the
#: allowlist; the values document the route (and a test can hold them to the
#: module: every one must exist on `agent_module()`).
ACTIONS: dict[str, str] = {
    "start": "_start",
    "poll": "_poll",
    "decide": "_decide",
    "app_state": "_answer_app_state",
    "canvas_shot": "_answer_canvas_shot",
    "sessions": "_sessions",
    "live_run": "_live_run",
    "cards": "_cards",
    "defaults": "_defaults",
    "history": "_history",
    "snapshots": "_snapshots",
    "snapshot_plan": "_snapshot_plan",
    "snapshot_revert": "_snapshot_revert",
    "shots_dir": "_shots_dir",
    "image_to_png": "_image_to_png",
    "terminal_command": "_terminal_command",
    "cancel": "_cancel",
    "live_host": "_live_host",
    "send": "_send",
    "set_mode": "_set_mode",
    "commands": "_commands",
    "task_output": "_task_output",
    "task_stop": "_task_stop",
    "task_background": "_task_background",
    "tasks": "_run_tasks",
}

#: The engine name the call log records for these routes. `/api/run` wrote
#: "builtin" or "fused"; neither is true here, and the Calls page only shows it.
ENGINE = "inprocess"

_SHARED_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "templates", "shared")
_APP_ENTRY_PATH = os.path.join(_SHARED_DIR, "app_entry.py")
_APP_ENTRY: ModuleType | None = None
_APP_ENTRY_LOCK = threading.Lock()


def _app_entry() -> ModuleType:
    """`templates/shared/app_entry.py`, loaded by path once.

    By path, the way every shared helper is reached (it is a template-side
    module and is not part of the package's import graph), and NOT by leaning
    on the `sys.path` insert `agent.py` happens to make at import: that would
    tie this route to an import side effect of a different module."""
    global _APP_ENTRY
    if _APP_ENTRY is None:
        with _APP_ENTRY_LOCK:
            if _APP_ENTRY is None:
                spec = importlib.util.spec_from_file_location(
                    "fused_render_shared_app_entry", _APP_ENTRY_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _APP_ENTRY = mod
    return _APP_ENTRY


def _app_entry_main(dir: str = "") -> dict:
    """What `templates/claude/app.py::main` answered: the html file the chat's
    left pane renders for a project folder, by the shell's "Open as app" rule
    (D301), shared with the `app` template through `entry_html`.

    A blank `dir` is "no folder", not the server's cwd — which is what
    `abspath("")` would quietly turn it into (D1310)."""
    if not isinstance(dir, str) or not dir.strip():
        return {"entry": None}
    return {"entry": _app_entry().entry_html(dir)}


#: Actions that may spawn a session, and so carry the agent gate.
_SPAWN_ACTIONS = ("start", "send")
#: The `reason`s an agent refusal carries (`agent._backend_disabled_error`,
#: the plan-mode refusal) — answered as a 400 wherever they come from.
_REFUSAL_REASONS = ("backend_disabled", "unsupported")


def _backend_refusal(action: str, bound: dict, agent=None) -> dict | None:
    """The 400 body for a `start`/`send` the agent gate refuses, else None.

    A `send` is gated on the RUN's backend whenever the run is on disk — a
    follow-up is a turn of that run's agent whatever the request says
    (invariant 8), and a bare send to a live host of an agent switched off
    since must not slip through. Otherwise only when the request NAMES a
    backend: a bare `start` is Claude for a new chat and the session's own
    for a resume, both of which `_start` checks itself (and `_send` repeats
    the run check, for callers that never come through here — the
    scheduler's live-host path). `backend` reaches `main` as a string (the
    binder's str-coercion), so `""` is "not named"."""
    if action not in _SPAWN_ACTIONS:
        return None
    backend = str(bound.get("backend") or "")
    if action == "send":
        backend = _run_backend(agent, str(bound.get("run_id") or "")) or backend
    if not backend:
        return None
    from fused_render.agent_backends import registry
    if not registry.is_known(backend):
        return {"error": f"unknown agent backend: {backend!r}",
                "reason": "backend_unknown", "backend": backend}
    if not registry.is_enabled(backend):
        return registry.disabled_error(backend)
    if (not registry.get(backend).caps.plan_mode
            and str(bound.get("permission_mode") or "") == "plan"):
        return {"error": f"plan mode is not available for "
                         f"{registry.get(backend).label}",
                "reason": "unsupported", "backend": backend}
    return None


def _run_backend(agent, run_id: str) -> str:
    """The backend of run `run_id` off its meta.json, "" when there is no
    such run (or no agent module that can say where runs live) — and "" for
    a bot's Claude run, which the Claude switch does not govern (D17/Q7;
    `_send` applies the same exemption)."""
    runs = getattr(agent, "RUNS", None)
    bad_id = getattr(agent, "_bad_id", None)
    run_backend = getattr(agent, "_run_backend", None)
    if not (runs and bad_id and run_backend) or bad_id(run_id):
        return ""
    run_dir = os.path.join(runs, run_id)
    meta_path = os.path.join(run_dir, "meta.json")
    if not os.path.isfile(meta_path):
        return ""
    backend = str(run_backend(run_dir) or "")
    if backend == "claude":
        try:
            with open(meta_path, encoding="utf-8") as fh:
                if (json.load(fh) or {}).get("bot"):
                    return ""
        except (OSError, ValueError, AttributeError):
            pass
    return backend


def _error_obj(exc: BaseException) -> dict:
    """`/api/run`'s error object for an exception a handler raised."""
    return {"type": type(exc).__name__, "message": str(exc),
            "traceback": traceback.format_exc()}


def _envelope_of(fut) -> dict:
    """A finished pool future as `/api/run`'s result envelope — the shape
    `gate._file_owner` and `calls.enrich_run` read."""
    if fut.cancelled():
        return {"ok": False, "error": {"type": "Cancelled",
                                       "message": "the call never ran"}}
    exc = fut.exception()
    if exc is not None:
        return {"ok": False, "error": {
            "type": type(exc).__name__, "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc))}}
    return {"ok": True, "result": fut.result()}


async def _run(label: str, budget: float, fn, kwargs: dict,
               executor=None, on_cancel=None):
    """Run `fn(**kwargs)` on `executor` (default `pool.POOL`) under `budget`
    seconds.

    Returns `(envelope, status, cfut)`: status 200 with `{"ok": True,
    "result": ...}`, 500 with the exception's error object, or 504 when the
    budget expired — in which case `cfut` is the still-running (or cancelled
    before it started) work, for a caller that has to act when it lands.

    If the REQUEST is cancelled while waiting (the client went away, the
    server is shutting down), `on_cancel(cfut)` runs before the
    CancelledError propagates: the work may still land, and a start/send
    caller must still file what it did (D1310; see the 504 branch of
    `api_claude_agent`)."""
    # THE HANDLER'S OWN TIME, measured on the worker thread around the call
    # alone, as `duration_ms` on the envelope — the field `calls.enrich_run`
    # files as `run_ms`, which `/api/run`'s child used to report. Queue wait
    # is excluded on purpose: the call log's `server_ms` already includes it,
    # and the gap between the two is what shows a saturated pool.
    timing: dict = {}

    def timed():
        t0 = time.perf_counter()
        try:
            return fn(**kwargs)
        finally:
            timing["ms"] = (time.perf_counter() - t0) * 1000.0

    cfut = (executor or pool.POOL).submit(timed)
    try:
        result = await asyncio.wait_for(asyncio.wrap_future(cfut), budget)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel(cfut)
        raise
    except TimeoutError:
        if cfut.cancelled() or cfut.cancel():
            # NEVER RAN (D1310). The whole budget went on waiting for a pool
            # worker — the clock starts at submit — and the job is now
            # cancelled, so the handler ran zero times and nothing will land
            # later. A different answer from "Timeout" on purpose: the caller
            # (and the gate, which files this like any failed call) can treat
            # it as a clean failure with no late outcome to wait for.
            logger.warning("claude agent: %s never got a worker in %s s",
                           label, budget)
            return ({"ok": False, "error": {
                "type": "NotRun",
                "message": f"{label} waited {budget:g} s for a worker and never ran"}},
                504, cfut)
        # The thread keeps running until the handler returns (pool.py says
        # why that is accepted); only the caller stops waiting. No
        # duration_ms: the handler has not finished, so there is none yet.
        logger.warning("claude agent: %s exceeded %s s", label, budget)
        return ({"ok": False, "error": {
            "type": "Timeout", "message": f"{label} exceeded {budget:g} s"}},
            504, cfut)
    except Exception as exc:  # noqa: BLE001 — a handler bug is a 500, not a crash
        logger.exception("claude agent: %s failed", label)
        return ({"ok": False, "error": _error_obj(exc),
                 "duration_ms": timing.get("ms")}, 500, cfut)
    return {"ok": True, "result": result, "duration_ms": timing.get("ms")}, 200, cfut


def _respond(envelope: dict, status: int) -> Response:
    if status == 200:
        try:
            # dumps_result, not JSONResponse: the same encoder /api/run used
            # (`ensure_ascii=False`, compact separators), so the body is the
            # bytes that sat in its `result` field.
            return Response(content=dumps_result(envelope["result"]),
                            media_type="application/json")
        except (TypeError, ValueError) as exc:
            # `allow_nan=False` and a non-JSON value are the handler's bug;
            # /api/run's child failed the same way, as an error, not a crash.
            logger.exception("claude agent: unserializable result")
            return JSONResponse({"error": _error_obj(exc)}, status_code=500)
    return JSONResponse({"error": envelope["error"]}, status_code=status)


@router.post("/api/claude/agent")
async def api_claude_agent(request: Request, body: dict = Body(default={}),
                           x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return await agent_dispatch(body, getattr(request.state, "fused_call", None))


async def agent_dispatch(body: dict, call: dict | None) -> Response:
    """Everything after the transport's guard: the POST route's body, shared
    with the `/api/claude/agent/ws` socket (whose guard is its origin check).
    `call` is the call-log record (`request.state.fused_call` for a POST)."""
    action = body.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        return _error(f"unknown claude agent action: {action!r}")
    try:
        agent = claude_agent.agent_module()
    except Exception as exc:  # noqa: BLE001 — a packaging bug, said loudly
        logger.exception("claude agent: the agent module did not import")
        return JSONResponse({"error": _error_obj(exc)}, status_code=500)
    # `params` is the page's fields as sent, for the gate and the call log;
    # `body` is the per-request CARRIER the gate may write its minted claim
    # token onto (`_queue_admit_token`) for `_file_owner` to consume — kept
    # apart so that token never reaches `main` or the log, the same split
    # /api/run kept between its `params` and its body.
    params = dict(body)
    try:
        bound = bind_params(agent.main, params)
    except ParamError as exc:
        return _error(f"{action}: {exc}")

    # THE AGENT GATE (docs/COPILOT_BACKEND.md D17, invariant 10). A `start`
    # naming a backend that Preferences has switched off, or a `send` into a
    # run of one (or naming one, for a run not on disk), is a 400
    # with the registry's body, BEFORE the queue gate below mints a
    # placeholder for a run that must not exist. Request-level only: `_start`
    # keeps the one check that actually stands between a request and a spawn
    # (it reads prefs.json itself), so a resume that names no backend is
    # still refused there if the session's own agent is off. Plan mode is
    # refused the same way for Copilot (§7 caps.plan_mode).
    refused = _backend_refusal(action, bound, agent)
    if refused is not None:
        return JSONResponse(refused, status_code=400)

    # THE QUEUE'S GATE, ON THE SERVER TOO (Akshil's QA, 2026-09-16). The chat
    # asks `/api/tasks/queue/admit` before it spawns, but a page whose copy of
    # the pref was stale — a tab opened before the switch, a send made before
    # its prefs read landed — skipped the door and started a second run in a
    # busy folder. The door is the server's rule, so the server holds it here
    # as well: a `start`/`send` into a folder another task is RUNNING in is
    # refused with the same words the composer shows for a refused admission.
    # `_folder_busy` answers "" for every other action.
    refused = gate._folder_busy(params, body)
    if refused:
        return Response(content=dumps_result({"error": refused}),
                        media_type="application/json")

    # THE START MAY STILL LAND. A `_start` that blew its budget — or whose
    # request was cancelled under it (the client hung up mid-send, D1310) — is
    # running on in its pool thread and may yet spawn a host; filing it as
    # failed NOW would drop the placeholder its gate minted and leave that run
    # owning nothing, and filing nothing at all would leak the `admit:`
    # placeholder for good. So the filing waits for the work: whenever it
    # finishes (or at once, if it was cancelled before it began),
    # `_file_owner` sees the real outcome — a run to file, or an error to
    # release the claim for.
    def _late(fut, params=params, body=body):
        # `late` for a start: a retry may own the folder by now, and a late
        # run must not take it back (see `gate._file_owner`). A send is filed
        # as before — it is the folder's own session continuing.
        gate._file_owner(params, _envelope_of(fut), body,
                         late=(action == "start"))
        git_status.invalidate_status_cache()

    def _on_cancel(fut):
        fut.add_done_callback(_late)

    handler = (functools.partial(claude_remote.admit_call, agent.main)
               if action in _SPAWN_ACTIONS else agent.main)
    envelope, status, cfut = await _run(
        action, pool.budget(action), handler, bound,
        executor=pool.pool_for(action),
        on_cancel=_on_cancel if action in ("start", "send") else None)
    timed_out = status == 504 and envelope["error"].get("type") == "Timeout"
    if status == 504 and action == "start":
        # A start that never ran was admitted by `/api/tasks/queue/admit`
        # (flag on), whose placeholder `_folder_busy` consumed with no token on
        # `body` — `_drop_placeholder` cannot see it, and it would hold the
        # folder until its TTL against the user's retry. Released by the
        # request's own `queue_claim`, for both 504 kinds.
        key = gate._queue_target(params)
        if key:
            gate._drop_admitted_placeholder(key, params, body)
    if timed_out and action in ("start", "send"):
        if action == "start":
            # THE FOLDER GOES BACK NOW, not when the late `_start` lands. The
            # page shows the Timeout and the user retries — and a placeholder
            # still holding the folder would refuse that retry as "another
            # task is running" for a run that may never exist. Released here;
            # the late filing below still runs, and `_file_owner` then files
            # `started` for a `_start` that did spawn (`started` guards
            # against overwriting a retry that has since claimed the folder),
            # while its own drop is a no-op on a token already released.
            # Only `start`: a timed-out `send` minted no placeholder of its own
            # to give back (D1310).
            key = gate._queue_target(params)
            if key:
                gate._drop_placeholder(key, body)
        cfut.add_done_callback(_late)
    else:
        # THE FOLDER'S OWNER IS FILED HERE, at the one place a chat's turn is
        # actually spawned — see `gate._file_owner`. Nothing before this call
        # has a run id or a session to file under for a brand-new chat's first
        # send. Runs on the error paths too: a start that raised must give back
        # a placeholder its gate minted. A NotRun 504 lands here as well: the
        # handler never ran, so it is filed now as the failure it is (the
        # placeholder dropped, a send's spent claim restored) with no `_late`.
        gate._file_owner(params, envelope, body)
    if action == "cancel" and status == 200:
        # THE TASKS PAGE HEARS THE STOP NOW. Its own readers (the CLI's
        # registry row, the transcript tail) only notice a stop once the CLI
        # writes its interrupt marker — a beat later, and later still for a
        # stop pressed before the first turn opened (`agent._start_pending_stop`,
        # whose interrupt deliberately waits). The chat already flipped; the
        # row flips with it. `mark_turn_ended` is the queue manager's own
        # word, the same one the session host files on a `result` row.
        result = envelope.get("result") or {}
        session_id = str(result.get("session_id") or "") if isinstance(result, dict) else ""
        if session_id:
            tasks_watch.mark_turn_ended(session_id, str(bound.get("run_id") or ""))
            tasks_watch.notify({session_id})
    # A turn writes files (and `_poll` commits them at turn end), so the git
    # status cache is dropped after every call, success or failure — the same
    # unconditional drop /api/run made after every script.
    git_status.invalidate_status_cache()
    # The call log's record (calls.py): attributed to agent.py, with the page's
    # params and — on failure — the error a user has since clicked away from.
    # A record exists only when the page sent `X-Fused-Page`
    # (`claude_agent.CLAUDE_PAGE_ID`), which `calls.is_first_party` files as
    # first-party.
    shell_calls.enrich_run(
        call,
        resolved=claude_agent.AGENT_PATH, params=params, engine=ENGINE,
        result=envelope,
    )
    # THE HANDLER'S OWN AGENT REFUSAL IS A 400 TOO. `_start`/`_send` gate on
    # the agent they actually resolve — a bare start, a resume whose own
    # agent is off, a plan-mode Copilot turn — which the request-level gate
    # above cannot see. Same body, same status, so the page reads one shape
    # (its `disabledBackendOf` looks at 400s only).
    result = envelope.get("result") if status == 200 else None
    if (action in _SPAWN_ACTIONS and isinstance(result, dict)
            and result.get("reason") in _REFUSAL_REASONS):
        return JSONResponse(result, status_code=400)
    return _respond(envelope, status)


@router.post("/api/claude/app-entry")
async def api_claude_app_entry(request: Request, body: dict = Body(default={}),
                               x_fused: str | None = Header(default=None)):
    """`{"dir": <abs>}` → `{"entry": <abs> | null}` — what the chat's
    `runPython("./app.py")` answered (the file is gone with the template)."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    params = dict(body)
    try:
        bound = bind_params(_app_entry_main, params)
    except ParamError as exc:
        return _error(f"app-entry: {exc}")
    envelope, status, _ = await _run("app-entry", pool.DEFAULT_BUDGET_S,
                                     _app_entry_main, bound)
    shell_calls.enrich_run(
        getattr(request.state, "fused_call", None),
        resolved=_APP_ENTRY_PATH, params=params, engine=ENGINE,
        result=envelope,
    )
    return _respond(envelope, status)


@router.post("/api/claude/artifacts")
async def api_claude_artifacts(request: Request, body: dict = Body(default={}),
                               x_fused: str | None = Header(default=None)):
    """`{"action": "list"|"live", "file", "session_id"}` → `{"artifacts": [...]}`
    or `{"error": ...}` — `claude_agent/artifacts.py::main`, the module the
    chat used to run through `/api/run`. Imported lazily: its import puts
    `templates/shared` on `sys.path`, which no other route here needs."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return await artifacts_dispatch(body, getattr(request.state, "fused_call", None))


async def artifacts_dispatch(body: dict, call: dict | None) -> Response:
    """`api_claude_artifacts` past its guard; also the socket's `artifacts`."""
    try:
        from fused_render.claude_agent import artifacts
    except Exception as exc:  # noqa: BLE001
        logger.exception("claude agent: artifacts did not import")
        return JSONResponse({"error": _error_obj(exc)}, status_code=500)
    params = dict(body)
    try:
        bound = bind_params(artifacts.main, params)
    except ParamError as exc:
        return _error(f"artifacts: {exc}")
    envelope, status, _ = await _run("artifacts", pool.DEFAULT_BUDGET_S,
                                     artifacts.main, bound)
    shell_calls.enrich_run(
        call,
        resolved=os.path.abspath(artifacts.__file__), params=params,
        engine=ENGINE, result=envelope,
    )
    return _respond(envelope, status)


# ---- the chat's agent/artifacts calls over a WebSocket -----------------------
#
# WHY: every chat poll (~400 ms) and send is a POST that holds one of WebKit's
# six per-host HTTP/1.1 connections, a pool shared by every native window of
# the app (same reason `/api/run/ws` exists, routers/run.py). Sockets do not
# count toward that cap. Transport only: the same `agent_dispatch` /
# `artifacts_dispatch` run, on the same pool lanes and budgets, so a cancel or
# decide still rides CONTROL_POOL and the poll rate is unchanged.
#
# Protocol: `{id, route: "agent"|"artifacts", headers, body}` frames, answered
# by `{"id": .., "status": n, "body": <the POST's JSON body>}`. The body is
# NESTED rather than spliced (as run.ws does): a poll result can itself carry
# `id` / `status` keys. The socket's guard is the origin check (a socket has
# no CORS preflight, so X-Fused cannot protect it). LanApp 1008s every socket
# but /api/fs/events, so a LAN peer falls back to POST.

_WS_ROUTES = {
    "agent": ("/api/claude/agent", agent_dispatch),
    "artifacts": ("/api/claude/artifacts", artifacts_dispatch),
}


@router.websocket("/api/claude/agent/ws")
@netlog.tracked_ws("claude-agent")
async def api_claude_agent_ws(ws: WebSocket):
    if not ws_origin_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()

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

    def frame(rid, status: int, body_json: str) -> str:
        return '{"id":%s,"status":%d,"body":%s}' % (json.dumps(rid), status,
                                                   body_json or "null")

    async def handle(text: str) -> None:
        start = time.monotonic()
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            await reply(frame(None, 400, json.dumps(
                {"error": "message must be a JSON object"})))
            return
        rid = msg.get("id")
        entry = _WS_ROUTES.get(msg.get("route"))
        body = msg.get("body")
        if entry is None or not isinstance(body, dict):
            what = ("unknown route" if entry is None else "body must be an object")
            await reply(frame(rid, 400, json.dumps(
                {"error": f"{what}: {msg.get('route')!r}"})))
            return
        route, dispatch = entry
        raw_headers = msg.get("headers")
        headers = ({str(k).lower(): str(v) for k, v in raw_headers.items()}
                   if isinstance(raw_headers, dict) else {})
        call = shell_calls.begin_from(route, "WS", headers)
        raw_source = headers.get("x-fused-source")
        try:
            with shell_jobs.ambient_source(unquote(raw_source) if raw_source else ""):
                resp = await dispatch(body, call)
        except asyncio.CancelledError:
            shell_calls.finish(call, status=None,
                               elapsed_ms=(time.monotonic() - start) * 1000,
                               outcome="disconnected")
            raise
        except Exception as exc:  # noqa: BLE001 — mirrors unhandled_exception
            err_id = uuid.uuid4().hex[:12]
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            dur = (time.monotonic() - start) * 1000
            logger.info("WS %s -> 500 (%.0f ms)", route, dur)
            logger.error("unhandled error on WS %s [err_id %s]\n%s", route, err_id, tb)
            shell_calls.finish(call, status=500, elapsed_ms=dur, outcome="error",
                               err_id=err_id)
            await reply(frame(rid, 500, json.dumps({
                "error": f"fused-render internal error on WS {route} "
                         f"(err_id {err_id}):\n\n{tb}",
            })))
            return
        content = bytes(resp.body).decode("utf-8")
        sent = await reply(frame(rid, resp.status_code, content))
        dur = (time.monotonic() - start) * 1000
        logger.info("WS %s -> %s (%.0f ms)", route, resp.status_code, dur)
        shell_calls.finish(
            call, status=resp.status_code, elapsed_ms=dur,
            outcome=None if sent else "disconnected",
            content_length=(str(len(content.encode("utf-8")))
                            if call is not None and call.get("result_bytes") is None
                            else None),
        )

    try:
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
        # NOT cancelled on hang-up, as for the POST (an aborted fetch never
        # reaches the handler): a half-run `send` is worse than a finished
        # one, and its gate bookkeeping must still be filed.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


#: `/api/claude/worktrees`' actions, and its budget. 20 s (SPEC W-3): a
#: `git worktree add` checks out a whole tree, which the 60 s default would
#: let hang a menu far past the point anyone is still waiting on it.
WORKTREE_ACTIONS = ("list", "create", "remove")
WORKTREE_BUDGET_S = 20.0
#: The worktree route's own two threads, NOT `pool.POOL`: a create can hold a
#: thread for most of its 20 s (and past it, since a timed-out handler runs
#: on), and the chat's 400 ms polls must never queue behind a checkout.
_WORKTREE_POOL = ThreadPoolExecutor(max_workers=2,
                                    thread_name_prefix="claude-worktrees")


@router.post("/api/claude/worktrees")
async def api_claude_worktrees(request: Request, body: dict = Body(default={}),
                               x_fused: str | None = Header(default=None)):
    """`{"action": "list"|"create"|"remove", "file", "name"?, "base"?,
    "force"?}` → `claude_agent/worktrees.py`'s dict verbatim (its own
    `{"error": ...}` is a 200 answer, like every handler here). `file` is the
    chat's `_file`, any path inside the repo. The git calls run on
    `_WORKTREE_POOL` under `WORKTREE_BUDGET_S` (the module keeps its own
    deadline inside it), so a slow checkout is at worst a 504 with the agent
    route's error object, never a stalled event loop or a starved poll."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    action = body.get("action")
    if not isinstance(action, str) or action not in WORKTREE_ACTIONS:
        return _error(f"unknown worktree action: {action!r}")
    from fused_render.claude_agent import worktrees
    params = dict(body)
    try:
        bound = bind_params(worktrees.main, params)
    except ParamError as exc:
        return _error(f"worktrees: {exc}")
    envelope, status, _ = await _run(f"worktrees {action}", WORKTREE_BUDGET_S,
                                     worktrees.main, bound,
                                     executor=_WORKTREE_POOL)
    if action != "list":
        # A new or removed worktree moves what git reports for the repo.
        git_status.invalidate_status_cache()
    shell_calls.enrich_run(
        getattr(request.state, "fused_call", None),
        resolved=os.path.abspath(worktrees.__file__), params=params,
        engine=ENGINE, result=envelope,
    )
    return _respond(envelope, status)
