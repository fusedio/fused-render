"""/api/marketplace/* — the Marketplace's routes (backend: fused_render/
marketplace.py, which documents the API, cache, install semantics; the
reviewed marketplace is docs/CONTRIB_LOOP.md §18).

Every endpoint is a sync def on purpose: the catalog build, search, clones
and the engine's moves block on HTTP and git, and FastAPI runs sync
endpoints on its threadpool, keeping those waits off the event loop.

Never gated: the Community is always offered (2026-10-29; before that its
pref gated the shell's entry points, not the API).

The mutating routes (POST /install, /ack, /adopt, /check) carry the same
X-Fused guard as every other write route (`X-Fused: 1`, else 403 — the D3
CSRF guard; duplicated here like appfile.py's because a router must not
import the app factory).

Answers the frontend reads (shapes in marketplace.py):

- POST /install `{slug, switch?}` →
  `{status: "ok", job}` — the job started (or the one already running):
  `{slug, action: "install"|"update"|"switch", state: "queued"|
  "downloading"|"installing"|"done"|"error", step: "download"|"place"|
  "done", progress, bytes_done, bytes_total, message, path, error}`;
  `{status: "confirm", switch: "reviewed", slug, path, who, message}` — a
  GitHub-link copy of the same repo exists: nothing ran; post again with
  `switch: true` to switch it to reviewed releases (D13);
  `{status: "error", message}` (400) — the request cannot start: bad or
  unknown slug, a legacy copy ("Switch to the reviewed version first"), the
  user's own repo, an unrecorded clone, a row with no reviewed source.
  A job that fails later carries `state: "error"` and `error` = the plain
  sentence (the engine's own words for an update or a switch).
- POST /adopt `{slug}` → `{status: "ok", path, message}` (the legacy copy is
  a marketplace copy now) | `{status: "error", message}`.
- POST /check `{slug?}` → `{status: "ok", catalog: {ok, stale, fetched_at,
  message}, blocklist: {ok, checked_at, count}, enforced: [..], sync:
  <the sync's answer>|null, app: AppEntry|null}`.
- GET /blocklist → `{blocked: [{slug, commit, level, reason, what_to_do,
  at}], checked_at, fetched_at, generated_at, stale, status, message}`.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse, Response

from fused_render import marketplace

router = APIRouter()

#: The CSP keeps a community SVG/HTML file opened DIRECTLY at its asset URL
#: from running script on this origin; an <img> of it is unaffected.
_ASSET_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox",
}


def _error(message: str, status: int = 400) -> JSONResponse:
    # Both spellings: the contract's {status:"error", message} and the
    # {error} shape the shell's shared fetch helpers read.
    return JSONResponse({"status": "error", "message": message, "error": message},
                        status_code=status)


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    if x_fused != "1":
        return _error("missing or invalid X-Fused header", status=403)
    return None


@router.get("/api/marketplace/catalog")
def api_marketplace_catalog(refresh: str = "0"):
    """The catalog (cached 10 min; `refresh=1` forces a rebuild, at most once
    a minute). The only route that rebuilds a stale cache — and it answers
    the cache at once while another request's rebuild is running. Answers
    `{status:"error", message}` only when nothing is cached at all."""
    return marketplace.catalog(refresh=refresh in ("1", "true", "yes"))


@router.get("/api/marketplace/search")
def api_marketplace_search(q: str = "", category: str = "", cursor: str = "", limit: int = 24):
    """Server-ranked search (the udf.ai `marketplace_search`): every
    AppEntry plus `score`/`highlights`, and `next_cursor` for the next page.
    Like /catalog, a failure is `{status:"error", message}` with HTTP 200."""
    return marketplace.search(q, category=category, cursor=cursor,
                              limit=max(1, min(int(limit), 100)))


@router.get("/api/marketplace/app/{slug}")
def api_marketplace_app(slug: str):
    """One app: its AppEntry, readme markdown, file list, releases and the
    base URL its relative readme links resolve against."""
    detail = marketplace.app_detail(slug)
    if detail is None:
        return _error(f"no app named {slug!r}", status=404)
    return detail


@router.get("/api/marketplace/asset/{slug}/{path:path}")
def api_marketplace_asset(slug: str, path: str, v: str | None = None):
    """Raw bytes of one file in a catalog app (preview.png, icon.svg, readme
    images…). 404 unless `path` is the app's own icon or preview or a file in
    its detail's file list.

    Browser caching follows the URL: the catalog's icon/preview URLs carry
    `?v=<release id, first 12>`, so a day is safe (new content, new URL). Any other
    URL — unversioned readme images resolved against `readme_base`, or a `v`
    that is not the app's current release (a readme's own `?v=1`) —
    revalidates every time, so a catalog that moved on is never shadowed by
    an old cached copy."""
    got = marketplace.get_asset_versioned(slug, path)
    if got is None:
        return _error("not found", status=404)
    data, content_type, version = got
    cache = "max-age=86400" if v and v == version else "no-cache"
    return Response(data, media_type=content_type,
                    headers={**_ASSET_HEADERS, "Cache-Control": cache})


@router.post("/api/marketplace/install")
def api_marketplace_install(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Start installing `slug` — or updating its marketplace copy, or (with
    `switch: true`) switching the user's GitHub-link copy of the same repo to
    reviewed releases, or (with `overwrite: true`) installing over the
    user's showcase copy — in the background. Answers the job immediately
    (the running one, if any: idempotent), or a `confirm` question (module
    docstring). X-Fused-guarded."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        got = marketplace.start_install(str(body.get("slug") or ""), switch=body.get("switch") is True,
                                        overwrite=body.get("overwrite") is True)
    except marketplace.InstallError as exc:
        return _error(str(exc))
    if got.get("status") == "confirm":
        return got
    return {"status": "ok", "job": got}


@router.get("/api/marketplace/jobs")
def api_marketplace_jobs():
    """Running installs/updates, plus finished ones for ~60 s."""
    return marketplace.list_jobs()


@router.get("/api/marketplace/acks")
def api_marketplace_acks():
    """The slugs whose Python notice the user accepted (marketplace.py's
    acks section: localStorage until D1386, lost on every port-walk
    relaunch)."""
    return {"slugs": marketplace.python_acks()}


@router.post("/api/marketplace/ack")
def api_marketplace_ack(body: dict = Body(default=None),
                        x_fused: str | None = Header(default=None)):
    """Accept `slug`'s Python notice; idempotent. X-Fused-guarded."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    slug = body.get("slug") if isinstance(body, dict) else None
    try:
        slugs = marketplace.ack_python(slug)
    except ValueError as exc:
        return _error(str(exc))
    return {"ok": True, "slugs": slugs}


@router.get("/api/marketplace/installed")
def api_marketplace_installed():
    """Every Marketplace install in the workspace (catalog-backed where the
    catalog still lists it)."""
    return marketplace.installed_list()


@router.post("/api/marketplace/adopt")
def api_marketplace_adopt(body: dict = Body(default=None), x_fused: str | None = Header(default=None)):
    """"Switch to the reviewed version" for a copy installed by the old
    Marketplace (`market.adopt_legacy` in place; edits and data stay).
    X-Fused-guarded."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    slug = body.get("slug") if isinstance(body, dict) else None
    try:
        got = marketplace.adopt(str(slug or ""))
    except marketplace.InstallError as exc:
        return _error(str(exc))
    except ImportError:
        return _error("Switching to the reviewed version isn't available in this build.", status=503)
    return {"status": "ok", "path": got["path"], "message": got["message"]}


@router.get("/api/marketplace/blocklist")
def api_marketplace_blocklist():
    """The block list fused enforces (cached; the 10-minute TTL like the
    catalog's, the last list when offline) and when it was last checked."""
    return marketplace.blocklist()


@router.post("/api/marketplace/check")
def api_marketplace_check(body: dict = Body(default=None), x_fused: str | None = Header(default=None)):
    """"Check for a new release": the catalog rebuilt now, the block list
    refreshed and enforced, then (with `slug`) a sync of that marketplace
    copy. X-Fused-guarded."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    slug = body.get("slug") if isinstance(body, dict) else None
    return marketplace.check(slug if isinstance(slug, str) and slug else None)
