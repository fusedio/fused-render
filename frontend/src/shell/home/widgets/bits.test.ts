import { expect, test } from "bun:test";
import { fitExtra } from "./bits";

test("fitExtra: no free height buys nothing", () => {
  expect(fitExtra(0, 44)).toBe(0);
});

test("fitExtra: a partial row buys nothing, whole rows count", () => {
  expect(fitExtra(43, 44)).toBe(0);
  expect(fitExtra(44, 44)).toBe(1);
  expect(fitExtra(140, 44)).toBe(3);
});

test("fitExtra: wide two-column lists add two items per line", () => {
  expect(fitExtra(100, 44, 2)).toBe(4);
  expect(fitExtra(30, 44, 2)).toBe(0);
});

test("fitExtra: overflow is negative, bad input is zero", () => {
  expect(fitExtra(-50, 44)).toBe(-2);
  expect(fitExtra(100, 0)).toBe(0);
});
