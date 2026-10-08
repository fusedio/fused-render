import { expect, test } from "bun:test";
import { pageDone } from "./data";

test("pageDone: more remain", () => {
  expect(pageDone(0, 48, 100)).toBe(false);
});
test("pageDone: reaches the total", () => {
  expect(pageDone(48, 48, 96)).toBe(true);
  expect(pageDone(96, 4, 100)).toBe(true);
});
test("pageDone: empty page ends it even if total is stale", () => {
  expect(pageDone(48, 0, 500)).toBe(true);
});
