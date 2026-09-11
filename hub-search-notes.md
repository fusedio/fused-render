# Decisions log — hub-search-discovery build

Kept live while building the plan in hub-search-discovery-plan.html. A different
builder picking this up should read this before touching code.

NOTE: this file used to be at `DECISIONS.md`, which was wrong — that filename
is the project's own decision record (1452 lines on `main`, referenced by
D-number throughout the source) and the previous builder overwrote it with
this file's content in commit `7a7743c7`. Fixed by: `git checkout origin/main
-- DECISIONS.md` to restore the real file, and moving this content here
(`hub-search-notes.md`) instead. Any new decisions worth a permanent D-numbered
entry in the real `DECISIONS.md` are appended there directly, not here.

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

## Task 4 — the dense results table

- **New pure module `hubTableView.ts`** exports one function per cell rule
  (`ageLabel`, `fitCell`, `speedLabel`, `runModeLabel`, `popLabel`,
  `variantLabel`, `familyDisplay`, `paramsLabel`) rather than one aggregating
  `tableRow()` function — I wrote an aggregator first, then removed it: it
  wasn't tested on its own (the plan only asked to "drive every cell rule"),
  and `HubResultsTable.tsx` reads each cell straight from the primary
  model, so the aggregator only added an untested indirection.
- **Byte formatting**: the plan says "reuses `shared/modelSize.ts`", but that
  module's exports (`modelSizeLabel`, `modelSizeHint`) are catalog/job-hybrid
  helpers (advertised `size_gb` vs. a live download's total) that do not apply
  to a Hub search row at all — a Hub result already has its own size story
  (`hubSize.ts`'s `hubSizeLabel`/`hubSizeTitle`/`hubSizeBytes`, task-1/2/3's
  `estimatedSize` + the lazy `usedStorage` fallback), which is what the Size
  column actually reuses. What `shared/modelSize.ts` really share is
  `@platform/lib/format`'s `formatSize` underneath — `hubTableView.ts` reuses
  THAT (by way of `formatParams`/`timeAgo`, also from `format.ts`) rather than
  writing a second byte/param formatter, which I believe is the substance of
  what the plan's line was after.
- **Family display rule** (`familyDisplay`): the row's bold name is
  `primary.baseModel ?? primary.id`; the muted sub-line is `primary.id`
  ONLY when it differs from the bold name — i.e., a standalone repo (no
  `baseModel`) shows one line, never its own id twice. This is a design
  choice I made reading the mock's own caption ("The second line... is the
  variant actually being offered") rather than something spelled out as a
  rule anywhere in the plan text itself.
- **`SwitchEngines` was exported** out of `RecommendedCard.tsx` (was a
  private function) so `HubResultsTable.tsx` could reuse it rather than
  forking a second copy of the "why Download is dead" amber link.
- **Existing disk-state washes reused directly** (`am-card-have`,
  `am-card-part-unknown`, `am-card-arriving` — the same classes
  `RecommendedCard`/`RepoCard` use), rather than new `am-hubtable-*`
  equivalents — same D436 argument the plan itself makes for the table
  overall, applied one level down to the row wash specifically.
- **Column-drop thresholds are container-query breakpoints on the table's own
  inline size** (`container-type: inline-size` on `.am-hubtable-wrap`), not
  the viewport — a split pane narrows the same columns a full-width window
  would. The five pixel thresholds (760/660/560/460/380) are my own numbers,
  chosen to be comfortably wider than each column's own content plus the ones
  still visible at that point; they are not measured against a real rendered
  table (no browser available to this builder) and are the one thing in this
  task most worth a visual check before merge.
- **Frontend test-text hazard (repo-instructed check) found ONE real hit**:
  `frontend/src/apps/ai_models/local/repoCardControls.test.ts` greps
  `RecommendedCard.tsx`'s source text and asserted counts (`2` occurrences of
  `<DownloadGlyph />`, `<InfoButton name={model.id}`, `slug={model.id}`, plus
  a whole "every card on the page has the same bones" describe block) that
  assumed HubResultCard still lived in that file. Fixed by: lowering the
  RecommendedCard-only counts to `1`, adding an equivalent count check against
  the new `HubResultsTable.tsx`, and splitting the "same bones" describe block
  into one for `RecommendedCard` (still a card) and a new one for the table
  row (a different shape by design — the whole point of task 4). No
  `tests/` (Python) file referenced any of the deleted symbols/classes
  (`HubResultCard`, `cc-mdgrid`, `am-grid`) — confirmed by grep before
  finishing this task.
- **`.am-grid`** (the deleted grid's own CSS) had no other consumer anywhere
  in the frontend (confirmed by grep) — removed rather than left dead, with a
  one-line pointer comment left in its place.
- **Not implemented, and worth flagging for the reviewer**: I did not verify
  this table visually in a running app (no browser/dev-server access from
  this builder, and starting one is out of scope per this build's own
  hazards). Typecheck, the full `bun test` run, and `python -m pytest
  tests/test_hub_models.py` are all green, and `bun run build` succeeds, but
  the column-hiding container queries, the fit bar's rendered width, and the
  overall row density are unverified by eye. Recommend an actual look at
  "search qwen" and a narrowed split pane before merge, per the plan's own
  "how we'll know it works" section.

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

---

## Fix-builder notes (this session)

Context: code review + full suite run on PR #953 produced a fix list. Item 0
was restoring `DECISIONS.md` (see note at top of this file). Notes on the
remaining fixes are appended below as they land.

- **D-numbered entries added: D772, D773, D774** (in the restored
  `DECISIONS.md`, appended at the end, dated 2026-09-02). These cover the
  three real product decisions this feature made that the repo's own
  convention treats as decision-log material — a server-side `sort=fit`
  reorder rather than a client-side one, hiding `verdict:"no"` by default
  with the on-disk bypass this fix session added, and the base_model family
  grouping including the position/naming fixes from findings E and F. I
  judged these warranted entries because they match the granularity of
  existing entries (a feature-level design choice with genuine rejected
  alternatives), not just an implementation detail — the repo's own
  `DECISIONS.md` intro says the file is meant to let "a fresh session ...
  continue the project from these three files alone", and none of these three
  choices are otherwise written down anywhere a fresh session would find them
  (the plan HTML predates the review fixes, and this notes file is explicitly
  NOT the decision log per the note restoring `DECISIONS.md`).
- Flake investigation (item 1): both suspected flakes verified as
  PRE-EXISTING and NOT caused by this diff — neither file is touched by
  `origin/main..HEAD`.
  - `test_ai_worker_base.py::test_a_refused_body_this_cannot_frame_ends_the_connection`:
    passed 5/5 in isolation, but running the whole file under `-n auto` (and
    even serially) reliably fails ONE of the three parametrized cases each
    run — a different one each time (`chunked`, `empty-transfer-encoding`,
    `transfer-encoding-and-content-length`) — with a bare `ConnectionResetError`
    at line 167. This is a genuine socket-level race in the test's own harness
    (client/server timing), not a parametrized-ordering artifact as
    hypothesized — left alone.
  - `test_fs_raw_bearer_proxy.py::test_bearer_read_proxies_bytes_with_auth_header`:
    passed 5/5 in isolation and 5/5 more under `-n auto` (both the single test
    and the whole file). Could not reproduce a failure at all in this
    session — left alone as an unreproduced flake, likely resource
    contention from running alongside the rest of a loaded full-suite pass.
- Server fixes (A, B, C, F) landed in one commit on `hub_models.py` +
  `test_hub_models.py`; frontend fixes (D, E, G) in one commit across
  `hubFamilies.ts`, `hubTableView.ts`, `HubResults.tsx`, `HubResultsTable.tsx`
  and `ai-models.css`. See those commits' own messages for the substance;
  the one below is what took real judgment.
- **Finding E (the "one root cause" family-row conflation) — scoped
  decision.** Rather than three point-fixes, this touches the shared root:
  `groupIntoFamilies` now positions a family at its PRIMARY's index in the
  input (not first-appearance) so a size/downloads sort survives grouping
  with no special-casing per sort; `familyDisplay` now names the row by the
  primary's own id (matching href/download) with the base model demoted to
  a muted "from …" line; and "N variants" is now a real toggle disclosing
  each sibling's own id/size/disk-state with its own Download/Cancel, via a
  new `HubVariantRow`. The variant row deliberately does NOT do the lazy
  IntersectionObserver total-size lookup the primary row does — that lookup
  is scoped to a row always on screen, and a closed disclosure paying Hub
  round trips for siblings nobody has opened yet would be the same
  over-eagerness the viewport gate exists to prevent. This is UNVERIFIED
  visually (no browser access from this session, same limitation the
  previous builder flagged for the table overall) — worth a look at a real
  fit-tie case (e.g. a 4bit/8bit pair) before merge.
- One frontend text-hazard test needed updating for finding E:
  `repoCardControls.test.ts`'s "leads every Download with the same glyph"
  counted exactly one `<DownloadGlyph />` in `HubResultsTable.tsx`; there are
  now two (the family row's own, and the new `HubVariantRow`'s) — count
  bumped to 2 with a comment explaining why.

## Fix builder: cache-key/fetch-size defect (review finding)

- Bug: `fetch` (line ~808) depends on `include_unfit` (and `task_filter`),
  but the cache `key` (line ~841, pre-fix) did not include either `fetch`
  or `include_unfit`. Toggling `includeUnfit` off within the 90s TTL for
  the same query/task/sort/count reused the smaller `includeUnfit=True`
  payload (`fetch = count`), leaving no overfetch buffer for the
  verdict:"no" drop to backfill from — the exact under-fetch the `fetch`
  change was written to prevent, reintroduced through the cache.
- Fix: added `fetch` itself to the cache key tuple (not `include_unfit`
  separately — `fetch` is the actual payload-size determinant and already
  folds in `task_filter` too), and extended the comment above `key` in the
  same voice as the existing `extra_tags` justification.
- TDD: added
  `test_unchecking_include_unfit_inside_the_window_does_not_reuse_the_smaller_fetch`
  to `tests/test_hub_models.py` — sends task filter + `includeUnfit=True`,
  then the same request with `includeUnfit=False`, and asserts (a) a
  second live Hub request actually happens (`len(fake.calls) == 2`, i.e.
  not a stale cache hit) and (b) the two requests' `limit` query params are
  24 and 96 respectively. Confirmed it fails against the pre-fix code
  (`1 == 2`) and passes after the fix. Full `tests/test_hub_models.py`:
  111 passed.
- Checked for the same staleness elsewhere: `count`, `query`, `task_filter`,
  `sort`, `_token()` presence, and `extra_tags` were already all in the key
  and none of them changed meaning after the original `fetch` change — only
  `include_unfit` was newly load-bearing for `fetch` and missing from the
  key. No other field needed adding.
- Checked the `sort == "fit"` vs `sort == "downloads"` question: both map
  to the same `sort_field`/`direction` (`_SORTS["downloads"]`, since `"fit"`
  is not a key in `_SORTS` and falls through to the `else` branch), so a
  `sort="fit"` request and a `sort="downloads"` request with otherwise
  identical params send the Hub an IDENTICAL query. But the cache key
  stores the *requested* `sort` string, not `sort_field`, so `"fit"` and
  `"downloads"` get separate cache entries holding equivalent raw rows —
  this is redundant (one extra Hub-shaped cache slot, not a correctness
  bug) but not a collision: `sort == _FIT_SORT` triggers an additional
  post-join reorder-by-fit-score step (line ~885) that runs freshly on
  every request from the live `sort` variable, never from anything cached,
  so a `"fit"` request is never served the wrong ordering because it hit a
  `"downloads"` cache entry or vice versa (they don't share entries at
  all). No second bug here.

## CI-red fix: two tests carried the host's RAM as an unstated premise

`test_a_row_with_params_carries_fit_speed_and_created` and
`test_size_is_recovered_from_the_dtype_map` both fixture an 8B-param BF16
row (16GB footprint) and then index `models[0]`. On the dev Mac (32GB) that
row fits and survives this branch's new hide-unfit-by-default filter; on a
CI runner (as little as 7GB) `fit.verdict` returns `verdict: "no"`, the
filter drops the only row, and `models[0]` raises `IndexError` — the
feature working as designed, the test carrying an assumption it never
stated.

Reproduced the exact mechanism directly against `fit.verdict`:
`machine_ram_gb=7.0` + no GPU + 16GB footprint -> `"no"`;
`machine_ram_gb=32.0` + no GPU + the same footprint -> `"easy"`.

Fix: added `_pin_hardware(monkeypatch, ram_gb=32.0)` (follows the existing
`fit.machine_ram_gb`/`hw_detect.cached_hardware` stubbing pattern already
used in `tests/test_ai_fit.py`, not a new one) and called it from those two
tests plus a third with the same latent defect:
`test_speed_estimate_is_absent_for_a_non_text_capability` (4B BF16 = 8GB,
also asserts `fit is not None` after the default filter, also one small
runner away from the identical `IndexError`).

Audited the rest of the file's `safetensors=`/size fixtures for the same
class of bug: `_fitted()`'s "clearly-no" (4000GB) and "clearly-easy" (1GB)
fixtures are deliberately extreme (per its own docstring) and stay
host-independent on any real machine — left those alone. No other
safetensors fixture in the file sits in the ambiguous middle.

Confirmed not vacuous: `test_a_verdict_no_row_is_absent_by_default_and_present_with_the_opt_in_flag`
and `test_limit_still_counts_rows_actually_returned_after_the_fit_filter`
still exercise the real `verdict: "no"` drop (via `_fitted`'s 4000GB
fixture, unpinned) and both still pass — the unfit filter is still under
test, not merely dodged.

`tests/test_hub_models.py -q`: 111 passed.

---

## Resumed-builder notes (2026-09-01/02) — Parts A-D of the review's follow-up brief

Context: the previous builder died mid-edit from a network error with
~920 lines of UNCOMMITTED but good work spanning the server-side Part B
filters/quant field, the frontend `hubSize.ts`/`hubTableView.ts` fixes and
tests, and the start of `HubResultsTable.tsx`'s fit/speed fallback wiring.
That work is now committed (see the commit log below); this section covers
what was finished on top of it.

### What was already done (verified, not redone)

- `_quant()`, the three server-side search filters (`fitLevel`, `quant`,
  `paramsBand`) plus `publisher`, `_fetch_file_size`, and `api_hub_size`'s
  `file`/`capability` extension — all in `hub_models.py`, all with passing
  tests in `tests/test_hub_models.py` (144 passed at the time of resume).
- `hubSize.ts`'s `knownFit`/`knownSpeedEstimate`/the `file`-aware
  `lookupTotalSize` (fixes the ≈1.4 TB GGUF-repo-total bug), and
  `hubTableView.ts`'s banded `speedLabel`/new `scoreLabel`/`quantLabel` —
  all with passing tests already written by the dead builder.
- `api.ts`'s `HubModel.file`/`HubModel.quant`, `HubFitLevel`/`HubParamsBand`,
  and `getHubModelSize`'s `file`/`capability` params.

Committed as-is in `b971d27b` (server) with no functional changes.

### Part A — finished the table wiring

`HubResultsTable.tsx` had `fitOverride`/`speedOverride`/`effectiveFit`/
`effectiveSpeed` state already wired by the dead builder, but the row's own
cells (`fitCell(model.fit)`, `speedLabel(model.speedEstimate)`,
`runModeLabel(model.fit?.runMode)`) still read the raw search-reply values
instead of the effective ones — exactly the bug report (a row shows a real
size but `—` for Fit/tok-s/Mode). Fixed by switching those three call sites
to `effectiveFit`/`effectiveSpeed`. The file also had two dangling imports
(`getHubModelSize` from `@platform/lib/api`, `knownFit`/`knownSpeedEstimate`
from `hubSize.ts`) that were used but never imported — a genuine build
break the dead builder's own last edit introduced; typecheck caught both
immediately.

Added three columns — Score, Quant, Capability — to header, primary row and
variant row in lockstep:

- Order: Fit, **Score**, Model, Task, **Capability**, Params, **Quant**,
  Size, tok/s, Mode, Pop., New, Var., action. Score sits next to Fit (its
  numeric echo); Capability next to Task (the internal runner key vs. the
  friendly label); Quant next to Params (both are "how much/what kind of
  weight" facts).
- Header: 14 `<th>` (was 11). Primary row: 14 `<td>` (was 11), no colSpan.
  Variant row: the name cell's `colSpan` went from `2` (covering
  Model+Task) to `3` (covering Model+Task+Capability) — the variant's own
  id stands in for all three, same as before — plus a new `td` each for the
  empty Score slot and for Quant (via `quantLabel(model.quant)`). Total
  variant-row `td` count: 12, colSpan-weighted to 14 to match the header.
- Column-drop ladder (container-query, table's own inline size, never
  viewport): added Capability at 900px (widest — it echoes Task, the least
  new information of the three), Score at 600px (a numeric echo of the Fit
  bar, which never leaves the row), Quant at 500px (kept longer than Score
  because it's often the more decisive fact at that width). Fit, name, Size
  and the action column still never drop, per the existing rule.
- `hubFamilies.test.ts`'s `model()` fixture didn't have `file`/`quant` keys
  and `HubResults.tsx`'s own `lookupTotalSize(id)` call (the page-level size
  SORT's bulk lookup, not the per-row lazy one) was still on the OLD
  1-argument signature — both are call sites the dead builder's own
  signature change should have touched and didn't. Fixed; both were real
  typecheck failures, not hypothetical ones.

`npm run typecheck`: clean. `bun test` on every touched `.test.ts` (+
`repoCardControls.test.ts`, which greps `HubResultsTable.tsx`'s source):
all green, no counts needed touching (columns changed, `<DownloadGlyph />`
count did not).

### Part B — filter UI, then a scope correction

First pass put all four facets (fit level, quant, params band, publisher)
into `SearchControls.tsx`, visible on both faces of the Local tab —
`FIT_LEVELS`/`PARAMS_BANDS`/`activeFitLevel`/`activeParamsBand` were added
to `hubSearchView.ts` for this and are still there, since the vocabulary is
correct regardless of where the chrome renders.

**Correction (mid-task, from the orchestrator):** the four facets must
render ONLY while a search is active, never on the idle "your models" face
— filtering a handful of already-downloaded, deliberately-chosen models by
publisher or quant does nothing, and having the controls live on both faces
made "which grid does this filter apply to?" genuinely ambiguous. Fixed by
moving the chrome into a new `HubFilterBar` inside `HubResults.tsx` (mounted
only while the search face is on screen — `HubResults` itself is
conditionally rendered by `LocalTab`, so this needed no extra gating logic
of its own) and reverting `SearchControls.tsx` to only query/task/sort/
includeUnfit, its original scope. `ControlMenu` is now `export`ed out of
`SearchControls.tsx` so `HubFilterBar` can reuse it rather than a third
hand-rolled dropdown. See D776.

### Part C — URL params

`readHubUrl()` in `LocalTab.tsx` seeds all eight pieces of state (both the
live controls AND the initial `settled` object) from `location.search` on
mount, using the app's existing `readParam`/`writeParams` convention
(`params.ts`, `BenchmarkTab.tsx`'s `benchCap`/`benchMetric`/`benchModel`).
Names: `hubQ`, `hubSort`, `hubTask`, `hubFit`, `hubQuant`, `hubParams`,
`hubOrg`, `hubUnfit` — every one prefixed, per the brief's explicit
"do NOT use bare `q`/`sort`/`model`" instruction (`?model=` already means
the Playground's own seed, page-wide, and colliding with it is a known,
deliberately-unfixed live bug — see the memory note on `_side=claude`
leaking `model=`).

Two requirements needed real design, not just wiring:

- **Write on settle, not per keystroke.** The `writeParams` call is in a
  `useEffect` keyed on `settled` (the debounced object), never on
  `query`/`task`/etc directly — a burst of typing is one `replaceState`
  after the 350ms debounce, not one per character.
- **A shared link must RUN the search, not just prefill the box.** Solved
  by seeding `settled`'s OWN initial `useState` from the same
  `readHubUrl()` read that seeds the live controls, rather than leaving
  `settled` at an empty default and waiting for the debounce effect to
  catch up. `searchChrome`/`face` already derive from `settled`, so this
  alone puts the page straight into the results face on the very first
  render when `?hubQ=` (or `?hubTask=`) is present.

**Then the orchestrator's scope correction added a third requirement**: a
facet param with no query must be INERT, not half-applied. `readHubUrl`
computes `asked = !!(q.trim() || task.trim())` and only reads
`hubFit`/`hubParams`/`hubQuant`/`hubOrg` off the URL when `asked` is true;
otherwise all four seed to their "any"/"" no-op default regardless of what
the URL says. This matters because `HubFilterBar` doesn't even mount on the
idle face — a value that snuck into state anyway would sit there inert
until the reader typed a query, at which point it would silently apply to
a search nobody asked to filter. The write side mirrors this: the
`writeParams` effect computes the same `asked` boolean off `settled` and
passes `null` for all four facet keys whenever it's false, so the URL never
advertises a filter that isn't in effect on whatever's actually on screen.

`activeSort`/`activeFitLevel`/`activeParamsBand` (already written for the
menu triggers) double as the URL value's VALIDATION for free: each already
falls back to its own first/no-op entry for a value it doesn't recognise,
which is exactly what a malformed or hand-edited param needs — no separate
`isValidSort`-type helper was written.

No dedicated test file exists for `LocalTab.tsx`'s URL wiring (there's no
DOM harness in this repo, per every other component-level doc in this
feature) — `BenchmarkTab.tsx`'s own `readParam`/`writeParams` usage is
similarly untested at the component level, so this follows that precedent
rather than inventing a new one.

### Part D — this file, and D775/D776

Highest D-number was D633 on this branch (D630 on origin/main at the time
of resume, since renumbered to D772-D774 after a later merge collided with
origin/main's own unrelated D631-D633; see the note at the end of this
file) — D775 and D776 added, covering the measured-only quant rule and
the filter server/client split + the facet-scoping correction. Both are
genuinely non-obvious design choices with real rejected alternatives, not
implementation detail — see `DECISIONS.md` directly rather than duplicating
the text here.

### Testing

- `.venv/bin/python -m pytest tests/test_hub_models.py -q`: 144 passed
  (unchanged by this session's frontend-only work).
- `.venv/bin/python -m pytest tests/test_doc_duplicate_ids.py -q`: 3 passed
  (after appending D775/D776).
- `cd frontend && npm run typecheck`: clean, twice (once after Part A/B's
  first pass, once after the scope correction + Part C).
- `cd frontend && bun test src/apps/ai_models` (every test file under the
  app, not just the touched ones, to catch any cross-file text-hazard or
  import fallout from moving `ControlMenu`): 587 passed, 0 failed, across
  22 files.
- `node scripts/check-boundaries.mjs`: clean (426 files) — checked because
  this session moved a component (`ControlMenu`) between files and added a
  new cross-file import (`HubResults.tsx` importing from
  `SearchControls.tsx`).
- Grepped `tests/` for every frontend source line touched
  (`HubResultsTable`, `am-hubtable`, `speedLabel`, `fitCell`,
  `hubTableView`, `hubSize.ts`, `SearchControls`, `LocalTab`, `HubResults`):
  no Python test asserts on any of them.

### Left for a human with a browser

Nothing in this session was visually verified — no DOM harness, no browser
access. The column-drop thresholds, the row density with three more
columns, the `HubFilterBar`'s placement/wrapping inside the results
section, and the URL round-trip (typing a query, reloading the tab, and
confirming the address bar and the results agree) are all unverified by
eye. See the parent report's own to-verify list.

## Fix builder (second session): F1-F7 from a live-browser review

A different builder confirmed F1's symptom directly in a running dev server
(searching `Llama-3.2-1B-Instruct-GGUF` showed every row with dashes for
Fit/Score/Params/Quant and repo-wide sizes) and F2's (`Qwen3-4B-4bit` showed
`U32`/`I32`/`U8`/`BF16` in the Quant column and understated Params for
packed 4-bit repos). Both are fixed this session — see D777/D778 in
`DECISIONS.md` for the substance. F3-F7 were caught by review/reasoning
rather than a live repro; each has its own new test (Python) or existing
scoped bun suite coverage (frontend) demonstrating the fixed behavior.

**Known limitation, deliberately NOT fixed this session (per the fix
brief's own scope line) — family grouping under-fills the page.**
`groupIntoFamilies` (`hubFamilies.ts`) runs CLIENT-side, on the results
`api_hub_search` already truncated to `limit` (D774's own row-positioning
fix does not change this). So a query whose top results mostly share one
`base_model` tag returns, say, 24 raw rows that fold into far fewer than 24
family rows on screen — the Hub's remaining candidates that could have
backfilled those folded-away slots were already discarded server-side by
the `[:count]` truncation, and nothing re-asks for more. The summary count
was fixed (an earlier finding) to count families rather than raw rows, so
the NUMBER shown is honest, but the grid itself is still under-filled
relative to what `limit` promises. This is the same shape of bug the
`includeUnfit`/`fetch` overfetch fix and Part 3's filters both had to solve
for their own drops (see D773/D776) — `api_hub_search`'s own overfetch
comments already treat "something drops rows after the Hub's answer but
before the page sees them" as requiring the SAME `fetch` multiplier
`includeUnfit` gets. Family grouping is the one drop that still runs
entirely on the client, after `limit` has already applied, so it cannot be
backfilled without either moving the grouping server-side (a bigger
change, since `hubFamilies.ts` has no Python counterpart today) or having
the client ask for more rows when it notices under-fill (which would turn
one search into a variable number of round trips depending on how clustered
the results happen to be). Recorded here rather than silently left alone,
per the fix brief's own instruction, because it is a real gap in what
`limit` promises — not a dismissed non-issue.

### This session's test/verification commands

- `.venv/bin/python -m pytest tests/test_hub_models.py -q`: 153 passed (144
  before this session's new tests; 9 added across F1-F3/F6).
- `bun test src/apps/ai_models` (frontend/): 588 passed, 0 failed.
- `npm run typecheck` (frontend/): clean.
- `node scripts/check-boundaries.mjs` (frontend/): clean (426 files).

### Left for a human with a browser (this session's additions)

- F1: confirm searching a GGUF repo (e.g. `Llama-3.2-1B-Instruct-GGUF`) now
  shows a per-file size and a populated Fit/Quant, not a repo-wide total
  with dashes.
- F2: confirm searching `Qwen3-4B-4bit`-shaped queries now shows `4-bit`
  (or similar) in Quant rather than `U32`/`I32`/`U8`, and a plausible full
  parameter count (not a ~4x-undercounted one) in Params.
- F4: confirm the variant disclosure rows stay column-aligned with the
  header/family row as the pane narrows through 900px and 600px (where
  `.am-col-cap`/`.am-col-score` drop).
- F5: confirm the table never clips Fit/Model/Size/the action column at any
  pane width — including via the new `overflow-x: auto` safety net on
  `.am-hubtable-wrap`, which should only ever engage in the gap between
  ladder steps, never at a width the ladder already covers cleanly — and
  confirm the PAGE itself still never scrolls sideways.
- F7: confirm `.am-hubtable-row:hover`'s new subtle background reads
  sensibly rather than looking like a false click affordance on a row that
  is not, as a whole, clickable.

## Second fix-builder round: GGUF rows still blank on this Mac (D779)

Confirmed live before touching anything (as instructed):
`registry.for_capability(registry.TEXT_GENERATION)` on this Apple Silicon
Mac returns `mlx-text` with `hub_filter_tags == ()`, while
`registry.available_runners(registry.TEXT_GENERATION)` (new) shows
`llamacpp-text` (`hub_filter_tags == ("gguf",)`) is also available, just
not preferred. So D412's GGUF-pick branch in `_model_row` never ran for a
GGUF-only repo — `file` stayed `None`, and everything downstream
(`quant`, `params`, `estimatedSize`, `fit`) went `None` with it, exactly
matching the reported table (dashes plus a whole-repo `usedStorage`
standing in for one file's size).

Fix: `registry.available_runners(capability)` (new, in `registry.py`) lists
every runner for a capability that is genuinely available here, not only
the preferred one. `_model_row` tries the preferred runner's GGUF pick
first (unchanged); only when the preferred runner declares NO format tag
at all does it fall through to the first available secondary runner that
does declare `gguf`, and try the same `pick_gguf_file` over the same
`siblings` — no new Hub request either way. D412's drop rule stays scoped
to the ACTIVE runner's own inability to resolve a pick; a secondary runner
finding nothing loadable does not drop the row.

Decision recorded as **D779** in `DECISIONS.md` (appended, dated
2026-09-02) — includes why a "downloads via a different engine" UI note
was investigated and NOT added: `POST /api/ai/runtime/download` resolves
strictly through `for_capability(capability)` with no per-model override
anywhere in `supervisor.py` (`_runner_or_raise`, `_fetch_only`), so a row
resolved via the secondary runner is still, today, downloaded by asking
the PREFERRED runner's own worker to fetch it — which for a GGUF-only
repo on this Mac means MLX's worker is asked to fetch a repo with no
safetensors, which will not produce a usable download. This is a REAL,
pre-existing gap in the download/load path (routing is per-capability,
never per-model or per-format), not something this round's display fix
introduces or worsens — Download on such a row was already going to hit
this whether or not the search table shows correct numbers first. Flagged
here rather than fixed: fixing it means giving `supervisor.load`/
`POST /api/ai/runtime/download` a way to target a specific runner for one
model, which touches the download architecture, not the search display —
out of scope for a round scoped to "the GGUF rows show no fit/quant/size".
Worth a follow-up round or an explicit product call on whether search
should also gate the Download button (or route it) once search can name
a resolvable-but-not-preferred repo, which it now can.

Tests: `tests/test_hub_models.py`'s autouse `_no_format_filter` fixture now
also pins `hub.available_runners` to `()` (previously only `for_capability`
was pinned) — needed because leaving it real would let this Mac's own
registry state (both `mlx-text` and `llamacpp-text` genuinely available)
decide format-filter-adjacent tests never written to exercise D779, the
exact machine-dependent trap this round was warned about. Four new tests
in `test_hub_models.py` exercise both arrangements explicitly (secondary
runner present and resolves; secondary runner present but finds nothing;
only the active runner available at all) plus three in `test_ai_registry.py`
for `available_runners` itself (present-and-ordered on Apple Silicon,
excluded on an Intel Mac per `_llamacpp_platform`'s own doc, empty for an
unserved capability).

## Fourth fix-builder round: ten code-review findings (2026-09-02)

Merged `origin/main` first, as its own commit, before any fix — the branch's
merge base (`09d1cdbc`) predates main's `a86c57e5`, which dropped
`test_shutdown_is_a_single_handler_reaping_every_background_pid`'s
`CORE_WATCH_PID` expectation after #945 removed the retired core_apps
watcher from `scripts/dev.sh`. That test was the one branch-only local
failure the brief described; merging main made it pass locally the same
way it already does on the CI merge ref. No conflicts.

Then all ten findings, MUST-FIX first. See D784-D791 in `DECISIONS.md` for
the full reasoning on each; this is the short version plus anything the
brief itself got wrong or that a future reader should know before touching
this code again.

**(1) GGUF rows scored on three defaults at once — fixed via a genuinely
free Hub field, `expand[]=gguf`.** The brief guessed the file's size might
be "knowable" from data already in hand; it is not (the LIST endpoint's
`siblings` carries only `rfilename`, confirmed by this module's own
`_EXPAND` comment, and a per-file size still costs the lazy client-side
`hub/size` round trip same as before). What IS free, and what the brief's
"params may be derivable" turned out to mean once checked live: the Hub's
`expand[]=gguf` returns `{"total": <param count>, ...}` for any repo
shipping a `.gguf`, and `total` is the SAME number across every
quantization of the same model (verified against three real repos at
Q4_K_M/Q8_0/mixed-quant — all three reported `1235814432`). That is real,
quantization-invariant evidence `fit._weight_bytes`/`speed.estimate_tok_s`
already know how to turn into a footprint estimate given a `quantization`
string (which `_quant` already resolves for a GGUF row, unused for this
purpose until now). D784 has the rejected alternatives — in particular,
do NOT re-derive params from a mixed-repo's own (nulled) safetensors map;
D778 already settled that one row must describe ONE upload consistently.

**(2) hubFamilies primary-selection is sort-aware, not "trust the array's
own order."** My first instinct was "the input array is already sorted by
whatever's active, so just take group[0] in array order" — this is WRONG
against an existing, deliberately-kept test (`hubFamilies.test.ts`'s
"picks the best-FITTING member as primary" case builds its fixture in an
order that does NOT match fit order, and still expects the fit-best member
to win for the default `sort="fit"` case). `groupIntoFamilies` now takes
an explicit `sort: ResultSort = "fit"` parameter and a `primaryComparator`
keyed on it — `byFitThenDownloads` only for `sort === "fit"`,
`byMatchThenDownloads` (new) for everything else including `"best"`.

**(3) Match cell staleness: chose "blank the score," not "recompute
client-side."** Recomputing would need a full or partial reimplementation
of `_composite_score`'s five-axis formula in TypeScript — a second copy to
keep in sync forever, and a partial reconstruction (subtract the default
fit contribution, add back the real one) silently assumes the OTHER four
axes were not ALSO defaulted, which was routinely true before fix (1) and
can still happen for the cases it doesn't cover. `matchCell`/`matchTitle`
both took a `stale` parameter instead (default `false`, every existing
call site untouched) — blank bar/dash number, real dot/colour, an honest
hover sentence. Fix (1) reduces how often `stale` is ever `true` but does
not eliminate it.

**(4) Hoist column presence now covers variants, hoist/summary still
doesn't.** `columnVisible`'s signature is unchanged; it now re-derives
unanimity from the actual `values` it's handed rather than trusting the
(primaries-only) `Hoist.unanimous` flag, and the call site passes the full
`families.flatMap(f => [f.primary, ...f.variants])` list. `hoistValue`
itself, feeding the summary line, stays primaries-only per D781's own
reasoning — this is a presence fix, not a summary fix.

**(5) CSS majority opacity → a real token, `--fg-faint`.** Computed actual
WCAG contrast ratios before picking values (not just eyeballing): the
pre-fix compound was ~2.32:1 light / ~2.95:1 dark, both under the 3:1
floor; `--fg-faint` lands at ~3.6:1 light / ~4.5:1 dark. Added to both
`:root` and `:root[data-theme="light"]` in `tokens.css`; `tests/
test_theme.py`'s palette-completeness/no-literal-colours checks both pass.

**(6)+(7) One `_speed_score` fix covers both.** Below
`_SPEED_ANCHOR_PARAMS` (1B, same value as the frontend's
`SPEED_ANCHOR_PARAMS`), `_speed_score` now returns `_SPEED_DEFAULT`
regardless of what `speedEstimate` says — matching what `speedLabel`
already refuses to print. `_SPEED_DEFAULT` itself dropped 70 → 63
(`_saturating(12, 12)`, i.e. AT the conversational anchor, never above
it). Checked every OTHER axis default against "can this ever outscore a
real, good measurement" and found no other violation — left `_FIT_DEFAULT`
/`_CAPABILITY_DEFAULT`/`_RECENCY_DEFAULT` unchanged, and did not touch
`_popularity_score`'s 0.0 floor (D780's own deliberate exception).

**(8) Sort key separated from the displayed number.** `_composite_raw_score`
(unclamped) is now what `sort=best` and the internal ranking compare on;
`_composite_score` (clamped `[0, 100]`, the wire `matchScore`) wraps it.
Kept the raw figures in a plain `dict[int, float]` keyed by `id(row)` for
the duration of one request rather than writing them onto the row dicts,
so nothing new leaks into the JSON reply.

**(9) Band border flipped from `border-bottom` on the ending row to
`border-top` on the starting one** — `banded={i > 0 && i % BAND_EVERY ===
0}`, was `(i + 1) % BAND_EVERY === 0`. No DOM harness in this repo, so
this one is unverified by eye; a human should confirm the 6th/11th/16th
family row (not the 5th/10th/15th) now carries the visible rule, and that
it still looks right whether or not a preceding family's disclosure is
open.

**(10) Dead code removed.** `runModeLabel` and `.am-hubtable-dash` — grepped
`tests/` for both (this repo's pytest tests can assert on literal frontend
source lines) before deleting; no hits outside `hubTableView.test.ts`
itself, whose own tests for `runModeLabel` are deleted with it.

### What I found the brief got wrong

- **"You already resolve a specific GGUF file for these rows, so its size
  is knowable"** — not from data already in hand. The LIST endpoint's
  `siblings` never carries a size (only `rfilename`); a per-file size is
  still exactly the lazy client-side round trip it already was. What the
  brief was gesturing at, and what turned out to actually be free, was
  `params` (via `expand[]=gguf`'s `total`), not size — I did not add a
  size estimate to the wire response at all, on purpose (see D784's
  rejected alternatives: an estimate written into `estimatedSize` would
  suppress the client's lazy lookup that resolves the REAL bytes).

### Tests run this session

- `.venv/bin/python -m pytest tests/test_hub_models.py -q`: 180 passed (175
  before this session; 5 net new — 3 for fix (1), 1 each for fix (7) and
  fix (8), plus 2 existing `_speed_score` tests updated to pass `params`).
- `.venv/bin/python -m pytest tests/test_doc_duplicate_ids.py -q`: 3 passed
  (after appending D784-D791).
- `.venv/bin/python -m pytest tests/test_theme.py -q`: 133 passed.
- `.venv/bin/python -m pytest tests/test_ai_fit.py tests/test_ai_speed.py
  -q`: 117 passed (checking `fit.verdict`/`speed.estimate_tok_s`'s new
  `quantization=` call pattern from `_model_row` didn't regress either
  module).
- `.venv/bin/python -m pytest tests/test_dev_sh_process_cleanup.py -q`: 28
  passed (confirms the merge fixed the one branch-only failure the brief
  named).
- `cd frontend && bun test` (full suite, per the mock.module-is-process-
  wide rule, since `hubFamilies.ts`'s signature changed): 3003 passed, 1
  failed — `appCardMenu.test.ts` / `appShot.ts`'s `window.addEventListener
  is not a function`. Confirmed pre-existing and unrelated: fails
  identically in isolation AND with this session's changes `git stash`ed
  back out, in a file this session never touched.
- `cd frontend && bun run typecheck`: clean.
- `cd frontend && node scripts/check-boundaries.mjs`: clean (426 files).
- Grepped `tests/` for every backend/frontend symbol touched this session
  (`runModeLabel`, `am-hubtable-dash`, `am-hubtable-majority`, `matchCell`,
  `matchTitle`, `groupIntoFamilies`, `columnVisible`, `byFitThenDownloads`,
  `BAND_EVERY`, `_speed_score`, `_SPEED_DEFAULT`, `_composite_score`,
  `_composite_raw_score`, `_EXPAND`): no hits outside `test_hub_models.py`
  itself.

### Left for a human with a browser

- Fix (5): confirm `--fg-faint` reads as "muted but legible" (not
  invisible, not full-strength) in both light and dark theme, on an actual
  hoisted-majority Quant/Capability cell.
- Fix (9): confirm the band border now separates FAMILIES correctly with a
  disclosure open — specifically, that expanding the 5th (or 10th, ...)
  family's variants no longer draws a line between that family and its own
  children.
- Fix (3): confirm a GGUF row whose fit corrects via the lazy lookup shows
  a real dot/colour with a blank bar/number, and that the hover text reads
  sensibly rather than confusingly terse.

## Merge-conflict note: D631-D633 collided with origin/main, renumbered to D772-D774

Merging `origin/main` (6ddc31d1) into this branch conflicted in
`DECISIONS.md`: main had independently used D631, D632, D633 for the
`fused.ai` namespace/verb rewrite (an unrelated feature) while this branch
had used the same three numbers for the Hub search decisions (server-side
`sort=fit` ordering, `verdict:"no"` hidden by default, and `base_model`
family grouping). Main's three keep their numbers as the authoritative
side; this branch's three were renumbered to **D772, D773, D774**
(preserving their original text and order, appended after this branch's
own D791) to clear `tests/test_doc_duplicate_ids.py`. Every in-repo
cross-reference to the branch's old D631/D632/D633 — in this file and in
`DECISIONS.md`'s own later entries (D777, D785) — was updated to the new
numbers; references to D631/D632/D633 elsewhere in the repo (`fused_render/
server/ai.py`, `common.py`, `ai_runtime.py`, the AI Playground frontend
files) are main's `fused.ai` decisions and were left untouched.

## Fifth fix-builder round: five more code review findings (three cascades from the fourth round's own fix)

D792-D794 in DECISIONS.md carry the full reasoning; this is what changed and
what the brief got wrong.

**(1) GGUF params feed under-reported memory fit by ~3.4x for unfittable
models (HIGH, a cascade from D772's own fix).** `hub_models.py`'s
`gguf_quantization`/`fit_params` are now set ONLY when `formats.gguf_quant_
token(file)` actually resolved a token — an unsuffixed or full-precision
GGUF (`model.gguf`, `...-F16.gguf`) goes back to `fit: null`/`speedEstimate:
null`, letting the client's lazy per-file lookup supply the real verdict.
`params` itself stays reported either way (the real, quantization-invariant
HUB total is still worth the Params column). Added a regression test pinned
at 16GB RAM specifically — 32GB (this suite's usual pin) can't distinguish
"wrongly under-reported 4.1GB guess" from "correctly estimated ~14GB" since
both read as comfortably fitting; 16GB is the smallest pin where the two
readings disagree.

**(2)/(3) Match cell precedence and staleness (MEDIUM each, one commit).**
`effectiveFit`/`effectiveSpeed` used `model.fit ?? fitOverride ?? null`,
which let a GGUF row's derived guess (recognized-quant, params x bytes-per-
param — real as of D772) permanently beat the lazy lookup's REAL measured
verdict once the guess existed, since `wantsTotal` stays true for every
GGUF row regardless of whether `model.fit` is null. Fixed the precedence
(`fitOverride !== undefined` wins) and, separately, fixed `matchScoreStale`
treating a lookup that resolved to `null` ("nothing to judge",
`knownFit`'s own pinned contract) as if it were a correction — it isn't,
and the old check blanked a perfectly valid server score for nothing.
Pulled all four rules (`resolveFit`, `resolveSpeed`, `matchFitBasis`,
`isMatchScoreStale`) out of the component into pure, tested functions in
`hubTableView.ts` — the brief asked for tests on logic that used to live
inline with no way to drive it, so this round extracted it first.

**(4) Hoist summary vs. presence contradiction, SECOND time reported
(MEDIUM).** The third round's fix for the blank-column bug made
`columnVisible` re-check primaries+variants while leaving the hoist/summary
on primaries only (D781's original call) — which reproduced the exact
"summary says one thing, column shows another" bug one level up. Pulled
BOTH computations into one new function, `familyHoist`, reading a single
`allCapabilityValues`/`allQuantValues` set for everything (hoist, summary,
presence) — a differing variant now downgrades "all BF16" to "mostly BF16"
at the same instant it keeps the column visible, by construction, since
there is only one computation left to disagree with itself. Also pure and
tested (three states: unanimous, majority-with-a-differing-variant,
all-unknown), same reasoning as (2)/(3).

**(5) hubFamilies ordering-invariant doc, LOW.** The claim that positioning
by primary index preserves order "for the SAME key `primaryComparator`
used to choose the primary" is only true for `fit`/`best`; for
`size`/`downloads`/`trending`/`new`, `models` is sorted by that key while
`primaryComparator` still returns `byMatchThenDownloads` — different keys.
Rewrote to state the real guarantee: placing a family at its primary's own
array index produces exactly `models`'s own order with non-primary rows
deleted, which preserves relative order unconditionally (deleting elements
never reorders survivors) — no dependency on how the primary was picked.
Doc-only, no behavior change.

### What this round's brief got wrong

Nothing substantive — the fifth-round brief's descriptions of all five
findings matched what was in the code. One thing worth flagging for a
future round: `isMatchScoreStale` still only fires when `model.fit` was
fully `null` (per the brief's literal instruction, `fitOverride != null &&
model.fit == null`). A GGUF row whose `model.fit` is a non-null D772 GUESS,
later corrected by a real measurement that differs from the guess, does
NOT get marked stale — the displayed `matchScore` was computed against the
guess's fit-axis contribution and is technically as outdated as the
null-default case, just not reported as such. The reviewer's finding named
only the resolves-to-null false positive, not this gap, so it was left
alone (see D793's own "Rejected" column) rather than silently widening
scope.

### Tests run this session

- `.venv/bin/python -m pytest tests/test_hub_models.py -q`: 181 passed (180
  before this session, 1 new — the unrecognized-quant regression test for
  finding 1, pinned at 16GB RAM).
- `.venv/bin/python -m pytest tests/test_ai_fit.py tests/test_ai_speed.py
  -q`: 117 passed (checking the `fit_params`/`gguf_quantization` call
  pattern change didn't regress either module).
- `.venv/bin/python -m pytest tests/test_doc_duplicate_ids.py -q`: 3 passed
  (after appending D792-D794).
- `cd frontend && bun test src/apps/ai_models`: 636 passed (was 3 pass
  short before this round's new `resolveFit`/`resolveSpeed`/
  `matchFitBasis`/`isMatchScoreStale`/`familyHoist` test blocks were added;
  no full `bun test` run — no `mock.module` touched this round, and the
  rule only requires the full suite when a mock is touched).
- `cd frontend && bun run typecheck`: clean.
- `cd frontend && node scripts/check-boundaries.mjs`: clean (426 files).
- Grepped `tests/` for every backend/frontend symbol touched this session
  (`gguf_quantization`, `fit_params`, `resolveFit`, `resolveSpeed`,
  `matchFitBasis`, `isMatchScoreStale`, `familyHoist`, `hoistValue`,
  `hoistSummary`, `columnVisible`, `effectiveFit`, `matchScoreStale`,
  `fitOverride`, `speedOverride`): no hits outside this repo's own frontend
  test files.

### Left for a human with a browser

- Finding (2)'s new `matchFitBasis` hover text ("This fit is an estimate
  from the parameter count and quantization alone…" / "…measured from the
  actual file…") has never been seen rendered — confirm it reads naturally
  beside the existing verdict/mode sentences rather than as a bolted-on
  fourth clause.
- Finding (4)'s "mostly" downgrade on a real result set with a
  quant-diverse variant — confirm the summary line and the Quant column
  visibly agree now, on an actual page rather than only in `familyHoist`'s
  own unit tests.

## Sixth fix-builder round: derived GGUF fit deleted, not guarded

D795-D798 in DECISIONS.md carry the full reasoning; this is what changed,
what the brief got wrong, and where I diverged from a literal reading of it.

**(1) The whole GGUF params x bytes-per-param derivation is gone.**
`_model_row` (`fused_render/server/routers/hub_models.py`) still reads
`gguf.total` into `params` for a `file`-resolved row (still real, still
quantization-invariant, still free, still feeds `_capability_score` and the
Params column) but never threads it into `fit.verdict`/`speed.estimate_tok_s`
any more. Both are now:

```python
fit_verdict = (
    fit.verdict(capability, model_id, size_gb, params=params,
                footprint_store=footprint_store, hardware=hardware)
    if file is None else None)
speed_estimate = (
    speed.estimate_tok_s(size_gb, params=params, hardware=hardware)
    if file is None and capability == TEXT_GENERATION else None)
```

i.e. gated on `file is None` (not a GGUF row) rather than on any property of
`quant`. This is unconditional the way the brief asked — no whitelist, no
"but this token is probably fine" branch. `gguf_quantization` and the
separate `fit_params` variable D792 introduced are deleted; there is exactly
one `params` now, used for both the Params column and (only for a
non-GGUF row) `fit`/`speedEstimate`.

**(2) Pinned the exact case that survived two prior rounds.** New test
`test_gguf_row_with_recognized_quant_still_reports_no_derived_fit`
(`tests/test_hub_models.py`) uses the reviewer's own repro: a 30B model
whose file is `x-Q8_K_XL.gguf` — `formats.gguf_quant_token` DOES resolve
`"Q8_K_XL"` (confirmed via the regex, not assumed), so this is not the
round-2 "unrecognized token" case at all — and asserts `fit is None` and
`speedEstimate is None` regardless. Also had to fix an existing test that
assumed the old behavior:
`test_gguf_row_with_real_params_scores_well_above_the_three_defaults_at_once`
asserted `with_meta["fit"] is not None` for a Q4_K_M row — renamed to
`test_gguf_row_with_real_params_scores_above_no_params_via_capability_alone`
and rewritten to assert both rows' `fit`/`speedEstimate` are `None`, with the
`matchScore` gap now attributed to `_capability_score` alone (real `params`
vs. none). Kept `test_gguf_row_with_unrecognized_quant_token_never_claims_
easy` (the round-2 pin) unmodified — it still passes unchanged, since
`fit: null` was already its assertion.

**(3) Axis defaults: verified numerically rather than changed.** The brief
worried deleting derived fit would "reintroduce a page where every GGUF row
ties near the bottom" and asked me to revisit the axis defaults. I did not
change `_FIT_DEFAULT`/`_SPEED_DEFAULT`/`_CAPABILITY_DEFAULT` — they already
implement "absence is neutral, not penal" (D780's own doctrine, re-verified
against code review finding 7 at the time: no default can ever outscore a
genuinely good real value). Ran the actual scoring functions rather than
hand-estimating: a same-params/recency/downloads pair at 7B params/32GB RAM
scores ~88.6 (safetensors, real "easy" fit) vs. ~62.4 (GGUF, fit/speed both
defaulted, capability real) — a real but moderate 26-point gap, not a
collapse to the bottom (`verdict: "no"` or CPU-penalized rows score far
lower). `_capability_score` is untouched by this round and keeps
differentiating GGUF rows from each other by real `params` even with no fit
verdict, so a 30B GGUF row still outranks a 1B GGUF row on capability alone.
I considered this the honest read of "make the fit axis's absence neutral
rather than penal for a row we simply have not measured yet" — those
constants already ARE that; a GGUF-specific default would have been a new
inconsistency (one row shape's "no evidence" reading differently from
every other row shape's), and the brief explicitly forbade inventing a fit
verdict to achieve this.

**(4) `resolveFit`/`resolveSpeed` gained a `file` parameter (real, previously
unreported defect).** The brief was right that this was live and unfixed:
`wantsTotal = !model.estimatedSize` in `HubResultsTable.tsx` is NOT gated on
whether `model.file` is set, so a row with `model.file === null` still runs
the lazy lookup (for the repo-wide `usedStorage` total). `api_hub_size`
(`hub_models.py`) only computes a fit/speed verdict when it receives a
`file`, so that lookup always answers `fit: null` — and `lookupTotalSize`
caches that `null` identically to a real "asked, nothing to judge" answer
(`hubSize.test.ts:295`'s own pinned contract). The old precedence
(`fitOverride !== undefined ? fitOverride : model.fit`) let that
never-judges `null` win outright, wiping a real `basis: "measured"` verdict
`footprint_store` had already supplied for an on-disk model at search time.
Fixed by adding a `file: string | null` parameter to both `resolveFit` and
`resolveSpeed`; the override only wins when `file !== null`. Updated the one
call site (`HubResultsTable.tsx`) to pass `model.file`, and rewrote the
`resolveFit`/`resolveSpeed` describe block in `hubTableView.test.ts` to pin
this directly (a `file === null` row's real `modelFit` survives both a
`null` and a differing-verdict override).

**(5) `matchFitBasis` simplified to a straight read of `AiFitVerdict.basis`.**
Old signature took `(effectiveFit, fitOverride, wantsTotal)` and re-derived
a fourth "estimated" state for the (now nonexistent) GGUF-guess case. New
signature is `matchFitBasis(fit: AiFitVerdict | null): MatchFitBasis`
where `MatchFitBasis = AiFitVerdict["basis"] | null` — the same
`measured`/`declared`/`download` ladder `fitNote.ts` already has copy for.
`matchTitle`'s hover text now says "measured from real memory usage ... when
this model ran" for `basis === "measured"`, and "judged from this repo's own
reported size — not yet measured by an actual run here" for `declared`/
`download`, mirroring `fitNote.ts`'s own measured-vs-everything-else split
rather than inventing new wording. The old sentence ("an estimate from the
parameter count and quantization alone ... still in flight") is deleted —
it described exactly the mechanism this round removed, and would have been
false for every row going forward. Added the tests the brief flagged as
missing (`matchTitle`'s basis branch had none before this round).

**(6) `isMatchScoreStale` widened to compare verdicts, not just nullness —
confirmed the reachable path is dissolved, fixed defensively anyway.** Per
the brief's own instruction: `fitOverride != null && (modelFit == null ||
fitOverride.verdict !== modelFit.verdict)`. Traced whether the
differing-non-null-verdict path is actually reachable post-(1)/(4): it is
NOT — `resolveFit`'s `file !== null` gate (item 4) means an override can
only ever win for a row whose `file !== null`, and post-(1) every such row's
`model.fit` is unconditionally `None` from the server. So `modelFit` is
always `null` in the one branch where `fitOverride` can be honoured, and the
new comparison is behaviorally identical to the old null-only check today.
Kept the fix anyway (D798's own "Rejected" column explains why: unreachable
today is not an invariant either function's type enforces, and the fifth
round's own notes explicitly flagged this as a known, deliberately
unaddressed gap) — updated the one existing test that pinned the OLD,
narrower behavior (`isMatchScoreStale(verdict("easy"), verdict("tight"))`
now asserts `true`, was `false`) and added a same-verdict "not stale"
counterpart.

### What this round's brief got wrong

Nothing substantive that I could find. Two places where I diverged from a
literal reading, both flagged above rather than silently decided: (3) the
brief asked me to "revisit defaults" and I concluded the existing constants
already satisfied the goal rather than changing them — verified
numerically, not asserted; (6) the brief's suggested `isMatchScoreStale`
condition is correct but, given (1)/(4)'s combined effect, is not currently
exercisable by any live code path — I said so rather than claiming it fixes
a reachable bug.

One thing worth flagging for a future round: deleting derived fit means a
GGUF row can no longer be hidden by the `verdict: "no"` default-hide rule
(D773) — `(row.get("fit") or {}).get("verdict")` reads `None`, not `"no"`,
for every GGUF row until the lazy per-file lookup resolves one client-side.
This was already true for round 2's unrecognized-quant rows and is an
accepted, not newly-introduced, consequence of this round's fix — but it
means an obviously-oversized GGUF model can sit in the results list (with a
neutral-not-bad match score, see (3)) until a reader scrolls it into view.
Nothing in this brief asked me to change that, and doing so would mean
either inventing a fit verdict (forbidden) or hiding GGUF rows by some other
signal (out of scope, not requested) — leaving it as a known property for
whoever picks up server-side memory judgement for GGUF rows next, if anyone
does.

### Tests run this session

- `.venv/bin/python -m pytest tests/test_hub_models.py -q`: 182 passed (181
  before this session — 1 renamed/rewritten test plus 1 new pinned test for
  the `Q8_K_XL` case).
- `.venv/bin/python -m pytest tests/test_ai_fit.py tests/test_ai_speed.py
  tests/test_doc_duplicate_ids.py -q`: 120 passed (checking the call-pattern
  change — `fit.verdict`/`speed.estimate_tok_s` no longer called at all for
  a GGUF row — didn't regress either module, and D795-D798 clear the
  duplicate-id guard).
- `cd frontend && bun test src/apps/ai_models`: 639 passed (was 636 before
  this session — 3 net new: two `matchTitle` basis-branch tests the brief
  flagged as missing, one `isMatchScoreStale` same-verdict counterpart; some
  existing tests in `resolveFit`/`resolveSpeed`/`matchFitBasis` describe
  blocks were rewritten in place for the new signatures rather than added
  alongside).
- `cd frontend && bun run typecheck`: clean.
- `cd frontend && node scripts/check-boundaries.mjs`: clean (426 files).
- No `mock.module` touched this round, so no full `bun test` run (only the
  targeted `src/apps/ai_models` run, per the standing rule).
- Grepped `tests/` (Python) for every backend/frontend symbol touched this
  session (`gguf_quantization`, `fit_params`, `wantsTotal`, `resolveFit`,
  `resolveSpeed`, `matchFitBasis`, `isMatchScoreStale`, `MatchFitBasis`,
  `"estimated"`/`'estimated'`): no hits outside this repo's own frontend
  test files and two unrelated `estimated` matches in
  `test_model_templates.py`/`test_ai_runtime.py` (a different `estimated`
  boolean field, unrelated to this feature).

### Left for a human with a browser

- The new `matchTitle` basis sentences ("measured from real memory usage
  recorded when this model ran on this machine" / "judged from this repo's
  own reported size — not yet measured by an actual run here") have never
  been seen rendered — confirm they read naturally beside the existing
  verdict/mode sentences.
- The (3) numeric claim (a GGUF row's Match score sits ~26 points below a
  comparable "easy" safetensors row, not near the bottom) was verified by
  calling `_composite_raw_score` directly with synthetic rows, not by
  loading an actual page — a human with a real, quant-diverse result set
  should confirm the table reads as "reasonably ranked, honestly unjudged"
  rather than "buried" for GGUF rows in practice.

## Seventh fix-builder round: the packed-dtype size over-report, and the hoist rule's final form

D799-D800 in DECISIONS.md carry the full reasoning; this is what changed and
what this round's brief got wrong.

### (1) `_estimated_bytes` packed-dtype guard (D799)

Confirmed the brief's diagnosis LIVE, not just by reasoning about the
bytes-per-param ratio. Fetched `https://huggingface.co/api/models/<id>?expand[]=safetensors`
for the actual repos:

- `mlx-community/Lens-3.8B-4bit` and `mlx-community/Lens-3.8B-8bit` both
  report the IDENTICAL `safetensors.parameters` map:
  `{"BF16": 27_361_664, "U32": 4_076_863_488}`, total 4,104,225,152. The old
  unguarded sum for both: `27_361_664*2 + 4_076_863_488*4` = 16,362,177,280
  bytes ≈ 15.24 GiB — matches the brief's "~15 GiB, identical for both
  variants" exactly.
- `mlx-community/Lens-3.8B-bf16` (the brief said `microsoft/Lens-3.8B-bf16`,
  which 401s — wrong org; the real bf16 original is under `mlx-community`,
  found via the Hub's own search API) reports `{"BF16": 4_104_225_152}` —
  same total param count as the quantized siblings, all-float. Real size:
  4,104,225,152 * 2 = 8,208,450,304 bytes ≈ 7.65 GiB, matching the brief's
  "~7.6 GiB" figure.
- `config` came back `{}` for all three (no `quantization`/
  `quantization_config` block), confirming why `_quant` already reports `—`
  for these rows — there's no config-declared bit width for `_quant`'s
  second-priority source to read either.

Diagnosis confirmed as stated. **One correction to the brief**: the bf16
original's repo id is `mlx-community/Lens-3.8B-bf16`, not
`microsoft/Lens-3.8B-bf16` (that id 401s — either private, gated, or simply
wrong). Found the real one via the Hub's `/api/models?search=` endpoint.

Fix: `_estimated_bytes` now sums packed-dtype bytes separately from the
total and refuses (`None`) when packed bytes are a STRICT MAJORITY of the
naive total — i.e. `packed_bytes * 2 > total_bytes`. Chose byte-share, not
param-count share, as the "dominated by" measure: it's the direct measure of
how wrong the returned NUMBER would be (a packed dtype's unknown packing
factor only corrupts its own byte contribution), and it correctly protects
the case the brief itself flagged as a legitimate mix — a small integer
buffer (quant scale/zero-point tensor) alongside real float weights. Added
`test_a_minority_packed_dtype_still_reports_a_size` to pin that the fix
doesn't over-correct: a `U8` tensor at ~0.006% of naive bytes beside a
`BF16`-dominated repo still gets a real `estimatedSize`.

Did NOT add a `config`-based partial unpack (the way `_params` already does
for the COUNT when `config` declares bits) — the brief was explicit that no
new estimate should replace the guess this round removes, and this is a
"make the guess a little more correct" pattern this branch has now rejected
three times (D775, D777, D795) for the exact same reason each time: a
whitelist/partial-fix approach keeps coming back in a new shape.

### (2) The hoist rule, unanimity-only (D800)

Implemented exactly option 2 as specified: `hoistValue` no longer has a
majority branch at all — it returns a value ONLY when literally every row
in the given set (primaries + variants, one set, per D794) agrees, and a
`null` anywhere breaks that outright (never filtered out to manufacture
agreement). `columnVisible` simplified to `hoist === null` after excluding
the all-unknown case, since a non-null hoist is unanimous by construction
now — no more `hoist.unanimous` field on `Hoist` at all (dropped from the
interface; every returned `Hoist` is unanimous by definition).

The muted-majority cell styling the old non-unanimous hoist used to double
as is now a SEPARATE function, `majorityValue` — same 80% floor
(`HOIST_MAJORITY`, unchanged), same modal-value logic, but it never gates
column visibility or the summary line, only `isMajorityValue`'s cosmetic
signal. `familyHoist` now returns both `capabilityHoist`/`quantHoist` (the
unanimity-only pair, feeding `showTask`/`showQuant`/`summary`) AND
`capabilityMajority`/`quantMajority` (feeding the muted-cell styling in
`HubResultsTable.tsx` — renamed the props flowing into `HubResultRow` from
`capabilityHoist`/`quantHoist` to `capabilityMajority`/`quantMajority` since
that's the only thing that component ever actually used them for).

`hoistSummary` dropped its "(mostly X)"/"mostly X" text entirely — with
`hoistValue` now unanimity-only, there's no non-null-but-not-unanimous
value left to phrase a lesser claim about; the function either states the
unanimous fact or is silent about that column.

**Denominator fix**: `familyHoist`'s call to `hoistSummary` now passes
`allRows.length` (primaries + variants, the exact set the hoist was
computed over) instead of `families.length`. Worth flagging precisely what
this changes: the "N models" count in the caption now counts every
underlying repo row (including a family's hidden quant/finetune variants),
not just the number of distinct top-level results on screen. I judged this
the correct literal reading of "the count and the claim measure different
sets" — mathematically, `families.length` was never a FALSE count (a
unanimous fact over the full set is still true of the primaries subset), so
this is a hygiene fix for consistency rather than a correctness bug I could
demonstrate producing a wrong on-screen claim. Flagging this as a judgment
call in case the intended fix was actually "keep families.length for
display, just don't let the majority branch decide a claim about it" — which
is also satisfied by this change, since the majority branch is gone
entirely regardless of which count is used.

Four states pinned directly in `familyHoist`'s test suite (`hubTableView.test.ts`):
unanimous across primaries+variants; a majority with a differing VARIANT
(the exact regressed shape — 3 primaries BF16, 15 hidden variants Q4_K_M,
at 80% majority for Q4_K_M across the full set); a majority with a
differing PRIMARY (4 BF16 + 1 Q4_K_M primaries, 80%); and one `null` among
otherwise-agreeing knowns. Also rewrote `hoistValue`/`columnVisible`/
`isMajorityValue`/`hoistSummary`'s own describe blocks for the new
signatures, and added a new `majorityValue` describe block.

### What this round's brief got wrong

One factual error, corrected above: the bf16 original repo id is
`mlx-community/Lens-3.8B-bf16`, not `microsoft/Lens-3.8B-bf16` (401s). The
diagnosis itself (the mechanism, the ~2x/~6.6x over-report, the identical
dtype maps between 4-bit and 8-bit) was exactly right once fetched from the
correct id.

### Tests run this session

- `.venv/bin/python -m pytest tests/test_hub_models.py -q`: 184 passed (182
  before this session, 2 new: packed-majority and packed-minority
  `_estimated_bytes` pins).
- `.venv/bin/python -m pytest tests/test_hub_models.py tests/test_doc_duplicate_ids.py -q`:
  187 passed together.
- `cd frontend && bun test src/apps/ai_models`: 642 passed (was 639 before
  this session — net +3: `majorityValue` describe block (5 tests) minus
  removed/consolidated `hoistValue`/`columnVisible` cases from the old
  majority-branch coverage that no longer applies).
- `cd frontend && bun run typecheck`: clean.
- `node frontend/scripts/check-boundaries.mjs`: clean (426 files).
- No `mock.module` touched this round (grepped — none in
  `frontend/src/apps/ai_models/`), so no full `bun test` run, per the
  standing rule.
- Grepped `tests/` (Python) for every frontend symbol touched
  (`hoistValue`, `hoistSummary`, `familyHoist`, `isMajorityValue`,
  `columnVisible`, `majorityValue`, `capabilityHoist`, `quantHoist`,
  `unanimous`): no hits — this repo's pytest suite does not assert against
  any of this session's touched lines.

### Left for a human with a browser

- The new hoist behavior (a genuinely diverse quant column staying visible
  with no "mostly X" summary clause, majority cells muted) has never been
  seen rendered on a real, quant-diverse Hub result set — confirm the
  now-silent-on-that-column summary line reads as intentional rather than
  as a missing sentence.
- The `_estimated_bytes` fix's downstream effect — an MLX-quantized repo's
  Size column now falling back to the client's lazy per-file `hub/size`
  lookup instead of showing an (wrong) instant number — has not been
  observed live either; confirm the column shows a real measured size after
  the lazy lookup resolves, rather than sitting blank/dash indefinitely for
  a repo whose measured lookup also can't produce a number.

## Two-pane port (build session starting HEAD b179f8021)

Brief: `/private/tmp/claude-501/-Users-iamsdas-Work-fused-render/ed8c10b1-1b36-4a1e-a82e-d717465cba7e/scratchpad/builder-brief.md`,
mockup: `/private/tmp/claude-501/-Users-iamsdas-Work-fused-render/ed8c10b1-1b36-4a1e-a82e-d717465cba7e/scratchpad/ai-models-two-pane.html`.
Port the Local tab's idle two-face design (search-controls bar + carousels /
Hub table) into a two-pane layout: a 196px capability-nav rail on the left,
a per-capability "what you have" → "suggestions" → "search Hub" pane on the
right, opening into a full mockup-accurate Hub search screen.

### (1) D806 — the one mandated server change (done, committed)

`hub_models.py::api_hub_search`'s `fitLevel` filter treated a row with NO
`fit.verdict` (Hub gave no size to measure) identically to a row whose
verdict is the measured `"no"` — both failed `verdict in allowed`, so
"Easy fit only" / "Easy or tight" silently hid every unmeasured repo
alongside genuinely-too-big ones, with no count and no way to ask for them
back. Fixed to also pass `verdict is None`. See D806 in `DECISIONS.md` for
the full rationale (the two-pane search screen deliberately renders unknown
fit with its own `?` glyph, so the filter must not have already erased the
distinction upstream). Two new tests added directly above
`test_an_unknown_fit_level_is_refused` in `tests/test_hub_models.py`:
`test_fit_level_easy_still_shows_an_unknown_verdict_row`,
`test_fit_level_tight_still_shows_an_unknown_verdict_row`.

Verified: `.venv/bin/python -m pytest tests/test_hub_models.py tests/test_ai_registry.py -q`
→ 230 passed. Committed as `186db6d67`; D806 logged in `DECISIONS.md`,
committed as `12bc1210d`.

### (2) Findings that change scope from the brief

- **`Availability.reason` is already on the wire.** `fused_render/ai/registry.py`'s
  `Availability` dataclass (`ok: bool`, `reason: str`) already flows through
  `fused_render/ai/catalog.py::describe()` into the `/api/ai/catalog`
  payload's per-capability `available`/`reason` fields, and the frontend
  already types this on `AiCatalogCapability` in `platform/lib/api.ts`. The
  brief's conditional "add it if it isn't already exposed" does not apply —
  no server change needed for capability-disabled-with-reason; the nav
  component can read `catalog.capabilities[...].available`/`.reason`
  directly.
- **Capability display order conflicts with the mockup.** The mockup's
  `CAPS` array orders capabilities text → image → speech → embed → video.
  The app's existing shared `CAPABILITY_ORDER` constant
  (`lib/aiModelGroups.ts`, used identically by Benchmark/Playground/Discover
  tabs) orders image → text → speech → embed → video. Not yet resolved;
  leaning toward keeping the app's existing shared order (breaking it here
  would make the Local tab's capability order the only one in the app that
  disagrees with itself) and documenting the mockup deviation explicitly in
  the final report, rather than either reordering the shared constant (blast
  radius: 3 other tabs) or forking a second, Local-tab-only order.
- **`hubTableView.ts` needs surgical trimming, not wholesale deletion.**
  It mixes two families of exports: generic row-formatters that the new
  search-screen hit row still needs (`ageLabel`, `speedLabel`, `quantLabel`,
  `popLabel`, `paramsLabel`, `matchCell`, `matchTitle`, `resolveFit`,
  `resolveSpeed`, `splitRepoId`, `capabilityHint`, `matchFitBasis`,
  `isMatchScoreStale`) and family-grouping/column-hoisting-specific exports
  that are dead once family grouping is gone (`HOIST_MAJORITY`, `hoistValue`,
  `majorityValue`, `columnVisible`, `isMajorityValue`, `hoistSummary`,
  `familyHoist`, `occupiedColumns`, `familyDisplay`, `groupIntoFamilies`).
  Plan: delete only the second group (+ its test cases in
  `hubTableView.test.ts`), keep the file under its current name for the
  first group's helpers, and grep `tests/` (Python) before deleting any of
  it per the standing rule (`pytest-greps-frontend-source-lines`).

### Status: PARTIAL-DONE — stopping here to report

Only the backend fix + decision/notes logging are complete. **None of the
frontend implementation has been started**: no plain-language capability
metadata module, no `CapabilityNav`, no capability pane / row components, no
full search screen, no `LocalTab.tsx` rewrite, no CSS added to
`ai-models.css`, no dead-code deletion (`HubResultsTable.tsx`,
`hubFamilies.ts`+test, the hoist half of `hubTableView.ts`+test, possibly
`Carousel.tsx`/`RecommendedCard.tsx`/`RepoCard.tsx`/shadcn `table.tsx`), and
no `bun` commands have been run this session (no `bun test`, no
`typecheck`, no `build`, no `check-boundaries.mjs`).

Also still unread, and required by the brief before building markup: the
mockup's lines 787-1319 (`screen`/`pane`/`controls`/`menu`/`row`/`drawer`
render functions and the 15-entry `STATES` array), `frontend/src/styles/ai-models.css`,
and `frontend/src/styles/tokens.css`.

### Resume point for the next builder

1. Read `ai-models-two-pane.html` lines 787-1319 (the render functions and
   `STATES` array — this is the acceptance-bar reference for markup, copy,
   spacing, and every state).
2. Read `frontend/src/styles/ai-models.css` (3843 lines, unread) and
   `frontend/src/styles/tokens.css` for the token/theme rules to reuse.
3. Decide the capability-order question above (default: keep
   `CAPABILITY_ORDER`, log the deviation) and add a plain-language metadata
   module (label/icon/searchNoun per capability) — likely a new file next to
   `lib/aiModelGroups.ts` rather than overloading `CAPABILITY_LABELS` in
   `lib/engines.ts` (those are the technical labels Benchmark/Playground
   still need unchanged).
4. Build `CapabilityNav`, the capability pane (what-you-have / suggestions /
   search-Hub-link), the full search screen (chip + `ControlMenu`-based
   filters + fixed-grid hit rows using the kept `hubTableView.ts` helpers),
   then rewire `LocalTab.tsx` and delete the dead code (grep `tests/` first
   for every deleted symbol).
5. Run targeted `bun test` per touched file as you go; `bun run --cwd
   frontend typecheck` and one final `bun run --cwd frontend build` +
   `node frontend/scripts/check-boundaries.mjs` at the end; re-run
   `.venv/bin/python -m pytest tests/test_hub_models.py tests/test_ai_registry.py -q`
   plus any other Python files touched by the deletions.
6. HEAD at handoff: `12bc1210d` (branch `worktree-hub-search-discovery`,
   tree clean).

### Continuation builder update (2026-09-11) — CAPABILITY_ORDER reorder done

The order question left open above is now resolved by explicit instruction
(not by me): the mockup's order wins everywhere, not just on the Local tab.
Changed `CAPABILITY_ORDER` in `frontend/src/apps/ai_models/lib/aiModelGroups.ts`
to `["text-generation", "text-to-image", "automatic-speech-recognition",
"embeddings", "text-to-video"]` (was image-generation-first). Also swapped
the head two entries of `PLAYGROUND_GROUPS` (`frontend/src/apps/ai_models/
playground/groups.ts`) to match, per that file's own "edited together"
comment, and rewrote both files' header comments so they no longer describe
the now-superseded 2026-08-25 "image generation leads" decision as current.
Logged as D807 in `DECISIONS.md`.

Fixed the order-dependent test assertions this broke: four `sections.map(s
=> s.key)`/`g.models.groups.map(s => s.label)` arrays and one runner-order
array in `aiModelGroups.test.ts`, plus `orderCapabilities`'s test in
`benchmark.test.ts`. Checked (and left alone, correctly) several other
`sections[0]`/`groups[0]`-indexed assertions in `aiModelGroups.test.ts` that
turned out to be order-agnostic (single-capability inputs). Grepped `tests/`
(Python) for a literal order assertion — none exists. `bun test
src/apps/ai_models` (673 tests) is green after the fix.

`Availability.reason` confirmed on the wire already (prior builder's D806
notes) — no server change needed for the no-engine capability state; I will
read it straight off `catalog.capabilities[...].reason` client-side.

Status: still PARTIAL — this is only the capability-order prerequisite.
Next: read `frontend/src/styles/ai-models.css` + `tokens.css`, then build
the plain-language capability metadata module, `CapabilityNav`, capability
pane, row/drawer components, full search screen, rewire `LocalTab.tsx`, and
delete dead code, per the builder brief's full scope list. HEAD at this
checkpoint: commit "Reorder CAPABILITY_ORDER to the mockup's order" (see
git log), tree otherwise clean.

### Units 1-6 (2026-09-11, same continuation) — markup port and hard-stop rule reached

Built the plain-language capability metadata module, `CapabilityNav.tsx`,
`CapabilityPane.tsx` (+ `EngineFilesPane`), `ModelRow.tsx`, rewrote
`SearchControls.tsx` and `HubSearchScreen.tsx` for the mockup's full search
screen, then rewired `LocalTab.tsx` into the single `<div className="tp"
data-part="page">` two-pane wrapper. Markup, class names and copy ported
verbatim from the mockup's render functions per the brief. Hard-stop rule
(LocalTab renders the two-pane layout, typecheck clean) reached and
committed as `01516f06d`. Commits `904f406cf`, `01516f06d` cover this span.

### Unit 7 (2026-09-11) — delete the dead carousel/table-era code

Deleted the card-grid/dense-table components and CSS the search screen
replaced (`HubResultsTable.tsx`, `hubFamilies.ts`+test, the hoist half of
`hubTableView.ts`+test, `Carousel.tsx`/`RecommendedCard.tsx`/`RepoCard.tsx`,
shadcn `table.tsx`, and their CSS in `ai-models.css` — net ~980 lines).
Rewrote the hub-table-related tests that pinned the deleted symbols and
fixed Python test docstrings (`tests/`) that cited the deleted files by
name, per the standing `pytest-greps-frontend-source-lines` rule. Committed
as `16c8d07fe`.

### Unit 8 (2026-09-11) — CSS for the two-pane layout

Added the CSS the ported markup needed: a new "THE TWO-PANE LOCAL TAB"
section in `ai-models.css` (~600 lines) plus the two missing
`.am-hub-controls-push`/`.am-hub-resultline` rules in the pre-existing Hub
search section. Values copied from the mockup's `<style>` block, all colors
via `tokens.css` tokens (no literals). The mockup's own bare/generic class
names already present verbatim in the ported TSX (`.row`, `.chip`, `.mono`,
`.tick`, `.drawer`, `.acts`, `.plain`, `.unknown`, `.why`, `.bar-prog`,
`.match`, `.mbar`, `.mnum`, `.rowwrap`, `.scoped`, `.rule`, `.capicon`,
`.iconbtn`, `.row-*`) are scoped under a `.tp ` ancestor combinator rather
than left bare (LocalTab's own `.tp` wrapper makes this safe) or renamed
(renaming would have broken the "ported verbatim" mandate from units 1-6).
Distinctive names (`.tp-nav`, `.tp-pane`, `.tp-head`, `.tp-group`,
`.hubdoor`, `.hits`, `.adv-head`, `.bigsearch`, `.advfoot`, `.sorthint`,
`.offpane`) left unscoped. The hit row's match/verdict coloring uses
attribute selectors (`.tp .match[data-verdict="easy|tight|no|unknown"]`)
matching the real `data-verdict={cell.verdict}` markup rather than the
mockup's separate `.fit-*` classes. Also fixed `HitRow`'s inline
`style={{ color: "var(--danger, #c0392b)" }}` — no `--danger` token exists
anywhere in `tokens.css` — by switching to `className="row-reason danger"`
backed by `.tp .row-reason.danger { color: var(--error); }`. Logged as
D808/D809 in `DECISIONS.md`. Committed as `f74f5975e`.

### Unit 9 (2026-09-11) — final verification

`bun run --cwd frontend typecheck`: clean. `bun run --cwd frontend build`:
succeeds (pre-existing dynamic/static import warning for `api.ts`,
unrelated to this work — `main.tsx` and many other modules already
statically import it). `node frontend/scripts/check-boundaries.mjs`:
`boundaries OK (753 files)`. `.venv/bin/python -m pytest
tests/test_hub_models.py tests/test_ai_registry.py tests/test_fused_ai_client.py -q`:
277 passed. Full `bun test --cwd frontend`: 5269 pass, 0 fail (run during
Unit 8's verification, unchanged by Unit 9 since no further TSX/CSS edits
followed).

Status: DONE. HEAD: `f74f5975e` (branch `worktree-hub-search-discovery`),
tree clean after the Unit 8 commit. All 9 build-order units complete.

## Fix round 1 (2026-09-11) — live-vs-mockup polish pass

A separate orchestrator/coordinator round after Unit 9's "done", driven by
`fix-brief-1.md` (items A-N, then O added mid-round; L was revised twice
and F/M/C were re-emphasized by direct user screenshot feedback). This
builder session completed items **N, J, L (revised), M, F, H, O** across
four commits before running out of round budget. Items **A, B, C, D, E, I,
K remain open** — see "Resume point" below.

Commits this round (newest first):
- `86a30d6c8` — item O: `.tp .btn`/`.tp .btn-primary` scoped mockup colours
  (supersedes the brief's original "do not touch `.btn`" rule per a later
  coordinator message).
- `bec5be256` — items L (revised)/M/F/H: dropped `.tp`'s outer frame for a
  single top hairline; fixed the hubdoor link and back button (both were
  bare `<button>`s catching native browser chrome instead of the intended
  plain-text/quiet styling); fixed a specificity bug that let the shared
  `.tp .capicon` (18×18) rule beat the intended `.tp-nav .capicon` (15×15).
- `1381195e0` — item J: removed `HubSearchScreen`'s unused `runners` prop
  and its `LocalTab.tsx` pass-through/import. (The brief's other J claim,
  `ModelRow.tsx`'s `ReactNode` import being unused, is wrong — it's used
  twice — left untouched.)
- `4c3c10e56` — item N: pinned hardware to 32GB in
  `test_the_untagged_mirror_signal_pulls_in_a_republish_too`, which was red
  on every CI lane and green only on this Mac (see D811).

Full decision writeups: D810 (L/M/F/H/O bundle), D811 (N), D812 (J).

### Resume point for items A, B, C, D, E, I, K

Everything below was read but not yet implemented; picking this up should
start from here rather than re-reading the brief and mockup from scratch.

- **C (hit row rewrite) — highest priority, reinforced twice by direct user
  screenshot feedback ("cards are empty and cramped... follow the
  mockup!").** Current `HitRow` in `HubSearchScreen.tsx` (function starts
  ~line 171) uses grid order name→match→facts→actions with a separate
  `.row-owner` span and a `row-note mono` line that can end in a dangling
  "—". Mockup wants: match cell FIRST, full repo id in mono bold (no
  separate owner span), `from <base>` line only when a base model is known,
  one `row-meta` line with no dangling dash, popularity as a 2-col grid
  (`row-facts pop`), Download+ⓘ actions, `.row.hit.rich` grid
  `112px minmax(0,1fr) 150px 152px`, 73px row height. CSS for `.row.hit`
  (`84px minmax(0,1fr) auto auto` — needs replacing with the `rich` track
  spec), `.row-owner`, `.row-reason*` all live around
  `frontend/src/styles/ai-models.css:3428-3497` (line numbers will have
  shifted after this round's edits — search for `.tp .row.hit`). No
  existing helper maps a fit verdict to a glyph (●/▲/■/?) —
  `frontend/src/apps/ai_models/lib/hubTableView.ts` is the right home for a
  small exported `verdictGlyph()` alongside `matchCell`/`matchTitle`, with
  its own test in `hubTableView.test.ts` (add/adjust per the Verification
  section). Needs a `.downloaded` chip class distinct from the generic
  `.chip`, and an ⓘ `iconbtn` opening the same drawer pattern
  `ModelRow.tsx`'s `Drawer` component uses (read that component before
  wiring it in — it is NOT currently shared by `HitRow`).
- **B (menu buttons/dropdown)** — real architectural tension, documented at
  the top of `SearchControls.tsx`: the file deliberately avoids
  reproducing the mockup's per-option `<p class="h">` hover sentence
  because `@platform/ui/ContextMenu`'s `MenuEntry` has no slot for it,
  riding the sentence on the trigger's own `title` instead. The brief
  explicitly wants the mockup's `.dd`/`.l`/`.h`/`.chk` markup with a hover
  line under EVERY option. Needs a decision: extend `MenuEntry`/`ContextMenu`
  with an optional hint string (more portable, but touches a `@platform`
  shared surface), or accept the drift and give the four hub menus their
  own dropdown scoped under `.tp` (bigger diff, but keeps `ContextMenu`
  alone). Also needs: `Key:` prefix (`<span class="k">Fit:</span>`) inside
  the trigger, `.menubtn`/`.dd` CSS per the brief's exact block (fontSize
  12px not 13px, radius 7px not 6px, active state with `×` remover).
- **A (seed Task filter from capability)** — `LocalTab.tsx`'s
  `onOpenSearch={() => setSearching(true)}` (around line 549, shifted since
  this round's edit near line 520-ish) needs to also seed
  `settled.task`/URL `hubTask` with the capability's pipeline tag before
  opening search; `CapabilityPane.tsx`'s `onOpenSearch: () => void` prop
  needs a signature change to pass the tag through. Need to find the
  capability→pipeline-tag mapping (likely in `capabilityMeta.ts` or
  `aiModelGroups.ts` — not yet located precisely).
- **D (gated rows)** — move the chip to `.row-name` (`Gated`, `warn-chip`
  class already exists in CSS), change the action to `<a class="btn">Accept
  terms</a>` (not `btn-link`), remove `.am-hub-login` banner from the
  search screen entirely (currently rendered via `needsHubLogin(...)` in
  `HubSearchScreen.tsx` ~line 429).
- **E (result line)** — mostly already implemented in
  `SearchControls.tsx`'s `.am-hub-resultline` block (hidden-unfit count
  logic already exists); still needs the count wrapped in `<b>` per the
  mockup and a check of whether `hub_models.py`'s response already carries
  the exact fields used (`hiddenUnfit` already exists on `HubModel`
  search response per `HubSearchScreen.tsx`'s `data.hiddenUnfit`) — likely
  no server change needed, just JSX/CSS.
- **I (Try as link)** — find the Try button (likely in `ModelRow.tsx`'s
  `Actions` component, not yet located precisely) and convert
  `<button class="btn btn-primary" onClick>` to an `<a>`/router-link to the
  Playground route with `?model=<encoded repo id>`; add a test asserting
  the href contains the encoded id.
- **K (narrow width)** — no code change believed needed: confirmed via full
  mockup scan that no real `[data-w=narrow]` CSS rule exists (it's a JS
  demo-toggle attribute only); the only real breakpoints are
  `@media (max-width:760px){.tp{grid-template-columns:1fr}}` and
  `@media (max-width:720px){.row-facts.pop{display:none}}`. Once item C's
  `.row-facts.pop` class exists, just confirm it wraps/hides reasonably at
  narrow widths rather than overflowing — no new breakpoint to invent.

Verification run this round: `.venv/bin/python -m pytest
tests/test_hub_models.py -q` → 195 passed (includes the fixed item-N test).
`bun run --cwd frontend typecheck` → clean, three times (after J, after
L/M/F/H, after O). `bun test src/apps/ai_models` (frontend/, item O check)
→ 597 pass, 0 fail. `grep -rn` in `tests/` for `runners={runners}`,
`runnersByCapability`, `SectionRunner`, `adv-back`, `btn-link`,
`data-adv-back` → no hits (no Python test asserts on any of these literal
frontend lines). Full `bun run --cwd frontend build` was NOT run this
round (deferred to whoever finishes items A/B/C/D/E/I/K, per the brief's
"once at the end" instruction).

## Fix round 2 (items C, A, B, D, E, I)

Implemented in priority order per `fix-brief-1.md`: C+D first, then A, then
B (+E folded in), then I. Also fixed two nits found in live review before
this round's items: T-nit (drawer's "Why we suggest it" line now reads the
pane's own visible title lowercased, not `capabilityLabel`'s internal key)
and S-nit (`.tp .btn-link` no longer shows a permanent underline on the
drawer's HF link).

- **D818 — Hit rows rewritten to `.row.hit.rich` (items C, D).** `HitRow`/
  `HitDrawer` in `HubSearchScreen.tsx` now match the mockup's dense row
  layout; the gated state's login action moved into the drawer instead of
  a page-level `.am-hub-login` banner. `hubTableView.ts` gained
  `verdictGlyph`. Self-review caught and fixed a stale test
  (`HubSearchScreen.test.ts` pinned the pre-rewrite ternary literally) and
  a redundant ternary in the gated chip (`gate.pill` is always one of
  exactly `"gated"` / `"gated — by approval"` when `gate` is truthy, so the
  chip just renders the mockup's literal `Gated` string now).
- **D819 — Task filter seeded from the opening capability (item A).**
  `CapabilityPane`'s `onOpenSearch` now takes the capability key and
  `LocalTab.tsx` seeds `settled.task` with it before opening search.
  Confirmed via `capabilityMeta.ts` that `capabilityKey` already IS the
  Hub's own pipeline tag (the same vocabulary the Task filter and `hubTask`
  URL param use) — no separate capability→tag lookup table was needed, so
  this landed smaller than the round-1 notes above anticipated.
- **D820 — Controls-row menus rebuilt as `ControlMenu`, one hover
  sentence per option (item B); match count bolded (item E).** The
  shared `ContextMenu`/`MenuEntry` surface has no slot for a per-option
  hover sentence (`.dd .h`), so rather than extend that one shared surface
  for a field only this screen uses, `SearchControls.tsx` now owns a small
  `ControlMenu` component matching the mockup's `.menubtn`/`.dd`/`.l`/`.h`/
  `.chk` markup verbatim, with its own outside-click/Escape dismissal
  (same contract `ContextMenu` gave every menu on this page). Every
  option's hint reuses that option's own existing `.title`/`.help` field
  (`FIT_LEVELS`, `PARAMS_BANDS`, `SORTS`, `activeTask`'s task entries) — no
  new copy was invented. The dropdown's `box-shadow` uses `--shadow-lg`,
  which already resolves to the mockup's exact literal `rgba(0, 0, 0,
  0.18)` in light mode, avoiding both a new token and a literal color.
  Item E was mostly already in place per the round-1 notes; this round
  just wrapped the count in `<b>` and tightened the result-line spacing to
  match the mockup's metrics.
- **D821 — Local's Try button is a real link to the Playground (item
  I).** `ModelRow.tsx`'s `model.have` branch now renders `<a
  href={tabHref("playground", `?model=${encodeURIComponent(model.id)}`)}>`
  instead of a plain `<button onClick>`, following the same intercepted-
  left-click pattern `AiModelsPage.tsx`'s tab strip already uses (a real
  `<a>` for middle-click/copy-link, `e.preventDefault()` +
  `navigateUrl(tryHref)` on a plain left-click, skipping interception for
  modified clicks). `onTry` still fires first so the actual load starts
  before navigation. Verified by test that the href contains the encoded
  repo id (`ModelRow.test.ts`).

Deviations from the brief: D (gated-row rewrite) landed together with C in
one commit rather than strictly after A/B in file order, since both are
the same `HitRow`/`HitDrawer` rewrite and splitting them into separate
commits would have meant a broken intermediate state. Item B's
architecture deviates from the round-1 notes' "keep `ContextMenu`"
framing — this round resolves that tension by building the small
scoped dropdown described above instead.

Verification this round: `bun test src/apps/ai_models/local` passed at
each item boundary (41 then 42 tests, 0 fail) and once more at the end
(42 pass, 0 fail). `bun run --cwd frontend typecheck` clean after item A,
after item B/E, and after item I. `bun run --cwd frontend build` was not
run this round either — still deferred, per the brief, to whoever closes
out the remaining lettered items (F, G, H, J, K, L, M, N).

To verify in the browser:
- Hit rows in the Hub search results match the mockup's dense
  `.row.hit.rich` layout (name, chips, glyph column, facts) at both full
  and narrow width; opening a gated row's drawer shows the login/accept
  action there, not as a page banner.
- Opening the search screen from a capability pane (e.g. "Chat") lands
  with the Task filter already set to that capability, not "Any task".
- Task/Fit/Size/Sort dropdowns show the mockup's `.menubtn`/`.dd` styling
  (12px font, 7px radius, active state with a `×` remover) and each option
  shows its hover sentence as a visible line under the label, not only on
  mouse hover.
- The result line's match count renders bold; the "N hidden — will not fit
  here" clause still only appears while "Show models that will not fit" is
  off.
- Clicking "Try" on a model this Mac already has navigates to the
  Playground with that model pre-selected; middle-click / Cmd-click opens
  it in a new tab instead of intercepting.
- The drawer's "Why we suggest it" sentence reads the pane's own visible
  (lowercased) title, e.g. "chat", not an internal key like
  "text-generation".
- The drawer's Hugging Face link has no permanent underline, only on
  hover/focus, matching every other `.btn-link` on the page.

## Fix round 3 (2026-09-11)

Twelve items from `fix-brief-3.md` plus two mid-round additions (item 11:
standard capability names, superseding item 3's label half; item 12: label
the match-cell score), committed one per item (D822-D833 in
`DECISIONS.md`):

- Item 1: pinned every `.row.hit.rich` to a uniform 73px so rows with and
  without a `from <base>` line no longer produce a ragged list.
- Item 2: `HitRow`'s row-meta line drops the DASH glyph (`"—"`) along with
  null/empty segments, so an unknown params/quant field no longer leaves a
  dangling `"· —"`.
- Item 3 (superseded in its label by item 11): the Task menu's active
  label now reads through `capabilityLabel()` for any of the five
  capabilities this app runs, instead of the Hub's own per-runner string.
- Item 11: standard, Title Case capability names ("Text generation",
  "Image generation", "Speech to text", "Embeddings", "Video generation")
  now come from ONE table (`engines.ts`'s `CAPABILITY_LABELS`), with
  `capabilityMeta.ts`'s `plain` field reading straight off it — so nav,
  pane heading, back-link, Task menu, and the drawer's "why we suggest it"
  sentence all agree by construction. The old friendly names ("Chat &
  writing" etc.) are gone from the Local tab.
- Item 4: removed the "Searching huggingface.co, limited to models an
  engine here can load." line — not in the mockup — and its now-unused
  CSS.
- Item 5: quant/publisher text filters were already wired in a prior
  round; confirmed only, no diff.
- Item 6: class/CSS parity pass against the mockup's selector list found
  one real gap (`.resultline` was missing from the controls row) and
  fixed it; every other listed selector was already present and applied.
- Item 7: both round-2 nits verified already correct (the drawer sentence
  now inherits the standard name via item 11; `.tp .btn-link` already has
  no permanent underline).
- Item 8: dropped the long "Fit not measured yet" italic sentence and its
  now-unused CSS; `.row-meta.reason` (the "Will not fit" text) is
  untouched.
- Item 9: `.tp .btn` no longer inherits a browser underline, and
  `.row-act .btn` now has a fixed min-width/centred text so "Accept
  terms" lines up with "Download".
- Item 10: `.tp .btn` rebuilt to match `.menubtn`'s exact metrics (28px
  height, 6px radius, quiet border/fill, 12px/500 type), explicitly
  overriding every property the global `.btn` sets; `.btn-primary`'s
  accent fill is untouched.
- Item 12: the match-cell number now carries a small uppercase "match"
  caption underneath it (`<span class="score"><b>84</b><small>match</small></span>`),
  the cell's existing `title` (`matchTitle`) already explained the score
  correctly, and the sorthint legend now opens by naming the number
  ("Match score (0–100) is ranked for this Mac: …").

Verification this round: `bun test` scoped to the touched files
(`hubSearchView.test.ts`, `capabilityMeta.test.ts`) green after items 3
and 11 (91 pass, 0 fail across 7 files); every other item is CSS/markup
only per the brief, so ran no tests. `bun run --cwd frontend typecheck`
run clean at the end of the round.

To verify in the browser:
- Every hit row in the results list is the same height (73px), whether
  or not it shows a "from <base>" line.
- No row-meta line ends with a dangling "· —"; no row shows an italic
  "Fit not measured yet…" sentence.
- The left nav, the pane heading, the "← Back to …" link, the Task menu,
  and the drawer's "Why we suggest it" sentence all say the SAME standard
  name for a capability (e.g. "Text generation"), not "Chat & writing".
- No "Searching huggingface.co…" line above the controls row.
- "Accept terms" and "Download" buttons in the row-act column are the
  same width and both centred; plain `.btn`s throughout the two-pane
  screen look identical in size/border/radius to the `.menubtn` triggers
  above them.
- Each match-cell number (e.g. "84") has a small "MATCH" caption under
  it, and hovering the cell still shows the memory/speed/freshness
  explanation; the legend line below the list now opens by naming the
  score.

Extension to item 11: the same standard capability names now also show
up outside the Local tab — Playground's mode picker/headings and the
Home page's "AI Playground" strip both pulled two labels of their own
("Transcription", "Video") that disagreed with the Local tab's ("Speech
to text", "Video generation") for the exact same capability;
`playground/groups.ts` now derives every label from `capabilityLabel()`
instead of hand-picking it, so Playground/Home/Local/Benchmark/Engines
all read off the one table. Benchmark and Engines already called
`capabilityLabel()` directly and needed no change; Usage shows no
capability names. No test asserted the old strings, so no test changes
were needed here.

To verify in the browser (extension): open the Playground tab and the
Home page's "AI Playground" strip — both should read "Speech to text"
and "Video generation" (not "Transcription"/"Video"), matching the
Local tab's nav exactly.

### Additions to round 3: items 13-14

- Item 13: the Local tab's nav rows now draw the exact same icon
  components the Playground sidebar uses per capability, instead of a
  private, differently-drawn set. The shared map moved to
  `lib/capabilityIcons.tsx` (next to `engines.ts`'s `capabilityLabel()`),
  kept out of `playground/groups.ts`'s import graph (Home's own eager
  path) so no JSX reaches the front door. `capabilityMeta.ts` became
  `.tsx`; its `icon` field is a `ReactNode` now, not a raw SVG string.
- Item 14: model ids are clickable links to huggingface.co again, in
  both the search hit rows (`.mono-name`) and the capability pane's
  rows for downloaded/suggested models — restoring `origin/main`'s
  `RepoCard.tsx` behavior. No colour change; underline appears on
  hover/focus only.
- Amendment to item 11: embeddings specifically keeps "Search &
  similarity" (not "Embeddings") everywhere, while the other four
  capabilities keep their standard names — a one-line change to
  `engines.ts`'s `CAPABILITY_LABELS` table since every surface already
  reads through it.

To verify in the browser:
- The Local tab's capability nav icons (Text generation, Image
  generation, Speech to text, Search & similarity, Video generation)
  look identical to the Playground sidebar's icons for the same
  capabilities.
- Clicking a model's name/id in a search hit row or in a capability
  pane's "On this Mac"/suggestion list opens huggingface.co in a new
  tab, without also opening that row's info drawer.
- The embeddings capability reads "Search & similarity" in the nav,
  pane heading, Task menu, and Playground — not "Embeddings".

### Round 4: user-reported regression + code-review fixes (2026-09-11)

- Item 6 (regression, fixed first): D831 (round 3, item 9)'s
  `.tp .row-act .btn { min-width: 108px; … }` unintentionally widened the
  capability pane's Try/Download buttons too (mockup wants natural width,
  ~46px for "Try"). Rescoped to `.tp .row.hit.rich .row-act .btn` — search
  hit rows only. CSS-only, no test.
- Item 1: Size sort ranked a GGUF row by its repo-wide `usedStorage` total
  (`lookupTotalSize(id, null)`) instead of the single file the row's cell
  actually shows — the "17 bytes/param" bug. `measureSizes` now takes
  `{id, file}` pairs, looks up the per-file size for rows with a file, and
  skips the lookup entirely for file-less rows (their cell always renders
  null). New tests in `HubSearchScreen.test.ts` call the exported
  `measureSizes` directly against a seeded multi-quant cache.
- Item 2: the debounce effect fired an extra `searchHubModels` call on
  every mount (settling `liveQuery === settled.q` still built a new
  `settled` object identity) and separately reverted a filter/sort click
  made within the 350ms window (the timer's `setTimeout` closed over a
  stale `settled`, deps being `[liveQuery]` only). Fixed with an early
  return when unchanged, and a `settledRef` kept current every render so
  the timer merges into the latest settled state, not a stale one.
- Items 3 & 4: the empty-result message rendered `matches ""` when a pane
  opened with a task filter but no typed query (default state from a
  pane's "Search Hugging Face for more…" door) — now branches on
  `settled.q.trim()`. Also deleted a `summary` computation
  (`resultsSummary`) rendered into a `display: "none"` paragraph — dead
  output left over from the `HubResults` port; `resultsSummary` itself
  stays in `hubSearchView.ts`, still used/tested elsewhere.
- Item 5: `capabilityMeta.tsx`'s text-generation `searchNoun` was still
  "chat models", the last leftover of the pre-D825 vocabulary; changed to
  "text generation models" to match the "image models"/"video models"
  style the other capabilities use. `embeddings`'s "embedding models"
  stays as-is. CSS/markup-only; the one test assertion of the old string
  was updated to match.

To verify in the browser:
- Capability pane's "Try"/"Download" buttons are back to natural width
  (not stretched to 108px); search-hit row action buttons are still all
  the same fixed width.
- Open a pane via its "Search Hugging Face for more…" door (task filter
  set, no typed query) with zero results — the empty-state message should
  NOT read `matches ""`.
- The text-generation pane's search placeholder/copy reads "text
  generation models", not "chat models".

## Fix round 5 (2026-09-11)

Nine items, three from the original brief plus six added mid-round via
coordinator messages (5-9). D843-D850 in DECISIONS.md; see there for full
rationale on each.

- Items 1 & 2 (D843): a search from a capability pane's own "search
  Hugging Face" door 400'd for embeddings — the endpoint only understood
  a single Hub pipeline tag, and embeddings spans two
  (`feature-extraction`, `sentence-similarity`) which Hub's AND-only
  `filter=` cannot express in one request. Added
  `ai/tasks.py::tags_for_capability` and a `capability` field on the
  search request; the server now fetches per-tag and merges/de-dupes
  when needed, keeping `task` for backward compat. Since the left pane
  already scopes the whole screen to one capability, the Task menu in
  `SearchControls.tsx` had no job left and was removed outright.
- Item 3 (D844): the match-score cell's tooltip was a native `title=` —
  small, unstyled, one line. Replaced with a `data-tip` + scoped
  `.tp .match[data-tip]::after` CSS popover, opening upward; confirmed by
  grep no ancestor clips it with `overflow: hidden`.
- Item 4 (D845): the stats cell (downloads/likes/date) was a fragile
  2-column grid with an `nth-child(3)` escape hatch. Reworked to a single
  right-aligned flex column: downloads+likes grouped on one line
  (`.counts`), date underneath.
- Item 5 (D846): removed the redundant "View on Hugging Face ↗" link
  from the hit row's drawer (the row's own name already links there,
  D837) — kept "Close". `.tp .btn-link` CSS confirmed still used
  elsewhere (`ModelRow.tsx`, `CapabilityPane.tsx`, this Close button), so
  not removed.
- Item 6 (D847): the Search button in `.bigsearch` was shorter than the
  query input beside it. Added a scoped `.tp .bigsearch .btn` rule
  (`height: auto; align-self: stretch; ...`) and a `btn-lg` className
  hook, rather than loosening `.tp .btn` globally.
- Item 7 (D848): removed the "Show models that will not fit" toggle
  entirely — every row is always shown now (the per-row red "Will not
  fit" line is the only warning left). Frontend dropped the checkbox,
  `includeUnfit`/`hiddenUnfit` state, and the `?hubUnfit=1` URL param
  (an old saved URL with it is now silently ignored). Server always
  behaves as `includeUnfit: true`, accepting but ignoring the field from
  an old client. Rewrote the pytest tests that pinned the old
  default-drop/overfetch behavior (including the overfetch-sizing test,
  whose expected `limit` shrank now that `include_unfit` no longer
  varies).
- Item 8 (D849): only the right pane scrolls on the Local tab now; the
  capability nav stays put. This reverses D815's own explicit choice not
  to add an inner scroller. `.tp-page-fill`/`.tp` no longer scroll;
  `.tp-pane` — the one class all three right-column panes already share
  as their root, found by grep rather than editing three files — is now
  the scroller; `.tp-nav` gained `align-self: start`. Verified by reading
  (not in-browser) that D844's popover is unaffected by the new
  `overflow-x: hidden` on `.tp-pane`.
- Item 9 (D850): hit rows show more metadata, ordered common-to-rare
  left-to-right. Meta line gained a format token (GGUF/MLX/Safetensors,
  derived from `format`/`library`, skipped if neither) and an
  `"N variants"` count (only shown when > 1). The "from X" line now uses
  `relation` for phrasing: "quantized from X" / "fine-tuned from X" /
  "adapter for X" / "merge of X", plain "from X" otherwise. Server gained
  `_count_variants(raw)` — GGUF repos count `.gguf` siblings minus
  mmproj/vision-projector helpers, safetensors/MLX repos count distinct
  bit-width/dtype subfolders only when there's clearly more than one,
  else 1 — read from data already fetched (`siblings`), no extra Hub
  request. Last-updated (rather than created) in the stats cell was
  already done as part of item 4's rework.

### Tests run this round

- `.venv/bin/pytest tests/test_hub_models.py -q` — 204 pass (200 after
  item 7's rewrites, +4 more after item 9c's variant tests, +2 for the
  mmproj/subfolder edge cases — 6 new tests total for item 9c).
- `bun test src/apps/ai_models/local/` — 48 pass throughout (item 7's
  checkbox-removal test rewritten; no new frontend test needed for item
  9's display-only changes beyond what typecheck catches).
- `bun run --cwd frontend typecheck` — clean after every item.
- Items 3, 4, 5, 6, 8 are CSS/markup-only per the brief; no tests written
  for them, matching this round's own "CSS-only rounds skip tests" rule.

### To verify in the browser

- Embeddings capability's "search Hugging Face" door no longer 400s.
- No Task menu in the search controls row on any capability pane.
- Hovering/focusing the match-score cell shows a styled popover, not a
  native tooltip; it opens upward and isn't clipped.
- The stats cell shows downloads+likes on one line, last-updated date
  underneath, right-aligned.
- The hit row drawer has no outbound Hugging Face link, only Close.
- The Search button in the big search bar is the same height as the
  query input beside it.
- No "Show models that will not fit" checkbox anywhere; every row (fit
  or not) is always in the list.
- Scrolling the Local tab's right pane leaves the left capability nav in
  place; the nav no longer scrolls off screen.
- A hit row's meta line can show a format token (GGUF/MLX/Safetensors)
  and an "N variants" count after size, when applicable; the "from X"
  line reads "quantized from"/"fine-tuned from"/"adapter for"/"merge of"
  when the repo has a known relation to its base model.

## Fix round 6

- Item 1: a `capability`-scoped search now drops any row whose
  `_model_row`-classified `capability` disagrees with the filter, before
  count/limit truncation — the Hub's own `filter=<tag>` over-matches (it
  matches any tag in a repo's tag list, not just its `pipeline_tag`). The
  legacy `task` path is unaffected.
- Item 2: `formatToken` (new `lib/formatToken.ts`) now names any Hub
  library, not just gguf/mlx/safetensors — falls back to a capitalized
  library name instead of showing nothing.
- Item 3: the stats cell's downloads/likes/updated are one right-aligned
  column now (`.counts` inline wrapper from round 5 removed) — three
  direct children of `.row-facts.pop`, no grouping span.
- Item 4: the drawer's Close control moved from a footer link to a
  top-right `×` button (`aria-label="Close details"`); the footer row is
  gone since Close was its only child.
- Item 5: the search response now carries `facets: { publishers, quants }`
  (each `{id, count}`), computed pre-narrowing — before the post-join
  `quant` filter, and (since `publisher` narrows the Hub-side wire request
  itself) from a second, cache-shared fetch with `author` dropped whenever
  `publisher` is set. The Publisher/Quant free-text inputs are now
  searchable dropdowns (`SearchMenu` in `SearchControls.tsx`) fed by these
  facets, with a filter input, Enter-applies-free-text fallback, and a
  click-to-select option list — same `.dd`/`.menubtn` markup as the other
  menus.
- Item 6 (addendum to D849): the Sort menu's dropdown was clipped by the
  round-5 scrolling pane's `overflow-x: hidden` — it's the row's
  right-most trigger and its options ran past the pane's edge. Added
  `ControlMenu`'s `align="right"` prop (`.dd.right`, anchors to the
  trigger's right edge) and a hard `.dd` max-width
  (`min(360px, calc(100vw - 48px))`) regardless of anchor. Publisher/Quant
  menus (item 5) sit before the `.push` spacer and stay left-anchored.

### Round 6 test results

- `.venv/bin/python -m pytest tests/test_hub_models.py -q` — 208 pass.
- `bun test frontend/src/apps/ai_models/local/` — 53 pass (5 new
  SearchMenu-behavior tests, 3 ControlMenu-anchor assertions updated).
- `bun test frontend/src/apps/ai_models/lib/formatToken.test.ts` — 5 pass.
- Items 3, 4, 6 are CSS/markup-only; no tests, per this round's own rule.

### To verify in the browser (round 6)

- A capability-scoped Hub search (e.g. embeddings) never shows a row
  whose own capability badge disagrees with the pane it was opened from.
- A `transformers`/`diffusers`/`sentence-transformers` repo's hit row now
  shows a format token instead of nothing.
- The stats cell is one right-aligned column (downloads, likes, updated
  each on their own line).
- The drawer's Close is a `×` in the top-right corner, not a footer link.
- Publisher and Quant are dropdown menus now (not free-text inputs),
  populated with the current search's actual values and counts; typing an
  unlisted value and pressing Enter still applies it.
- The Sort menu's dropdown opens fully on screen, anchored to its own
  right edge, never clipped by the scrolling pane.

### Fix round 7 (2026-09-11)

- Item 1: each control menu (Fit/Size/Sort `ControlMenu`, Quant/Publisher
  `SearchMenu`) used to own its own `open` state and its own outside-click
  listener, so opening one never closed another — live testing found up to
  four `.tp .dd` dropdowns open at once, with neither an outside click nor
  Escape closing any of them. `SearchControls` now owns a single
  `openMenu` id for the whole row, and one shared document `mousedown` +
  `keydown` listener (registered only while a menu is open) closes it on
  an outside click or Escape. Picking an option, or applying free text
  with Enter in a `SearchMenu`'s filter input, still closes that menu.
- Item 2: the drawer's `×` close button was a bare 8×12px glyph. Grown to
  a 24×24px flex-centered hit target with a quiet hover background,
  `top`/`right` nudged so the glyph's visual centre didn't shift.

### Round 7 test results

- `bun test frontend/src/apps/ai_models/local/` — 59 pass (6 new tests
  pinning the single-`openMenu` state, the outside-mousedown/Escape close,
  the listener's open-only lifecycle, and that picking/applying a
  `SearchMenu` option still closes via the shared `setOpen`).
- `bun run --cwd frontend typecheck` — clean.
- Item 2 is CSS-only; no test, per this round's own rule.

### To verify in the browser (round 7)

- Opening the Publisher menu, then Sort, then Quant, then Fit leaves only
  the last one open — never more than one `.tp .dd` visible at a time.
- A click/mousedown outside any open menu closes it; Escape closes it too.
- Typing in the Publisher/Quant filter input, or clicking inside a
  `.tp .dd`, does not close that menu.
- The drawer's `×` in the top-right corner is now comfortably clickable,
  not just the bare glyph, with a quiet hover highlight.

### Fix round 8 (2026-09-11)

- Item 1: the hit row's meta line dropped the leading task label
  (e.g. "speech recognition") — the search screen is already scoped by
  the left pane's capability (D843), so repeating it per row was
  redundant. Meta line now starts with params (`8.7B · BF16 · ≈16 GB ·
  Transformers`). `ModelRow.tsx` pane rows and the drawer's "Task" field
  are unchanged.

### Round 8 test results

- Markup-only change; no new test. Grepped both `HubSearchScreen.test.ts`
  and `hubSearchView.test.ts` for `metaParts`/`model.task` source-pinning
  — none found, nothing to update.
- `bun run --cwd frontend typecheck` — clean.

### Fix round 9 (2026-09-11)

- Item 1: dropped the downloaded-vs-not row background colour. The
  section headings ("On this Mac" / "Also worth having") already carry
  that distinction, so `.tp .row.have`'s `--am-surface-have` background
  was redundant; every row now uses `--am-surface-none`. The `have`
  class stays on the row (nothing else keyed off it), and the
  `--am-surface-have` token stays in `tokens.css` unused.
- Item 2: only a partially-downloaded repo now gets a yellowish row.
  Added `partial: boolean` to `ModelRowModel`, set from `resumable(repo)`
  in `diskRow()` (`LocalTab.tsx`) and `false` in `catalogRow()`. Row
  `className` appends `partial` next to `have`. New `.tp .row.partial`
  rule mixes `--warning` into background (8%) and border (45%) — same
  ratio as the existing warn chip/fit-tight rules, so it reads as the
  same faint amber in both themes.

### Round 9 test results

- UI-only round; no new tests. Grepped `frontend/src` for
  `\.row\.have`/`am-surface-have`/`ModelRowModel` — no other consumer of
  the deleted CSS rule, and neither `ModelRow.test.ts` nor
  `CapabilityPane.test.ts` constructs a `ModelRowModel` literal, so no
  test edits were needed.
- `bun test frontend/src/apps/ai_models/local/ModelRow.test.ts
  frontend/src/apps/ai_models/local/CapabilityPane.test.ts` — 23 pass,
  0 fail.
- `bun run --cwd frontend typecheck` — clean.
