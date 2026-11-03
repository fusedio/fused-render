"""The ONE hosted canvas fused-render's backend lives on (owner 2026-10-27:
"only have 1 canvas for all the backend required in fused-render").

Every UDF fused-render calls or links to is on this canvas
(`fused_app_marketplace`, docs/CONTRIB_LOOP.md §18.3): the marketplace
catalog/search/detail/collections/categories/blocklist/stats, the install
lookup (`fused_app_install`), the public listing page (`fused_app_listing`)
and the GitHub install page (`fused_app_github`). Change the host or the
canvas path HERE and nowhere else; `frontend/src/platform/lib/backend-canvas.ts`
mirrors the two constants for links the page builds itself.

The canvas is public (owner 2026-10-27), so it is addressed by name, not by a
share token: `https://udf.ai/fc_public/fused_app_marketplace/<udf>.<ext>`. The
public path REQUIRES the extension — `<udf>.json` for data, `<udf>.html` for a
page; the bare `<udf>` (and `?format=json`) answers 404 there.

Not here, by design: the canvases the user's own Fused account creates for
public sharing (share_app.py / share_file.py push one canvas per app under
the user's login) — those are the user's, not the backend's.

`FUSED_RENDER_MARKETPLACE_API` (env) overrides the base per process — a
test or a dev build pointing at another share of the same canvas.
"""
from __future__ import annotations

import os
from urllib.parse import quote, urlencode

CANVAS_HOST = "https://udf.ai"
#: The public canvas's path: `fc_public/<canvas name>`.
CANVAS_PATH = "fc_public/fused_app_marketplace"
#: `<host>/<path>` — what every UDF URL starts with.
API_BASE = f"{CANVAS_HOST}/{CANVAS_PATH}"
ENV_OVERRIDE = "FUSED_RENDER_MARKETPLACE_API"


def api_base() -> str:
    """The base in force: the env override, else `API_BASE`. Read per call."""
    return (os.environ.get(ENV_OVERRIDE) or API_BASE).strip().rstrip("/")


def udf_url(name: str, ext: str = "json", **params) -> str:
    """`<base>/<name>.<ext>?k=v…` (None params dropped)."""
    query = {k: v for k, v in params.items() if v is not None}
    return f"{api_base()}/{name}.{ext}" + (f"?{urlencode(query)}" if query else "")


def github_link_url(repo: str) -> str:
    """The GitHub install page for `<owner>/<repo>` (share_app.py's install
    link): the canvas's `fused_app_github` served as HTML."""
    return f"{api_base()}/fused_app_github.html?repo={quote(repo, safe='/')}"


def listing_page_url(slug: str) -> str:
    """The public listing page of a marketplace app (§18.3 B6)."""
    return f"{api_base()}/fused_app_listing.html?slug={quote(slug, safe='')}"
