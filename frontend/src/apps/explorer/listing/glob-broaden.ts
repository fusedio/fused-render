// Widens a zero-hit PATTERN (glob) query along an ordered ladder of NAMED
// rungs — each a small, deliberate alternative with a reason a person would
// recognize, not a chain of string mutations tried until one sticks.
//
// Adding a future rung means appending one more record to RUNGS below;
// nothing else here has to change.
//
// `resolve_query` (fused_render/index/query.py) already widens a slash-free
// glob for free: "*.js" resolves to "**/*.js*" server-side — the leading
// "**/" matches any depth, and the final segment's own trailing edge is
// already at ITS broadest too (a single, segment-confined "*" is exactly
// what an unstarred end gets) — so both the SUBFOLDER dimension and the NAME
// dimension's trailing edge are already maximally broad for a query that
// doesn't already end in "*" of its own.
//
// A former rung 1, "widen the name" (append a trailing "*" to the raw query
// text), lived here once. For a time (search-trailing-space round, A3,
// DECISIONS.md) `expand_whitespace_query`'s own final-segment trailing wrap
// used the cross-directory "**" token instead of a single "*", which made
// appending a user "*" actively BACKWARDS (it suppressed the wrap's own
// "**" for a narrower, single-segment star) rather than merely redundant —
// that version of this comment described that state. A later code-review
// round (worktree-search-trailing-space, finding 1) narrowed the trailing
// wrap back to a single "*" (a `**` there let a folder-anchored query like
// "/*.pdf" leak across a directory boundary it should not cross), which
// restores the ORIGINAL reason this rung is dead: appending a user "*" to
// an unstarred end and letting the wrap rule skip it produces the exact
// same resolved pattern the wrap would have produced unprompted — plain
// redundant, not narrowing. Either way the conclusion is the same and the
// rung stays deleted rather than "fixed forward": there is no text this
// ladder could still append to a name that `expand_whitespace_query` has
// not already appended an equally- (or, in the "**" era, more-)
// unrestricted version of itself. This is also what resolves the former
// rung 1's own display-text bug (a trailing-space query like "*a* "
// producing the raw offer text "*a* *", which read as if it inserted a
// second star beside the user's own) — the whole rung is gone, not
// patched.
//
// What remains, rung "look in subfolders", still has real work to do: the
// server's own "**/" prefix only fires for a query with NO "/" in it at
// all; a query that already names one folder level ("iamsdas/*.js") gets no
// such help and still benefits from inserting "**/" before its own final
// segment.

import { expandWhitespaceQuery } from "@apps/explorer/lib/home-search";

// One rung of the ladder: what a person would call this widening (shown
// beside the resulting pattern in the offer row), and how to derive the
// widened pattern — or null if this rung has nothing to say about the
// given query.
export interface BroadenRung {
  label: string;
  widen: (query: string) => string | null;
}

// "Look in subfolders": insert the same "**/" `resolve_query` would already
// have added server-side for a slash-free glob, immediately before the
// query's own last segment, so the pattern reaches every depth under
// whatever base it already had: "/home/iamsdas/*.js" becomes
// "/home/iamsdas/**/*.js".
function widenSubfolders(query: string): string | null {
  if (!query.includes("/")) return null;
  const segments = query.split("/");
  const last = segments[segments.length - 1];
  const head = segments.slice(0, -1).join("/");
  return `${head}/**/${last}`;
}

const RUNGS: BroadenRung[] = [{ label: "Look in subfolders", widen: widenSubfolders }];

// A query is already maximally broad on the SUBFOLDER dimension when its
// last segment is already "**" (an explicit recursive tail), when a
// trailing slash leaves that segment empty, or when the segment BEFORE the
// last is already "**" (the widened form of itself). Any of these means
// inserting another "**/" would rerun an equivalent-or-narrower search that
// would still return zero hits — the finding-3 shapes. This gates the whole
// ladder — there is currently only the one rung ("Look in subfolders"), and
// a pattern already this broad has nothing left to offer on the subfolder
// dimension, its only dimension.
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
// This is required, not optional: comparing the RAW rung output against the
// RAW query — this ladder's original check — is not a safe proxy for "will
// this search something new", because `expandWhitespaceQuery` (mirroring
// `expand_whitespace_query`, fused_render/index/query.py) can map two
// DIFFERENT raw strings onto the SAME resolved pattern, or (just as easily)
// leave two strings that merely LOOK closer together resolving to genuinely
// different patterns — the wrap only touches a query's final segment, and
// only when that segment does not already start or end with `*` (see its
// own `startsWith`/`endsWith` guards above). A former rung here ("widen the
// name": append a trailing `*` to the raw query) is exactly the case this
// check exists to catch — on a query whose final segment was already
// starred, that rung's raw output differed from the raw query yet resolved
// to an identical or narrower pattern (see the file header comment for why
// that rung was deleted rather than reprieved by this check alone) — so
// every rung's output is checked against the RESOLVED pattern here, not the
// raw text, even though only one rung remains today.
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
