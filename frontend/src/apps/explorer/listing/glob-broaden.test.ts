// broadenGlobOffer's contract: an ordered ladder of NAMED, deliberate
// widenings, each checked for whether it actually widens the query before
// being offered. Never a chain of string mutations tried until one sticks —
// each rung is verifiable on its own, and the winner is the first one whose
// pattern differs from the query.
import { describe, expect, test } from "bun:test";
import { broadenGlobOffer, broadenGlobPattern } from "@apps/explorer/listing/glob-broaden";

describe("broadenGlobOffer", () => {
  test("widens the name first: a trailing * is appended when the query lacks one", () => {
    const offer = broadenGlobOffer("/home/iamsdas/*.js");
    expect(offer).toEqual({ pattern: "/home/iamsdas/*.js*", label: "Widen the name" });
  });

  test("a relative two-segment pattern is widened the same way", () => {
    expect(broadenGlobOffer("*/*.json")).toEqual({
      pattern: "*/*.json*",
      label: "Widen the name",
    });
  });

  test("a depth-1 anchor at the box root (leading slash, no further segment) widens by name too", () => {
    expect(broadenGlobOffer("/*.csv")).toEqual({ pattern: "/*.csv*", label: "Widen the name" });
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

  test("a bare, slash-free glob still gets name-widened", () => {
    // Slash-free is already maximally broad on the SUBFOLDER dimension
    // (resolve_query's own implicit **/ prefix already covers every depth),
    // but the NAME dimension is untouched — catching ".jsx"/".json" here is
    // still a genuine widen.
    expect(broadenGlobOffer("*.js")).toEqual({ pattern: "*.js*", label: "Widen the name" });
  });

  test("not a glob at all — no widening exists to offer", () => {
    expect(broadenGlobOffer("readme")).toBeNull();
    expect(broadenGlobOffer("/home/iamsdas/readme")).toBeNull();
  });

  test("empty query — nothing to widen", () => {
    expect(broadenGlobOffer("")).toBeNull();
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
    expect(broadenGlobPattern("/home/iamsdas/*.js")).toBe("/home/iamsdas/*.js*");
  });

  test("null when the ladder offers nothing", () => {
    expect(broadenGlobPattern("readme")).toBeNull();
  });
});
