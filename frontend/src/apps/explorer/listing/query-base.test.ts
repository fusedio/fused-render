import { describe, expect, test } from "bun:test";
import { escapesBase, escapesFsPath } from "@apps/explorer/listing/query-base";

describe("escapesBase", () => {
  test.each([
    ["*/*.json"],
    ["data/2024"],
    [".csv"],
    ["**/*.csv"],
    ["readme"],
    ["..config"],
    ["a..b/c"],
  ])("%s does not escape the box root", (query) => {
    expect(escapesBase(query)).toBe(false);
  });

  test.each([
    ["~"],
    ["~/Work"],
    ["~/a/*/b.csv"],
    ["/tmp"],
    ["/tmp/abc.txt"],
    ["C:/x"],
    ["C:\\x"],
    ["../sibling/*.json"],
  ])("%s escapes the box root", (query) => {
    expect(escapesBase(query)).toBe(true);
  });
});

// SPEC-omnibox-search-affordance.md correction (2026-09-10, defect 2): an
// absolute/tilde query anchored INSIDE the folder currently being searched
// must not gate — same base, same scope as the equivalent relative query.
describe("escapesFsPath", () => {
  const FS_PATH = "/Users/iamsdas";
  const HOME = "/Users/iamsdas";

  test.each([
    ["*/*.json"], // relative — escapesBase itself already says no
    ["data/2024"],
  ])("%s never escapes (escapesBase already says no)", (query) => {
    expect(escapesFsPath(query, FS_PATH, HOME)).toBe(false);
  });

  test.each([
    ["/Users/iamsdas/*/*.json"],
    ["/Users/iamsdas/*/*.json/"],
    ["~/*/*.json"],
    // Exact-folder edges: a full, glob-free address naming fsPath itself.
    ["/Users/iamsdas"],
    ["/Users/iamsdas/"],
  ])("%s is anchored inside the folder being searched — not an escape", (query) => {
    expect(escapesFsPath(query, FS_PATH, HOME)).toBe(false);
  });

  test.each([
    ["/etc/*/x.conf"],
    ["/Users/iamsdas2"], // segment comparison, not a string prefix
    ["/Users/iamsdas2/*.json"],
    ["/Users"], // an ancestor of fsPath is still a different (bigger) scope
    ["../sibling/*.json"],
  ])("%s names a genuinely different subtree — stays gated", (query) => {
    expect(escapesFsPath(query, FS_PATH, HOME)).toBe(true);
  });

  test("a tilde query gates while home is still unresolved — nothing to compare against yet", () => {
    expect(escapesFsPath("~/*/*.json", FS_PATH, undefined)).toBe(true);
  });
});
