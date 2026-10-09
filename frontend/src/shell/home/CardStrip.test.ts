import { expect, test } from "bun:test";
import { iconRowsThatFit, stripEdges } from "./CardStrip";

test("stripEdges: at the start only next is available", () => {
  expect(stripEdges(0, 400, 1000)).toEqual({ canPrev: false, canNext: true });
});
test("stripEdges: in the middle both are available", () => {
  expect(stripEdges(200, 400, 1000)).toEqual({ canPrev: true, canNext: true });
});
test("stripEdges: at the end only prev is available (1px tolerance)", () => {
  expect(stripEdges(600, 400, 1000)).toEqual({ canPrev: true, canNext: false });
  expect(stripEdges(599.5, 400, 1000)).toEqual({ canPrev: true, canNext: false });
});
test("stripEdges: no overflow means neither", () => {
  expect(stripEdges(0, 400, 400)).toEqual({ canPrev: false, canNext: false });
  expect(stripEdges(0, 400, 400.5)).toEqual({ canPrev: false, canNext: false });
});

import { nearEnd } from "./CardStrip";
test("nearEnd: far from the end is false", () => {
  expect(nearEnd(0, 400, 1000)).toBe(false);
});
test("nearEnd: within one viewport of the end is true", () => {
  expect(nearEnd(200, 400, 1000)).toBe(true);
  expect(nearEnd(600, 400, 1000)).toBe(true);
});
test("nearEnd: no overflow is true", () => {
  expect(nearEnd(0, 400, 400)).toBe(true);
});

test("iconRowsThatFit: exact fits for 2, 3 and 4 rows", () => {
  expect(iconRowsThatFit(2 * 80 + 8, 80, 8, 1)).toBe(2);
  expect(iconRowsThatFit(3 * 80 + 16, 80, 8, 1)).toBe(3);
  expect(iconRowsThatFit(4 * 80 + 24, 80, 8, 1)).toBe(4);
});
test("iconRowsThatFit: a partial row rounds down", () => {
  expect(iconRowsThatFit(3 * 80 + 16 + 87, 80, 8, 1)).toBe(3);
});
test("iconRowsThatFit: never below minRows", () => {
  expect(iconRowsThatFit(100, 80, 8, 2)).toBe(2);
});
test("iconRowsThatFit: zero or negative height returns minRows", () => {
  expect(iconRowsThatFit(0, 80, 8, 2)).toBe(2);
  expect(iconRowsThatFit(-50, 80, 8, 3)).toBe(3);
});
