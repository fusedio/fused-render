"""`/api/remote/*` — an `s3://` address as an explorer location.

  GET  /api/remote/stat?url=           file or folder, and whether it is cached
  GET  /api/remote/list?url=&cursor=   one level of a folder, one page
  POST /api/remote/fetch               download an object into the local cache
  GET  /api/remote/fetch/status?id=    where that download is up to
  POST /api/remote/fetch/cancel        stop it

All of it is gated on the Canvases flag (off -> 403 `disabled`). Every error is
`{"error": <plain message>, "code": <code>}`. Scope is the one public bucket
`remote_access.BUCKET`: any other bucket is 400 `unsupported_bucket`, checked
right after parsing, before any network call. Folders come from its index only
(`remote_index`; 503 `index_not_ready` while it loads); files are a live HEAD
(size/etag must be current) plus a download, and need no index. Blocking work
runs in the threadpool, never on the event loop. The two POSTs carry the same
`X-Fused` write guard as every other mutating route.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from fused_render import remote_access, remote_fetch
from fused_render.remote_access import RemoteError, canonical_url
from fused_render.server.common import _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()

_STATUS = {"disabled": 403, "forbidden": 403, "bad_url": 400, "unsupported_bucket": 400,
           "not_found": 404, "not_public": 403, "network": 502, "index_not_ready": 503,
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


def _index_has_dir(prefix: str) -> bool | None:
    """True/False from the index, or None (not ready)."""
    from fused_render import remote_index

    try:
        answer = remote_index.has_dir(prefix)
    except ImportError:
        return None
    return None if answer is None else bool(answer)


def _index_answer(name: str, arg: str) -> bool | None:
    """`remote_index.<name>(arg)` for the file-or-folder question; ANY failure
    reads as None (unknown) — the live HEAD decides then."""
    try:
        from fused_render import remote_index

        answer = getattr(remote_index, name)(arg)
    except Exception:  # noqa: BLE001
        logger.debug("remote index %s failed", name, exc_info=True)
        return None
    return None if answer is None else bool(answer)


def _index_list(prefix: str, cursor: str | None) -> dict | None:
    """The index's page for `prefix`, or None (not ready). A cursor the index
    rejects (ValueError) is `bad_url`."""
    from fused_render import remote_index

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
            "warn_bytes": remote_fetch.WARN_BYTES}


def _missing(bucket: str, key: str) -> RemoteError:
    return RemoteError("not_found", f"{canonical_url(bucket, key)} does not exist")


def _stat(url) -> dict:
    bucket, key = _parse(url)
    if not key or key.endswith("/"):
        known = _index_has_dir(key)
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
        if d is True and e.code == "network":
            return _dir_payload(bucket, key + "/")
        raise
    # An object that is also a prefix resolves to the file here; the folder is
    # reached by statting with the trailing slash.
    path = remote_fetch.cached_path(bucket, key, info)
    return {"url": canonical_url(bucket, key), "bucket": bucket, "key": key,
            "name": _name(bucket, key), "kind": "file", "size": info.size,
            "mtime": info.mtime, "etag": info.etag, "cached": path is not None,
            "path": path, "warn_bytes": remote_fetch.WARN_BYTES}


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
    if not raw and not next_cursor and prefix and _index_has_dir(prefix) is False:
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
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _fetch(url) -> dict:
    bucket, key = _parse(url)
    return remote_fetch.start(bucket, key)


@router.post("/api/remote/fetch")
async def api_remote_fetch(request: Request, x_fused: str | None = Header(default=None)):
    refused = _guard(x_fused) or _gate()
    if refused is not None:
        return refused
    body = await _body(request)
    if body is None:
        return _fail("bad_url", 'the request body must be {"url": "s3://…"}')
    return await run_in_threadpool(_handle, _fetch, body.get("url"))


@router.get("/api/remote/fetch/status")
def api_remote_fetch_status(id: str | None = None):  # noqa: A002 - contract name
    gate = _gate()
    if gate is not None:
        return gate
    record = remote_fetch.status(id or "")
    if record is None:
        return _fail("not_found", f"no such download: {id}")
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
