import { beforeEach, describe, expect, it } from "bun:test";

// The flag is the layout document's `explorerSide.hidden` (platform/lib/
// ui-layout.ts, D1384). `_setUiLayoutForTest` seeds that module's cache — what
// main.tsx's boot hydrate does in the app — and takes it offline, so nothing
// here fetches.
import { _setUiLayoutForTest, getUiLayout } from "@platform/lib/ui-layout";
import { getSideHidden, setSideHidden } from "./side-hidden-store";

beforeEach(() => {
  _setUiLayoutForTest();
});

describe("side-hidden-store", () => {
  it("starts clean", () => {
    expect(getSideHidden()).toBe(false);
  });

  it("remembers a close", () => {
    setSideHidden(true);
    expect(getSideHidden()).toBe(true);
  });

  it("clears on a reopen", () => {
    setSideHidden(true);
    setSideHidden(false);
    expect(getSideHidden()).toBe(false);
  });
});

describe("side-hidden-store persistence (D1384)", () => {
  it("a close is written to the layout document", () => {
    setSideHidden(true);
    expect(getUiLayout().explorerSide?.hidden).toBe(true);
  });

  it("a reopen writes false, and leaves the pane's other fields alone", () => {
    _setUiLayoutForTest({ v: 1, explorerSide: { width: 420, maximized: true } });
    setSideHidden(true);
    setSideHidden(false);
    expect(getUiLayout().explorerSide).toEqual({ width: 420, maximized: true, hidden: false });
  });

  it("a hydrated close is the answer before the first render (survives a relaunch)", () => {
    _setUiLayoutForTest({ v: 1, explorerSide: { hidden: true } });
    expect(getSideHidden()).toBe(true);
  });

  it("nothing stored reads as open", () => {
    _setUiLayoutForTest({ v: 1 });
    expect(getSideHidden()).toBe(false);
  });
});
