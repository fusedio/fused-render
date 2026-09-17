// broadenGlobOffer's contract: an ordered ladder of NAMED, deliberate
// widenings, each checked for whether it actually widens the query before
// being offered. Never a chain of string mutations tried until one sticks —
// each rung is verifiable on its own, and the winner is the first one whose
// pattern differs from the query.
import { describe, expect, test } from "bun:test";
import { broadenGlobOffer, broadenGlobPattern } from "@apps/explorer/listing/glob-broaden";

describe("broadenGlobOffer", () => {
  // Follow-up (search-trailing-space): `expand_whitespace_query`/
  // `expandWhitespaceQuery` now wrap a glob query's final segment in a
  // trailing "*" whenever it doesn't already have one — so the RESOLVED
  // pattern for "/home/iamsdas/*.js" was ALREADY "/home/iamsdas/*.js*"
  // before any widening. Rung 1 ("widen the name") appending that same
  // trailing "*" to the raw text resolves to the identical pattern — not a
  // genuine widen — so the ladder now falls straight through to rung 2 for
  // every one of these. See glob-broaden.ts's `genuinelyWidens`.
  test("rung 1 is a no-op once the trailing * is already implied; falls through to subfolders", () => {
    const offer = broadenGlobOffer("/home/iamsdas/*.js");
    expect(offer).toEqual({ pattern: "/home/iamsdas/**/*.js", label: "Look in subfolders" });
  });

  test("a relative two-segment pattern falls through to subfolders the same way", () => {
    expect(broadenGlobOffer("*/*.json")).toEqual({
      pattern: "*/**/*.json",
      label: "Look in subfolders",
    });
  });

  test("a depth-1 anchor at the box root (leading slash, no further segment) falls through too", () => {
    expect(broadenGlobOffer("/*.csv")).toEqual({
      pattern: "/**/*.csv",
      label: "Look in subfolders",
    });
  });

  test("looks in subfolders next: offered once the query already ends in *", () => {
    // Name-widening is a no-op here (appending another "*" reruns an
    // identical search), so the ladder falls through to the recursive rung.
    const offer = broadenGlobOffer("/home/iamsdas/*.js*");
    expect(offer).toEqual({
      pattern: "/home/iamsdas/**/*.js*",
      label: "Look in subfolders",
    });
  });

  test("null when neither rung widens anything", () => {
    // Already widened by name AND by subfolder — nothing left on the ladder.
    expect(broadenGlobOffer("/home/iamsdas/**/*.js*")).toBeNull();
  });

  test("already recursively broadened (an explicit **/ before the last segment) has nothing left to offer", () => {
    // Covers name-widening too: appending "*" here isn't a no-op string-wise,
    // but the pattern is already maximally broad on both dimensions, so the
    // ladder offers nothing rather than a technically-different pattern that
    // is not what the user came here to widen.
    expect(broadenGlobOffer("/home/iamsdas/**/*.js")).toBeNull();
  });

  test("a bare, slash-free glob has nothing left to offer", () => {
    // Slash-free is already maximally broad on the SUBFOLDER dimension
    // (resolve_query's own implicit **/ prefix already covers every depth),
    // and — as of the search-trailing-space follow-up — the NAME dimension
    // is now ALSO already at its broadest: `expandWhitespaceQuery("*.js")`
    // already resolves to "*.js*", so rung 1 offering to append that same
    // "*" is not a genuine widen, and there is no "/" for rung 2 to work
    // with either. This is the accepted precision-glob-loss consequence
    // (DECISIONS.md), not a bug: a bare `*.js` search already matches
    // ".jsx"/".json" everywhere, so there is nothing broader left to offer.
    expect(broadenGlobOffer("*.js")).toBeNull();
  });

  test("not a glob at all — no widening exists to offer", () => {
    expect(broadenGlobOffer("readme")).toBeNull();
    expect(broadenGlobOffer("/home/iamsdas/readme")).toBeNull();
  });

  test("empty query — nothing to widen", () => {
    expect(broadenGlobOffer("")).toBeNull();
  });

  // -- SPEC-search-space-wildcard.md: whitespace is an implied wildcard, so
  // a query with no literal "*" at all can still be running in glob mode
  // server-side. But expand_whitespace_query already wraps the final
  // segment in a leading AND trailing "*" whenever it has no user-typed "*"
  // of its own — appending a trailing "*" to the raw query text (rung 1)
  // gives that segment a user-typed "*", which SUPPRESSES the implied
  // leading "*" the original zero-hit search already had. The "widened"
  // query would therefore be a strict subset of the original, guaranteed to
  // also return zero hits. So a pure-whitespace query (no literal "*") gets
  // no offer at all — it is already at its broadest expressible form.
  test("a whitespace query with no literal * has nothing left to offer", () => {
    expect(broadenGlobOffer("hello world")).toBeNull();
  });

  // -- the finding-3 shapes: a pattern already maximally broad on the
  // subfolder dimension, whose LAST segment (rather than the one before it)
  // is what says so -----------------------------------------------------
  test("a trailing recursive segment is already maximally broad", () => {
    expect(broadenGlobOffer("/home/x/**")).toBeNull();
  });

  test("a trailing slash (empty last segment) is already maximally broad", () => {
    expect(broadenGlobOffer("/home/x/*/")).toBeNull();
  });
});

describe("broadenGlobPattern (pattern text only, for callers that don't need the label)", () => {
  test("returns the winning rung's pattern", () => {
    expect(broadenGlobPattern("/home/iamsdas/*.js")).toBe("/home/iamsdas/**/*.js");
  });

  test("null when the ladder offers nothing", () => {
    expect(broadenGlobPattern("readme")).toBeNull();
  });

  test("null when the ladder's only candidate resolves identically (search-trailing-space)", () => {
    expect(broadenGlobPattern("*.js")).toBeNull();
  });
});
