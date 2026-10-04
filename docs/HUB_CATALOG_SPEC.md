# On-device Hub catalog for hub search

Handoff for the builder. Scope below was agreed with the user; the
"Implementation notes" section is orientation, not a prescription. Where
this document and the code disagree, trust the code and record the
correction in `DECISIONS.md`.

## Context

Hub search fetches at most 200 rows live from the Hugging Face Hub per settled search, sorted by downloads because `best` and `fit` are not Hub sort fields, then scores only those rows against this machine. A fresh MLX quant with a handful of downloads can never appear under the default sort, and every keystroke and pane open costs a Hub request, which risks rate limiting. Scoring is cheap arithmetic; the live fetch is the only bottleneck. This change moves the candidate source to a per-capability catalog stored on the user's device, built the way the file index is built, so ranking runs over the whole slice with no Hub traffic per search.

## What's Changing

- Each capability gets a pool of every Hub row carrying that capability's pipeline tags in formats this machine's installed runners can load. Pools are stored as parquet in the branch-scoped home dir and queried with DuckDB, mirroring the file index's write-then-swap-manifest and fresh-connection-per-query pattern.
- A pool is built lazily the first time its capability pane is searched. Small capabilities cost a few requests; text generation costs most of the ~16 s slice. Until a pool exists, or while a 429 blocks its build, that pane serves today's live Hub search unchanged.
- Every search over a built pool is one local query: filters, ILIKE substring text search, D780 scoring, sort, and facets all run over the whole pool with zero Hub requests. The 200-row cap and 90 s cache do not apply on this path.
- Facets, hidden-unfit counts, and family pull-in widen from the fetched window to the whole pool, which fixes the documented missing-publisher facet bug.
- A daily supervisor thread, shaped like hardware refresh, runs one `lastModified` delta per built pool. Unbuilt pools are never refreshed.
- The builder reads the `RateLimit` header on 429, backs off, and resumes later. No retry storms.
- The shared tag table gains `image-to-image` under image generation, so those repos become searchable and loadable app-wide. The image edit path must handle them.
- **Added after scoping (user: "build it all"):** `hub_metadata` moves its store from `ai_hub_metadata.json` into the same parquet/DuckDB catalog directory, as its own table keyed by repo id. Its per-repo `config.json` fetch stays (the bulk list endpoint's `expand[]=config` returns only `architectures`, `model_type`, `quantization_config.bits`, `tokenizer_config`, `chat_template_jinja`; none of the KV-cache geometry fields). What changes: the 13-day TTL is replaced by re-harvest on `lastModified` change as seen by the daily pool delta (rows not in any pool keep a TTL fallback); the 500-repo `MAX_REPOS` bound goes away; the shallow fields the bulk fetch does return (`model_type`, first `architectures` entry, quant `bits`) are written into pool rows so a lookup that only needs those never triggers a per-repo fetch.

## Affected Flows

```
TODAY
  open pane / type ──► server ──► Hub (1 request, ≤200 rows by downloads) ──► score 96 ──► page

AFTER
  first search in a capability pane ──► live Hub search serves the page
                                    └─► background: Hub (tag × format, ~5-20 pages) ──► pool on disk
  every later search in that pane   ──► server ──► DuckDB over pool ──► score all ──► page   (0 requests)
  daily thread                      ──► Hub (1 lastModified delta per built pool) ──► pool
                                                                                    └─► rows whose lastModified moved: hub_metadata re-harvest queued
  429 during build                  ──► back off per RateLimit header; pane stays on live search
```

- **Opening a capability pane for the first time**: results look like today, the pool builds behind it, later searches switch to catalog mode.
- **Sorting by best or fit**: candidates are the whole pool, so a three-day-old quant with nine downloads can rank first if it fits.
- **Typing a text query**: substring match over the pool, no Hub request. Hub-side search quality is traded for zero traffic.
- **Facet chips**: publisher and quant counts reflect the whole pool.
- **Load more**: pages through the locally ranked list instead of refetching a wider Hub window.
- **Rate-limited machine**: live search fallback keeps the pane usable; the pool completes when the window clears.
- **Image generation pane and Load button**: `image-to-image` repos appear and are loadable.
- **Server start**: the daily refresh thread starts alongside hardware and hub-metadata refresh.
- **hub_metadata readers** (`ai_runtime.py`, `supervisor.py`, `app.py` startup): same `cached()` / `get()` API, backed by the catalog store. Existing JSON file, if present, is imported once and then ignored.

## Constraints

- No burst of requests on page open or per keystroke. Per-pane first build and one delta per day are the only Hub traffic on the catalog path.
- Pools filter to installed-runner formats. The row builder's drop rules and the D780 scoring math are unchanged and still run at query time, so a runner change does not force a rebuild.
- The POST + `X-Fused` guard on the search endpoint stays.
- Storage follows the existing home-dir convention, branch-scoped, atomic swap, manifest last. Multiple dev servers sharing the home dir must not corrupt or redundantly rebuild a pool. Reuse `fused_render/index/store.py` patterns (`store_lock`, manifest-last swap, fresh DuckDB connection per query); do not import the file index's schema.
- Tests that fake the Hub must keep the no-egress guard; the bulk builder needs its own coverage under it. Existing assertions on Hub URL params and call counts move to the live fallback path.
- The `hub/size` lazy hydration endpoint is unchanged and remains the per-row detail path.
- hub_metadata's single network seam (`_fetch_raw`) stays the one function tests monkeypatch. Request paths never fetch.

## Out of Scope

- Hub-side text search once a pool exists.
- Whole-slice or all-format catalogs, and any explicit build button or progress UI.
- A `total` count field in the search response.
- Changes to the frontend sort or grouping logic beyond consuming the same response shape.
- Reusing code from the earlier sub-20-second experiment, which is not in this repo.
- Bulk-fetching `config.json` for pool rows. One per-repo read on demand only, as today.

## Implementation notes (orientation, verified against the code on 2026-09-11)

Key seams in `fused_render/server/routers/hub_models.py`:

- `_EXPAND` (~:182) is the field list sent to the Hub list endpoint; the pool builder should request the same fields so `_model_row` (~:1060) can build rows from stored raw rows unchanged. Store the raw Hub row fields the pool needs (id, pipeline_tag, downloads, likes, lastModified, createdAt, library_name, gated, private, tags, safetensors.total, gguf.total, siblings filenames, shallow config) plus the shallow config fields listed above.
- `_fetch(params)` (~:1396) is the single live Hub call; `_get` (~:1367) wraps httpx. The catalog path bypasses both.
- `api_hub_search` (~:1683) is the endpoint. Branch early: if a built pool exists for the requested capability, run the catalog path; else today's code path unchanged, plus a non-blocking "ensure build started" call.
- `_OVERFETCH`/`_MAX_FETCH` (~:554) and the 90 s `_cache` (~:145, ~:1562) apply only to the live path.
- `_facets`, `_pull_in_family_members`, hiddenUnfit counting: on the catalog path these run over the whole pool result set.
- `supported_tags()` (~:686) and `fused_render/ai/tasks.py` `_TASKS` (:103) define capability → pipeline tags. `image-to-image` (:192) currently has `None` capability; set it to `IMAGE_GENERATION`. Check `capability_for_tag` callers and the image edit path (grep `image_strength`, `image_path`, mflux edit) so an `image-to-image` repo loads and edits.
- Formats: pools filter to Hub `filter=` values for installed runners (e.g. `mlx`, `gguf`); look at how the live path derives its format tags today and reuse that.

Hub API facts (measured):

- `GET /api/models?filter=<fmt>&pipeline_tag=<tag>&sort=lastModified&limit=1000&expand[]=...`; cursor pagination via the `Link: <url>; rel="next"` header. MLX slice ≈ 23.5k rows / 24 pages / ~16 s. 24 h delta ≈ 166 rows / 1 request.
- Rate limits are request counts. On 429 the `RateLimit` header (not `Retry-After`) carries the window. Back off, persist "blocked until", resume on the next trigger.
- Reference probes: `probe_hf_paging.py` / `probe_hf_design.py` were scratch scripts; the projection there (~262 B/row) is a size guide only.

Storage:

- Directory: `os.path.join(storage.home_dir(), "hub_catalog")` (`from fused_render.shell import storage`), mirroring `fused_render/index/config.py:index_dir`.
- One parquet file per capability pool, generation-numbered, plus a `manifest.json` written last. hub_metadata as a separate parquet table (`metadata-<gen>.parquet`) with a small append log compacted into it; single-row upserts from a background thread only.
- Fresh DuckDB connection per query; never a long-lived shared connection (see index store docstrings for why).

Supervisor:

- `start_hardware_refresh` (`fused_render/ai/supervisor.py` ~:2180) and `start_hub_metadata_refresh` (~:2260, tick at ~:2237) are the templates for `start_hub_catalog_refresh`. Wire it in `fused_render/server/app.py` next to the existing two (~:537-551).

Tests:

- Existing: `tests/test_hub_models.py`, `tests/test_ai_hub_metadata.py`, `tests/test_ai_supervisor_hub_metadata_refresh.py`, `tests/test_ai_supervisor_hardware_refresh.py`, `tests/test_ai_tasks.py`. Find the autouse no-egress fixture in `tests/conftest.py` (grep `urlopen`/`httpx` patching) and keep every new test under it.
- Suite: `.venv/bin/python -m pytest -n auto -q` from the worktree. Run only touched test files while iterating.

Decisions:

- Log every decision as a new D-number in `DECISIONS.md` (grep the highest existing number first; main may have taken more since). Note in the log where this spec was wrong.

## Builder notes (resume from here)

Progress as of this session (see git log on `worktree-hub-catalog` for commits):

1. **Done**: `image-to-image` → `IMAGE_GENERATION` in `fused_render/ai/tasks.py`,
   plus the `hub_cache.py` format-override gate fix and the cascading test
   updates. Logged as D1235. `tests/test_ai_tasks.py`,
   `tests/test_ai_models_api.py`, `tests/test_hub_models.py` all green;
   confirmed no other cascades via `pytest tests/ -k "ai_ or hub_ or task" -n auto`
   (3 unrelated pre-existing flaky failures: `test_ai_metrics.py`'s claude-binary
   test and two `test_ai_worker_base.py` socket-framing tests — reproduce with
   no hub_catalog changes present, unrelated to this feature, do not touch).

2. **Done this session**: `fused_render/ai/hub_catalog_config.py` +
   `fused_render/ai/hub_catalog.py` — the store primitives. `HubCatalogConfig`
   (dir = `storage.home_dir()/hub_catalog`, `pools_dir`/`metadata_dir`/
   `manifest_json`), `store_lock` (OS flock/msvcrt over an open fd, ported
   pattern not code from `index/store.py`), `read_manifest`/`pool_entry`/
   `pool_exists`/`is_blocked`/`set_blocked_until`/`write_pool`/`query_pool`/
   `delete_catalog`. Row shape: a few DuckDB-filterable columns (id,
   capability, format, downloads, likes, lastModified, createdAt,
   libraryName, gated, private) + the WHOLE raw Hub row as a `raw` JSON
   string column, so `_model_row` (unchanged) does the real parsing at query
   time instead of a second schema shadowing `_EXPAND`. `write_pool` bumps
   the generation, writes the manifest last, then reclaims the previous
   generation's file. `query_pool(cfg, capability, where=...)` opens a fresh
   DuckDB connection, runs an optional WHERE over the indexed columns, and
   returns the matching rows' `raw` dicts already `json.loads`'d.
   `set_blocked_until`/`is_blocked` share the manifest entry with the pool
   file, settable even before a pool exists. Logged as D1236. Tests:
   `tests/test_ai_hub_catalog.py`, 9 passing.

3. **Done this session**: `fused_render/ai/hub_catalog_builder.py` — the
   bulk builder. Pages `(tag, format)` pairs via `httpx.Response.links["next"]`,
   own `httpx.get` calls (not `hub_models._get`/`_fetch`, which drop the
   `Link`/`RateLimit` headers this needs). `build_capability_pool(cfg, capability)`
   is the synchronous full build; `ensure_build_started(capability, cfg=None)`
   is the non-blocking entry point for the search route (module-level
   `_building` thread-registry, skips if a pool exists or a backoff is
   active). 429 → `hub_catalog.set_blocked_until`, called AFTER `write_pool`
   (write_pool always resets `blockedUntil` to None — order matters, see
   D1237 for the bug this caught). Logged as D1237. Tests:
   `tests/test_ai_hub_catalog_builder.py`, 9 passing; re-ran
   `tests/test_hub_models.py` (245 total) to confirm zero interaction with
   the live path so far — it isn't wired in yet.

4. **Done**: `api_hub_search` (hub_models.py) branches early on
   `hub_catalog.pool_exists(capability)`: catalog path runs the D780
   scoring/filters/ILIKE/facets/family-pull-in over the whole pool via
   `hub_catalog.query_pool` (fresh DuckDB connection per query), zero Hub
   requests; else the live path is unchanged, plus a fire-and-forget
   `hub_catalog_builder.ensure_build_started(capability)`. Logged as D1238.
   Tests: `tests/test_hub_models_catalog.py` (new), `tests/test_hub_models.py`
   re-run to confirm the live path is untouched when no pool exists.

5. **Done**: `start_hub_catalog_refresh` in `fused_render/ai/supervisor.py`,
   modeled on `start_hardware_refresh`/`start_hub_metadata_refresh`: a daily
   tick walks the manifest, and for each capability with a BUILT pool that
   isn't currently blocked and hasn't been refreshed in the last 24h, calls
   `hub_catalog_builder.refresh_capability_pool_delta`. One
   `try`/`except Exception` per capability so one failure can't kill the
   sweep or the loop. Wired into `fused_render/server/app.py` as a third
   `@on_startup` hook beside the hardware/hub-metadata ones. Logged as
   D1239. Tests: `tests/test_ai_supervisor_hub_catalog_refresh.py` (new, 7
   tests, drives `_hub_catalog_refresh_tick()` directly plus thread-start
   idempotency), `tests/test_app_lifespan.py` updated for the new startup
   handler in `EXPECTED_STARTUP`.

6. **Done**: `hub_metadata.py`'s store moved off `ai_hub_metadata.json` to a
   repo-keyed parquet table (`repos.parquet`) + append log
   (`repos_log.parquet`) in the catalog dir, written under
   `hub_catalog.store_lock`, read with a fresh DuckDB connection each time.
   `get()`/`cached()`'s public API and the `_fetch_raw` seam are unchanged;
   `get()`'s two write sites now go through a cheap log-append (`_upsert`)
   instead of a full-store rewrite, with the log folding into the table past
   `_LOG_COMPACT_THRESHOLD = 50` rows. `MAX_REPOS`/`_bounded` are dropped.
   New `invalidate(repo_id)` resets `fetchedAt` to `0.0` (reusing `get()`'s
   existing stale-fallback logic unchanged) and is called from
   `hub_catalog_builder.refresh_capability_pool_delta` for every repo id the
   delta actually saw move, so a built pool's repos re-harvest on
   `lastModified` change instead of waiting out the 13-day TTL; repos in no
   pool keep the TTL as a fallback. A one-time import folds the old JSON
   file in if present (double-checked-locking, renamed to `.imported`
   after). Logged as D1240. Tests: `tests/test_ai_hub_metadata.py` (7 new
   tests alongside the 23 pre-existing, all passing unmodified against the
   new backend since `_load()`/`_write()` preserve the old dict shape),
   `tests/test_ai_hub_catalog_builder.py` (4 new tests covering the delta →
   invalidate hook).

   **Deviation from this doc's wording** (see DECISIONS.md D1240 for full
   reasoning): "write the shallow config fields the bulk list returns into
   pool rows and let `cached()` answer from those" was scoped OUT.
   `cached(repo_id)` has no capability/pool context to know which pool row
   to consult, so building a safe repo-id → capability reverse index was
   judged too large/risky for the remaining budget. `cached()` still always
   goes through `hub_metadata`'s own store as before; this is a missed
   optimization, not a correctness gap.

A fresh builder resuming this doc would find items 3-6 (spec section
headings) all committed as of D1240; nothing is left "not started." The
combined targeted-test run at the end of this build (`test_ai_hub_metadata.py
test_ai_supervisor_hub_metadata_refresh.py test_ai_hub_catalog_builder.py
test_ai_supervisor_hub_catalog_refresh.py test_ai_hub_catalog.py
test_hub_models.py test_hub_models_catalog.py test_app_lifespan.py
test_ai_supervisor_hardware_refresh.py`) passed 289/289.
