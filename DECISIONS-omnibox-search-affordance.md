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

## Code review fixes (2026-09-10)

Seven findings came back; 1, 2, 3 and 7 were must-fix. All seven addressed.

**Finding 1 (HIGH) — Tab ran the action row instead of completing a typed
path.** `completionKeyAction`'s Tab default (nothing arrowed to) was always
index 0, and once an action row occupied that slot, Tab ran it instead of
completing the first real folder match — destroying a typed path on every
keystroke, since `typedAddress` is "missing" for every partial path while
it's being typed. Fixed by giving `completionKeyAction` a `tabDefaultIndex`
parameter (default 0 — every existing call site and test unaffected);
`SearchField.tsx` passes the index of the first real completion whenever
one exists (`actionRowCount`), falling back to 0 (the action row) only when
it's the sole row on offer. The action row stays reachable by explicitly
arrowing to it, unchanged. Pure-function coverage in
`completion-keys.test.ts` (two new tests: the redirected default, and that
an EXPLICIT highlight on row 0 still works). A driven, full-stack
reproduction of the original bug is no longer possible after finding 3
landed in the same round (see finding 3's own note below) — kept the fix
anyway, per the finding's explicit "must-fix", as defensive correctness.

**Finding 2 (MEDIUM) — the offer row named the wrong folder.** The
`commitInPlace: true` row is only reached when `gated` is true, i.e. the
query's base genuinely differs from the folder on screen — yet its label
said "Search this folder for …", which searches a DIFFERENT folder than the
one open. Fixed by reusing `folderToOpen` (now exported from
`enter-prompt.ts`) to name the real folder, matching the wording the
retired banner already used verbatim (`Press Enter to open <folder> and
search`); falls back to the old "this folder" wording only when
`folderToOpen` has nothing to name (a bare `~`/`/`). `SearchActionRow` grew
a `label: string` field carrying the row's full, ready-to-render text —
computed once in `search-action-rows.ts` rather than reconstructed at the
render layer from `query`+`commitInPlace`, since that reconstruction is
exactly the logic this finding says was wrong. This also makes
`folderToOpen` a live import again, resolving finding 6 below without
further changes.

**Finding 3 (MEDIUM, must-fix) — the not-found report fired on every
half-typed path.** The missing-path branch's guard (`typedAddress.status
=== "missing"`) fires for every uncommitted keystroke, not just a dead end
— `/home/iamsdas/Doc` showed "No such file or folder: Doc" plus a search
offer sitting right above the live `Documents` completion. Fixed per the
decided approach (not re-litigated): `searchAffordance` takes a new
`hasCompletions: boolean` parameter and returns `NOTHING` — suppressing
BOTH the notice and its search offer, not just the notice text — the moment
the completion dropdown already has a real match for the same text. No
Enter/commit requirement introduced (the user has objected twice in this
project to that shape of fix). Tested both directions in
`search-action-rows.test.ts` (`hasCompletions suppresses the not-found row
entirely`) and end-to-end in `search-dropdown-actions.render.test.tsx`
("a half-typed path WITH a live completion shows no not-found notice" /
"...with NO completions still shows it").

A consequence worth naming: combined with defect 2's existing fix (glob
queries never carry folder completions) and this fix (missing-path rows
now suppress themselves the moment completions exist), an action row and a
real folder completion can no longer coexist in the dropdown AT ALL — the
one coexistence scenario decisions.md documented earlier (a missing exact
name with a same-folder near-match) is exactly the case this finding closes.
The "arrow-key navigation across the action row" test was rebuilt around
the lone-action-row case (the gated-glob scenario) since the coexistence
case it originally exercised no longer occurs; finding 1's own
`tabDefaultIndex` guard in `SearchField.tsx` is consequently unreachable
through any live combination of props this component can produce today,
covered instead by `completion-keys.test.ts`'s direct, differentiating
unit tests. Left in as defensive correctness (finding 1 was independently
marked must-fix), not re-litigated.

**Finding 4 (LOW/MEDIUM) — `escapesFsPath` disagreed with the server on
`/*.csv` and bare `/`.** Read `resolve_query`'s own docstring
(`fused_render/index/query.py`): a leading `/` is tried as an absolute path
first, falling back to a depth-1 anchor at the box's own root ONLY when the
walk can't consume its first segment — which happens unconditionally (no
filesystem check needed) for a bare `/` and for a `/`-prefixed query whose
very first segment is itself glob-bearing (`/*.csv`), since `_walk_from`'s
loop condition already excludes a glob-bearing segment before ever
checking `os.path.isdir`. Added an `isBareSlash && baseSegments.length ===
0` check that returns `false` (not escaping) for exactly those two shapes,
matching the server exactly with no directory access of its own. A literal
first segment (`/etc/...`) stays gated on purpose — whether the server's
own fallback fires there depends on real filesystem state this predicate
cannot see, and the safe side of that ambiguity is the gate, same bias
`escapesBase` already takes. Added both cases to `query-base.test.ts`.

**Finding 5 (LOW) — `searchAffordance` mixed live and deferred values.**
`query`/`pristine` were computed from the live `query` prop while `escapes`
(from `useListingSearch.ts`) is computed off the deferred, debounced `q`
— the exact same live/deferred mismatch `FileSearchField.tsx`'s own
pristine guard was already fixed against (its own `isPristineQuery(q, ...)`
call, not `query`). `SearchField.tsx` gained a new `q: string` prop
(threaded from both `Listing.tsx` and `FileSearchField.tsx`, which already
had `q` from `useListingSearch` — `FileSearchField.tsx` just wasn't passing
it down); `pristine` and the `searchAffordance` call now read `q`, not
`query`. `query` itself stays live everywhere else (the `<input>`'s own
`value`, every keystroke handler) — only the affordance calculation needed
to agree with `escapes` about which render it describes. `isPathQuery`
stays on the live `query` too, per its own existing doc comment
(deliberately immediate, not deferred) — untouched, not part of this
finding.

**Finding 6 (LOW) — `enterPrompt` was dead code.** Confirmed by grep
(`tests/` has no reference — `test_github_login.py`'s own "Press Enter to
open github.com..." is an unrelated CLI prompt) that nothing outside its
own test called `enterPrompt` any more once Listing.tsx's banner was
removed, and finding 2's fix (reusing `folderToOpen` directly) didn't
resurrect it. Deleted `enterPrompt` from `enter-prompt.ts` and its own
`describe` block from `enter-prompt.test.ts`, replaced with direct
`folderToOpen` coverage (the folder-naming logic `enterPrompt` used to
wrap) so the behaviour it exercised isn't lost, just no longer tested
through a deleted wrapper. `pathNotFoundMessage` and `folderToOpen`
themselves both survive — both have real callers now.

**Finding 7 (test isolation, must-fix) — the hard-constraint test failed
in a full `bun test` run.** Root cause confirmed exactly as diagnosed:
`useListingSearch.render.test.ts` (unchanged by this branch) calls
`mock.module("@platform/lib/router", ...)` with a stub missing `navigate`;
bun's `mock.module` replaces the module registry for the WHOLE PROCESS, so
in a full run this file's own `navigate()` call (via `SearchField.tsx`)
silently bound to `undefined` and did nothing, dropping `pushStateCalls`
from 1 to 0. Fixed by having THIS file stub `@platform/lib/router` itself,
completely — every named export the real module has (confirmed against
the module file itself, not just this file's own current import graph, so
a future addition elsewhere in the tree doesn't reopen the same crash) —
and tracking `navigate()`'s own calls directly (`navigateCalls`) rather
than the real implementation's internal use of `history.pushState`. This
makes the test's verdict independent of whatever mock.module call ran
earlier in the process. Verified with two full, unfiltered `bun test` runs
at the repo's `frontend/` root: 4315 pass, 0 fail, both times.

## Three user-reported defects, running-screen review (2026-09-10)

**Item 1 — one arrow keypress moved both the dropdown highlight and the
listing selection behind it.** Confirmed the diagnosis exactly as given:
`useListingSelection.ts`'s document-level keydown listener is bubble-phase
(`document.addEventListener("keydown", onKeyDown)`, not capture), and
`SearchField.tsx`'s own `onKeyDown` never stopped the same ArrowUp/Down
from continuing to bubble past it. Fixed with `e.stopPropagation()` in the
one branch that fires only while the dropdown is open and navigable
(`action.type === "move"`, gated by `completionKeyAction`'s own
`showCompletion` check) — when the dropdown is closed that branch is never
reached, so the preserved behaviour (arrows drive the listing from the
search box or nothing focused) is untouched. Checked both capture-phase
listeners the task named, `usePaneFocusGuard.ts` and `listing/row-drag.ts`:
both register on `keydown` with capture `true`, but the former only acts on
`Tab` and the latter only on keys during an active row-drag (its own
`onKeyDown`, unrelated to arrows) — neither interferes with this fix, and
neither would have run before this stopPropagation anyway (capture fires
BEFORE bubble, and this fix is a bubble-phase stop, so it couldn't have
raced them even if they did handle arrows). No second "dropdown open" flag
threaded into `useListingSelection` — the fix lives entirely in
`SearchField.tsx`.

**Item 2 — the clear button left the box half-open.** Confirmed:
`clearSearchQuery` only called `setQuery("")`/`setPinnedOpen(false)`, and
the clear button's own `onMouseDown` calls `preventDefault()` (needed so
the browser's native mousedown-blur doesn't fire before the click
completes and steal the click), which meant the field never actually
blurred and `fieldActive` stayed true — an empty query is pristine by
definition, so `showSearchExamples(fieldActive, pristine)` stayed
satisfied. Fixed by having `clearSearchQuery` itself set `fieldActive`
false directly (same pattern `navigateToCompletion` already uses) and call
`searchInputRef.current?.blur()` — Escape's own separate
`e.currentTarget.blur()` call is now redundant and removed, so Escape and
the clear button share the exact one teardown path rather than the clear
button lacking half of what Escape already did. `search-clear-button.test.ts`
previously asserted the OLD behaviour ("never blurs") as intentional —
updated in place, plus a new driven test in
`search-dropdown-actions.render.test.tsx` confirming the dropdown actually
closes and DOM focus actually leaves on a real click.

**Item 3 — the two dropdown surfaces' vertical rhythm.** Read the actual
CSS before touching anything: the folder-completion rows, the not-found
notice, the search-action row and the teaching-example rows already ALL
render the one `.listing-completion-row` class (confirmed by grep across
`SearchField.tsx`), so they were already sharing a single padding
declaration (`padding: 4px 11px 4px var(--chip-inset)`) rather than two
separately-typed numbers — there was no second rule set to have drifted
from the first at the CSS level. The two-line, stacked layout the examples
rows use (`.listing-completion-example { flex-direction: column; gap: 2px;
}`, one line for the pattern, one for its sentence) makes them taller than
a single-line folder-completion row even at identical padding, which is
almost certainly what read as "more generous" on the running screen the
user compared them on. Per the task's explicit instruction (take the
padding FROM the examples panel, onto the completion rows, via one shared
value the same way `--chip-inset` already works) rather than re-litigating
which surface should move: extracted the existing 4px into a new
`--completion-row-pad-y` custom property (defined alongside `--chip-inset`
on `.listing-search-box`, inherited the same way) and raised it to 6px —
this could not be "given the examples panel's OWN number" since both
surfaces already read the same number; 6px is a judgment call for what
"more generous" means now that the value has a name, not a value copied
from a second rule that turned out not to exist. **This needs a human's
eyes on the actual running screen to confirm 6px is the right amount** —
it was chosen without one, since the two surfaces' padding was never
actually divergent in the committed CSS to begin with.

## Items 7-11, running-screen review continued (2026-09-10)

**Item 7 — the dropdown covering the table's NAME/SIZE/MODIFIED header.**
Instructed to check this AFTER item 4's cap landed, since a content-width
panel no longer spans the table. Reasoned through the numbers rather than
rendering: `.listing-completion` is now capped at 460px, left-aligned to
the field's own left edge; the table's SIZE column is a fixed 96px and
MODIFIED a fixed 172px, both anchored at the table's right edge, while
NAME takes the flexible remainder. On any listing wide enough to be
interesting, 460px from the left edge lands well short of where SIZE/
MODIFIED start, so those two headers are clear and only part of NAME's
own header sits behind the panel — which the task's own framing already
calls "normal behavior... rather than a defect" once it's partial, not the
full-row occlusion the report described. Made NO further change for this
item (no offset/shadow tweak) — that fallback was explicitly conditional
on "if it still reads as headerless," a judgment only a running screen can
make, and this reasoning cannot rule out a narrow pane where NAME alone
IS the visible header. **Needs a human on a running screen at typical AND
narrow widths** to confirm nothing needs the fallback after all.

**Item 9 — the offer row survived its own commit.** See the code's own
comments (`search-action-rows.ts`, `SearchField.tsx`) for the full
reasoning: `gated` (a fact about the query TEXT, `escapesFsPath`) became
`awaitingCommit` (a fact about STATE, `!gateOpen` — useListingSearch.ts's
own commit-gate flag, already computed, just not previously threaded to
Listing.tsx). This was the third regression traced to the same
text-vs-state confusion (defect 1, FINDING 5, this one) — the rename and
the switch to a real hook value close the whole class, not just this
instance. `escapes` itself, now dead once `awaitingCommit` took over its
one real job, was removed from SearchFieldProps and both hosts'
plumbing — grep confirmed no test asserted on the prop's presence.

**Item 10 — the same screenshot's other half: pressing the offer searched
without opening.** `commitInPlace: true` called `commitSearch()`, which
runs the search but never navigates — the breadcrumb, the URL, and the
search rows' own relative paths (built against the server's actual base)
disagreed about where the search ran. Added `resolveFolderToOpen`
(enter-prompt.ts, alongside `folderToOpen`) to turn the row's own display
text into a real fsPath (expanding a leading `~` via `home`, the same way
`escapesFsPath` already does), and switched both the action row's press
and a bare gated Enter to `navigate(folder, { isDir: true, q: query })` —
the exact mechanism FileSearchField.tsx's own file-to-folder hand-off
already uses (`navHintQCommitted`), so the destination's gate opens
immediately with the query text intact. A path-shaped query's bare Enter
(the PR #1091 hard constraint) is untouched — this only changed the
NON-path, gated (`commitInPlace: true`) branch.

**A consequence worth naming**: after item 10, pressing a `commitInPlace`
row or a gated Enter always NAVIGATES to a different fsPath (never merely
updates state on the same one, unless `home` hasn't resolved yet, the one
fallback branch that still calls `commitSearch()` in place). Item 9's own
before/after test therefore had to be a PURE-FUNCTION test (the same query
text with `awaitingCommit` true then false, synthetically), not a driven
render test staying on one mount — a real navigation would remount the
component entirely, and this test suite has no harness for simulating a
second page's mount with the first's `history.state` carried over. The
driven tests in `search-dropdown-actions.render.test.tsx` instead confirm
the NAVIGATION CALL itself is correct (right folder, right query, `~`
expanded); closing the loop end-to-end (mount the destination, confirm no
offer row) is left as something **a human on a running screen** can
confirm faster than building that harness.

**Item 11 — the match count was reported twice, and folds item 6 into
itself.** The footer's own "N matches" line duplicated the search box's
own pinned chip verbatim, with none of the chip's own caveat/elapsed-time
detail (confirmed by reading both — the footer's old branch used its
`truncated` parameter for NOTHING, and the box's chip already owns
truncation, the index-scan caveat, and the latency figure). Division of
labour: `statusLine`'s `searching` branch now returns `null` (no line) when
nothing is selected, keeping `"N of M selected"` for when something is.
Once that's the only distinction, `showsSearchFooter` (item 6's own
one-line alias) was a pure rename of `showsSearchHits` with nothing left
to say for itself — deleted; both of its call sites (the byte-sum gate,
`statusLine`'s `searching` input) now read `showsSearchHits` directly,
per the task's own instruction to "leave one clear reason for it to
exist, not two half-reasons." Item 6's own fix (fall back to the folder's
honest item count while gated) is now just a consequence of `statusLine`
never being asked to report a search count in that state at all.

## Item 12 — the pin crowds out the query text (running-screen review, 2026-09-10)

**The defect**: a committed search in a ~600px-wide box rendered the pin
"31 matches · not refreshed" while the query `~/*/*.zip` clipped to about
four visible characters (`~/*/`) — confirmed present but invisible by
reading the input's own `value`. Root cause: `.has-pin`/`.wide-pin`
(explorer.css) reserve a FIXED `padding-right` on the input regardless of
the box's actual width, and `searchCount` (Listing.tsx) was one
un-splittable string — count, caveat and latency baked into a single
value by `withCaveat` plus a `· <elapsed>` suffix — so there was no way to
give the query room back except hiding the WHOLE pin at once, which
nothing did.

**The rule, applied**: the query outranks the pin always. The pin degrades
one rung at a time as the box narrows, dropping the least actionable part
first (Listing.tsx's own comment already explained WHY the count alone
gets "matches" appended while the caveat/latency don't — the same
ordering: a caveat you cannot read is worse than one you cannot act on).

**What changed**:
- Listing.tsx no longer folds the caveat/latency into `searchCount` via
  `withCaveat`/string concatenation. A new `searchCountDetail: string |
  null` carries just that piece (`caveat.note` or the formatted elapsed
  time); `searchCount` stays the bare count text ("31 matches", "top 100
  of 4.9K+"). `searchCountFull` (the title/aria-label sentence) is
  UNCHANGED — it always carries the complete claim, truncated or not.
- SearchField.tsx renders the pin as `<span class="listing-search-count"
  title={searchCountFull} aria-label={searchCountFull}>` (title/aria-label
  set ONCE, unconditionally, on the outer element — this is what keeps the
  freshness caveat reachable even once nothing is visible) wrapping two
  children: `.listing-search-count-base` (the count) and
  `.listing-search-count-detail` (the caveat/latency, only rendered when
  `searchCountDetail !== null`).
- explorer.css gained two `@container` blocks on `.listing-search-box`'s
  own EXISTING `container-type: inline-size` (the same axis the shortcut
  hint's `@container (max-width: 360px)` rule already uses — not a second,
  independent width signal):
  - **480px** (judgment call, NOT measured — needs a human on a running
    screen): hides `.listing-search-count-detail` and collapses the
    `.wide-pin` input-padding reservations (210/240/126/156px) down to
    the plain `.has-pin` values (116/146/126/156px — the crumb-slot pair
    already coincide). Chosen as comfortably above the wide reservation
    plus a usable amount of query room; not verified against an actual
    rendered box.
  - **360px** (reused verbatim, not invented): hides BOTH
    `.listing-search-count-base` and `-detail`, and collapses ALL
    `.has-pin` reservations back to the same padding the box uses with NO
    pin at all (10px / 40px-with-clear). `.listing-search-count` itself
    keeps a small (10×10px) footprint rather than `display: none` — an
    invisible-but-present hoverable target, so the tooltip (the untouched
    `title`/`aria-label`, still the FULL sentence) stays reachable exactly
    as the task required ("do not lose the freshness caveat silently").
  - Every override selector adds `:has(.listing-search-count)` (a
    selector already used elsewhere in this file, `#breadcrumb:has(...)`)
    so it only fires when the element it targets is the MATCH-COUNT chip —
    the streaming spinner's own, separate, much smaller `.has-pin`
    reservation (`searching && spinner`, no count yet) is untouched by
    this fix, on purpose: the reported defect and this rule are both about
    the count/caveat/latency chip specifically.
  - The 360px block's selectors deliberately match BOTH `.has-pin` and
    `.has-pin.wide-pin` explicitly (not just the broader `.has-pin`) so
    its specificity ties the 480px block's `.has-pin.wide-pin` selector at
    every width where both blocks' media conditions are true — source
    order (360's block placed after 480's) is what decides the winner at
    that tie, and relying on the narrower selector's lower specificity
    alone would have let the 480px rung's wider reservation silently win
    back below 360px.

**Not touched**: `FilesHome.tsx` (out of scope, per spec); the tracked
`DECISIONS.md`; the spinner-only pin case; `searchCountFull`'s own
composition logic (still folds the caveat/elapsed into one sentence for
the tooltip — only the VISIBLE chip needed splitting).

**Cannot be verified headlessly** — this entire fix is CSS container
queries and fixed-padding arithmetic reasoned through, not rendered. A
human needs a running screen to confirm, at minimum:
- the 480px threshold is the right point to drop the detail — it was
  chosen from the padding numbers alone, not observed;
- the pin visually reads as "giving way" smoothly rather than jumping;
- the 360px rung's 10×10px hoverable remnant is actually discoverable
  (and not, say, sitting under the clear button or the star) and its
  tooltip fires on hover in both the crumb-slot and plain hosts;
- the specific reported scenario (~600px box, "31 matches · not
  refreshed", `~/*/*.zip`) now shows the query text at whichever rung a
  600px box lands on.

Scoped tests: `search-count-pin-degrade.test.ts` (new, 8 tests — CSS/JSX
text assertions in the same no-DOM pattern `search-examples-width.test.ts`
and `search-mode-chip.test.ts` already use, since bun's jsdom does not
evaluate `@container`), plus the existing `search-mode-chip`,
`search-clear-button`, `index-caveat`, `search-examples-width`,
`search-hint-width`, `search-action-rows`, `search-dropdown-actions.render`
(isolated) and `FileSearchField.render` suites re-run clean. `bun run
typecheck`: clean.

## Cannot be verified headlessly

See SPEC-omnibox-search-affordance.md's own "Cannot be verified headlessly"
section (kept up to date there, not duplicated here) - the two-magnifiers
risk it originally flagged is resolved by item 3's revision, but the
button's own narrow-width collapse point and the teaching panel's
precedence over completions on a pristine path both still need a human on
a running screen. (The panel's own real-world EXAMPLES are moot as of
item 8 below - it is fixed prose now, not folder-derived content - but
whether that prose reads well at the panel's 460px cap is itself an item
4/8 visual question, not yet confirmed on a screen.)

## Finding 7's fix was itself the CI failure - corrected (2026-09-10)

Finding 7's "stub every named export" fix (see above) traded the missing-
export crash for a worse problem: a COMPLETE `mock.module("@platform/lib/
router", ...)` still replaces the module registry entry for the whole bun
process, so `withPreviewFlag` - re-typed in that stub as the identity
function `(src) => src` - silently became what every file loaded AFTER this
one saw too. `paneUrl.ts` imports `withPreviewFlag` and `paneUrl.test.ts`'s
own "the shell-mounted flags ride on the end, and are idempotent" test (both
completely untouched by this branch, confirmed by
`git diff origin/main..HEAD -- frontend/src/apps/claude/pane/` returning
nothing) failed on Linux CI for exactly that reason - CI enumerates test
files in a different order than macOS, and Linux's order let
`search-dropdown-actions.render.test.tsx`'s stub apply before
`paneUrl.test.ts` loaded, where macOS's order didn't.

**Approach taken (candidate b): `spyOn` the one function this file needs
to observe, not `mock.module` at all.** `search-dropdown-actions.render.
test.tsx` now does `const router = await import("@platform/lib/router")`
(dynamically, after the file's own `location` stub and after
`FileSearchField`'s own transitive import already evaluated the module -
a static `import * as router` at the top would be hoisted ahead of the
`location` stub and crash router.ts's module-init IIFE) and, in
`beforeEach`, `spyOn(router, "navigate").mockImplementation(...)` to
collect `navigateCalls`; `afterEach` calls `navigateSpy.mockRestore()`.
Every other export (`withPreviewFlag`, `urlForFsPath`, `replaceSearch`,
`currentUrl`, …) is now the REAL implementation for this file's own tests,
running against the real `globalThis.history`/`document`/`window` stubs
already set up in `beforeEach` - which is also strictly more honest than
the old hand-typed stub, since a future export this file's own import graph
doesn't reach today can no longer silently diverge from the real thing.

Verified `spyOn` on this module works and genuinely reverses: a throwaway
test confirmed `spyOn(router, "navigate")` replaces the live ESM binding
(observed via a tracked call array) and `mockRestore()` puts the real
function back (observed by letting it run for real afterward and seeing
its own side effect fire) - bun's ESM interop here behaves like a mutable,
shared object per module specifier, not a frozen native-ESM namespace.

**Candidate (a) - a component seam - was not available and not added.**
`SearchField.tsx` calls the imported `navigate` directly
(`src/apps/explorer/SearchField.tsx:32,236,552`); there is no `onNavigate`
prop or callback already on the component to assert through, and adding
one purely to make this test observable would be reshaping production code
for the test's sake, which the task ruled out unless a seam already exists
or is genuinely the cleaner design - neither is true here, since the
component correctly owns navigation as a direct effect and no other caller
needs to intercept it. **No production code was touched by this fix.**

**Candidate (c) - restoring the module registry - didn't apply**: there
was never a registry entry to restore, since this fix stops writing one.

**Proof, and a pre-existing complication this surfaced:**

- Full `bun test` from `frontend/`, run twice: **4315 pass, 0 fail** both
  times (208 files), matching Finding 7's own prior verification exactly.
- `bun run typecheck`: clean, no errors.
- `bun test src/apps/claude/pane/paneUrl.test.ts
  src/apps/explorer/listing/search-dropdown-actions.render.test.tsx` (both
  orders): **36 pass, 0 fail** each way - `paneSrcFor > the shell-mounted
  flags ride on the end, and are idempotent` passes in both, and all 8 of
  this file's own tests (including the hard-constraint Enter test) pass in
  both.
- The exact 3-file combo the task specified (adding
  `useListingSearch.render.test.ts`) crashes in BOTH argument orders with
  `SyntaxError: Export named 'getConfig' not found in module
  '.../platform/lib/api.ts'`, aborting `search-dropdown-actions.render.
  test.tsx`'s entire file (0 of its 8 tests run, including the hard-
  constraint test) before either can produce a pass/fail verdict for it.
  **Confirmed pre-existing and unrelated to this fix**: the IDENTICAL crash
  reproduces byte-for-byte with the file reverted to its PRE-fix state
  (the complete router mock.module version), in both argument orders.
  Root cause is `useListingSearch.render.test.ts`'s OWN pre-existing,
  unchanged `mock.module("@platform/lib/api", ...)` (3 exports:
  `indexRank`, `requestFolderScan`, `getPrefs` - missing `getConfig`,
  which `home-path.ts` imports and calls once `FileSearchField` mounts) -
  the exact same disease as Finding 7's original bug, just on a different
  module (`api.ts` not `router.ts`) and a different pre-existing culprit
  file, already named in this document's own "Known infra fragility"
  section above and explicitly out of scope for this branch to fix.
  `useListingSelection.render.test.ts` paired with this file (2-file combo,
  both orders) crashes the same way with a DIFFERENT missing export
  (`NAV_EVENT` from `router.ts` this time) - also reproduced identically on
  the pre-fix file, also pre-existing, also unrelated.
- Both of those subset crashes are provably artifacts of hand-picking a
  small file subset, not of real CI order: bun sorts the files it loads
  internally (both argument orders of the same 3 files produced BYTE-
  IDENTICAL output, proving CLI argument order does not control bun's
  execution order at all), and the full, unfiltered suite - the actual
  shape of what CI runs - passed twice with zero failures, meaning neither
  crash is reachable through any real file ordering the full suite
  produces.
- Net effect on the actual guard: `paneSrcFor`'s idempotence test and the
  hard-constraint Enter test both pass in every configuration where they
  get to run at all (isolation, the 2-file router-collision combo in both
  orders, and the full suite twice); the only runs where the hard-
  constraint test doesn't produce a verdict are the two pre-existing,
  unrelated subset crashes described above, where it doesn't run at all
  rather than running and failing - not a regression this fix introduced
  or could fix without editing the other files, which the task explicitly
  ruled out.

## Build-breaking `*/` inside a CSS comment (2026-09-10)

- `explorer.css`'s "THE DEFECT" comment above the pin-degradation rules
  quoted a concrete example glob (home dir, wildcard folder, wildcard
  filename, `.zip`) using literal asterisks. Written that way, the
  wildcard-then-slash pair reproduced the two-character sequence `*/`,
  which is CSS's comment terminator - comments don't nest and have no
  escape, so it closed the block comment right there, mid-sentence.
  Everything after it on the following lines was then parsed as live CSS,
  and the apostrophe in "isn't" (a few words later, now outside any
  comment) opened a string literal that never closed. Vite's build failed
  with an opaque "Unterminated string" pointing at `shell.css` (the
  bundled entry point), not at the actual `explorer.css` line - the real
  location took a grep for the `*/*` byte pattern to find.
- Fixed by rewriting the example in words ("a two-segment glob ending in
  `.zip` ... a wildcard folder, a wildcard filename") instead of literal
  asterisks, so the sequence never appears. The concrete shape of the
  example is preserved; only the glob syntax itself is spelled out.
- Why the full frontend suite (4324 tests) and `bun run typecheck` both
  stayed green on the broken commit: every CSS-text test in this
  directory (`search-mode-chip`, `search-examples-width`,
  `search-count-pin-degrade`, etc.) reads `explorer.css` as a string and
  strips comments with `/\/\*[\s\S]*?\*\//g` before asserting on
  selectors - a non-greedy regex, which itself stops at the FIRST `*/`,
  same as the real CSS parser, so it silently "corrected" the malformed
  file into something that read as valid CSS text and never noticed
  anything was wrong. `tsc` never parses CSS at all, so typecheck cannot
  see this class of bug either. Only a real CSS parser - which only
  `vite`/`@tailwindcss/vite` (via `lightningcss`) run in this pipeline -
  ever actually hits the syntax error.
- Added a permanent guard for this class of bug:
  `frontend/src/apps/explorer/listing/explorer-css-comment-syntax.test.ts`
  runs `lightningcss`'s own `transform()` (already a project dependency,
  the same parser tailwindcss v4 uses) directly over `explorer.css` and
  asserts it doesn't throw - no DOM, no build step, and it reproduces the
  build failure exactly (verified against the pre-fix comment text before
  committing the fix). This is different from the file's other CSS tests:
  they strip comments and assert on declarations; this one only checks
  the file parses as CSS at all, which is the property that broke.
- Grepped `tests/` (the Python suite) for the comment text being rewritten
  and for `explorer.css` generally - no literal assertions on this file's
  source lines exist there, so nothing else needed updating.

## The star overlapping the last crumb (2026-09-10, running-screen review, PR #1092)

**The defect**: at rest (field unfocused, crumbs showing), the bookmark star
overlapped the last crumb's text - screenshot showed `~ / Downloads /
Archive` with the star's tinted hover pill covering "ve". Diagnosis handed
in with the task, verified rather than re-derived: `.listing-search-crumbs`
reserved only `right: 10px` (enough to clear the box's own border), while
`#breadcrumb .crumb-search-slot .listing-search-box .bookmark-star-btn`
sits at `right: 8px` in a 24px hit box. A short path never shows this
because the crumbs are left-aligned; `PathCrumbs.tsx` deliberately pins the
strip's `scrollLeft` to its own END (unchanged, per the task's own
instruction - that comment at `explorer.css:1670`-ish is load-bearing), so a
long path's tail runs straight into the star's zone instead.

**Fix**: `--pin-right` (already 40px on `.crumb-search-slot
.listing-search-box`, set for exactly this same star clearance so the
count/spinner pin doesn't collide with the star either) is the existing
vocabulary for this distance - not a new custom property, per the file's
own `--chip-inset`/`--completion-row-pad-y` precedent against duplicating a
number across rules. Added one override, scoped to the host that actually
has a star:

```css
#breadcrumb .crumb-search-slot .listing-search-box .listing-search-crumbs {
  right: var(--pin-right);
}
```

Specificity (1 id + 4 classes) beats the base `.listing-search-crumbs {
right: 10px; }` rule regardless of source order, and `--pin-right` inherits
from the ancestor `.listing-search-box` element down to the `.listing-
search-crumbs` child, so no new cascade path was needed. The base rule and
the inline/pane host (no star, `--pin-right` never set to 40px there) are
untouched - confirmed the override's ancestry requirement
(`.crumb-search-slot .listing-search-box`) is exactly the scope the star
selector itself uses, so the two rules describe the same host by
construction, not by coincidence.

**The count/spinner pin - checked, not reachable alongside the crumbs**:
the task asked to verify whether a committed search's count chip
("31 matches") could also collide with the crumbs. Traced both render
conditions instead of assuming: the crumbs render only while `query === ""
&& !pinnedOpen` (`SearchField.tsx`); the count/spinner pin (`hasPin` in
`Listing.tsx`) requires `searchCount !== null` or `searching && spinner`,
and `useListingSearch.ts`'s own `searchState` collapses to `IDLE_SEARCH`
whenever `!runsSearch` (`runsSearch = searching && !isPathQuery`, and
`searching` itself requires a non-empty query) - so an empty query can never
produce a non-null count or an active spinner. The two states are disjoint
by construction. No rule added for the count chip; this is stated rather
than assumed, per the task's own instruction not to add a speculative rule.

**Tests**: new `frontend/src/apps/explorer/listing/search-crumbs-star-
clearance.test.ts`, same no-DOM CSS-text-parsing pattern
`search-mode-chip.test.ts` and `search-count-pin-degrade.test.ts` already
use (`rulesFor()` matches on the exact trimmed selector string, comments
stripped first). Asserts: the override rule exists and reads `--pin-right`
rather than a literal number; the base, unscoped `.listing-search-crumbs`
rule is untouched (still `right: 10px`); the override's ancestry matches
the star's own selector; the count/spinner-pin non-collision claim above is
grounded in the actual `query === "" && !pinnedOpen` condition read from
`SearchField.tsx`, not asserted as given.

Ran alongside the existing `search-mode-chip`, `search-hint-width`,
`search-examples-width`, `search-count-pin-degrade`, and
`explorer-css-comment-syntax` suites (the last one specifically because
this is another CSS edit, and a `*/` inside a comment already broke the
build once this session) - 35 pass, 0 fail. `bun run build` and `bun run
typecheck` both clean. Grepped `tests/` for `listing-search-crumbs`,
`pin-right`, `chip-inset`, `bookmark-star-btn`, `crumb-search-slot` - no
hits, nothing else to update.

**Cannot be verified headlessly** - a human needs a running screen for:
- whether the path's tail now clears the star with air that reads as
  deliberate (some visible gap) rather than merely "not touching" -
  `--pin-right`'s 40px was tuned for the count chip's own text, not
  measured against the star glyph's actual rendered gap from the crumbs'
  monospace text, though it is by construction at least as much clearance
  as the star needs (the star's own box ends at `8px + 24px = 32px` from
  the edge; the crumbs now stop 8px further in than that);
- whether a long path still visibly ends at the current folder (the
  tail-pin behavior itself is unchanged, but worth a glance alongside the
  new stop point);
- both themes and the hover state of the star sitting that much closer to
  the crumbs text now that they no longer overlap.

## The teaching panel must vanish on the first keystroke (2026-09-10)

**The request**: "the 'type any text' stuff should only be shown for the
cwd. remove it as soon as the user starts typing" — the three-sentence
teaching panel (`showSearchExamples`) must gate on "the box still holds the
untouched, pre-filled cwd," not the broader `isPristineQuery`.

**Why `isPristineQuery` was the wrong gate for the panel specifically**: it
returns `true` for two shapes the panel must NOT stay up through —

1. An empty query (`trimmed === "" → true`). Deleting the box down to
   nothing is not "the cwd."
2. The cwd plus a trailing slash, and anything with leading/trailing
   whitespace, because it `.trim()`s and strips ONE trailing slash from
   both sides before comparing. Typing `/` after a pre-filled path (the
   single most natural first keystroke when extending it) left the panel
   up through that keystroke.

**Why `isPristineQuery` itself was left untouched**: it has other
consumers, and each one genuinely wants the broader, tolerant notion:

- `search-action-rows.ts`'s `searchAffordance` — `if (pristine) return
  NOTHING` (its very first check) must still swallow an empty query (its
  own second check, `trimmed === ""`, is a redundant safety net for the
  same case, not the primary guard — narrowing `pristine` here would still
  be caught by that second line, but there was no reason to touch a
  consumer that already works and isn't the one with the bug).
- `SearchField.tsx`'s `showCompletion` exclusion (`!pristine`) — must keep
  excluding an empty box from ever offering folder completions.
- `FileSearchField.tsx:119`'s `if (isPristineQuery(q, parentPath, home))
  return;` — the navigation-hand-off guard. Must still treat an emptied
  box, and a manually-trailing-slashed re-typing of the parent path, as "no
  real edit yet," or focusing/clearing the box would trigger a premature
  hand-off to the parent folder. Confirmed this is unaffected: this call
  site was never touched.

**What changed instead**: a second, narrower predicate,
`isUntouchedCwdQuery(query, fsPath, home)` (query-pristine.ts) — exact
string equality against `fsPath` or `contractHome(fsPath, home)`, no empty
case, no trailing-slash tolerance, no trim. `SearchField.tsx` now computes
`untouchedCwd = isUntouchedCwdQuery(query, fsPath, home)` and passes THAT
(not `pristine`) into `showSearchExamples`. `showSearchExamples`'s
parameter was renamed `pristine` → `untouchedCwd` so the function's own
signature doesn't quietly imply it still means the old thing.

**Live `query`, not deferred `q`**: every other `isPristineQuery` call in
this file reads the deferred `q` (Finding 5's fix, to stay in step with
`escapes`/`awaitingCommit`, which are also computed off `q`). The panel has
no such deferred sibling to stay in step with — its whole job now is to
react on the keystroke ITSELF, so `untouchedCwd` reads the live `query`
prop (updated synchronously by the input's own `onChange`), not `q`. Using
`q` here would have reintroduced exactly the kind of one-render lag the
user's bug report was about.

**query-pristine.ts's file comment** was rewritten to explain both
predicates and why there are two — see the file itself; not duplicating
that explanation here.

**Verification**: `bun run build` and `bun run typecheck` both clean (the
`*/`-in-a-CSS-comment lesson from earlier this session was CSS-only and
doesn't apply to this change — no CSS touched). Ran in isolation:
`query-pristine.test.ts` (14 pass, including 7 new tests for
`isUntouchedCwdQuery` covering the empty/trailing-slash/whitespace cases
`isPristineQuery` tolerates and this predicate must not),
`search-examples.test.ts` (3 pass, untouched — its tests pass raw booleans
and don't encode which predicate produces them), `search-action-rows.test.ts`
(17 pass, untouched — `searchAffordance`'s own pristine-consumer behavior
is unaffected), `search-dropdown-actions.render.test.tsx` (14 pass),
`FileSearchField.render.test.tsx` (4 pass). Grepped `tests/` (the Python
suite) for `isPristineQuery`, `isUntouchedCwdQuery`, `showSearchExamples`,
`query-pristine`, `search-examples` — no hits, nothing there asserts on
these symbols or files.

**Confirmed NOT regressed**: an empty box still does not run a search or
offer to search (that's `isPristineQuery`'s job, untouched, in
`searchAffordance` and `showCompletion`); `FileSearchField.tsx`'s
navigation-hand-off guard is untouched and still reads the broader
`isPristineQuery` off `q`.

**Cannot be verified headlessly** — a human needs a running screen to
confirm: the panel is up on focus over an untouched, pre-filled folder, and
disappears the INSTANT a key lands — including when that key is `/`
appended to the end of the pre-filled path, or a bare space.

## Pattern search now caps at 100 rendered rows, matching substring (2026-09-10)

**The ask**: the substring path already caps its rendered list at the first
100 matches (`SEARCH_RESULT_CAP`, listing/types.ts); the glob (pattern) path
had no display cap at all — `capHits` returned every fetched hit
unmodified for `mode === "glob"`, and `resultCountLabel` never said "Showing
top N of" for it either. A broad pattern (`*.zip` over a large tree) could
therefore render thousands of rows at once. Cap the pattern path the same
way, reusing the existing constant rather than adding a second one.

**Where the cap actually lives**: entirely in the frontend, at the response-
assembly layer — `listing/result-cap.ts`'s `capHits`/`resultCountLabel`,
consumed by `useListingSearch.ts` (`visibleHits = capHits(displayHits,
mode)`). It is not a query parameter and not an index-layer limit: ranking
still runs over the whole corpus (substring) or the whole match set (glob),
and the SERVER fetch ceilings — `SEARCH_RANK_LIMIT` (200, substring) /
`SEARCH_GLOB_RANK_LIMIT` (5,000, glob) — are a separate, pre-existing "how
many rows are worth asking for" limit that stays exactly as it was; only how
many of those fetched rows get RENDERED changes here. This is why the fix
belongs in `result-cap.ts`/`types.ts`/`useListingSearch.ts` and touches
nothing server-side (`fused_render/index/query.py`, `server/routers/
search.py`) — confirmed by tracing both query shapes from the omnibox
request through to rendering before writing a line of the fix.

**What changed**: `capHits` no longer special-cases `mode === "glob"` — both
shapes slice to `SEARCH_RESULT_CAP` the same way. `resultCountLabel` no
longer special-cases glob either — past the cap it says "Showing top 100 of
N[+]" for both shapes; under the cap, the plain "N matches". `mode` stays on
both signatures (callers still pass it) but no longer changes the behavior
of either function — a no-op parameter kept only because dropping it would
be a wider signature change than this task asked for. Updated the comments
on `SEARCH_GLOB_RANK_LIMIT` (types.ts) and the `visibleHits` computation
(useListingSearch.ts) that described the old, now-false "a glob answer is
never cut down to SEARCH_RESULT_CAP" behavior.

**TDD**: rewrote the two `result-cap.test.ts` tests that encoded the old
uncapped-glob behavior into tests for the new capped behavior (watched them
fail against the unmodified code, then implemented). Added a companion test
for the short-list case (glob under the cap is untouched, same array, no
copy) to mirror the existing substring coverage.

**Verification**: `bun test src/apps/explorer` (1125 pass) and `bunx tsc
--noEmit` (clean). No Python file touched, so no Python test run — the cap
never was a server-side concept, confirmed above.

## Zero-hit PATTERN search offers to rerun broadened (2026-09-10)

**The ask**: a committed PATTERN (glob) search that settles on zero matches
gives up too early — `/home/iamsdas/*.js` finds nothing because the pattern
only reaches direct children, even when `/home/iamsdas/**/*.js` (searching
every depth) would find plenty. The results body's first row should become
an offer to rerun with that broadened form, activated the same two ways
every real row is — Enter and click.

**Row activation has exactly two call sites** (`Listing.tsx`'s
`onRowPointerUp`, `useListingSelection.ts`'s Enter-key handler), and both are
keyed on `navRows`/`rowCtxByPath` — real filesystem paths only. Two ways to
make an offer land on Enter/click were on the table: teach the router a
non-filesystem "action" case it can navigate to, or give both call sites a
small branch that recognizes this one offer and short-circuits before
`navigate()`. Teaching the router was rejected — `navigate(path, {isDir})`
is a contract every OTHER consumer (bookmarks, address bar, the crumb strip,
drag/drop) already relies on meaning "this is a real filesystem entry"; bending
it to also accept a synthetic action either weakens that contract for
everyone or requires a parallel special case inside `navigate` itself, which
is strictly more surface than a branch at the two places that already choose
between "do something" and "do nothing" today (the empty-`navRows` guard in
useListingSelection.ts's Enter case; the `rowCtxByPath.get(path)` lookup in
`onRowPointerUp`). The branch approach also means the offer never has to
pretend to be `RowCtx`-shaped.

**The sentinel** (`listing/zero-match-offer.ts`): a NUL-byte-prefixed string,
`ZERO_MATCH_OFFER_PATH`, with a single predicate, `isZeroMatchOfferPath`,
that both call sites import — never a literal string compared in two
places. It cannot collide with any real row: a NUL byte is illegal inside a
path on POSIX (rejected by the kernel) and on Windows (rejected by every
Win32 file API), so no path this app can ever list, type, or sync from a
filesystem can equal it.

**Kept out of `navRows`/`rowCtxByPath`**: those two are the one flat list
every OTHER `navRows` consumer already assumes is nothing but real,
selectable rows — Select All, the marquee sweep, range-select, `useFlip`,
the reconcile-on-vanish effect, the footer's selection count. Folding the
sentinel into that array would have required auditing and adjusting every
one of those for a single row that isn't selectable, isn't draggable, and
must never count toward "N selected." Instead `useListingSelection` takes a
new, optional `zeroMatchOffer: { path, onActivate } | null` prop, read only
inside the Enter handler's pre-existing `if (!rows.length) return;` guard —
the one branch that already knows `navRows` is empty. Passing `null` (the
default) makes every existing caller's behavior byte-for-byte the same as
before this change. `Listing.tsx`'s `onRowPointerUp` checks the sentinel at
the very top of the function, before the `pressRef` read — the offer row
never registers an `onPointerDown`/press-tracking entry in the first place,
so it can't accidentally call `selectOnly()` and manufacture a phantom
one-item selection on a plain click.

**Deriving the broadened pattern from the query itself**
(`listing/glob-broaden.ts`, `broadenGlobPattern`): not a special case for the
one example in the ask. Mirrors `resolve_query`'s own glob detection
(`"*" in raw`) and its own free broadening (a slash-free glob already gets an
implicit `**/` prefix server-side, so there is nothing left to widen for
`*.js`). For a query that does carry a slash, `**/` is inserted immediately
before the query's own LAST segment, leaving the base and every earlier
segment exactly as written. Returns `null` — no offer — when the query isn't
a glob at all (a substring query is untouched), or is already maximally
broad (no slash, or the segment before the last is already `**`, meaning
offering to widen it again would rerun the identical search).

**Rerunning without a stale-closure race** (`useListingSearch.ts`,
`rerunQuery`): the offer's `onActivate` needs to both rewrite the box AND
commit the search in the one call — `setQuery` alone would leave the
broadened text sitting behind whatever commit gate the ORIGINAL query left
open or closed. `rerunQuery` sets `committedGate.current` directly rather
than calling `commitSearch()` after `setQuery()`, because `commitSearch`
reads the `query` state variable, which would still hold the pre-update text
until React's next render lands — a value committed against stale text
would never match once the real update arrives.

**The row's own treatment**: reuses the existing `status-message`
`<tr><td colSpan={cols}>` shape already used for every other non-file row in
this body (`Searching…`, the capped-away count) rather than a `fh-row`, and
the `fh-link-button` class already used for the one other piece of
interactive, non-file-navigating text in a results row
(`empty-result.tsx`'s "enable it in Preferences" link) for the "Search …
instead" control — no new visual treatment invented. States the original
query found nothing AND names the broadened pattern in the same row, so
Enter/click's effect is never a surprise.

**TDD**: `glob-broaden.test.ts` (7 cases: shallow slash-bearing pattern,
relative two-segment pattern, depth-1 leading-slash anchor, already-`**/`-
broadened → null, bare slash-free glob → null for two shapes, non-glob query
→ null, empty query → null) and `zero-match-offer.test.ts` (3 cases: sentinel
contains a NUL byte, predicate recognizes only the sentinel, predicate
rejects real paths — including ones that echo the sentinel's own words, the
empty string, and `"/"`) were written and watched fail against
`Cannot find module` before either implementation file existed. Added two
cases to `useListingSelection.render.test.ts`'s Enter suite: the offer's
`onActivate` runs (and nothing navigates) when `zeroMatchOffer` is set and
`navRows` is empty; plain empty rows with no offer still do nothing, exactly
as before.

**Verification**: `bun test src/apps/explorer` (1137 pass, 0 fail) and
`bunx tsc --noEmit` (clean). Grepped `tests/` (the Python suite) for
`Listing.tsx`, `useListingSelection`, `useListingSearch`, `onRowPointerUp`,
`EmptyResultMessage` — the only hits are `test_claude_ask_lifecycle.py`/
`test_claude_fix_with_ai_ask.py` asserting on the unrelated
`window._fusedClaudeAsk` wiring, untouched here. Confirmed live via
`agent-browser` against the already-running dev server: typing
`/home/iamsdas/*.js` under `/explorer/view/home/iamsdas` (a folder with no
direct `.js` children but several levels down) renders "No matches
for /home/iamsdas/*.js. Search /home/iamsdas/**/*.js instead" as the sole
body row; both clicking the button and pressing Enter with nothing selected
rewrite the box to the broadened pattern and populate real hits.

## Code review of the zero-hit offer: keyboard, pointer-button, and ladder shape (2026-09-10)

**Keyboard activation** (`Listing.tsx`, the offer row's `<button>`): wired
only `onPointerUp`/`onPointerDown`, no `onClick`. A focused `<button>` fails
`useListingSelection.ts`'s `navActive` predicate (search input or document
body/root only), so Tab-focusing the offer and pressing Enter or Space never
reached the document-level Enter handler's own zero-match branch at all —
the offer was mouse-only. Added `onClick`, guarded with `if (e.detail !== 0)
return;`: `detail` is the click count a pointer device reports (always
>= 1); a keyboard-synthesized click reports 0, which is exactly the one
case `onPointerUp` never sees (no pointer events fire for a keyboard
activation at all), so the guard cannot suppress a real keyboard press while
still preventing a double run for a mouse click.

**Pointer button** (`Listing.tsx`, `onRowPointerUp`'s sentinel branch): the
zero-match offer's branch ran on any button release at all, unlike every
real row (`onRowPointerDown` gates on `e.button === 0`). A right-click,
middle-click, or a drag that released over the offer row all reran the
broadened search, and a right-click also had its own job — opening the
background context menu — that this stepped on. Added the same
`e.button !== 0` guard real rows already carry.

**The ladder's shape**: covered in the commit above this one
("Glob broadening: named, ordered rungs instead of one recursive
mutation") — name-widening before subfolder-widening, because it keeps the
search in the folder the user was already looking at; a future rung (case-
insensitivity, say) is one more record in `glob-broaden.ts`'s `RUNGS` list,
nothing else to edit.

**TDD**: both wiring bugs are pinned at the source in
`zero-match-offer.test.ts` (`selection.test.ts`'s own precedent for this
technique — Listing.tsx has no full-mount render harness): watched fail
against the un-fixed source (`e.button !== 0` absent from the sentinel
branch; `onClick=` absent from the offer row) before either fix landed.

**Verification**: `bun test src/apps/explorer` (1145 pass, 0 fail) and
`bunx tsc --noEmit` (clean).

## SEARCH_GLOB_RANK_LIMIT was a 50x over-fetch (2026-09-10)

`SEARCH_GLOB_RANK_LIMIT` (`types.ts`) governed how many glob hits are fetched
per request; the RENDERED list has been capped at `SEARCH_RESULT_CAP` (100,
`capHits`) since the change noted above ("Pattern search now caps at 100
rendered rows"), which left the 5,000-row fetch limit fetching 50x what the
list ever shows. Reduced to 1,000 — 10x the display cap, still comfortably
past it so the count chip can report a true, un-truncated total for any glob
that does not itself run past a thousand hits, without carrying an order of
magnitude more rows over the wire than anything past row 100 is ever acted
on.

The chip's "N" vs "N+" distinction (`resultCountLabel`, `result-cap.ts`) is
driven by the server's own `truncated` flag and the actual `total` returned,
neither of which reads the limit constant directly — the distinction stays
honest at any fetch limit by construction, so no test pins the literal
value and none was added for this change.

**Verification**: `bun test src/apps/explorer` (1145 pass, 0 fail) and
`bunx tsc --noEmit` (clean).

## The offer row wraps instead of clipping at a narrow pane (2026-09-10)

`table.listing-table td` sets `white-space: nowrap` for every cell, right for
a single filename but wrong for the offer's longer sentence: at a narrow
explorer-pane width the row ellipsized instead of wrapping onto a second
line, clipping the very offer meant to get the user out of a zero-hit dead
end. Added `table.listing-table td.status-message { white-space: normal; }`
in `explorer.css`, scoped to the status-message row shape shared by
"Searching…", the capped-away count, and this offer — not to real listing
rows, which keep the single-line ellipsis they need.

**TDD**: `zero-match-offer-wrap.test.ts` reads the built CSS and asserts the
`white-space: normal` rule exists on `table.listing-table td.status-message`
— same rules-extraction technique as `search-mode-chip.test.ts`.

**Verification**: visually confirmed at 500px and a genuinely narrow 300px
pane width (`agent-browser`, screenshots in the session scratchpad) — the
row wraps onto multiple lines rather than clipping. `bun test
src/apps/explorer` (1146 pass, 0 fail) and `bunx tsc --noEmit` (clean).

## Offer row copy: state-and-pattern only, no echoed query or rung label (2026-09-10)

The row read the original query back (`No matches for {q}.`) and named which
rung it would run (`{label}: search {pattern} instead`) — accurate, but long
enough at a narrow pane to be exactly what the wrap fix above has to catch.
Shortened to "No matches for given search term. Widen instead to: {pattern}"
— the query is never echoed (nothing here needs it: the user just typed it),
and the rung's label is not rendered, only its resulting pattern.

The named, ordered rung list in `glob-broaden.ts` (`RUNGS`, each carrying a
`label`) is unchanged — labels still exist there and still drive which
pattern is offered and in what order, so a future rung is still a single
record appended to that list. They simply are not surfaced in this row
anymore; `broadenOffer.label` is computed but no longer rendered.

**Verification**: `bun test src/apps/explorer` (1146 pass, 0 fail) and
`bunx tsc --noEmit` (clean).
