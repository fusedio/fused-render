import { describe, expect, test } from "bun:test";
import { queryNamesOpenFolder } from "@apps/explorer/listing/query-current-folder";

const HOME = "/Users/a";
const OPEN = "/Users/a/Fused/local/random";

describe("queryNamesOpenFolder", () => {
  test("the open folder, absolute, matches", () => {
    expect(queryNamesOpenFolder("/Users/a/Fused/local/random", OPEN, HOME)).toBe(true);
  });

  test("the open folder, absolute with a trailing slash, matches", () => {
    expect(queryNamesOpenFolder("/Users/a/Fused/local/random/", OPEN, HOME)).toBe(true);
  });

  test("the open folder, \"~\"-contracted, matches", () => {
    expect(queryNamesOpenFolder("~/Fused/local/random", OPEN, HOME)).toBe(true);
  });

  test("the open folder, \"~\"-contracted with a trailing slash, matches", () => {
    expect(queryNamesOpenFolder("~/Fused/local/random/", OPEN, HOME)).toBe(true);
  });

  test("one more segment past the open folder is a real query, not a match", () => {
    expect(queryNamesOpenFolder("~/Fused/local/random/ico", OPEN, HOME)).toBe(false);
    expect(queryNamesOpenFolder("/Users/a/Fused/local/random/ico", OPEN, HOME)).toBe(false);
  });

  test("a glob under the open folder is a real query, not a match", () => {
    expect(queryNamesOpenFolder("~/Fused/local/random/*.svg", OPEN, HOME)).toBe(false);
  });

  test("a different absolute path does not match", () => {
    expect(queryNamesOpenFolder("/Users/a/Fused/local/other", OPEN, HOME)).toBe(false);
  });

  test("a different \"~\" path does not match", () => {
    expect(queryNamesOpenFolder("~/Fused/local/other", OPEN, HOME)).toBe(false);
  });

  test("a plain filter word never resolves to an address, so it never matches", () => {
    expect(queryNamesOpenFolder("random", OPEN, HOME)).toBe(false);
  });

  test("home undefined: the absolute spelling still matches", () => {
    expect(queryNamesOpenFolder("/Users/a/Fused/local/random", OPEN, undefined)).toBe(true);
  });

  test("home undefined: a \"~\" query cannot resolve, so it does not match", () => {
    expect(queryNamesOpenFolder("~/Fused/local/random", OPEN, undefined)).toBe(false);
  });
});
