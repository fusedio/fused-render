import { describe, expect, test } from "bun:test";
import { requestSearchFocus, subscribeSearchFocusRequest } from "@apps/explorer/listing/search-focus";

describe("requestSearchFocus", () => {
  test("a seed reaches listeners verbatim, under-home contraction included", () => {
    const seeds: string[] = [];
    const off = subscribeSearchFocusRequest((seed) => seeds.push(seed));
    try {
      requestSearchFocus("~/Downloads");
      expect(seeds).toEqual(["~/Downloads"]);
    } finally {
      off();
    }
  });

  test("a seed reaches listeners verbatim, not-under-home full path included", () => {
    const seeds: string[] = [];
    const off = subscribeSearchFocusRequest((seed) => seeds.push(seed));
    try {
      requestSearchFocus("/tmp/data");
      expect(seeds).toEqual(["/tmp/data"]);
    } finally {
      off();
    }
  });

  test("an unsubscribed listener is not notified", () => {
    const seeds: string[] = [];
    const off = subscribeSearchFocusRequest((seed) => seeds.push(seed));
    off();
    requestSearchFocus("~/Work");
    expect(seeds).toEqual([]);
  });
});

// Both the bar's click-to-edit and Ctrl/Cmd+L ask the merged search field to
// focus over a claimed folder, and both must hand it the same seed — a
// click that opens the field with a path and a shortcut that opens it empty
// (or vice versa) would make one gesture behave two different ways. Same
// text-scanning approach as search-bar-expand.test.ts: no DOM in this suite.
import { readFileSync } from "node:fs";
import { join } from "node:path";

const BREADCRUMB = readFileSync(join(import.meta.dir, "../Breadcrumb.tsx"), "utf8");

test("click-to-edit's requestSearchFocus call over a claimed folder passes the current-path seed", () => {
  const at = BREADCRUMB.indexOf("if (claimedRef.current) {\n        requestSearchFocus(");
  expect(at).toBeGreaterThan(-1);
  const call = BREADCRUMB.slice(at, at + 200);
  expect(call).toMatch(/requestSearchFocus\(displayPathRef\.current\)/);
});

test("Ctrl/Cmd+L's requestSearchFocus call over a claimed folder passes the same current-path seed", () => {
  const occurrences = [...BREADCRUMB.matchAll(/requestSearchFocus\(displayPathRef\.current\)/g)];
  // One inside the click handler, one inside the Ctrl/Cmd+L handler — the
  // same call shape at both sites, not two different ways of seeding.
  expect(occurrences.length).toBe(2);
});
