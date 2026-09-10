import { describe, expect, test } from "bun:test";
import { isPathShapedQuery } from "@apps/explorer/listing/path-shaped-query";

const HOME = "/Users/a";
const OPEN = "/Users/a/Fused/local/random";

describe("isPathShapedQuery", () => {
  test("the open folder itself is path-shaped", () => {
    expect(isPathShapedQuery("/Users/a/Fused/local/random", OPEN, HOME)).toBe(true);
    expect(isPathShapedQuery("~/Fused/local/random", OPEN, HOME)).toBe(true);
  });

  test("a partial path that resolves to no existing directory is still path-shaped — shape only, never existence", () => {
    expect(isPathShapedQuery("~/Work/agent-skills/u", OPEN, HOME)).toBe(true);
    expect(isPathShapedQuery("/Users/a/Fused/local/random/ico", OPEN, HOME)).toBe(true);
  });

  test("a glob is never path-shaped, even under a real folder", () => {
    expect(isPathShapedQuery("~/Work/*", OPEN, HOME)).toBe(false);
    expect(isPathShapedQuery("~/Fused/local/random/*.svg", OPEN, HOME)).toBe(false);
    expect(isPathShapedQuery("a/*.py", OPEN, HOME)).toBe(false);
  });

  test("a bare filter word is never path-shaped", () => {
    expect(isPathShapedQuery("random", OPEN, HOME)).toBe(false);
    expect(isPathShapedQuery("readme", OPEN, HOME)).toBe(false);
  });

  // Finding 2 (code review): the user's rule was scoped to ABSOLUTE paths
  // only ("any search on absolute path without pattern is useless") —
  // relative slash-bearing queries were never in scope and must keep
  // live-filtering the subtree exactly as they did before this predicate
  // existed. `listingAddress` alone can't tell the two apart (it resolves
  // both), so `isPathShapedQuery` narrows on top of it with `escapesBase`.
  test("a relative path with a slash is NOT path-shaped — it must keep live-filtering as a search", () => {
    expect(isPathShapedQuery("sub/dir", OPEN, HOME)).toBe(false);
    expect(isPathShapedQuery("src/util", OPEN, HOME)).toBe(false);
  });

  test("an absolute path is path-shaped", () => {
    expect(isPathShapedQuery("/Users/x/y", OPEN, HOME)).toBe(true);
  });

  test("a home-relative path, and bare ~, are path-shaped", () => {
    expect(isPathShapedQuery("~/Work/a", OPEN, HOME)).toBe(true);
    expect(isPathShapedQuery("~", OPEN, HOME)).toBe(true);
  });

  test("a glob under an absolute-ish base is still a search, never path-shaped", () => {
    expect(isPathShapedQuery("~/Work/*", OPEN, HOME)).toBe(false);
  });

  test("home undefined: a \"~\" query cannot resolve, so it is not path-shaped", () => {
    expect(isPathShapedQuery("~/Fused/local/random", OPEN, undefined)).toBe(false);
  });

  test("home undefined: the absolute spelling is still path-shaped", () => {
    expect(isPathShapedQuery("/Users/a/Fused/local/random", OPEN, undefined)).toBe(true);
  });

  test("a Windows drive-letter path is path-shaped", () => {
    expect(isPathShapedQuery("C:\\Users\\a", OPEN, HOME)).toBe(true);
  });
});
