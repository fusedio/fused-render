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
export interface SearchExample {
  pattern: string;
  hint: string;
}

export const SEARCH_EXAMPLES: SearchExample[] = [
  { pattern: "*.csv", hint: "CSV files in this folder" },
  { pattern: ".csv", hint: "CSV files in this folder and everything below it" },
  { pattern: "~/work/*/*.csv", hint: "searches from ~/work instead of here" },
];

/** Whether the examples panel should occupy the dropdown's surface.
 *
 * A pristine query resolves to a real, existing folder, so the completion
 * dropdown could legitimately have something to show for it too — but
 * that folder's own children are already listed in the rows directly
 * below, so duplicating that listing while teaching nothing is the worse
 * trade: PRISTINE WINS the surface over completions now, the opposite of
 * the old empty-query precedence (decided deliberately — do not
 * re-litigate). This function only ever answers for ITS OWN side of that
 * trade; the other half — completions never showing while pristine — is
 * SearchField.tsx's own `showCompletion` computation excluding `pristine`
 * before this function is ever asked, which is what keeps the two from
 * ever both being true rather than this function guessing at it. */
export function showSearchExamples(fieldActive: boolean, pristine: boolean): boolean {
  return fieldActive && pristine;
}
