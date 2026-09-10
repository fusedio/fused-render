// SPEC-omnibox-search-affordance.md correction (2026-09-10): the field
// always arrives pre-filled with the folder currently being searched
// (SearchField.tsx's own `onFocus`: `setQuery(contractHome(fsPath, home))`
// on an empty box) — a hard user requirement, not a default worth changing.
// "Has the user actually typed something" is therefore NOT `query === ""`;
// an untouched, pre-filled box reads as edited by that test even though
// nothing has been typed into it at all.
//
// This file exports TWO predicates over the same idea, deliberately not
// one, because "nothing has been typed yet" and "the panel may still show"
// turned out to be different questions once the teaching panel got its own
// requirement (2026-09-10): remove it the INSTANT the user types anything,
// including a trailing "/" appended to the pre-filled path, or a lone space.
//
// `isPristineQuery` — the ORIGINAL, still the one search-gating consumers
// use (the not-found/offer row in search-action-rows.ts, the completion
// exclusion and `searchAffordance` call in SearchField.tsx,
// FileSearchField.tsx's navigation-effect guard): empty, OR still exactly
// the folder the box pre-filled itself with — in EITHER notation
// `contractHome` can produce, and tolerant of a trailing slash on either
// side and of surrounding whitespace. Those consumers need "nothing to
// search for yet" (an empty box included) more than they need "not one
// character has landed" — an empty box must not run a search or offer to
// search for nothing, and a query that manually re-adds the folder's own
// trailing slash is still naming the same folder, not a different query,
// for their purposes.
//
// `isUntouchedCwdQuery` — NARROWER, for the teaching panel ONLY
// (SearchField.tsx's `showExamples` gate). No empty case (an emptied box is
// an edit, not "the cwd"), no trailing-slash tolerance, no trim: the panel
// must disappear on the very first keystroke of ANY kind — a bare space, a
// trailing "/" appended while extending a path, or a real character — so
// this checks the query against the pre-filled value byte-for-byte, in
// either notation `contractHome` can produce.
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

/** True only while the query is BYTE-IDENTICAL to the value the box
 * pre-filled itself with on focus — no empty case, no trailing-slash
 * tolerance, no trim. Gates the teaching panel: "the box still holds the
 * untouched cwd", not "there is nothing to search for" (that's
 * `isPristineQuery`, above — a different question with different
 * consumers). The first keystroke of any kind, including one that merely
 * appends a "/" or a space, must make this false. */
export function isUntouchedCwdQuery(
  query: string,
  fsPath: string,
  home: string | undefined,
): boolean {
  return query === fsPath || query === contractHome(fsPath, home);
}
