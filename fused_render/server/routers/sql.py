"""The routes of the local SQL engine for pages (SPEC §55.2). The work is
`fused_render.sql.runner` (the engine and its sessions) and
`fused_render.sql.files` (the named files its SQL can reach).

Every route, reads included, requires the `X-Fused` header
(`_require_fused`), and none is on the LAN allowlist (`lan.py`), so a LAN
peer never reaches them. Errors are `{code, message}` with `code` one of
`unsupported` (501: duckdb/pyarrow missing), `invalid_args` (400),
`not_found` (404), `cancelled` (400), `too_large` (413: an upload past
FUSED_RENDER_SQL_MAX_UPLOAD_BYTES, a result past
FUSED_RENDER_SQL_MAX_RESULT_BYTES), `engine_error` (400; 503 while the app
is quitting, the engine cannot start or too many engines are live).

Request bodies (a file's bytes, an Arrow stream) are streamed to a file in
the engine's `io/`, never held in memory whole, and refused once past the
upload ceiling; a query's answer is the spooled Arrow IPC stream the worker
wrote, removed once sent. Nothing here logs SQL text or file names.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from fused_render.server.common import _require_fused
from fused_render.sql import files, runner

logger = logging.getLogger(__name__)

router = APIRouter()

ARROW_STREAM = "application/vnd.apache.arrow.stream"
_DISCONNECT_POLL_S = 0.5
_FILE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox allow-scripts allow-popups "
                               "allow-popups-to-escape-sandbox allow-downloads",
}


def _err(code: str, message: str, status: int | None = None) -> JSONResponse:
    if status is None:
        status = {"unsupported": 501, "not_found": 404, "too_large": 413}.get(code, 400)
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _not_found() -> JSONResponse:
    return _err("not_found", "no such session")


def _from_exc(exc: Exception) -> JSONResponse:
    if isinstance(exc, runner.NotInstalled):
        return _err("unsupported", str(exc))
    if isinstance(exc, runner.Unavailable):
        return _err("engine_error", str(exc), 503)
    if isinstance(exc, (runner.NoSession, files.NotFound)):
        return _err("not_found", str(exc) or "not found")
    if isinstance(exc, files.BadName):
        return _err("invalid_args", str(exc))
    if isinstance(exc, files.Unsupported):
        return _err("unsupported", str(exc))
    if isinstance(exc, runner.QueryFailed):
        return _err(exc.code, exc.message)
    if isinstance(exc, runner.WorkerGone):
        return _err("engine_error", str(exc))
    raise exc


async def _json_body(request: Request) -> dict | None:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class _TooLarge(Exception):
    pass


async def _spool_body(request: Request, path: str) -> None:
    """Stream the request body to `path` (a new file in `io/`). _TooLarge as
    soon as it passes the upload ceiling (the caller removes the partial
    file)."""
    limit = runner.max_upload_bytes()
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _TooLarge
    total = 0
    with open(path, "xb") as fh:
        async for chunk in request.stream():
            if chunk:
                total += len(chunk)
                if total > limit:
                    raise _TooLarge
                fh.write(chunk)


def _too_large_upload() -> JSONResponse:
    return _err("too_large", f"the body is larger than {runner.max_upload_bytes()} bytes")


async def run_until_disconnected(request: Request, work, on_disconnect) -> tuple[bool, object]:
    """Await `work` (a coroutine); if the caller disconnects first, call
    `on_disconnect()` and still wait for `work` to end. Returns
    (disconnected, result-or-exception)."""
    task = asyncio.ensure_future(work)
    disconnected = False
    while True:
        done, _ = await asyncio.wait({task}, timeout=_DISCONNECT_POLL_S)
        if done:
            break
        if not disconnected and await request.is_disconnected():
            disconnected = True
            on_disconnect()
    try:
        return disconnected, task.result()
    except Exception as exc:  # noqa: BLE001 — handed back to the caller
        return disconnected, exc


@router.post("/api/sql/session")
async def api_sql_session_create(request: Request, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = await _json_body(request)
    if body is None:
        return _err("invalid_args", "the body must be a JSON object")
    scope = body.get("scope")
    if scope is not None and (not isinstance(scope, str) or len(scope) > 256):
        return _err("invalid_args", "scope must be a string of at most 256 characters")
    try:
        sid = runner.create_session(scope or "")
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)
    return {"session": sid}


@router.post("/api/sql/session/{session}/query")
async def api_sql_query(session: str, request: Request,
                        x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = await _json_body(request)
    if body is None:
        return _err("invalid_args", "the body must be a JSON object")
    sql = body.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return _err("invalid_args", "sql must be a non-empty string")
    qid = body.get("query_id")
    if qid is not None and (not isinstance(qid, str) or not runner.QUERY_ID_RE.match(qid)):
        return _err("invalid_args", "query_id is 1-128 of A-Z a-z 0-9 . _ -")
    qid = qid or os.urandom(8).hex()
    disconnected, result = await run_until_disconnected(
        request, runner.query(session, sql, qid), lambda: runner.cancel(session, qid))
    if isinstance(result, Exception):
        return _from_exc(result)
    if disconnected:
        _remove(result)
        return _err("cancelled", "the caller went away")
    return FileResponse(result, media_type=ARROW_STREAM,
                        background=BackgroundTask(_remove, result))


@router.post("/api/sql/session/{session}/keepalive")
async def api_sql_keepalive(session: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        runner.keepalive(session)
    except runner.NoSession:
        return _not_found()
    return {}


@router.post("/api/sql/session/{session}/cancel")
async def api_sql_cancel(session: str, request: Request,
                         x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = await _json_body(request)
    if body is None:
        return _err("invalid_args", "the body must be a JSON object")
    qid = body.get("query_id")
    if not isinstance(qid, str) or not runner.QUERY_ID_RE.match(qid):
        return _err("invalid_args", "query_id is 1-128 of A-Z a-z 0-9 . _ -")
    try:
        runner.session_files_dir(session)
    except runner.NoSession:
        return _not_found()
    runner.cancel(session, qid)
    return {}


@router.put("/api/sql/session/{session}/file/{name:path}")
async def api_sql_file_put(session: str, name: str, request: Request,
                           x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        files.check_name(name)
        files_dir = runner.session_files_dir(session)
        tmp = runner.io_path(session)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)
    try:
        await _spool_body(request, tmp)
        files.register_bytes(files_dir, name, tmp)
    except _TooLarge:
        return _too_large_upload()
    except OSError as exc:
        return _err("engine_error", f"could not store the file: {exc}")
    finally:
        _remove(tmp)
    return {}


@router.post("/api/sql/session/{session}/file/{name}/link")
async def api_sql_file_link(session: str, name: str, request: Request,
                            x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = await _json_body(request)
    if body is None:
        return _err("invalid_args", "the body must be a JSON object")
    handle = body.get("handle")
    try:
        files.check_name(name)
        files_dir = runner.session_files_dir(session)
        io_dir = runner.session_io_dir(session)
        files.register_link(files_dir, io_dir, name, handle)
    except OSError as exc:
        return _err("engine_error", f"could not link the file: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)
    return {}


@router.get("/api/sql/session/{session}/file/{name:path}")
async def api_sql_file_get(session: str, name: str,
                           x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        files.check_name(name)
        path = files.read_path(runner.session_files_dir(session), name)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)
    return FileResponse(path, media_type="application/octet-stream", headers=_FILE_HEADERS)


@router.delete("/api/sql/session/{session}/file/{name:path}")
async def api_sql_file_delete(session: str, name: str,
                              x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        files.check_name(name)
        files.drop(runner.session_files_dir(session), name)
    except OSError as exc:
        return _err("engine_error", f"could not remove the file: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)
    return {}


@router.post("/api/sql/session/{session}/arrow/{table}")
async def api_sql_arrow(session: str, table: str, request: Request, create: str = "0",
                        x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not runner.TABLE_RE.match(table):
        return _err("invalid_args", "a table name is [A-Za-z_][A-Za-z0-9_]{0,127}")
    if create not in ("0", "1"):
        return _err("invalid_args", "create is 0 or 1")
    try:
        tmp = runner.io_path(session)
    except Exception as exc:  # noqa: BLE001
        return _from_exc(exc)
    try:
        try:
            await _spool_body(request, tmp)
        except _TooLarge:
            return _too_large_upload()
        except OSError as exc:
            return _err("engine_error", f"could not store the body: {exc}")
        try:
            await runner.insert(session, table, create == "1", tmp)
        except Exception as exc:  # noqa: BLE001
            return _from_exc(exc)
    finally:
        _remove(tmp)
    return {}


@router.delete("/api/sql/session/{session}")
async def api_sql_session_delete(session: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        runner.delete_session(session)
    except runner.NoSession:
        return _not_found()
    return {}
