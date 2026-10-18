import { beforeEach, describe, expect, it } from "bun:test";

import { _setUiLayoutForTest, getUiLayout } from "@platform/lib/ui-layout";
import { MIN_W } from "./side-width";

// The width is the layout document's `explorerSide.width` (platform/lib/
// ui-layout.ts, D1377). `_setUiLayoutForTest` seeds that module's cache — what
// main.tsx's boot hydrate does in the app — and takes it offline, so nothing
// here fetches.
const {
  clearSideWidth,
  getSideWidth,
  parseStoredSideWidth,
  setSideWidth,
  subscribeSideWidth,
} = await import("./side-store");

beforeEach(() => {
  _setUiLayoutForTest();
});

describe("parseStoredSideWidth", () => {
  it("no stored value is NO CHOICE, not a zero", () => {
    // `null` is a real state the consumers branch on (the column opens at the
    // container's share); a 0 would read as a chosen width.
    expect(parseStoredSideWidth(null)).toBeNull();
    expect(parseStoredSideWidth(undefined)).toBeNull();
    expect(parseStoredSideWidth("")).toBeNull();
  });

  it("a value that is not a number is no choice either", () => {
    expect(parseStoredSideWidth("wide")).toBeNull();
    expect(parseStoredSideWidth("NaN")).toBeNull();
    expect(parseStoredSideWidth("Infinity")).toBeNull();
  });

  it("a real width comes back, rounded", () => {
    expect(parseStoredSideWidth(String(MIN_W + 100))).toBe(MIN_W + 100);
    expect(parseStoredSideWidth(String(MIN_W + 100.6))).toBe(MIN_W + 101);
  });

  it("a width below the legibility floor is RAISED to it, never honoured", () => {
    // The floor is measured, not preferred (side-width.ts's MIN_W header), so a
    // hand-edited or corrupted value cannot open the column unusably narrow.
    expect(parseStoredSideWidth("40")).toBe(MIN_W);
    expect(parseStoredSideWidth("-999")).toBe(MIN_W);
  });
});

describe("side-store persistence (#29, D1377)", () => {
  it("a completed drag is written to the layout document, in whole pixels", () => {
    setSideWidth(MIN_W + 120.4);
    expect(getUiLayout().explorerSide?.width).toBe(MIN_W + 120);
  });

  it("a hydrated width is the store's answer before the first render", () => {
    // The two consumers read this in a `useState` initializer, so a seed that
    // arrived in an effect would paint the default share and then jump.
    _setUiLayoutForTest({ v: 1, explorerSide: { width: MIN_W + 220 } });
    expect(getSideWidth()).toBe(MIN_W + 220);
  });

  it("with nothing stored the store still starts at NO CHOICE", () => {
    expect(getSideWidth()).toBeNull();
  });

  it("a stored value under the floor is clamped on the way in", () => {
    _setUiLayoutForTest({ v: 1, explorerSide: { width: 12 } });
    expect(getSideWidth()).toBe(MIN_W);
  });

  it("clearing the width stores NO CHOICE, not a zero", () => {
    setSideWidth(MIN_W + 90);
    clearSideWidth();
    expect(getUiLayout().explorerSide?.width).toBeNull();
    expect(getSideWidth()).toBeNull();
  });

  it("still notifies subscribers — the reopen drag depends on it", () => {
    let seen = 0;
    const off = subscribeSideWidth(() => (seen += 1));
    setSideWidth(MIN_W + 40);
    setSideWidth(MIN_W + 40); // same value: no event
    setSideWidth(MIN_W + 41);
    off();
    setSideWidth(MIN_W + 42);
    expect(seen).toBe(2);
  });
});
