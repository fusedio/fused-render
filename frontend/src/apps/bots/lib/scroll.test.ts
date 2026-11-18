import { describe, expect, test } from "bun:test";
import { nearestScrollTop } from "./scroll";

describe("nearestScrollTop", () => {
  test("a box already in view does not move the container", () => {
    expect(nearestScrollTop(120, 180, 100, 300)).toBe(100);
  });
  test("a box below the view scrolls just enough to show its bottom", () => {
    expect(nearestScrollTop(500, 560, 100, 300)).toBe(260);
  });
  test("a box above the view scrolls to its top", () => {
    expect(nearestScrollTop(40, 90, 100, 300)).toBe(40);
  });
  test("a box taller than the view aligns its top", () => {
    expect(nearestScrollTop(500, 900, 100, 300)).toBe(500);
  });
});
