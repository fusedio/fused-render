// A PINNED RAIL WINDOW MOVES (rail-move-lib.ts; owner, 2026-11-17): each rule
// with no DOM — the viewport's bounds, the drag's threshold, the stored
// position, what a head press grabs, the floating placement and resize, and
// which window stands on top.
import { describe, expect, test } from "bun:test";
import {
  FLOAT_GRIPS,
  MOVE_THRESHOLD_PX,
  POPOVER_HEAD_SELECTOR,
  RAIL_HEAD_SELECTOR,
  clampPos,
  floatPlacement,
  floatResize,
  headDocks,
  headPress,
  isDockEdge,
  isFloating,
  parsePos,
  pastThreshold,
  railPressWindow,
  raised,
  serialisePos,
  snapEdges,
  snapResize,
  dockedBAxes,
  EDGE_SNAP_PX,
  viewportOf,
  wishSize,
} from "./rail-move-lib";
import { PEEK_MAX_HEIGHT, PEEK_MAX_WIDTH, PEEK_WIDTH, dragResize } from "./rail-peek-lib";

const VIEW = { width: 1400, height: 900 };

describe("clampPos: fully inside the viewport, 0 inset (the breakout's bounds)", () => {
  test("inside stays where it is", () => {
    expect(clampPos({ left: 100, top: 50 }, { width: 400, height: 600 }, VIEW)).toEqual({ left: 100, top: 50 });
  });
  test("past any edge comes back to it", () => {
    expect(clampPos({ left: -30, top: -5 }, { width: 400, height: 600 }, VIEW)).toEqual({ left: 0, top: 0 });
    expect(clampPos({ left: 1200, top: 500 }, { width: 400, height: 600 }, VIEW)).toEqual({ left: 1000, top: 300 });
  });
  test("a card bigger than the viewport stands at its top-left corner", () => {
    expect(clampPos({ left: 50, top: 50 }, { width: 1600, height: 1000 }, VIEW)).toEqual({ left: 0, top: 0 });
  });
  test("an unknown viewport clamps nothing but the top-left", () => {
    const none = viewportOf(undefined);
    expect(none).toEqual({ width: Infinity, height: Infinity });
    expect(viewportOf({ innerWidth: 800, innerHeight: 600 })).toEqual({ width: 800, height: 600 });
    expect(clampPos({ left: 5000, top: -1 }, { width: 400, height: 600 }, none)).toEqual({ left: 5000, top: 0 });
  });
});

describe("pastThreshold: a click or a double-click on the head never moves the card", () => {
  test("under the threshold is a press, at it a drag", () => {
    expect(pastThreshold(0, 0)).toBe(false);
    expect(pastThreshold(2, 2)).toBe(false);
    expect(pastThreshold(MOVE_THRESHOLD_PX, 0)).toBe(true);
    expect(pastThreshold(0, -MOVE_THRESHOLD_PX)).toBe(true);
    expect(pastThreshold(3, 3)).toBe(true);
  });
});

describe("the stored position", () => {
  test("round-trips, rounded", () => {
    expect(parsePos(serialisePos({ left: 10.4, top: 20.6 }))).toEqual({ left: 10, top: 21 });
    expect(serialisePos(null)).toBeNull();
  });
  test("anything that is not one reads as docked", () => {
    expect(parsePos(null)).toBeNull();
    expect(parsePos("")).toBeNull();
    expect(parsePos("not json")).toBeNull();
    expect(parsePos("null")).toBeNull();
    expect(parsePos('{"left":"1","top":2}')).toBeNull();
    expect(parsePos('{"left":1}')).toBeNull();
    expect(parsePos("[1,2]")).toBeNull();
  });
});

describe("what a head press grabs", () => {
  /** An element matching the selectors given (`closest` answers for them). */
  const at = (...matches: string[]) => ({
    closest: (s: string) => (matches.some((m) => s === m || s.split(",").map((x) => x.trim()).includes(m)) ? {} : null),
  });
  test("the head's bare area grabs; a control in it does not; outside the head nothing", () => {
    expect(headPress(at(RAIL_HEAD_SELECTOR), RAIL_HEAD_SELECTOR)).toBe("head");
    expect(headPress(at(RAIL_HEAD_SELECTOR, "button"), RAIL_HEAD_SELECTOR)).toBe("control");
    expect(headPress(at(RAIL_HEAD_SELECTOR, "input"), RAIL_HEAD_SELECTOR)).toBe("control");
    expect(headPress(at(RAIL_HEAD_SELECTOR, '[role="menuitem"]'), RAIL_HEAD_SELECTOR)).toBe("control");
    expect(headPress(at("button"), RAIL_HEAD_SELECTOR)).toBe("none");
    expect(headPress(null, RAIL_HEAD_SELECTOR)).toBe("none");
    expect(headPress(at(POPOVER_HEAD_SELECTOR), POPOVER_HEAD_SELECTOR)).toBe("head");
  });
  test("a double-click docks on the bare head, not on a control nor on a title that renames on it", () => {
    expect(headDocks(at(RAIL_HEAD_SELECTOR), RAIL_HEAD_SELECTOR)).toBe(true);
    expect(headDocks(at(RAIL_HEAD_SELECTOR, "button"), RAIL_HEAD_SELECTOR)).toBe(false);
    expect(headDocks(at(RAIL_HEAD_SELECTOR, ".task-side-peek-title"), RAIL_HEAD_SELECTOR)).toBe(false);
    expect(headDocks(at(".claude-rail-peek-body"), RAIL_HEAD_SELECTOR)).toBe(false);
    expect(headDocks(null, RAIL_HEAD_SELECTOR)).toBe(false);
  });
});

describe("floating", () => {
  test("only a pinned window with a position floats; hover and temp cards never do", () => {
    expect(isFloating({ movable: true, pos: { left: 1, top: 2 } })).toBe(true);
    expect(isFloating({ movable: true, pos: null })).toBe(false);
    expect(isFloating({ movable: false, pos: { left: 1, top: 2 } })).toBe(false);
  });
  test("the size a card asks for: its width under the cap, its height", () => {
    expect(wishSize({})).toEqual({ width: PEEK_WIDTH, height: PEEK_MAX_HEIGHT });
    expect(wishSize({ width: 2000, maxHeight: 300 })).toEqual({ width: PEEK_MAX_WIDTH, height: 300 });
  });
  test("the placement: at its position, at its size, inside the viewport — never null", () => {
    expect(floatPlacement({ left: 100, top: 40 }, { width: 420, height: 640 }, VIEW)).toEqual({ left: 100, top: 40, width: 420, height: 640 });
    expect(floatPlacement({ left: 1300, top: 800 }, { width: 420, height: 640 }, VIEW)).toEqual({ left: 980, top: 260, width: 420, height: 640 });
    expect(floatPlacement({ left: 10, top: 10 }, { width: 420, height: 640 }, { width: 300, height: 200 })).toEqual({
      left: 0,
      top: 0,
      width: 300,
      height: 200,
    });
  });
});

describe("floatResize: the docked grips plus the right side, clamped to the viewport", () => {
  const start = { left: 200, top: 100, width: 420, height: 500 };
  const lim = { minWidth: 360, minHeight: 240, maxWidth: 722 };
  test("the grips: the docked five, then the right edge and corners", () => {
    expect(FLOAT_GRIPS).toEqual(["l", "r", "t", "b", "tl", "bl", "tr", "br"]);
    expect(isDockEdge("tl")).toBe(true);
    expect(isDockEdge("r")).toBe(false);
    expect(isDockEdge("br")).toBe(false);
  });
  test("a left grip moves the left edge, a right grip the right edge", () => {
    expect(floatResize("l", start, -50, 0, VIEW, lim)).toEqual({ left: 150, top: 100, width: 470, height: 500 });
    expect(floatResize("r", start, 50, 0, VIEW, lim)).toEqual({ left: 200, top: 100, width: 470, height: 500 });
  });
  test("a top grip keeps the bottom, a bottom grip the top; corners do both", () => {
    expect(floatResize("t", start, 0, -40, VIEW, lim)).toEqual({ left: 200, top: 60, width: 420, height: 540 });
    expect(floatResize("b", start, 0, 40, VIEW, lim)).toEqual({ left: 200, top: 100, width: 420, height: 540 });
    expect(floatResize("tr", start, 30, -20, VIEW, lim)).toEqual({ left: 200, top: 80, width: 450, height: 520 });
    expect(floatResize("br", start, 30, 20, VIEW, lim)).toEqual({ left: 200, top: 100, width: 450, height: 520 });
    expect(floatResize("bl", start, -30, 20, VIEW, lim)).toEqual({ left: 170, top: 100, width: 450, height: 520 });
  });
  test("the minimums and the width cap hold; the viewport's edges hold", () => {
    expect(floatResize("r", start, -400, 0, VIEW, lim).width).toBe(360);
    expect(floatResize("r", start, 900, 0, VIEW, lim).width).toBe(722);
    expect(floatResize("b", start, 0, -400, VIEW, lim).height).toBe(240);
    // The left edge stops at the viewport's 0; the right at its width.
    expect(floatResize("l", start, -500, 0, VIEW, lim)).toMatchObject({ left: 0, width: 620 });
    expect(floatResize("r", { ...start, left: 1000 }, 900, 0, VIEW, lim)).toMatchObject({ left: 1000, width: 400 });
    // The top stops at 0, the bottom at the viewport's height.
    expect(floatResize("t", start, 0, -500, VIEW, lim)).toMatchObject({ top: 0, height: 600 });
    expect(floatResize("b", start, 0, 900, VIEW, lim)).toMatchObject({ top: 100, height: 800 });
  });
  test("a height ceiling (the favourite's card: half the window) holds", () => {
    expect(floatResize("b", start, 0, 300, VIEW, { ...lim, maxHeight: 450 }).height).toBe(450);
  });
});

describe("last pressed on top", () => {
  test("only while either window floats, and only the window pressed last", () => {
    expect(raised({ win: "a", front: "a", anyFloating: true })).toBe(true);
    expect(raised({ win: "a", front: "b", anyFloating: true })).toBe(false);
    expect(raised({ win: "a", front: "a", anyFloating: false })).toBe(false);
    expect(raised({ win: "b", front: null, anyFloating: true })).toBe(false);
  });
});

describe("which window a press on the rail brings to the front (2026-11-17)", () => {
  const at = (...matches: string[]) => ({
    closest: (s: string) => (s.split(",").some((x) => matches.includes(x.trim())) ? {} : null),
  });
  test("a favourite's ring or a pinned bot's face is window B's", () => {
    expect(railPressWindow(at(".claude-rail-favs"))).toBe("b");
    expect(railPressWindow(at(".claude-rail-bots"))).toBe("b");
  });
  test("a list ring or a nav door (Tasks, Bots, Git, +) is window A's", () => {
    expect(railPressWindow(at(".claude-rail-chats"))).toBe("a");
    expect(railPressWindow(at(".claude-rail-nav"))).toBe("a");
  });
  test("anything else on the rail names no window", () => {
    expect(railPressWindow(at(".claude-rail"))).toBeNull();
    expect(railPressWindow(null)).toBeNull();
  });
});


describe("snapEdges: the edge magnet of a head drag (round 3, owner 2026-11-17)", () => {
  const size = { width: 400, height: 300 };
  // The other window's card: 600..1000 across, 100..500 down.
  const other = { left: 600, top: 100, width: 400, height: 400 };
  const free = { left: 150, top: 560 };
  test("nothing near: the box stays where the drag put it, no guide", () => {
    expect(snapEdges(free, size, other, VIEW)).toEqual({ pos: free, x: [], y: [] });
  });
  test("left↔left, left↔right, right↔left, right↔right", () => {
    expect(snapEdges({ left: 608, top: 560 }, size, other, VIEW).pos.left).toBe(600);
    expect(snapEdges({ left: 994, top: 560 }, size, other, VIEW).pos.left).toBe(1000);
    expect(snapEdges({ left: 205, top: 560 }, size, other, VIEW).pos.left).toBe(200);
    // right↔right, the other card narrower (600..900): flush right at 500.
    expect(snapEdges({ left: 507, top: 560 }, size, { ...other, width: 300 }, VIEW).pos.left).toBe(500);
  });
  test("top↔top, top↔bottom, bottom↔top, bottom↔bottom", () => {
    expect(snapEdges({ left: 150, top: 106 }, size, other, VIEW).pos.top).toBe(100);
    expect(snapEdges({ left: 150, top: 509 }, size, other, VIEW).pos.top).toBe(500);
    // bottom (top + 300) to the other's top (100): top −200 is off-screen, so
    // a case the viewport allows — the other card lower down.
    const lower = { ...other, top: 400 };
    expect(snapEdges({ left: 150, top: 95 }, size, lower, VIEW).pos.top).toBe(100);
    expect(snapEdges({ left: 150, top: 495 }, size, lower, VIEW).pos.top).toBe(500);
  });
  test("the viewport's edges, with no other card", () => {
    expect(snapEdges({ left: 9, top: 300 }, size, null, VIEW).pos).toEqual({ left: 0, top: 300 });
    expect(snapEdges({ left: 991, top: 300 }, size, null, VIEW).pos).toEqual({ left: 1000, top: 300 });
    expect(snapEdges({ left: 300, top: 595 }, size, null, VIEW).pos).toEqual({ left: 300, top: 600 });
  });
  test("12px reaches, 13px does not", () => {
    expect(EDGE_SNAP_PX).toBe(12);
    expect(snapEdges({ left: 612, top: 560 }, size, other, VIEW).pos.left).toBe(600);
    expect(snapEdges({ left: 613, top: 560 }, size, other, VIEW).pos.left).toBe(613);
  });
  test("x and y snap on their own, the nearest edge wins", () => {
    const both = snapEdges({ left: 605, top: 503 }, size, other, VIEW);
    expect(both.pos).toEqual({ left: 600, top: 500 });
    expect(both.x.length).toBeGreaterThan(0);
    expect(both.y.length).toBeGreaterThan(0);
    const xOnly = snapEdges({ left: 605, top: 560 }, size, other, VIEW);
    expect(xOnly.pos).toEqual({ left: 600, top: 560 });
    expect(xOnly.y).toEqual([]);
    // 6px from the other's left, 4px from its right — and 1px off its
    // middle: the nearest line wins, here the middle.
    expect(snapEdges({ left: 606, top: 560 }, { width: 390, height: 300 }, other, VIEW).pos.left).toBe(605);
    // Two edges in reach and no middle (the other card's right edge and the
    // app window's): 5px from the card's, 9px from the window's.
    const wide = { left: 600, top: 100, width: 400 - 14, height: 400 };
    expect(snapEdges({ left: 991, top: 560 }, size, wide, VIEW).pos.left).toBe(986);
  });
  test("the guides run along the shared edge over both cards — every edge the two share", () => {
    // Same width, left edges level: the right edges are level too. Two lines,
    // and no third down the middle for the same fact.
    const s = snapEdges({ left: 605, top: 560 }, size, other, VIEW);
    expect(s.x).toEqual([
      { x: 600, top: 100, bottom: 860 },
      { x: 1000, top: 100, bottom: 860 },
    ]);
    // A narrower card shares one edge only.
    const narrow = snapEdges({ left: 605, top: 560 }, { width: 300, height: 300 }, other, VIEW);
    expect(narrow.x).toEqual([{ x: 600, top: 100, bottom: 860 }]);
    const h = snapEdges({ left: 150, top: 106 }, size, other, VIEW);
    expect(h.y).toEqual([{ y: 100, left: 150, right: 1000 }]);
  });
  test("the middle is a line too: centre to the other card's centre, either axis (owner, 2026-11-18)", () => {
    // The other's centre: x 800, y 300. A 300×200 box centred there stands at 650, 200.
    const small = { width: 300, height: 200 };
    const x = snapEdges({ left: 655, top: 620 }, small, other, VIEW);
    expect(x.pos.left).toBe(650);
    expect(x.x).toEqual([{ x: 800, top: 100, bottom: 820 }]);
    const y = snapEdges({ left: 100, top: 193 }, small, other, VIEW);
    expect(y.pos.top).toBe(200);
    expect(y.y).toEqual([{ y: 300, left: 100, right: 1000 }]);
    // An edge nearer than the centre wins the pull.
    expect(snapEdges({ left: 603, top: 620 }, small, other, VIEW).pos.left).toBe(600);
  });
  test("an edge on the other card's middle, and the middle on the other card's edge (owner, 2026-11-18)", () => {
    // The other's middle: x 800, y 300. Its edges: x 600 / 1000, y 100 / 500.
    const small = { width: 300, height: 200 };
    // LEFT edge on its middle (x 800).
    const l = snapEdges({ left: 806, top: 620 }, small, other, VIEW);
    expect(l.pos.left).toBe(800);
    expect(l.x).toEqual([{ x: 800, top: 100, bottom: 820 }]);
    // RIGHT edge on its middle: left 500.
    const r = snapEdges({ left: 495, top: 620 }, small, other, VIEW);
    expect(r.pos.left).toBe(500);
    expect(r.x).toEqual([{ x: 800, top: 100, bottom: 820 }]);
    // TOP edge on its middle (y 300), BOTTOM edge on it (top 150): a box
    // 150 tall, so its other edge meets none of the other card's lines.
    const t = snapEdges({ left: 100, top: 307 }, { width: 300, height: 150 }, other, VIEW);
    expect(t.pos.top).toBe(300);
    expect(t.y).toEqual([{ y: 300, left: 100, right: 1000 }]);
    const b = snapEdges({ left: 100, top: 146 }, { width: 300, height: 150 }, other, VIEW);
    expect(b.pos.top).toBe(150);
    expect(b.y).toEqual([{ y: 300, left: 100, right: 1000 }]);
    // This box's MIDDLE on the other's left edge (x 600): left 450; on its
    // bottom edge (y 500): top 400.
    const ml = snapEdges({ left: 455, top: 620 }, small, other, VIEW);
    expect(ml.pos.left).toBe(450);
    expect(ml.x).toEqual([{ x: 600, top: 100, bottom: 820 }]);
    const mb = snapEdges({ left: 100, top: 394 }, small, other, VIEW);
    expect(mb.pos.top).toBe(400);
    expect(mb.y).toEqual([{ y: 500, left: 100, right: 1000 }]);
  });
  test("the app window draws no guide — not its edges, not its middle — and has no pull to its middle (owner, 2026-11-18)", () => {
    // Flush with its edges still (the ruling of 2026-11-17), silently.
    const left = snapEdges({ left: 9, top: 300 }, size, null, VIEW);
    expect(left.pos.left).toBe(0);
    expect(left).toMatchObject({ x: [], y: [] });
    const right = snapEdges({ left: 991, top: 300 }, size, other, VIEW);
    expect(right.pos.left).toBe(VIEW.width - size.width);
    expect(right.x).toEqual([{ x: 1000, top: 100, bottom: 600 }]);
    const bottom = snapEdges({ left: 100, top: 595 }, size, null, VIEW);
    expect(bottom.pos.top).toBe(600);
    expect(bottom.y).toEqual([]);
    // The middle of the window ((1400 − 400) / 2 = 500, (900 − 300) / 2 =
    // 300): 7px off it, the box stays where the drag put it.
    const mid = snapEdges({ left: 507, top: 293 }, size, null, VIEW);
    expect(mid).toEqual({ pos: { left: 507, top: 293 }, x: [], y: [] });
  });
  test("the other card's edge standing on the app window's far edge is drawn inside it", () => {
    // The other card flush with the window's bottom: bottom↔bottom at y 900.
    const low = { left: 600, top: 500, width: 400, height: 400 };
    const s = snapEdges({ left: 100, top: 596 }, size, low, VIEW);
    expect(s.pos.top).toBe(600);
    expect(s.y).toEqual([{ y: VIEW.height - 1, left: 100, right: 1000 }]);
  });
  test("a line the drag put the box on itself is drawn too, with no pull", () => {
    const s = snapEdges({ left: 600, top: 560 }, { width: 300, height: 300 }, other, VIEW);
    expect(s.pos.left).toBe(600);
    expect(s.x).toEqual([{ x: 600, top: 100, bottom: 860 }]);
  });
  test("the result stays inside the viewport: an edge off-screen loses to the viewport's own", () => {
    // The other card hangs 6px off the left: flush with it would be −6.
    const off = { left: -6, top: 100, width: 300, height: 300 };
    const s = snapEdges({ left: 4, top: 560 }, size, off, VIEW);
    expect(s.pos.left).toBe(0);
    // The line it stands on is the app window's, not the card's: no guide.
    expect(s.x).toEqual([]);
    // A viewport nobody knows (a test shim) snaps to the other card alone.
    const open = { width: Infinity, height: Infinity };
    expect(snapEdges({ left: 4, top: 560 }, size, null, open)).toEqual({ pos: { left: 4, top: 560 }, x: [], y: [] });
  });
});

describe("snapResize: a resize's magnet on the edge its grip holds (owner, 2026-11-18)", () => {
  const LIMITS = { minWidth: 280, minHeight: 240, maxWidth: 722 };
  // The other window's card: 600..1000 across, 100..500 down.
  const other = { left: 600, top: 100, width: 400, height: 400 };
  // A floating card, 100..500 across, 550..850 down.
  const start = { left: 100, top: 550, width: 400, height: 300 };
  const float = (edge: Parameters<typeof floatResize>[0]) => (dx: number, dy: number) => floatResize(edge, start, dx, dy, VIEW, LIMITS);
  test("nothing near: the drag's own box, no guide", () => {
    const r = snapResize("r", 30, 0, float("r"), other, VIEW);
    expect(r.box).toEqual({ left: 100, top: 550, width: 430, height: 300 });
    expect(r).toMatchObject({ x: [], y: [] });
  });
  test("a right grip takes the other card's left edge within reach", () => {
    // The right edge at 500 + 93 = 593, 7px short of 600.
    const r = snapResize("r", 93, 0, float("r"), other, VIEW);
    expect(r.box).toEqual({ left: 100, top: 550, width: 500, height: 300 });
    expect(r.x).toEqual([{ x: 600, top: 100, bottom: 850 }]);
    expect(r.y).toEqual([]);
    // 13px short: no pull.
    expect(snapResize("r", 87, 0, float("r"), other, VIEW).box.width).toBe(487);
  });
  test("a top grip takes the other card's bottom edge; a corner grip both axes", () => {
    // The top at 550 − 44 = 506, 6px under the other's bottom (500).
    const t = snapResize("t", 0, -44, float("t"), other, VIEW);
    expect(t.box).toEqual({ left: 100, top: 500, width: 400, height: 350 });
    expect(t.y).toEqual([{ y: 500, left: 100, right: 1000 }]);
    const tr = snapResize("tr", 95, -45, float("tr"), other, VIEW);
    expect(tr.box).toEqual({ left: 100, top: 500, width: 500, height: 350 });
    expect(tr.x).toHaveLength(1);
    expect(tr.y).toHaveLength(1);
  });
  test("only the held edge counts: a height grip draws no vertical line", () => {
    // The left edges would be level at 600 — but the bottom grip holds neither.
    const level = { ...start, left: 600 };
    const r = snapResize("b", 0, 10, (dx, dy) => floatResize("b", level, dx, dy, VIEW, LIMITS), other, VIEW);
    expect(r.x).toEqual([]);
  });
  test("the other card's MIDDLE is a line for the held edge too (owner, 2026-11-18: \"for end but not for middle\")", () => {
    // The other's middle across: x 800. The right edge at 500 + 293 = 793.
    const wide = { ...start, width: 300 };
    const r = snapResize("r", 393, 0, (dx, dy) => floatResize("r", wide, dx, dy, VIEW, LIMITS), other, VIEW);
    expect(r.box).toEqual({ left: 100, top: 550, width: 700, height: 300 });
    expect(r.x).toEqual([{ x: 800, top: 100, bottom: 850 }]);
    // …and down: its middle at y 300. A top grip from 550 up to 305.
    const t = snapResize("t", 0, -245, float("t"), other, VIEW);
    expect(t.box.top).toBe(300);
    expect(t.y).toEqual([{ y: 300, left: 100, right: 1000 }]);
  });
  test("…and this card's own middle meets the other's: the held edge stops there", () => {
    // A card 700..(grip) across, the other's middle at 800: centred on it
    // when the right edge stands at 900. The edge at 894 is 6px short.
    const beside = { left: 700, top: 550, width: 100, height: 300 };
    const lim = { minWidth: 100, minHeight: 240, maxWidth: 722 };
    const r = snapResize("r", 94, 0, (dx, dy) => floatResize("r", beside, dx, dy, VIEW, lim), other, VIEW);
    expect(r.box).toEqual({ left: 700, top: 550, width: 200, height: 300 });
    expect(r.x).toEqual([{ x: 800, top: 100, bottom: 850 }]);
  });
  test("the app window is no line for a resize: flush with its edge draws nothing, and nothing pulls to it", () => {
    const flush = snapResize("l", -100, 0, float("l"), null, VIEW);
    expect(flush.box.left).toBe(0);
    expect(flush).toMatchObject({ x: [], y: [] });
    // 5px short of the window's left edge, the other card far away: no pull.
    const far = { left: 1200, top: 100, width: 150, height: 100 };
    expect(snapResize("l", -95, 0, float("l"), far, VIEW).box.left).toBe(5);
  });
  test("a snap the chassis' clamps refuse does not happen, and draws nothing", () => {
    // The other card's left edge 6px past the widest this card may be.
    const far = { left: 100 + LIMITS.maxWidth + 6, top: 100, width: 300, height: 400 };
    const r = snapResize("r", 322, 0, float("r"), far, VIEW);
    expect(r.box.width).toBe(LIMITS.maxWidth);
    expect(r.x).toEqual([]);
  });
  test("a nearer line the clamps refuse does not hide the next one in reach", () => {
    // The widest this card may be puts its right edge at 100 + 722 = 822,
    // where the drag has it. A slim other card: edges at 814 and 824, its
    // middle at 819. 824 is 2px on — past the maximum, refused — and the
    // middle 3px back: taken.
    const slim = { left: 814, top: 100, width: 10, height: 400 };
    const r = snapResize("r", 322, 0, float("r"), slim, VIEW);
    expect(r.box.width).toBe(719);
    expect(r.x).toEqual([{ x: 819, top: 100, bottom: 850 }]);
  });
  test("the docked drag is one more chassis: the width magnet is its left↔left case", () => {
    // Two cards in the rail's column, right edges level at 1000.
    const row = { left: 0, top: 0, width: 1400, height: 900 };
    const docked = { left: 590, top: 550, width: 410, height: 300 };
    const r = snapResize("l", 4, 0, (dx, dy) => dragResize("l", docked, dx, dy, row), other, VIEW);
    expect(r.box).toEqual({ left: 600, top: 550, width: 400, height: 300 });
    expect(r.x).toEqual([{ x: 600, top: 100, bottom: 850 }]);
  });
  test("a drag the clamps hold back still takes a stop the pointer is near", () => {
    // A left grip pushed right past the minimum width (280): the left edge
    // stands at 220 however far the pointer goes. The other card's left
    // edge at 216 is 4px back from where the POINTER asks (dx 120 → 220),
    // and reachable (width 284).
    const near = { left: 216, top: 100, width: 300, height: 300 };
    const r = snapResize("l", 124, 0, float("l"), near, VIEW);
    expect(r.box).toEqual({ left: 216, top: 550, width: 284, height: 300 });
    expect(r.x).toEqual([{ x: 216, top: 100, bottom: 850 }]);
  });
  test("an axis switched off takes no pull and draws nothing", () => {
    const r = snapResize("tr", 95, -45, float("tr"), other, VIEW, undefined, { x: true, y: false });
    expect(r.box).toEqual({ left: 100, top: 505, width: 500, height: 345 });
    expect(r.x).toHaveLength(1);
    expect(r.y).toEqual([]);
  });
});

describe("dockedBAxes: where a docked window A holds still for window B's resize", () => {
  const mine = { left: 600, top: 500, width: 400, height: 300 };
  test("window A floating, or this card floating: both axes", () => {
    expect(dockedBAxes({ floating: false, aFloats: true, mine, other: null })).toEqual({ x: true, y: true });
    expect(dockedBAxes({ floating: true, aFloats: false, mine, other: null })).toEqual({ x: true, y: true });
  });
  test("both docked, stacked in the rail's column (right edges level): the x axis alone", () => {
    const above = { left: 520, top: 40, width: 480, height: 400 };
    expect(dockedBAxes({ floating: false, aFloats: false, mine, other: above })).toEqual({ x: true, y: false });
  });
  test("both docked, window A beside this card, or no window A: none", () => {
    const beside = { left: 180, top: 40, width: 412, height: 400 };
    expect(dockedBAxes({ floating: false, aFloats: false, mine, other: beside })).toBeNull();
    expect(dockedBAxes({ floating: false, aFloats: false, mine, other: null })).toBeNull();
  });
});
