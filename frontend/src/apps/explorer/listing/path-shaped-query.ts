// Which mode the field's chip shows, and whether the query is even worth
// asking the index about — the SAME predicate answers both (SearchField.tsx's
// `chipIsSearch`, useListingSearch.ts's own request gate), so the chip can
// never show "Path" over a box that quietly ran a search anyway.
//
// A query is "Path" only when it is ABSOLUTE-ISH shaped (a leading "~", a
// leading "/", or a Windows drive prefix) AND carries no glob. This is
// NARROWER than `listingAddress`'s own null/non-null split (decision 5's
// shape gate, still the right resolver for `useTypedPathAddress` and
// `completionTarget` — those need to know what to stat/list for ANY
// slash-bearing query, relative included) — `listingAddress` returns
// non-null for a bare relative query like "src/util" too, but the user's own
// rule was scoped to absolute paths specifically ("non patterned absolute
// paths should not be considered as [search]" / "any search on absolute path
// without pattern is useless" — DECISIONS.md). A relative slash-bearing query
// was never in scope: it has to keep live-filtering the subtree exactly as
// it did before this predicate existed, because the completion dropdown that
// justifies suppressing the search (see below) is prefix-only and
// non-recursive within one directory (`useCompletion.ts`'s `listDir`) — it
// cannot answer "src/2024" the way a live subtree search matching
// `report-2024.md` anywhere under `src/` can.
//
// The absolute-ish shape test is `escapesBase` (query-base.ts's own
// syntactic "does this query's base differ from the box root" predicate,
// already exactly: leading "~"/"~/", leading "/", a drive prefix, or a ".."
// segment) rather than a fourth hand-rolled shape test — reused deliberately,
// not just for economy: `escapesBase` already gates Enter-required commit
// (decision 4) for exactly the same queries, and reusing it means "reads as
// Path" and "requires Enter before searching" can never drift out of sync,
// which two independently-written shape tests eventually would. Its one
// broader case than the three named here — a ".." segment with no leading
// "~"/"/" (e.g. "../sibling") — is not something finding 2 asked for
// specifically, but it is the same kind of case: the query's base is not the
// box root, shape alone cannot tell whether it's a real path, and the
// completion dropdown's reasoning for suppressing the search applies to it
// exactly as much as to a leading "/".
//
// A glob ("*" anywhere in the query) is excluded via `listingAddress` —
// it returns null for one, the same way the server's own `resolve_query`
// treats "*" as the one glob trigger (`is_glob = "*" in raw`,
// fused_render/index/query.py) — so this needs no glob check of its own.
//
// This does NOT verify the address exists. An earlier version of this
// predicate did (an async `statPath`, debounced and cached) — dropped once
// the actual complaint surfaced: the completion dropdown (`completionTarget`,
// `useCompletion.ts`) already lists and resolves a path-shaped query on every
// keystroke, and Enter already accepts a row from it, so a match-count search
// running BEHIND the same text only ever re-announced what the dropdown had
// already shown. Existence is also unknowable synchronously, and this
// predicate has to answer the same tick the query changes: a typed prefix of
// a real path ("~/Work/agent-skills/u", nothing by that exact name yet)
// reads as "Path" the same as the folder that already exists, because shape
// is the only thing left to ask.
import { escapesBase } from "@apps/explorer/listing/query-base";
import { listingAddress } from "@apps/explorer/listing/listing-address";

export function isPathShapedQuery(
  query: string,
  fsPath: string,
  home: string | undefined,
): boolean {
  const raw = query.trim();
  return escapesBase(raw) && listingAddress(query, fsPath, home) !== null;
}
