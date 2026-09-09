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
