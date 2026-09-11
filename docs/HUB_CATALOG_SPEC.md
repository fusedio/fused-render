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
