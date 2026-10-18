import { beforeEach, describe, expect, it } from "bun:test";

// The flag is the layout document's `explorerSide.maximized` (platform/lib/
// ui-layout.ts, D1377). `_setUiLayoutForTest` seeds that module's cache — what
// main.tsx's boot hydrate does in the app — and takes it offline, so nothing
// here fetches.
import { _setUiLayoutForTest, getUiLayout } from "@platform/lib/ui-layout";
import { getSideMaximized, setSideMaximized, subscribeSideMaximized } from "@platform/lib/side-maximized-store";

beforeEach(() => {
  _setUiLayoutForTest();
});

describe("side-maximized-store", () => {
  it("defaults to false", () => {
    expect(getSideMaximized()).toBe(false);
  });

  it("round-trips set/get", () => {
    setSideMaximized(true);
    expect(getSideMaximized()).toBe(true);
    setSideMaximized(false);
    expect(getSideMaximized()).toBe(false);
  });

  it("is written to the layout document, and a hydrated value is the answer", () => {
    setSideMaximized(true);
    expect(getUiLayout().explorerSide?.maximized).toBe(true);
    _setUiLayoutForTest({ v: 1, explorerSide: { maximized: true } });
    expect(getSideMaximized()).toBe(true);
    setSideMaximized(false);
    expect(getUiLayout().explorerSide?.maximized).toBe(false);
  });

  it("notifies subscribers on change only, and unsubscribes", () => {
    let calls = 0;
    const off = subscribeSideMaximized(() => calls++);
    setSideMaximized(true);
    expect(calls).toBe(1);
    setSideMaximized(true);
    expect(calls).toBe(1);
    off();
    setSideMaximized(false);
    expect(calls).toBe(1);
  });
});
