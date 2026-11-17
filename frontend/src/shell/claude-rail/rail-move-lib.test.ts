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
  dragStartBox,
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
  EDGE_SNAP_PX,
  viewportOf,
  wishSize,
} from "./rail-move-lib";
import { PEEK_MAX_HEIGHT, PEEK_MAX_WIDTH, PEEK_WIDTH } from "./rail-peek-lib";

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
  test("a viewport shrunk under the drag (CodeRabbit on #2329): the held edge follows the pointer, the opposite one stays only while inside", () => {
    const narrow = { width: 500, height: 900 };
    // The right edge (620) is past the viewport's 500: it comes back to it,
    // and the left edge goes where the pointer took it (200 - 100)…
    expect(floatResize("l", start, -100, 0, narrow, lim)).toEqual({ left: 100, top: 100, width: 400, height: 500 });
    // …short of the minimum width.
    expect(floatResize("l", start, -50, 0, narrow, lim)).toEqual({ left: 140, top: 100, width: 360, height: 500 });
    expect(floatResize("l", start, -150, 0, narrow, lim)).toEqual({ left: 50, top: 100, width: 450, height: 500 });
    // The bottom (600) past a 450 viewport: the same for a top grip.
    const short = { width: 1400, height: 450 };
    expect(floatResize("t", start, 0, -60, short, lim)).toEqual({ left: 200, top: 40, width: 420, height: 410 });
    // An opposite edge still inside stays where it was.
    expect(floatResize("l", start, -50, 0, { width: 700, height: 900 }, lim)).toEqual({ left: 150, top: 100, width: 470, height: 500 });
    expect(floatResize("r", start, -30, 0, narrow, lim)).toEqual({ left: 200, top: 100, width: 300, height: 500 });
    // An axis no grip holds is squeezed inside, as the card is drawn.
    expect(floatResize("b", start, 0, 20, narrow, lim)).toEqual({ left: 80, top: 100, width: 420, height: 520 });
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
    expect(snapEdges(free, size, other, VIEW)).toEqual({ pos: free, x: null, y: null });
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
    expect(both.x).not.toBeNull();
    expect(both.y).not.toBeNull();
    const xOnly = snapEdges({ left: 605, top: 560 }, size, other, VIEW);
    expect(xOnly.pos).toEqual({ left: 600, top: 560 });
    expect(xOnly.y).toBeNull();
    // 6px from the other's left, 4px from its right: the right wins.
    expect(snapEdges({ left: 606, top: 560 }, { width: 390, height: 300 }, other, VIEW).pos.left).toBe(610);
  });
  test("the guides run along the shared edge over both cards; the app window's own edge draws none", () => {
    const s = snapEdges({ left: 605, top: 560 }, size, other, VIEW);
    expect(s.x).toEqual({ x: 600, top: 100, bottom: 860 });
    // Snapped to the viewport's edge: the position, and no line (a tall card
    // the clamp holds against it would draw one on every drag).
    const v = snapEdges({ left: 991, top: 300 }, size, null, VIEW);
    expect(v.pos.left).toBe(VIEW.width - size.width);
    expect(v.x).toBeNull();
    const h = snapEdges({ left: 150, top: 106 }, size, other, VIEW);
    expect(h.y).toEqual({ y: 100, left: 150, right: 1000 });
  });
  test("the result stays inside the viewport: an edge off-screen loses to the viewport's own", () => {
    // The other card hangs 6px off the left: flush with it would be −6.
    const off = { left: -6, top: 100, width: 300, height: 300 };
    const s = snapEdges({ left: 4, top: 560 }, size, off, VIEW);
    expect(s.pos.left).toBe(0);
    expect(s.x).toBeNull();
    // A viewport nobody knows (a test shim) snaps to the other card alone.
    const open = { width: Infinity, height: Infinity };
    expect(snapEdges({ left: 4, top: 560 }, size, null, open)).toEqual({ pos: { left: 4, top: 560 }, x: null, y: null });
  });
});

describe("dragStartBox: the head drag's box is the one the card floats at (CodeRabbit on #2329)", () => {
  const drawn = { left: 900, top: 100, width: 400, height: 300 };
  test("a floating card (no float size given): the drawn box", () => {
    expect(dragStartBox(drawn, 950, null, VIEW)).toBe(drawn);
  });
  test("a docked card floating at another size: the top keeps its offset, the grabbed spot its share of the width", () => {
    // Grabbed a quarter in: a quarter in of the 600-wide card too.
    expect(dragStartBox(drawn, 1000, { width: 600, height: 700 }, VIEW)).toEqual({ left: 850, top: 100, width: 600, height: 700 });
    // The same size: the drawn box.
    expect(dragStartBox(drawn, 1000, { width: 400, height: 300 }, VIEW)).toEqual(drawn);
  });
  test("the float size is clamped to the viewport, as the card is drawn (`floatPlacement`)", () => {
    expect(dragStartBox(drawn, 900, { width: 600, height: 2000 }, VIEW)).toEqual({ left: 900, top: 100, width: 600, height: 900 });
  });
});
