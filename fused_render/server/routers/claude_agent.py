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
`ACTIONS` names the seventeen actions `agent.main` dispatches and the handler
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
import importlib.util
import logging
import os
import threading
import time
import traceback
from types import ModuleType

from fastapi import APIRouter, Body, Header, Request, Response
from fastapi.responses import JSONResponse

from fused_render import calls as shell_calls
from fused_render import claude_agent
from fused_render._binding import ParamError, bind_params
from fused_render.claude_agent import gate, pool
from fused_render.executor import dumps_result
from fused_render.server import git_status
from fused_render.server.common import _error, _require_fused

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
    "sessions": "_sessions",
    "live_run": "_live_run",
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
    `abspath("")` would quietly turn it into (D1309)."""
    if not isinstance(dir, str) or not dir.strip():
        return {"entry": None}
    return {"entry": _app_entry().entry_html(dir)}


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
    caller must still file what it did (D1309; see the 504 branch of
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
            # NEVER RAN (D1309). The whole budget went on waiting for a pool
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
    # request was cancelled under it (the client hung up mid-send, D1309) — is
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

    envelope, status, cfut = await _run(
        action, pool.budget(action), agent.main, bound,
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
            # to give back (D1309).
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
        getattr(request.state, "fused_call", None),
        resolved=claude_agent.AGENT_PATH, params=params, engine=ENGINE,
        result=envelope,
    )
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
        getattr(request.state, "fused_call", None),
        resolved=os.path.abspath(artifacts.__file__), params=params,
        engine=ENGINE, result=envelope,
    )
    return _respond(envelope, status)
