"""One UDF run, from a page's request to its answer (SPEC §54.2).

`run(msg, send)` is what the `/api/udf/ws` socket calls for each
`{op: "run"}` message. It sends the page, in order:

  {id, type: "saved", result}     the saved copy, when there is one
  {id, type: "started", kind}     the worker loaded the UDF and is running it
  {id, type: "done", result}      the fresh answer
  {id, type: "error", code, error, ...}   instead of "done"

`result` is `{token, url, content_type, size, saved_at, seconds, stdout,
stderr, fused_cached, source}`; `source` is "saved", "fused-cache" or
"fresh". The bytes are at `url` (GET /api/udf/results/<token>).

Error codes: bad_ref, bad_env, env_locked, signed_out, unavailable,
udf_error (with line/stdout/stderr), needs_confirmation (with
execution_type/title/reason; nothing ran), too_large (with size/limit; past
MAX_BYTES), timeout, cancelled, failed.

THE GATE for batch and map UDFs is the worker's (it reads the UDF's code
after loading it): without `confirmed: true` such a run answers
needs_confirmation and nothing runs. The runtime asks the user and sends
`confirmed` only on their yes.

Two identical requests (same run key, same `confirmed` and cache flag) in
flight at once share one run.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable

from fused_render import canvas_node_exec
from fused_render.udf import runner, store

logger = logging.getLogger(__name__)

KEY_VERSION = 1
FORMATS = ("auto", "html", "png", "json", "parquet")
CACHE_MODES = ("auto", "prefer-saved", "fresh")
# A realtime run is cut off by Fused well inside this.
RUN_TIMEOUT_S = 330.0
# A batch job starts a machine and is waited for.
BATCH_RUN_TIMEOUT_S = 3600.0
SCHEMA_TIMEOUT_S = 120.0
# A result past this never reaches a page (it travels whole).
MAX_BYTES = 128 * 1024 * 1024
# A UDF's source is code someone wrote; past this it is not one.
SOURCE_MAX_BYTES = 2 * 1024 * 1024

Send = Callable[[dict], Awaitable[None]]


class RunError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra

    def frame(self, rid) -> dict:
        return dict(self.extra, id=rid, type="error", code=self.code, error=str(self))


class Target:
    """What to run, resolved: where (`env`), what `fused.load` gets
    (`load_as`, `collection`) and what identifies it for the run key."""

    def __init__(self, env: str, source: str, collection: str | None, ident: list,
                 file_sha: str | None = None):
        self.env = env
        self.load_as = source
        self.collection = collection
        self.ident = ident
        self.file_sha = file_sha


def _sha_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _env_ok(env) -> str:
    from fused_render import canvases

    good, bad = canvases._require_env(env)
    if bad is not None:
        body = json.loads(bad.body)
        raise RunError("env_locked" if body.get("code") == "env_locked" else "bad_env",
                       body.get("error") or "unknown environment")
    return good


def resolve(ref) -> Target:
    """A page's reference -> a Target. `{file, env?}`: a UDF's .py on disk; a
    canvas node file runs in its canvas's environment and canvas, any other
    .py needs `env`. `{canvas, name, env?}`: a UDF as saved on Fused (`env`
    defaults to the workbench environment)."""
    from fused_render import canvases

    if not isinstance(ref, dict):
        raise RunError("bad_ref", "the UDF reference must be an object")
    file = ref.get("file")
    if file is not None:
        if not isinstance(file, str) or not file:
            raise RunError("bad_ref", "'file' must be a path")
        path = os.path.normpath(os.path.expanduser(file))
        if not os.path.isabs(path) or not path.endswith(".py"):
            raise RunError("bad_ref", "'file' must be the absolute path of a .py file")
        try:
            if os.path.getsize(path) > SOURCE_MAX_BYTES:
                raise RunError("bad_ref", "this file is too large to be a UDF's source")
            sha = _sha_of_file(path)
        except OSError as exc:
            raise RunError("bad_ref", f"could not read the file: {exc}") from exc
        info = canvases.canvas_node_file(path)
        if info is not None and info["kind"] == "udf":
            env, collection = info["env"], info["name"]
        else:
            env, collection = ref.get("env"), None
            if env is None:
                raise RunError("bad_ref", "this file is not a node of a canvas you work on: "
                                          "say which Fused environment to run it in ('env')")
        env = _env_ok(env)
        return Target(env, path, collection, ["file", path, sha], file_sha=sha)
    canvas, name = ref.get("canvas"), ref.get("name")
    if not (isinstance(canvas, str) and canvas and isinstance(name, str) and name):
        raise RunError("bad_ref", "give a UDF as {file} or as {canvas, name}")
    if "/" in name or os.sep in name or name.endswith(".py"):
        raise RunError("bad_ref", "'name' is a UDF's name, not a path")
    env = _env_ok(ref.get("env") or canvases.workbench_env())
    return Target(env, name, canvas, ["name", canvas, name])


def run_key(target: Target, account: str | None, params: dict, fmt: str) -> str:
    material = json.dumps([KEY_VERSION, target.env, account or "", target.ident, params, fmt],
                          sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _page_result(meta: dict, source: str | None = None) -> dict:
    token = meta["token"]
    return {
        "token": token,
        # Versioned: a key's token is reused when a fresh answer replaces
        # the saved one, and an <img> must not keep the old bytes.
        "url": f"/api/udf/results/{token}?v={int((meta.get('saved_at') or 0) * 1000)}",
        "content_type": meta.get("content_type") or "",
        "size": meta.get("size") or 0,
        "saved_at": meta.get("saved_at"),
        "seconds": meta.get("seconds"),
        "stdout": meta.get("stdout") or "",
        "stderr": meta.get("stderr") or "",
        "fused_cached": bool(meta.get("fused_cached")),
        "source": source or ("fused-cache" if meta.get("fused_cached") else "fresh"),
    }


class _Flight:
    """One run in flight that several pages may be waiting on."""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.listeners: list[Callable[[dict], None]] = []
        self.waiting = 0
        self.env = ""
        self.rid = ""


_FLIGHTS: dict[tuple, _Flight] = {}


async def _execute(flight: _Flight, target: Target, key: str, params: dict, fmt: str,
                   cache: str, confirmed: bool) -> dict:
    """Run once in the worker; the outcome every waiter of `flight` gets:
    {"result": meta} or {"error": RunError}."""
    token, body_path = store.new_body_path()
    flight.env, flight.rid = target.env, token
    deadline = {"s": RUN_TIMEOUT_S}

    def on_event(event: dict) -> None:
        if event.get("event") == "started":
            if event.get("kind") not in (None, canvas_node_exec.NONE):
                deadline["s"] = BATCH_RUN_TIMEOUT_S
            for listener in list(flight.listeners):
                listener({"type": "started", "kind": event.get("kind")})

    request = {"op": "run", "id": token, "source": target.load_as,
               "collection": target.collection, "params": params, "format": fmt,
               "cache": cache != "fresh", "confirmed": confirmed, "out": body_path}
    task = asyncio.ensure_future(runner.run(target.env, request, on_event))
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        while True:
            remaining = deadline["s"] - (loop.time() - started)
            if remaining <= 0:
                runner.cancel(target.env, token)
                task.cancel()
                raise RunError("timeout", f"the run timed out after {int(deadline['s'])} s")
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if done:
                break
        final = task.result()
    except runner.RunnerUnavailable as exc:
        store.drop_body(body_path)
        raise RunError("unavailable", str(exc)) from exc
    except runner.WorkerGone as exc:
        store.drop_body(body_path)
        raise RunError("failed", str(exc)) from exc
    except RunError:
        store.drop_body(body_path)
        raise
    if final.get("event") == "needs_confirmation":
        store.drop_body(body_path)
        kind = final.get("kind") or ""
        raise RunError("needs_confirmation", canvas_node_exec.paused_reason(kind),
                       execution_type=kind, title=canvas_node_exec.confirm_title(kind),
                       reason=canvas_node_exec.paused_reason(kind))
    if not final.get("ok"):
        store.drop_body(body_path)
        if final.get("cancelled"):
            raise RunError("cancelled", "the run was cancelled")
        raise RunError("udf_error", final.get("error") or "the UDF raised an error",
                       line=final.get("line"), stdout=final.get("stdout") or "",
                       stderr=final.get("stderr") or "")
    size = final.get("size") or 0
    if size > MAX_BYTES:
        store.drop_body(body_path)
        raise RunError("too_large", f"the result is {size / 1e6:.1f} MB, too large to show "
                                    f"(the limit is {MAX_BYTES / 1e6:.0f} MB)",
                       size=size, limit=MAX_BYTES)
    keep_key = key
    if target.file_sha is not None and final.get("code_sha256") != target.file_sha:
        # The worker loaded other code than the file the key was made from
        # (it changed in between): the answer is not filed under the key.
        # `code_sha256` is the hash of the code that ran (`udf.code`, the
        # file's text as fused.load read it).
        keep_key = None
    meta = {"content_type": final.get("content_type") or "", "size": size,
            "seconds": final.get("seconds"), "stdout": final.get("stdout") or "",
            "stderr": final.get("stderr") or "", "fused_cached": bool(final.get("fused_cached")),
            "env": target.env}
    return {"result": await asyncio.to_thread(store.keep, body_path, meta, keep_key)}


async def run(msg: dict, send: Send, cancelled: asyncio.Event | None = None) -> None:
    """Answer one `{op: "run"}` message (see the module doc)."""
    rid = msg.get("id")
    try:
        await _run(msg, send, rid, cancelled)
    except RunError as err:
        await send(err.frame(rid))
    except Exception as exc:  # noqa: BLE001 — a bug must still answer the page
        logger.exception("udf run failed")
        await send({"id": rid, "type": "error", "code": "failed",
                    "error": f"the run failed: {exc}"})


async def _run(msg: dict, send: Send, rid, cancelled: asyncio.Event | None) -> None:
    from fused_render import canvases

    target = await asyncio.to_thread(resolve, msg.get("ref"))
    params = msg.get("params") if msg.get("params") is not None else {}
    if not isinstance(params, dict):
        raise RunError("bad_ref", "'params' must be an object")
    fmt = msg.get("format") or "auto"
    if fmt not in FORMATS:
        raise RunError("bad_ref", f"'format' must be one of: {', '.join(FORMATS)}")
    cache = msg.get("cache") or "auto"
    if cache not in CACHE_MODES:
        raise RunError("bad_ref", f"'cache' must be one of: {', '.join(CACHE_MODES)}")
    confirmed = msg.get("confirmed") is True
    if not canvases._logged_in(target.env):
        raise RunError("signed_out", "not signed in to Fused — sign in first")
    account = canvases._listing_account(target.env)
    key = run_key(target, account, params, fmt)

    if cache != "fresh":
        saved = await asyncio.to_thread(store.get, key)
        if saved is not None:
            await send({"id": rid, "type": "saved", "result": _page_result(saved, "saved")})
            if cache == "prefer-saved":
                await send({"id": rid, "type": "done", "result": _page_result(saved, "saved")})
                return

    flight_key = (key, confirmed, cache == "fresh")
    flight = _FLIGHTS.get(flight_key)
    if flight is None:
        flight = _Flight()
        _FLIGHTS[flight_key] = flight

        async def go():
            try:
                return await _execute(flight, target, key, params, fmt, cache, confirmed)
            except RunError as err:
                return {"error": err}
            finally:
                if _FLIGHTS.get(flight_key) is flight:
                    _FLIGHTS.pop(flight_key, None)

        flight.task = asyncio.ensure_future(go())

    started_frames: list[dict] = []

    def listen(frame: dict) -> None:
        started_frames.append(frame)
        asyncio.ensure_future(send(dict(frame, id=rid)))

    flight.listeners.append(listen)
    flight.waiting += 1
    try:
        waits = {flight.task}
        stop = None
        if cancelled is not None:
            stop = asyncio.ensure_future(cancelled.wait())
            waits.add(stop)
        done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        if stop is not None and stop in done and not flight.task.done():
            if flight.waiting == 1 and flight.rid:
                # Unlisted first: a retry right after starts a new run instead
                # of joining this one and getting its "cancelled".
                if _FLIGHTS.get(flight_key) is flight:
                    _FLIGHTS.pop(flight_key, None)
                runner.cancel(flight.env, flight.rid)
            raise RunError("cancelled", "the run was cancelled")
        if stop is not None:
            stop.cancel()
        outcome = flight.task.result()
    finally:
        flight.waiting -= 1
        try:
            flight.listeners.remove(listen)
        except ValueError:
            pass
    if "error" in outcome:
        raise outcome["error"]
    await send({"id": rid, "type": "done", "result": _page_result(outcome["result"])})


async def schema(msg: dict, send: Send) -> None:
    """Answer one `{op: "schema"}` message: `{id, type: "schema", schema}`
    (the UDF's parameters as an OpenAPI operation) or an error frame."""
    rid = msg.get("id")
    try:
        from fused_render import canvases

        target = await asyncio.to_thread(resolve, msg.get("ref"))
        if not canvases._logged_in(target.env):
            raise RunError("signed_out", "not signed in to Fused — sign in first")
        request = {"op": "schema", "id": "s" + secrets.token_hex(8),
                   "source": target.load_as, "collection": target.collection}
        try:
            final = await asyncio.wait_for(runner.run(target.env, request), SCHEMA_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise RunError("timeout", "reading the UDF's parameters timed out") from exc
        except runner.RunnerUnavailable as exc:
            raise RunError("unavailable", str(exc)) from exc
        except runner.WorkerGone as exc:
            raise RunError("failed", str(exc)) from exc
        if not final.get("ok"):
            raise RunError("udf_error", final.get("error") or "could not read the UDF")
        await send({"id": rid, "type": "schema", "schema": final.get("schema")})
    except RunError as err:
        await send(err.frame(rid))
    except Exception as exc:  # noqa: BLE001
        logger.exception("udf schema failed")
        await send({"id": rid, "type": "error", "code": "failed", "error": str(exc)})
