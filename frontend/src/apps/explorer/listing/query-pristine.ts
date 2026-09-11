// SPEC-omnibox-search-affordance.md correction (2026-09-10): the field
// always arrives pre-filled with the folder currently being searched
// (SearchField.tsx's own `onFocus`: `setQuery(contractHome(fsPath, home))`
// on an empty box) — a hard user requirement, not a default worth changing.
// "Has the user actually typed something" is therefore NOT `query === ""`;
// an untouched, pre-filled box reads as edited by that test even though
// nothing has been typed into it at all.
//
// `isPristineQuery` is what every search-gating consumer uses (the
// not-found/offer row in search-action-rows.ts, the completion exclusion
// and `searchAffordance` call in SearchField.tsx, FileSearchField.tsx's
// navigation-effect guard): empty, OR still exactly the folder the box
// pre-filled itself with — in EITHER notation `contractHome` can produce,
// and tolerant of a trailing slash on either side and of surrounding
// whitespace. Those consumers need "nothing to search for yet" (an empty
// box included) more than they need "not one character has landed" — an
// empty box must not run a search or offer to search for nothing, and a
// query that manually re-adds the folder's own trailing slash is still
// naming the same folder, not a different query, for their purposes.
import { contractHome } from "@apps/explorer/listing/home-path";

function stripTrailingSlash(s: string): string {
  const stripped = s.replace(/\/+$/, "");
  return stripped === "" ? s : stripped;
}

export function isPristineQuery(
  query: string,
  fsPath: string,
  home: string | undefined,
): boolean {
  const trimmed = query.trim();
  if (trimmed === "") return true;
  const typed = stripTrailingSlash(trimmed);
  return typed === stripTrailingSlash(fsPath) || typed === stripTrailingSlash(contractHome(fsPath, home));
}
