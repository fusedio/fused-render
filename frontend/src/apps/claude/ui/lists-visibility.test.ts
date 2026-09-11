import { expect, test } from "bun:test";
import {
  computeLists,
  filledTabs,
  isAlone,
  isFilled,
  nextTab,
  type ListCounts,
} from "./lists-visibility";

const counts = (over: Partial<ListCounts> = {}): ListCounts => ({
  recent: null,
  artifacts: null,
  snaps: null,
  snapsFailed: false,
  ...over,
});

test("boot: nothing read yet — no bar, and only the two lists with a stand-in", () => {
  const view = computeLists(counts(), "recent");
  expect(view.tabbed).toBe(false);
  // Recent has a skeleton and snapshots have a sentence; artifacts have
  // nothing to stand in for (T:18272-18290).
  expect(view.shown).toEqual({ recent: true, artifacts: false, snaps: true });
  expect(view.tabShown).toEqual({
    recent: false,
    artifacts: false,
    snaps: false,
  });
});

test("`null` is not zero: an unread list is never FILLED, so the bar cannot flash", () => {
  expect(isFilled(counts(), "recent")).toBe(false);
  expect(isAlone(counts(), "recent")).toBe(true);
  expect(isFilled(counts({ recent: 0 }), "recent")).toBe(false);
  expect(isAlone(counts({ recent: 0 }), "recent")).toBe(false);
});

test("one filled list is just that list under its own heading", () => {
  const view = computeLists(
    counts({ recent: 3, artifacts: 0, snaps: 0 }),
    "recent",
  );
  expect(view.tabbed).toBe(false);
  expect(view.shown).toEqual({ recent: true, artifacts: false, snaps: false });
});

test("two filled lists earn the bar, and only filled lists get a tab", () => {
  const view = computeLists(
    counts({ recent: 2, artifacts: 1, snaps: 0 }),
    "recent",
  );
  expect(view.tabbed).toBe(true);
  expect(view.tabShown).toEqual({
    recent: true,
    artifacts: true,
    snaps: false,
  });
  expect(view.shown).toEqual({ recent: true, artifacts: false, snaps: false });
});

test("a failed snapshots read keeps its tab, so the retry stays reachable", () => {
  const c = counts({ recent: 2, artifacts: 0, snaps: 0, snapsFailed: true });
  expect(filledTabs(c)).toEqual(["recent", "snaps"]);
  expect(computeLists(c, "snaps").shown.snaps).toBe(true);
});

test("a selected tab whose list emptied falls back to the first filled one", () => {
  const view = computeLists(
    counts({ recent: 4, artifacts: 2, snaps: 0 }),
    "snaps",
  );
  expect(view.selected).toBe("recent");
  expect(view.shown.recent).toBe(true);
});

test("an untabbed block keeps the selection it was handed", () => {
  expect(computeLists(counts({ recent: 4 }), "snaps").selected).toBe("snaps");
});

test("arrows walk the SHOWN tabs, wrapping; a hidden tab is not a stop", () => {
  const c = counts({ recent: 2, artifacts: 0, snaps: 1 });
  expect(nextTab(c, "recent", 1)).toBe("snaps");
  expect(nextTab(c, "snaps", 1)).toBe("recent");
  expect(nextTab(c, "recent", -1)).toBe("snaps");
  // Fewer than two on the bar: nothing to walk to.
  expect(nextTab(counts({ recent: 2 }), "recent", 1)).toBe(null);
  // Asking from a tab that is not on the bar is not a move.
  expect(nextTab(c, "artifacts", 1)).toBe(null);
});
