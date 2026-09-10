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
