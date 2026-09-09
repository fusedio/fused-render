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

## What landed — browser-review fixups

Three items came back from an in-browser pass at 1400x900 and 900x700.

- **Current-folder crumb was a click dead zone (defect).** `explorer.css`'s
  `.listing-search-crumbs .path-crumb` rule re-enabled `pointer-events: auto`
  on every `.path-crumb`, but `path-crumbs.tsx` renders the LAST segment (the
  current folder) as a plain `<span class="path-crumb last">` with no click
  handler — parent segments are `<a class="path-crumb">`. The span caught the
  press, did nothing (no handler), and did not let it fall through to the
  input beneath (pointer-events had been turned back on), so a click on the
  current folder's name was silently swallowed. Worst at narrow widths,
  where the tail-pin scrolls the strip to its end and the current folder
  fills most of the visible field. Fixed by splitting the rule into
  `.listing-search-crumbs a.path-crumb` (pointer-events: auto — the segments
  that actually navigate) and `.listing-search-crumbs span.path-crumb` (no
  pointer-events override, so it inherits `none` from the strip and a press
  falls through to the input, same trick the magnifier already uses). No
  click handler was added to the last segment — navigating to the folder you
  are already in is a no-op, so there is nothing for a handler to do.
  Verified both still hold: a parent crumb navigates, the current-folder
  crumb focuses the field.

- **Magnifier accent-on-focus (polish) — already landed, no change made.**
  The brief asked for the glyph to go accent alongside the border in the
  live states. `explorer.css` already had
  `.crumb-search-slot .listing-search.expanded .listing-search-glyph,
  .crumb-search-slot .listing-search.searching .listing-search-glyph { color:
  var(--accent); }` from the decision-1/2 pass (commit `3d3829e6`), reusing
  the exact `.expanded`/`.searching` states the border rule keys off, and it
  is present in the built bundle (`grep`-confirmed in the shipped CSS). No
  competing rule sets `color` on `.listing-search-glyph` after it, and the
  SVG's `stroke="currentColor"` inherits from the span. Left untouched;
  logged here so the built-and-shipped state is on record and this item
  reads as resolved rather than silently dropped.

- **Completion write-back preserved the typed notation (polish).**
  `useCompletion.ts`'s `CompletionItem.path` used to always build an
  absolute path (`base + "/" + e.name + …`), so taking a row after typing
  `~/Work/fusedudfs/ai` rewrote the field to
  `/home/iamsdas/Work/fusedudfs/ai_utils/` — a visible notation jump
  mid-typing. `completion-target.ts` gained `applyQueryNotation(absPath,
  query, fsPath, home)`: a tilde query (`"~"` or `"~/…"`) stays tilde
  (reusing `displayDir`'s own `~` substitution rather than a second one), an
  absolute query (`"/…"`) stays absolute, and anything else is re-relativized
  against `fsPath`, the same base `completionTarget` resolves a relative
  query against going the other direction. Every branch is a string slice
  off one end, so the trailing `/` decision 2 already relies on (it moves
  the dropdown into the folder) survives untouched in all three notations.
  `useCompletion.ts` now runs each row's built absolute path through this
  before assigning it to `CompletionItem.path`.

  Tests: `completion-target.test.ts` gained an `applyQueryNotation` describe
  block — tilde round-trip, bare `~`, a tilde query whose resolved path falls
  outside home (defers to `displayDir`'s own absolute fallback), absolute
  round-trip, relative round-trip, a relative query resolving to the search
  root itself (writes back `""`), and the trailing-slash-survives check
  across notations.

## What landed — items 4 through 10 (dropdown behavior and the search chip)

Six more items came back, four of them behavioral bugs rather than polish.
Verified live at `localhost:2021` (`agent-browser`) against this worktree's
own build, in addition to `bunx tsc --noEmit` and the scoped test run.

- **Item 4 — the dropdown is capped at 5 visible rows, scrolling for the
  rest.** `MAX_ITEMS` (50, `useCompletion.ts`) is unchanged — it still bounds
  what is FETCHED and rendered into the DOM. What is capped here is how much
  of that list is visible at once. The rows moved into their own
  `.listing-completion-rows` wrapper (`overflow-y: auto`, native scrollbar
  hidden via `scrollbar-width: none` / the WebKit pseudo-element, same
  pattern the rest of this file already uses to hide a scrollbar it wants to
  keep functional but not draw) as a SIBLING of `.listing-completion-header`,
  not a child of it — the header had to stay put while only the rows
  scrolled. The 5-row height comes from a `useLayoutEffect` measuring the
  first rendered row's real `offsetHeight` (`firstRowRef`) rather than a
  guessed pixel constant, so it tracks whatever the CSS actually renders a
  row at; `rowsMaxHeight` is only set once there are more than 5 items,
  otherwise the container sizes to its content (a 3-entry folder shows 3
  rows, no dead space). A second `useLayoutEffect` calls
  `scrollIntoView({ block: "nearest" })` on the highlighted row whenever
  `highlight` changes, so ArrowDown/ArrowUp past the visible 5 scrolls the
  highlight into view without over-scrolling once it's already visible.
  Verified in the browser: `~/` at 1400×900 shows exactly 5 rows with a 6th
  visibly clipped underneath.

- **Items 5 and 7 — Enter's meaning in the dropdown, and its final split
  from Tab.** These landed together because item 7 revises item 5 before
  item 5 ever shipped on its own; the account below is the FINAL behavior,
  not the intermediate one.

  The bug (item 5): `highlight` reset to `0` on every list change, so the
  dropdown's `enter-accept` gate (`highlight >= 0`) was true the INSTANT a
  dropdown rendered, before the user had touched an arrow key. That made
  Enter on a fully-typed real folder path silently take row 0 of the
  dropdown instead of navigating, and whether it did depended on typing
  speed relative to the dropdown's own open debounce — the same keystroke,
  two different outcomes. Fixed by resetting `highlight` to `-1`
  (unselected) instead of `0`; `moveHighlight` in the new
  `listing/completion-keys.ts` special-cases `-1` explicitly (ArrowDown → 0,
  ArrowUp → last row) rather than trusting the general wraparound formula,
  which gives the wrong answer for ArrowUp from `-1` (see that file's
  comment). **This `-1` starting point is load-bearing — do not change it
  back to `0`.** Reverting it silently reintroduces the timing-dependent
  Enter bug this decision exists to close.

  The split (item 7, superseding decision 5's original text — see below):
  **Tab completes text only.** It fills the field with the row's `path`
  (notation-preserving, decision 3) and lets the dropdown re-key on the new
  directory; it never navigates. **Enter navigates.** An explicitly
  highlighted row (the user arrowed to it) is taken as a DESTINATION —
  `navigateToCompletion` calls `navigate(item.absPath, { isDir: item.is_dir
  })` and then clears the query (`setQuery("")`), so the field shows crumbs
  for the folder just arrived at instead of a stale query sitting next to a
  now-pointless open dropdown. The reason for the split is history churn,
  not taste: `platform/lib/router.ts`'s `navigate` has no history-replace
  option, so if Tab navigated per keystroke, walking three directory
  segments by Tab would push three entries onto the Back stack for what is,
  from the user's point of view, one act of typing a path. Tab staying
  text-only means only Enter — one deliberate commit — ever touches
  history. Nothing highlighted (dropdown closed, or open with nothing
  arrowed to) still falls through to decision 5's original `typedAddress`
  resolve-and-navigate, then decision 4's search commit, unchanged.

  **This supersedes decision 2's "a directory row's path is not a
  destination yet" framing above** — that was true when accepting a row
  only ever filled text (both Tab and Enter did). It is no longer true for
  Enter: an explicitly highlighted row's `absPath` IS the destination Enter
  hands to `navigate()`. It stays true for Tab, which is exactly the split's
  point.

  A row's own click does exactly what Tab does (fills text, does not
  navigate) via one shared `acceptCompletion` function both call, so the two
  cannot drift apart — a row's mousedown already calls
  `e.preventDefault()` to keep focus on the input (needed for item 6 below
  to not fire on a row click), and Tab's key handler and the click handler
  both go through the same function afterward.

  Tests (`completion-keys.test.ts`): Enter with no highlight passes through
  whether the dropdown is showing or not; Enter after an ArrowDown accepts
  the highlighted row; Tab accepts the first row with nothing highlighted
  and the highlighted row when one is arrowed to; Tab and Enter deliberately
  disagree with nothing highlighted; a zero-item dropdown behaves as not
  showing at all. `moveHighlight`'s own tests cover both wraparound
  directions and both `-1`-start directions separately, since a first,
  wrong implementation (plain modulo) passed the Down case by coincidence
  and failed the Up case — see completion-keys.ts's own comment.

- **Item 6 — the dropdown outlived focus leaving the field.** Clicking
  anywhere else in the app while a path-shaped query sat in the field left
  the dropdown open and pinned over whatever was now focused, still naming
  the last-typed path. Fixed with a new `fieldActive` boolean, set
  unconditionally in the input's `onFocus`/`onBlur` — deliberately separate
  from `pinnedOpen`, which intentionally OUTLIVES a blur once there is a
  query (that's what keeps the crumb strip expanded and the border lit
  after focus moves on; the dropdown needs the opposite). `showCompletion`
  is now `fieldActive && completion.target !== null && completion.items.length
  > 0` (plus item 9's exact-match suppression below). A row's `onMouseDown`
  already called `e.preventDefault()` before this change specifically to
  keep the input focused through a click — that guard is what stops this
  fix from closing the dropdown out from under its own row click. Refocusing
  the field and retyping re-opens it correctly, since dismissal and
  reopening are both just "does focus match", nothing to separately
  suppress or resurrect.

- **Item 8 — Enter did not open a highlighted search result for a plain
  search word.** The input's `onKeyDown` called `e.preventDefault()` the
  instant `e.key === "Enter"`, before checking whether any branch would
  actually act. For a plain filter word (no `/`, no `*`) none of this
  file's branches act on Enter — but the `preventDefault()` had already
  fired, and `useListingSelection.ts`'s document-level Enter handler (which
  is what actually opens the top/highlighted search hit while the search
  box has focus) starts with `if (e.defaultPrevented) return;` and bailed
  every time. Fixed by moving `preventDefault()` OUT of the top of the Enter
  branch and INTO only the branches that actually act: the completion
  dropdown's `move`/`tab-accept`/`enter-accept` cases (unchanged, they
  already did this), the `typedAddress.status === "exists"` branch, and the
  `pathLike` → `commitSearch()` branch. The genuine passthrough case — not
  path-shaped, not a resolved address, dropdown not claiming the key — now
  calls `preventDefault()` nowhere, so the event reaches
  `useListingSelection.ts` un-prevented and its own guard (`leadIdx === -1 &&
  !rowsAnswerQueryRef.current` → bail, so stale rows never get opened by
  accident) decides whether to open row 0 or the arrowed row. `completion-
  keys.ts`'s `enter-passthrough` action was already the right shape for this
  — the fix was entirely about the CALLER never calling `preventDefault()`
  for it, not about the action type itself. Escape's unconditional
  `preventDefault()` in the same handler is untouched; it is a documented,
  separate contract (clipboard-cancel precedence) this fix does not touch.
  Verified in the browser: typing "ai_utils" (matching a real folder) and
  pressing Enter navigated into it — no arrowing needed, top hit taken.

- **Item 9 — the "Press Enter to search" prompt lied once the field held a
  resolved path, and a completion row could be pointless.** The prompt shown
  while a path/pattern query sits uncommitted (decision 4's gate) was a flat
  constant, derived only from that gate and never from decision 5's
  `typedAddress` — which is what Enter actually checks FIRST (item 8's fix
  above didn't change that order). A complete, real folder in the field
  showed "Press Enter to search" right next to a dropdown naming the exact
  folder Enter was about to open. New pure function `listing/enter-
  prompt.ts`'s `enterPrompt(typedAddress)` reads the same `TypedAddress` the
  Enter handler branches on, so the two cannot disagree: `status ===
  "exists"` names the resolved file or folder ("Press Enter to open
  ai_utils"); `"checking"` keeps the search wording rather than flashing a
  third state mid-resolve (Enter falls through to the search commit while
  unresolved, so that wording is also what's actually true); `"idle"` and
  `"missing"` keep it too, unchanged from before.

  Also fixed while in there: a dropdown holding exactly one row whose name
  is already an exact match for the typed partial has nothing left to offer
  — the user finished typing that segment — and it was the panel sitting
  over the prompt in the screenshot that reported this bug. New
  `completion-target.ts` export `isExactSingleMatch(items, target)` (kept
  structural — `{ name: string }[]` — rather than importing
  `CompletionItem`, so `completion-target.ts` stays a leaf `useCompletion.ts`
  depends on, not the reverse) folds into `showCompletion` alongside the
  existing `fieldActive` gate from item 6.

  Tests: `enter-prompt.test.ts` covers all four `TypedAddress` states, both
  file and folder naming for `"exists"`. `completion-target.test.ts` gained
  an `isExactSingleMatch` describe block: the redundant single-exact-match
  case, a single row that only prefixes the partial (still has more to
  type, not redundant), multiple rows including an exact match among them
  (never redundant — the OTHER rows are still useful), no target, and zero
  rows.

- **Item 10 — search timing next to the match count.** Reused
  `formatElapsed` from `apps/explorer/lib/home-search.ts` rather than
  writing a second one — same "42 ms" under a second / "1.2 s" at or above
  it. `useListingSearch.ts`'s `RankAnswer` gained an `elapsedMs` field,
  measured the same way `home-search.ts` measures its own (`Date.now()` at
  request issue to `Date.now()` when the response is applied — end-to-end
  latency the user actually felt), and `SearchState`'s `"ok"` variant
  (`listing/types.ts`) carries it through. The measurement is baked into the
  `RankAnswer` object at fetch time and never recomputed: a query answered
  from `memo.current.get(q)` hands back that same object, so a cache hit
  reports the real cost of the request that actually produced it rather
  than ~0 ms for a reply that just came from memory — the same rule
  `home-search.ts`'s own doc comment states for its held answers.

  `Listing.tsx` appends `" · " + formatElapsed(...)` to both the terse chip
  text and `searchCountFull` (the title/aria-label sentence), but only in a
  new `else` branch alongside the existing scan-caveat fold — i.e. only once
  `caveat === null` AND `searchState.status === "ok"` AND a count is already
  present. `behind` (a stale count) always produces a caveat
  (`index-caveat.ts`'s `indexCaveat` returns non-null whenever `behind` is
  true), so gating on "no caveat" is sufficient to keep timing off a stale
  count without a second `behind` check — a stale count paired with a fresh
  latency figure would describe two different requests, which is exactly
  the case being avoided. This branch also sets `widePin = true`, reusing
  the existing wider chip reservation (`.wide-pin`, 210px) that was already
  sized for the longest scan-caveat text ("building index… 12,345 files",
  29 characters) — comfortably wider than the longest settled count-plus-
  timing string ("top 100 of 4.9K+ · 1.2 s", 25 characters), so no new CSS
  rule was needed. Verified in the browser: a plain 2-character query shows
  "1 · 68 ms"; a `~/` dropdown query (uncommitted, `not refreshed`) shows no
  timing, confirming the `behind`-gates-via-caveat path.

  Tests: `useListingSearch.render.test.ts` gained a "decision 10" describe
  block using the file's existing fake `Clock` (it already controls
  `Date.now()` deterministically) — one test advances the clock between
  issuing a request and resolving it and asserts `searchState.elapsedMs`
  equals exactly that gap; a second establishes a 400ms-measured answer,
  moves on to a different query, then returns to the first and asserts the
  answer served back from the memo (`rankCalls.length` unchanged — no new
  request) still reports 400ms rather than being re-timed to ~0ms.

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

## What landed — review-round fixups

A round of code review turned up 14 items; all are fixed on this branch.

- Windows drive-letter paths (`C:/Users/me`, backslashes folded to `/`, a
  bare drive root keeping its own slash) are now a recognized candidate in
  both `listing-address.ts` and `completion-target.ts`, mirroring the
  server's `_DRIVE_ABS` — covered by new tests in both files' `.test.ts`.
- The `.has-pin` search box no longer collapses to a fixed 260px: that rule
  is gone, so a pin (the multi-selection readout or the streaming spinner)
  reserves space as padding instead of shrinking the box, and the crumbs —
  not the box — give up room first.
- `requestSearchFocus()` is now gated on `barChrome`: a `<Listing>` mounted
  without bar chrome (the preview pane) no longer steals focus into its own
  field when a click on the claimed bar's crumb asks some `Listing` to focus.
- The dropdown's `rowsMaxHeight` effect now has `fieldActive` in its
  dependency array, so refocusing a field whose query and target directory
  never changed (the dropdown itself unmounts on blur) recomputes the cap
  instead of reusing a stale or absent one.
- The five-row completion dropdown now caps at 5.5 rows (was 5), a
  deliberate half-row "there is more, scroll" cue; the CSS fallback
  `max-height` was recomputed to match (132px, up from 120px).
- The tail-pin layout effect in `path-crumbs.tsx` and the `boxWide`
  measurement effect in `Listing.tsx` both had no dependency array; the
  first now re-pins on `[fsPath, home]` (the ResizeObserver still handles
  resize-driven re-pinning on its own), the second is `[]` since the
  ResizeObserver it attaches is what tracks width from then on.
- The stranded `cursor: pointer` on the search field's last (non-clickable)
  crumb is overridden back to `default` on the one selector that targets it.
- The Enter gate in `useListingSearch.ts` committed against the
  `useDeferredValue`d query, not the live one — under load, a commit
  recorded against a still-trailing deferred value stopped matching once
  the deferred value caught up a moment later, requiring a second Enter to
  actually open the gate. It now commits the live, trimmed `query`, so the
  gate opens as soon as the deferred value reaches it.
- A comment-hygiene pass removed "used to" / "before D***" history
  narration from comments this branch's diff actually added, across
  `explorer.css`, `Breadcrumb.tsx`, `FilesHome.tsx`, `Listing.tsx`,
  `path-crumbs.tsx`, `enter-prompt.ts`, `useListingSearch.ts`,
  `platform/lib/api.ts`, and `fused_render/index/query.py`. The sweep was
  scoped to added diff lines (`git diff main...HEAD`), not whole-file
  content: the wider codebase already narrates history in comments as
  house style outside this PR's own additions, and rewriting those would
  have been well outside this review round's scope.

Two deviations from a strict TDD loop, both because the tooling does not
support the test:

- The focus-gating fix (`requestSearchFocus`/`barChrome`) and the dropdown
  `rowsMaxHeight`/`boxWide` effect-dependency fixes are React component
  wiring inside `Listing.tsx`, which this codebase has no render-test
  harness for (no `@testing-library/react`, `jsdom`, or `happy-dom` — only
  extracted pure functions/hooks are unit-tested here, via `bun:test` and
  `hook-harness.ts`). Verified instead by `tsc --noEmit` and by the
  existing `Listing.test.tsx` / `FilesHome.render.test.tsx` /
  `useListingSearch.render.test.ts` suites passing unchanged.
- The Enter-gate fix could not get a new automated test either, for a more
  specific reason: `hook-harness.ts`'s `act()` (from `react-test-renderer`)
  flushes `useDeferredValue`'s low-priority re-render synchronously within
  the same `act()` call — confirmed by a throwaway probe test before
  writing the fix. That means `q` (deferred) and `query.trim()` (live) can
  never actually differ at any point this harness lets a test observe, so
  the exact race the bug depended on is not reproducible in this test
  environment at all. The fix was verified by reasoning about the
  closure-capture bug directly, plus the existing commit/gate tests in
  `useListingSearch.render.test.ts` passing unchanged.

## Decision 4 revisited: the gate keys on base, not syntax

The gate that decides whether Enter is required moved off `q.includes("/")
|| q.includes("*")` and onto a new pure predicate, `escapesBase` in
`listing/query-base.ts`. The syntactic test made an in-folder glob like
`*/*.json` demand Enter even though it never leaves the folder being
searched — a slash inside a query only limits how deep the pattern reaches
(SPEC-one-search-language.md's `**/` rule), it does not by itself relocate
the base. `escapesBase` answers the narrower, actually-relevant question:
can this query's base differ from the box's own root? Yes only for a
leading `~` (alone or `~/`), a leading `/`, a drive letter, or a `..`
segment; everything else — `*/*.json`, `data/2024`, `**/*.csv`, `.csv` —
is relative to the box root and live-filters like plain text.

The predicate is deliberately conservative about a leading `/`: server-side
(`fused_render/index/query.py`) a leading `/` is ambiguous between an
absolute path and a depth-1 anchor at the box root, resolved only by
walking the filesystem. `escapesBase` cannot do that synchronously and does
not try to — it treats every leading `/` as escaping, which gates the
expensive case (a genuine absolute path) and costs one extra keypress on
the cheap one (`/foo` used as an anchor).

`escapesBase` lives in its own module rather than inside the hook so
`FilesHome.tsx`'s box — which has no commit gate at all — can adopt it
later without a file move. It is not wired into `FilesHome.tsx` in this
round. `completion-target.ts` remains the sibling that does the full
three-notation resolution (`~`, `/`, drive letters) down to a real
directory + partial; `escapesBase` answers only the yes/no question and,
on purpose, needs neither `fsPath` nor `home` to do it.

`useListingSearch.render.test.ts`'s decision-4 describe block was updated
rather than left pointing at the old syntactic rule: its glob-gates-until-
commit example changed from the box-relative `a/*.py` (which no longer
gates) to the escaping `~/a/*.py`, and a new case confirms a box-anchored
glob like `a/*.py` fires on every debounced keystroke same as plain text.

One incidental finding while sweeping comments: the sandboxed shell's
`grep` (a wrapper invoking `ugrep -I`, which skips files its heuristics
classify as binary) silently returns no matches on
`useListingSearch.ts`, which contains one legitimate embedded NUL byte
(a cache-key join separator, `.join("\x00")`). `command grep -a` (bypassing
the wrapper, forcing text mode) works correctly. A chained pipe
(`grep ... | grep ...`) over a diff dump containing this file's content
was also observed to silently drop matches that a direct grep on the same
dump found. Anyone re-sweeping this branch's comments should prefer
`command grep -a` or the Read tool over piped greps on diff dumps.

## Completion dropdown surface treatment

The user reported the completion dropdown "looks bad and out of place."
DOM measurements showed the panel's geometry already tracked the input
field's edges exactly, so every fix here is surface treatment, not layout,
in `frontend/src/styles/explorer.css`.

- **Neutral border, not accent.** `.listing-completion` used
  `border: 1px solid var(--accent)`, but this file reserves accent for a
  control's live state (the focused search input gets an accent border plus
  a 3px glow). A passive suggestion list repeating that treatment put two
  equally-loud accent rectangles on screen and drew the eye to a border
  instead of the results. Changed to `var(--border)`, matching every other
  menu surface in the app (`context-menu.css:15-17`).
- **Row/header left padding matches the input's text gutter.** The dropdown
  rows padded 11px on the left while the input they complete pads 28px
  (`padding-left: 28px` on `.listing-search .listing-search-input`, clearing
  the magnifier glyph), so suggestion text sat 17px left of the query text
  it completes. Both `.listing-completion-row` and
  `.listing-completion-header` now pad 28px on the left (right padding
  unchanged at 11px) so a suggestion name lines up directly under the query.
  The two rules are commented as needing to move together with the input's
  gutter if that gutter ever changes.
- **Float treatment.** Swapped `background: var(--bg-panel)` for
  `var(--bg-alt)` and `box-shadow: 0 4px 16px var(--shadow-md)` for
  `0 8px 28px var(--shadow-lg)`, matching the app's other floating menus
  (again `context-menu.css`) so the dropdown reads as a card above the
  listing rather than a flat box drawn on it. `top` moved from
  `calc(100% + 4px)` to `calc(100% + 7px)`: the input's focus ring is a 3px
  `box-shadow`, so 4px of gap left only 1px of clearance past the ring's
  outer edge; 7px clears the ring with 4px of air.

Scope was CSS-only in `explorer.css`; no `.tsx`, test, or layout geometry
changed.

## Search chip: the plain-count branch names its own numbers

The user saw the search chip read `10 · 45 ms` and `471 · not refreshed` and
said it makes no sense — two unlabeled numbers joined by a middot read as two
unrelated figures. `resultCountLabel` (`listing/result-cap.ts`) already
produces the labelled form ("10 matches") but only reached `title`/
`aria-label`, never the visible chip; Home's box, which the user asked this
to match, renders the labelled form on screen (`homeCountNote`,
`lib/home-search.ts`, shown at `FilesHome.tsx`).

Fixed at the composition point in `Listing.tsx` (~1764-1769), not in the
elapsed branch further down, so every downstream combination inherits the
noun: the plain branch is now `` `${compact(hits.length)}${suffix} match${hits.length === 1 ? "" : "es"}` ``,
pluralised off the raw hit count. The `cappedAway > 0` branch ("top 100 of
4.9K+") was left alone — it already names what its numbers are, and a noun
would only add length for no gain. The chip keeps `compact()` throughout
rather than switching to `resultCountLabel`: the chip wants "4.9K", the
tooltip wants "4,900" (`toLocaleString`), and that's the one place
`resultCountLabel` should stay.

No CSS changed. `.listing-search .listing-search-box.has-pin.wide-pin
.listing-search-input`'s 210px reservation was checked by rendering the two
composed strings headlessly (12px, the app's system-ui stack): "10 matches ·
45 ms" measures ~105px and "471 matches · not refreshed" measures ~150px,
both comfortably under 210px.

No existing test asserted this composed chip string before this change —
`searchCount`'s composition is inline in `Listing.tsx` and never extracted
to a pure helper or exercised by a component-level render harness (the only
render tests near it, `Listing.test.tsx` and `empty-result.test.tsx`, cover
`snapshotListing`/`useDirListing` and `EmptyResultMessage`, not this chip).
Per scope, no new test harness was built for it; `result-cap.test.ts`
(`resultCountLabel`) and `index-caveat.test.ts` were re-run untouched and
still pass, since neither was changed.

## Flake: `test_the_run_listing_is_not_re_read_on_every_keystroke`

The test's own widened `RUNS_CACHE_S` (3600s) rules out wall-clock expiry as
the source of the `assert 2 == 1` failure on `test-python (3.11)`, which
leaves a second caller of the monkeypatched `runner.list_runs` as the only
remaining explanation among the candidates worth checking.

Confirmed by direct reproduction (a script that drives the test body in a
tight loop in-process, after starting the Activity job bridge the way an
earlier test file does): `mirror_index_jobs_once` — the tick function behind
`_index_job_loop` — reads `runner.list_runs` directly, deliberately bypassing
`_live_runs`'s cache (see that function's own docstring). Its thread is
started by `start_index_job_bridge`, which any test whose app runs a full
ASGI lifespan (`with TestClient(...) as client:`, e.g. `test_capture_stream.py`,
`test_tasks_watch.py`) triggers; the thread is idempotent-start but never
stopped, so once live it keeps ticking — every `INDEX_JOB_ACTIVE_S` (1.5s)
or `INDEX_JOB_IDLE_S` (10s) — for the rest of the worker process. A bare
`TestClient(create_app(...))` without `with` (what this test and most of
`test_index_search.py` use) never runs lifespan itself and never starts this
thread, so the thread has to be left running by an earlier test in the same
pytest worker — order- and timing-dependent, which fits both "passed on the
previous CI run" and the single-interpreter-version reproduction: thread
wake-up latency differs enough between 3.11 and 3.12/3.13 to change whether a
tick lands inside the five-request window.

Reproduction: with the bridge thread live, driving the test body for 90s
(≈850 iterations) hit `calls == 2` seven times. With the fix below applied,
the same 90s/847-iteration run had zero failures. Isolated pytest runs of
just this test (no `with TestClient` predecessor in-process) never failed in
either state — the bridge thread has to already be running, which a scoped
`pytest -k` invocation alone won't produce.

Fix: `monkeypatch.setattr(index_routes, "mirror_index_jobs_once", lambda
cfg=None: False)` for the duration of the test, silencing the one other
caller of `runner.list_runs` rather than weakening `assert len(calls) == 1`.
The `RUNS_CACHE_S` widening stays (a real defense against a slow round trip
aging the cache mid-loop), and the comment above it was rewritten to drop the
now-wrong wall-clock explanation and the CI-run references.

## `behind` split: an uncommitted escaping query is not "not refreshed"

A user typed a leading-`~` query over an already-committed one and saw the
old query's rows on screen, captioned "not refreshed", with no visible cue
that Enter was needed. `index-caveat.ts`'s own comment already named the
bug: `behind` was "two situations wearing one name" — a genuine older
generation, and rows answering a query already edited past. Once the Enter
gate went base-aware (`escapesBase`, decision 4 revisited above), a
same-base query never gates at all, so the second situation is reachable
only when the query escapes the box root and has not been committed. One
state, one meaning, so it gets its own name.

`useListingSearch.ts`'s `generationBehind` is now just `searching &&
answerGen.current !== gen` — the actual "an older generation answered
these rows" claim, still captioned "not refreshed" by `index-caveat.ts`
unchanged. The other situation is a new return value, `awaitingCommit`:
`searching && !gateOpen && staleRows && !pending`. Gated on `!gateOpen`
explicitly rather than left to fall out of `staleRows` alone — a reader
should not have to re-derive the gate's reachability argument to see when
this is true, and an explicit condition survives a future change to the
gate. Since `generationBehind` no longer folds in `staleRows && !pending`,
the chip carries no caveat at all while `awaitingCommit` is true — verified
by a test, not special-cased in `Listing.tsx`.

`Listing.tsx`'s search body used to have two separate branches reaching the
Enter prompt: `searchState.status === "idle"` (no answer at all yet) and,
unreachably as far as the comment claimed, nothing for the case where a
previous committed answer's rows were still on screen — `displayHits.length`
caught that case first and rendered the stale rows instead. The two
branches merged into one, `awaitingCommit || searchState.status === "idle"`,
placed ahead of the `displayHits.length` branch so it wins either way: rows
on screen for a query that has moved past the box's own base are not a
stale answer to be captioned, they are an answer to a different folder, and
get replaced by the prompt rather than dimmed and labelled. The old
duplicate idle-only branch (identical JSX, now dead) was deleted rather than
kept beside the merged one.

`enter-prompt.ts`'s bare "Press Enter to search" became "Press Enter to
search outside this folder" — its one call site is this same merged branch,
which the gate guarantees only reaches for an escaping, uncommitted query,
so no new parameter was needed to know that. It deliberately does not name
the actual base ("search ~/Work"): a leading-slash query's base is only
decidable by a filesystem walk on the server (`fused_render/index/query.py`'s
`resolve_query`), which this predicate cannot do synchronously and does not
attempt to (decision 4 revisited, above) — a named base would be wrong
exactly when it mattered most.

No render-test coverage was added for the `Listing.tsx` branch reordering,
for the same reason the rest of this file's React wiring has none (see the
two-deviations note above this section): there is no `@testing-library/
react`/jsdom harness in this codebase, only extracted pure functions and
hooks are unit-tested. Verified by `bunx tsc --noEmit`, the existing
`Listing.test.tsx` suite passing unchanged, and by reasoning about the
branch order directly. `useListingSearch.render.test.ts`'s existing
"editing a committed escaping query further re-gates" case gained the two
assertions that actually pin this fix: `awaitingCommit` true and `behind`
false in the exact state the bug report was about.

## The resting crumb strip matches the plain bar's 12px, not the input's 13px

`.listing-search-crumbs` (`explorer.css`) had `font-size: 13px`, chosen to
match `.listing-search-input`'s 13px — the wrong reference. This element is
a PATH STRIP, and every other path strip in the app (`#breadcrumb .crumbs`/
`.panel-crumbs`) is 12px monospace. Monospace is already the widest face in
the row, and the rest of the bar's text sits at 12–12.5px, so the extra 1px
made the resting path the largest thing in the bar — the "way bigger" the
complaint was about. Changed to 12px; the rule's comment now says the strip
wears the plain crumb strip's face AND size for that reason, not the
input's, and why.

Checked whether the strip's centring or `.path-crumb-sep` spacing inside it
was tuned against 13px: `.listing-search-crumbs` centers with flex
(`align-items: center` over `inset: 0`), and `.path-crumb-sep`'s `margin: 0
4px` is a flat px value, not derived from the font size either place it's
declared. Both are size-independent, so neither needed a nudge.

`.listing-search-input`'s own 13px is untouched — pre-existing on `main`
and outside this fix's scope.

## The Enter prompt names the folder it is about to open, not just "outside this folder"

The previous fix (decision 9 revisited, above) deliberately left the escaping
prompt generic — "Press Enter to search outside this folder" — reasoning
that pressing Enter only searches. That undersold what Enter actually does:
it also relocates the search base to the folder the query names, and the
vague wording hid that half. Per the user's own report ("lets have the text
say open directory and search perhaps?"), the wording became "Press Enter to
open ~/Work and search" — naming the folder as typed (`~` kept, never
expanded to `/home/<user>`).

Checked for an already-resolved base before deriving one: `typedAddress`
(`useTypedPathAddress.ts`) only carries a resolved path on `status ===
"exists"`, which is the sibling branch this one never reaches (a resolved
real path takes the "open <name>" branch instead, decision 9's original
case). `completion-target.ts`'s `completionTarget` resolves a dir+partial
split, but its very first line (`if (!raw || raw.includes("*")) return
null`) refuses any query containing `*` — exactly the shape this prompt
exists for (`~/Work/*/*.json`) — and `Listing.tsx`'s own comment on `useCompletion` already says as much: a glob gets no dropdown at all. So neither
resolved source is ever populated for the case this prompt handles; the
folder is derived from the query string itself, in `enter-prompt.ts`'s new
`folderToOpen`, no server round trip.

Derivation: split the query on `/`; the folder is everything before the
first segment containing `*` or `?` (`~/Work/*/*.json` -> `~/Work`;
`~/*.json` -> `~`; `/tmp/data/*.csv` -> `/tmp/data`; `../x/*.json` ->
`../x`). If no segment has a glob character, the query is a path with a
name pattern on the end rather than a folder to search inside, so the last
segment is dropped instead (`~/Work/notes` -> `~/Work`) — the same
plain-filter-vs-path distinction `completion-target.ts` already draws. When
that leaves nothing (`~` alone, `/` alone), the fallback is generic
phrasing rather than an empty name: "Press Enter to open that folder and
search" — chosen over reusing the old "outside this folder" wording because
that phrase read as a location-independent warning, while "that folder"
reads as continuing the same "open ... and search" sentence shape the named
case uses, just without a name to slot in.

`enter-prompt.ts` stays pure: it gained a second parameter, `query: string`,
rather than reaching into anything stateful. `Listing.tsx`'s one call site
already had `query` in scope (`useListingSearch`'s return), so the change
there is one argument, not new plumbing.

Known limitation, recorded rather than solved: the named folder may not
exist. `resolve_query` (`fused_render/index/query.py`'s `_walk_from`) widens
the search from a missing folder instead of failing, so "Press Enter to
open ~/Work and search" would over-promise for a typo'd or since-deleted
`~/Work`. Solving this needs a filesystem probe this client does not have
synchronously and the user has said edge cases come after the UX is
settled — left for later, not attempted here.

## Flake follow-up: `863a0eaf1`'s monkeypatch was not the whole fix

`863a0eaf1` silenced future bridge ticks by monkeypatching
`index_routes.mirror_index_jobs_once`, and CI (`test-python (3.11)`, run
34356290461) still hit `assert 2 == 1`. Two hypotheses were on the table: a
tick already inside the real `mirror_index_jobs_once` when the patch lands,
or some other caller of `runner.list_runs` entirely.

Established which by deterministic reproduction, not by re-running the
existing test in a loop and hoping to get lucky: a script
(`repro_deterministic.py` in scratch, not committed) ran the real
`mirror_index_jobs_once` on a thread named like the bridge's own, paused
mid-body via a monkeypatched `load_config` blocking on a `threading.Event`
— simulating a tick that had already resolved and called
`mirror_index_jobs_once` before the test's patches land, the same way the
real bridge thread might be paused between bytecode ops at that instant.
While that thread sat paused inside the real function's frame, the test's
own two monkeypatches (instrumenting `runner.list_runs`, replacing
`index_routes.mirror_index_jobs_once`) were applied from the main thread,
then the paused thread was released to continue. Result: `calls == 2`,
reproduced on demand, no timing luck involved. This confirms hypothesis 1:
`_index_job_loop` calls `mirror_index_jobs_once()` by a plain name lookup
that re-patching intercepts for the *next* tick, but a tick already past
that lookup — mid-execution of the real function's body — reaches its own
`runner.list_runs(...)` call by a *separate* name lookup, on `runner`, which
was already patched (the test patches `runner.list_runs` first) and doesn't
care which version of `mirror_index_jobs_once` is calling it. Re-patching
the caller can never retract a call already in flight. A probabilistic
repro (`repro_race.py`, also scratch-only) that just started the thread and
raced timing without synchronization did not reproduce this in 199
iterations over 20s — the window is narrow enough that only forcing the
interleaving makes it observable on demand, which is consistent with a race
that shows up on one Python version's scheduling and not others.

Fix: stopped trying to out-race the patch and instead made the recording
counter attribute calls to their origin. `runner.list_runs` is now wrapped
by a `recording` closure that only appends to `calls` when
`threading.current_thread().name != index_routes.INDEX_JOB_BRIDGE_THREAD_NAME`
— a new named constant (`fused_render/server/routers/index.py`, replacing
the bare `"index-job-bridge"` literal `start_index_job_bridge` already
passed to `threading.Thread`) so the test imports one spelling instead of
duplicating the string. This works regardless of which hypothesis holds:
it excludes both a future tick that the `mirror_index_jobs_once` patch
already silenced (that call never happens now) and any that reaches
`runner.list_runs` regardless of the patch, whatever thread it runs on —
covering "some other caller" too, not just the bridge specifically, as long
as that caller isn't the request path itself. Confirmed the request path is
never mistaken for the bridge thread: FastAPI's `async def
api_index_rank` runs on the `TestClient`'s own `anyio` blocking-portal
thread (named `asyncio-portal-...`), never `MainThread` and never
`index-job-bridge` — checked directly rather than assumed, since a naive
"only count calls from the test's own thread" (the brief's literal
phrasing) would have zeroed out the legitimate calls too and made the test
vacuously pass.

The existing `mirror_index_jobs_once` monkeypatch was kept — it still
silences the bridge's steady-state ticking, the common case — with the
thread-origin filter as the belt-and-suspenders that closes the in-flight
gap. `RUNS_CACHE_S = 3600.0` and `assert len(calls) == 1` are unchanged.

Verified: `repro_deterministic.py` re-run with the fix's exact filter
returns `calls == 1` where it previously reproduced `calls == 2`.
`pytest tests/test_capture_stream.py
tests/test_index_search.py::test_the_run_listing_is_not_re_read_on_every_keystroke
-n0 -q`, looped 25 times (single worker, so the bridge thread `test_capture_stream.py`'s
lifespan starts is guaranteed to still be running when the target test executes
after it in the same process) — 25/25 passed, 0 assertion failures, in two
separate 25-run batches. One batch mid-run hit 5 unrelated `RuntimeError:
React shell not built` collisions (the frontend builder's concurrent rebuild of
`frontend/` transiently removing `fused_render/static/shell-dist/`), not the
target assertion — confirmed by grepping every run's log for
`assert len(calls)` (no hits) versus `shell-dist` (the 5 failing logs only).
`pytest tests/test_index_jobs.py tests/test_index_search.py -q`: 109 passed.

## Ctrl/Cmd+L over a claimed folder seeds the field instead of focusing it empty

Decision 1 (the merged field is the bar's one path affordance) left a gap:
`requestSearchFocus()` took no argument, so Ctrl/Cmd+L over a claimed folder
opened an EMPTY search box — a browser location bar seeds itself with the
current address on the same chord, and this one didn't, so the "select all,
type to replace, or copy" gesture stopped working the moment a folder
claimed the bar.

Fixed by giving `requestSearchFocus` an optional `seed` parameter
(`listing/search-focus.ts`), threaded through to every subscriber. Click-to-
edit (Breadcrumb.tsx's other caller) still calls it with nothing, unchanged.
Ctrl/Cmd+L calls it with `displayPath` — the exact `~`-contracted string
edit mode already seeds its own `<input>` with (`underHome ? "~" + rest :
fsPath`), read through a new `displayPathRef` kept fresh every render so the
always-on keydown listener (bound once, `[]` deps) never closes over a
stale folder. No second contraction was written.

`Listing.tsx`'s subscriber does three things with a seed: `setQuery(seed)`
so React owns the text the same way anything typed does, `setPinnedOpen(true)`
so the strip opens wide the way focusing it always has, and focuses the
input. Selecting the seeded text could not happen in that same call: React 18
batches `setQuery`, so the DOM's `value` is still last render's (empty)
string at the moment `.select()` would fire, and calling it there selected
nothing. A `seedSelectRef` flag is set instead and consumed by a second
effect keyed on `query`, which fires only after the seeded text has actually
painted — that is where `.select()` runs.

Knock-on, expected and not special-cased: the seeded string always starts
with `~/` (underHome) or `/` (not), and `query-base.ts`'s `escapesBase` is
purely syntactic on those two prefixes — so every Ctrl/Cmd+L seed escapes the
box root regardless of which folder it names, the Enter gate applies, and
since the seeded path is exactly the folder already on screen, `statPath`
resolves it and the chip reads "Press Enter to open `<name>`" (enter-
prompt.ts's `exists` branch) rather than a search prompt. Confirmed by
tracing `escapesBase`/`enterPrompt` rather than by a live server round trip
(`useTypedPathAddress` calls the real API, which a unit test does not run).

## Right-click over the RESTING merged field reopens the bar menu

Change 3's regression: a claimed folder's search `<input>` covers the whole
row, so right-clicking anywhere on the bar landed on that `<input>` and got
the browser's own copy/paste menu — even over the plain breadcrumbs shown at
rest, where the bar's own menu (New File, Paste, Refresh, ...) used to be
one right-click away.

Fixed narrowly: `listing/search-box-context-menu.ts` exports one predicate,
`searchBoxRestingForContextMenu(query, pinnedOpen)`, which is exactly
`query === "" && !pinnedOpen` — the same condition `Listing.tsx` already
gates `PathCrumbs` (`.listing-search-crumbs`) on. Not a new definition of
"resting": the same one, pulled out so the render and the handler read it
from one place and can't drift apart. `Listing.tsx` wires an `onContextMenu`
onto `.listing-search-box` (the `searchBoxRef` div that wraps the magnifier,
the crumbs-or-input, and any pinned chip) that calls the predicate first and
returns immediately when it is false — leaving the event alone, so the
browser's native input menu opens. When it is true, `openTopbarMenu(e.clientX,
e.clientY)` is called with no crumb argument (this box is always the CURRENT
folder, never an ancestor) — the same route the bar's own right-click
(Breadcrumb.tsx's `onBarContextMenu`) already uses for its dead space — and
the event is only `preventDefault()`-ed if that call found an owner to hand
it to.

The handler stands down (does nothing at all, not even `preventDefault`) the
moment the field has content or focus for one reason: this is the box the
user pastes glob patterns into, and a right-click there is reaching for
Paste. `pinnedOpen` alone (focused, still empty) already means the crumbs
are gone and the caret is live, so treating "focused-empty" as still
resting would break Paste on the very first click into the field — before a
single character is typed.

The handler sits on the BOX (`.listing-search-box`), not on
`.listing-search-crumbs`, because that inner strip is `pointer-events: none`
everywhere except its own crumb anchors (explorer.css ~1560, which carries
an explicit warning against widening that back to the current-folder span —
doing so creates a dead zone that swallows presses meant for the input
underneath it). A handler on the box needs none of that: it sits below the
crumbs in the DOM but wraps them, so it sees every right-click that reaches
either the crumbs' anchors or the input beneath them, without touching any
`pointer-events` rule.

## `behind` still fired for a search that had never landed an answer

Live in the browser, over `~/Downloads`, Ctrl+L seeded the field and selected
it (the seeding decision above). The pinned chip read exactly `not
refreshed`, with no count beside it, on a first-ever query in a freshly
loaded folder. The `behind`/`awaitingCommit` split (above) had already
narrowed `generationBehind` to `searching && answerGen.current !==
gen` — the actual "an older generation answered these rows" claim — but
`answerGen.current` is initialized to `gen` at mount and only ever moves
forward when an answer actually lands (a memo hit or a fetch reply). A `gen`
bump for any unrelated reason (a dir-watch event, a completed scan
elsewhere) before this search had EVER gotten an answer made the two differ
regardless, and "not refreshed" is a claim about an EXISTING answer — with
none on screen it was simply false.

Fixed with the explicit signal the split's own writeup called for rather
than re-deriving it from row counts: a new ref, `answered`, set `true` at
the same two places `answerGen.current` is assigned (`useListingSearch.ts`,
the memo hit and the fetch reply), and cleared back to `false` everywhere
`setAnswer(null)` already runs (the `[fsPath, pinned]` reset effect, and
leaving search entirely). `generationBehind` is now `searching &&
answered.current && answerGen.current !== gen` — true by inspection: a
generation mismatch means nothing until an answer has actually been
recorded for this search. The old `staleRows` disjunct the split had
deliberately removed stays removed; this is a narrower condition on
`generationBehind` itself, not a resurrection of the conflation.

`useListingSearch.render.test.ts` gained a case: a query is typed and its
request left outstanding (no reply yet), the render is driven forward two
generations, and `behind` is asserted `false` — where it read `true` before
this fix. Resolving the outstanding request afterward (now landing under the
new generation) leaves `behind` `false` too, confirming the fix answers the
right claim rather than merely suppressing it once.

## The listing body blanked to nothing but the prompt row

The other half of the same live session: with `~/Downloads` seeded and
uncommitted (Ctrl+L, above), the entire table body was one row — the Enter
prompt — and the folder's own entries were gone. Clearing rows was approved
for STALE SEARCH RESULTS, answers to a query the user has since typed past
(`4a0ae74cf`, "Replace stale rows with the Enter prompt while a search
awaits commit"). The current directory's own entries are neither: they are
not an answer to any query, stale or otherwise, and Ctrl+L is a gesture for
reading the path, not for searching at all. The fix that landed the Enter
prompt had replaced the WHOLE body with it, in both states it covers — no
answer ever asked for yet (`searchState.status === "idle"`) and a previous
committed answer sitting stale (`awaitingCommit`) — losing the folder's own
rows in both.

Fixed by making explicit the choice `Listing.tsx` was already making
implicitly in three places that had to agree — the column count, the
`<thead>`, and the body branch — and had started to drift, since the body
branch alone had grown the `awaitingCommit` case the other two never learned
about. Pulled into one pure predicate, `search-body-mode.ts`'s
`showingSearchHits(searchState, awaitingCommit)`: `searchState.status !==
"idle" && !awaitingCommit`. False in exactly the two states above — nothing
to show as search hits — and all three read-sites (`cols`, `<thead>`, the
body `if`) call it instead of branching on `searching` or duplicating the
`awaitingCommit || status === "idle"` test that used to live only in the
body.

When it is false, the body falls through to the SAME branch that already
renders the plain folder listing when there is no query at all — the
`state.status === "loading" / "error" / "ok"` chain built on `useDirListing`'s
own `state` and the already-memoized `sortedEntries`, unchanged. This
satisfies "make the choice where the component already chooses between the
folder listing and search hits, rather than reconstructing the folder's rows
from a second source" directly: no new fetch, no second row-building loop,
the exact `<tr>`s a plain unfocused folder view renders. A query still typed
and uncommitted (`searching` true) has one banner `<tr>` — the Enter prompt,
unchanged text and unchanged call (`enterPrompt(typedAddress, query)`) —
prepended above those rows, rather than replacing them. Not a caveat folded
into the count chip: the user had already rejected that shape ("the not
refreshed or press enter to... part feels weird"), and the chip logic below
is untouched by this fix — it is one row, above real rows, saying what Enter
will do.

Stale SEARCH results are unaffected: the `displayHits.length` /
`scanPending` / empty-answer branches inside `showingSearchHits`'s `true`
side are the same code, reached the same way, for a COMMITTED query whose
rows are being shown or replaced — the split is by source (a previous
query's hits vs. the directory's own entries), not by staleness, matching
the constraint this fix was scoped against.

Not extended to `navRows` / `rowCtxByPath` / the search auto-select effect
(all still keyed on the raw `searching` flag): those stay empty while the
folder's rows are the ones on screen in this state, so arrow-key navigation
and the top-hit auto-select do not act on rows that are not search hits —
correct by the same "opening a folder selects nothing" rule already
documented at the plain-folder auto-select site (Listing.tsx), which this
state now resembles rather than deviates from. Keyboard navigation OF the
now-visible folder rows while a query sits uncommitted was not wired up;
this was not asked for and left as a known gap rather than guessed at.

Verified with a new pure-function test, `search-body-mode.test.ts`, pinning
all four inputs `showingSearchHits` distinguishes: idle-with-nothing-asked,
a settled answer, `awaitingCommit` overriding a settled `"ok"` status (the
committed-search-with-rows case), and `pending`/`error` both counting as
hits. The Listing.tsx branch reordering itself has no render-test coverage,
for the same reason the rest of this file's React wiring has none (see the
two-deviations note above): confirmed instead by `bunx tsc --noEmit`, the
existing `Listing.test.tsx` suite passing unchanged, and reading the new
`showingSearchHits`-gated branches directly.

Three states, traced through the code after both fixes above (no live
capture tool available in this pass; reasoned from `useListingSearch.ts`,
`index-caveat.ts`, and `enter-prompt.ts` directly):

- **A first-ever query in a bumped generation** (the request still
  outstanding when `gen` moves): `behind` is `false`, `searchState.status`
  is `"pending"`, `searchCount` stays `null` (only set once `status === "ok"
  && hits.length > 0`) and no caveat applies (`behind` is `false`) — the
  chip renders no text at all (a spinner glyph only once the wait crosses
  `PENDING_INDICATOR_MS`). Body: one row, "Searching…" — `showingSearchHits`
  is `true` here (status is not `"idle"`), so this is the ordinary
  search-pending branch, unchanged by this fix.
- **Ctrl/Cmd+L over a folder** (seeded, escaping, uncommitted, nothing ever
  asked): `searchState.status` is `"idle"`, `awaitingCommit` is `false`
  (no previous answer to be stale), `behind` is `false`. Chip: no text
  (`searchCount` stays `null`; no caveat). Body: one banner row, "Press
  Enter to open Downloads" (`enterPrompt` resolves the seeded path via
  `typedAddress.status === "exists"`), followed by the folder's own rows —
  `~/Downloads`'s entries, Name/Size/Modified, exactly as a plain unfocused
  listing of that folder renders them.
- **An escaping query typed over a committed search that had returned
  rows** (edited further past a committed answer, not yet re-committed):
  `searchState.status` is `"ok"` (the stale committed answer), `awaitingCommit`
  is `true`, so `showingSearchHits` is `false` despite the `"ok"` status.
  Chip: unchanged from before this fix — `searchCount` is still built from
  the stale answer's `hits` (e.g. `"1 match"`), no caveat (`behind` is
  `false`), an elapsed suffix appended (e.g. `"1 match · 12 ms"`) — the chip
  logic reads `hits`/`searchState` directly and was never gated on
  `awaitingCommit`. Body: one banner row naming the folder the escaping
  query would search (e.g. "Press Enter to open ~/a and search"), followed
  by the folder's own current rows — NOT the previous committed answer's
  stale hits, which are dropped from the body exactly as approved for stale
  search results.

## A Ctrl/Cmd+L seed is provisional: a blur that never touches it discards it

The seeding decision above documented `pinnedOpen` staying "until it blurs
empty" — correct for text the user TYPED, where losing it to a stray click
would be terrible. Seeding broke that rule's premise: the field is never
empty right after Ctrl/Cmd+L, so a blur left it pinned open with the
crumbs stood down and no way out except Escape or manually clearing the
text — reported as "I can't escape the focus by clicking anywhere else."

Fixed by making a seeded value provisional: it survives only while
untouched. A new `queryProvisional` boolean (Listing.tsx, alongside
`pinnedOpen`) is set `true` in the same subscriber call that lands the seed
(`setQuery(seed)`) — right where the seed already lands, so the two state
writes can't drift apart. It is cleared back to `false` by every place that
turns the value into something indistinguishable from typed text: `onChange`
(any keystroke, including a paste, which fires `onChange` too), a completion
accepted via `acceptCompletion` (Tab, or a row's own click/mousedown) or
`navigateToCompletion` (Enter on a highlighted row), and a committed Enter
(both the "exists" navigate branch and the search-commit branch). Escape
clears it too, consistent with the query it just wiped.

The blur decision itself is a pure function, `searchBoxBlurAction`
(`listing/search-provisional.ts`, mirroring `search-box-context-menu.ts`'s
`searchBoxRestingForContextMenu`): given `(queryProvisional, queryEmpty)` it
returns `"discard"` (provisional — wipe the query, unpin, stand the crumbs
back up, regardless of whether the text looks empty, since an untouched seed
never is), `"unpin"` (not provisional, user emptied it themselves — today's
existing behavior), or `"keep-open"` (not provisional, text present —
survives, unchanged). `onBlur` in Listing.tsx just carries out whichever one
comes back.

Watched-for trap avoided: the existing `navigateToCompletion` comment
documents that `onBlur`'s `e.currentTarget.value` still holds the pre-clear
text at blur time, because React has not yet flushed the DOM write — so a
handler that read the DOM to decide "empty?" would miss a same-tick clear.
`searchBoxBlurAction` sidesteps this by never reading the DOM at all: `onBlur`
now passes the `query` REACT STATE (`query === ""`), which is always current
by the time a genuine blur fires (nothing else writes to it in the same
tick), rather than `e.currentTarget.value`.

Clicking a completion row does not run this path in the first place: a
row's `onMouseDown` calls `e.preventDefault()` (pre-existing, for item 6 in
the section above), which keeps focus on the input throughout the click, so
`onBlur` never fires — `acceptCompletion`/`navigateToCompletion` run instead,
each clearing `queryProvisional` itself. Clicking the field itself or the
magnifier only ever raises `onFocus`, never `onBlur`, so neither can trigger
a discard either.

Verified with `search-provisional.test.ts`, pinning all four
`(queryProvisional, queryEmpty)` combinations against `searchBoxBlurAction`.
The four scenarios asked for, traced through the code:

- **Blur straight after Ctrl/Cmd+L**: `queryProvisional` is still `true`
  (nothing has run since the seed landed). `searchBoxBlurAction` returns
  `"discard"`: query clears to `""`, `pinnedOpen` and `queryProvisional`
  both go `false`. The field returns to resting — crumbs visible, folder
  listing shown, no ring.
- **Blur after one keystroke**: the keystroke's `onChange` already cleared
  `queryProvisional` to `false` before any blur happens. `searchBoxBlurAction`
  returns `"keep-open"` (query is non-empty) — exactly today's behavior for
  typed text: it survives the blur, unchanged.
- **Blur after Enter**: Enter commits (`typedAddress.status === "exists"`
  navigates, or `escapes` calls `commitSearch()`), and both branches clear
  `queryProvisional` first. Any later blur runs the same `"keep-open"` path
  as typed text — a committed query, provisional or not, is now a real query.
- **Clicking a completion row while provisional**: the row's own `mousedown`
  fires before any blur could, `preventDefault()`s it, and calls
  `acceptCompletion`, which fills the row's path into `query` and clears
  `queryProvisional` itself. No blur, and no discard — the completion is
  accepted normally, whether or not the field started out provisional.

## The chip is a fourth reader of `showsSearchHits`

`Listing.tsx`'s match-count chip, its `widePin` reserved-width flag, and the
selection readout's shortfall annotation ("N selected of M+") were all gated
on raw `searching` (`q.length >= MIN_QUERY_CHARS`), not on `showsSearchHits`
(`search-body-mode.ts`'s `showingSearchHits(searchState, awaitingCommit)`).
An uncommitted query whose base escapes the current folder leaves `searching`
true and `searchState` sitting on the previous committed answer while
`awaitingCommit` makes the body fall back to the folder's own rows — so the
chip kept describing a search that is not on screen.

Fixed by switching all three onto `showsSearchHits`, already computed above
the chip block for the column count, the `<thead>`, and the body branch:

- The chip's own gate (`searchState.status === "ok" && hits.length > 0`).
- The index-scan caveat (`searchCaveat`), which could otherwise fold a
  "not refreshed" or "indexing…" note onto a chip with no count at all,
  still describing the stale answer.
- The selection shortfall's "of M+" annotation, which reads `hits.length`
  and `searchState.truncated` — properties of the last committed search
  answer, not of whatever is actually selected on screen. The plain "N
  selected" count itself needed no change: it comes straight off `sel.paths`,
  the live selection, and was never wrong.

No new pure predicate was extracted: the chip's gate is exactly
`showsSearchHits`, no new logic composed around it, so `showingSearchHits`'s
existing test — "awaitingCommit wins even when a previous committed answer
is 'ok' on screen" (`search-body-mode.test.ts`) — already pins the boundary
that fixes the chip. `EntryActionsMenu.tsx`, `navRows`/`rowCtxByPath`, and the
search auto-select stay keyed on raw `searching`, unchanged and out of scope.

## The star moves inside the field's border, gated on `barSearchSlot`

`BookmarkStar` renders at three sites, and only one of them needed to
change: `Breadcrumb.tsx`'s main bar (search row when claimed, otherwise
just crumbs), `StaticBreadcrumb` (a label row with no search box at all),
and `Panel.tsx`'s own pane bar (a different component, also no search
row). The move is scoped to `Listing.tsx`, which already knows whether
*this* search row has portaled into a claimed crumb bar — `barSearchSlot`,
truthy only after the portal lands. `Breadcrumb.tsx`'s own star render is
gated on the dual condition, `claimed`, so exactly one copy renders for any
given bar: inside the box when claimed, outside it (where it always was)
otherwise.

Inside the box it is absolutely positioned, trailing the count/spinner pin
at the box's own right edge — the same mechanism the chip already used,
extended rather than duplicated: `--pin-right` grows by 30px (24px hit
area + 6px gap) so the chip and spinner shift in to clear the star, and
`.has-pin`'s `padding-right` grows by the same 30px so typed text and the
placeholder stay clear of it too. `.wide-pin` gets its own explicit bump
for the same reason, since the generic rule also covers listing rows that
render outside a crumb bar (and so carry no star to clear).

Since the star now lives inside the field, it no longer stands down while
searching — unlike the arrows, which still vacate their spot so the field
can reach the bar's left edge, the star grows and shrinks with the box
it's now part of, the way a browser's own address-bar star does while you
type. That removes `.bookmark-star-btn` from the searching/expanded hide
rule entirely.

**What carries the bar's auto margin now, for the claimed case:** nothing
new needs to. Tracing the pre-existing (untouched) comment on
`.crumbs:has(.path-crumb) ~ .bookmark-star-btn { margin-right: auto }`
confirms it never applied there in the first place — a claimed folder
never renders a `.crumbs` element at all (`claimed ? null : <div
className="crumbs">`), so that rule's `:has()` guard was already finding
nothing to match over a claimed bar, by the codebase's own prior
documentation. The mechanism that actually eats the bar's slack for a
claimed search row is the unconditional `.crumb-search-slot >
.listing-search { flex: 1 1 auto }` row-grow rule, untouched by this
change. The auto-margin rule keeps its job exactly where it always had
one: the unclaimed bar, where the star (still outside the box, still the
zone's last child per the DOM-order test) remains its tail.

**One pinned invariant genuinely cannot hold, and I did not force it
green.** `search-bar-expand.test.ts` pinned "`.bookmark-star-btn`'s hide
rule must stay scoped to `#breadcrumb`" — but once the star stops hiding
while searching at all, there is no rule left to be scoped. I replaced
that test with one asserting the opposite of what it used to: no
searching-scoped rule exists for `.bookmark-star-btn` any more
(`ruleFor(".bookmark-star-btn")` is `undefined`). This is a real,
load-bearing assertion — it would fail loudly if a future change
reintroduced an unscoped hide rule for the star — not a deletion to dodge
a failure. The other two pinned invariants both still hold unchanged: the
DOM-order test (`<CrumbNav />` < `.crumbs` < `<BookmarkStar
id="bookmark-btn"`) is unaffected, since it is scanning the unclaimed
bar's own JSX, which the move never touched; the "rendered unconditionally"
test also passes as written, since the ternary I used (`claimed ? null :
<BookmarkStar .../>`) does not match the `&&`-guard pattern that test
checks for.

## The completion header comes out clean; the row-cap math never depended on it

`Listing.tsx`'s "In {displayDir(...)}" header sat as a plain sibling of
`.listing-completion-rows`, not inside it. The 5.5-row cap
(`rowsMaxHeight`, set from `firstRowRef.current.offsetHeight * 5.5`) was
always measured off a rendered *row's* own height and applied as
`max-height` to the rows container alone — the header was never part of
that container, never part of the measurement, and never counted against
the cap. Removing it needed no correction to `rowsMaxHeight` or the
`Listing.tsx:371` comment describing it; both already only reasoned about
`.listing-completion-row`. `displayDir` keeps a caller inside
`completion-target.ts` itself (`applyQueryNotation`'s own `~`-relative
fallback), so it stays exported — only the `Listing.tsx` import and call
site are gone.

## Merging main's marquee/status-line rework (95f749dd7)

`git merge origin/main` resolved with no conflict markers in either
`Listing.tsx` or `explorer.css` — the `ort` strategy applied both sides'
hunks cleanly, matching the pre-merge line-number analysis: main's five
`Listing.tsx` hunks and three `explorer.css` hunks sit adjacent to, not
inside, the completion dropdown, the `.listing-search-box` trailing
cluster, the `showsSearchHits` derivation, and the search-count/`widePin`
block this branch owns. I still read the merged file rather than trusting
the auto-merge, checking specifically for duplicate or orphaned
declarations around those regions and around row selection.

Main's own `useListingSelection.ts` change removes the
`if (e.defaultPrevented) return` line from the Escape handler, because
its matching removal in `App.tsx` deletes the capture-phase
Escape-cancels-pending-clipboard listener that line existed to defer to
(`fs-clipboard.ts`'s copy/cut/clear-epoch comment was edited in the same
commit to stop mentioning "an Escape clear"). Neither half of that
handler ever gated on search state, and this branch's own
`showsSearchHits`-driven Enter routing lives in a separate `onKeyDown` on
the search `<input>`, not in `useListingSelection.ts`'s document-level
Escape branch — so this is main tidying its own feature with no bearing
on search-driven row behaviour. I read the rest of
`useListingSelection.ts`'s Enter/selection code path directly rather than
scanning the diff for interaction: `inSearch` still gates the
document-level Enter handler exactly where it did before, so a stale row
selection cannot intercept Enter ahead of the search field's own commit
logic added by this branch.

All four verification commands are green post-merge:

- `bunx tsc --noEmit`: clean, no output.
- `bun test --run`: 3320 pass, 0 fail, 14267 `expect()` calls across 144
  files (up from the pre-merge 3293, matching the new
  `status-line.test.ts` and expanded `marquee.test.ts` /
  `useListingSelection.render.test.ts` main added).
- `node scripts/check-boundaries.mjs`: `boundaries OK (498 files)`.
- `bun run build`: succeeds; the only warnings are pre-existing
  chunk-size and dynamic/static dual-import notices unrelated to this
  merge.

Current anchors in the merged tree: `Listing.tsx`'s `showsSearchHits`
derivation is at line 1481, the completion dropdown JSX starts at the
`{showCompletion && (` on line 2139, and the `.listing-search-box`
trailing cluster (search count chip, selection-count chip, then
`{barSearchSlot && <BookmarkStar .../>}`) runs from roughly line 2184 to
2215. `Breadcrumb.tsx`'s `FolderSearchSlot` is defined at line 284 and
`BookmarkStar` at line 384.

## The selection-count chip leaves the search field

The user's screenshot showed two rows selected and the search field reading
"2 selected" right next to the bottom status line's own "2 of 2 selected ·
2.9 KB + 1 folder" — the same fact stated twice. The field's own chip is
gone; the bottom status line is the one surviving readout.

`selectionLabel` and `selectionShortfall` (`Listing.tsx`) existed only to
feed that chip and are deleted with it. `selectionShortfall`'s removal takes
the "N selected of M+" ceiling annotation out of the UI entirely — the
bottom status line's own searching-and-selected branch
(`listing/status-line.ts`) reads `${fmt(selected)} of ${fmt(hits)} selected`,
where `hits` is the raw fetched count with no "+" and no truncation
awareness, so it does not carry that annotation in any form. That is a
known, accepted gap from this change, not a replacement built to close it —
the user asked for the chip gone, and inventing a new "+" marker on the
status line was out of scope for this pass.

`hasPin` dropped `sel.paths.length > 1` from its three-way OR to two
(`(searching && spinner) || searchCount !== null`), and its comment now says
"two chip conditions" instead of three. `compact()` keeps its caller — the
search count chip still uses it — and `.has-pin`/`.wide-pin`/`--pin-right`
in `explorer.css` keep theirs for the same reason; none of that CSS was
touched. The star stays the box's last child; with the selection chip gone,
a folder with a selection and no active search now pins nothing, so the
star sits alone against the field's resting right padding (no `has-pin`),
which is the correct state — there is no longer a chip beside it to clear.

The star's comment was rewritten to drop "now rather than out at the bar's
end" and describe the arrangement as it stands: inside the field's border,
gated on `barSearchSlot`, with `Breadcrumb.tsx` keeping the star for a pane
or framed listing that has no bar to sit inside.

## Search focus: one bold neutral border, not accent-plus-ring

Two rules disagreed on how the merged field signals focus: the plain
`:focus` rule already argued (in its own comment) for a muted brighten, not
accent; the `.expanded`/`.searching` rule overrode it with an accent border,
a 3px accent glow, and an accent magnifier glyph. They are now one rule —
`.listing-search-input:focus`, `.expanded .listing-search-input`, and
`.searching .listing-search-input` grouped together, `border-color:
var(--fg)`, no box-shadow, no glyph override (the glyph falls back to its
resting `--fg-muted`). The `:focus` rule's own stated principle is the one
that survived; the merged comment argues for it directly — the accent
already means "active sort" on the column header, and focus is the single
most frequent transient state in the field, already legible from the caret,
the crumbs-to-input swap, and the dropdown.

The `.expanded`/`.searching` distinction itself is untouched: both still
light the field with the same bold-neutral treatment, `.expanded` firing on
focus alone and `.searching` keeping it lit after a blur that leaves text
behind. Only the declarations each selector maps to changed.

`--accent-rgb` keeps other callers outside this rule (badge/toast/tour
backgrounds elsewhere in `explorer.css`), so the token stays defined —
checked by grep, not assumed.

Geometry check: `--fg` (`#e8eaed` dark / `#1f2023` light) replaces
`var(--fg-muted)`/`var(--accent)` as a `border-color` only; no padding,
border-width, or border-radius changed, and the box-shadow ring is removed
entirely rather than shrunk, so the field's 29px height and its 9px/9px
vertical gaps are unaffected in every state.

`listing/search-bar-expand.test.ts` was checked for assertions naming the
accent, the box-shadow, or the selection chip — it has none (it asserts
`.searching`'s CSS declarations structurally and `Breadcrumb.tsx`'s DOM
order, neither of which named color values or the chip), so no test edit
was needed for either item.

Verification: `bunx tsc --noEmit` clean; `bun test --run` — 3320 pass, 0
fail, matching the post-merge baseline exactly; `node
scripts/check-boundaries.mjs` — `boundaries OK (498 files)`; `bun run
build` succeeds, and the built CSS's `.listing-search-input:focus` rule
confirms the merge (one selector group, `border-color:var(--fg)`, no
box-shadow).

## On-demand scan targets the resolved base, not the open folder

`applyStep`'s "scan" branch (`useListingSearch.ts`) called `requestFolderScan(fsPath)`
— the folder currently open — regardless of what the answer's own `base` said.
For a query that stays inside the open folder the two agree, so this was
invisible; for a query that escapes it (`~/other/...`, an absolute path, a
drive letter, a `..` segment — `query-base.ts`'s `escapesBase`), the server's
`base` names the folder the answer is actually about, and it can differ from
`fsPath`. Scanning `fsPath` in that case scans a folder nothing asked about
and never indexes the real target, so `covered` never turns true and the
uncovered state never resolves. The scan branch now reads
`requestFolderScan(res.base || fsPath)`.

`res.base` is typed as a required, always-present string on
`IndexRankResult`, and `applyStep`'s caller only reaches this branch on a
successful answer (an error response never gets here to be asked "what is
its base"), so there is no error case to special-case. An empty-string
`base` is defensive-only — nothing in `resolve_query` is known to produce
one — and falls back to `fsPath` rather than sending an empty root to the
server.

`useListingSearch.render.test.ts` gained
`"an escaping query asks for a scan of the resolved base, not the open
folder"`, asserting `scanCalls` names the answer's `base` rather than the
open folder for a `~`-prefixed query with `covered: false`; it fails against
the unpatched branch (`scanCalls` came back `["/d"]`, the open folder, not
`["/home/u/other"]`, the resolved base).

## Bare Windows drive root normalizes through `_root_or_bare`

`resolve_query`'s drive-letter branch (`query.py`) built `base` from
`_walk_from` and returned it as-is, skipping the `_root_or_bare` pass that
`stats()`, `_walk_from`'s other caller, and `search_ranked`'s own root
argument each get. For any drive path with something after the separator
this made no difference — `_walk_from` only collapses to the bare two-
character form (`"C:"`) when `rest` is empty, i.e. when the whole query is
just the drive. A bare `"C:\"` or `"C:/"` therefore resolved to `base:
"C:"`, not the canonical `"C:/"` `canonical_root()` (`index/runner.py`)
actually stores the drive under.

Checked whether `search_ranked`'s downstream `_root_or_bare(norm(os.path.
abspath(...)).rstrip("/"))` wrap on its `root` parameter neutralizes this
before it reaches a real lookup: it does not — `os.path.abspath` treats a
bare `"C:"` as a relative path segment on the platform this actually runs
on and joins it onto the process's cwd instead of restoring `"C:/"`, so the
wrap makes the value worse, not better. This is a genuine defect, not
something already absorbed elsewhere. The drive branch now closes with
`base = _root_or_bare(base.rstrip("/"))`, matching the other three call
sites.

`test_index_query.py` gained
`test_resolve_bare_windows_drive_root_backslash_normalizes_to_slash_form`
and `test_resolve_bare_windows_drive_root_forward_slash_normalizes_to_slash_form`,
pinning `resolve_query("/box", "C:\\")` and `resolve_query("/box", "C:/")`
to `base: "C:/"`; both fail against the unpatched branch (`base: "C:"`).
`test-python-windows` is a pre-existing, unrelated CI failure on this
branch; verification here ran on Linux only, and that failure neither
masks nor is masked by this fix.

## Highlight retries against the query's leaf when the full query refuses

`hitsFromRank` (`ranked-hits.ts`) ran `substringMatch(q, h.rel)` with `q`
exactly as typed — `useListingSearch.ts` never strips a base prefix before
handing the query down — against `h.rel`, which is relative to the
server's resolved base, not to the query. For a query that escapes the open
folder, the consumed base prefix can never appear as a literal substring of
a base-relative `rel`, so the match refused and the row rendered with no
highlight even though the row itself stayed, correctly, in place. The
segment after the query's last `"/"` is what the server's own walk actually
matched against, so `hitsFromRank` now retries that leaf against `rel`
whenever the full-query match refuses. A query with no `"/"` has a leaf
equal to itself, so the retry is a no-op there rather than a second,
different test; the "refusal drops only the highlight, never the row"
property is unchanged either way.

`ranked-hits.test.ts` gained `"a query carrying a base prefix the server
already consumed still highlights the leaf"`, asserting
`hitsFromRank([hit({ rel: "report.csv" })], "~/other/rep")` lands
`positions: [0, 1, 2]` against `"report.csv"`; confirmed failing (empty
`positions`) against the pre-fix single-match version by re-running the
test against `git show HEAD:frontend/src/apps/explorer/listing/ranked-hits.ts`.

## Search blur discards an uncommitted query, not just a seed

`searchBoxBlurAction` (`search-provisional.ts`) decided blur off `provisional`
— whether the app itself put the current text in the box (Ctrl/Cmd+L's seed)
— and `queryEmpty`. A query the user typed and pressed no Enter for (one that
escapes the box root: a leading `~`, `/`, a drive letter, or a `..`
segment — `escapesBase`, `query-base.ts`) survived a blur exactly like a
committed one, so clicking outside the field with `~/Downloads/agents/`
typed neither released focus nor cleared it — reported directly. The
distinction that matters is committed vs. not: does the query in the box
have an actual answer on screen. `provisional` answered a narrower question
(did the app write this, not the user) that happened to coincide with
"uncommitted" for a seed, but said nothing about a typed-and-unentered query,
which is the case that broke.

`committed` is `showsSearchHits` (`Listing.tsx`, `showingSearchHits` from
`listing/search-body-mode.ts`) passed straight into `searchBoxBlurAction`,
not a new flag. It already asks exactly this question — the same boolean
already picks the table's column count, `<thead>`, and body branch — and for
a query still waiting on Enter it is `false` by construction: `gateOpen` is
false, no answer has ever been fetched for it, and `showingSearchHits`'s own
idle-status branch (`useListingSearch.ts`'s `searchState` ternary) covers
exactly that "typed-but-uncommitted query with no answer ever fetched" case.
An auto-searching query like `*.zip` never sits behind the gate, so it reads
`committed: true` the moment `searchState.status` leaves `"idle"`, matching
`showsSearchHits` turning true — no separate case to wire.
`searchBoxBlurAction`'s new signature is `(committed, queryEmpty)`: empty
always unpins; a non-empty, uncommitted query discards; a non-empty,
committed one keeps the box open.

`queryProvisional` is deleted along with its five write sites (the seed
subscriber, `onChange`, `acceptCompletion`, `navigateToCompletion`, Escape,
and both Enter-commit branches) — it had exactly one read, the blur handler,
and nothing else in the component ever looked at it. Checked whether a seed
still needs it now that the flag is gone: a seed is always the
`"~"`-contracted current path, which always matches one of `escapesBase`'s
own conditions, so it is always uncommitted (`committed: false`) from the
instant it lands until Enter runs it — a blur before that already discards
it through the same path a typed escaping query takes, with no seed-specific
bit required. `provisional` carried no information the committed/uncommitted
split cannot express.

The regression this invites: a completion dropdown row's own `mousedown`
already calls `e.preventDefault()` before `acceptCompletion`/before the
input can blur (`Listing.tsx`, unchanged by this pass) — canceling the
browser's default mousedown focus-shift, so the input never loses focus and
`onBlur` never fires for a row click at all (the existing comment already
says so: "clicking a row never fires this in the first place — focus stays
on the input throughout"). That guard is untouched, so a row click still
completes or navigates exactly as before; discard-on-blur has nothing to
race there. Keyboard completion (`navigateToCompletion`) and Escape both set
`query`/`pinnedOpen` explicitly before calling `.blur()` themselves, and the
subsequent `onBlur` call — reading `query`/`showsSearchHits` from the
pre-clear render closure — either repeats the same reset harmlessly
(`discard`/`unpin`) or, in the `keep-open` case, touches neither `query` nor
`pinnedOpen` at all, so it never fights the explicit reset either handler
already made.

Hand-verified by tracing each path against the actual code (no display is
available in this environment to click a live browser, so this is a
line-by-line trace of the exact handlers and closures involved, not a
literal click):

- Clicking a completion dropdown row: unaffected — the row's `mousedown`
  `preventDefault` still blocks the browser's default blur before
  `acceptCompletion` runs, exactly as the pre-existing comment describes.
- Keyboard selection (arrows + Enter) of a completion row: unaffected —
  `navigateToCompletion` sets `query`/`pinnedOpen`/`fieldActive` explicitly
  before its own `.blur()` call; the resulting `onBlur` either repeats the
  same reset or (an already-committed prior search) touches neither, never
  overriding it.
- Escape: unaffected — same explicit-then-blur shape as completion's Enter
  path; `onBlur` never re-opens what Escape just cleared.
- A committed search surviving a click elsewhere: `showsSearchHits` is true
  while committed rows are on screen, so `searchBoxBlurAction` returns
  `keep-open` exactly as `provisional: false, queryEmpty: false` did before.
- Clicking outside with an uncommitted query (`~/Downloads/agents/`, no
  Enter yet): `showsSearchHits` is `false` (the idle-status branch of
  `searchState`), so blur now returns `discard` — the field clears and
  `PathCrumbs` renders again (`query === "" && !pinnedOpen`), fixing the
  report.

Verification: `bunx tsc --noEmit` clean; `bun test --run` — 3323 pass, 0
fail (3322 baseline plus one added `search-provisional.test.ts` case); `node
scripts/check-boundaries.mjs` — `boundaries OK (498 files)`; `bun run build`
succeeds.

`search-provisional.test.ts`'s four cases were rewritten for the new
`(committed, queryEmpty)` signature rather than edited case-by-case: the old
suite tested `provisional`, which no longer exists as a parameter, so every
assertion needed a new name and a new pair of inputs. The rewritten suite
adds a fifth case (an emptied, committed field still unpins) and names one
case for the auto-searching-query shape explicitly (`*.zip`, `committed:
true` the moment its results land) alongside the base
committed/uncommitted/empty split, per the brief's own worry that this shape
not be missed. Confirmed failing against the pre-patch `searchBoxBlurAction`
(three of the five cases returned `discard` where the new rule expects
`keep-open`/`unpin`) before implementing the new function body.

## Brief 22 — three corrections inside the merged field

Four commits: two CSS-only fixes (items A and B), a bug fix (C1), and a
design addition (C2).

**Item A.** The grouped focus rule
(`.listing-search-input:focus`/`.expanded`/`.searching`) drops
`border-color` from `--fg` back to `--fg-muted`, restoring the pre-branch
value. The comment above it now argues for a muted brighten rather than a
bold jump; the sorted-column-header reasoning for staying off the accent
carries over unchanged. Geometry is untouched — the change is a single
custom-property swap on an existing rule, nothing that touches height or
padding — so the field's 29px height and 9px/9px vertical gaps stand as
they did.

**Item B.** The star and the magnifier used to rest at identical weight —
both `var(--fg-muted)`, no background on either — differing only on hover.
The star now carries the app's quiet-control pill at rest (`--ctl-quiet-bg`
→ `--ctl-quiet-bg-hover` on hover, tokens.css), so the resting pill is what
marks it pressable before the pointer arrives; the magnifier drops a step
dimmer, to `rgba(var(--fg-muted-rgb), 0.6)`, so it now rests visibly below
the star instead of level with it. `.active` keeps its own accent-tinted
pill (`rgba(var(--tint), 0.08)`), distinct from the neutral quiet-hover pill
by colour and from a plain outline star by the filled glyph `StarIcon`
already draws for a bookmarked view — no size step added; the pill plus the
dimmer decoration read as a clear enough pair without stacking a third
signal. Panel-mode bars pick up the same rules from the same
`.bookmark-star-btn` base (only padding/svg size are scoped per surface in
`preview.css`), so the panel star gets the resting pill too with no
panel-specific change needed.

**C1.** The wide-hint measurement (`boxWide`, gating whether the
placeholder's pattern example renders) used an object ref read once inside
a `useLayoutEffect` with `[]` deps. The search box that ref points at is
portaled into the crumb bar once a folder claims it (search-slot.ts) — a
swap that rebuilds the node, exactly the failure mode node-slot.ts's own
comment documents ("a reference captured at mount would then point at a
detached div"). The one-shot effect measured whichever node existed at the
very first commit (before the portal swap landed) and never looked again,
so `boxWide` froze at that reading and the long hint never appeared at any
width. Fixed by extracting the measurement into a callback ref
(`listing/search-hint-width.ts`, `useWidthThresholdRef`), which React calls
with the live node on every mount — portal swaps included — so the
ResizeObserver always tracks whatever node is actually in the document.
Confirmed by tracing node-slot.ts's own documented failure mode against the
ref/effect shape in Listing.tsx, not by a live browser (none available
here); `search-hint-width.test.ts` exercises the callback ref directly
against fake elements, including the node-swap case that reproduces the
bug's exact shape.

**C2.** `showCompletion` requires a non-null completion target and at least
one item, both of which an empty query always fails, so a field focused
with nothing typed renders no dropdown at all. `listing/search-examples.ts`
gates a three-row panel onto that exact gap (`showSearchExamples`) and owns
the copy: `*.csv` ("CSV files in this folder"), `.csv` ("CSV files in this
folder and everything below it"), `~/work/*/*.csv` ("searches from ~/work
instead of here") — the first two written to read as a contrast on purpose.
The panel reuses `.listing-completion`'s surface and `.listing-completion-row`'s
padding/hover rhythm; a stacked modifier (`.listing-completion-example`)
replaces the side-by-side name/hint a path row uses, since an example pairs
a pattern with a full sentence rather than a one-word file hint. A click
writes the pattern into the field and keeps focus there (mirroring
`acceptCompletion`) rather than searching it blind, and its `onMouseDown`
carries the same `preventDefault()` a real completion row uses — without it
the click-away blur rule would discard the just-inserted, uncommitted query
before it ever got read. `showsSearchHits` and the blur truth table are
untouched; the panel only ever renders in the query-empty branch that
`showCompletion` already excludes itself from.

Verification: `bunx tsc --noEmit` clean; `bun test --run` — 3331 pass, 0
fail (3323 baseline plus 8 added: 4 in `search-hint-width.test.ts`, 4 in
`search-examples.test.ts`); `node scripts/check-boundaries.mjs` —
`boundaries OK (502 files)`; `bun run build` succeeds.

No test assertion in `search-bar-expand.test.ts` referenced the focus
border colour or the star's background — that file's rules are scoped to
the `.searching` class family and to whether the star still stands down
with the bar (it does not; item B changes nothing about that), so nothing
there needed updating.

## Brief 23

**D1 (item A — verifying the trace).** The caveat's `!pending` guard
(`index-caveat.ts`'s `searchCaveat`) does not receive `useListingSearch`'s
raw local `pending` state directly — `Listing.tsx` passes it `scanPending`,
which `index-source.ts` derives as `progress.answerComing = pending ||
polling`. These are different names, but not a different mechanism: in the
URL-restore reproduction, `polling` is false (no rescan is running), so
`scanPending` reduces exactly to that root `pending` value, and the trace's
conclusion — a debounce-armed-late flag reaching the guard late — holds.
The imprecision is worth naming because it means the fix has to reach
`scanPending`'s derivation chain, not just the raw state in isolation; it
doesn't mean the trace pointed at the wrong bug.

**D2 (item A — a new flag instead of reusing `pending`).** The brief's
prescribed fix ("arm the flag when the effect schedules the request, not
when it fires") was implemented as a new, dedicated `requestComing` state
rather than moving the existing `setPending(true)` earlier. Moving
`setPending` earlier was the literal first reading, but `pending` also
drives the spinner and the heavy-dim treatment (`unsettled`/`slow`), and
`PENDING_INDICATOR_MS` equals `INSTANT_DEBOUNCE_MS` at 200ms each — so
re-arming `pending` on every debounced effect rerun during a multi-keystroke
typing session would leave it continuously true for the whole session,
turning the spinner on and keeping it on while the user types, a regression
the brief asked to be checked for. `requestComing` is armed and cleared in
parallel with `pending` (same early returns, same memo-hit branch, same
success/rejection handlers) but wired only into the caveat call site in
`Listing.tsx`; `pending`'s own consumers are untouched. Confirmed no
spinner regression: all pre-existing `scanPending`-assertion tests in
`useListingSearch.render.test.ts` pass unchanged.

**D3 (item A — genuine staleness still shows the caveat).** Added a
dedicated test (`describe("a dir-watch bump while searching: the deferral
itself is the caveat")`) ahead of the existing "a completed scan" test,
covering the case `listing/revalidate.ts`'s `shouldReconcile` deliberately
declines: a dir-watch bump during an active search, where `behind` becomes
true and no re-ask is ever scheduled — `requestComing` stays false and
`searchCaveat` still returns the "not refreshed" caveat. This is
unaffected by D1/D2 because `requestComing` never arms outside an effect
run that is about to schedule a request.

**D4 (item A — regression test, TDD).** `describe("a URL-restored search
racing the app's own startup scan")` mounts with `urlSync=true` (query
seeded before first render, `searching` starts true), resolves the first
rank answer, calls `noteIndexLifecycle()` matching the existing completed-
scan test's shape, and asserts the caveat is null before advancing the
clock past the debounce. Confirmed failing pre-fix (`git stash push -u`
scoped to the four source files, test file left in place): `not refreshed`
returned instead of `null`. Confirmed passing post-fix, 28/28 in the scoped
file.

**D5 (item B — 440px, `max-width` not `width`).** The examples panel
(`.listing-completion-row.listing-completion-example`) gets `max-width:
440px` — inside the requested 420–480px band, sized to its own two-line
pattern/hint content with headroom for the longest hint sentence in
`search-examples.ts`. `max-width` rather than `width` so a field narrower
than 440px still constrains the panel to the field's own width (the row's
normal block-layout fallback) instead of overflowing it. No change to
`.listing-completion-row` itself (the plain path-completion dropdown), so
that rule's own full-field width is untouched. Left alignment under the
magnifier is not a separate rule — it falls out of the row already being a
block box with no auto margins, at the same `padding-left: 28px` gutter
the input's own text already uses.

Verification: `./frontend/node_modules/.bin/tsc --noEmit --project
frontend/tsconfig.json` clean; `bun test --run` — 3333 pass, 0 fail (3331
baseline plus 2 added in `useListingSearch.render.test.ts`); `node
frontend/scripts/check-boundaries.mjs` — `boundaries OK (502 files)`; `bun
run build` (run from `frontend/`) succeeds, bundle written to
`fused_render/static/shell-dist/`.

Item B's appearance at wide/narrow field widths was reasoned from the CSS
(block-box left alignment, `max-width` fallback behavior) rather than
observed in a browser — no display is available here.

## Brief 24

**D6 (item E — the cap belongs on the surface, not the row).** D5 capped
`.listing-completion-row.listing-completion-example` at 440px, but that row
is a block box inside `.listing-completion` — the element that actually
paints the panel's background, border and shadow — and a `max-width` on a
child cannot shrink the parent surface around it, so the panel kept
rendering full field width regardless. Fixed by capping the surface
instead: the examples panel gets its own modifier class,
`.listing-completion.listing-completion-examples`, carrying `max-width:
460px` (kept in the same 420–480px band as D5's original number, `max-width`
again rather than `width` so a narrower field still constrains it). The
row's own now-inert `max-width: 440px` was removed rather than left
stranded. The plain `.listing-completion` rule (the real path-completion
dropdown) is untouched and stays uncapped, since it lists real paths of
whatever length rather than fixed example strings.

**D7 (item A — the Enter-to-open-and-search row gets the one accent wash
in this family).** Of the family of `.status-message` rows in the search
body, exactly one (the `searching && !showsSearchHits` banner built from
`enterPrompt`, Listing.tsx) is an instruction rather than a state report,
so it alone carries a new modifier class, `listing-enter-row`, applied
alongside `status-message` on that `<td>` and nowhere else. The rule lives
in `explorer.css` (not `preview.css`, where the shared `.status-message`
base and its `.error` variant live) — colocated with the rest of the
search-field-specific chrome, and the file the repo's established
CSS-parsing test pattern already reads. It washes the row with
`background: rgba(var(--accent-rgb), 0.13)` and `color: var(--accent-soft)`
— never `--on-accent` or a solid `--accent` fill, which would read as an
error/alert rather than an instruction. No left accent bar: the wash plus
the recolored text already read as two combined signals distinct from the
plain-grey siblings, and a third (a border) risked tipping the row toward
"warning label," which the row should not read as. This is a judgment
call, not something observed in a browser — no display is available here,
so if the wash alone doesn't read as attention-grabbing enough once
someone can look at it, the border is the next thing to try. Two things in
the brief's framing did not hold up under a grep of `explorer.css`: there
is no literal row-striping rule anywhere in the file (only `tr.row:hover`
and `tr.row.selected` differ visually from a resting row), so "check it
reads against the striped rows beneath it" was evaluated against hover/
selected only.

**D8 (item D — the search-hits header names its base).** The single
header the search-hits table renders (`showsSearchHits`'s branch of
Listing.tsx's `<thead>`) said only "Path," with nothing answering "path
relative to what" — the merged search field replaced the crumb strip that
used to answer that. It now reads "Path in ~/Downloads" (or whatever
`searchBase` contracts to), computed once as `baseLabel` and used for both
the header text and its `title` attribute; the header falls back to the
bare "Path" label when `searchBase` is empty, and a new `col-search-base`
class truncates a long base with an ellipsis (`max-width: 0` under the
table's existing `table-layout: fixed`, plus `overflow: hidden;
white-space: nowrap`) rather than wrapping the sticky header taller. The
header stays a plain, non-sortable `<th>` — no `sortable` class, no click
handler. The "~" contraction is a new `contractHome(fsPath, home)` helper
(`listing/home-path.ts`), matching the inline logic already duplicated in
`Breadcrumb.tsx`, `listing/path-crumbs.tsx` and `Panel.tsx` — grepping all
three found exactly three copies of that logic, not the four the brief
described; only the new fourth call site (this header) was added, and the
three existing ones were left exactly as they are, per the brief's own
scope. The helper's shape (`fsPath.startsWith(home + "/") ? "~" +
fsPath.slice(home.length) : fsPath`) is identical to what all three sites
already inline, so it would drop into any of them with no behavior change
— left for a follow-up, not done here. `useListingSearch.ts`'s own
definition of `searchBase` (`answer.base` while searching, else `fsPath`)
means it is never actually empty in the current implementation, so the
bare-"Path" fallback is traced as currently unreachable rather than
exercised by a live case — implemented anyway, since the header must not
break if that ever changes.

**D9 (item B — a real clear button, not the native cancel).** The native
WebKit `::-webkit-search-cancel-button` stays suppressed (explorer.css
already documented why: it collides with the absolutely-positioned count
chip). A new `.listing-search-clear` button sits between the count/spinner
chip and the star — the star stays the field's own last child — shown
only while `query !== ""` (`hasClear`). It reuses the same teardown Escape
already ran, pulled out into `clearSearchQuery()` (clears the query,
un-pins the box) so the two call sites can never drift apart; unlike
Escape, the button never blurs — the press is caught on `onMouseDown` with
`preventDefault()`, the same click-away-blur-discard pattern the
completion and example rows already use, so focus stays in the field.
Styled as a quiet pill with the star's own `--ctl-quiet-bg`/
`--ctl-quiet-bg-hover` token pair. Positioning followed the existing
`--pin-right`/`has-pin`/`wide-pin` matrix: a new `has-clear` box class
reserves an extra 24px hit area plus a 6px gap, both in the count chip's
own `--pin-right` offset and in the input's `padding-right` (a new
`has-clear` variant of every existing has-pin × wide-pin × crumb-slot
combination, plus a bare `has-clear:not(.has-pin)` case for a freshly
typed query with no chip pinned yet). `Panel.tsx` renders no
`.listing-search` markup at all (confirmed by grep — no `Listing`/
`listing-search` reference anywhere in that file), so panel-mode bars are
unaffected by any of this; there was nothing to check there beyond
confirming the file doesn't touch this code.

**D10 (item C — spaced, muted "/" separators without touching the
string).** A multi-segment search-hit path now reads as layered folders:
`renderHighlightPath` (`listing/bits.tsx`), an opt-in sibling of
`renderHighlight`, wraps each "/" inside a highlight segment's own text in
a `<span className="path-sep">` (styled with `margin: 0 2px; color:
rgba(var(--fg-muted-rgb), 0.6)` in `explorer.css`, which also covers
`FilesHome.tsx`'s `.fh-result-path` since both stylesheets load into one
global bundle via `shell.css`). The character it wraps is still the
literal "/" from the original string — nothing is inserted or removed —
because `highlightSegments` (`platform/lib/fuzzy.ts`) computes its match
runs against raw character offsets, and the path still has to reproduce
exactly on copy. The wrapping only rearranges a segment's own children, so
a fuzzy match whose run straddles a separator stays the one continuous
`<mark>` `highlightSegments` already produced for it, never two marks with
a bare slash between them — covered by a dedicated
`highlight-path.test.tsx` case (`"ab/cd"` matched at `[1, 2, 3]`, straddling
the `/` at index 2), confirmed failing before `renderHighlightPath`
existed. Listing.tsx's search-hit rows (`entry.rel`) and FilesHome.tsx's
path span (`display`) use the new helper; FilesHome.tsx's name span and
every other `renderHighlight` caller are unchanged, and a test asserts
`renderHighlight` itself gains no separator wrapping.

Verification for the whole brief: `./frontend/node_modules/.bin/tsc
--noEmit --project frontend/tsconfig.json` clean; `bun test --run` — 3363
pass, 0 fail (3333 baseline plus 30 added across the five items' test
files); `node frontend/scripts/check-boundaries.mjs` — `boundaries OK (509
files)`; `bun run build` (run from `frontend/`) succeeds, bundle written
to `fused_render/static/shell-dist/` (the only build warnings are
pre-existing manual-chunking notices about `router.ts`/`api.ts` being both
statically and dynamically imported, unrelated to this brief). Every test
added for E, A, D, B and C was confirmed to fail before its corresponding
fix, each via a `git stash push -u` scoped to only that item's changed
source files.

All visual claims above (the accent wash's legibility, the clear button
and star sitting side by side, the examples panel's width against the
field at various sizes) are reasoned from the CSS and JSX rather than
observed — no display is available in this environment.

**D11 (item C — the path stays case-sensitive; the row's own label chrome
does not).** The search-hits header's "Path in {base}" text inherited the
row's uppercase transform, which silently altered a real Linux path (a
correctness defect, not a style nit, on a case-sensitive filesystem). The
"Path in " chrome text stays uppercase with the rest of the header row;
only the path itself is wrapped in a new `<span className=
"col-search-base-path">` (`Listing.tsx`) carrying `text-transform: none`
(`explorer.css`), so the two halves of the same header cell now
deliberately diverge in case. The exact-home case (`baseText = searchBase
=== home ? "~" : contractHome(searchBase, home)`) is computed at this one
call site rather than folded into `contractHome` itself — the helper's
existing contract (home itself keeps its full path; only strictly-below
paths contract) is exactly what the three crumb-strip call sites depend
on, and the header wants the opposite behavior for the exact-home case
only. Folding it into the helper would have silently changed those three
sites too; a local ternary at the one call site that wants it does not.

**D12 (item A — the field opens seeded with the current path, selected;
`seed` becomes required).** Both `requestSearchFocus` callers in
`Breadcrumb.tsx` — click-to-edit and Ctrl/Cmd+L, the only two callers,
confirmed by a full grep of the codebase — now pass
`displayPathRef.current`, the same "~"-contracted current path both
gestures already compute for their own now-removed path-only editor. With
every caller supplying a seed, `search-focus.ts`'s `seed` parameter moves
from optional to required (`type Listener = (seed: string) => void`), and
the subscriber in `Listing.tsx` drops its old `if (seed !== undefined)`
branch — it now always seeds, selects, opens, and focuses.

That covers both callers reaching the field through Breadcrumb.tsx's
document-level click/keydown handlers, but neither of those handlers ever
fires for a press that lands directly on the real `<input>` — `input` and
`.listing-search` both sit in `BAR_EDIT_EXCLUDE`, and the crumb text,
magnifier, and `.listing-search-crumbs` strip all use `pointer-events:
none` specifically so a click on any of them falls through to the input
underneath. That fall-through press never reaches `requestSearchFocus` at
all; it goes straight to the input's own `onFocus`. So the input's
`onFocus` handler independently seeds too, whenever `query === ""`:
`setQuery(contractHome(fsPath, home)); seedSelectRef.current = true`,
before opening and marking the field active. Between the two, every click
that opens the merged field over a claimed folder now seeds it — the
`requestSearchFocus` path for Breadcrumb-mediated gestures, the `onFocus`
path for a direct hit on the field itself.

The seed is placed with `setQuery`, not typed into the DOM directly, so
it is exactly as "uncommitted" as anything else typed into the field —
`search-provisional.ts`'s existing blur truth table (empty → unpin,
non-empty-and-uncommitted → discard, non-empty-and-committed → keep-open)
governs it unchanged, and Escape's existing revert handles it unchanged
too. `seedSelectRef` (set alongside every `setQuery(seed)` call, read by a
`useEffect` on `[query]` to call `.select()` only once React has actually
committed the value) exists because `.select()` called synchronously right
after `.focus()` would select whatever the input held BEFORE this render's
`setQuery` — React 18 batches the two `setState` calls, so the DOM has not
caught up yet at the point `.select()` would naively run.

One consequence, reported rather than fixed since the brief scoped this
item to seeding, not to the examples panel: `search-examples.ts`'s
`showSearchExamples` gate requires `query === ""`, so the examples panel
—  previously the first thing an empty-query click showed — now rarely
appears on a fresh click, since the click itself populates `query`
immediately. It still shows for the one path left where the field opens
genuinely empty: Ctrl/Cmd+L or a click over an UNCLAIMED folder's bar,
where `requestSearchFocus`/the claimed-folder seeding path never runs at
all, or after the user clears a seeded query by hand.

`closedByClickAwayRef` is untouched by any of this — it belongs entirely
to Breadcrumb.tsx's own unclaimed-folder plain-text path editor, a
separate code path from the merged search field this item changes.
Traced, not observed: no display is available in this environment.

**D13 (item B — the magnifier moves to the trailing edge, sharing the
clear button's slot).** Read literally, "take the glyph out of absolute
positioning and make it a real member of the trailing group" would mean a
flex conversion of the whole pin/chip region — count chip, spinner, clear
button, and star all currently live as siblings positioned independently
off `--pin-right`/`right: 8px`/`right: 38px`, each with its own gate
(`hasPin`, `hasClear`, `barSearchSlot`) and its own class-matrix padding
reservation on the input. Converting that whole region to normal flow
would touch the count chip's gate, the `wide-pin`/`has-clear` combinatorial
padding matrix, and the star's guaranteed last-child position — all things
the brief explicitly said not to change. Taken instead as "move the glyph
out of its OWN fixed left gutter and into the group of things living at
the trailing edge" — the lighter interpretation — the magnifier now shares
`.listing-search-clear`'s own absolutely-positioned slot (`right: 8px`
base, `right: 38px` inside `.crumb-search-slot .listing-search-box` to
clear the star), rendered in that same JSX position as the clear button's
own else-branch: `{hasClear ? <button className="listing-search-clear">
… : <span className="listing-search-glyph">…}`. The two are mutually
exclusive on the exact same condition already gating the clear button, so
sharing one CSS position needs no new state and no new class.

The magnifier stays pure decoration — no click handler, `pointer-events:
none` unchanged — so a press on it still falls through to the input
beneath, the same click-through behavior it always had; only which edge
that input sits under moved. Reclaiming the glyph's old fixed `left: 9px`
gutter also let the input drop its `padding-left: 28px` (back to the base
`padding-right: 10px` rule, symmetric now — no left override at all), the
crumb strip drop its `left: 28px` (now `left: 10px`, the input's own text
edge), and the completion/example rows drop their `padding-left: 28px`
(now `10px`, still lined up under the query text since neither the input
nor the row reserves gutter space for the glyph any more).

The resting bar — an idle, unfocused claimed-folder row showing the crumb
strip and nothing else pinned — still shows the magnifier at its new
position (`hasClear` is false with an empty query, so the glyph branch
renders), so the box never reads as missing its one search affordance;
traced through the render condition, not observed. The trailing region at
its most crowded (a committed query, a pinned match count or caveat chip,
the clear button occupying the shared slot, and the star) is unchanged
from before this item — the glyph and the clear button were already
mutually exclusive occupants of one slot, so this item does not add a
fifth simultaneous element, only relocates where that slot sits. Panel-mode
bars are confirmed unaffected: `grep -n "listing-search" Panel.tsx` finds
nothing, so panel instances have no magnifier, clear button, or crumb
strip to move in the first place. `showsSearchHits`'s return value, the
count chip's gate on it, the blur truth table, `generationBehind`,
`EntryActionsMenu.tsx`, and the star's last-child position are all
unchanged.

Verification for this round (items C, A, B): `./frontend/node_modules/
.bin/tsc --noEmit --project frontend/tsconfig.json` clean; `bun test
--run` — 3372 pass, 0 fail, up from the 3363-pass baseline this round
started from, across new and rewritten cases in `home-path.test.ts`,
`search-base-header.test.ts`, `search-focus.test.ts`, and
`search-clear-button.test.ts`; `node frontend/scripts/
check-boundaries.mjs` — `boundaries OK (509 files)`; `bun run build` (run
from `frontend/`) succeeds, bundle written to `fused_render/static/
shell-dist/` (only pre-existing manual-chunking warnings, unrelated to
this round). Every test added or changed for C, A and B was confirmed to
fail before its corresponding fix, each via a `git stash push -u` scoped
to only that item's changed source files, restored via `git stash apply`
(never `pop`).

All visual claims in this round — the header's path staying
case-sensitive, the field opening pre-filled and selected, the magnifier's
new position and the resting bar's appearance, the trailing region's
crowding at its busiest — are traced through the CSS and JSX, not observed
in a browser, except item C's original defect, which the requester
verified directly.

## A folder that changes and then goes quiet never gets reindexed

**Mechanism.** `note_folder_opened` (`fused_render/index/freshness.py`) is
reached only from a listing fetch, ~`FRESHNESS_DELAY_S` (3s) after the
directory-mtime change that made the client re-fetch in the first place. The
`QUIET_S` (30s) gate compares that check's `now` against the folder's own
`disk_ns`, so a check fired by the very change it is inspecting is refused by
construction — it always lands inside the window. Nothing else asks again:
once the folder is actually quiet, its mtime stops moving, so the watcher
never broadcasts again, so no further listing is fetched, so no further check
runs. A page reload works only because a fresh mount happens to list at some
arbitrary later moment, comfortably past 30s.

The pre-existing `QUIET_S` comment asserted "the next open after the churn
stops still fires" — false whenever nothing re-opens the folder, which is
exactly the reported case (a user re-running one search, not re-opening the
folder). It now reads:

> How long a directory must have been settled before its staleness is acted
> on. A churny directory (a build tree, a cache) has a mtime that moves
> continuously, so it is never quiet and never triggers — which is what stops
> it queueing scan after scan. There is no guarantee a LATER open ever asks
> again once the churn stops — a user who sits still after the last change
> never fires another listing — so a refusal here is not the end of the
> question: it comes back as `FreshnessCheck.retry_after`, which tells the
> caller exactly when the folder will have been quiet for this long, so it
> can ask again itself instead of depending on one to arrive by luck.

One claim in the brief turned out to be wrong: it stated flatly that
`freshness.py` is entirely untested ("no `test_freshness.py` anywhere, no
test references `freshness`"). `tests/test_index_freshness.py` already
existed with 22 passing tests, and `tests/test_index_api.py` already had a
substantial freshness-check test section (throttle, defer, per-root
debounce). The gap was narrower than described: the specific race — a
refused check with no way to distinguish "still churning" from every other
refusal — was untested, not the module as a whole.

**The fix.** `note_folder_opened` returns a `FreshnessCheck` NamedTuple
(`started: str | None`, `retry_after: float | None`) instead of `str | None`.
`retry_after` is set in exactly one place: the `QUIET_S` gate, computed as
the actual remaining wait (`quiet_at - now`) rather than a bare refusal. Every
other early return (outside the roots, mount-guarded, within
`MIN_INTERVAL_S`, vanished path, live run in progress, not actually stale)
keeps returning the "never ask again" `FreshnessCheck()`.

`routers/index.py`'s `_run_freshness_check` schedules a retry when
`result.retry_after is not None`, via a new `_schedule_freshness_retry(path,
root, delay)`.

**Retry policy — one honest retry, coalesced per root, never chained.**
`_schedule_freshness_retry` keeps a `root -> pending threading.Timer` dict;
scheduling for a root that already has one pending cancels it and replaces it
with a timer for the new deadline, so a folder touched fifty times in a row
ends with exactly one live timer, not fifty. The retry itself
(`_run_freshness_retry`) never reschedules regardless of outcome — if it
finds the folder started, good; if `note_folder_opened` still returns
`retry_after` (the folder is somehow still churning), that is left alone
rather than chained. A directory that never truly settles gets one wasted
retry, not an unbounded chain; the next real listing starts the decision
over from scratch. This was a deliberate choice among the options the brief
allowed (one retry / a bounded number / re-arm only on a fresh change) —
one retry is the simplest policy that still fixes the reported case (a
folder that changes once and then goes quiet), and re-arming on a fresh
change is already what happens for free: a later listing calls
`_run_freshness_check` again independently of any pending retry.

**Clearing `FRESHNESS_CHECK_S` without retuning it.** The retry calls
`freshness.note_folder_opened` directly, bypassing `_run_freshness_check`
(and therefore `_freshness_due`/`FRESHNESS_CHECK_S`/`_freshness_wait`)
entirely. This is not the same as raising or removing the throttle for
everyone: `FRESHNESS_CHECK_S` paces new demand for checks arriving from
browsing, and the coalescing above already guarantees at most one pending
retry per root at any moment — the retry is the continuation of a check that
already earned its slot and was told to come back at a specific time, not a
second independent demand. Nothing about this lets a root be hammered faster
than one retry per triggering refusal.

**Timer mechanism.** `_schedule_freshness_retry` does not import
`index_touch._real_schedule` — `index_touch.py` imports this module from
inside its own functions (to reach `runner` and `_wake_index_job_bridge`), so
importing it back here would be circular. The three-line pattern
(`threading.Timer(delay, fn); t.daemon = True; t.start()`) is duplicated
directly instead, keeping the same "never holds the process open" property
(a daemon timer) without inventing a second scheduling primitive.

`note_folder_opened`'s "never raises" promise extends to the retry:
`_run_freshness_retry` wraps its body in `try/except Exception`, logging and
returning rather than propagating — a background timer failing must not take
the process down.

**Tests.** Failing-first: added
`test_a_folder_that_goes_quiet_after_the_check_refused_it_still_gets_scanned`
to `tests/test_index_api.py`, reproducing the reported case at the router
level — a check ~3s after a change is refused, and only a later,
un-prompted retry (no further listing) finds the folder stale and scans it.
Confirmed failing on pre-fix code (`AttributeError:
_schedule_freshness_retry` does not exist) by temporarily restoring the two
pre-fix source files from `HEAD` with the test changes kept, running just
that test, then restoring the fix. Also added
`test_the_freshness_retry_is_coalesced_per_root` (fifty scheduling calls for
one root leave exactly one live timer, for the latest deadline) and
`test_the_freshness_retry_does_not_chain_a_second_one` (a retry that still
gets `retry_after` back does not schedule another). `freshness.py`'s own
suite gained `test_churning_is_the_only_refusal_that_asks_to_be_retried` and
`test_a_change_that_turns_out_not_to_be_stale_still_reports_retry_after`,
alongside updating every existing assertion for the `FreshnessCheck` return
type. All driven by the `now` parameter and a fake clock — no sleeping.

Full results: `tests/test_index_api.py` + `tests/test_index_freshness.py` —
131 passed. Every other test file importing `routers.index`
(`test_git_repos_api.py`, `test_index_cancel.py`, `test_index_fda_gate.py`,
`test_index_jobs.py`, `test_index_rank_concurrency.py`,
`test_index_scan_on_demand.py`, `test_index_search.py`,
`test_index_touch.py`, `test_mac_update.py`, `test_shell_prefs.py`) — 311
passed, 5 failed, 1 skipped; the 5 failures are exactly
`tests/test_index_scan_on_demand.py`'s pre-existing failures called out as
not-mine, confirmed identical (same tests, same assertions) against
unmodified `HEAD` via a tagged `git stash push -u`/`apply`/`drop` cycle, so
nothing was added to that count.

**D14 (a query naming exactly the folder already open is not a pending
search).** Traced, not observed: no display is available in this
environment. Confirmed the mechanism the brief named, by reading the code
rather than re-deriving it: clicking the bar / Ctrl-Cmd+L both call
`requestSearchFocus(displayPathRef.current)` (Breadcrumb.tsx), which reaches
the one subscriber in Listing.tsx that does `setQuery(seed)`. That seed
always starts with `"~"`, one of `escapesBase`'s own conditions
(`query-base.ts`), so `useListingSearch`'s gate (decision 4) never opens for
it on its own — `escapes` is true and `committedGate.current` is null until
Enter, so `gateOpen` is false, no fetch is ever issued, and `searchState`
stays `IDLE_SEARCH`. `showsSearchHits` (`showingSearchHits`,
`search-body-mode.ts`) is therefore already `false` for this case, so the
folder's own rows already render underneath — the banner render at
`Listing.tsx`'s `if (searching && !showsSearchHits)` only ever WRAPS them
with an extra row on top, it does not replace them. The banner's text comes
from `enterPrompt(typedAddress, query)`; `typedAddress.status` is `"exists"`
for the open folder (once its own debounced `statPath` round trip lands),
so the prompt takes the resolved-path branch and names the leaf —
`Press Enter to open random`. The second symptom, the "0 matches" footer, is
`statusLine` (`listing/status-line.ts`), which branches purely on the
`searching` boolean it is handed and, once in that branch, reports
`hits.length` — 0, because the gate above never let a fetch happen. Both
symptoms are visible for the same reason: nothing about this specific query
is any different from a real escaping query as far as `searching`,
`escapesBase`, or the gate are concerned, even though the folder it names is
already open.

Fixed at the gate's OWN render site (Listing.tsx), not inside `escapesBase`.
`escapesBase` takes neither `fsPath` nor `home` by design (its own leading
comment) and answers a purely syntactic question — widening it to compare
against the open folder would turn one narrow yes/no predicate into two
unrelated ones sharing a name, and every other caller of `escapesBase`
(there is only the one, in `useListingSearch.ts`) would need to start
passing `fsPath`/`home` it has no other use for. `Listing.tsx` already holds
both, has already resolved a query into an absolute address once (via
`useTypedPathAddress`/`listingAddress`, decision 5) for exactly this reason,
and is the one place both symptom sites (the banner branch, the `statusText`
computation) live. Reusing that same resolution rather than duplicating a
second path-comparison also means this needed no new normalization logic:
`listingAddress` (`listing-address.ts`) already turns `"~/…"` and an
absolute spelling into the same string and already strips a trailing slash.

The new pure predicate is `queryNamesOpenFolder(query, fsPath, home)`
(`listing/query-current-folder.ts`): `listingAddress(query, fsPath, home)`
resolved and compared against `fsPath` with its own trailing slash
stripped. It is synchronous and cannot flash true-then-false while a stat is
in flight, unlike `typedAddress` (which this predicate deliberately does not
use, for that reason) — `fsPath` is already open, so there is nothing to
confirm on the filesystem, only whether the typed text names it. A glob or
one more path segment past the folder (`~/Fused/local/random/*.svg`,
`~/Fused/local/random/ico`) resolves via `listingAddress` to `null` or to
something other than `fsPath`, so both stay real, ungated queries exactly as
before — this predicate only recognizes the exact folder, either spelling,
with or without a trailing slash.

Two render sites read it, both in `Listing.tsx`:
- The banner gate: `if (searching && !showsSearchHits && !isOpenFolderQuery)`.
- `statusText`'s search branch: a new `showsSearchFooter = searching &&
  !isOpenFolderQuery` replaces the raw `searching` fed to `statusLine`
  (and gates the selected-bytes/selected-folders loop the same way, so a
  selection made while the field holds the open folder's own path still
  reports its byte sum instead of silently going quiet the way a real
  search's selection does).

`showsSearchHits` itself is untouched — it was already `false` in this
case, which is exactly why the brief's "do not change what it returns for
any other case" reads as "leave it alone entirely" here: there is no case
where its return value needed to change. The search count chip's own gate
(`showsSearchHits && searchState.status === "ok" && hits.length > 0`) is
likewise untouched and was never reachable in this scenario either way.
`escapesBase`'s contract and leading comment are untouched — this predicate
does not widen it, so there was nothing in that comment to correct. The
`seedSelectRef` comment above (`Listing.tsx`) DID need correcting: it
described the seed sitting uncommitted as the whole story, written before a
plain click could seed the field with the folder already open. It now says
the gate still never opens for the seed on its own (still true — decision 4
is unchanged), but that being ungated is no longer the same thing as
reading like a pending search: `queryNamesOpenFolder` is what keeps this one
uncommitted query from painting the banner or the footer, and typing
anything past the folder's own path — even one more segment — leaves that
exemption and is uncommitted in the ordinary, banner-showing sense again.

The four path-spelling cases:
- Field holds exactly the open folder (absolute, `~`-contracted, with or
  without a trailing slash — four spellings): `queryNamesOpenFolder` is true
  in all four; no banner, no "0 matches" footer, the folder's own rows
  render (they already did, underneath the banner, before this fix — only
  the banner and the footer text change). Traced through
  `listing-address.ts`'s own trailing-slash stripping and `~`-resolution,
  confirmed by `query-current-folder.test.ts`'s matching cases; not
  observed.
- Field holds the folder's path plus more (`.../ico`, `.../*.svg`):
  `listingAddress` resolves to a different path or to `null`;
  `queryNamesOpenFolder` is false; unchanged from before this fix — a real,
  gated query. Traced, and covered by `query-current-folder.test.ts`.
- Field holds a different absolute or `~` path: resolves to a different
  address; `queryNamesOpenFolder` false; unchanged — banner and "N matches"
  footer both still show once a real answer lands or while uncommitted.
  Traced, covered by `query-current-folder.test.ts`.
- Ctrl/Cmd+L: confirmed (by reading Breadcrumb.tsx's two call sites) to call
  the identical `requestSearchFocus(displayPathRef.current)` the click uses,
  landing in the same Listing.tsx subscriber — no separate code path exists
  for it, so it was not re-tested separately beyond confirming that identity
  holds. Traced.

Escape-to-revert and clicking-away-to-discard are untouched: neither this
fix nor its predicate touches `search-provisional.ts`'s blur truth table,
the Escape handler, or anything upstream of `query` itself — it only changes
what two RENDER sites downstream of `query` do with an already-known value.

What the bar looks like while holding the unmodified current path: `searching`
itself (the length-gated boolean `useListingSearch` computes) is untouched
by this fix and stays true, so `.searching`'s CSS
(`#breadcrumb:has(.listing-search.searching)`) still stands the crumb strip
down and lights the border — the field stays visibly focused, editable, and
its seeded text stays selected for replacement — the field reads as active,
which is correct: the user clicked into it deliberately. `hasClear`
(`query !== ""`) is untouched and stays
true, so the clear button (magnifier's shared trailing slot, decision D13)
still shows. `hasPin` and the inline search-count chip are unaffected
because `searchCount` was already `null` in this case (gated on
`showsSearchHits`, already false) before this fix. The only chrome that
changes is the banner row and the bottom-left status text — the "search is
pending" claims — not the "this field is active" claims. Traced through the
render logic; not observed.

Tests: `query-current-folder.test.ts` (new) covers the predicate directly —
all four spellings of the open folder, a segment past it, a glob past it, a
different absolute path, a different `~` path, a plain filter word, and
`home === undefined` for both the absolute and the `~` spelling.
`search-enter-banner.test.ts`'s first test asserts the literal gate text in
`Listing.tsx` includes `!isOpenFolderQuery`; confirmed to fail on the
pre-fix source via a tagged `git stash push -u -m
"brief27-listing-only-check" -- frontend/src/apps/explorer/Listing.tsx`
(only `Listing.tsx`, keeping the new test), `bun test --run` on that one
file (`Expected: > -1, Received: -1`), then `git stash apply` and `git
stash drop` on that same entry — no stash entries left behind.
`enter-prompt.test.ts` and `query-base.test.ts` needed no changes: neither
function's contract moved.

Verification: `./frontend/node_modules/.bin/tsc --noEmit --project
frontend/tsconfig.json` clean. `bun test --run` — 3383 pass, 0 fail, up from
the 3372-pass baseline this round started from (11 new cases, all in
`query-current-folder.test.ts`). `node frontend/scripts/check-boundaries.mjs`
— `boundaries OK (511 files)`, up from 509 by the two new files
(`query-current-folder.ts`, `query-current-folder.test.ts`). `bun run build`
(from `frontend/`) succeeds; only the pre-existing manual-chunking warnings,
unrelated to this change.

## D15 — a mode chip, a retired magnifier, a keyboard hint

The field now says which of its two jobs it is doing, at both ends of the box.

The chip, at the leading edge (`.listing-search-mode`, `Listing.tsx`), is driven
directly off `chipIsSearch = searching && !isOpenFolderQuery` — `searching`
(a non-empty query, already computed by `useListingSearch`) layered onto
`isOpenFolderQuery` (D14's `queryNamesOpenFolder`, already computed by this
render). Not a new predicate: `isOpenFolderQuery` alone is false for an empty
query too (`listingAddress` returns `null` for empty/whitespace input), so an
untouched field would otherwise show "Search" at rest — `searching` is what
tells the two resting cases apart. `Path`/folder-outline when false, `Search`/
magnifier when true; the magnifier glyph is the one the trailing slot used to
own, so the meaning moves rather than doubling.

The chip is a readout: `pointer-events: none`, no hover rule, no
`--ctl-quiet-bg` pair — traced by grepping for both across `explorer.css` and
confirmed absent. `Search` takes `--accent`, matching the enter-row wash's own
argument that the accent marks a pending action; `Path` stays `--fg-muted`.

Width went through a second pass after a user report against the first cut's
screenshot: `.listing-search-mode-label` had carried a `min-width: 34px` sized
for "Search", which meant the shorter "Path" state left its own trailing
padding behind the word before the crumbs began — the label's `gap: 4px` (on
the chip's flex container) already fixes the glyph-to-label distance, so the
label's own width could only ever add space AFTER the word, not before it,
which was the reported gap. The label now carries no width of its own.
The invariant that must still hold — the crumbs never shifting when the mode
word changes — moved to a single fixed offset on `.listing-search-crumbs`
(`left`) and `.listing-search-input` (`padding-left`), sized for the chip at
its widest state instead of for each state individually. That number was
measured, not estimated: `-apple-system, BlinkMacSystemFont, "Segoe UI",
Roboto, Helvetica, Arial, sans-serif` at `500 11px` (the label's own rule) run
through `CanvasRenderingContext2D.measureText` in headless Chromium (`chromium
--headless=new --dump-dom`, no display available in this environment) gives
"Search" a width of 34.85px; the offset is 8px (chip's own left inset) + 12px
(glyph) + 4px (glyph-to-label gap) + 35px (rounded up from the measurement) +
8px (clearance before the crumbs) = 67px. Chromium's Linux font substitution
is not macOS's San Francisco, so this is a close measurement rather than an
exact one for the shipped font; the number is small enough (67px, on a field
that is at minimum several hundred px wide before its own narrow-width rules
kick in) that a few px of font-substitution error has no visible consequence,
but a browser check against the actual rendering is still the way to confirm
it precisely. `.listing-completion-row`'s own left padding tracks the same
67px so a suggestion still lands under the query text it completes.

The trailing decorative magnifier (`.listing-search-glyph`, the ternary's
else-branch) is deleted outright, markup and CSS both — the chip's own
magnifier now carries that meaning, and grepping `Listing.tsx` and
`explorer.css` afterward turns up no remaining reference to the class. The
trailing slot's ternary (`hasClear ? clear : glyph`) becomes a single `&&`
(`hasClear && clear`): nothing renders in the query-less, unfocused-clear-slot
gap that leaves — Item C's hint is a distinct sibling condition, not the old
else-branch reused, because their gating conditions differ (`!hasClear` alone
for the glyph before; `!pinnedOpen && !hasClear` for the hint now).

The keyboard hint (`.listing-search-shortcut-hint`) shares that same trailing
CSS position (`right: 8px`, `right: 38px` inside a claimed crumb bar — the
exact numbers the deleted glyph used, confirmed by diffing against the
pre-deletion rule before it was removed) because its gating condition
(`!pinnedOpen && !hasClear`) never overlaps the clear button's own
(`hasClear`). `pinnedOpen` is not simply `!hasClear`'s negation, though: the
"unpin" blur path in `search-provisional.ts` sets `pinnedOpen` false while a
committed query remains, so `!pinnedOpen` alone would paint the hint on top of
a still-present clear button in that one state — `!hasClear` in the same
condition is what keeps them from colliding. The label reads `isMac ? "⌘L" :
"Ctrl L"`, `isMac` imported from `@platform/lib/platform` alongside the
existing `isMod` import — the app's one platform detection, not a fresh
`navigator` check, matching `shortcut-chord.ts`'s own rule that platform
detection stays singular. The key cap (`.listing-search-shortcut-hint kbd`)
carries the exact declarations `preferences.css`'s `.fh-ai-hint kbd` already
uses (`padding: 0 4px`, `border: 1px solid var(--border)`, `border-radius:
4px`, `background: var(--bg-alt)`) rather than a new style — duplicated
rather than shared across stylesheets because nothing in `Listing.tsx`'s CSS
file reaches into `preferences.css`.

Narrow-width absence for the hint is `container-type: inline-size` on
`.listing-search-box` plus `@container (max-width: 360px) { display: none }`
on the hint itself — a CSS query rather than a measured ref, because
`search-hint-width.ts`'s own comment records a `useLayoutEffect([])` version
of a similar measurement on this branch freezing on a null ref and never
recovering. The container query answers the BOX's own width, not the row's:
the row can stay as wide as the whole crumb bar while the box inside it,
squeezed by a deep path taking up the crumbs' share of that width, is the one
that actually runs out of room for the hint.

Panel-mode bars: traced by grepping `Panel.tsx`, which renders only
`<BookmarkStar name="Panel" />` per pane — no reference to `.listing-search-*`
markup or classes anywhere in that file, so none of this chip/hint work has
anything to touch there. `EntryActionsMenu.tsx` and its `title="App actions"`
were not opened. `showsSearchHits` and the search-count chip's gate on it are
untouched — grepped, only referenced where they already were.

Geometry: `--topbar-h` (48px) and `--topbar-pad-y` (8px) are untouched; the
only property this round changed on `.listing-search-input` besides adding
the trailing-slot elements was `padding-left` (10px → 67px) — vertical
padding, border, and height all stay exactly as D13/D14 left them, focused and
unfocused alike, since no rule touching `top`, `bottom`, `height`, or
vertical `padding`/`border` was edited.

Tests: `search-mode-chip.test.ts` (new) covers the chip's predicate
composition, its readout-not-control CSS, the accent/muted color split, the
label's lack of a reserved width, the fixed crumbs/input offset, the hint's
visibility gate, its platform-detection source, its kbd styling, and its
container-query narrow-width rule. `search-clear-button.test.ts` drops its
three magnifier-specific assertions (the ternary, the glyph's own CSS, and the
claimed-bar offset — none of those selectors exist any more) for one assertion
that no `listing-search-glyph` reference survives anywhere, plus three new
assertions for the hint occupying that same slot on the same offsets. Its one
surviving unmodified assertion — "the input reserves no left gutter keyed to
the magnifier's old name" — targets the compound selector
`.listing-search .listing-search-input`, a distinct rule from the base
`.listing-search-input` this round edited, so `padding-left`'s move to the
base rule leaves the compound rule's own declarations, and this assertion,
unaffected.

Pre-change failure, confirmed by a tagged `git stash push -u -m
"brief28-pretest-check"` (Listing.tsx and explorer.css only, keeping the new
and edited test files) followed by `bun test --run` on the two search test
files: 15 of 22 tests failed against the pre-chip/pre-hint source. A second,
narrower check after Item A and B were already committed — stashing out only
the Item C diff — showed 8 of 23 failing against the pre-hint source
specifically. Both stash entries were applied back and dropped by their own
recorded SHA; `git stash list` is empty.

Verification: `./frontend/node_modules/.bin/tsc --noEmit --project
frontend/tsconfig.json` clean at every one of the three commits. `bun test
--run` — 3389 pass after item A, 3387 after item B (net -2: three
magnifier-specific tests replaced by one), 3395 after item C, 0 fail
throughout, up from the 3383-pass baseline this round started from.
`node frontend/scripts/check-boundaries.mjs` — `boundaries OK (512 files)`
from item A onward, up from 511 by the one new file
(`search-mode-chip.test.ts`). `bun run build` (from `frontend/`) succeeds at
the final state; only the pre-existing manual-chunking warnings, unrelated to
this change.

## D16 — a settled zero-hit search still gets its count and its latency

The match-count chip's guard read `showsSearchHits && searchState.status ===
"ok" && hits.length > 0` — confirmed unchanged from the traced form before
touching anything (`Listing.tsx`, the block above `searchCount`'s
assignment). Withholding the chip on an empty answer withheld the latency
readout too, as a side effect: the latency branch (`else if
(searchState.status === "ok" && searchCount !== null)`) never runs once
`searchCount` stays `null`, so one guard produced two absences for the one
user-visible complaint ("how come we don't show counts and search timing
when no matches?").

The fix drops the `hits.length > 0` clause outright: the guard is now
`showsSearchHits && searchState.status === "ok"`. A settled answer of zero is
still a settled answer, and `resultCountLabel`/the chip's own terse form both
already treat 0 as a plural (`0 !== 1`, so the `"es"` branch fires) — no zero
special case needed anywhere downstream.

Consequences walked and confirmed, not assumed:

- `cappedAway` (`useListingSearch.ts`, `displayHits.length -
  visibleHits.length`) is 0 when `displayHits` is empty, since `capHits` of
  an empty array returns an empty array — the `top N of M` branch stays
  unreachable at zero hits, same as before.
- `hasPin` (`(searching && spinner) || searchCount !== null`) now goes true
  for a settled zero-hit search, where it did not before — the chip
  genuinely occupies a slot it did not occupy previously. The input's
  right-padding rules for every `has-pin`/`wide-pin`/`has-clear` combination
  already existed in `explorer.css` (D15's own matrix, four fixed-padding
  rules covering has-pin alone, has-pin+wide-pin, has-clear alone, and both
  together) — zero hits reaches an already-handled combination, not a new
  one. The clear button (`.listing-search-clear`) and the star
  (`BookmarkStar`) both sit at fixed `right: 8px` / `right: 38px` offsets
  independent of `has-pin`/`wide-pin`, so neither moves.
- The keyboard hint (D15, `.listing-search-shortcut-hint`) is gated on
  `!pinnedOpen && !hasClear`; `hasClear` is `query !== ""`, which is true
  whenever a search is running at all — the hint and a zero-hit chip can
  never both want the trailing slot, confirmed by reading the gate rather
  than assumed from "the hint is rest-only."
- The scan-caveat branch (`if (caveat) { … } else if (searchState.status ===
  "ok" && searchCount !== null) { … }`) is untouched text — the caveat still
  runs first and the latency figure is still its `else if`, so a stale count
  can never pick up a fresh elapsed time.
- `showingSearchHits` (`search-body-mode.ts`) is untouched: `searchState.status
  !== "idle" && !awaitingCommit`. The open-folder query case
  (`queryNamesOpenFolder`, D14) never reaches the chip at all through this
  change — an uncommitted query (decision 4's Enter gate) leaves
  `searchState` at `IDLE_SEARCH`, so `showingSearchHits` reads `false` and
  `searchCount` stays `null` regardless of `awaitingCommit`.
- A pending or errored search still renders no count: both the base
  assignment and the latency branch require `searchState.status === "ok"` on
  their own, independent of `showsSearchHits`.

No existing assertion was weakened or deleted. `search-clear-button.test.ts`
and `search-mode-chip.test.ts` (D15) needed no changes — neither touches the
count-chip's own hit-count guard.

Tests: `search-zero-count.test.ts` (new) — the codebase's established
pattern for `Listing.tsx`'s inline JSX conditionals (source-text assertions,
no render harness; see the "Two deviations from a strict TDD loop" note
above). Covers: the guard no longer carries `hits.length > 0` (this
assertion fails against the pre-change source — confirmed by stashing only
`Listing.tsx` via a tagged `git stash push -u -m
"brief29-pretest-check"` and running the new test file, then popping and
dropping that stash entry, `git stash list` empty afterward); the latency
branch stays the guard's own `else if`, downstream of the caveat branch;
zero hits still take the plural ternary rather than a bespoke zero case; and
`showingSearchHits` (called directly, a real pure function, not text
matching) reads `false` for `IDLE_SEARCH` and for a settled `"ok"` state
paired with `awaitingCommit: true`, covering the open-folder and
pending/uncommitted cases behaviorally rather than by pattern. Two new cases
also went into `result-cap.test.ts`, an actual behavioral check rather than
text matching: `resultCountLabel(0, false)` is `"0 matches"`, and
`capHits(hits(0))` has length 0.

Verification: `./frontend/node_modules/.bin/tsc --noEmit --project
frontend/tsconfig.json` clean. `bun test --run` — 3967 pass, 0 fail, up from
the 3960-pass baseline this round started from (7 new cases: 5 in
`search-zero-count.test.ts`, 2 in `result-cap.test.ts`).
`node frontend/scripts/check-boundaries.mjs` — `boundaries OK (630 files)`,
up from 629 by the one new file (`search-zero-count.test.ts`). `bun run
build` (from `frontend/`) succeeds; only the pre-existing manual-chunking
warnings, unrelated to this change.

## D18 — the merged field reaches the file view

A plain file's crumb bar shows the same merged field a folder's does,
resting on the file's own path crumbs with the file's own name as the last
crumb. Searching on a file page is the same as searching in the parent
folder with the query pre-seeded: committing a query navigates to the
parent's listing with the query applied, as a pushed history entry (Back
returns to the file). The parent's own `Listing.tsx` does the searching —
nothing on the file page ever issues a search request.

Field ownership: the merged field's box/dropdown/star JSX moved out of
`Listing.tsx` into a new shared component, `SearchField.tsx`. Both hosts
supply their own search state as props and render `<SearchField>` — a
folder's `Listing.tsx`, and a new `FileSearchField.tsx` mounted over a plain
file (`Preview.tsx`, gated the same way `usePreviewFileMenu`'s `ownsBar` is:
`actionsInTopbar && !stat.is_dir`). This was the only ownership shape that
satisfies "no second copy of the field's JSX": a file's box and a folder's
box are visually and behaviorally the same control, so they are the same
component, not two components kept in sync by hand.

`FileSearchField` reuses `useListingSearch` unmodified — the same hook a
folder calls — aimed at the file's parent path, with its third parameter
(`urlSync`) `false` so the file's own URL is never mirrored with a `q=` the
file page has no business owning. It computes `escapes`/`gateOpen` exactly
as a folder would, and the instant the box's own commit gate opens (Enter
for an escaping path, or immediately for a plain filter/glob that never
escapes the folder) a `useLayoutEffect` calls `navigate(parentPath, { isDir:
true, q: query })` and stops. `opts.q` both appends `q=` to the destination
URL and stashes `qCommitted: true` in `history.state`
(`navHintQCommitted()`), which is the existing mechanism (already built for
folder-to-folder navigation) that lets the parent's own `useListingSearch`
seed its `committedGate` already open — the parent never asks for a second
Enter. Firing this from `useLayoutEffect`, ahead of `useListingSearch`'s own
passive-effect-driven fetch, is what keeps a search request from ever going
out against the parent while the file's page is still on screen: the
navigation swaps the whole view before that fetch could ever be scheduled.
An auto-searchable query needs no Enter over a file either, for the same
reason — `gateOpen` is already true for a non-escaping query, so the effect
fires on the very next render after such a query is typed.

Three star cases, confirmed:

- **Folder bar** — unchanged. `Listing.tsx` still calls `claimFolderChrome`
  with a slot node, `BarSearchSlot` (renamed from `FolderSearchSlot`;
  Breadcrumb.tsx) still portals the row in, the star still renders inside
  the field's border via the same `barSearchSlot` gate.
- **File bar** — new. `FileSearchField` calls `claimFolderChrome(null)`: it
  claims the "claimed" boolean (the star moves inside the field, the crumb
  bar's own path-edit stands down) without a slot node, since Preview.tsx
  has no split-pane column for the bar to relocate into — that layout
  behavior stays folder-only. The star ends up inside the field's border
  over a file exactly as it does over a folder, through the same JS gate.
- **Panel-mode bars** — still outside the field, unaffected. Panel mode
  never calls `claimFolderChrome`, so `claimed` stays false there regardless
  of this change.

`claimed` (Breadcrumb.tsx) is now a "does something on this page have a
search row" question rather than a "is this a folder" question — it reads
`folderChromeClaimed()`, a plain boolean on the claim stack, indifferent to
whether the claimant is `Listing.tsx` (with a slot) or `FileSearchField.tsx`
(without one).

The file bar's auto-margin slack is carried by the same, unmodified CSS
rule that already carried it for a folder: `.crumb-search-slot >
.listing-search { flex: 1 1 auto; ... }` (`explorer.css`). It required no
change because it is scoped by class name, and `SearchField.tsx` kept that
exact outer class for both hosts — the file bar's field grows to fill the
row the same way a folder's does, automatically, with no file-specific rule
anywhere.

`inSearchSlot(slot, row)` — previously a private two-line helper inside
`Listing.tsx` — moved to `search-slot.ts` as a shared export, so both hosts
portal into the slot through the one function rather than each carrying its
own copy.

Three existing test files that read `Listing.tsx`'s source as text
(`search-clear-button.test.ts`, `search-mode-chip.test.ts`,
`search-examples-width.test.ts`) were retargeted to read `SearchField.tsx`
instead, since the markup and declarations they assert on physically moved
there. `search-mode-chip.test.ts`'s provenance test for `isOpenFolderQuery`
was rewritten: that value is no longer a `const` computed inline inside the
search box's own file, it is a prop (`SearchFieldProps.isOpenFolderQuery`)
computed once per host against that host's own base path — the test now
confirms the prop's declaration precedes the chip's own read of it in
`SearchField.tsx`, and separately confirms `Listing.tsx` still defines the
predicate via `queryNamesOpenFolder(query, fsPath, home)`. The same file's
platform-detection test was adjusted for `SearchField.tsx`'s actual import
line (`import { isMac } from "@platform/lib/platform";`, no `isMod` in that
file — `isMod` belongs to the keyboard-shortcut handler in Breadcrumb.tsx,
not the field itself). No assertion about behavior, only about which file a
piece of code now lives in, was weakened.

Verification: `bunx tsc --noEmit` clean. `bun test --run` — 3967 pass, 0
fail. `node scripts/check-boundaries.mjs` — `boundaries OK (632 files)`, up
by two (`FileSearchField.tsx`, `SearchField.tsx`). `bun run build` succeeds;
only the pre-existing manual-chunking warnings, unrelated to this change.

## D19 — a relative `..` query walks out of the box it was typed in

`resolve_query` (`fused_render/index/query.py`) had three escape shapes —
`~`, a leading `/`, a Windows drive letter — and one catch-all: anything
else, including a query with a `..` segment in it, fell to `base, pattern =
root, raw`. `_walk_from`, the walker every escape shape already used,
resolves a literal `..` segment for free: `os.path.isdir(base + "/" +
"..")` asks the filesystem, and the filesystem answers a `..` component the
same way `cd` would, so the walk climbs correctly the moment it is given
the chance to try. The catch-all branch never gave it that chance.

Fixed by adding one more branch ahead of the catch-all: a bare relative
query with a `..` segment anywhere in it (`any(seg == ".." for seg in
raw.split("/"))`) walks from `root` through `_walk_from`, exactly the
mechanism `~` and the drive-letter branches already call. The one thing
`_walk_from` does not do on its own is clean up the string it returns —
each consumed `..` segment is appended literally (`base` ends up something
like `/home/iamsdas/Downloads/..`), so the resolved base is passed through
`os.path.normpath` before it is returned. That normalization is also what
keeps a `..` run longer than the tree is deep from growing an ever-longer
trail of dot-segments: `/..` normalizes to `/` the same way `cd ..` at the
filesystem root stays at the root, so the walk cannot climb past it — there
is no separate clamp to write, `_walk_from`'s own directory-existence check
already stops it there and `normpath` keeps the string clean.

A `..` that resolves through a folder that does not exist behaves exactly
like every other escape shape's version of the same case: `_walk_from`
stops one segment early and folds the missing name into the pattern
(`../nope/x.csv` from `~/a/b` resolves to base `~/a`, pattern
`nope/x.csv`), the same "widen instead of fail" rule `~/nope/x.csv` already
followed. Nothing new needed writing for that case — it was already the
walker's behavior for every other branch, and routing `..` through the same
walker inherits it.

`~`, the leading-slash branch, and the drive-letter branch are untouched:
the new branch is an `elif` that only fires when none of the earlier three
matched, so a query that already escapes one of those ways keeps resolving
exactly as it did. The implicit `**/` prefix decision (`is_glob and "/" not
in raw`) runs after all branches, unchanged, and reads `raw`, not the
resolved pattern — a `..` query almost always contains a `/` (that's what
makes it a `..` *segment*), so it does not get the any-depth widening a
slash-free query does, same as every other multi-segment query.

`tests/test_index_query.py` gained four cases: a `..` that walks up one
level to a real sibling, a `..` pair that walks back to where it started
(round-trips through `_home/a/b` -> `_home/a` -> `_home` -> `_home/a` ->
`_home/a/b`), a `..` into a name that does not exist (widens, does not
fail), and a `..` run longer than the tree is deep against a monkeypatched
`/`/`/etc` filesystem (clamps at `/etc`, does not manufacture a path with
dot-segments still in it). All four fail against the unpatched code — the
`..`-into-a-sibling and round-trip cases return `pattern` still carrying
the raw `..` segments and `base` unmoved; the clamp case returns `base:
"/"`, `pattern: "../../../etc/*.conf"` instead of walking anywhere.

`enter-prompt.test.ts`'s `..` case (`folderToOpen("../x/*.json")` ->
`"../x"`) needed no change to its asserted string: `folderToOpen` is a pure
syntactic split of the query text, with no server round trip, and it
already named the folder correctly before this fix — the resolver bug was
that pressing Enter on that banner did not actually search there. Renamed
from "a relative .. prefix survives into the named folder" to "a relative
.. prefix names the folder the search will actually walk to": the old name
described `..` as inert text passing through untouched, which was true of
the search that ran (the bug) but not of what the banner said; the new name
says what is true now that `resolve_query` walks it for real.

## D20 — a request that fails with rows still on screen is not a healthy answer

`useListingSearch`'s never-blank rule keeps the last good rows on screen
while the next request is in flight, which is right for the ordinary case
(a debounce, a round trip, a fresh answer). It also kept them on screen,
untouched and undimmed, when the next request *failed* instead of
answering: `setFailure(err.message)` ran, but nothing downstream read
`failure` unless `displayHits` was empty, so a query that used to match and
now errors (`foo` -> hits -> extend to `foobar` -> that request rejects)
reported `status: "ok"` with the old rows, no caveat, no staleness class —
indistinguishable from `foobar` genuinely matching those same rows. The
zero-hits case already had this right (`status: "error"` when `failure !==
"" && displayHits.length === 0`); the gap was only the case with something
already on screen to hide behind.

Fixed with one new derived value, `requestFailed = failure !== "" &&
displayHits.length > 0`, folded into the existing `behind` boolean (so it
picks up the same `listing-behind` dimming class `Listing.tsx` already
applies to generation-stale rows) and also exposed on its own so the
caption can say something more specific than "not refreshed." `searchCaveat`
(`index-caveat.ts`) gained a fourth, optional input, `failed`, checked
after the scanning branch and before `behind`: "not refreshed… clear the
search and run it again" promises a plain re-run will catch up, which is
false immediately after one just failed, so a failed request gets its own
caption — "search failed" / "The last search request failed, so these
results still answer an earlier query. Edit the search or press Enter
again to retry." `failed` is optional because `FilesHome.tsx` calls
`searchCaveat` too and already reports its own search's failures through a
separate `ErrorBanner` row rather than this chip; omitting the argument
there keeps its caption exactly as it was.

The zero-hits+failure case is untouched: `requestFailed` requires
`displayHits.length > 0`, so a failed request with nothing already on
screen still falls through to the existing `status: "error"` branch,
unchanged.

A failed refetch now reads to the user as: the rows from the last query
that worked, dimmed the same way a generation-stale answer already dims,
with the status chip reading "N matches · search failed" instead of a bare
match count — not a silent "N matches" that happens to be lying about which
query it answers.

Four tests were added, all confirmed to fail against the unpatched code:
two on `indexCaveat` (the "search failed" caption fires when told a request
failed, and a running scan still outranks it), one on `searchCaveat` (the
same caption via the composed `state` shape, and that omitting `failed`
falls back to the generic "not refreshed" caption `FilesHome.tsx` still
relies on), and one hook-level test in
`useListingSearch.render.test.ts` that drives the actual scenario — types
`foo`, resolves a real hit, extends to `foobar`, rejects that request, and
asserts the old row is still the one on screen while `requestFailed` and
`behind` both flip true.

## D21 — the header says nothing about a base it does not have yet

`searchBase` (`useListingSearch.ts`), the value the header's "Path in …"
label and every row-path join key off, fell back to `fsPath` — the folder
already open — whenever `answer` was `null`. That is right while idle (no
search: `showsSearchHits` never renders the header at all, so the value is
inert), but it is also what ran for the box's very FIRST request, before
any answer has landed: a query like `~/Work/*/*.json` is written
specifically to walk out of `fsPath`, so naming `fsPath` as its base was
the header asserting a base this search does not have — visibly, "PATH IN
~/Downloads" printed over a search that, once it lands, answers for
`~/Work`.

Two shapes were on the table: carry a client-side guess of the resolved
base (the same syntactic split `enter-prompt.ts`'s banner already makes),
or say nothing until the real answer lands. The guess was rejected —
`enter-prompt.ts`'s own known limitation (a named folder may not exist) is
tolerable for a banner that reads "Press Enter to open X", a suggestion the
user can decline, but the header is not phrased as a suggestion; printing
a second, client-computed base next to the one the server will actually
report risks the exact failure this item exists to fix, just moved one
banner over. Saying nothing costs a moment of a bare "Path" heading (the
existing fallback for `searchBase === ""`, already exercised by the idle
case) and then the real base the instant it lands — one transition, from
blank to correct, never through a wrong intermediate value.

Fixed by narrowing the fallback: `fsPath` is still reported while nothing
is searching (unchanged — the box's resting value, and the one existing
test pinning it), but a search with no answer yet — `searching && answer
=== null` — now reports `""` instead. Every other caller of `searchBase`
(`navRows`, `rowCtxByPath`, the row-path joins in `Listing.tsx`) is already
guarded by `searching`, and `hits` is `[]` whenever `answer` is `null`, so
no row is ever built by joining onto the empty string — it only ever
reaches the header's own "nothing known yet" branch, which already renders
a bare "Path" for exactly this input. Once ANY answer lands — including a
stale one still answering an earlier query while a newer request is
out, a case this change does not touch — `answer.base` is real again and
gets named, same as before.

Two tests: one in `useListingSearch.render.test.ts` driving the box's
first-ever request for an escaping query (`~/other/rep` from `/proj`) and
asserting `searchBase` reads `""` while it is out, then `/home/u/other`
the instant the answer resolves — confirmed failing against the unpatched
code (it reported `/proj`, not `""`, while pending). The existing
`"is the box's own root when nothing is searching"` case needed no change
and still passes unmodified, pinning the untouched idle fallback.

## D22 — the clamp-at-root fixture described its fake filesystem in the host's
own dialect, not POSIX

`test_resolve_relative_dotdot_clamps_at_the_filesystem_root`
(`tests/test_index_query.py`) monkeypatches `os.path.isdir` with a lambda
that answers from a fixed set, `real_dirs = {"/", "/etc"}`, keyed by
`os.path.normpath(p) in real_dirs`. `os.path.normpath` is whichever of
`posixpath.normpath` or `ntpath.normpath` the host aliases `os.path` to; the
candidates `_walk_from` builds while consuming a `..` run past the root
(`/..`, `/../..`, `/../../../etc`, ...) are POSIX-style strings the fixture
itself wrote, so on a Windows CI runner `ntpath.normpath("/../../../etc")`
answers `"\etc"` — a backslash-separated string that matches nothing in
`real_dirs`, which only holds forward-slash keys. Every candidate reads as
"not a directory," `_walk_from` stops consuming at the very first segment,
and the resolver never climbs — the assertion then fails not on the
resolver's clamping behavior but on the fixture's own platform leak.

Fixed by keying the lookup on `posixpath.normpath` instead of
`os.path.normpath`: `posixpath` is a plain importable stdlib module on every
platform, not an alias like `os.path` — it always implements POSIX
semantics regardless of which OS is running the test, so the same lambda
now answers identically on Linux, macOS, and Windows. `fused_render.index.
ignore.norm` was the other candidate the brief named; it was rejected
because `norm` only flips its separator conversion when the real, current
platform is Windows (`ignore.WINDOWS`), so it inherits the exact same host
dependence `os.path.normpath` has here — it launders backslashes into
forward slashes on an actual Windows host, but does nothing on Linux, so it
would not have made the fixture's answer independent of where the test
runs. `posixpath.normpath` alone is unconditional and platform-blind by
construction, which is what a fixture describing a fake filesystem in POSIX
terms needs.

The production line the test exercises, `query.py`'s `base = norm(os.path.
normpath(walked_base)).rstrip("/") or "/"`, was checked rather than assumed
platform-correct: `ntpath.normpath("/../../../etc")` (imported directly,
standing in for what a real Windows host's `os.path.normpath` returns) gives
`"\etc"`, but that string is then passed through `norm()`, which — on an
actual Windows host, where `ignore.WINDOWS` is genuinely `True` — replaces
every backslash with a forward slash, turning it back into `"/etc"` before
it reaches the caller. Verified by forcing both `os.path.normpath =
ntpath.normpath` and `fused_render.index.ignore.WINDOWS = True` for the
duration of one call and running `resolve_query("/", "../../../etc/*.conf")`
unmodified against that simulated host: it still returned `{"base": "/etc",
"pattern": "*.conf", "mode": "glob"}`. The production line is correct
because `norm()` runs after `os.path.normpath` unconditionally, on every
branch that calls it, and is what actually launders the separator — the bug
was confined to the test fixture, which never routed its bookkeeping through
`norm()` at all.

Separately confirmed platform-independence of the fixed fixture itself,
without needing the WINDOWS-flag trick above (which only matters for the
production `norm()` call): compared `lambda p: ntpath.normpath(p) in
real_dirs` (standing in for the old, host-aliased fixture on a Windows host)
against `lambda p: posixpath.normpath(p) in real_dirs` (the fix) over the
four candidates the walk actually builds (`/..`, `/../..`, `/../../..`,
`/../../../etc`). The `ntpath`-keyed version answered `False` for all four —
reproducing the reported CI failure exactly — while the `posixpath`-keyed
version answered `True` for all four, matching what the real POSIX
filesystem the fixture describes should report on any host.
`./.venv/bin/python -m pytest tests/test_index_query.py
tests/test_index_search.py tests/test_index_freshness.py -q` stayed at 157
passed after the fixture change; `./frontend/node_modules/.bin/tsc --noEmit
--project frontend/tsconfig.json` stayed clean (this fix touches no
frontend file — checked for completeness, unaffected by construction).
