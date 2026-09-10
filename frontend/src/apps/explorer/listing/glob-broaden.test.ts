// broadenGlobPattern's own contract: derive the recursively-broadened form of
// a glob straight from the query text, with no special-cased examples.
import { describe, expect, test } from "bun:test";
import { broadenGlobPattern } from "@apps/explorer/listing/glob-broaden";

describe("broadenGlobPattern", () => {
  test("a shallow, slash-bearing pattern gets a recursive segment inserted before its last component", () => {
    expect(broadenGlobPattern("/home/iamsdas/*.js")).toBe("/home/iamsdas/**/*.js");
  });

  test("a relative two-segment pattern is widened the same way", () => {
    expect(broadenGlobPattern("*/*.json")).toBe("*/**/*.json");
  });

  test("a depth-1 anchor at the box root (leading slash, no further segment) widens too", () => {
    expect(broadenGlobPattern("/*.csv")).toBe("/**/*.csv");
  });

  test("already recursively broadened (an explicit **/ before the last segment) has nothing left to offer", () => {
    expect(broadenGlobPattern("/home/iamsdas/**/*.js")).toBeNull();
  });

  test("a bare, slash-free glob is already maximally broad (the server's own implicit **/ prefix already covers every depth)", () => {
    expect(broadenGlobPattern("*.js")).toBeNull();
    expect(broadenGlobPattern("a*b")).toBeNull();
  });

  test("not a glob at all — no widening exists to offer", () => {
    expect(broadenGlobPattern("readme")).toBeNull();
    expect(broadenGlobPattern("/home/iamsdas/readme")).toBeNull();
  });

  test("empty query — nothing to widen", () => {
    expect(broadenGlobPattern("")).toBeNull();
  });
});
