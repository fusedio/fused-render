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

## What landed — decisions 1–7 (the one-field UI merge)

Built in a follow-up pass, after the deferral logged below. `Listing.tsx`'s
`.listing-search` box is now the one field decision 1 describes; `Breadcrumb.tsx`
keeps its own `editing`/spring-load/Ctrl+L path exactly as it was — the merge
turned out to be scoped to the search box absorbing the crumbs' resting
display, not the other way around, so `Breadcrumb.tsx` needed no interaction-
model changes at all.

- **Decision 1 (one field, magnifier, crumbs-when-empty).** `PathCrumbs`
  (`listing/path-crumbs.tsx`) renders inside `.listing-search-box` behind the
  input, shown only at `query === "" && !pinnedOpen` — resting, unfocused,
  and empty. The magnifier glyph is absolutely positioned at the field's left
  edge, `pointer-events: none`, so a press on it falls through to the input
  underneath (the same click-through trick the crumbs use for their own
  non-link area). A narrow field keeps the CURRENT folder segment readable
  by pinning `PathCrumbs`'s own `scrollLeft` to its end via `ResizeObserver`,
  rather than right-aligning the text — the tail of a long path matters more
  than its root.

  A third state exists beyond "resting" and "has a query": **focused and
  still empty**. Crumbs read as a row of links, and links sitting where a
  caret is about to type read as editable content the first keystroke is
  about to destroy — worse, they say nothing about the field accepting more
  than a filename. Focusing with nothing typed now hides the crumbs (the
  same `!pinnedOpen` condition) and shows a placeholder hint instead:
  `"Search, or type a path or pattern like ~/work/*/*.csv"`, or the shorter
  `"Search, or type a path or pattern"` once `ResizeObserver`-measured box
  width drops below where the long form would clip mid-example
  (`Listing.tsx`'s `boxWide`/`HINT_WIDE_PX`). The accent border, ring, and
  glyph color that mark a query as "live" apply here too — focusing IS the
  state change, not a wait for the first character (`.listing-search.expanded`
  in `explorer.css`, alongside `.searching` for a non-empty query).

  Two lessons from getting here, for whoever touches this again:
  - **Don't share CSS classes across visually-similar-but-distinct elements.**
    `PathCrumbs` used to render the plain (unclaimed-folder) crumb strip's own
    `crumbs` class, purely to borrow its look. Every `.crumbs`/`#breadcrumb
    .crumbs` rule in `explorer.css` — several written only for that other,
    mutually-exclusive strip — matched the in-field strip too, at a
    specificity `.listing-search-crumbs` alone could never out-rank, and one
    of those accidental matches happened to be silently hiding the strip on
    focus (a `:has(.listing-search.expanded) .crumbs` rule). Removing the
    shared class without first checking why the old behavior worked briefly
    reintroduced the crumbs-behind-the-caret bug (see below) before it was
    caught. `PathCrumbs` now declares everything it needs — the monospace
    face, left alignment, hidden scrollbar — directly on its own
    `.listing-search-crumbs` class, self-sufficient.
  - **A silently-working mechanism is not dead code just because a later
    rename makes it unreachable.** When `PathCrumbs` stopped sharing
    `.crumbs`, the `:has(.listing-search.expanded) .crumbs` rule became
    genuinely unreachable and was deleted as such — correctly, for the
    post-rename architecture, but without noticing it had been the ONLY
    thing hiding the crumbs on focus. The fix is a plain JSX condition
    (`query === "" && !pinnedOpen`) rather than a reconstructed `:has()`
    selector: there is no longer a hidden CSS mechanism for a future rename
    to quietly stop matching.
  - Two deliberate deviations from the agreed visual mock
    (`explorer-search-ux-report.html`), kept on the owner's call and logged
    here so neither gets "fixed" back toward the mock later: the tail-pin
    scroll above (mock uses an ellipsis instead), and `/` and `~` as the
    literal separators/home marker (mock uses `›` and the word "Home").
- **Decision 2 (completion dropdown).** `completion-target.ts`'s
  `completionTarget` (pure) splits a path-shaped query into a directory to
  list and a partial name to narrow it by — the same exclusions decision 5's
  `listing-address.ts` makes (a glob or a bare filter word gets no dropdown
  at all). `useCompletion.ts` is the debounced `listDir` behind it, keyed on
  the directory rather than the whole query: a keystroke that only moves the
  partial narrows the same fetched page client-side, and a trailing `/`
  (a new directory) is what actually re-fetches. The dropdown itself renders
  in `Listing.tsx` right after the input as `.listing-completion`, header
  ("In ~/work/data", `completion-target.ts`'s `displayDir`) plus one row per
  entry (name, and a right-aligned "folder" or file-size hint via the
  existing `formatSize`). Down/Up cycle a highlighted row (wrapping), Enter
  takes it (writes the resolved path back, trailing `/` on a directory so
  the dropdown moves straight into it instead of closing on a segment that
  isn't a destination yet), Escape returns to crumbs via the handler
  decision 1 already needed. Rows take the mouse on `mousedown` rather than
  `click`, since `click` fires after the input's own blur — which, for an
  empty field, has already folded the box and unmounted the row under the
  pointer by the time `click` would land.
- **Decisions 6 and 7 (address follows the resolved base; no Back-stack
  pollution while typing).** No new code — the existing architecture already
  satisfies both. `navigate()` (decision 5's Enter handler, and the existing
  row-click handler) is the ONLY thing in `Listing.tsx` that ever calls
  through to the router; typing into the field only calls `setQuery`, never
  `navigate`. So a resolved base can only ever change on an explicit Enter
  or click — one history entry, exactly when the base changes — and a half-
  typed path can never push anything, because nothing fires on keystrokes at
  all. Clearing the field is likewise inert (`onChange` only sets `query`),
  so the user is left exactly where they already were: the resolved base,
  not automatically routed anywhere else. Decisions 6/7 turned out to be a
  property of decision 5's design (navigate only on an explicit resolve),
  not something requiring separate wiring.

Decisions 4 and 5 were already committed in the prior pass on this branch
(see git history: "Explorer: gate ranked search on Enter for a path-shaped
query" and the Enter-handler work in `Listing.tsx`/`listing-address.ts`/
`useTypedPathAddress.ts`) — decision 4's Enter-only gate for `/`/`*` queries
lives in `useListingSearch`'s `pathLike`/`commitSearch`, and decision 5's
three-way resolution is the input's `onKeyDown` Enter branch, reading
`useTypedPathAddress`'s `status` (`"exists"` navigates with `is_dir`;
anything else falls through to `commitSearch` when `pathLike`). A real file
gets the same `navigate()` a real folder does rather than a separate
"Open"-style row above the results — the destination view's own stat already
handles opening a file target, so a second code path to do the same thing
was judged unnecessary for this pass.

Tests: `frontend/src/apps/explorer/listing/completion-target.test.ts` (new),
`listing-address.test.ts` (from the prior pass, now tracked),
`search-bar-expand.test.ts` (updated for the `.crumbs`-class decoupling —
one dead rule's assertion removed, since a claimed folder never renders
`.crumbs` at all post-merge). No dedicated render test for the completion
dropdown's keyboard contract (Down/Up/Enter/Escape) or the focused-empty
placeholder swap — both are exercised only by the pure functions underneath
them (`completion-target.test.ts`) and by hand/visual review; see "Deferred"
below.

## Deferred edge cases

- The completion dropdown and the focused-empty placeholder state have no
  render-test coverage of their own (no `@testing-library/react` harness for
  `Listing.tsx`'s search box exists on this branch to extend) — the pure
  logic underneath both (`completion-target.ts`, the `boxWide` threshold) is
  covered, but the wiring (Down/Up wraparound, Enter-takes-highlighted-row,
  the crumbs↔placeholder swap on focus/blur) was verified by hand and by
  code/CSS reasoning at two widths (~1400px and ~900px), not in a browser —
  no browser tool was available to this pass. A resuming builder who wants
  this locked down should stand up that harness rather than trust the
  reasoning here indefinitely.
- The dropdown's `z-index: 5` is defensive, not load-bearing today: neither
  `#breadcrumb` nor the listing table currently establishes its own
  stacking context, so the dropdown (a positioned descendant) already paints
  above both regardless. If either ever picks up `position`/`transform`/
  `isolation` for unrelated reasons, the z-index is what keeps the dropdown
  on top — but it has not been exercised against a real competing stacking
  context.
- `~` alone, a lone `/`, and unusual characters in a glob passed to the
  completion dropdown — deferred per the spec's own explicit list, untouched
  here.
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
