// The focused-and-empty teaching panel: `showCompletion` (Listing.tsx)
// requires a non-null completion target AND at least one item, both of
// which an empty query always fails — so a field that is focused with
// nothing typed yet renders no dropdown at all, right where a user staring
// at the placeholder is already looking. These three rows fill that surface
// with the one thing the placeholder cannot teach in one line: the contrast
// between a bounded glob and an unbounded one.
export interface SearchExample {
  pattern: string;
  hint: string;
}

export const SEARCH_EXAMPLES: SearchExample[] = [
  { pattern: "*.csv", hint: "CSV files in this folder" },
  { pattern: ".csv", hint: "CSV files in this folder and everything below it" },
  { pattern: "~/work/*/*.csv", hint: "searches from ~/work instead of here" },
];

/** Whether the examples panel should occupy the dropdown's surface. Mutually
 * exclusive with `showCompletion` by construction — a non-empty query is
 * the only way `showCompletion` can be true, and this requires an empty
 * one — but `showCompletion` is still taken as an explicit input rather
 * than assumed, so a caller can never accidentally render both at once. */
export function showSearchExamples(
  fieldActive: boolean,
  query: string,
  showCompletion: boolean,
): boolean {
  return fieldActive && query === "" && !showCompletion;
}
