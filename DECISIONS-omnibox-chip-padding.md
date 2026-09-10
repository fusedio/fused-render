# DECISIONS — omnibox chip padding + Path/Search rule

## Task 1: per-mode chip padding

Straightforward: two states of one CSS custom property, `--chip-inset`, set
on `.listing-search-box` (the container both `.listing-search-crumbs` and
`.listing-search-input` are descendants of) instead of on the chip alone.
`.listing-search-box` gets the base "Path" value (55px); `.listing-search-
box.search` overrides it to the "Search" value (67px, unchanged from before).
The mode class had to move from the chip-only to the box too, since the box
is the nearest common ancestor of both consumers.

**Measuring "Path"'s width.** The existing 67px comment says "Search" was
measured at 35px, at this rule's own 11px/500-weight system font, but doesn't
say how (no browser is available in this sandbox to reproduce it directly).
I measured both words with AppKit's `NSFont.systemFont(ofSize: 11, weight:
.medium)` (a Swift script via `NSAttributedString.size`) and got Search ≈
37.3px, Path ≈ 24.3px — close to but not identical to the file's own 35px,
which is expected (a different renderer/OS-version pairing than whatever
produced the original number). Rather than trust my absolute AppKit numbers,
I scaled: `Path ≈ 35 × (24.3 / 37.3) ≈ 22.7`, rounded up to 23px, giving
`8 + 12 + 4 + 23 + 8 = 55px`. This keeps "Path"'s number consistent with the
file's own already-committed "Search" baseline rather than introducing a
second, independently-measured constant that could disagree with it for
reasons that have nothing to do with the words themselves.

Also updated `.listing-completion-row`'s left padding (the suggestion
dropdown) from a hardcoded 67px to the same `var(--chip-inset)` — it's a
descendant of `.listing-search-box` too, and in practice only ever renders
while the chip reads "Path" (the dropdown's own shape gate, `completion-
target.ts`, matches `isPathQuery`'s), so it now automatically tracks the
narrower value instead of carrying a third copy of a number that used to be
shared by exactly two rules.

## Task 2: what "Path" means — SCOPE CHANGED MID-BUILD

**Original brief:** verify the query resolves to a real, existing directory
via the app's async stat path (`useTypedPathAddress`/`statPath`), debounced
and cached per resolved path, with "Search" as the safe default while
verification is pending, to avoid flickering the chip or hammering an
NFS-mounted folder with stats on every keystroke.

**What actually happened:** before I wrote any Task 2 code, the coordinator
cancelled this whole approach after the user reviewed a screenshot: typing
`~/Work/agent-skills/u` showed "Search" with "55 matches · 88 ms", while the
suggestions dropdown *underneath* already listed `ui`/`utilities` and Enter
already accepts one. The user's own words: "any search on an absolute path
without a pattern is useless." So existence was never the right test — a
non-existent PREFIX of a real path is exactly as much "Path" as the real
folder itself, because the useful behavior (the dropdown) is identical
either way, and doesn't need the search to be running behind it at all.

**Final rule, shape-only:** `isPathShapedQuery` (`listing/path-shaped-
query.ts`) is a one-line wrapper around the SAME `listingAddress` decision-5
shape gate `useTypedPathAddress`/`completionTarget` already use — path-shaped
if it has `/`, is `~`/`~/…`, or a `C:\`-style drive prefix, AND has no glob
(`listingAddress` already returns `null` for any query containing `*`, which
is also the one and only glob trigger the server's own `resolve_query`
recognizes — `is_glob = "*" in raw`, `fused_render/index/query.py`). No
existence check, no `statPath`, no debounce, no cache, no async state at all
— it answers synchronously off the same tick the query changes, which is
also why it can drive the chip immediately with zero flicker risk (there's
no pending state to flicker through). This eliminates the original brief's
whole NFS-mount-wedge risk by construction rather than by debouncing around
it.

**Second half — the request itself is now suppressed, not just relabeled.**
`isPathQuery` (surfaced from `useListingSearch.ts`, computed once there) now
also gates whether `useListingSearch`'s rank-request effect ever fires:
`runsSearch = searching && !isPathQuery`, and the whole fetch effect's early
return that used to key on `!searching` now keys on `!runsSearch`. The
completion dropdown (`useCompletion.ts` → `listDir`) is a fully independent
hook with its own fetch path — confirmed by reading it before touching
anything — so suppressing the rank request never touches suggestions at all;
they keep working exactly as before, including Enter accepting a row.

**Ripple effect this exposed (not asked for, but load-bearing).** Several
places in `Listing.tsx` branched on raw `searching` on the assumption that
"searching → eventually gets a real answer" — true before this change (every
committed query, however oddly shaped, eventually got a `RankAnswer` from
the server), false now (a path-shaped query commits and NEVER gets one).
Left alone, this would have made `navRows` (arrow-key nav) and `rowCtxByPath`
permanently point at an empty `visibleHits` while the folder's own rows sat
on screen, and would have made `listingLoaded` lie, and would have let the
search auto-select effect run `nextSearchSelection` over an empty result set
every time. All four were switched from `searching` to `showsSearchHits`
(`showingSearchHits(searchState, awaitingCommit)`, already the file's own
declared "one place this choice is made") — hoisting nothing, just pointing
each at the value that was already the correct discriminator for "are hits
actually on screen." The enter-banner and the status-footer's `searching &&
!isOpenFolderQuery` were widened to `!isPathQuery` for the same reason:
`isOpenFolderQuery` only ever excluded the ONE query that named the folder
already open; every other path-shaped, uncommitted query still got the
"Enter to search" banner promise before, and now that promise would be false
for all of them (Enter either navigates directly, if the address is real, or
does nothing at all, since no search is coming) — that's a real widening
of behavior, not a cosmetic rename.

**`queryNamesOpenFolder` / `query-current-folder.ts` deleted.** Its ONE job
(is this query exactly the folder already open) is now a narrower case the
broader `isPathQuery` subsumes everywhere it was used — chip, enter banner,
footer, and now the search-suppression gate too. Confirmed dead via grep
before deleting (frontend + `tests/` for any pytest assertion on the
symbol name or its file — none), and deleted its test file alongside it.

**Existing tests fixed, not just grepped.** `useListingSearch.render.test.ts`
calls the hook directly with `home` always `undefined` (the harness never
seeds one) — every `"~/…"` query in that file already resolves to
`isPathQuery === false` regardless of this change (`listingAddress` returns
`null` for a leading `~` when `home` is `undefined`, independent of whether
a glob is present), so none of the file's existing assertions needed content
changes — only the hook's new `(fsPath, home, refresh, urlSync)` signature
needed threading `undefined` through every call site. I nearly "fixed" three
of those tests by adding a glob to their queries before realizing this and
reverting that plan — recorded here as the dead end it was, in case a future
change makes `home` non-undefined in that harness and actually breaks them.

**New tests added** (per the coordinator's explicit list): `path-shaped-
query.test.ts` covers partial-path→Path, existing-path→Path, glob→Search,
bare-word→Search, relative-with-slash→Path, and the `home`-undefined edge
cases `listingAddress` already carried. `useListingSearch.render.test.ts`
gained a new describe block asserting an absolute path-shaped query (with
and without Enter/`commitSearch`) never issues a rank request and settles at
`searchState.status === "idle"`, while the same query WITH a glob still
does. `search-mode-chip.test.ts` was rewritten to assert `isPathQuery` (not
`queryNamesOpenFolder`) drives the chip, that both hosts read it off their
own `useListingSearch()` destructure rather than computing it a second way,
and that the predicate lives in exactly one place (the hook).

## Things that turned out wrong along the way

- Assumed I'd need to hoist `showsSearchHits`'s declaration earlier in
  `Listing.tsx` to reuse it at `navRows` (which renders before the original
  declaration site). Did exactly that — moved the single declaration up
  next to the other search-hook results, left one comment behind at the old
  site pointing at it, rather than leaving two separate calls to
  `showingSearchHits`. No test depended on the old line position.
- Nearly left `searchBase`/`rowsAnswerQuery`/`unsettled` keyed on raw
  `searching`, reasoning "they self-resolve correctly anyway since `answer`
  stays null" — true today, but it makes correctness depend on `answer`
  always being null rather than on the actual intent ("is a search really
  running"), which is exactly the kind of implicit invariant this bug class
  came from in the first place. Switched all three to `runsSearch` instead.
- **Overwrote the repo's real `DECISIONS.md`** (a pre-existing, tracked
  1598-line project decision log) with this file's content on the first
  pass, because the build brief said "a `DECISIONS.md` in the worktree"
  and I didn't check whether one already existed there. Caught it in
  self-review before reporting done, restored the original content from
  `HEAD~1`, and moved these notes to this feature-scoped filename instead
  — matching the repo's own established convention referenced elsewhere in
  comments (`DECISIONS-one-field-search.md`). Fixed via a follow-up commit,
  not an amend, since the bad commit's other changes were already correct.

## Code review round: six findings, fixed in order

### Finding 1 (HIGH) — Enter could open an arbitrary folder row

`rowsAnswerQuery` (`useListingSearch.ts`) was `!runsSearch || (!staleRows &&
!deferredStale)`. `runsSearch` is false for EVERY path-shaped query (no rank
request is ever issued for one), so `rowsAnswerQuery` read `true`
unconditionally whenever the box held a path-shaped query — the same flag
the document-level Enter handler (`useListingSelection.ts`) reads to decide
whether opening `rows[0]` with nothing selected is a safe guess. `navRows`
(`Listing.tsx`) falls back to the FOLDER's own rows for a path-shaped query
(`showsSearchHits` is false for it), so "safe to open row 0" was true over
rows that had nothing to do with what was typed — Enter could open an
arbitrary, unrelated folder entry.

Fixed by keying the trivial-true shortcut on `!searching` instead of
`!runsSearch`: an empty box has no query to fail to answer (browsing is
unaffected, matching pre-existing behavior), but a path-shaped query IS a
query (`searching` true, `runsSearch` false) and its rows never answer it.
`runsSearch` still governs the staleness check for an actual, running
search. New tests in `useListingSearch.render.test.ts` cover both the
`false` case (absolute path-shaped query) and the untouched `true` case
(empty box).

Side effect: fixing this exposed that bun's `mock.module("@platform/lib/
router", …)` is process-wide, and `useListingSelection.render.test.ts`'s
own mock — missing `navHintQCommitted` — could win the module-registration
race against `useListingSearch.render.test.ts`'s mock (which does provide
it) when both run in the same `bun test` invocation, throwing a
`SyntaxError` and failing whichever file loaded second. Fixed by adding the
same export to both files' mocks rather than relying on load order.

### Finding 2 (MEDIUM, scope overreach) — "Path" narrowed to absolute-ish shape

`isPathShapedQuery` wrapped `listingAddress` directly, which resolves ANY
glob-free query containing a `/` — relative ones (`src/util`,
`listing/useListing`) included. That silently killed subtree search for
those queries: the completion dropdown that's supposed to already answer
them (`useCompletion.ts`'s `listDir`) is prefix-only and non-recursive
within one directory, so `src/2024` would find nothing that
`report-2024.md` anywhere under `src/` would have matched via a live rank
search.

The user's own rule was scoped to ABSOLUTE paths only ("any search on
absolute path without pattern is useless") — a relative slash-bearing query
was never in scope. Narrowed `isPathShapedQuery` to additionally require
`escapesBase(query)` (`query-base.ts`'s existing leading-`~`/leading-`/`/
drive-prefix/`..`-segment shape test) on top of `listingAddress`'s
null/non-null split. Reused `escapesBase` rather than writing a fourth
shape test deliberately: it is already the exact gate decision 4 uses for
"requires Enter before searching," and every path-shaped query already had
to satisfy it anyway (a query that doesn't escape the box root live-filters
and never needs Enter) — reusing it means "reads as Path" and "requires
commit" can never independently drift apart the way two hand-written shape
tests eventually would. `escapesBase`'s one broader case than the three
findings 2 named (a leading `..` segment with no `~`/`/`) was left in
rather than carved out: it's the same kind of case (base differs from the
box root, shape alone can't confirm a real path, the dropdown's reasoning
for suppressing the search applies identically), and finding 2 didn't ask
for it to be excluded.

`listingAddress` itself is untouched — it stays the resolver
`useTypedPathAddress` and `completionTarget` use, both of which need an
answer for relative queries too (what to stat, what to list).

Tests added: `src/util`/`sub/dir` → Search (relative, no longer path-shaped;
`useListingSearch.render.test.ts` additionally confirms `src/util` still
issues a rank request), `/Users/x/y` → Path, `~/Work/a` → Path, `~` → Path,
`~/Work/*` → Search (glob, unchanged). The `C:\x` → Path case already
existed from the earlier round and needed no change.

### Finding 3 (MEDIUM) — a committed, non-existent path query was a silent dead end

Type `/nope/here`, press Enter: `commitSearch()` runs, the gate opens
(`gateOpen`), but `runsSearch` is false so nothing is ever asked, the
Enter-banner excluded every `isPathQuery` unconditionally (see finding 1's
era of this file, before this round), and the footer fell back to the
folder's own item count (finding 4). Nothing on screen said the query had
been refused.

Added `pathQueryRefused = isPathQuery && gateOpen && typedAddress.status
=== "missing"` in `Listing.tsx` and widened the banner gate to `searching
&& !showsSearchHits && (!isPathQuery || pathQueryRefused)`. The message is
a new `pathNotFoundMessage(query)` (`enter-prompt.ts`), not `enterPrompt`
reused — `enterPrompt`'s whole vocabulary is "Press Enter to open/search",
an instruction for something still to happen, and here Enter has ALREADY
run and been refused; reusing it would print a promise for a second Enter
that does nothing. Named the same way `enterPrompt`'s own `"exists"` branch
names a resolved address (trailing-slash-stripped last path segment) for
the symmetry: one message says what Enter opened, the other says what it
could not find. This only fires once `gateOpen` (Enter has been pressed
for this exact text) — a path-shaped query still mid-typing, not yet
committed, says nothing extra, matching how the dropdown already carries
the "here's what's real so far" job during typing.

`search-enter-banner.test.ts`'s literal-source-text assertion of the gate
condition needed updating to the new text — it greps `Listing.tsx`'s
source rather than rendering, by design (see its own header comment), so
it's exact-string-coupled to this line.

### Finding 4 (LOW/MEDIUM) — footer read "Empty folder" before the folder loaded

The footer's render gate was `state.status === "ok" || searching`.
`showsSearchFooter` (`searching && !isPathQuery`, computed a few lines
above this exact gate, with a comment explaining exactly why raw
`searching` is wrong here) is false for a path-shaped query, so `statusText`
falls to the non-searching branch — `total: sortedEntries.length` — which
is `0` while the folder is still loading (or has errored) rather than
"nothing has been read yet." Reachable on a reload of a URL whose `?q=`
holds a path query while the listing loads, and permanently on an errored
folder with a path query in the box: exactly the failure mode the comment
directly above this gate already exists to prevent, just for a case that
predates `isPathQuery`.

Verified the review's suggested fix before applying it: `statusLine`'s own
`searching` input is ALREADY passed `showsSearchFooter`, not raw
`searching`, at the call site immediately above this render gate — so
`state.status === "ok" || showsSearchFooter` makes the render gate agree
with the exact value `statusLine` itself already keys off, rather than
introducing a second definition of the same idea. For a real (non-path)
search this changes nothing: `showsSearchFooter` equals `searching` in
that case.

No render-level regression test added for findings 3/4 — there is no
existing full-`Listing.tsx`-render test harness in this codebase
(`Listing.test.tsx` only tests extracted pure helpers, not the mounted
component), and building one from scratch is out of proportion to a
two-line conditional fix. Coverage here is the pure-function test
(`pathNotFoundMessage`, `enter-prompt.test.ts`) plus the existing
source-text gate assertion (`search-enter-banner.test.ts`), plus
`tsc --noEmit` confirming the new `gateOpen` destructure and control flow
type-check cleanly.

### Finding 5 (LOW cause, but voided this PR's own coverage) — router mock missing an export

`useListingSearch.render.test.ts`'s `mock.module("@platform/lib/router", …)`
omitted `navHintQCommitted`, which `useListingSearch.ts` imports —
`SyntaxError: Export named 'navHintQCommitted' not found`, failing the
WHOLE FILE to load (0 pass / 1 fail), which silently voided the "a
path-shaped query never asks the index" describe block — this PR's own
central coverage claim. Fixed by adding the export to the mock (returning
`false`, since no test in this file exercises the seeded-already-committed
path). Confirmed by running the file alone before and after: 0/1 → 34/0,
then re-confirmed after every later change in this round kept it green.

The SAME class of gap exists elsewhere in this directory and predates this
whole feature: `empty-result.test.tsx` needs `navigateUrl` from the real
`router.ts`, and when the directory's full `bun test` run loads a file
whose mock lacks it (either of the two files above, once fixed to include
`navHintQCommitted` but not `navigateUrl`), the process-wide mock clobbers
the real module for every file loaded after it in the same process,
throwing the same kind of `SyntaxError` for `empty-result.test.tsx`.
Confirmed via a tagged `git stash push -u`/`apply`/`drop` cycle that this
exact failure (1 fail, 1 unhandled error, same `navigateUrl` message) is
present at this branch's OWN baseline — i.e. it predates every fix in this
round and is not something finding 5 asked to be fixed (finding 5 named
only `useListingSearch.render.test.ts`'s own whole-file failure). Left
alone, since only two files in the whole app mock `@platform/lib/router`
at all (grepped) and neither one's job is to be the one true mock of that
module — a real fix would be a shared router-mock factory both files
import, which is a bigger refactor than this finding asked for.

### Finding 6 (LOW) — re-evaluated after finding 2, concluded no code change needed

`FileSearchField.tsx`'s forwarding effect (`if (!active || firedRef.current)
return; if (!searching || !gateOpen) return; … navigate(parentPath, {
isDir: true, q: query })`) has no `isPathQuery` check, so it always hands
the query to the parent folder's `Listing` once `gateOpen`. The finding's
repro was a RELATIVE path-shaped query (`sub/x`) — before finding 2, that
was `isPathQuery === true`, so the parent Listing would ALSO treat it as
Path (same predicate everywhere), suppress its own rank request, and the
user would land on the parent folder with a query that visibly does
nothing.

Finding 2 removes the premise: `sub/x` is no longer path-shaped anywhere in
the app (relative slash-bearing queries are plain Search again), so the
parent Listing the effect navigates to picks it up as an ordinary query and
live-filters it via a rank request — exactly the behavior "hand the query
to the parent and let its Listing take it from there" (the effect's own
comment) was always supposed to produce. For a genuinely absolute
path-shaped query (`/other/folder`), the forwarding behavior is unchanged
from before this whole round: `escapes` is true, so the effect still waits
for `gateOpen` (Enter), and once it fires, the parent Listing gets the same
`isPathQuery` treatment the file view's own box already gave it — including,
now, finding 1's fix (Enter on the parent's landing render can't open an
arbitrary row) and finding 3's fix (a missing address gets an honest
banner there too, not silence). No gap specific to `FileSearchField.tsx`
remains once findings 1–3 are in.

No test added here: exercising the forwarding effect end-to-end would need
driving real keystrokes through the rendered `SearchField` input and
spying on `history.pushState`/`navigate` inside
`FileSearchField.render.test.tsx`'s harness (which currently only asserts
resting-crumb rendering and deliberately avoids mocking `@platform/lib/
router` at all, per its own header comment on why `mock.module` is
avoided here) — a bigger harness investment than a "re-evaluate and say
what you concluded" finding calls for. The reasoning above is traced
through the actual source, not observed running.

## Full-suite verification

`bun test` (whole frontend suite) and `bunx tsc --noEmit --project
frontend/tsconfig.json` were run after every finding and once more at the
end — see the build report for the final counts. The one known-red file
(`empty-result.test.tsx`, finding 5's sibling gap) is pre-existing at this
branch's baseline, confirmed via the stash cycle described under finding 5
above, and unrelated to any of the six findings' fixes.

## Task 3: "All files" — a navigation shortcut in the home search bar

The user asked for a chip/button in the HOME screen's search bar (`FilesHome.
tsx`'s `FilesSearch()`, not `SearchField.tsx` — that file is mid-review on
this same branch and untouched here) that "opens file explorer at home
directory like the browse files button in the recent files screen." Explicit
in the brief: this is navigation, not a search modifier — it must not read or
write `query`/`ai`/anything else `FilesSearch` tracks.

**Reused, verbatim.** The recents screen already has exactly this button:
`.files-hero-cta` ("Browse files"), rendered beside the tab strip at
`FilesHome.tsx` (`onClick={() => navigate(home, { isDir: true })}`, `navigate`
imported module-level from `@platform/lib/router`). Its handler is the literal
one-liner the new control calls — same `home` prop `FilesSearch` already
receives, same `navigate(..., { isDir: true })` shape — so there is exactly
one way this app opens "the home directory as a folder," not two that could
drift apart.

**What was added.** A second button, inside `.files-search` itself (the
icon/input/Clear row), styled with the SAME `.files-hero-cta` class (reused,
not reinvented — it's genuinely the same action, offered a second time) plus
one position-only modifier, `.files-search-allfiles` (`preferences.css`,
`flex: none; margin: 0` — cancels the pill's own bottom margin, which exists
only to clear the tab strip it doesn't sit in here), following the same
"position only, chassis comes from the existing class" pattern the file's own
`.fh-index-cta-btn` already established. `aria-label="All files"` (the
button's rendered text is also "All files") plus a `title` describing the
destination; same directional arrow glyph as the recents-screen button, for
visual continuity between the two entry points.

**Tests** (`FilesHome.render.test.tsx`, TDD — written failing first, against
the not-yet-existing `aria-label="All files"` node): (1) clicking it pushes
exactly one URL, `"/explorer/view" + home`, matching what the mount's fake
`history.pushState` records for `navigate(home, { isDir: true })`; (2) typing
a query first, then clicking it, leaves the input's value untouched and
issues no extra rank request beyond the one the typed query itself caused —
covering "does not alter the search query/scope state" directly rather than
by inference.

**Needs human eyes** (headless render tests cannot see layout — see the
build report): where the button sits inside the bar relative to the
magnifier/input/Clear, in both light and dark theme, and in a narrow window
(does it wrap or get truncated against the input).
