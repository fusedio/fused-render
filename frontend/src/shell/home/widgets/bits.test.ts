import { expect, test } from "bun:test";
import { seedCount } from "./bits";

test("seedCount: mounted empty, items arrive later -> first-paint guess", () => {
  expect(seedCount(0, 5, 3)).toBe(3);
  expect(seedCount(0, 2, 3)).toBe(2);
});

test("seedCount: a measured count or an empty list is left alone", () => {
  expect(seedCount(4, 10, 3)).toBe(4);
  expect(seedCount(0, 0, 3)).toBe(0);
});

import { fitCount, moreLineFits } from "./bits";

// avail 200px, rows 44px pitch, "+N more" line 20px.
const base = { total: 10, avail: 200, rowH: 44, moreH: 20, cols: 1 };

test("fitCount: exact fit with the more line stays put", () => {
  // 4 rows = 176 + 20 more = 196 <= 200, a 5th row would be 240
  expect(fitCount({ ...base, n: 4, contentH: 176 })).toBe(4);
});

test("fitCount: overflow with the more line shrinks", () => {
  // 5 rows = 220 + 20 > 200
  expect(fitCount({ ...base, n: 5, contentH: 220 })).toBe(4);
});

test("fitCount: spare room grows by a row", () => {
  expect(fitCount({ ...base, n: 2, contentH: 88 })).toBe(3);
});

test("fitCount: growth must leave room for the more line", () => {
  // 4 rows + more = 196. Growing to 5 = 220 + 20 overflows.
  expect(fitCount({ ...base, n: 4, contentH: 176 })).toBe(4);
  // all items shown needs no more line: 9 -> 10 rows = 440 > 200 never fits here
  expect(fitCount({ total: 5, avail: 230, rowH: 44, moreH: 20, cols: 1, n: 4, contentH: 176 })).toBe(5);
});

test("fitCount: showing everything drops the more line", () => {
  // 5 of 5: 220 <= 230 fits without a more line even though 220 + 20 > 230
  expect(fitCount({ total: 5, avail: 230, rowH: 44, moreH: 20, cols: 1, n: 5, contentH: 220 })).toBe(5);
});

test("fitCount: never below one row", () => {
  expect(fitCount({ ...base, n: 1, contentH: 300, avail: 10 })).toBe(1);
});

test("fitCount: never past total", () => {
  expect(fitCount({ total: 3, avail: 900, rowH: 44, moreH: 20, cols: 1, n: 3, contentH: 132 })).toBe(3);
});

test("fitCount: two columns move two items per line", () => {
  expect(fitCount({ ...base, cols: 2, n: 4, contentH: 88 })).toBe(6);
  expect(fitCount({ ...base, cols: 2, n: 8, contentH: 176 + 0, avail: 150 })).toBe(6);
});

test("fitCount: converges (no oscillation) from every start", () => {
  for (const avail of [60, 100, 150, 200, 260, 400]) {
    for (const cols of [1, 2]) {
      for (let start = 1; start <= 12; start++) {
        let n = start;
        const seen: number[] = [];
        for (let i = 0; i < 30; i++) {
          const lines = Math.ceil(n / cols);
          const next = fitCount({ total: 12, avail, rowH: 44, moreH: 20, cols, n, contentH: lines * 44 });
          seen.push(next);
          if (next === n) break;
          n = next;
        }
        expect(seen[seen.length - 1]).toBe(n);
        expect(seen.length).toBeLessThan(30);
      }
    }
  }
});

test("moreLineFits: the line renders only when items plus line fit the height", () => {
  expect(moreLineFits(44, 24, 120)).toBe(true);
  expect(moreLineFits(96, 24, 120)).toBe(true);
  expect(moreLineFits(100, 24, 120)).toBe(false);
  expect(moreLineFits(NaN, 24, 120)).toBe(true);
});
