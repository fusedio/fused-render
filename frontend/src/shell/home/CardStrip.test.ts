import { expect, test } from "bun:test";
import { stripEdges } from "./CardStrip";

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
