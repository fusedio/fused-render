// The width magnet between the rail's two windows (card-snap.ts,
// rail-peek-lib `snapWidth`).
import { afterEach, describe, expect, it } from "bun:test";
import { PEEK_MAX_WIDTH, PEEK_MIN_WIDTH, SNAP_PX, snapWidth, type Box } from "./rail-peek-lib";
import { publishWindowABox, resetCardSnap, setSnapGuide, snapGuide, windowABox } from "./card-snap";

// Window A above window B, both right edges at x=1000 (the rail's column).
const B: Box = { left: 600, top: 500, width: 400, height: 300 };
const at = (width: number, top = 40, height = 400): Box => ({ left: 1000 - width, top, width, height });

describe("snapWidth", () => {
  it("takes the other card's width within SNAP_PX, right edge kept, guide over both", () => {
    const r = snapWidth(at(400 + SNAP_PX), "l", B, PEEK_MIN_WIDTH, PEEK_MAX_WIDTH);
    expect(r.box).toEqual({ left: 600, top: 40, width: 400, height: 400 });
    expect(r.guide).toEqual({ x: 600, top: 40, bottom: 800 });
    expect(snapWidth(at(400 - 5), "bl", B, PEEK_MIN_WIDTH, PEEK_MAX_WIDTH).box.width).toBe(400);
  });

  it("lets go past SNAP_PX", () => {
    const asked = at(400 + SNAP_PX + 1);
    const r = snapWidth(asked, "tl", B, PEEK_MIN_WIDTH, PEEK_MAX_WIDTH);
    expect(r.box).toBe(asked);
    expect(r.guide).toBeNull();
  });

  it("never on a height-only grip, with no other card, or side by side", () => {
    const asked = at(404);
    expect(snapWidth(asked, "t", B, PEEK_MIN_WIDTH, PEEK_MAX_WIDTH).guide).toBeNull();
    expect(snapWidth(asked, "b", B, PEEK_MIN_WIDTH, PEEK_MAX_WIDTH).guide).toBeNull();
    expect(snapWidth(asked, "l", null, PEEK_MIN_WIDTH, PEEK_MAX_WIDTH).guide).toBeNull();
    // Window A beside B, its right edge at B's left: not one column.
    const beside: Box = { left: 592 - 404, top: 40, width: 404, height: 400 };
    expect(snapWidth(beside, "l", B, PEEK_MIN_WIDTH, PEEK_MAX_WIDTH).box).toBe(beside);
  });

  it("never to a width this card's own clamps refuse", () => {
    const asked = at(405);
    expect(snapWidth(asked, "l", B, PEEK_MIN_WIDTH, 399).box).toBe(asked);
    expect(snapWidth(asked, "l", B, 401, PEEK_MAX_WIDTH).box).toBe(asked);
  });
});

describe("card-snap store", () => {
  afterEach(() => resetCardSnap());

  it("a closing card's null never wipes the card now up", () => {
    publishWindowABox("list", at(420));
    publishWindowABox("plus", at(500));
    publishWindowABox("list", null);
    expect(windowABox()?.width).toBe(500);
    publishWindowABox("plus", null);
    expect(windowABox()).toBeNull();
  });

  it("keeps the guide until it is cleared", () => {
    setSnapGuide({ x: 600, top: 40, bottom: 800 });
    expect(snapGuide()).toEqual({ x: 600, top: 40, bottom: 800 });
    setSnapGuide(null);
    expect(snapGuide()).toBeNull();
  });
});
