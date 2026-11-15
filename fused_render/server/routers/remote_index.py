"""/api/remote-index/* — search over the public `fused-asset` bucket
(backend: fused_render/remote_index.py, which documents the store).

Both routes are gated on the canvases feature flag (`canvases_enabled`) and
never error for "off" or "not built yet": a search box treats those as one
condition, so they answer 200 with `enabled: false` / `ready: false`.

- GET /status → `{enabled, state: "absent"|"fetching"|"ready"|"error",
  rows, fetched_at, error}`.
- GET /rank?q=&limit=&ranked= → `{ready, hits: [{url, key, is_dir, size,
  mtime}], total, truncated}` — files and folders; a folder's key/url end
  in "/" and its size/mtime are null. The query runs on a worker thread and is interrupted
  (duckdb `interrupt`) when the client disconnects, through the same
  `cancellable` / `CancelToken` machinery `/api/index/rank` uses; an
  abandoned request answers 499 to nobody.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from fused_render import remote_index
from fused_render.index.cancel import Cancelled, cancellable
from fused_render.shell.prefs import canvases_enabled

logger = logging.getLogger(__name__)

router = APIRouter()

#: Default page size, mirroring the local home search's RANK_LIMIT.
RANK_LIMIT = 200

_NOT_READY = {"ready": False, "hits": [], "total": 0, "truncated": False}


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


@router.get("/api/remote-index/status")
def api_remote_index_status():
    if not canvases_enabled():
        return {"enabled": False, "state": "absent", "rows": 0,
                "fetched_at": None, "error": None}
    try:
        return {"enabled": True, **remote_index.status()}
    except Exception:  # noqa: BLE001 - details (local paths) stay in the log
        logger.exception("remote index status failed")
        return _error("could not read the Fused assets index state",
                      "internal", 500)


@router.get("/api/remote-index/rank")
async def api_remote_index_rank(request: Request, q: str = Query(default=""),
                                limit: str = Query(default=""),
                                ranked: str = Query(default="true")):
    if not canvases_enabled():
        return dict(_NOT_READY)
    # Parsed here rather than typed on the signature, so a malformed value
    # answers the contract's `{error, code}` body instead of FastAPI's 422.
    try:
        limit_n = int(limit) if limit.strip() else RANK_LIMIT
    except ValueError:
        return _error("'limit' must be an integer", "bad_url", 400)
    want_ranked = ranked.strip().lower() not in ("0", "false", "no", "off")
    async with cancellable(request) as token:
        try:
            return await asyncio.to_thread(remote_index.rank, q, limit_n,
                                           want_ranked, token)
        except Cancelled:
            logger.debug("remote index rank %r abandoned by the client", q)
            return Response(status_code=499)
        except Exception:  # noqa: BLE001 - details (local paths) stay in the log
            logger.exception("remote index rank failed for %r", q)
            return _error("Fused assets search failed", "internal", 500)
