"""/api/marketplace/* — the Marketplace's routes (backend: fused_render/
marketplace.py, which documents the udf.ai API, cache, install semantics).

Every endpoint is a sync def on purpose: the catalog build, search and
installs block on HTTP, and FastAPI runs sync endpoints on its threadpool,
keeping those waits off the event loop.

NOT gated on the `marketplace_enabled` pref (shell/prefs.py): like every other
feature flag here, the pref gates the shell's entry points, not the API.

The two mutating routes, POST /install and POST /ack, carry the same X-Fused
guard as every other write route (`X-Fused: 1`, else 403 — the D3 CSRF guard; duplicated here
like appfile.py's because a router must not import the app factory).
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
    """Start installing `slug` — or updating it, when a Marketplace copy is
    already in `<workspace>/app/` — in the background. Answers the job
    immediately (the running one, if any: idempotent). X-Fused-guarded."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        job = marketplace.start_install(str(body.get("slug") or ""))
    except marketplace.InstallError as exc:
        return _error(str(exc))
    return {"status": "ok", "job": job}


@router.get("/api/marketplace/jobs")
def api_marketplace_jobs():
    """Running installs/updates, plus finished ones for ~60 s."""
    return marketplace.list_jobs()


@router.get("/api/marketplace/acks")
def api_marketplace_acks():
    """The slugs whose Python notice the user accepted (marketplace.py's
    acks section: localStorage until D1384, lost on every port-walk
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
