// Which mode the field's chip shows, and whether the query is even worth
// asking the index about — the SAME predicate answers both (SearchField.tsx's
// `chipIsSearch`, useListingSearch.ts's own request gate), so the chip can
// never show "Path" over a box that quietly ran a search anyway.
//
// A query is "Path" the moment it is PATH-SHAPED and carries no glob: exactly
// `listingAddress`'s own null/non-null split (decision 5's cheap shape gate,
// the same one `useTypedPathAddress` and `completionTarget` already use to
// decide what to stat and what to list). A glob ("*" anywhere in the query)
// is excluded there already — `listingAddress` returns null for one, the same
// way the server's own `resolve_query` treats "*" as the one glob trigger
// (`is_glob = "*" in raw`, fused_render/index/query.py) — so this needs no
// glob check of its own.
//
// This does NOT verify the address exists. An earlier version of this
// predicate did (an async `statPath`, debounced and cached) — dropped once
// the actual complaint surfaced: the completion dropdown (`completionTarget`,
// `useCompletion.ts`) already lists and resolves a path-shaped query on every
// keystroke, and Enter already accepts a row from it, so a match-count search
// running BEHIND the same text only ever re-announced what the dropdown had
// already shown ("any search on an absolute path without a pattern is
// useless" — see DECISIONS.md). Existence is also unknowable synchronously,
// and this predicate has to answer the same tick the query changes: a typed
// prefix of a real path ("~/Work/agent-skills/u", nothing by that exact name
// yet) reads as "Path" the same as the folder that already exists, because
// shape is the only thing left to ask.
import { listingAddress } from "@apps/explorer/listing/listing-address";

export function isPathShapedQuery(
  query: string,
  fsPath: string,
  home: string | undefined,
): boolean {
  return listingAddress(query, fsPath, home) !== null;
}
