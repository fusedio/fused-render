// The focused teaching panel — this surface's own placeholder guidance,
// with the one thing a single placeholder line cannot teach: the contrast
// between a bounded glob and an unbounded one.
//
// SPEC-omnibox-search-affordance.md correction (2026-09-10): this used to
// gate on `query === ""`, back when an empty box was the only state a user
// staring at the placeholder could be in. It no longer is — the field
// always arrives pre-filled with the folder currently being searched (a
// hard requirement), so on focus the query is essentially NEVER empty, and
// this panel practically never rendered at all. `pristine` (query-
// pristine.ts's `isPristineQuery`) is the fix: empty OR still exactly the
// untouched pre-filled path, which is the box's actual resting state.
//
// ITEM 8 (running-screen review, 2026-09-10) DELETED this file's own
// `buildSearchExamples` (and its `SearchExample`/`ExampleEntry` types): the
// panel it fed showed three derived, pressable example rows ("*.zip",
// ".zip", "~/*/*.zip") built from the folder's own most-common extension.
// Having seen it on a running screen, the user asked for an explanation
// only, not a set of rows to press — SearchField.tsx's own panel now
// renders three sentences of prose instead (bare text searches deep, a `*`
// pattern stays shallow, a leading `~/`/`/` starts elsewhere — the exact
// inversion the three example rows existed to teach, kept as words rather
// than as three derived patterns). That prose needs no folder-specific
// derivation, so the `entries` prop threading this function existed for
// (SearchFieldProps.entries, Listing.tsx's own `sortedEntries` pass,
// FileSearchField.tsx's `entries={[]}`) went with it — grep `tests/` before
// reintroducing any of this: nothing there referenced these symbols at the
// time of removal (confirmed), but a literal-frontend-source-line Python
// test elsewhere in this repo would not show up in `bun test` staying
// green.
//
// `showSearchExamples` below is UNCHANGED IN SHAPE by this (still `active &&
// <gate>`), but the gate it's handed is no longer `pristine`
// (query-pristine.ts's `isPristineQuery`, which also treats an empty box
// and a trailing-slash-decorated one as pristine). The user's own follow-up
// request (2026-09-10) is narrower than that: the panel is teaching about
// THE CWD specifically, so it must show only while the box still holds that
// untouched value and vanish on the very first keystroke — a bare space, a
// trailing "/", anything. SearchField.tsx now passes `isUntouchedCwdQuery`
// (query-pristine.ts) here instead — see that file's own comment for why
// there are two predicates rather than one. The parameter below stays
// generic (`untouchedCwd`, not `pristine`) precisely so this function
// doesn't silently claim a stake in which one is correct; it only decides
// WHEN the panel appears given whatever the caller decides "untouched"
// means, not what it contains.

/** Whether the examples panel should occupy the dropdown's surface.
 *
 * An untouched-cwd query resolves to a real, existing folder, so the
 * completion dropdown could legitimately have something to show for it too
 * — but that folder's own children are already listed in the rows directly
 * below, so duplicating that listing while teaching nothing is the worse
 * trade: the untouched cwd WINS the surface over completions, the opposite
 * of the old empty-query precedence (decided deliberately — do not
 * re-litigate). This function only ever answers for ITS OWN side of that
 * trade; the other half — completions never showing over an untouched cwd —
 * is SearchField.tsx's own `showCompletion` computation excluding
 * `pristine` (the broader predicate, on purpose — see query-pristine.ts)
 * before this function is ever asked, which is what keeps the two from
 * ever both being true rather than this function guessing at it. */
export function showSearchExamples(fieldActive: boolean, untouchedCwd: boolean): boolean {
  return fieldActive && untouchedCwd;
}
