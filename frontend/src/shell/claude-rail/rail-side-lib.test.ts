// A rail card on either side (rail-side-lib): the left sidebar's card is the
// Claude rail's card mirrored — same placement, cone and bridge, on the other
// side of its rail.
import { describe, expect, it } from "bun:test";
import { PEEK_GAP, PEEK_INSET, PEEK_MIN_WIDTH, inSafeCone, placeClear, type Box } from "./rail-peek-lib";
import { BRIDGE_INSET, bridgeBox, inSafeConeBeside, mirrorBox, mirrorPoint, placeBeside } from "./rail-side-lib";

const W = 1400;
const H = 900;
const row: Box = { left: 0, top: 0, width: W, height: H };
// The collapsed left sidebar: 48px at the window's left edge.
const leftRail: Box = { left: 0, top: 0, width: 48, height: H };
const door = (top: number): Box => ({ left: 10, top, width: 28, height: 28 });
const wish = { width: 420, maxHeight: 640 };

describe("mirrorBox / mirrorPoint", () => {
  it("mirror about the viewport's width, and are their own inverse", () => {
    const b = { left: 10, top: 5, width: 28, height: 30 };
    expect(mirrorBox(b, W)).toEqual({ left: W - 38, top: 5, width: 28, height: 30 });
    expect(mirrorBox(mirrorBox(b, W), W)).toEqual(b);
    expect(mirrorPoint({ x: 24, y: 9 }, W)).toEqual({ x: W - 24, y: 9 });
  });
});

describe("placeBeside", () => {
  it("left: right of the rail, a gap off its edge, top level with the door, window A's size", () => {
    const at = placeBeside("left", { door: door(120), rail: leftRail, row, avoid: null, wish, viewportWidth: W })!;
    expect(at).toEqual({ left: 48 + PEEK_GAP, top: 120, width: 420, height: 640 });
  });
  it("left: pushed up only as far as the row's bottom needs, never above its top", () => {
    const low = placeBeside("left", { door: door(800), rail: leftRail, row, avoid: null, wish, viewportWidth: W })!;
    expect(low.top).toBe(H - PEEK_INSET - 640);
    const tall = placeBeside("left", { door: door(10), rail: leftRail, row: { ...row, height: 500 }, avoid: null, wish, viewportWidth: W })!;
    expect(tall.top).toBe(PEEK_INSET);
    expect(tall.height).toBe(500 - 2 * PEEK_INSET);
  });
  it("left: no room in a narrow window — no card", () => {
    const narrow = 48 + PEEK_GAP + PEEK_MIN_WIDTH - 1;
    expect(placeBeside("left", { door: door(100), rail: leftRail, row: { ...row, width: narrow }, avoid: null, wish, viewportWidth: narrow })).toBeNull();
  });
  it("left: narrower than its wish where the row is short of room, never under the floor", () => {
    const vw = 48 + PEEK_GAP + 380 + PEEK_INSET;
    const at = placeBeside("left", { door: door(100), rail: leftRail, row: { ...row, width: vw }, avoid: null, wish, viewportWidth: vw })!;
    expect(at.left).toBe(48 + PEEK_GAP);
    expect(at.width).toBe(380);
  });
  it("left: a door scrolled out of its run anchors at the run's edge", () => {
    const scroller = { left: 0, top: 200, width: 48, height: 300 };
    const above = placeBeside("left", { door: door(50), rail: leftRail, row, avoid: null, wish, scroller, viewportWidth: W })!;
    expect(above.top).toBe(200);
    const inside = placeBeside("left", { door: door(220), rail: leftRail, row, avoid: null, wish, scroller, viewportWidth: W })!;
    expect(inside.top).toBe(220);
  });
  it("left: ignores any box to avoid (no window B on that side)", () => {
    const avoid = { left: 60, top: 100, width: 400, height: 300 };
    const at = placeBeside("left", { door: door(120), rail: leftRail, row, avoid, wish, viewportWidth: W })!;
    expect(at.left).toBe(48 + PEEK_GAP);
    expect(at.top).toBe(120);
  });
  it("right: exactly the Claude rail's own placement", () => {
    const rail = { left: W - 48, top: 0, width: 48, height: H };
    const d = { left: W - 38, top: 300, width: 28, height: 28 };
    const avoid = { left: W - 48 - 8 - 380, top: 500, width: 380, height: 300 };
    expect(placeBeside("right", { door: d, rail, row, avoid, wish, viewportWidth: W })).toEqual(placeClear(d, rail, row, avoid, wish));
  });
});

describe("inSafeConeBeside", () => {
  const card = { left: 56, top: 100, width: 420, height: 640 };
  const apex = { x: 24, y: 400 };
  it("left: the triangle from the door to the card's LEFT edge", () => {
    expect(inSafeConeBeside("left", { x: 50, y: 400 }, apex, card, W)).toBe(true);
    // At or behind the apex (the rail's own column) is not the cone's.
    expect(inSafeConeBeside("left", { x: 20, y: 400 }, apex, card, W)).toBe(false);
    // Inside the card is the card's.
    expect(inSafeConeBeside("left", { x: 100, y: 400 }, apex, card, W)).toBe(false);
    // Far above the cone.
    expect(inSafeConeBeside("left", { x: 30, y: 50 }, apex, card, W)).toBe(false);
  });
  it("right: the Claude rail's cone itself", () => {
    const c = { left: 900, top: 100, width: 420, height: 640 };
    const a = { x: W - 24, y: 400 };
    for (const p of [{ x: W - 60, y: 400 }, { x: W - 10, y: 400 }, { x: 1000, y: 400 }]) expect(inSafeConeBeside("right", p, a, c, W)).toBe(inSafeCone(p, a, c));
  });
});

describe("bridgeBox", () => {
  it("left: from the rail's right edge (plus the inset) to the card's left", () => {
    const box = { left: 56, top: 100, width: 420, height: 640 };
    expect(bridgeBox("left", box, 48)).toEqual({ left: 48 + BRIDGE_INSET, top: 100, width: 56 - 48 - BRIDGE_INSET, height: 640 });
    // A card touching the rail: none.
    expect(bridgeBox("left", { ...box, left: 49 }, 48)).toBeNull();
  });
  it("right: DoorCard's own bridge", () => {
    const box = { left: 900, top: 100, width: 420, height: 640 };
    expect(bridgeBox("right", box, 1352)).toEqual({ left: 1320, top: 100, width: 1352 - 3 - 1320, height: 640 });
    expect(bridgeBox("right", box, 1321)).toBeNull();
  });
});
