// The rail's expanded flag + width (railstate.ts): read off the layout
// document, clamped, absent = collapsed at the default width.
import { beforeEach, describe, expect, it } from "bun:test";
import { _setUiLayoutForTest, getUiLayout, sanitizeUiLayout } from "./ui-layout";

const {
  RAIL_COLLAPSED_WIDTH,
  RAIL_DEFAULT_WIDTH,
  RAIL_MAX_WIDTH,
  RAIL_MIN_WIDTH,
  getRailState,
  loadRailState,
  setRailState,
  subscribeRailState,
  toggleRailExpanded,
} = await import("./railstate");

beforeEach(() => {
  _setUiLayoutForTest({ v: 1 });
  setRailState({ width: RAIL_DEFAULT_WIDTH, expanded: false }, false);
});

describe("document field", () => {
  it("bounds are the sidebar's 180..400, default 200, collapsed 48", () => {
    expect([RAIL_MIN_WIDTH, RAIL_MAX_WIDTH, RAIL_DEFAULT_WIDTH, RAIL_COLLAPSED_WIDTH]).toEqual([180, 400, 200, 48]);
  });
  it("sanitize keeps a good rail, clamps width, reads expanded strictly", () => {
    expect(sanitizeUiLayout({ v: 1, rail: { width: 250, expanded: true } }).rail).toEqual({ width: 250, expanded: true });
    expect(sanitizeUiLayout({ v: 1, rail: { width: 9999, expanded: 1 } }).rail).toEqual({ width: 400, expanded: false });
    expect(sanitizeUiLayout({ v: 1, rail: { width: 10.4, expanded: true } }).rail).toEqual({ width: 180, expanded: true });
    expect(sanitizeUiLayout({ v: 1, rail: "x" }).rail).toBeUndefined();
  });
});

describe("store", () => {
  it("absent field loads collapsed at the default width", () => {
    expect(loadRailState()).toEqual({ width: RAIL_DEFAULT_WIDTH, expanded: false });
  });
  it("persists a toggle, rounded and clamped, and notifies", () => {
    let n = 0;
    const off = subscribeRailState(() => n++);
    toggleRailExpanded();
    expect(getRailState().expanded).toBe(true);
    expect(getUiLayout().rail).toEqual({ width: RAIL_DEFAULT_WIDTH, expanded: true });
    setRailState((s) => ({ ...s, width: 999.6 }));
    expect(getUiLayout().rail).toEqual({ width: 400, expanded: true });
    expect(n).toBe(2);
    off();
    toggleRailExpanded();
    expect(n).toBe(2);
  });
  it("persist=false (a drag frame) does not write the document", () => {
    setRailState({ width: 300, expanded: true }, false);
    expect(getRailState().width).toBe(300);
    expect(getUiLayout().rail).toBeUndefined();
  });
});
