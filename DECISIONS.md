# Decisions log — hub-search-discovery build

Kept live while building the plan in hub-search-discovery-plan.html. A different
builder picking this up should read this before touching code.

## Task 1 — fit, speed, created on every Hub row

- `_model_row` gains two new positional params: `footprint_store` and `hardware`,
  appended after `dirs` (so `_model_row(raw, cache_dir, dirs, footprint_store, hardware)`).
  Both threaded from `api_hub_search`'s one-per-request read, matching
  `ai_runtime.py:906-923`'s pattern exactly.
- `fit.verdict(capability, model_id, size_gb, ...)` is called with `size_gb` derived
  from `_estimated_bytes(safetensors) / fit.GB_BYTES` — NOT from `params` +
  `quantization`. Reasoning: `_estimated_bytes` already computes a real per-dtype
  byte total off `safetensors.parameters`, which is strictly better evidence than
  the `params * bytes-per-param` guess `fit._weight_bytes` falls back to when no
  `quantization` string is recognized (and Hub search rows never carry a
  `quantization` display string — that is a curated/catalog-only field). So:
  `quantization=None` is passed always for Hub rows, `params=_params(safetensors)`
  is still passed (a raw int, which `fit.parse_params` accepts directly), and
  `size_gb` carries the real safetensors-derived total. This means `_weight_bytes`
  takes the `valid_size_gb` branch (recognized=False, valid_size_gb is not None).
  A row with neither params nor safetensors size gets `size_gb=None, params=None`
  and `fit.verdict` returns `None` (its own contract for "nothing to guess from").
- `speedEstimate` is computed only for `capability == registry.TEXT_GENERATION`,
  via `speed.estimate_tok_s(size_gb=..., params=..., quantization=None, hardware=hardware)`.
  Absent (`None`) for every other capability, per the plan's own row-shape test.
- `created` is `raw.get("createdAt")` if a non-empty string, else `None` — mirrors
  the `updated`/`lastModified` field's existing absent-is-null pattern.
- Both `footprints.load_store()` and `hw_detect.cached_hardware()` are read exactly
  once per `api_hub_search` call, before the row comprehension — never inside
  `_model_row` itself, and never per-row.

## Task 3 — one entry per model family

- `_EXPAND` already contained `"tags"` before this task started (it was added
  in task 1 or earlier, not by me) — the plan's file list says to add it, but
  it was a no-op; nothing changed there.
- `_base_model(tags)` uses `str.partition(":")` on the tag with its
  `base_model:` prefix stripped, taking the FIRST matching tag when more than
  one base_model tag exists (undocumented by the Hub, so "first wins" is a
  choice, not a spec). A malformed tag (missing the second colon, or an empty
  relation/id either side of it) is skipped rather than partially parsed.
- `hubFamilies.ts`'s `groupIntoFamilies` keys a family on `baseModel ?? id`.
  A variant whose named base model never appears among the SAME page of
  results (dropped upstream by D313, or just outside the query match) still
  groups under that base id rather than standing alone under its own —
  covered by its own test (`"a variant whose base model never appeared..."`).
  Primary selection is fit-score desc, then downloads desc, then the
  server's own ranking as the final tie-break (relies on `Array.sort`'s
  ES2019+ stability guarantee, not manual index bookkeeping).
- Family ORDER (the array `groupIntoFamilies` returns) follows first
  appearance of each family's key in the input — i.e., whichever member
  (primary or variant) the server ranked first decides where the whole
  family sits. This wasn't specified by the plan; I chose it because it's
  the only rule that makes the server's sort (by fit/trending/downloads/etc)
  visibly survive grouping rather than being silently redone.

## Task 2 — rank by fit, trending, hide what cannot run

- `sort="fit"` is accepted by `api_hub_search` but deliberately NOT a key in
  `_SORTS` — there is no Hub wire field for it. It resolves to the same
  `("downloads", -1)` tuple `_SORTS["downloads"]` uses for the actual Hub
  request, then the route re-sorts `models` by `fit.score` (descending, `None`
  treated as -1.0 so nulls sort last, Python's stable sort keeping the Hub's
  own ranking as the tie-break) AFTER the per-row join and BEFORE `[:count]`.
- The client-facing `HubSort`/`ResultSort` types needed NO special-casing for
  "fit"/"trending" beyond adding them to the union: both are values the page's
  OWN server accepts directly (unlike the frontend-only "size"), so `wireSort`
  passes them straight through as identity, exactly like "downloads"/"likes"/
  etc. Only "size" ever gets rewritten by `wireSort`.
- **Deviation from the plan's literal cache-key instruction.** The plan's task
  2 file list says the verdict-filter and the fit-sort "join the cache key
  alongside `extra_tags`, for the reason given there." I did NOT add
  `include_unfit` (nor a `sort=="fit"` distinction beyond the existing `sort`
  key, which was already part of the tuple) to `_cache`'s key. Reasoning:
  `extra_tags` earns its place in the key because it changes the WIRE request
  sent to the Hub (`params["filter"]`), so two different values are genuinely
  two different Hub answers that must not share a cache slot. The unfit filter
  and the fit-score reorder both run in the ALREADY-uncached per-request join
  section (same section as `_local_state`/`fit.verdict`/`speed.estimate_tok_s`),
  which re-executes on every request regardless of the cache — so two searches
  differing only in `includeUnfit` correctly (and harmlessly) reuse the same
  cached raw Hub rows, and adding the flag to the key would only cost an extra,
  needless Hub round trip. `sort` was already part of the key before this task
  (unchanged).
- The "show models that will not fit" toggle's wire name is `includeUnfit`
  (camelCase, matching the JS/JSON convention used everywhere else on this
  reply's own fields — `estimatedSize`, `speedEstimate` — even though the
  Python body previously only had single lowercase words like `q`/`task`/
  `sort`/`limit`). The response echoes it back as `hiddenUnfit: number` — the
  count of `verdict: "no"` rows dropped (0 whenever `includeUnfit` is true, or
  whenever nothing needed hiding) so the default filter is never silent
  (mirrors D316's "never a silent drop" rule already applied to gated repos).
- `SORT_ICONS`: "Fit" reuses `MenuIcons.info`, "Trending" reuses
  `MenuIcons.share` — no glyph in the shared set reads literally as either
  concept, so these are the closest defensible reuses (info = "a judgement
  about this machine worth a second look"; share = "being passed around right
  now"). Flagged here in case a reviewer wants a better pairing.
- The toggle resets to `false` in `clearSearch` (LocalTab) — unlike `sort`,
  which the same function's own comment says is deliberately left alone. This
  is a plan-text-driven choice ("the toggle's state... joins the one-act
  clearSearch") rather than something I inferred independently; worth a second
  look if the intent was actually "leave it alone like sort".
