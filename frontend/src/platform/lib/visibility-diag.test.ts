import { describe, expect, test } from "bun:test";
import { RAF_RESUMED_MS, RAF_STALL_MS, stallTransition } from "./visibility-diag";

describe("stallTransition", () => {
  test("a long rAF gap while visible is a stall, once", () => {
    expect(stallTransition(false, true, RAF_STALL_MS + 1)).toBe("raf-stalled");
    expect(stallTransition(true, true, RAF_STALL_MS + 1)).toBeNull();
  });
  test("a hidden document never counts as stalled (rAF is paused by design)", () => {
    expect(stallTransition(false, false, 60_000)).toBeNull();
  });
  test("fresh frames end a stall", () => {
    expect(stallTransition(true, true, RAF_RESUMED_MS - 1)).toBe("raf-resumed");
    expect(stallTransition(true, true, RAF_RESUMED_MS + 1)).toBeNull();
    expect(stallTransition(false, true, 16)).toBeNull();
  });
});
