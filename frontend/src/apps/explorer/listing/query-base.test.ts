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

  // FINDING 4 (code review, 2026-09-10): the server's own leading-slash rule
  // (resolve_query's docstring, fused_render/index/query.py) anchors these
  // two shapes at the box's own root unconditionally — the walk from "/"
  // never even advances past its first segment, filesystem or no filesystem
  // — so this predicate can match the server exactly here with no directory
  // check of its own.
  test.each([
    ["/*.csv"],
    ["/"],
  ])("%s never advances past root — the server's own depth-1/bare-root fallback, not an escape", (query) => {
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

  // FINDING (code review round 2, worktree-search-trailing-space): a plain
  // `.trim()` (the prior fix, once asserted here) is itself wrong. A
  // trailing space is not edge noise to the server — `expand_whitespace_
  // query` (fused_render/index/query.py) turns it into a wildcard on the
  // FINAL segment, which can peel that segment off the walked base
  // entirely. Verified live against `/api/index/rank`: `q=/Users/iamsdas%20`
  // (root `/Users/iamsdas`) answers `base: "/Users"`, not
  // `/Users/iamsdas` — a genuinely different (parent) subtree, so this
  // MUST gate, not stay silent.
  test("a trailing space on the exact open folder peels the last segment into a glob — gates (server walks only to the parent)", () => {
    expect(escapesFsPath("/Users/iamsdas ", FS_PATH, HOME)).toBe(true);
  });

  // A trailing slash (no space) carries no whitespace at all, so it is
  // untouched by `expand_whitespace_query` and stays exactly the same
  // folder — unaffected by the fix above.
  test.each([["/Users/iamsdas/"], [" /Users/iamsdas"]])(
    "%j (a trailing slash, or a leading space in front of the escape form) is still not an escape",
    (query) => {
      expect(escapesFsPath(query, FS_PATH, HOME)).toBe(false);
    },
  );

  test("a trailing space still gates a genuinely different subtree", () => {
    expect(escapesFsPath("/Users/iamsdas2 ", FS_PATH, HOME)).toBe(true);
  });

  // Verified live: `q=~%20` (root `/Users/iamsdas`) answers
  // `base: "/Users/iamsdas", pattern: "**/**~**"` — the box's own root, not
  // `home` — because the trailing space turns the whole "~" into a glob
  // token that no longer starts with a literal "~" at all. When `home` and
  // `fsPath` happen to be equal (as in every other test in this file) the
  // old trim-based code coincidentally answered `false` for the wrong
  // reason (it resolved "~" as a home path that happened to equal fsPath);
  // this fixture pins the case that tells the two apart.
  test('"~ " (tilde plus trailing space) is a current-folder glob, not a home escape — even when home is a different folder', () => {
    expect(escapesFsPath("~ ", "/Users/iamsdas/work", "/Users/iamsdas")).toBe(false);
  });

  test('"~ " (tilde plus trailing space) is not an escape when home equals fsPath either', () => {
    expect(escapesFsPath("~ ", FS_PATH, HOME)).toBe(false);
  });

  test("a plain in-folder query with no whitespace or glob is unaffected", () => {
    expect(escapesFsPath("report", FS_PATH, HOME)).toBe(false);
    expect(escapesFsPath("/Users/iamsdas/report", FS_PATH, HOME)).toBe(false);
  });
});
