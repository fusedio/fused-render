// SPEC-omnibox-search-affordance.md correction (2026-09-10): the field
// always arrives pre-filled with the folder currently being searched
// (SearchField.tsx's own `onFocus`: `setQuery(contractHome(fsPath, home))`
// on an empty box) — a hard user requirement, not a default worth changing.
// "Has the user actually typed something" is therefore NOT `query === ""`;
// an untouched, pre-filled box reads as edited by that test even though
// nothing has been typed into it at all.
//
// `isPristineQuery` is the one place that distinction is made: empty, OR
// still exactly the folder the box pre-filled itself with — in EITHER
// notation `contractHome` can produce (a leading "~" under home, or the
// plain absolute path otherwise), and tolerant of a trailing slash on
// either side (a real folder's own path never carries one, but a query
// typed by hand might). Deliberately a full-string comparison after
// stripping that one trailing slash, not a prefix test — "/Users/iamsdas2"
// typed into a box opened at "/Users/iamsdas" is an EDIT, not the same
// folder wearing an extra character, and a prefix match would blur the two.
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
