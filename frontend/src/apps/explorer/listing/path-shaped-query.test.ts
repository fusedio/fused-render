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

  test("a relative path with a slash is still path-shaped", () => {
    expect(isPathShapedQuery("sub/dir", OPEN, HOME)).toBe(true);
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
