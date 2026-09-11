"""Builds/refreshes the on-device Hub catalog pools (SPEC docs/HUB_CATALOG_SPEC.md).

One pool per capability: every Hub row carrying that capability's pipeline
tags, in formats this machine's installed runners can load, paged through
the Hub's list endpoint via its `Link: rel="next"` cursor (not the live
search route's single-page `_OVERFETCH`/`_MAX_FETCH`, which exists to keep
an interactive request fast — a background build has no such deadline).

Reuses `hub_models.py`'s `_EXPAND` field list and `hub_endpoint`/`_token`
SHAPES (same fields requested, same auth header), not those private
functions themselves: `_get`/`_fetch` swallow the response headers this
builder needs (`Link`, `RateLimit`), so this module makes its own httpx
calls rather than adapting those to return more than they promise their
existing callers.

**429 handling**: no retry loop. A 429 reads the `RateLimit` header (an
IETF-draft-shaped `limit=..., remaining=..., reset=<seconds>` value — the
same header family GitHub's REST API uses), persists a "blocked until"
timestamp via `hub_catalog.set_blocked_until`, and returns whatever was
already accumulated as a partial pool (or leaves the previous generation's
pool untouched if nothing was accumulated yet) rather than throwing the work
away. The next trigger (a search, or the daily delta) checks
`hub_catalog.is_blocked` first and skips silently until the window clears.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlencode

import httpx

from fused_render.ai import hub_catalog
from fused_render.ai import tasks as ai_tasks
from fused_render.ai.hub_catalog_config import HubCatalogConfig, load_config
from fused_render.ai.registry import for_capability

#: Same field list `hub_models._EXPAND` requests — the pool needs to feed
#: `_model_row` exactly the rows a live search would have, so a query over a
#: built pool sees identical row shapes to the live fallback.
_EXPAND = (
    "pipeline_tag", "downloads", "likes", "lastModified", "createdAt",
    "library_name", "gated", "private", "tags", "safetensors", "siblings",
    "config", "gguf",
)

#: Hub list endpoint page size. 1000 is the Hub's own maximum per the spec's
#: measured facts (23.5k rows / 24 pages at this size).
_PAGE_LIMIT = 1000

#: Safety cap on pages per (tag, format) pair — a pathological or
#: misclassified tag must not page forever if the Hub's `Link` header loops
#: or the count estimate in the spec (~24 pages for the biggest slice, MLX)
#: is wildly exceeded on some future tag.
_MAX_PAGES = 200

#: One request's timeout — matches `hub_models._TIMEOUT_S`'s reasoning
#: (generous for the Hub under load, short enough a hung read cannot wedge
#: the background thread indefinitely; a build already has no interactive
#: deadline, but an infinite hang would still pin a thread forever).
_TIMEOUT_S = 15.0

#: Fallback backoff when a 429's `RateLimit` header cannot be parsed for a
#: `reset` value — long enough to be a genuine pause, short enough that a
#: misparsed header does not block this capability for the rest of the day.
_DEFAULT_BACKOFF_S = 5 * 60

# capability -> in-flight build thread, so a second trigger (another search,
# the daily refresh) while a build is running is a no-op rather than a
# second concurrent build. Module-level like `supervisor.py`'s refresh
# thread handles; per-process, which is fine since `store_lock` is the
# cross-process guard for the actual write.
_building: dict[str, threading.Thread] = {}
_building_lock = threading.Lock()


def _token() -> str | None:
    from fused_render.server.routers.hub_models import _token as _hub_token
    return _hub_token()


def _hub_endpoint() -> str:
    from fused_render.server.routers.hub_models import hub_endpoint
    return hub_endpoint()


def _formats_for_capability(capability: str) -> tuple[str, ...]:
    """The Hub `filter=` format tags this machine's installed/active runner
    for `capability` declares (`Runner.hub_filter_tags`) — e.g. `("mlx",)` or
    `("gguf",)`. Empty when the active runner declares none, in which case
    the build pages the tag with no format filter at all (every format the
    Hub returns for that pipeline tag), matching what `hub_filter_tags`'s own
    docstring says an empty tuple means for the live path."""
    runner = for_capability(capability)
    if runner is None:
        return ()
    return tuple(runner.hub_filter_tags)


def _parse_ratelimit_reset(value: str | None) -> float | None:
    """Seconds until the window clears, from a `RateLimit` header shaped like
    `limit=100, remaining=0, reset=42` (IETF draft / GitHub REST style,
    per the spec's measured facts: 429 carries `RateLimit`, not
    `Retry-After`). Returns None when unparseable, so the caller can fall
    back to `_DEFAULT_BACKOFF_S` rather than raising."""
    if not value:
        return None
    for part in value.split(","):
        part = part.strip()
        if part.lower().startswith("reset="):
            raw = part.split("=", 1)[1].strip().strip('"')
            try:
                return float(raw)
            except ValueError:
                return None
    return None


def _page(url: str, headers: dict):
    """One GET: `(rows, response, error)`. `response` is the raw
    `httpx.Response` (case-insensitive `.headers`, RFC-5988-parsed `.links`)
    so the caller can read `Link`/`RateLimit` without this helper having to
    re-expose them field by field. `error` is only ever set for a genuine
    failure the caller should stop on (network error, non-list body). A 429
    is reported as `error == "429"` — a sentinel the caller branches on
    explicitly — so this helper stays free of any `hub_catalog`-specific
    backoff bookkeeping; that lives in the caller."""
    try:
        response = httpx.get(url, headers=headers, timeout=_TIMEOUT_S,
                              follow_redirects=True)
    except httpx.HTTPError as e:
        return None, None, f"network error: {e.__class__.__name__}"
    if response.status_code == 429:
        return None, response, "429"
    if response.status_code >= 400:
        return None, None, f"hub answered {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return None, None, "non-json response"
    if not isinstance(payload, list):
        return None, None, "unexpected response shape"
    return payload, response, None


def _fetch_all_pages(tag: str, fmt: str | None) -> tuple[list[dict], float | None, str | None]:
    """Every row for one (tag, format) pair, paged via `Link: rel="next"`.

    Returns `(rows, rate_limit_reset_s, error)`. `rate_limit_reset_s` is not
    None when a 429 interrupted paging — the parsed `reset` seconds from
    that response's `RateLimit` header (or `_DEFAULT_BACKOFF_S` if the
    header did not parse) — and `rows` holds whatever pages completed before
    it, which the caller still keeps (a partial page's worth of real rows is
    strictly better than throwing the whole fetch away)."""
    params: dict[str, object] = {
        "sort": "lastModified", "direction": -1, "limit": _PAGE_LIMIT,
        "expand[]": list(_EXPAND),
    }
    filter_value = [tag, fmt] if fmt else tag
    params["filter"] = filter_value
    headers = {"Accept": "application/json"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{_hub_endpoint()}/api/models?{urlencode(params, doseq=True)}"
    rows: list[dict] = []
    for _ in range(_MAX_PAGES):
        page_rows, response, error = _page(url, headers)
        if error == "429":
            reset_s = _parse_ratelimit_reset(
                response.headers.get("RateLimit") if response is not None else None)
            return rows, (reset_s if reset_s is not None else _DEFAULT_BACKOFF_S), None
        if error is not None:
            return rows, None, error
        rows.extend(r for r in page_rows if isinstance(r, dict))
        next_link = response.links.get("next") if response is not None else None
        if not next_link:
            break
        url = next_link["url"]
        # Only the first request needs the query params — the `Link` header
        # already carries a full absolute URL with the cursor baked in.
    return rows, None, None


def build_capability_pool(cfg: HubCatalogConfig, capability: str) -> dict:
    """Build (or fully rebuild) `capability`'s pool: fetch every
    (pipeline_tag, format) pair the capability resolves to, merge, and write
    one generation via `hub_catalog.write_pool`. Synchronous — callers that
    want this off the request thread use `ensure_build_started`.

    Returns a small summary dict: `{"rows": N, "rateLimited": bool}` — or,
    when a 429 was hit before a single row came back, does NOT call
    `write_pool` at all (an empty pool would be worse than no pool: the
    search route's `pool_exists` check would start serving zero results
    instead of falling back to the live path)."""
    tags = ai_tasks.tags_for_capability(capability)
    formats = _formats_for_capability(capability)
    format_list: tuple[str | None, ...] = formats if formats else (None,)

    merged: dict[str, dict] = {}
    rate_limit_reset_s: float | None = None
    for tag in tags:
        for fmt in format_list:
            rows, reset_s, _error = _fetch_all_pages(tag, fmt)
            for raw in rows:
                repo_id = raw.get("id") if isinstance(raw, dict) else None
                if not isinstance(repo_id, str):
                    continue
                # First writer wins per repo id across (tag, format) pairs —
                # the same de-dupe `_fetch_query_tags` already does for the
                # live multi-tag path.
                merged.setdefault(repo_id, {
                    "capability": capability,
                    "format": fmt or "",
                    "raw": raw,
                })
            if reset_s is not None:
                rate_limit_reset_s = reset_s
                break
        if rate_limit_reset_s is not None:
            break

    if rate_limit_reset_s is not None and not merged:
        # Nothing to write a pool for yet — just persist the backoff.
        hub_catalog.set_blocked_until(cfg, capability, time.time() + rate_limit_reset_s)
        return {"rows": 0, "rateLimited": True}

    # `write_pool` writes a fresh manifest entry (clearing any prior
    # `blockedUntil`), so the backoff has to be set AFTER it when both apply
    # — otherwise this write would immediately clobber the block it is
    # itself supposed to be recording.
    hub_catalog.write_pool(cfg, capability, list(merged.values()))
    if rate_limit_reset_s is not None:
        hub_catalog.set_blocked_until(cfg, capability, time.time() + rate_limit_reset_s)
    return {"rows": len(merged), "rateLimited": rate_limit_reset_s is not None}


def ensure_build_started(capability: str, *, cfg: HubCatalogConfig | None = None) -> bool:
    """Kick off a background build for `capability` if one is not already
    running, blocked on a 429 backoff, or already built. Non-blocking —
    called from the search route on a cache miss so the FIRST search in a
    capability pane still serves the live path unchanged (per spec) while
    the pool builds behind it.

    Returns True if a build was (just now, or already) started/in-flight,
    False if skipped (blocked, or already has a pool — a caller wanting to
    FORCE a rebuild, e.g. the daily delta, calls `build_capability_pool`
    directly instead)."""
    cfg = cfg or load_config()
    if hub_catalog.pool_exists(cfg, capability):
        return False
    if hub_catalog.is_blocked(cfg, capability):
        return False
    with _building_lock:
        existing = _building.get(capability)
        if existing is not None and existing.is_alive():
            return True

        def run() -> None:
            try:
                build_capability_pool(cfg, capability)
            except Exception:  # noqa: BLE001 - a background build must never
                # crash the thread silently into nothing; the next trigger
                # simply retries since no pool/manifest entry got written.
                import logging
                logging.getLogger(__name__).exception(
                    "hub-catalog build failed for %s", capability)

        thread = threading.Thread(
            target=run, name=f"hub-catalog-build-{capability}", daemon=True)
        _building[capability] = thread
        thread.start()
    return True
