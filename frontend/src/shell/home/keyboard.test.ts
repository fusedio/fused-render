import { expect, test } from "bun:test";
import { keyboardTarget } from "./WidgetGrid";

test("keyboardTarget maps arrows to neighbours and clamps at the ends", () => {
  expect(keyboardTarget("ArrowRight", 1, 4)).toBe(2);
  expect(keyboardTarget("ArrowDown", 1, 4)).toBe(2);
  expect(keyboardTarget("ArrowLeft", 1, 4)).toBe(0);
  expect(keyboardTarget("ArrowUp", 1, 4)).toBe(0);
  expect(keyboardTarget("ArrowLeft", 0, 4)).toBeNull();
  expect(keyboardTarget("ArrowRight", 3, 4)).toBeNull();
  expect(keyboardTarget("Enter", 1, 4)).toBeNull();
});
