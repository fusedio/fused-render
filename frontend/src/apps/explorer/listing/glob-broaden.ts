// Widens a zero-hit PATTERN (glob) query along an ordered ladder of NAMED
// rungs — each a small, deliberate alternative with a reason a person would
// recognize, not a chain of string mutations tried until one sticks.
//
// The order is a decision, not an accident: "widen the name" comes before
// "look in subfolders" because it keeps the search in the folder the user
// was already looking at (see DECISIONS-omnibox-search-affordance.md for
// what changing that order would cost). Adding a future rung — case-
// insensitivity, say — means appending one more record to RUNGS below;
// nothing else here has to change.
//
// `resolve_query` (fused_render/index/query.py) already widens a slash-free
// glob for free: "*.js" resolves to "**/*.js" server-side, matching any
// depth, so the SUBFOLDER dimension is already maximally broad for a
// slash-free query — but the NAME dimension is untouched, and rung 1 still
// applies to it.
//
// Follow-up (search-trailing-space): `expand_whitespace_query` now wraps
// EVERY glob-mode query's final segment in a trailing "*" too, not only a
// whitespace-derived one (see home-search.ts's own doc comment) — so by the
// time a search is glob-mode and zero-hit, the ACTUAL pattern the server ran
// already ends in "*". Rung 1 ("append a trailing * to the raw query text")
// can therefore no longer be assumed to widen anything just because the RAW
// text doesn't end in "*" — the string comparison the ladder used to make
// its "genuinely widens" decision on has to run each candidate back through
// `expandWhitespaceQuery` and compare the RESOLVED patterns, not the raw
// query text, or it will offer a rerun that resolves to the exact same
// pattern that already returned zero hits (this exact bug, previously fixed
// for the whitespace-only case below, generalizes to every glob query now).

import { expandWhitespaceQuery } from "@apps/explorer/lib/home-search";

// One rung of the ladder: what a person would call this widening (shown
// beside the resulting pattern in the offer row), and how to derive the
// widened pattern — or null if this rung has nothing to say about the
// given query.
export interface BroadenRung {
  label: string;
  widen: (query: string) => string | null;
}

// Rung 1 — "widen the name": append a trailing "*" to the query's own text,
// so ".js" also catches ".jsx", ".json", ".js.map" in the same place the
// user already typed. Cheap, and reads like something a person would type
// themselves. Doesn't apply once the query already ends in "*" — appending
// another would rerun an identical search.
function widenName(query: string): string | null {
  if (query.endsWith("*")) return null;
  return `${query}*`;
}

// Rung 2 — "look in subfolders": insert the same "**/" `resolve_query` would
// already have added server-side for a slash-free glob, immediately before
// the query's own last segment, so the pattern reaches every depth under
// whatever base it already had: "/home/iamsdas/*.js" becomes
// "/home/iamsdas/**/*.js". Only reached once rung 1 doesn't apply.
function widenSubfolders(query: string): string | null {
  if (!query.includes("/")) return null;
  const segments = query.split("/");
  const last = segments[segments.length - 1];
  const head = segments.slice(0, -1).join("/");
  return `${head}/**/${last}`;
}

const RUNGS: BroadenRung[] = [
  { label: "Widen the name", widen: widenName },
  { label: "Look in subfolders", widen: widenSubfolders },
];

// A query is already maximally broad on the SUBFOLDER dimension when its
// last segment is already "**" (an explicit recursive tail), when a
// trailing slash leaves that segment empty, or when the segment BEFORE the
// last is already "**" (the widened form of itself). Any of these means
// inserting another "**/" would rerun an equivalent-or-narrower search that
// would still return zero hits — the finding-3 shapes. This gates the whole
// ladder, not just rung 2: a pattern already this broad has nothing left to
// offer on the subfolder dimension, and rung 1 alone is not worth a
// separate offer once the query is already at its recursive ceiling.
function alreadyMaximallyBroadOnDepth(query: string): boolean {
  if (!query.includes("/")) return false;
  const segments = query.split("/");
  const last = segments[segments.length - 1];
  const beforeLast = segments[segments.length - 2];
  return last === "**" || last === "" || beforeLast === "**";
}

export interface BroadenOffer {
  pattern: string;
  label: string;
}

// A whitespace-only query (no literal `*` typed at all) DOES settle in
// `mode: "glob"` server-side (SPEC-search-space-wildcard.md §1), but this
// ladder never gets to see it as a genuine widen candidate: it is gated out
// here, kept deliberately conservative rather than generalized to "would
// this resolve to glob mode" — a query with no literal `*` is left entirely
// to the substring-mode UI elsewhere. A query IS treated as glob-like once
// it carries a literal `*` anywhere.
function looksLikeGlob(query: string): boolean {
  return query.includes("*");
}

// Whether running `candidate` through the SAME transform the server applies
// (`expandWhitespaceQuery`, mirroring `expand_whitespace_query` in
// fused_render/index/query.py) would actually search something different
// from what `current` already searched and got zero hits for.
//
// This is required, not optional, now that `expandWhitespaceQuery` always
// wraps a glob-mode query's final segment in a trailing `*` (search-
// trailing-space follow-up): comparing the RAW rung output against the RAW
// query — this ladder's original check — used to be a safe proxy for "will
// this search something new" back when only a whitespace-derived query got
// an implied wildcard. It no longer is. `/home/x/*.js` and `/home/x/*.js*`
// are different raw strings but now resolve to the IDENTICAL server pattern
// ("/home/x/*.js*"), because the trailing `*` rung 1 would add was already
// implied. Comparing raw strings would offer that rerun anyway — a rerun
// guaranteed to return the same zero hits, exactly the bug a previous round
// hit for the whitespace-only case (see the `looksLikeGlob` doc comment
// above and DECISIONS.md) — so every rung's output is checked against the
// RESOLVED pattern here, not the raw text.
function genuinelyWidens(current: string, candidate: string): boolean {
  return expandWhitespaceQuery(candidate) !== expandWhitespaceQuery(current);
}

// Walks the ladder in order and returns the first rung that actually
// widens the query — never one that would rerun a semantically identical,
// still-zero-hit search. Null when no rung applies, meaning there is
// nothing left to offer.
export function broadenGlobOffer(query: string): BroadenOffer | null {
  if (!looksLikeGlob(query)) return null;
  if (alreadyMaximallyBroadOnDepth(query)) return null;
  for (const rung of RUNGS) {
    const pattern = rung.widen(query);
    if (pattern !== null && pattern !== query && genuinelyWidens(query, pattern)) {
      return { pattern, label: rung.label };
    }
  }
  return null;
}

// Convenience for a caller that only needs the resulting pattern text, not
// which rung produced it.
export function broadenGlobPattern(query: string): string | null {
  return broadenGlobOffer(query)?.pattern ?? null;
}
