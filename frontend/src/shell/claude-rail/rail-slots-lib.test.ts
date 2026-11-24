// FREE PINS (rail-slots-lib.ts header; owner, 2026-11-18; many pins
// 2026-11-24): each rule on its own, then the position store's half — each
// pin's frozen box, the push and the way back, the pins' stacking, and which
// card a magnet reaches for — and the slots of a chat window.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { dropFavPin, setAPinned, setFavPinned } from "@platform/lib/last-chat";
import { botsPulseAnswered, botsRowsNow, publishBots, resetBotsPulseForTests } from "@platform/lib/botsPulse";
import { resetRailExtrasFlagForTests, setRailExtrasForTests } from "./rail-extras-flag";
import {
  commitWindowBox,
  commitWindowPos,
  dockBox,
  freezeFromDock,
  magnetBox,
  noteFrontWindow,
  PIN_Z_TOP,
  pinZ,
  publishPusher,
  publishWindowBox,
  resetRailMoveForTests,
  sizeWindowMark,
  subscribeWindowPos,
  setLiveWindowPos,
  windowBox,
  windowPos,
  windowSize,
} from "./rail-move-store";
import { listCardsNow, listOrphanedNow, publishListCards, resetListHostForTests, withdrawListCards } from "./rail-list-host-store";
import { PEEK_GAP, PEEK_INSET } from "./rail-peek-lib";
import {
  COLUMN_MIN_HEIGHT,
  PUSH_GAP,
  aPinTask,
  aPinTasks,
  defaultPinBox,
  overlaps,
  parsePinMark,
  pinHint,
  pinOf,
  placeColumn,
  pushedPins,
  pushedPos,
  PUSH_MIN_HEIGHT,
  serialisePinBox,
  slotClick,
} from "./rail-slots-lib";
import {
  SLOT_FADE_MS,
  focusPin,
  freeSlot,
  pinSlot,
  registerPinFocus,
  resetRailSlotsForTests,
  slotsNow,
  syncSlots,
} from "./rail-slots-store";

const box = (left: number, top: number, width: number, height: number) => ({ left, top, width, height });

/** The list host store's two answers, read the way its hooks read them. */
function renderHookless() {
  return { cards: listCardsNow, orphaned: listOrphanedNow };
}

describe("a pin's mark", () => {
  it("keeps the frozen box whole, and reads it back", () => {
    const raw = serialisePinBox(box(100.4, 50.6, 420, 640));
    expect(parsePinMark(raw)).toEqual({ pos: { left: 100, top: 51 }, size: { width: 420, height: 640 } });
  });
  it("reads a position with no size — a mark from before free pins", () => {
    expect(parsePinMark('{"left":10,"top":20}')).toEqual({ pos: { left: 10, top: 20 }, size: null });
  });
  it("is nothing for junk, and has no size for one that is not a card's", () => {
    expect(parsePinMark(null)).toBeNull();
    expect(parsePinMark("nope")).toBeNull();
    expect(parsePinMark('{"left":"1","top":2}')).toBeNull();
    expect(parsePinMark('{"left":1,"top":2,"width":3,"height":4}')?.size).toBeNull();
    expect(parsePinMark('{"left":1,"top":2,"width":400}')?.size).toBeNull();
  });
});

describe("a non-pin pushes a pin", () => {
  const size = { width: 400, height: 500 };
  // The non-pin beside the rail: a 420-wide card whose right edge is at 1392.
  const dock = box(972, 60, 420, 640);
  it("leaves a pin that is clear of every non-pin at home, the same object", () => {
    const home = { left: 100, top: 60 };
    expect(pushedPos(home, size, [dock])).toBe(home);
    expect(pushedPos(home, size, [])).toBe(home);
  });
  it("moves a pin under a non-pin to its left, the gap kept, its top untouched", () => {
    // Frozen where the non-pin stood: the common case — pinned, then another ring clicked.
    const home = { left: 992, top: 80 };
    expect(pushedPos(home, size, [dock])).toEqual({ left: 972 - PUSH_GAP - 400, top: 80 });
  });
  it("a pin beside the non-pin, a hair off it, is not covered and does not move", () => {
    const home = { left: 972 - 400 - 2, top: 80 };
    expect(overlaps({ ...home, ...size }, dock)).toBe(false);
    expect(pushedPos(home, size, [dock])).toBe(home);
    // One pixel under it: covered, and pushed clear by the whole gap.
    expect(pushedPos({ left: 972 - 400 + 1, top: 80 }, size, [dock])).toEqual({ left: 972 - PUSH_GAP - 400, top: 80 });
  });
  it("does not move a pin above or below the non-pin", () => {
    const home = { left: 992, top: 60 + 640 + PUSH_GAP };
    expect(pushedPos(home, { width: 400, height: 100 }, [dock])).toBe(home);
  });
  it("UP FIRST: a pin covered from below steps up clear of it, whole", () => {
    const foot = box(1012, 600, 380, 300);
    const home = { left: 1000, top: 300 };
    expect(pushedPos(home, { width: 380, height: 400 }, [foot])).toEqual({ left: 1000, top: 600 - PUSH_GAP - 400 });
  });
  it("…one too tall to clear it goes to the top and is drawn shorter", () => {
    const foot = box(1012, 500, 380, 400);
    const home = { left: 1000, top: 100 };
    expect(pushedPos(home, { width: 380, height: 700 }, [foot])).toEqual({ left: 1000, top: 8, height: 500 - PUSH_GAP - 8 });
    // A pin already above the floor stays at its own top.
    expect(pushedPos({ left: 1000, top: 0 }, { width: 380, height: 700 }, [foot])).toEqual({ left: 1000, top: 0, height: 500 - PUSH_GAP });
  });
  it("…and under the floor height it goes LEFT, whole", () => {
    const foot = box(1012, 200, 380, 700);
    const home = { left: 1000, top: 100 };
    expect(PUSH_MIN_HEIGHT).toBe(240);
    expect(pushedPos(home, { width: 380, height: 700 }, [foot])).toEqual({ left: 1012 - PUSH_GAP - 380, top: 100 });
  });
  it("room above, but the top non-pin stands in it: LEFT from where the step up put it — not left and down", () => {
    const top = box(972, 60, 420, 300);
    const foot = box(1012, 500, 380, 400);
    const home = { left: 1000, top: 380 };
    expect(pushedPos(home, { width: 380, height: 300 }, [top, foot])).toEqual({ left: 972 - PUSH_GAP - 380, top: 500 - PUSH_GAP - 300 });
  });
  it("…a tall one too: left at the top, its own height again", () => {
    const top = box(972, 60, 420, 300);
    const foot = box(1012, 500, 380, 400);
    const home = { left: 1000, top: 380 };
    expect(pushedPos(home, { width: 380, height: 520 }, [top, foot])).toEqual({ left: 972 - PUSH_GAP - 380, top: 8 });
  });
  it("a WIDER foot window: it shifts only past the top one, and stays as tall as the room above the foot one", () => {
    const top = box(1032, 8, 360, 560);
    const foot = box(872, 600, 520, 590);
    const home = { left: 1000, top: 100 };
    // Up from the foot window: top 8, cut to 584. Left past the top window only — still over the foot one's column.
    expect(pushedPos(home, { width: 380, height: 900 }, [top, foot])).toEqual({ left: 1032 - PUSH_GAP - 380, top: 8, height: 600 - PUSH_GAP - 8 });
    // One standing clear above the foot window is under the top one alone: left at its own top, whole.
    expect(pushedPos({ left: 1000, top: 300 }, { width: 380, height: 300 }, [top, foot])).toEqual({ left: 1032 - PUSH_GAP - 380, top: 300 });
  });
  it("under BOTH non-pins at home: the same answer as one after the other", () => {
    const top = box(972, 8, 420, 480);
    const foot = box(1012, 500, 380, 400);
    // Mid-height, lying under the top window's foot and the foot window's top.
    expect(pushedPos({ left: 1000, top: 400 }, { width: 380, height: 300 }, [top, foot])).toEqual({ left: 972 - PUSH_GAP - 380, top: 500 - PUSH_GAP - 300 });
    // Tall: to the top, its own height.
    expect(pushedPos({ left: 1000, top: 120 }, { width: 380, height: 900 }, [top, foot])).toEqual({ left: 972 - PUSH_GAP - 380, top: 8 });
  });
  it("covered by the top non-pin alone, it goes left at its own top", () => {
    const top = box(972, 60, 420, 300);
    const home = { left: 1000, top: 200 };
    expect(pushedPos(home, { width: 380, height: 300 }, [top])).toEqual({ left: 972 - PUSH_GAP - 380, top: 200 });
  });
  it("clears both sections' non-pins, the wider one included", () => {
    const a = box(972, 60, 420, 300);
    const b = box(800, 380, 592, 400);
    const home = { left: 1000, top: 100 };
    // Up from B puts it at the top, cut, under A: left of A is enough — it stands above B there.
    expect(pushedPos(home, size, [a, b])).toEqual({ left: 972 - PUSH_GAP - 400, top: 8, height: 380 - PUSH_GAP - 8 });
  });
  it("with no room to clear it, goes as far left as it can: part of it shows", () => {
    const narrow = box(300, 60, 420, 640);
    const home = { left: 320, top: 80 };
    expect(pushedPos(home, size, [narrow])).toEqual({ left: 0, top: 80 });
    // Already at the edge: nothing to gain, it stays.
    const edge = { left: 0, top: 80 };
    expect(pushedPos(edge, size, [narrow])).toBe(edge);
  });
});

describe("every pin at once", () => {
  const dock = box(972, 60, 420, 640);
  const at = (left: number, top: number, width = 400, height = 500) => ({ pos: { left, top }, size: { width, height } });
  it("a pin the push moved is in the other pin's way: they do not end up stacked", () => {
    // A under the non-pin by the rail; B standing just where A would land.
    const a = at(992, 80);
    const b = at(560, 100);
    const got = pushedPins({ a, b }, [dock]);
    expect(got.a).toEqual({ left: 972 - PUSH_GAP - 400, top: 80 });
    expect(got.b).toEqual({ left: 972 - PUSH_GAP - 400 - PUSH_GAP - 400, top: 100 });
  });
  it("two pins stacked one above the other: only the one the non-pin covers moves", () => {
    // The non-pin by the rail is short; the top pin lies under it, the lower one is clear.
    const short = box(972, 60, 420, 400);
    const top = at(700, 60, 420, 500);
    const low = at(700, 562, 420, 300);
    const got = pushedPins({ a: top, b: low }, [short]);
    expect(got.a).toEqual({ left: 972 - PUSH_GAP - 420, top: 60 });
    // The lower pin only TOUCHES where the top one lands: it does not move.
    expect(got.b).toBe(low.pos);
  });
  it("…and the other way round: the non-pin covers only the LOWER pin, the top one stays", () => {
    const foot = box(1012, 562, 380, 380);
    const top = at(700, 60, 420, 500);
    const low = at(860, 562, 420, 380);
    const got = pushedPins({ a: low, b: top }, [foot]);
    // Up would land it on the top pin: no way up, so left.
    expect(got.a).toEqual({ left: 1012 - PUSH_GAP - 420, top: 562 });
    expect(got.b).toBe(top.pos);
  });
  it("two pins the reader laid over each other stay so, with nothing pushing", () => {
    const a = at(100, 80);
    const b = at(150, 100);
    const got = pushedPins({ a, b }, [dock]);
    expect(got.a).toBe(a.pos);
    expect(got.b).toBe(b.pos);
  });
  it("one pin alone is pushed as before; none: nothing", () => {
    const a = at(992, 80);
    expect(pushedPins({ a }, [dock])).toEqual({ a: { left: 972 - PUSH_GAP - 400, top: 80 } });
    expect(pushedPins({ a, b: null }, [dock])).toEqual({ a: { left: 972 - PUSH_GAP - 400, top: 80 }, b: null });
    expect(pushedPins({}, [dock])).toEqual({});
  });
  it("MANY PINS: a push that moves one pin onto the next moves that one too, nearest the rail first", () => {
    // Three pins in a row, each just where the one nearer the rail lands.
    const p1 = at(992, 80);
    const p2 = at(560, 100);
    const p3 = at(150, 120);
    const got = pushedPins({ "task:1": p1, "task:2": p2, "fav:3": p3 }, [dock]);
    const l1 = 972 - PUSH_GAP - 400;
    const l2 = l1 - PUSH_GAP - 400;
    expect(got["task:1"]).toEqual({ left: l1, top: 80 });
    expect(got["task:2"]).toEqual({ left: l2, top: 100 });
    // No room left of the second one: as far left as the viewport lets it.
    expect(got["fav:3"]).toEqual({ left: 0, top: 120 });
    // The order the pins are named in changes nothing.
    expect(pushedPins({ "fav:3": p3, "task:2": p2, "task:1": p1 }, [dock])).toEqual(got);
  });
  it("…and a pin far from every one of them stays home, the same object", () => {
    const near = at(992, 80);
    const far = at(20, 700, 300, 200);
    const got = pushedPins({ near, far }, [dock]);
    expect(got.far).toBe(far.pos);
  });
});

describe("the push, over 4000 random layouts", () => {
  // A small seeded generator: the same layouts every run.
  let seed = 20261119;
  const rnd = (lo: number, hi: number) => {
    seed = (seed * 1664525 + 1013904223) % 4294967296;
    return Math.round(lo + (seed / 4294967296) * (hi - lo));
  };
  const VIEW = 1600;
  const cover = (p: { left: number; top: number; width: number; height: number }, d: typeof p) => overlaps(p, d);
  it("only a covered pin moves; a moved pin clears what covered it or stands at the left edge; naming the pins the other way changes nothing", () => {
    for (let i = 0; i < 4000; i++) {
      const docks = Array.from({ length: rnd(1, 2) }, () => {
        const width = rnd(360, 722);
        return box(VIEW - 56 - width, rnd(0, 500), width, rnd(240, 900));
      });
      const mk = () => {
        const width = rnd(360, 722);
        return { pos: { left: rnd(0, VIEW - width), top: rnd(0, 600) }, size: { width, height: rnd(240, 900) } };
      };
      const a = mk();
      const b = rnd(0, 3) === 0 ? null : mk();
      const got = pushedPins({ a, b }, docks);
      const swapped = pushedPins({ a: b, b: a }, docks);
      const where = `layout ${i}`;
      // Naming them the other way round gives the same picture.
      expect([where, swapped.b]).toEqual([where, got.a]);
      expect([where, swapped.a]).toEqual([where, got.b]);
      const pins = [
        { pin: a, at: got.a! },
        ...(b ? [{ pin: b, at: got.b! }] : []),
      ];
      const boxOf = (p: typeof a, at: { left: number; top: number; height?: number }) => ({
        left: at.left,
        top: at.top,
        width: p.size.width,
        height: at.height ?? p.size.height,
      });
      for (const [n, { pin, at }] of pins.entries()) {
        const home = boxOf(pin, pin.pos);
        const now = boxOf(pin, at);
        const other = pins[1 - n];
        const moved = at !== pin.pos;
        // Never down, never right; cut short only in its own column, never
        // under the floor height.
        expect([where, at.top <= pin.pos.top, at.left <= pin.pos.left]).toEqual([where, true, true]);
        if (at.height !== undefined) {
          expect([where, now.height < pin.size.height && now.height >= Math.min(pin.size.height, PUSH_MIN_HEIGHT)]).toEqual([where, true]);
        }
        // A step up alone never lands it on the other pin, unless the reader had laid them so.
        if (moved && at.left === pin.pos.left && other && !cover(home, boxOf(other.pin, other.pin.pos)))
          expect([where, cover(now, boxOf(other.pin, other.at))]).toEqual([where, false]);
        const underDock = docks.some((d) => cover(home, d));
        const underMovedPin =
          !!other && other.at !== other.pin.pos && !cover(home, boxOf(other.pin, other.pin.pos)) && cover(home, boxOf(other.pin, other.at));
        // A pin nothing covers stays exactly where it is.
        if (!underDock && !underMovedPin) expect([where, moved]).toEqual([where, false]);
        // A pin that moved is clear of every non-pin, or went as far as the edge.
        if (moved) expect([where, at.left === 0 || !docks.some((d) => cover(now, d))]).toEqual([where, true]);
        // A pin under a non-pin never stays put while there is room to its left.
        if (underDock && pin.pos.left > 0) expect([where, moved]).toEqual([where, true]);
      }
    }
  });
});

describe("one column for the two non-pins", () => {
  const rail = box(1400, 40, 48, 800);
  const row = box(0, 40, 1448, 800);
  const ring = box(1408, 100, 32, 32);
  it("stands at its ring with no section B card up", () => {
    const got = placeColumn(ring, rail, row, null, { width: 420, maxHeight: 640 });
    expect(got).toEqual({ left: 1400 - PEEK_GAP - 420, top: 100, width: 420, height: 640 });
  });
  it("stands ABOVE section B's card, its height cut to what the column leaves — never beside it", () => {
    const b = box(1012, 440, 380, 392);
    const got = placeColumn(ring, rail, row, b, { width: 420, maxHeight: 640 })!;
    expect(got.left + got.width).toBe(1400 - PEEK_GAP);
    expect(got.top + got.height).toBeLessThanOrEqual(b.top - PEEK_GAP - PEEK_INSET);
    expect(got.height).toBe(440 - PEEK_GAP - 40 - 2 * PEEK_INSET);
  });
  it("stands over it only when the column leaves less than the floor", () => {
    const tall = box(1012, 40 + COLUMN_MIN_HEIGHT, 380, 600);
    const got = placeColumn(ring, rail, row, tall, { width: 420, maxHeight: 640 })!;
    expect(got.height).toBe(640);
    expect(got.left + got.width).toBe(1400 - PEEK_GAP);
  });
});

describe("a pin with no box of its own", () => {
  const view = { width: 1600, height: 950 };
  const rail = box(1552, 0, 48, 950);
  it("window A's stands at the top of the rail, the list card's size", () => {
    expect(defaultPinBox("a", rail, view)).toEqual({ left: 1552 - PEEK_GAP - 420, top: PEEK_INSET, width: 420, height: 640 });
  });
  it("window B's stands on the rail's foot, the favourite card's size", () => {
    expect(defaultPinBox("b", rail, view)).toEqual({ left: 1552 - PEEK_GAP - 380, top: 950 - PEEK_INSET - 520, width: 380, height: 520 });
  });
  it("with no rail it stands at the viewport's right edge, and always inside it", () => {
    const got = defaultPinBox("a", null, { width: 400, height: 300 });
    expect(got.left).toBeGreaterThanOrEqual(0);
    expect(got.left + got.width).toBeLessThanOrEqual(400);
    expect(got.top + got.height).toBeLessThanOrEqual(300);
  });
});

describe("the pin is the task's", () => {
  it("names section A's pinned task off its mark", () => {
    expect(aPinTask("task:abc")).toBe("abc");
    expect(aPinTask("tasks")).toBeNull();
    expect(aPinTask("task:")).toBeNull();
    expect(aPinTask(null)).toBeNull();
  });
  it("names every pinned task among section A's pins, in order", () => {
    expect(aPinTasks(["git", "task:k2", "plus", "task:k1", "mode:read"])).toEqual(["k2", "k1"]);
    expect(aPinTasks([])).toEqual([]);
  });
  it("finds the pinned window that shows a task, in either section, among many", () => {
    const marks = { aWords: ["tasks", "task:k1", "task:k4"], favKeys: ["k2", "k5"] };
    expect(pinOf("k1", marks)).toBe("a");
    expect(pinOf("k4", marks)).toBe("a");
    expect(pinOf("k5", marks)).toBe("b");
    expect(pinOf("k3", marks)).toBeNull();
    expect(pinOf("tasks", marks)).toBeNull();
  });
  it("a ring's click raises a pinned task, keeps its own open card, opens anything else", () => {
    expect(slotClick({ pinned: true, ownOpen: false })).toBe("raise");
    expect(slotClick({ pinned: true, ownOpen: true })).toBe("raise");
    expect(slotClick({ pinned: false, ownOpen: true })).toBe("keep");
    expect(slotClick({ pinned: false, ownOpen: false })).toBe("open");
  });
});

describe("pinning replaces nothing (many pins, 2026-11-24)", () => {
  it("the pin icon says Pin on a non-pin and Close on a pin — never what it would replace", () => {
    expect(pinHint({ pinned: true })).toBe("Close");
    expect(pinHint({ pinned: false })).toBe("Pin");
  });
});

describe("the position store, free pins on", () => {
  // Section A's pin and section B's, by their pins' ids (last-chat).
  const A = "task:k1";
  const B = "fav:f1";
  beforeEach(() => {
    resetRailMoveForTests();
    resetRailSlotsForTests();
    setRailExtrasForTests(true);
    setAPinned(A);
    setFavPinned("f1");
  });
  afterEach(async () => {
    setAPinned(null);
    setFavPinned(null);
    // The marks' own settle (last-chat `settlePos`, a 0ms timer) runs here,
    // not in whatever suite comes next in this process.
    await new Promise((r) => setTimeout(r, 0));
    resetRailMoveForTests();
    resetRailExtrasFlagForTests();
  });

  it("the pin's press freezes the non-pin card's box: position and size, under the pin's own id", () => {
    publishWindowBox("a", "list", box(972, 60, 420, 640));
    expect(freezeFromDock("a", A)).toBe(true);
    expect(windowPos(A)).toEqual({ left: 972, top: 60 });
    expect(windowSize(A)).toEqual({ width: 420, height: 640 });
    // The window itself holds nothing: a pin's box is its own.
    expect(windowPos("a")).toBeNull();
  });
  it("freezes nothing with no card up", () => {
    expect(freezeFromDock("a", A)).toBe(false);
    expect(windowPos(A)).toBeNull();
  });
  it("MANY PINS: each pin keeps its own box — a second pin in the same section changes nothing of the first", () => {
    publishWindowBox("a", "list", box(972, 60, 420, 640));
    freezeFromDock("a", A);
    commitWindowPos(A, { left: 100, top: 90 });
    publishWindowBox("a", "list", box(972, 300, 420, 500));
    expect(freezeFromDock("a", "tasks")).toBe(true);
    expect(windowPos(A)).toEqual({ left: 100, top: 90 });
    expect(windowSize(A)).toEqual({ width: 420, height: 640 });
    expect(windowPos("tasks")).toEqual({ left: 972, top: 300 });
    expect(windowSize("tasks")).toEqual({ width: 420, height: 500 });
    // A drag on one is that one's alone.
    setLiveWindowPos("tasks", { left: 500, top: 400 });
    expect(windowPos(A)).toEqual({ left: 100, top: 90 });
    expect(windowPos("tasks")).toEqual({ left: 500, top: 400 });
  });
  it("last pressed on top: a pin's press raises it, the others keep their order", () => {
    commitWindowBox(A, box(100, 100, 400, 500));
    commitWindowBox(B, box(200, 150, 400, 500));
    commitWindowBox("git", box(300, 200, 400, 500));
    noteFrontWindow("a", A);
    noteFrontWindow("b", B);
    noteFrontWindow("a", "git");
    expect([pinZ("git"), pinZ(B), pinZ(A)]).toEqual([PIN_Z_TOP, PIN_Z_TOP - 1, PIN_Z_TOP - 2]);
    // A press on the bottom one: on top, and the other two keep theirs.
    noteFrontWindow("a", A);
    expect([pinZ(A), pinZ("git"), pinZ(B)]).toEqual([PIN_Z_TOP, PIN_Z_TOP - 1, PIN_Z_TOP - 2]);
    // A new pin's freeze puts it on top.
    publishWindowBox("b", "bot", box(1012, 500, 380, 400));
    freezeFromDock("b", "bot:b1");
    expect(pinZ("bot:b1")).toBe(PIN_Z_TOP);
    expect(pinZ(A)).toBe(PIN_Z_TOP - 1);
  });
  it("tells a section's pins from its non-pin; a pin's magnet is the NEAREST other pin, any section", () => {
    publishWindowBox("a", "pin", box(100, 100, 400, 500), A);
    publishWindowBox("a", "list", box(972, 60, 420, 640));
    publishWindowBox("b", "favpin", box(560, 620, 380, 300), B);
    expect(dockBox("a")).toEqual(box(972, 60, 420, 640));
    expect(windowBox("a")).toEqual(box(100, 100, 400, 500));
    // A docked card lines up with the other section's non-pin.
    expect(magnetBox("a", false)).toEqual(box(972, 60, 420, 640));
    // A floating card with no box of its own: the other section's newest pin.
    expect(magnetBox("a", true)).toEqual(box(100, 100, 400, 500));
    expect(magnetBox("b", true)).toEqual(box(560, 620, 380, 300));
    // A pin being dragged: the nearest pin but itself, whichever section.
    const third = box(520, 100, 300, 300);
    expect(magnetBox("b", true, { self: "git", box: third })).toEqual(box(100, 100, 400, 500));
    expect(magnetBox("b", true, { self: "git", box: box(560, 560, 300, 50) })).toEqual(box(560, 620, 380, 300));
    // Never itself.
    expect(magnetBox("b", true, { self: A, box: box(100, 100, 400, 500) })).toEqual(box(560, 620, 380, 300));
  });
  it("the card being pinned stops pushing at the freeze: the pin is not drawn clear of its own old box", () => {
    // The non-pin, opened by a click: beside the rail, and a pusher.
    publishWindowBox("a", "list", box(972, 60, 420, 640));
    publishPusher("a", "list", box(972, 60, 420, 640));
    // …while the other section's non-pin stands too.
    publishPusher("b", "fav", box(1012, 720, 380, 200));
    freezeFromDock("a", A);
    // Frozen exactly where it stood — not pushed left of the box it had.
    expect(windowPos(A)).toEqual({ left: 972, top: 60 });
    // The other section's non-pin still pushes what it covers — left: a step up would land it on section A's pin.
    commitWindowBox(B, box(1000, 700, 380, 200));
    expect(windowPos(B)).toEqual({ left: 1012 - PUSH_GAP - 380, top: 700 });
  });
  it("a clicked-open non-pin pushes the pin, and its going puts it back home", () => {
    commitWindowBox(A, box(972, 60, 420, 640));
    const home = windowPos(A);
    publishPusher("a", "list", box(972, 60, 420, 640));
    expect(windowPos(A)).toEqual({ left: 972 - PUSH_GAP - 420, top: 60 });
    // The same answer is the same object: readers re-render on a real change only.
    expect(windowPos(A)).toBe(windowPos(A));
    // Nothing was written: the size and the home are as frozen.
    expect(windowSize(A)).toEqual({ width: 420, height: 640 });
    publishPusher("a", "list", null);
    expect(windowPos(A)).toBe(home);
  });
  it("MANY PINS: one non-pin pushes every pin it covers, and a pin it moved pushes the next", () => {
    commitWindowBox(A, box(992, 80, 400, 500));
    commitWindowBox("tasks", box(560, 100, 400, 500));
    commitWindowBox(B, box(20, 800, 300, 100));
    publishPusher("a", "list", box(972, 60, 420, 640));
    const l1 = 972 - PUSH_GAP - 400;
    expect(windowPos(A)).toEqual({ left: l1, top: 80 });
    expect(windowPos("tasks")).toEqual({ left: l1 - PUSH_GAP - 400, top: 100 });
    expect(windowPos(B)).toEqual({ left: 20, top: 800 });
    // Pinning the non-pin freezes the whole picture: both pushed pins stay where they are drawn.
    publishWindowBox("a", "list", box(972, 60, 420, 640));
    freezeFromDock("a", "git");
    publishPusher("a", "list", null);
    expect(windowPos(A)).toEqual({ left: l1, top: 80 });
    expect(windowPos("tasks")).toEqual({ left: l1 - PUSH_GAP - 400, top: 100 });
    expect(windowPos("git")).toEqual({ left: 972, top: 60 });
  });
  it("a pin cut short keeps its own size in its mark: the non-pin goes, and it is whole at home", () => {
    commitWindowBox(A, box(972, 60, 420, 800));
    const home = windowPos(A);
    publishPusher("b", "fav", box(1012, 440, 380, 480));
    expect(windowSize(A)).toEqual({ width: 420, height: 440 - PUSH_GAP - 8 });
    // The list's non-pin opens at the top too: left from where it was pushed up to — and whole again.
    publishPusher("a", "list", box(972, 60, 420, 300));
    expect(windowPos(A)).toEqual({ left: 972 - PUSH_GAP - 420, top: 8 });
    expect(windowSize(A)).toEqual({ width: 420, height: 800 });
    publishPusher("a", "list", null);
    publishPusher("b", "fav", null);
    expect(windowPos(A)).toBe(home);
    expect(windowSize(A)).toEqual({ width: 420, height: 800 });
  });
  it("a cut pin's drag ends at its own size, not the cut one", () => {
    commitWindowBox(A, box(972, 60, 420, 800));
    publishPusher("b", "fav", box(1012, 440, 380, 480));
    commitWindowPos(A, { left: 40, top: 70 });
    expect(windowSize(A)).toEqual({ width: 420, height: 800 });
  });
  it("a drag is never pushed, and where it ends is home — the size rides on", () => {
    commitWindowBox(A, box(972, 60, 420, 640));
    publishPusher("a", "list", box(972, 60, 420, 640));
    setLiveWindowPos(A, { left: 990, top: 70 });
    expect(windowPos(A)).toEqual({ left: 990, top: 70 });
    commitWindowPos(A, { left: 40, top: 70 });
    expect(windowPos(A)).toEqual({ left: 40, top: 70 });
    expect(windowSize(A)).toEqual({ width: 420, height: 640 });
  });
  it("a pin with no size in its mark is pushed by its card's live box", () => {
    commitWindowPos(A, { left: 992, top: 80 });
    expect(windowSize(A)).toBeNull();
    publishPusher("a", "list", box(972, 60, 420, 640));
    // No size known: it cannot be told whether it lies under the non-pin.
    expect(windowPos(A)).toEqual({ left: 992, top: 80 });
    publishWindowBox("a", "pin", box(992, 80, 400, 500), A);
    publishPusher("a", "list", box(972, 60, 420, 641));
    expect(windowPos(A)).toEqual({ left: 972 - PUSH_GAP - 400, top: 80 });
  });
  it("pinning freezes the whole picture: a pin this card pushed aside stays where it is drawn", () => {
    // Section A's pin, tall, by the rail; section B's non-pin opens over its foot: the pin goes up, cut short.
    commitWindowBox(A, box(972, 60, 420, 800));
    publishWindowBox("b", "fav", box(1012, 440, 380, 480));
    publishPusher("b", "fav", box(1012, 440, 380, 480));
    const cut = 440 - PUSH_GAP - 8;
    expect(windowPos(A)).toEqual({ left: 972, top: 8, height: cut });
    // Drawn at the cut height; the same object while nothing changes.
    expect(windowSize(A)).toEqual({ width: 420, height: cut });
    expect(windowSize(A)).toBe(windowSize(A));
    // The favourite is pinned: it stops pushing — and section A's pin does NOT grow back under it.
    freezeFromDock("b", B);
    expect(windowPos(A)).toEqual({ left: 972, top: 8 });
    expect(windowSize(A)).toEqual({ width: 420, height: cut });
    expect(windowPos(B)).toEqual({ left: 1012, top: 440 });
  });
  it("…with both non-pins up, what is frozen is the box drawn — not the one this card alone would give", () => {
    commitWindowBox(A, box(972, 60, 420, 800));
    publishWindowBox("b", "fav", box(1012, 440, 380, 480));
    publishPusher("b", "fav", box(1012, 440, 380, 480));
    publishPusher("a", "list", box(972, 60, 420, 300));
    // The top window blocks the step up: left, whole.
    const drawn = { left: 972 - PUSH_GAP - 420, top: 8 };
    expect(windowPos(A)).toEqual(drawn);
    expect(windowSize(A)).toEqual({ width: 420, height: 800 });
    freezeFromDock("b", B);
    expect(windowPos(A)).toEqual(drawn);
    expect(windowSize(A)).toEqual({ width: 420, height: 800 });
    // The list window goes too: the pin stays where it was drawn, whole.
    publishPusher("a", "list", null);
    expect(windowPos(A)).toEqual(drawn);
    expect(windowSize(A)).toEqual({ width: 420, height: 800 });
  });
  it("…but a pin pushed by its OWN section's non-pin still goes home when that closes", () => {
    commitWindowBox(B, box(1012, 440, 380, 480));
    publishPusher("b", "fav", box(1012, 440, 380, 480));
    publishWindowBox("a", "list", box(972, 60, 420, 300));
    publishPusher("a", "list", box(972, 60, 420, 300));
    // Section A's card is pinned: only what IT pushed is frozen — section B's pin is pushed by B's own non-pin.
    freezeFromDock("a", A);
    publishPusher("b", "fav", null);
    expect(windowPos(B)).toEqual({ left: 1012, top: 440 });
  });
  it("a pin with a position and no size gets one, once; a sized mark is left alone", () => {
    commitWindowPos(A, { left: 300, top: 80 });
    expect(windowSize(A)).toBeNull();
    sizeWindowMark(A, { width: 420, height: 640 });
    expect(windowPos(A)).toEqual({ left: 300, top: 80 });
    expect(windowSize(A)).toEqual({ width: 420, height: 640 });
    sizeWindowMark(A, { width: 500, height: 500 });
    expect(windowSize(A)).toEqual({ width: 420, height: 640 });
    // No pin, nothing to size.
    sizeWindowMark(B, { width: 380, height: 520 });
    expect(windowPos(B)).toBeNull();
  });
  it("a size-less pin's card box arriving after the non-pin is heard: the pin is pushed then", () => {
    commitWindowPos(A, { left: 992, top: 80 });
    publishPusher("a", "list", box(972, 60, 420, 640));
    let heard = 0;
    const off = subscribeWindowPos(() => heard++);
    publishWindowBox("a", "pin", box(992, 80, 400, 500), A);
    expect(heard).toBe(1);
    expect(windowPos(A)).toEqual({ left: 972 - PUSH_GAP - 400, top: 80 });
    // The same publication again says nothing.
    publishWindowBox("a", "pin", box(992, 80, 400, 500), A);
    expect(heard).toBe(1);
    off();
  });
  it("a pin let go has its box cleared a moment later; one still pinned keeps its own", async () => {
    commitWindowBox(A, box(972, 60, 420, 640));
    commitWindowBox(B, box(100, 100, 380, 480));
    dropFavPin("f1");
    // Its card fades out reading the box for that moment.
    expect(windowPos(B)).toEqual({ left: 100, top: 100 });
    await new Promise((r) => setTimeout(r, 0));
    expect(windowPos(B)).toBeNull();
    expect(windowPos(A)).toEqual({ left: 972, top: 60 });
  });
  it("off, nothing is pushed and no size is read: one position per window", () => {
    setRailExtrasForTests(false);
    commitWindowBox("a", box(972, 60, 420, 640));
    publishPusher("a", "list", box(972, 60, 420, 640));
    expect(windowPos("a")).toEqual({ left: 972, top: 60 });
    expect(windowSize("a")).toBeNull();
    publishWindowBox("a", "pin", box(1, 1, 400, 500), A);
    publishWindowBox("a", "list", box(972, 60, 420, 640));
    // The one card a window has, whoever published last.
    expect(magnetBox("a", true)).toEqual(box(1, 1, 400, 500));
  });
});

describe("a pin lets go only on an ANSWERED feed", () => {
  it("the bots' rows are not an answer until one has landed in this document", () => {
    resetBotsPulseForTests();
    expect(botsPulseAnswered()).toBe(false);
    expect(botsRowsNow()).toEqual([]);
    // An answer with no bots at all: "none left", which an unanswered empty list never says.
    publishBots([]);
    expect(botsPulseAnswered()).toBe(true);
    expect(botsRowsNow()).toEqual([]);
    resetBotsPulseForTests();
    expect(botsPulseAnswered()).toBe(false);
  });
});

describe("the list card's slots in the shell's host", () => {
  beforeEach(() => {
    resetRailSlotsForTests();
    resetListHostForTests();
  });
  const card = (id: string, pin: string | null, node: string) => ({ id, pin, node });
  it("a rail going takes its non-pin and leaves every pinned slot; only the last publisher counts", () => {
    const railA = {};
    const railB = {};
    const hear = renderHookless();
    publishListCards(railA, [card("s1", "k1", "pin-1"), card("s2", "k2", "pin-2"), card("s3", null, "free-A")]);
    expect(hear.cards().map((c) => c.node)).toEqual(["pin-1", "pin-2", "free-A"]);
    expect(hear.orphaned()).toBe(false);
    // The next page's rail publishes before the last one's cleanup runs.
    publishListCards(railB, [card("s1", "k1", "pin-1"), card("s2", "k2", "pin-2"), card("s3", null, "free-B")]);
    withdrawListCards(railA);
    expect(hear.cards().map((c) => c.node)).toEqual(["pin-1", "pin-2", "free-B"]);
    // …and a page with no rail: the free slot goes, the pinned ones stay, orphaned.
    withdrawListCards(railB);
    expect(hear.cards().map((c) => c.node)).toEqual(["pin-1", "pin-2"]);
    expect(hear.orphaned()).toBe(true);
  });
});

describe("the slots of a chat window (many pins, 2026-11-24)", () => {
  beforeEach(() => resetRailSlotsForTests());
  const pins = (g: "a" | "fav" | "bot") => slotsNow(g).map((s) => [s.id, s.pin, s.closing]);
  it("the pin's press hands the free slot the pin and makes a fresh free slot — no other slot changes", () => {
    expect(freeSlot("a")).toBe("x");
    pinSlot("a", "x", "k1");
    const second = freeSlot("a");
    expect(second).not.toBe("x");
    pinSlot("a", second, "k2");
    const third = freeSlot("a");
    expect(pins("a")).toEqual([
      ["x", "k1", false],
      [second, "k2", false],
      [third, null, false],
    ]);
    // A slot that is not free, or a pin some open slot shows already, changes nothing.
    pinSlot("a", "x", "k3");
    pinSlot("a", third, "k1");
    expect(freeSlot("a")).toBe(third);
    // Each kind of card has its own list.
    expect(freeSlot("fav")).toBe("x");
    expect(freeSlot("bot")).toBe("x");
  });
  it("a pin let go fades: its slot is closing, then dropped — the others stand", async () => {
    pinSlot("fav", "x", "k1");
    pinSlot("fav", freeSlot("fav"), "k2");
    syncSlots("fav", ["k2"]);
    expect(slotsNow("fav").find((s) => s.pin === "k1")?.closing).toBe(true);
    expect(slotsNow("fav").find((s) => s.pin === "k2")?.closing).toBe(false);
    await new Promise((r) => setTimeout(r, SLOT_FADE_MS + 20));
    expect(slotsNow("fav").some((s) => s.pin === "k1")).toBe(false);
    expect(slotsNow("fav").filter((s) => s.pin === null)).toHaveLength(1);
  });
  it("…and one pinned again while it fades stays, the same slot", () => {
    pinSlot("a", "x", "k1");
    syncSlots("a", []);
    expect(slotsNow("a")[0]).toEqual({ id: "x", pin: "k1", closing: true });
    syncSlots("a", ["k1"]);
    expect(slotsNow("a")[0]).toEqual({ id: "x", pin: "k1", closing: false });
  });
  it("rebuilt from the marks (a reload): one slot per pin before the free one, in the marks' order", () => {
    syncSlots("bot", ["b1", "b2"]);
    const list = slotsNow("bot");
    expect(list.map((s) => s.pin)).toEqual(["b1", "b2", null]);
    expect(list[2]!.id).toBe("x");
    // Asked again with the same marks: nothing changes, the same list.
    syncSlots("bot", ["b1", "b2"]);
    expect(slotsNow("bot")).toBe(list);
  });
  it("the keyboard lands in the pinned window that registered, by its pin's id", () => {
    expect(focusPin("task:k1")).toBe(false);
    const off = registerPinFocus("task:k1", () => true);
    const off2 = registerPinFocus("fav:k2", () => true);
    expect(focusPin("task:k1")).toBe(true);
    expect(focusPin("fav:k2")).toBe(true);
    expect(focusPin("bot:b")).toBe(false);
    off();
    expect(focusPin("task:k1")).toBe(false);
    expect(focusPin("fav:k2")).toBe(true);
    off2();
  });
});
