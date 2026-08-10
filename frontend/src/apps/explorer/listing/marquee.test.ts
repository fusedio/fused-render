// Sweep-to-select, as arithmetic: the region two points make, which rows it
// covers, what a modifier adds to that, when a press has travelled far enough
// to be a drag at all, and how fast the listing scrolls at its edges.
//
// The region is never drawn — the rows highlighting IS the feedback — so every
// case below is about which rows a sweep claims, not about a rectangle on
// screen.
//
// All of it is here rather than in an interaction test for the same reason
// pane-math's is: a headless test cannot see layout, so the DOM wiring is kept
// as thin as it can be and every DECISION it makes is one of these functions.
import { describe, expect, test } from "bun:test";
import {
  MARQUEE_DRAG_SLOP,
  autoScrollStep,
  marqueeBox,
  marqueeHits,
  passedDragSlop,
  rowBands,
} from "./marquee";

// A row band, in the scroller's content coordinates (see rowBands).
const band = (path: string, top: number, bottom: number) => ({
  path,
  top,
  bottom,
  left: 0,
  right: 400,
});

const rows = [band("/w/a", 0, 24), band("/w/b", 24, 48), band("/w/c", 48, 72)];

describe("marqueeBox", () => {
  test("normalizes whichever way the drag went", () => {
    const downRight = marqueeBox({ x: 10, y: 10 }, { x: 50, y: 90 });
    expect(downRight).toEqual({ left: 10, top: 10, right: 50, bottom: 90 });
    // Dragging up-and-left has to describe the same rectangle, or the box would
    // have a negative height and intersect nothing.
    expect(marqueeBox({ x: 50, y: 90 }, { x: 10, y: 10 })).toEqual(downRight);
  });

  test("a box with no area is still a box", () => {
    expect(marqueeBox({ x: 5, y: 5 }, { x: 5, y: 5 })).toEqual({
      left: 5,
      top: 5,
      right: 5,
      bottom: 5,
    });
  });
});

describe("passedDragSlop", () => {
  test("a press that barely moves is a click, not a marquee", () => {
    // The existing single-click-select / double-click-open behaviour depends on
    // this: every click wobbles a pixel or two between press and release.
    expect(passedDragSlop({ x: 100, y: 100 }, { x: 101, y: 102 })).toBe(false);
    expect(passedDragSlop({ x: 100, y: 100 }, { x: 100, y: 100 })).toBe(false);
  });

  test("past the slop in either axis is a drag", () => {
    expect(passedDragSlop({ x: 100, y: 100 }, { x: 100, y: 106 })).toBe(true);
    expect(passedDragSlop({ x: 100, y: 100 }, { x: 94, y: 100 })).toBe(true);
    expect(MARQUEE_DRAG_SLOP).toBe(4);
  });
});

describe("marqueeHits", () => {
  test("selects every row the box touches, in rendered order", () => {
    const box = marqueeBox({ x: 5, y: 30 }, { x: 200, y: 60 });
    expect(marqueeHits(box, rows, { additive: false, base: [] })).toEqual(["/w/b", "/w/c"]);
  });

  test("a box that touches nothing selects nothing", () => {
    const box = marqueeBox({ x: 5, y: 200 }, { x: 200, y: 300 });
    expect(marqueeHits(box, rows, { additive: false, base: [] })).toEqual([]);
  });

  test("grazing a row's edge counts — the band is inclusive at the top", () => {
    // Rows are laid end to end (one's bottom IS the next one's top), so a box
    // that stops exactly on the seam must not claim both rows below and above.
    const box = marqueeBox({ x: 5, y: 24 }, { x: 200, y: 24 });
    expect(marqueeHits(box, rows, { additive: false, base: [] })).toEqual(["/w/b"]);
  });

  test("a box beside the rows, not over them, hits nothing", () => {
    // Only matters for a view mode whose rows do not span the full width; a
    // full-width list row can't be missed horizontally.
    const box = marqueeBox({ x: 500, y: 0 }, { x: 600, y: 100 });
    expect(marqueeHits(box, rows, { additive: false, base: [] })).toEqual([]);
  });

  test("REPLACES the selection by default", () => {
    const box = marqueeBox({ x: 5, y: 50 }, { x: 200, y: 60 });
    expect(marqueeHits(box, rows, { additive: false, base: ["/w/a"] })).toEqual(["/w/c"]);
  });

  test("additive keeps what was already selected, in front", () => {
    // Selection.paths is ordered by when a path ENTERED the selection, so the
    // pre-drag rows stay ahead of the swept ones.
    const box = marqueeBox({ x: 5, y: 50 }, { x: 200, y: 60 });
    expect(marqueeHits(box, rows, { additive: true, base: ["/w/a"] })).toEqual(["/w/a", "/w/c"]);
  });

  test("additive never doubles a row it already held", () => {
    const box = marqueeBox({ x: 5, y: 0 }, { x: 200, y: 60 });
    expect(marqueeHits(box, rows, { additive: true, base: ["/w/b"] })).toEqual([
      "/w/b",
      "/w/a",
      "/w/c",
    ]);
  });

  test("an UNMEASURABLE listing leaves the selection exactly as it was", () => {
    // No bands is not "the sweep hit nothing" — it is "there was nothing to
    // measure": no rendered row to take geometry from (useMarquee's
    // measureBands), or a row that measured zero-height (rowBands). Both said in
    // their comments that the caller would leave the selection alone, and both
    // fell through to the non-additive return, so `selectPaths([])` collapsed a
    // real selection on a drag that could never have selected anything.
    const box = marqueeBox({ x: 5, y: 0 }, { x: 200, y: 300 });
    expect(marqueeHits(box, [], { additive: false, base: ["/w/a", "/w/b"] }))
      .toEqual(["/w/a", "/w/b"]);
    expect(marqueeHits(box, [], { additive: true, base: ["/w/a"] })).toEqual(["/w/a"]);
    // Nothing selected to begin with is still nothing.
    expect(marqueeHits(box, [], { additive: false, base: [] })).toEqual([]);
  });

  test("a box over real bands that touches none of them DOES clear", () => {
    // The distinction the case above turns on: a sweep across empty space below
    // the rows is a deliberate "start again from here", and must keep clearing.
    const box = marqueeBox({ x: 5, y: 200 }, { x: 200, y: 300 });
    expect(marqueeHits(box, rows, { additive: false, base: ["/w/a"] })).toEqual([]);
  });

  test("additive keeps a base path that is no longer a row", () => {
    // A row can leave the listing under a live dir-watch refresh; the union
    // is over the selection the user has, not over what is on screen. The
    // reconcile in useListingSelection is what prunes dead paths.
    const box = marqueeBox({ x: 5, y: 0 }, { x: 200, y: 10 });
    expect(marqueeHits(box, rows, { additive: true, base: ["/w/gone"] })).toEqual([
      "/w/gone",
      "/w/a",
    ]);
  });
});

describe("rowBands", () => {
  // The intersection is decided from the ROW MODEL plus one measured row, never
  // from a per-row DOM read: rows are uniform height, a long listing is
  // thousands of them, and any windowed/virtualized view has no node at all for
  // the rows currently off screen — which a marquee still has to be able to
  // sweep over.
  test("lays the rows out from the model and one measured row height", () => {
    expect(rowBands(["/w/a", "/w/b", "/w/c"], { firstTop: 40, height: 24, left: 0, right: 400 }))
      .toEqual([
        band("/w/a", 40, 64),
        band("/w/b", 64, 88),
        band("/w/c", 88, 112),
      ]);
  });

  test("no rows, or an unmeasurable row height, lays out nothing", () => {
    expect(rowBands([], { firstTop: 40, height: 24, left: 0, right: 400 })).toEqual([]);
    // A zero height would stack every row on the same band and select the whole
    // folder from a one-pixel drag.
    expect(rowBands(["/w/a"], { firstTop: 40, height: 0, left: 0, right: 400 })).toEqual([]);
  });
});

describe("autoScrollStep", () => {
  // Viewport coordinates here, unlike the bands above: the scroller's rect and
  // the pointer's client position.
  const view = { top: 100, bottom: 500, left: 0, right: 400 };
  // Somewhere over the listing horizontally, so only the vertical rule speaks.
  const step = (y: number) => autoScrollStep({ x: 200, y }, view);

  test("the middle of the listing does not scroll", () => {
    expect(step(300)).toBe(0);
    expect(step(140)).toBe(0);
  });

  test("the bottom edge scrolls down, the top edge up", () => {
    expect(step(495)).toBeGreaterThan(0);
    expect(step(105)).toBeLessThan(0);
  });

  test("deeper into the edge zone is faster", () => {
    expect(step(499)).toBeGreaterThan(step(485));
    expect(step(101)).toBeLessThan(step(115));
  });

  test("past the edge holds at full speed, it does not accelerate away", () => {
    // The pointer leaves the scroller during a long sweep; without the cap the
    // step would grow with the distance and the listing would fly.
    const atEdge = step(500);
    expect(step(900)).toBe(atEdge);
    expect(step(-900)).toBe(-atEdge);
  });

  test("a pointer to the SIDE of the listing does not scroll it", () => {
    // Both gestures pointer-capture, so the pointer keeps driving this loop from
    // over the preview pane or the sidebar. Parked out there and incidentally
    // within the edge zone, the listing scrolled on and the sweep kept selecting
    // rows the pointer was nowhere near. The row drag guarded this at its call
    // site; the sweep did not, which is why the rule is in here now.
    expect(autoScrollStep({ x: 900, y: 495 }, view)).toBe(0);
    expect(autoScrollStep({ x: -50, y: 105 }, view)).toBe(0);
    // …and the edges themselves are still inside.
    expect(autoScrollStep({ x: 0, y: 495 }, view)).toBeGreaterThan(0);
    expect(autoScrollStep({ x: 400, y: 495 }, view)).toBeGreaterThan(0);
  });
});
