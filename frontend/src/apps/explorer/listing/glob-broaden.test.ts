// broadenGlobOffer's contract: an ordered ladder of NAMED, deliberate
// widenings, each checked for whether it actually widens the query before
// being offered. Never a chain of string mutations tried until one sticks —
// each rung is verifiable on its own, and the winner is the first one whose
// pattern differs from the query.
import { describe, expect, test } from "bun:test";
import { broadenGlobOffer, broadenGlobPattern } from "@apps/explorer/listing/glob-broaden";

describe("broadenGlobOffer", () => {
  // Follow-up (search-trailing-space, A3/A5): "widen the name" (the former
  // rung 1) is deleted — `expand_whitespace_query`/`expandWhitespaceQuery`
  // now wraps a glob query's unstarred final segment in the CROSS-DIRECTORY
  // "**" token, so the resolved pattern for "/home/iamsdas/*.js" is already
  // "/home/iamsdas/*.js**" before any widening. Appending a single user "*"
  // to the raw text (what that rung used to do) would make the segment
  // already-starred and SUPPRESS that "**" wrap, trading it for a
  // single-segment-confined star — narrower, not wider. There is only one
  // rung left: "look in subfolders".
  test("look in subfolders", () => {
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

  test("still offers the subfolder rung when the query already ends in *", () => {
    const offer = broadenGlobOffer("/home/iamsdas/*.js*");
    expect(offer).toEqual({
      pattern: "/home/iamsdas/**/*.js*",
      label: "Look in subfolders",
    });
  });

  test("null when the subfolder rung is already maximally broad", () => {
    expect(broadenGlobOffer("/home/iamsdas/**/*.js*")).toBeNull();
  });

  test("already recursively broadened (an explicit **/ before the last segment) has nothing left to offer", () => {
    expect(broadenGlobOffer("/home/iamsdas/**/*.js")).toBeNull();
  });

  test("a bare, slash-free glob has nothing left to offer", () => {
    // Slash-free is already maximally broad on the SUBFOLDER dimension
    // (resolve_query's own implicit **/ prefix already covers every depth),
    // and the NAME dimension's trailing edge is already at its broadest too:
    // `expandWhitespaceQuery("*.js")` already resolves to "*.js**" (the
    // cross-directory token) — there is no rung left that could widen this
    // any further, and no "/" for the subfolder rung to work with either.
    // This is the accepted precision-glob-loss consequence (DECISIONS.md),
    // not a bug: a bare `*.js` search already matches ".jsx"/".json"
    // everywhere.
    expect(broadenGlobOffer("*.js")).toBeNull();
  });

  test("a trailing-space query offers nothing (A5/A6: the former name-widening rung is gone)", () => {
    // The bug this used to trigger: the deleted rung appended a literal "*"
    // to the raw text, so "*a* " (trailing space) displayed the confusing
    // offer text "*a* *". With that rung removed entirely there is nothing
    // left to offer for a slash-free query — correct, since
    // `expandWhitespaceQuery("*a* ")` already resolves to its broadest form.
    expect(broadenGlobOffer("*a* ")).toBeNull();
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

  test("null when there is no subfolder rung candidate and no other rung left", () => {
    expect(broadenGlobPattern("*.js")).toBeNull();
  });
});
