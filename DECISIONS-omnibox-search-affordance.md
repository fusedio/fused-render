# Decisions - omnibox search affordance (E+F)

Worktree notes for this branch only. Do not touch the tracked `DECISIONS.md`.

## Scope item 1/2 - chip word removal, --chip-inset collapse

Straightforward per spec. New `--chip-inset` value: 8px (chip's own left
inset) + 12px (glyph) + 8px clearance = 28px, replacing the old two
text-derived numbers (55px "Path" / 67px "Search"). The box's own `.search`
modifier class (`chipIsSearch ? " search" : ""` on `.listing-search-box`)
was dropped entirely - nothing but the dead `--chip-inset` override read it,
confirmed by grep before removing. `.listing-search-mode.search` (the
chip's own accent-colour rule) is UNRELATED and stays - it is about colour,
not layout, and the spec's "distinguishable by more than colour" bullet is
satisfied by the glyph shapes regardless.

The visible word became a `.sr-only` span (Tailwind's utility, already used
elsewhere in the explorer, e.g. `McpDialog.tsx`) rather than a bespoke
visually-hidden class - no new CSS needed.

## Scope item 3 - the search button - REVISED TWICE, see spec item 3

First shipped as an icon-only magnifier button (per the spec text as
written). The user then compared it against the original "Search ⌘L" text on
a running screen and preferred the words - the icon-only version is NOT
current. See SPEC-omnibox-search-affordance.md item 3 for the final,
revised shape: words as the button's content, `bar-ctl` (not
`bar-ctl-icon`) at wide widths, collapsing to the previously-built icon at
narrow widths (`boxWide`, reusing `HINT_WIDE_PX` - no second breakpoint).
The icon markup itself was kept, not deleted, and simply demoted to the
narrow-width branch.

Accessible name: `aria-label` carries "Search this folder (⌘L)" in BOTH
forms; the visible `title` tooltip is the shorter "Search this folder" in
both forms too - the shortcut doesn't need repeating in the tooltip once
it's either visible as text (wide) or already in the aria-label (narrow).

CSS gotcha: the button's own vertical centring can't use
`transform: translateY(-50%)` - `.bar-ctl:active` (base.css) already claims
`transform` for a 1px press nudge, and the two would fight (whichever
declaration a browser resolves last wins the WHOLE property). Used
`margin-top: -14px` (half of the 28px `bar-ctl` height) instead.

Restored `.listing-search-shortcut-hint kbd` CSS (padding/border/background
matching the app's key-cap vocabulary) - this was deleted during the
icon-only revision and needed back once the words returned.

## Scope item 4 - the dropdown's action row

`search-action-rows.ts`'s `searchAffordance` is the pure predicate; `hasAction`/
`actionRowCount` in SearchField.tsx fold it into the SAME index space
`completionKeyAction`/`moveHighlight` already use for folder completions -
no changes needed to either of those two functions, since they were already
generic over a row count and an index into it.

The not-found row reuses `pathNotFoundMessage()` verbatim (per spec
instruction "reuse it") rather than the illustrative "No folder at
&lt;path&gt;" wording the spec's prose used - the prose was a gloss, not a
literal string requirement, confirmed by the spec's own "the message text
already exists... reuse it."

**Deliberate scope narrowing on the not-found row's "search for it instead"
action**: rewriting the box to the query's own basename (`commitInPlace:
false`) rather than forcing the FULL escaping text to search as-is. The
alternative - bypass `isPathQuery`'s suppression and force a raw-text search
- would require changing `useListingSearch.ts`'s core gating (the fetch
effect's `if (isPathQuery) return` line), which is exactly the blast radius
the spec's Hard Constraint section warns against re-entering. Rewriting to a
plain word achieves the same user-visible outcome (something gets searched)
through the field's EXISTING, already-safe query-rewrite path, with zero
changes to the search-gating hook itself.

## The corrected model - defects 1 and 2 (2026-09-10 running-screen review)

The user typed `/Users/iamsdas/*/*.json` while standing in `/Users/iamsdas`
and got both a gated wait AND an offer row sitting over an unchanged plain
listing - "defeats the purpose of getting search results as you type when
you are in the same folder." Two separate, decidable-synchronously defects:

**Defect 2 (root cause)**: `escapesBase` (query-base.ts) treats EVERY
leading "/" as escaping the box root, even one that resolves right back
inside the folder already open. The box always arrives pre-filled with that
folder's own absolute path (hard requirement), so appending a pattern to it
is the single most natural gesture the box offers, and it landed on the
slower, gated route for no reason.

Fix: `escapesFsPath(query, fsPath, home)`, a NEW, narrower, fsPath-aware
predicate in query-base.ts, used ONLY by the commit gate
(`useListingSearch.ts`'s `escapes`) and the search offer's `gated` input.
`escapesBase` itself is UNTOUCHED - `isPathShapedQuery` (path-shaped-
query.ts) is built directly on it, and changing its meaning would flip
which queries read as "Path" at all, regressing the whole PR #1091 branch
this one is stacked after. `escapesFsPath` compares the query's own BASE
(the segments before its first glob-bearing one, or the whole address if
there is no glob) against `fsPath` by SEGMENT, not string prefix -
`/Users/iamsdas2` is correctly NOT inside `/Users/iamsdas`. A `..` segment
is always treated as escaping (matches `escapesBase`'s own existing
verdict) rather than attempting to resolve whether it re-descends into the
same subtree - not a case the correction asked about, and the "escaping"
answer is safe either way.

**Defect 1**: `searchAffordance` offered the commit-row for ANY non-path
query while `searching`, including one already answering LIVE (ungated) -
reading as "nothing has happened yet" over results already on screen. Fixed
by adding a `gated: boolean` parameter (the SAME `escapesFsPath`-derived
value the caller's own commit gate reads) - the offer only appears when
`gated` is true, or for the (unaffected, unconditional) missing-path branch.

**A consequence worth naming explicitly**: after defect 2's fix, a GATED
non-path query is ALWAYS a glob (the only way `escapesBase` is true while
`isPathQuery` is false), and `completionTarget` (completion-target.ts)
refuses ANY query containing "*" outright. So a gated query never has
folder completions to sit alongside any more - the action row and real
folder completions coexist ONLY in the missing-path case (a folder that
exists but whose exact typed name doesn't). The arrow-key regression test
had to be rebuilt around that scenario instead of a relative slash-bearing
word, once the original scenario stopped producing an action row at all
(correctly, per defect 1's fix).

**A regression `escapesFsPath` exposed in `FileSearchField.tsx`**, caught by
the driven render test, not inferred: that file's own "hand off to the
parent folder" layout effect fires on `searching && gateOpen`, meant for
once the user has actually committed something. Under the OLD `escapesBase`
gate, the box's own pre-filled seed value ALWAYS "escaped" (leading "/"),
so `gateOpen` was always false for it until a real edit or commit -
accidentally protecting this effect from firing prematurely. Once
`escapesFsPath` correctly says a same-subtree pre-fill does NOT escape,
`gateOpen` becomes true the moment the field is simply FOCUSED, before any
typing - the effect would auto-navigate on the untouched seed. Fixed with an
explicit `isPristineQuery` guard, checked against `q` (the SAME deferred
value `gateOpen`/`escapes` are computed from), not the live `query` - a
one-render window exists right after a keystroke where `q` still lags the
fresh value, and checking the live `query` there let that exact stale-`q`
render slip through.

## Correction 2 - the pristine query and the teaching panel (2026-09-10)

The examples/guidance panel (`SEARCH_EXAMPLES`/`HINT_LONG`, gated by
`showSearchExamples`) was built for "the field is focused and empty," which
made sense before the box always arrived pre-filled - after that became a
hard requirement, an empty, focused field essentially never happens, so the
panel never rendered.

**Pristine, defined** (`query-pristine.ts`'s `isPristineQuery`): the query is
empty, OR it is still EXACTLY the folder the box pre-filled itself with -
`contractHome(fsPath, home)`, the same call `SearchField.tsx`'s own
`onFocus` makes. Checked in EITHER notation (`~`-contracted or absolute)
since `contractHome` only contracts a STRICT descendant of home (home
itself renders its full path, never a bare "~" - `contractHome`'s own
rule), and tolerant of ONE trailing slash on either side. Deliberately a
full-string comparison after stripping that slash, not a prefix test -
"/Users/iamsdas2" typed into a box opened at "/Users/iamsdas" is an EDIT,
not the same folder wearing an extra character.

**Precedence, decided and not to be re-litigated**: pristine WINS the
dropdown surface over completions, the opposite of the old empty-query
precedence. A pristine, pre-filled path resolves to a real folder, so
`showCompletion` could legitimately be true for it (the completion
machinery happily lists that folder's own children) - but those children
are already listed in the rows directly below, so duplicating that listing
while teaching nothing is the worse trade. Enforced by construction:
`SearchField.tsx`'s own `showCompletion` computation excludes `pristine`
BEFORE `showSearchExamples` is ever asked - `showSearchExamples` itself is
a plain `fieldActive && pristine`, not a re-derivation of the exclusion the
other way. `searchAffordance` also short-circuits on `pristine` FIRST,
before even checking `searching`/`isPathQuery` - a pristine absolute path
is both path-shaped and (post defect-2 fix) no longer gated, so without
checking pristine first the missing-path branch could theoretically fire
for a folder that very much exists.

## Correction 3 - see spec item 3 above; noted here for completeness

Words stay. Both revisions (icon-only, then words-with-narrow-icon-fallback)
are documented in git history as separate commits rather than squashed, per
the "commit per logical unit" instruction - the icon-only commit is not
wrong to have existed, it's superseded.

## Correction 4 - teaching examples must come from the folder's own entries

`SEARCH_EXAMPLES` was a literal (`*.csv`, `~/work/*/*.csv`) that returns
zero rows anywhere but the author's own machine, and points at a folder
(`~/work`) that exists there only by luck. Replaced with
`buildSearchExamples(entries, fsPath, home)`:

- Most common extension among the folder's own currently-listed entries
  (directories and hidden entries excluded from the count; a name with no
  "." owns no extension), ties broken alphabetically for determinism across
  renders of the same unchanged folder.
- Slots 1 (`*.<ext>`) and 2 (`.<ext>`) always share that ONE extension - the
  pair's entire teaching value is the one-character difference between
  them.
- Slot 3 (`~/*/*.<ext>`) reuses the same extension and points at `~`
  (always real, unambiguously elsewhere, needs no probing) - omitted
  entirely while `fsPath` IS home, where "searches from ~ instead of here"
  would be a false claim.
- No extensioned files at all -> `[]`, no invented example, and the whole
  panel doesn't render (`showExamples` also checks `searchExamples.length >
  0`) - since pristine already wins the surface over completions
  unconditionally, an empty panel would otherwise render nothing at all
  rather than falling back to completions; that fall-through was
  deliberately NOT added, matching "show fewer examples, never invented
  ones" literally (fewer can mean zero).
- `entries` threaded as a new required `SearchField` prop: Listing.tsx
  passes its own `sortedEntries` (already loaded - no second fetch);
  FileSearchField.tsx passes `[]` (a file view keeps no listing of its
  parent folder's own entries), which correctly yields no examples there.
- `HINT_LONG` dropped its own copy of the same hardcoded example
  ("...like ~/work/*/*.csv") rather than deriving a second copy - one fact,
  one place, and the panel below is that place now.

## Known infra fragility encountered, not caused, not fixed here

`bun test` run as a FULL DIRECTORY intermittently fails
`search-dropdown-actions.render.test.tsx`'s hard-constraint test (and
unrelated pre-existing files, e.g. `empty-result.test.tsx`) with either a
wrong `pushStateCalls` count or a `SyntaxError: Export named '...' not
found in module '.../router.ts'`. Confirmed NOT caused by this branch's
code: the same test passes cleanly every time run in isolation
(`bun test src/apps/explorer/listing/search-dropdown-actions.render.test.tsx`),
and the router-export failure mode was already reproducible on
`empty-result.test.tsx` before this branch's own render test even existed.
This is bun's `mock.module` being process-wide (documented project-wide
issue) - some other test file's incomplete `mock.module("@platform/lib/
router", ...)` stub replaces the real module for the rest of the process,
and whichever later file first needs a real export that stub omitted fails
with exactly this error. Not something a single feature branch should try
to fix; flagging for the orchestrator's full-suite run rather than chasing
it here.

## Cannot be verified headlessly

See SPEC-omnibox-search-affordance.md's own "Cannot be verified headlessly"
section (kept up to date there, not duplicated here) - the two-magnifiers
risk it originally flagged is resolved by item 3's revision, but the
button's own narrow-width collapse point, the teaching panel's real-world
examples, and its precedence over completions on a pristine path all still
need a human on a running screen.
