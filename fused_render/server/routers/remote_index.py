"""/api/remote-index/* — search over the public `fused-asset` bucket
(backend: fused_render/remote_index.py, which documents the store).

The UI routes (/status, /rank) are gated on the canvases feature flag
(`canvases_enabled`) and never error for "off" or "not built yet": a search
box treats those as one condition, so they answer 200 with `enabled: false` /
`ready: false`. /search (the agent tool, at the bottom) errors instead.

- GET /status → `{enabled, state: "absent"|"fetching"|"ready"|"error",
  rows, fetched_at, error}`.
- GET /rank?q=&limit=&ranked=&under= → `{ready, hits: [{url, key, is_dir,
  size, mtime}], total, truncated}` — files and folders; a folder's key/url
  end in "/" and its size/mtime are null. `under` (an `s3://` folder
  address) scopes the search to that folder's subtree — the S3 folder
  view, with the local folder search's query grammar and `mode`/`pattern`
  in the answer; absent, the whole bucket (Home, the launcher). The query runs on a worker thread and is interrupted
  (duckdb `interrupt`) when the client disconnects, through the same
  `cancellable` / `CancelToken` machinery `/api/index/rank` uses; an
  abandoned request answers 499 to nobody.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from fused_render import remote_access, remote_index
from fused_render.index.cancel import Cancelled, cancellable
from fused_render.remote_access import RemoteError, canonical_url
from fused_render.server.common import _require_fused
from fused_render.shell.prefs import canvases_enabled

logger = logging.getLogger(__name__)

router = APIRouter()

#: Default page size, mirroring the local home search's RANK_LIMIT.
RANK_LIMIT = 200

_NOT_READY = {"ready": False, "hits": [], "total": 0, "truncated": False}


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


def _rank(q: str, limit: int, ranked: bool, token, prefix: str | None = None) -> dict:
    """`remote_index.rank`, after the (throttled) retry/refresh nudge: these
    routes are how a session that only searches keeps its index current."""
    remote_index.retry_if_needed()
    return remote_index.rank(q, limit, ranked, token, prefix=prefix)


@router.get("/api/remote-index/status")
def api_remote_index_status():
    if not canvases_enabled():
        return {"enabled": False, "state": "absent", "rows": 0,
                "fetched_at": None, "error": None}
    try:
        remote_index.retry_if_needed()
        return {"enabled": True, **remote_index.status()}
    except Exception:  # noqa: BLE001 - details (local paths) stay in the log
        logger.exception("remote index status failed")
        return _error("could not read the Fused assets index state",
                      "internal", 500)


@router.get("/api/remote-index/rank")
async def api_remote_index_rank(request: Request, q: str = Query(default=""),
                                limit: str = Query(default=""),
                                ranked: str = Query(default="true"),
                                under: str = Query(default="")):
    if not canvases_enabled():
        return dict(_NOT_READY)
    # Parsed here rather than typed on the signature, so a malformed value
    # answers the contract's `{error, code}` body instead of FastAPI's 422.
    try:
        limit_n = int(limit) if limit.strip() else RANK_LIMIT
    except ValueError:
        return _error("'limit' must be an integer", "bad_url", 400)
    want_ranked = ranked.strip().lower() not in ("0", "false", "no", "off")
    prefix = None
    if under.strip():
        try:
            bucket, key = remote_access.parse_url(under)
            remote_access.require_supported(bucket)
        except RemoteError as e:
            return _error(e.message, e.code, 400)
        prefix = key if (not key or key.endswith("/")) else key + "/"
    async with cancellable(request) as token:
        try:
            return await asyncio.to_thread(_rank, q, limit_n, want_ranked, token, prefix)
        except Cancelled:
            logger.debug("remote index rank %r abandoned by the client", q)
            return Response(status_code=499)
        except Exception:  # noqa: BLE001 - details (local paths) stay in the log
            logger.exception("remote index rank failed for %r", q)
            return _error("Fused assets search failed", "internal", 500)


# ---- /search: the agent tool `s3_search` --------------------------------------
#
# GET /search?q=&limit=            ranked search (`rank`), files and folders
# GET /search?path=&limit=&cursor= one level of a folder (`list_dir`), paged
#
# Exactly one of q / path (a blank value counts as absent); `cursor` only with
# path. Unlike /rank this one is for a tool, so every condition is an error:
# no `X-Fused: 1` 403 `forbidden`, flag off 403 `disabled`, store not ready 503 `index_not_ready` (after
# `retry_if_needed()`), bad input 400 `bad_url`, another bucket 400
# `unsupported_bucket`, a missing folder 404 `not_found`, a FILE given as
# `path` 400 `bad_url`. Answer:
# `{bucket, mode: "search"|"list", hits: [{url, is_dir, size, modified}],
#   truncated, cursor}` — `modified` ISO-8601 UTC or null, folders size null.
# In list mode `truncated` is "more pages follow" (cursor is not null).

#: Hits per call when `limit` is absent, and the most one call returns.
SEARCH_LIMIT = 20
SEARCH_LIMIT_MAX = 200


def _not_ready() -> JSONResponse:
    remote_index.retry_if_needed()
    message = "The S3 file index is still loading. Try again in a moment."
    try:
        st = remote_index.status()
        if st.get("state") == "error" and st.get("error"):
            message = (f"The S3 file index could not be loaded: {st['error']}. "
                       "It will be retried.")
    except Exception:  # noqa: BLE001 - the explanation is best-effort
        logger.debug("remote index status unavailable", exc_info=True)
    return _error(message, "index_not_ready", 503)


def _hit(url: str, is_dir: bool, size, mtime) -> dict:
    return {"url": url, "is_dir": is_dir, "size": None if is_dir else size,
            "modified": None if is_dir else remote_access.iso_utc(mtime)}


def _search_list(bucket: str, prefix: str, cursor: str | None, limit: int,
                 file_key: str | None) -> dict | JSONResponse:
    remote_index.retry_if_needed()
    try:
        page = remote_index.list_dir(prefix, cursor, limit)
    except ValueError:
        return _error("the listing cursor is not valid", "bad_url", 400)
    if page is None:
        return _not_ready()
    entries = page.get("entries") or []
    nxt = page.get("cursor") or None
    if not entries and not nxt and prefix and remote_index.has_dir(prefix) is False:
        if file_key and remote_index.has_file(file_key):
            return _error("that is a file, not a folder: read it with s3_read, "
                          "or list its parent folder", "bad_url", 400)
        return _error(f"{canonical_url(bucket, prefix)} does not exist", "not_found", 404)
    return {"bucket": bucket, "mode": "list",
            "hits": [_hit(canonical_url(bucket, e["key"]), bool(e["is_dir"]),
                          e.get("size"), e.get("mtime")) for e in entries],
            "truncated": nxt is not None, "cursor": nxt}


@router.get("/api/remote-index/search")
async def api_remote_index_search(request: Request, q: str = Query(default=""),
                                  path: str = Query(default=""),
                                  limit: str = Query(default=""),
                                  cursor: str = Query(default=""),
                                  x_fused: str | None = Header(default=None)):
    # Only fused-render's own tool server calls this (it sends X-Fused).
    if _require_fused(x_fused) is not None:
        return _error("missing or invalid X-Fused header", "forbidden", 403)
    if not canvases_enabled():
        return _error("the Fused integration is off — turn on Canvases in Preferences",
                      "disabled", 403)
    has_q, has_path = bool(q.strip()), bool(path.strip())
    if has_q == has_path:
        return _error("give exactly one of 'q' (search) or 'path' (list a folder)",
                      "bad_url", 400)
    if cursor and not has_path:
        return _error("'cursor' continues a folder listing; it needs 'path'", "bad_url", 400)
    try:
        limit_n = int(limit) if limit.strip() else SEARCH_LIMIT
    except ValueError:
        return _error("'limit' must be an integer", "bad_url", 400)
    if limit_n < 1:
        return _error("'limit' must be at least 1", "bad_url", 400)
    limit_n = min(limit_n, SEARCH_LIMIT_MAX)

    if has_path:
        try:
            bucket, key = remote_access.parse_url(path)
            remote_access.require_supported(bucket)
        except RemoteError as e:
            return _error(e.message, e.code, 400)
        if len(cursor) > 4096:
            return _error("the listing cursor is not valid", "bad_url", 400)
        prefix = key if (not key or key.endswith("/")) else key + "/"
        try:
            return await asyncio.to_thread(_search_list, bucket, prefix, cursor or None,
                                           limit_n, prefix[:-1] if prefix != key else None)
        except Exception:  # noqa: BLE001 - details (local paths) stay in the log
            logger.exception("remote index listing failed for %r", prefix)
            return _error("listing the S3 folder failed", "internal", 500)

    async with cancellable(request) as token:
        try:
            out = await asyncio.to_thread(_rank, q, limit_n, True, token)
        except Cancelled:
            logger.debug("remote index search %r abandoned by the client", q)
            return Response(status_code=499)
        except Exception:  # noqa: BLE001 - details (local paths) stay in the log
            logger.exception("remote index search failed for %r", q)
            return _error("searching the S3 file index failed", "internal", 500)
    if not out.get("ready"):
        return await asyncio.to_thread(_not_ready)
    return {"bucket": remote_index.BUCKET, "mode": "search",
            "hits": [_hit(h["url"], bool(h["is_dir"]), h.get("size"), h.get("mtime"))
                     for h in out["hits"]],
            "truncated": bool(out.get("truncated")), "cursor": None}
