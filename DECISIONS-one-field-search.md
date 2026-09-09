# Decisions — one field for paths and patterns

## What landed

- **Decision 8 (per-mode caps).** `listing/types.ts` gained
  `SEARCH_GLOB_RANK_LIMIT = 5_000`, kept separate from the unchanged
  `SEARCH_RANK_LIMIT = 200`. `useListingSearch.ts` picks which one to ask for
  with the same disambiguation the server uses (`q.includes("*")` mirrors
  `resolve_query`'s own `"*" in raw`) — a heuristic, not authoritative; the
  settled answer's own `res.mode` from the wire is what everything downstream
  actually keys on. `fused_render/index/query.py` gained
  `MAX_GLOB_RANK_LIMIT = 5_000`, and `search_ranked`'s limit clamp picks
  between it and the untouched `MAX_RANK_LIMIT` (2,000) on `glob`.
- **Decision 9 (full render).** `listing/result-cap.ts`'s `capHits` and
  `resultCountLabel` both take the answer's `mode`. A substring answer keeps
  the existing top-100 display cap; a glob answer's cap IS the fetch limit —
  `capHits` returns the array unsliced, so `visibleHits === displayHits` for
  every fetched glob hit, and the count chip never says "Showing top N of"
  for one. `RankAnswer.mode` (new field, threaded from `res.mode`) is the
  single addition that makes this possible without a second cap concept.
- **Decision 10 (select-all's shortfall message).** No change to
  `selectAllRows` itself — it already selects `navRowsRef.current`, which is
  `visibleHits`, which decision 9 just made equal to the whole fetched set.
  What was missing was the honesty: Listing.tsx now computes
  `selectionShortfall` (searching, the answer is truncated, and the current
  selection equals every visible row) and swaps the "N selected" chip for
  "N selected of M+" only in that exact case — a plain select-all inside the
  ceiling still reads as a plain count.
- **Decision 12 (copy progress + cancellation).** `useFileOps.ts`'s `doPaste`
  pushes one standing toast for a multi-file COPY (`op === "copy" &&
  paths.length > 1`), updated in place each iteration via `pushToast`'s
  `replaceId`, with a Cancel action that flips a ref checked at the top of
  the loop. Cancelling breaks the loop before the next `copyEntry` call;
  every file already copied is left exactly where it landed — there is no
  rollback, matching the rest of `doPaste`'s partial-failure handling. Cut
  and single-file copy show no progress toast (a rename is already
  near-instant, and there is nothing to report progress about for one file).

Tests: `frontend/src/apps/explorer/listing/result-cap.test.ts` (glob-mode
cases added), `tests/test_index_rank.py::test_glob_mode_clamps_to_its_own_wider_ceiling`
(new). `useFileOps.ts`'s copy-progress path has no dedicated test — there was
no existing test harness for `doPaste` in this codebase to extend (it has no
`useFileOps.test.ts` at all), and standing one up (mocking `copyEntry`,
`statPath`, `freePastePath` and the toast store together) was judged out of
budget for this pass. See "Deferred" below.

## Deferred: decisions 1–7 (the one-field UI merge)

Not built. This is the largest piece of the spec and the riskiest to rush:
`Breadcrumb.tsx` (938 lines) and `Listing.tsx` (2,156 lines) are both dense,
heavily-commented, single-purpose files, and today they carry two genuinely
separate inputs —

- `Breadcrumb.tsx`'s `editing` state (`crumb-edit` input, `submitEdit` at
  :756) — a path field, click-to-edit over the whole bar, Ctrl/Cmd+L,
  click-away dismissal, spring-load drag interaction with the crumb strip.
- `Listing.tsx`'s `.listing-search` box (:1773–1966, portalled into the
  breadcrumb bar via `FolderSearchSlot`) — the live-filter query field,
  wired to `useListingSearch`, with its own fold/pin/expand geometry keyed
  off `tightBar`/`pinnedOpen`/`searching`.

Merging these into the single field decision 1 describes means deciding, for
every one of the breadcrumb's existing behaviors above, whether it now lives
on the search input instead — the click-to-edit affordance, the spring-load
drag target, Ctrl+L, the crumbs-when-empty display — while preserving the
Cmd+A-selects-query-text guard and the pending-branch code the build brief
called out by name. That is a redesign of both files' interaction model, not
an additive change, and attempting it inside this pass's remaining budget
risked leaving the merge half-done AND regressing one of the two things
explicitly protected. The judgment call: ship the four decisions that were
tractable and directly serve the demo's back half (see every match, select
all, copy with progress) and land the field merge as its own follow-up pass
with a full budget, rather than a rushed diff across the two largest files in
the app.

What a resuming builder needs to know:

- The demo's `~/path/*/*.xyz` query already reaches the server and searches
  correctly through today's `.listing-search` box — the query language merge
  landed on this branch before this spec started, and typing a glob directly
  into the existing search field already resolves, fetches (now up to 5,000),
  and renders in full per decision 9. What decision 1 is missing is the
  VISUAL merge (one field, breadcrumbs shown empty) and decisions 2/4/5/6/7
  (completion, the Enter-only gate for `/`/`*` queries, three-way Enter
  resolution, and the address/history behavior) layered on top of it.
- Decision 4 (Enter-gate for `/`/`*`) is not built: `.listing-search-input`
  still fires the debounced live-filter fetch on every keystroke regardless
  of content. A half-typed glob (`~/big/*`) will search on each keystroke
  today, which is the exact perf problem decision 4 exists to prevent — it
  just hasn't bitten yet because SEARCH_GLOB_RANK_LIMIT is 5,000 rows, not
  the "thousands of folders" case the spec worries about.
- Decision 5 (Enter resolves 3 ways) is not built: there is no Enter handler
  on the search input at all beyond the existing Escape-to-clear. Enter today
  does nothing special in the search box; the answer already reaching the
  client (`res.base`, `res.mode`) has what a three-way resolver would need
  (an exact folder vs. file vs. "search" outcome), but nothing reads it for
  this purpose yet.
- Decisions 6/7 (address/history) are not built: there is no code that pushes
  a history entry when a resolved base changes, and no code that leaves the
  user "at the resolved base" on clear. `Breadcrumb.tsx`'s `navigate()` calls
  are the existing pattern to extend once decision 5's resolution exists to
  drive them.
- Decision 2 (completion dropdown) is not built at all — no dropdown
  component exists yet reading `listDir` (`platform/lib/api.ts:473-477`) for
  path-segment completion.

## Deferred edge cases

- Empty query, a bare `~`, a lone `/`, unusual characters in a glob, and the
  5,000-row ceiling actually being hit end-to-end (as opposed to the unit
  test pinning the clamp) — all per the spec's own explicit deferral list,
  untouched here.
- `doPaste`'s copy-progress toast has no automated test. A resuming builder
  who wants coverage here will need to stand up a `useFileOps.test.ts`
  harness first (there is none today) — mock `copyEntry`/`statPath`/
  `freePastePath` from `@platform/lib/api` and `@apps/explorer/lib/fs-actions`,
  and assert on `getToasts()` from the toast store across a fake multi-file
  clipboard.
- The copy-progress toast's message is plain text ("Copying N of M…"); it
  does not attempt a percentage bar or ETA. Given decision 12's own wording
  ("reports progress and can be cancelled"), a count is the honest minimum
  and was judged sufficient for the demoable state.
- Cut (rename) paste and single-file copy paste show no progress UI at all,
  by design — see "What landed" above. If a very large CUT batch turns out
  to be slow in practice (e.g. across filesystems where rename is not O(1)),
  that would need its own decision; nothing here assumes rename is always
  instant, only that it usually is.
