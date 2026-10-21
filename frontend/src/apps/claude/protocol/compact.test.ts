import { describe, expect, test } from "bun:test";

import { compactLabel } from "./compact";

describe("compactLabel", () => {
  test("both counts", () => {
    expect(compactLabel(167165, 16130, "auto")).toBe("Context compacted · 167k → 16k tokens");
  });
  test("pre only, small number, manual", () => {
    expect(compactLabel(800, undefined, "manual")).toBe("Context compacted · 800 tokens · manual");
  });
  test("exact halves round up", () => {
    expect(compactLabel(2500, 4500)).toBe("Context compacted · 3k → 5k tokens");
  });
  test("no numbers", () => {
    expect(compactLabel()).toBe("Context compacted");
  });
});
