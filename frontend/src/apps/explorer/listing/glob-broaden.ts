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

// Walks the ladder in order and returns the first rung that actually
// widens the query — never one that would rerun a semantically identical,
// still-zero-hit search. Null when no rung applies, meaning there is
// nothing left to offer.
export function broadenGlobOffer(query: string): BroadenOffer | null {
  if (!query.includes("*")) return null;
  if (alreadyMaximallyBroadOnDepth(query)) return null;
  for (const rung of RUNGS) {
    const pattern = rung.widen(query);
    if (pattern !== null && pattern !== query) return { pattern, label: rung.label };
  }
  return null;
}

// Convenience for a caller that only needs the resulting pattern text, not
// which rung produced it.
export function broadenGlobPattern(query: string): string | null {
  return broadenGlobOffer(query)?.pattern ?? null;
}
