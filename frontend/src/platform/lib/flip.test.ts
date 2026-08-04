// The two pure diffs behind the listing's row animations: what moved (FLIP) and
// what just appeared (the dir-watch "new row" cue). Both are keyed by row path,
// which is what makes them survive a re-sort or a refetch that replaces every
// object in the list.
import { expect, test } from "bun:test";

import { appearedKeys, flipDeltas } from "@platform/lib/flip";

test("a reorder yields each moved row's inverse offset", () => {
  const before = new Map([["a", 0], ["b", 30], ["c", 60]]);
  const after = new Map([["c", 0], ["b", 30], ["a", 60]]);
  // Positive delta = the row must start BELOW where it landed, i.e. it moved up.
  // Insertion order follows the NEW render order, which is the order the caller
  // walks the DOM in.
  expect([...flipDeltas(before, after)]).toEqual([
    ["c", 60],
    ["a", -60],
  ]);
});

test("rows that did not move are omitted, so nothing is animated pointlessly", () => {
  const before = new Map([["a", 0], ["b", 30]]);
  expect(flipDeltas(before, new Map([["a", 0], ["b", 30]])).size).toBe(0);
});

test("rows only in one snapshot are skipped — an entrance is not a move", () => {
  const before = new Map([["a", 0], ["gone", 30]]);
  const after = new Map([["a", 30], ["fresh", 0]]);
  expect([...flipDeltas(before, after)]).toEqual([["a", -30]]);
});

test("an empty before snapshot animates nothing (first render)", () => {
  expect(flipDeltas(new Map(), new Map([["a", 0], ["b", 30]])).size).toBe(0);
});

test("appearedKeys reports only genuinely new keys", () => {
  expect([...appearedKeys(["a", "b"], ["a", "b", "c"])]).toEqual(["c"]);
  expect([...appearedKeys(["a", "b"], ["b", "a"])]).toEqual([]);
  // A removal is not an appearance, and doesn't make its neighbours new.
  expect([...appearedKeys(["a", "b", "c"], ["a", "c"])]).toEqual([]);
});

test("the first listing is never all-new", () => {
  // No previous snapshot = the folder just opened. Highlighting every row there
  // would turn the cue into noise, and it would say nothing (of course they are
  // all new — you weren't looking at this folder before).
  expect([...appearedKeys(null, ["a", "b", "c"])]).toEqual([]);
});

test("a key that comes back after being removed counts as new again", () => {
  expect([...appearedKeys(["a"], ["a", "b"])]).toEqual(["b"]);
  expect([...appearedKeys(["a", "b"], ["a"])]).toEqual([]);
  expect([...appearedKeys(["a"], ["a", "b"])]).toEqual(["b"]);
});

test("duplicate keys in the next list are reported once", () => {
  expect([...appearedKeys(["a"], ["a", "b", "b"])]).toEqual(["b"]);
});
