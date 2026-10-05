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

import collections
import json
import logging
import os
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

import httpx

_log = logging.getLogger(__name__)

from fused_render.crashlog import describe_exit
from fused_render.ai import hub_catalog
from fused_render.ai import hub_metadata
from fused_render.ai import tasks as ai_tasks
from fused_render.ai.hub_catalog_config import HubCatalogConfig, load_config
from fused_render.ai.registry import available_runners, for_capability

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

# capability -> {"pagesDone": <shared list[int]>, "startedAt": <float>} for a
# build currently in flight, so `build_status` can report live progress
# without the request thread reaching into the build thread's locals. Set at
# the start of `build_capability_pool` and cleared in a `finally` so a
# crashed/finished build never leaves stale progress behind (the manifest
# entry `write_pool` wrote, if any, is the source of truth once this is gone).
_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()


def _token() -> str | None:
    from fused_render.server.routers.hub_models import _token as _hub_token
    return _hub_token()


def _hub_endpoint() -> str:
    from fused_render.server.routers.hub_models import hub_endpoint
    return hub_endpoint()


def _formats_for_capability(capability: str) -> tuple[str, ...]:
    """The Hub `filter=` format tags to page for `capability` on THIS
    machine — the UNION of `Runner.hub_filter_tags` across every runner that
    serves `capability` and can actually run here (`registry.available_runners`),
    not just the one `for_capability` currently prefers.

    C3 (bugbot): the pool is a shared, capability-wide cache — a search
    result asks "could ANYTHING on this machine load this repo", the exact
    question `available_runners` (not `for_capability`) answers (its own
    docstring: `for_capability` names the ACTIVE engine, which is the wrong
    question for search). Restricting the build to only the active runner's
    formats meant a repo servable by a second installed-but-not-preferred
    runner (`llamacpp-text`'s GGUF alongside an active `mlx-text`, D412's own
    example) was silently missing from the catalog pool, even though the
    live path (which already calls `available_runners`) would have shown it.

    A runner contributes its `hub_pool_tags`, else its `hub_filter_tags`
    (D1304). Before `hub_pool_tags`, a tagless `mlx-text` contributed nothing
    and the union collapsed to `("gguf",)`: "empty means unfiltered" only
    ever applied when EVERY runner was tagless.

    Empty when no available runner declares any filter tag at all, in which
    case the build pages the tag with no format filter (every format the Hub
    returns for that pipeline tag) — same "empty means unfiltered" contract
    `hub_filter_tags`'s own docstring documents for the live path."""
    seen: dict[str, None] = {}
    for runner in available_runners(capability):
        # `hub_pool_tags` first (D1304): a runner whose live-search filter
        # must stay empty (`mlx-text`) still has to contribute its format to
        # the pool, or its repos silently never enter it. `getattr` because
        # tests stand in bare namespaces for runners.
        for tag in (getattr(runner, "hub_pool_tags", ()) or runner.hub_filter_tags):
            seen.setdefault(tag, None)
    return tuple(seen)


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


def _fetch_all_pages(tag: str, fmt: str | None, *,
                      capability: str | None = None,
                      page_counter: list[int] | None = None,
                      on_rows=None,
                      ) -> tuple[list[dict], float | None, str | None]:
    """Every row for one (tag, format) pair, paged via `Link: rel="next"`.

    Returns `(rows, rate_limit_reset_s, error)`. `rate_limit_reset_s` is not
    None when a 429 interrupted paging — the parsed `reset` seconds from
    that response's `RateLimit` header (or `_DEFAULT_BACKOFF_S` if the
    header did not parse) — and `rows` holds whatever pages completed before
    it, which the caller still keeps (a partial page's worth of real rows is
    strictly better than throwing the whole fetch away).

    `capability`/`page_counter` are optional instrumentation: when given,
    every page fetched logs one INFO line (capability, tag/format, page
    index, rows so far, elapsed seconds since this call started) and bumps
    `page_counter[0]` so the caller can total pages across every (tag,
    format) pair for the manifest's `pages` count.

    `on_rows`: when given, each page's dict rows are handed to it as they
    arrive and NOT accumulated, so the returned `rows` is `[]`. This keeps a
    full build's memory at one page (~1,000 rows) instead of the whole
    slice; without it the accumulate-and-return behaviour is unchanged."""
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
    total = 0
    started = time.time()
    for page_index in range(_MAX_PAGES):
        page_rows, response, error = _page(url, headers)
        if error == "429":
            reset_s = _parse_ratelimit_reset(
                response.headers.get("RateLimit") if response is not None else None)
            return rows, (reset_s if reset_s is not None else _DEFAULT_BACKOFF_S), None
        if error is not None:
            return rows, None, error
        page_dicts = [r for r in page_rows if isinstance(r, dict)]
        total += len(page_dicts)
        if on_rows is not None:
            on_rows(page_dicts)
        else:
            rows.extend(page_dicts)
        del page_rows, page_dicts
        if capability is not None:
            if page_counter is not None:
                page_counter[0] += 1
            _log.info(
                "hub-catalog build: capability=%s tag=%s format=%s page=%d "
                "rows_so_far=%d elapsed=%.1fs",
                capability, tag, fmt or "", page_index, total,
                time.time() - started)
        next_link = response.links.get("next") if response is not None else None
        if not next_link:
            break
        url = next_link["url"]
        # Only the first request needs the query params — the `Link` header
        # already carries a full absolute URL with the cursor baked in.
    return rows, None, None


def build_capability_pool(cfg: HubCatalogConfig, capability: str, *,
                          formats: tuple[str, ...] | None = None,
                          page_counter: list[int] | None = None) -> dict:
    """Build (or fully rebuild) `capability`'s pool IN THIS PROCESS: fetch
    every (pipeline_tag, format) pair the capability resolves to, merge, and
    write one generation. Synchronous. This is the engine the build worker
    (`hub_catalog_worker`) runs; the server never calls it directly any more
    — `ensure_build_started` runs it in a child process so the transient
    memory of a build (and the heap fragmentation it leaves behind) dies with
    that process instead of pinning the server's footprint.

    Rows stream into a `hub_catalog.PoolWriter` a page at a time, de-duped by
    repo id, so peak memory is one page, not the slice.

    Returns a small summary dict: `{"rows": N, "rateLimited": bool}` — or,
    when a 429 was hit, does NOT write a pool at all (an empty/partial pool
    would be worse than no pool: the search route's `pool_exists` check would
    start serving zero/truncated results instead of falling back to the live
    path). Same principle for a genuine fetch error (network error, 5xx,
    non-JSON body) on ANY (tag, format) pair: nothing is committed, so a
    build that fails partway leaves whatever pool existed before (or none)
    untouched rather than committing a truncated one that `pool_exists` would
    then serve forever — the daily delta only ever WIDENS an existing pool, it
    never backfills a gap left by a build that silently skipped a pair
    (D1241).

    `formats`/`page_counter` are optional: the parent passes the format union
    it computed so the child cannot drift from it, and a counter object the
    worker uses to stream page progress back."""
    started_at = time.time()
    if page_counter is None:
        page_counter = [0]
    with _progress_lock:
        _progress[capability] = {"pagesDone": page_counter, "startedAt": started_at}
    try:
        return _build_capability_pool_inner(
            cfg, capability, started_at, page_counter, formats)
    finally:
        with _progress_lock:
            _progress.pop(capability, None)


def _build_capability_pool_inner(cfg: HubCatalogConfig, capability: str,
                                  started_at: float, page_counter: list[int],
                                  formats: tuple[str, ...] | None = None) -> dict:
    tags = ai_tasks.tags_for_capability(capability)
    if formats is None:
        formats = _formats_for_capability(capability)
    format_list: tuple[str | None, ...] = formats if formats else (None,)

    # Repo ids seen so far — a set of strings, NOT the rows. Rows stream into
    # the writer a page at a time and are dropped.
    seen: set[str] = set()
    rate_limit_reset_s: float | None = None
    with hub_catalog.PoolWriter(cfg, capability) as writer:
        for tag in tags:
            for fmt in format_list:
                def on_rows(page: list[dict], fmt=fmt) -> None:
                    fresh = []
                    for raw in page:
                        repo_id = raw.get("id")
                        # First writer wins per repo id across (tag, format)
                        # pairs — the same de-dupe `_fetch_query_tags`
                        # already does for the live multi-tag path.
                        if not isinstance(repo_id, str) or repo_id in seen:
                            continue
                        seen.add(repo_id)
                        fresh.append({"capability": capability,
                                      "format": fmt or "", "raw": raw})
                    writer.append(fresh)

                _rows, reset_s, error = _fetch_all_pages(
                    tag, fmt, capability=capability, page_counter=page_counter,
                    on_rows=on_rows)
                if error is not None:
                    # Abort the whole build without writing anything — see
                    # the docstring above (leaving the `with` discards the
                    # temp file). The next trigger (a search, or the daily
                    # delta's own retry-on-next-tick shape) simply tries
                    # again.
                    return {"rows": 0, "error": True}
                if reset_s is not None:
                    rate_limit_reset_s = reset_s
                    break
            if rate_limit_reset_s is not None:
                break

        if rate_limit_reset_s is not None:
            # C1 (bugbot): a 429 mid-build must NEVER produce a servable
            # pool, whether or not any rows were accumulated before it hit.
            # Writing a partial pool here made `pool_exists` accept a
            # truncated pool built from however many (tag, format) pairs
            # happened to complete before the rate limit landed — the search
            # route would then serve that partial pool FOREVER once the block
            # window passed, since `ensure_build_started` refuses to start a
            # new build while `pool_exists` is already true. Persist only the
            # backoff (leaving the `with` discards the partial file); the
            # next trigger, once `is_blocked` clears, restarts the WHOLE
            # build from page 1 of the first (tag, format) pair (no
            # partial-resume state) — a real request-cost trade-off accepted
            # deliberately over ever serving a truncated pool as if it were
            # complete.
            hub_catalog.set_blocked_until(cfg, capability, time.time() + rate_limit_reset_s)
            return {"rows": len(seen), "rateLimited": True}

        build_seconds = time.time() - started_at
        # `commit` writes a fresh manifest entry, clearing any prior
        # `blockedUntil` (a 429 never reaches here — it returned above).
        writer.commit(build_seconds=build_seconds, pages=page_counter[0],
                      started_at=started_at, formats=formats)
    _log.info(
        "hub-catalog build complete: capability=%s rows=%d pages=%d "
        "buildSeconds=%.1f rateLimited=False",
        capability, len(seen), page_counter[0], build_seconds)
    return {"rows": len(seen), "rateLimited": False}


def _fetch_delta_pages(tag: str, fmt: str | None, watermark: str, *,
                        capability: str | None = None,
                        page_counter: list[int] | None = None,
                        ) -> tuple[list[dict], float | None, str | None]:
    """Rows newer than `watermark` (a `lastModified` ISO8601 string, or `""`
    for "everything") for one (tag, format) pair — the daily refresh's
    fetch, paged the identical way `_fetch_all_pages` is, but STOPPING as
    soon as a page's rows fall at or below the watermark rather than always
    walking every page: the Hub returns pages sorted `lastModified` desc, so
    once one row in a page is not newer than the watermark, nothing after it
    in the whole ordering can be either. The spec's own measured fact (a 24h
    delta over the MLX slice is ~166 rows / 1 request) is exactly this
    shape: almost always a single page.

    Returns `(rows, rate_limit_reset_s, error)`, the same three-way shape
    `_fetch_all_pages` returns, for the identical reason: a 429 mid-page
    still keeps whatever newer rows were already seen on earlier pages."""
    params: dict[str, object] = {
        "sort": "lastModified", "direction": -1, "limit": _PAGE_LIMIT,
        "expand[]": list(_EXPAND),
    }
    params["filter"] = [tag, fmt] if fmt else tag
    headers = {"Accept": "application/json"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{_hub_endpoint()}/api/models?{urlencode(params, doseq=True)}"
    rows: list[dict] = []
    started = time.time()
    for page_index in range(_MAX_PAGES):
        page_rows, response, error = _page(url, headers)
        if error == "429":
            reset_s = _parse_ratelimit_reset(
                response.headers.get("RateLimit") if response is not None else None)
            return rows, (reset_s if reset_s is not None else _DEFAULT_BACKOFF_S), None
        if error is not None:
            return rows, None, error
        reached_watermark = False
        for raw in page_rows:
            if not isinstance(raw, dict):
                continue
            last_modified = raw.get("lastModified")
            if watermark and isinstance(last_modified, str) and last_modified <= watermark:
                # Everything from here on, in this page and every later one,
                # is at least as old — the Hub's own sort guarantees it.
                reached_watermark = True
                continue
            rows.append(raw)
        if capability is not None:
            if page_counter is not None:
                page_counter[0] += 1
            _log.info(
                "hub-catalog delta: capability=%s tag=%s format=%s page=%d "
                "rows_so_far=%d elapsed=%.1fs",
                capability, tag, fmt or "", page_index, len(rows),
                time.time() - started)
        if reached_watermark:
            break
        next_link = response.links.get("next") if response is not None else None
        if not next_link:
            break
        url = next_link["url"]
    return rows, None, None


def refresh_capability_pool_delta(cfg: HubCatalogConfig, capability: str) -> dict:
    """One `lastModified` delta for an ALREADY-BUILT pool (SPEC "Affected
    Flows": "daily thread -> Hub (1 lastModified delta per built pool)").

    Unlike `build_capability_pool`, this NEVER refetches the whole slice: it
    asks the Hub only for rows newer than the newest `lastModified` the pool
    already holds, and merges any match (new repo, or an existing repo whose
    metadata changed) into the existing set by id before writing a fresh
    generation. A capability with no pool yet is a no-op
    (`{"skipped": "no-pool"}` — the daily thread must never build one from
    scratch, only widen one that already exists, exactly as the spec's own
    line "unbuilt pools are never refreshed" requires) and so is one still
    sitting inside a 429 backoff window (`{"skipped": "blocked"}`).

    The merge itself runs in a short-lived child process (see
    `_run_pool_job`): rewriting a pool streams every existing row through
    memory, and that transient heap must not linger in the server."""
    if not hub_catalog.pool_exists(cfg, capability):
        return {"skipped": "no-pool"}
    if hub_catalog.is_blocked(cfg, capability):
        return {"skipped": "blocked"}
    result = _run_pool_job(cfg, capability, "delta", _formats_for_capability(capability))
    _invalidate_changed(result.pop("changedIds", ()))
    return result


def _invalidate_changed(changed_ids) -> None:
    """SPEC item 5's TTL replacement: a repo whose `lastModified` the delta
    just showed us has moved may now have a stale harvested `config.json`
    reading in `hub_metadata`'s store even though its own wall-clock TTL has
    not elapsed yet — force it to refetch on next `get()` rather than waiting
    out the full TTL. A repo this delta never touched (unchanged
    `lastModified`, or belonging to no pool at all) is untouched here and
    keeps relying on `hub_metadata`'s own TTL fallback. Runs in the SERVER
    process (the metadata store is its state), from the ids the child
    reports."""
    for repo_id in changed_ids:
        try:
            hub_metadata.invalidate(repo_id)
        except Exception:  # noqa: BLE001 - one repo's invalidation failing
            # must not stop the pool write, which already succeeded.
            pass


def _refresh_delta_inprocess(cfg: HubCatalogConfig, capability: str, *,
                              formats: tuple[str, ...] | None = None,
                              page_counter: list[int] | None = None) -> dict:
    """`refresh_capability_pool_delta`'s work, in THIS process (what the
    worker runs). Streams the existing pool back out batch by batch rather
    than loading it: only the (small) set of changed rows is held, and
    existing rows whose id is in it are dropped from the copy. Returns the
    public result plus `changedIds` for the parent's cache invalidation.

    Row ORDER differs from the old in-memory merge: a changed repo used to be
    replaced in place and is now written after the untouched rows. Nothing
    reads pool order (`query_pool` callers sort/score)."""
    if not hub_catalog.pool_exists(cfg, capability):
        return {"skipped": "no-pool"}
    if hub_catalog.is_blocked(cfg, capability):
        return {"skipped": "blocked"}

    started_at = time.time()
    if page_counter is None:
        page_counter = [0]
    watermark = hub_catalog.max_last_modified(cfg, capability)

    tags = ai_tasks.tags_for_capability(capability)
    if formats is None:
        formats = _formats_for_capability(capability)
    format_list: tuple[str | None, ...] = formats if formats else (None,)

    rate_limit_reset_s: float | None = None
    new_by_id: dict[str, dict] = {}
    for tag in tags:
        for fmt in format_list:
            new_rows, reset_s, error = _fetch_delta_pages(
                tag, fmt, watermark, capability=capability, page_counter=page_counter)
            if error is not None:
                # A genuine fetch error (not a 429 — that already returns
                # via `reset_s`) must not write a pool at all: unlike a 429,
                # which still writes whatever was already merged from EARLIER
                # (tag, format) pairs plus the existing rows, a silently
                # skipped pair here would mean this capability's next delta
                # tick recomputes its watermark from a pool that never saw
                # that pair's rows — an unbounded gap the delta path (which
                # only ever widens, never backfills a whole tag/format scan)
                # can never close on its own. Leave the existing pool exactly
                # as it was; the next daily tick retries this pair too.
                return {"skipped": "error"}
            for raw in new_rows:
                repo_id = raw.get("id") if isinstance(raw, dict) else None
                if isinstance(repo_id, str):
                    new_by_id[repo_id] = raw
            if reset_s is not None:
                rate_limit_reset_s = reset_s
                break
        if rate_limit_reset_s is not None:
            break

    with hub_catalog.PoolWriter(cfg, capability) as writer:
        for batch in hub_catalog.iter_pool_rows(cfg, capability):
            keep = [{"capability": capability, "format": "", "raw": raw}
                    for raw in batch if raw.get("id") not in new_by_id]
            writer.append(keep)
        writer.append([{"capability": capability, "format": "", "raw": raw}
                       for raw in new_by_id.values()])
        total = writer.rows
        build_seconds = time.time() - started_at
        # Unlike `build_capability_pool`, a delta always has the EXISTING rows
        # to write even when a 429 lands before a single new one comes back —
        # there is no "empty pool would be worse than no pool" case here,
        # only "no widening happened this round".
        writer.commit(build_seconds=build_seconds, pages=page_counter[0],
                      started_at=started_at, formats=formats)
    # `commit` clears `blockedUntil`, so a backoff must be set AFTER it.
    if rate_limit_reset_s is not None:
        hub_catalog.set_blocked_until(cfg, capability, time.time() + rate_limit_reset_s)
    _log.info(
        "hub-catalog delta complete: capability=%s rows=%d pages=%d "
        "buildSeconds=%.1f rateLimited=%s",
        capability, total, page_counter[0], build_seconds,
        rate_limit_reset_s is not None)
    return {"rows": total, "rateLimited": rate_limit_reset_s is not None,
            "changedIds": list(new_by_id)}


#: The build worker entrypoint, run as `python -m <module>` (never a script
#: path: a py2app bundle has no source file — see `index/runner.py`).
WORKER_MODULE = "fused_render.ai.hub_catalog_worker"

#: Worker -> parent protocol, on the worker's stdout (stderr is merged in, so
#: anything NOT carrying one of these prefixes is plain log output).
PAGES_PREFIX = "@@pages "
RESULT_PREFIX = "@@result "

#: Last lines of worker output kept for the failure log.
_TAIL_LINES = 40


def _spawn_kwargs() -> dict:
    """Popen kwargs for the worker. On POSIX these MUST stay
    posix_spawn-compatible: `close_fds=False`, no `cwd=`, no
    `start_new_session`/`preexec_fn`, and an ABSOLUTE `sys.executable` (not
    realpath'd). Any of those forces CPython onto fork()+exec, and a fork of a
    server process that has touched pyproj/rasterio runs PROJ's pthread_atfork
    handler and dies with SIGSEGV before Python starts. Same discipline as
    `index/runner.py:_detach_kwargs`/`envinstall.py`."""
    if os.name == "nt":
        return {}
    return {"close_fds": False}


def _run_pool_job(cfg: HubCatalogConfig, capability: str, mode: str,
                  formats: tuple[str, ...], page_counter: list[int] | None = None) -> dict:
    """Run one pool job (`mode` is `"build"` or `"delta"`) in a short-lived
    child process and return its result dict.

    Why a child: a full build parses ~100k raw Hub dicts, and the pymalloc
    arenas that leaves fragmented are never handed back to the OS, so an
    in-server build left the app at a ~3 GB footprint until restart. A child's
    memory is returned in full when it exits.

    The child gets only small args (mode, capability, catalog dir, the format
    union as JSON) — never rows, never the HF token: it resolves the token
    itself through the same `_token()` the in-process path uses, and inherits
    this process's environment. A crash or a non-zero exit raises
    `RuntimeError` (the caller's existing exception logging handles it); the
    worker only ever commits through `PoolWriter`/`set_blocked_until`, so a
    killed child leaves no pool file and no manifest entry behind."""
    cmd = [sys.executable, "-m", WORKER_MODULE, mode, capability, cfg.dir,
           json.dumps(list(formats))]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        **_spawn_kwargs())
    tail: collections.deque[str] = collections.deque(maxlen=_TAIL_LINES)
    result: dict | None = None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith(PAGES_PREFIX):
                try:
                    if page_counter is not None:
                        page_counter[0] = int(line[len(PAGES_PREFIX):])
                except ValueError:
                    pass
            elif line.startswith(RESULT_PREFIX):
                try:
                    parsed = json.loads(line[len(RESULT_PREFIX):])
                    result = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    pass
            else:
                tail.append(line)
    finally:
        proc.stdout.close()
        returncode = proc.wait()
    if returncode != 0 or result is None:
        desc = describe_exit(returncode)
        _log.error("hub-catalog %s worker for %s pid %s %s; last output:\n%s",
                   mode, capability, proc.pid, desc, "\n".join(tail))
        raise RuntimeError(f"hub-catalog {mode} worker for {capability} {desc}")
    return result


def build_capability_pool_in_child(cfg: HubCatalogConfig, capability: str) -> dict:
    """`build_capability_pool`, run in a child process. Same return value,
    same `_progress` registration (so `build_status` reports live pages),
    same 429/manifest semantics — those are the worker's own."""
    started_at = time.time()
    page_counter = [0]
    with _progress_lock:
        _progress[capability] = {"pagesDone": page_counter, "startedAt": started_at}
    try:
        return _run_pool_job(cfg, capability, "build",
                             _formats_for_capability(capability), page_counter)
    finally:
        with _progress_lock:
            _progress.pop(capability, None)


def build_status(capability: str, *, cfg: HubCatalogConfig | None = None) -> dict:
    """This capability's build/refresh state, for the search route's
    `poolState` (spec item 2): `{"state": "building"|"blocked"|"none",
    "pagesDone": int | None, "startedAt": float | None, "blockedUntil":
    float | None}`.

    `"blocked"` wins over `"building"` if somehow both were true (should not
    happen — a build clears its own progress entry before a caller could
    observe it mid-backoff, but the manifest's `blockedUntil` is the more
    conservative fact to report if it ever did). `"none"` covers both "no
    build has ever run" and "a build just finished/failed and left nothing
    in flight" — the route only calls this when `pool_exists` is already
    False, so `"none"` here means "still on the live path, nothing to wait
    for" rather than "pool is ready"."""
    cfg = cfg or load_config()
    entry = hub_catalog.pool_entry(cfg, capability)
    blocked_until = entry.get("blockedUntil") if entry else None
    is_blocked_now = (isinstance(blocked_until, (int, float))
                      and time.time() < blocked_until)

    with _progress_lock:
        progress = _progress.get(capability)
        pages_done = progress["pagesDone"][0] if progress else None
        started_at = progress["startedAt"] if progress else None
    # A build in flight (whatever thread started it — `ensure_build_started`
    # or a direct call) always has a `_progress` entry, registered before
    # its first Hub request and cleared in a `finally` when it exits by any
    # path (success, error, 429). That is a more direct "is one running
    # right now" signal than the `_building` thread registry, which only
    # `ensure_build_started` populates.
    is_building = progress is not None

    if started_at is None and entry:
        started_at = entry.get("startedAt")

    if is_blocked_now:
        state = "blocked"
    elif is_building:
        state = "building"
    else:
        state = "none"

    return {"state": state, "pagesDone": pages_done, "startedAt": started_at,
            "blockedUntil": blocked_until}


def _formats_are_stale(cfg: HubCatalogConfig, capability: str) -> bool:
    """C3 (bugbot): whether `capability`'s BUILT pool covers strictly fewer
    Hub format tags than this machine can serve right now — e.g. a pool
    built while only `mlx-text` was installed, on a machine that has since
    gained `llamacpp-text` too. A strict SUBSET (not merely "different"):
    a machine that LOST a runner since the last build has a pool that is
    now too WIDE, not too narrow, and narrowing it back down is the daily
    delta/rebuild's job, not something worth forcing early — only a pool
    that is missing formats it could now cover is stale in the sense this
    function exists to catch. An entry from before this field existed
    (`formats` absent) is never treated as stale — there is nothing to
    compare against, and re-triggering every pre-existing pool's build on
    the next search would defeat the whole point of the on-device pool."""
    entry = hub_catalog.pool_entry(cfg, capability)
    if not entry:
        return False
    stored = entry.get("formats")
    if stored is None:
        return False
    current = set(_formats_for_capability(capability))
    return set(stored) < current


def _schema_is_stale(cfg: HubCatalogConfig, capability: str) -> bool:
    """D1276: whether `capability`'s built pool predates the `modelType`
    parquet column (item 2b). OPPOSITE polarity from `_formats_are_stale`
    above, deliberately: that check treats an absent `formats` field as
    never-stale because it predates the field's very existence and nothing
    written it could be compared against, one pool among many written under
    many different histories. `schemaVersion` is different — `write_pool`
    has written it on EVERY build since it was introduced, unconditionally,
    so an entry with no `schemaVersion` key (or one below
    `hub_catalog.ROW_SCHEMA_VERSION`) unambiguously means "built before the
    column exists", not "built before this check existed". Every such pool
    IS stale, and each rebuilds once, the same way a machine that just
    gained a new runner's format rebuilds once under the check above."""
    entry = hub_catalog.pool_entry(cfg, capability)
    if not entry:
        return False
    stored = entry.get("schemaVersion")
    return not isinstance(stored, int) or stored < hub_catalog.ROW_SCHEMA_VERSION


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
    if (hub_catalog.pool_exists(cfg, capability)
            and not _formats_are_stale(cfg, capability)
            and not _schema_is_stale(cfg, capability)):
        return False
    if hub_catalog.is_blocked(cfg, capability):
        return False
    with _building_lock:
        existing = _building.get(capability)
        if existing is not None and existing.is_alive():
            return True

        def run() -> None:
            try:
                build_capability_pool_in_child(cfg, capability)
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
