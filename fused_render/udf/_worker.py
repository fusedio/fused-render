"""One Fused environment's UDF worker (SPEC §54.4).

Started by runner.py as ``[sys.executable, -m, fused_render.udf._worker]``
with the environment's CLI env (FUSED_ENV set as the CLI's is). It imports
the fused SDK once and stays up, so a run pays neither the import nor the
SDK's per-process setup (the realtime instance lookup, a TLS handshake per
request): runs go through ``fused.run_async``, whose shared HTTP session
keeps connections open. Measured on prod for a result Fused had cached:
4.5 s for a one-shot process, 0.65 s here from the second run on.

Protocol: JSON lines. stdin carries requests, stdout answers; nothing else
may reach stdout, so whatever the SDK prints is sent to stderr.

  {"op": "run", "id", "source", "collection", "params",
   "format": "auto" | "view" | "html" | "png" | "json" | "parquet",
   "cache": bool, "confirmed": bool, "out": "<path>",
   "tile": {"x", "y", "z"} (optional: a tile run),
   "loaded": "<id of a code op>" (optional: run the UDF that op loaded)}
      -> {"id", "event": "started", "kind", "code_sha256"}
      -> {"id", "event": "done", "ok": true, "content_type", "size",
          "stdout", "stderr", "fused_cached", "seconds", "code_sha256"}
         (the body is written to `out`)
      or {"id", "event": "done", "ok": false, "error", "line", "stdout",
          "stderr"}
      or {"id", "event": "needs_confirmation", "kind"}   nothing ran
  {"op": "code", "id", "source", "collection"}
      -> {"id", "event": "code", "ok": true, "code_sha256"} | {..., "ok": false, "error"}
         (the loaded UDF is kept under `id` for one run to take: the run
         then is of exactly the code that was hashed)
  {"op": "schema", "id", "source", "collection"}
      -> {"id", "event": "schema", "ok": true, "schema"} | {..., "ok": false, "error"}
  {"op": "cancel", "id"}   the run's next poll stops it (`_cancel_callback`)

`source` is what `fused.load` takes: a .py file's absolute path or a UDF's
name; `collection` is the canvas the name (and any `fused.load` inside the
UDF) is looked up in.

THE GATE: a UDF the hosted workbench never runs on its own (a batch job,
batch work started inside, a map; `canvas_node_exec`) answers
`needs_confirmation` unless the request says `confirmed`. A confirmed batch
run is the SDK's sync call on a thread, waited for, so it never blocks the
other runs. EOF on stdin ends the worker.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sys
import threading

# The protocol's own stdout, taken before anything can print: the SDK prints
# progress ("Batch job started: …") and it must never interleave with an answer.
_PROTO = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr

import fused  # noqa: E402 — after stdout is taken

from fused_render import canvas_node_exec  # noqa: E402
from fused_render.udf.shaping import SDK_FORMATS, NeedsPng, body_of, text_of  # noqa: E402

_env_name = os.environ.get("FUSED_ENV")
if _env_name:
    fused._env(_env_name)

_SEND_LOCK = threading.Lock()
_cancelled: set[str] = set()
# UDFs loaded by a code op, waiting for their run (taken once). A load whose
# run never comes (it joined another page's run) is dropped, oldest first.
_loaded: dict[str, object] = {}
_LOADED_MAX = 256


def send(obj: dict) -> None:
    line = json.dumps(obj, default=str)
    with _SEND_LOCK:
        _PROTO.write(line + "\n")
        _PROTO.flush()


def answer(rid: str, result, fmt: str, out: str, code_sha: str) -> dict:
    """The `done` event for one `_return_response=True` result; the body is
    written to `out`."""
    stdout, stderr = text_of(getattr(result, "stdout", None)), text_of(getattr(result, "stderr", None))
    error = getattr(result, "error_message", None)
    if error or getattr(result, "has_exception", False):
        line = getattr(result, "error_lineno", None)
        return {"id": rid, "event": "done", "ok": False,
                "error": text_of(error) or "the UDF raised an error",
                "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                "stdout": stdout, "stderr": stderr}
    content, content_type = body_of(result, fmt)
    with open(out, "wb") as fh:
        fh.write(content)
    seconds = getattr(result, "time_taken_seconds", None)
    return {"id": rid, "event": "done", "ok": True, "content_type": content_type,
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "stdout": stdout, "stderr": stderr,
            "fused_cached": bool(getattr(result, "is_cached", False)),
            "seconds": float(seconds) if isinstance(seconds, (int, float)) else None,
            "code_sha256": code_sha}


def _load(source: str, collection: str | None):
    udf = fused.load(source, collection_name=collection) if collection else fused.load(source)
    if collection:
        udf.collection_name = collection
    return udf


async def run(req: dict) -> None:
    rid = str(req.get("id"))
    fmt = req.get("format") if req.get("format") in SDK_FORMATS else "auto"
    sdk_kw = {"_format": SDK_FORMATS[fmt]} if SDK_FORMATS[fmt] else {}
    try:
        udf = _loaded.pop(str(req.get("loaded")), None)
        if udf is None:
            udf = await asyncio.to_thread(_load, req["source"], req.get("collection"))
    except Exception as exc:  # noqa: BLE001 — a file that is no UDF is an answer
        send({"id": rid, "event": "done", "ok": False,
              "error": f"could not load this as a UDF: {exc}", "line": None,
              "stdout": "", "stderr": ""})
        return
    code = getattr(udf, "code", None) or ""
    code_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
    kind = canvas_node_exec.execution_type(code)
    if kind != canvas_node_exec.NONE and req.get("confirmed") is not True:
        send({"id": rid, "event": "needs_confirmation", "kind": kind})
        return
    send({"id": rid, "event": "started", "kind": kind, "code_sha256": code_sha})
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    cache = req.get("cache") is not False
    tile = req.get("tile")
    # A tile run: the SDK takes the coordinates as `x`, `y`, `z`.
    tile_kw = {k: tile[k] for k in ("x", "y", "z")} if isinstance(tile, dict) else {}
    try:
        if kind == canvas_node_exec.NONE:
            result = await fused.run_async(
                udf, engine="remote", _return_response=True,
                cache=cache, parameters=params,
                _cancel_callback=lambda: rid in _cancelled, **tile_kw, **sdk_kw)
        else:
            # A batch job is waited for by the sync call; on a thread, so the
            # worker's other runs go on.
            result = await asyncio.to_thread(
                lambda: fused.run(udf, engine="remote", _return_response=True,
                                  cache=cache, parameters=params, **tile_kw, **sdk_kw))
    except Exception as exc:  # noqa: BLE001 — the SDK raises some run errors
        if rid in _cancelled:
            send({"id": rid, "event": "done", "ok": False, "error": "cancelled",
                  "cancelled": True, "line": None, "stdout": "", "stderr": ""})
        else:
            send({"id": rid, "event": "done", "ok": False,
                  "error": str(exc) or type(exc).__name__, "line": None,
                  "stdout": "", "stderr": ""})
        return
    finally:
        _cancelled.discard(rid)
    try:
        try:
            done = answer(rid, result, fmt, req["out"], code_sha)
        except NeedsPng:
            # A raster's `view`: Fused's own PNG of it, a second request. Fused
            # may answer it from its cache; under cache=False ("fresh") it
            # evaluates the UDF again.
            png = await fused.run_async(udf, engine="remote", _return_response=True,
                                        cache=cache, parameters=params, _format="png",
                                        **tile_kw)
            done = answer(rid, png, "png", req["out"], code_sha)
        send(done)
    except Exception as exc:  # noqa: BLE001 — writing the body failed
        send({"id": rid, "event": "done", "ok": False,
              "error": f"could not keep the result: {exc}", "line": None,
              "stdout": "", "stderr": ""})


async def code(req: dict) -> None:
    rid = str(req.get("id"))
    try:
        udf = await asyncio.to_thread(_load, req["source"], req.get("collection"))
    except Exception as exc:  # noqa: BLE001 — a name that is no UDF is an answer
        send({"id": rid, "event": "code", "ok": False,
              "error": f"could not load this as a UDF: {exc}"})
        return
    while len(_loaded) >= _LOADED_MAX:
        _loaded.pop(next(iter(_loaded)))
    _loaded[rid] = udf
    text = getattr(udf, "code", None) or ""
    send({"id": rid, "event": "code", "ok": True,
          "code_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})


def _schema(source: str, collection: str | None) -> dict:
    from fused.models.udf.openapi import generate_openapi_schema

    udf = _load(source, collection)
    doc = generate_openapi_schema(
        [udf], base_url=fused.options.base_url, title=f"UDF API ({udf.name})",
        version="1.0.0", include_session_token_security=False,
        include_canvas_passcode_security=False, include_non_visible_udfs=True)
    return doc["paths"][f"/{udf.name}"]["get"]


async def schema(req: dict) -> None:
    rid = str(req.get("id"))
    try:
        out = await asyncio.to_thread(_schema, req["source"], req.get("collection"))
        send({"id": rid, "event": "schema", "ok": True, "schema": out})
    except Exception as exc:  # noqa: BLE001
        send({"id": rid, "event": "schema", "ok": False, "error": str(exc) or type(exc).__name__})


async def main() -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def read() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=read, daemon=True, name="udf-worker-stdin").start()
    tasks: set[asyncio.Task] = set()
    while True:
        line = await queue.get()
        if line is None:
            break
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if not isinstance(req, dict):
            continue
        op = req.get("op")
        if op == "cancel":
            _cancelled.add(str(req.get("id")))
            continue
        handler = {"run": run, "schema": schema, "code": code}.get(op)
        if handler is None:
            continue
        task = asyncio.create_task(handler(req))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    # stdin closed: the server is gone or stopping us. Runs already started
    # finish and answer (a reader that is still there gets them); the
    # runner's stop() kills the process if that takes longer than it waits.
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
