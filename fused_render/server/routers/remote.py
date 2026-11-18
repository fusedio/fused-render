"""`/api/remote/*` — an `s3://` address as an explorer location.

  GET  /api/remote/stat?url=           file or folder, whether it is cached,
                                       and the templates that stream it
  GET  /api/remote/list?url=&cursor=   one level of a folder, one page
  GET  /api/remote/read?url=&offset=&length=
                                       a byte range of an object, streamed
                                       (the agent tool `s3_read`; X-Fused)
  POST /api/remote/fetch               download an object into the local cache
                                       (`{url, max_bytes?}`: over the cap,
                                       413 `too_large` instead of a download)
  POST /api/remote/sign                a URL to read an object from directly
                                       (`{url}` -> `{url, expires_at}`;
                                       `fused.s3.sign`, any page; X-Fused)
  GET  /api/remote/fetch/status?id=    where that download is up to
  POST /api/remote/fetch/cancel        stop it

All of it is gated on the Canvases flag (off -> 403 `disabled`). Every error is
`{"error": <plain message>, "code": <code>}`. Scope is the one public bucket
`remote_access.BUCKET`: any other bucket is 400 `unsupported_bucket`, checked
right after parsing, before any network call. Folders come from its index only
(`remote_index`; 503 `index_not_ready` while it loads); files are a live HEAD
(size/etag must be current) plus a download, and need no index. Blocking work
runs in the threadpool, never on the event loop. The POSTs carry the same
`X-Fused` write guard as every other mutating route.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from fused_render import remote_access, remote_fetch
from fused_render.remote_access import RemoteError, canonical_url
from fused_render.server.common import _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()

_STATUS = {"disabled": 403, "forbidden": 403, "bad_url": 400, "unsupported_bucket": 400,
           "not_found": 404, "not_public": 403, "network": 502, "index_not_ready": 503,
           "range_not_satisfiable": 416, "no_space": 507, "unknown_download": 404,
           "too_large": 413,
           "internal": 500}


def _fail(code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": message, "code": code},
                        status_code=_STATUS.get(code, 500))


def _gate() -> JSONResponse | None:
    from fused_render.shell import prefs

    if not prefs.canvases_enabled():
        return _fail("disabled", "the Fused integration is off — turn on Canvases in Preferences")
    return None


def _guard(x_fused: str | None) -> JSONResponse | None:
    """`_require_fused`, reshaped to carry a `code` like every other error here."""
    if _require_fused(x_fused) is None:
        return None
    return _fail("forbidden", "missing or invalid X-Fused header")


def _parse(url) -> tuple[str, str]:
    """`parse_url`, then the bucket scope check — before anything touches S3."""
    bucket, key = remote_access.parse_url(url)
    return remote_access.require_supported(bucket), key


def _name(bucket: str, key: str) -> str:
    stripped = key.rstrip("/")
    return stripped.rsplit("/", 1)[-1] if stripped else bucket


def _handle(fn, *args) -> dict | JSONResponse:
    """Run a route body; RemoteError -> its code, anything else -> `internal`
    with the detail logged, never sent (no local paths in a response)."""
    try:
        return fn(*args)
    except RemoteError as e:
        return _fail(e.code, e.message)
    except Exception:  # noqa: BLE001
        logger.exception("remote route failed")
        return _fail("internal", "something went wrong reading the remote location")


# ---- folders: the bucket index (remote_index) --------------------------------
#
# Imported lazily: the index needs an optional extra (pyarrow/duckdb). A
# missing extra (ImportError) reads as "not ready"; any other failure is a real
# error and surfaces as `internal`, never disguised as "still loading".


def _not_ready() -> RemoteError:
    """503 `index_not_ready`, after nudging a retry, saying why when known."""
    message = f"The {remote_access.BUCKET} index is still loading. Try again in a moment."
    try:
        from fused_render import remote_index

        remote_index.retry_if_needed()
        st = remote_index.status()
        if st.get("state") == "error" and st.get("error"):
            message = (f"The {remote_access.BUCKET} index could not be loaded: "
                       f"{st['error']}. It will be retried.")
    except Exception:  # noqa: BLE001 - the explanation is best-effort
        logger.debug("remote index status unavailable", exc_info=True)
    return RemoteError("index_not_ready", message)


def _index_answer(name: str, arg: str) -> bool | None:
    """`remote_index.<name>(arg)`: True/False, or None (not ready). Only a
    missing extra (ImportError) reads as None; a broken store is a real error."""
    from fused_render import remote_index

    try:
        answer = getattr(remote_index, name)(arg)
    except ImportError:
        return None
    return None if answer is None else bool(answer)


def _index_list(prefix: str, cursor: str | None) -> dict | None:
    """The index's page for `prefix`, or None (not ready). A cursor the index
    rejects (ValueError) is `bad_url`."""
    from fused_render import remote_index

    remote_index.retry_if_needed()  # throttled; retries a failed ingest, refreshes a stale one
    try:
        page = remote_index.list_dir(prefix, cursor)
    except ImportError:
        return None
    except ValueError:
        raise RemoteError("bad_url", "the listing cursor is not valid") from None
    return page if isinstance(page, dict) else None


def _dir_payload(bucket: str, prefix: str) -> dict:
    return {"url": canonical_url(bucket, prefix), "bucket": bucket, "key": prefix,
            "name": _name(bucket, prefix), "kind": "dir", "size": None, "mtime": None,
            "etag": None, "cached": False, "path": None,
            "warn_bytes": remote_fetch.WARN_BYTES, "templates": []}


def _templates(name: str) -> list:
    """The modes the object opens in before it is downloaded, each marked
    `stream` when its template reads the `s3://` address itself (D1492): the
    explorer frames those on the address and downloads for the rest.
    Best-effort: a registry problem means the download card, never a failed
    stat."""
    from fused_render.server import templates as server_templates

    try:
        return server_templates.remote_templates(name)
    except Exception:  # noqa: BLE001
        logger.warning("template lookup failed for %r", name, exc_info=True)
        return []


def _missing(bucket: str, key: str) -> RemoteError:
    return RemoteError("not_found", f"{canonical_url(bucket, key)} does not exist")


def _stat(url) -> dict:
    bucket, key = _parse(url)
    if not key or key.endswith("/"):
        known = _index_answer("has_dir", key)
        if known is None:
            raise _not_ready()
        if not known:
            raise _missing(bucket, key)
        return _dir_payload(bucket, key)
    # Index first, so folder navigation needs no network and works offline.
    d = _index_answer("has_dir", key + "/")
    f = _index_answer("has_file", key)
    if d is True and f is False:
        return _dir_payload(bucket, key + "/")
    try:
        info = remote_access.head(bucket, key)
    except RemoteError as e:
        if d is None and e.code == "not_found":
            raise _not_ready() from None  # may be a folder the index cannot show yet
        if d is True and e.code in ("not_found", "network"):
            return _dir_payload(bucket, key + "/")
        raise
    # An object that is also a prefix resolves to the file here; the folder is
    # reached by statting with the trailing slash.
    path = remote_fetch.cached_path(bucket, key, info)
    return {"url": canonical_url(bucket, key), "bucket": bucket, "key": key,
            "name": _name(bucket, key), "kind": "file", "size": info.size,
            "mtime": info.mtime, "etag": info.etag, "cached": path is not None,
            "path": path, "warn_bytes": remote_fetch.WARN_BYTES,
            "templates": _templates(_name(bucket, key))}


def _list(url, cursor) -> dict:
    bucket, key = _parse(url)
    prefix = key if (not key or key.endswith("/")) else key + "/"
    if cursor is not None and len(cursor) > 4096:
        raise RemoteError("bad_url", "the listing cursor is not valid")
    page = _index_list(prefix, cursor or None)
    if page is None:
        raise _not_ready()
    raw = page.get("entries") or []
    next_cursor = page.get("cursor") or None
    if not raw and not next_cursor and prefix and _index_answer("has_dir", prefix) is False:
        raise _missing(bucket, prefix)
    # The index already orders dirs first, then files, by name.
    entries = [{"url": canonical_url(bucket, e["key"]), "name": e["name"],
                "is_dir": bool(e["is_dir"]),
                "size": None if e["is_dir"] else e.get("size"),
                "mtime": None if e["is_dir"] else e.get("mtime")} for e in raw]
    return {"url": canonical_url(bucket, prefix), "entries": entries, "cursor": next_cursor}


@router.get("/api/remote/stat")
def api_remote_stat(url: str | None = None):
    return _gate() or _handle(_stat, url)


@router.get("/api/remote/list")
def api_remote_list(url: str | None = None, cursor: str | None = None):
    return _gate() or _handle(_list, url, cursor)


# ---- files: download into the local cache ------------------------------------


async def _body(request: Request) -> dict | None:
    """The JSON object body, or None. Parsed by hand so a malformed or
    non-object body is the contract's 400 `bad_url`, not FastAPI's 422."""
    try:
        body = json.loads(await request.body() or b"null")
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def _fetch(url, max_bytes) -> dict:
    bucket, key = _parse(url)
    if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                                  or max_bytes < 1):
        raise RemoteError("bad_url", "'max_bytes' must be a positive integer")
    return remote_fetch.start(bucket, key, max_bytes=max_bytes)


@router.post("/api/remote/fetch")
async def api_remote_fetch(request: Request, x_fused: str | None = Header(default=None)):
    refused = _guard(x_fused) or _gate()
    if refused is not None:
        return refused
    body = await _body(request)
    if body is None:
        return _fail("bad_url", 'the request body must be {"url": "s3://…"}')
    return await run_in_threadpool(_handle, _fetch, body.get("url"), body.get("max_bytes"))


# ---- files: a byte range, streamed -------------------------------------------
#
# `offset`/`length` are an agent's words; `remote_access` takes a Python slice
# (`obj[start:end]`, negative = from the end). A window longer than
# `RANGE_MAX_BYTES` (including `length` omitted on a big object) is CLAMPED to
# its first `RANGE_MAX_BYTES` bytes, never refused; `X-Remote-Range` always
# names the bytes actually sent. An object already in the local cache is read
# from that file with no network at all (`remote_fetch.cached_copy`; never on
# Windows, where proving the copy costs a full re-hash, so it reads S3); the
# window rules are the same `remote_access.resolve_range` either way.


def _int_param(name: str, raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        raise RemoteError("bad_url", f"'{name}' must be an integer") from None


def _window(offset: int | None, length: int | None) -> tuple[int, int]:
    """`(start, max_bytes)` for offset/length: the window starts at `offset`
    (negative = from the end, clamped to byte 0) and runs `length` bytes
    from THERE (to the end when omitted), never more than the cap."""
    if length is not None and length < 1:
        raise RemoteError("bad_url", "'length' must be at least 1")
    cap = remote_fetch.RANGE_MAX_BYTES
    return offset or 0, cap if length is None else min(length, cap)


def _file_range(path: str, info: remote_access.ObjectInfo, start: int, max_bytes: int,
                what: str) -> remote_access.RangeBody:
    first, stop = remote_access.resolve_range(start, None, info.size or 0, max_bytes)
    f = open(path, "rb")  # noqa: SIM115 - RangeBody closes it
    try:
        f.seek(first)
    except BaseException:
        f.close()
        raise
    chunks = iter(lambda: f.read(remote_access.RANGE_CHUNK), b"")
    return remote_access.RangeBody(chunks, f.close, info, first, stop, what=what)


def _open_read(url, offset, length) -> remote_access.RangeBody:
    bucket, key = _parse(url)
    start, max_bytes = _window(_int_param("offset", offset), _int_param("length", length))
    if not key or key.endswith("/"):
        raise RemoteError("bad_url", "a folder has no bytes to read — list it instead")
    what = canonical_url(bucket, key)
    # The tool server saves a range to the cache volume: below the floor, no
    # network either. Re-checked with the real window size once it is known.
    remote_fetch.require_space(0)
    body = None
    cached = remote_fetch.cached_copy(bucket, key)
    if cached is not None:
        try:
            body = _file_range(cached[0], cached[1], start, max_bytes, what)
        except OSError:
            logger.warning("cached copy of %s unreadable; reading from S3", what,
                           exc_info=True)
    if body is None:
        body, _ = remote_access.read_range(bucket, key, start, max_bytes=max_bytes)
    try:
        remote_fetch.require_space(body.end - body.start)
    except BaseException:
        body.close()
        raise
    return body


def _read_headers(body: remote_access.RangeBody) -> dict:
    info = body.info
    headers = {"Content-Length": str(body.end - body.start),
               "X-Remote-Range": f"{body.start}-{body.end}",
               "Content-Security-Policy": "sandbox",
               "X-Content-Type-Options": "nosniff",
               "Cache-Control": "no-store"}
    if info.size is not None:
        headers["X-Remote-Size"] = str(info.size)
    if info.etag and info.etag.isascii() and info.etag.isprintable():
        headers["X-Remote-Etag"] = info.etag
    modified = remote_access.iso_utc(info.mtime)
    if modified:
        headers["X-Remote-Modified"] = modified
    return headers


@router.get("/api/remote/read")
async def api_remote_read(url: str | None = None, offset: str | None = None,
                          length: str | None = None,
                          x_fused: str | None = Header(default=None)):
    # Only fused-render's own tool server calls this (it sends X-Fused): a web
    # page cannot drive outbound bucket reads, nor (later) reach private-bucket
    # bytes by DNS rebinding.
    refused = _guard(x_fused) or _gate()
    if refused is not None:
        return refused
    opened = await run_in_threadpool(_handle, _open_read, url, offset, length)
    if isinstance(opened, JSONResponse):
        return opened
    body = opened

    async def stream():
        try:
            async for chunk in iterate_in_threadpool(iter(body)):
                yield chunk
        finally:
            body.close()

    return StreamingResponse(stream(), media_type="application/octet-stream",
                             headers=_read_headers(body))


# ---- files: a URL the page reads itself ---------------------------------------
#
# `fused.s3.sign` (runtime.js) and `fused_s3.sign` (templates/shared) — the
# streaming path: a template asks for a read URL for its `s3://` address and
# hands it to its own reader (DuckDB, GDAL, a range-reading TIFF decoder)
# instead of waiting for a download. Open to every rendered page, like
# runPython. The page names the object; `remote_access.sign_read_url` decides
# whether it may be read, so a page can never point this at another host.
# Pure: no network call, nothing cached.


def _sign(url) -> dict:
    bucket, key = _parse(url)
    signed = remote_access.sign_read_url(bucket, key)
    return {"url": signed.url, "expires_at": remote_access.iso_utc(signed.expires_at)}


@router.post("/api/remote/sign")
async def api_remote_sign(request: Request, x_fused: str | None = Header(default=None)):
    refused = _guard(x_fused) or _gate()
    if refused is not None:
        return refused
    body = await _body(request)
    if body is None:
        return _fail("bad_url", 'the request body must be {"url": "s3://…"}')
    url = body.get("url")
    call = getattr(request.state, "fused_call", None)
    if call is not None and isinstance(url, str):
        # The call log names the object signed (never the signed URL).
        call["entrypoint"] = url[:2048]
        call["entrypoint_name"] = url.rstrip("/").rsplit("/", 1)[-1][:256] or None
    return _handle(_sign, url)


@router.get("/api/remote/fetch/status")
def api_remote_fetch_status(id: str | None = None):  # noqa: A002 - contract name
    gate = _gate()
    if gate is not None:
        return gate
    record = remote_fetch.status(id or "")
    if record is None:
        return _fail("unknown_download",
                     "that download is no longer tracked (the app restarted); start it again")
    return record


@router.post("/api/remote/fetch/cancel")
async def api_remote_fetch_cancel(request: Request, x_fused: str | None = Header(default=None)):
    refused = _guard(x_fused) or _gate()
    if refused is not None:
        return refused
    body = await _body(request)
    fid = body.get("id") if body is not None else None
    if not isinstance(fid, str) or not remote_fetch.cancel(fid):
        return _fail("not_found", f"no such download: {fid}")
    return {"ok": True}
