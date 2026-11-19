// FREE PINS (rail-slots-lib.ts header; owner, 2026-11-18): each rule on its
// own, then the position store's half — the frozen box, the push and the way
// back, and which card a magnet reaches for.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { publishTaskHeadUnified, resetTaskHeadUnifiedFlagForTests } from "@shell/task-head-flag";
import { setAPinned, setFavPinned } from "@platform/lib/last-chat";
import { botsPulseAnswered, botsRowsNow, publishBots, resetBotsPulseForTests } from "@platform/lib/botsPulse";
import { resetRailFreePinsFlagForTests, setRailFreePinsForTests, railFreePinsNow } from "./rail-free-pins-flag";
import {
  commitWindowBox,
  commitWindowPos,
  dockBox,
  freezeFromDock,
  magnetBox,
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
  defaultPinBox,
  doomedBy,
  overlaps,
  parsePinMark,
  pinHint,
  pinOf,
  placeColumn,
  pushedPins,
  pushedPos,
  sectionPinned,
  serialisePinBox,
  slotClick,
} from "./rail-slots-lib";
import {
  doomedWindow,
  focusPin,
  freeSlot,
  otherSlot,
  pinTitle,
  registerPinFocus,
  resetRailSlotsForTests,
  setDoomedWindow,
  setFreeSlot,
  setPinTitle,
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
  it("counts the gap: a pin a hair off the non-pin is still pushed clear", () => {
    const home = { left: 972 - 400 - 2, top: 80 };
    expect(overlaps({ ...home, ...size }, dock)).toBe(false);
    expect(pushedPos(home, size, [dock])).toEqual({ left: 972 - PUSH_GAP - 400, top: 80 });
  });
  it("does not move a pin above or below the non-pin", () => {
    const home = { left: 992, top: 60 + 640 + PUSH_GAP };
    expect(pushedPos(home, { width: 400, height: 100 }, [dock])).toBe(home);
  });
  it("clears both sections' non-pins, the wider one included", () => {
    const a = box(972, 60, 420, 300);
    const b = box(800, 380, 592, 400);
    const home = { left: 1000, top: 100 };
    // Left of A it would still lie over B (taller than A's foot): left of B too.
    expect(pushedPos(home, size, [a, b])).toEqual({ left: 800 - PUSH_GAP - 400, top: 100 });
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

describe("both pins at once", () => {
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
  it("two pins the reader laid over each other stay so, with nothing pushing", () => {
    const a = at(100, 80);
    const b = at(150, 100);
    const got = pushedPins({ a, b }, [dock]);
    expect(got.a).toBe(a.pos);
    expect(got.b).toBe(b.pos);
  });
  it("one pin alone is pushed as before; none: nothing", () => {
    const a = at(992, 80);
    expect(pushedPins({ a }, [dock])).toEqual({ a: { left: 972 - PUSH_GAP - 400, top: 80 }, b: null });
    expect(pushedPins({}, [dock])).toEqual({ a: null, b: null });
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
  it("finds the pinned window that shows a task, in either section", () => {
    expect(pinOf("k1", { aWord: "task:k1", favKey: null })).toBe("a");
    expect(pinOf("k2", { aWord: "task:k1", favKey: "k2" })).toBe("b");
    expect(pinOf("k3", { aWord: "task:k1", favKey: "k2" })).toBeNull();
    expect(pinOf("tasks", { aWord: "tasks", favKey: null })).toBeNull();
  });
  it("knows whether a section has a pin", () => {
    expect(sectionPinned("a", { aWord: "git", favKey: null, botId: null })).toBe(true);
    expect(sectionPinned("a", { aWord: null, favKey: "k", botId: "b" })).toBe(false);
    expect(sectionPinned("b", { aWord: "git", favKey: null, botId: "b1" })).toBe(true);
    expect(sectionPinned("b", { aWord: "git", favKey: null, botId: null })).toBe(false);
  });
  it("a ring's click raises a pinned task, keeps its own open card, opens anything else", () => {
    expect(slotClick({ pinned: true, ownOpen: false })).toBe("raise");
    expect(slotClick({ pinned: true, ownOpen: true })).toBe("raise");
    expect(slotClick({ pinned: false, ownOpen: true })).toBe("keep");
    expect(slotClick({ pinned: false, ownOpen: false })).toBe("open");
  });
});

describe("pinning a non-pin replaces its section's pin", () => {
  it("the pin icon says what goes, by name when it has one", () => {
    expect(pinHint({ pinned: true, replaces: "x" })).toBe("Close");
    expect(pinHint({ pinned: false, replaces: false })).toBe("Pin");
    expect(pinHint({ pinned: false, replaces: "Fix the build" })).toBe("Pin — replaces “Fix the build”");
    expect(pinHint({ pinned: false, replaces: null })).toBe("Pin — replaces the pinned window");
    expect(pinHint({ pinned: false, replaces: "  " })).toBe("Pin — replaces the pinned window");
  });
  it("marks the section's pinned window only from a non-pin card's icon", () => {
    expect(doomedBy({ win: "a", cardPinned: false, sectionPinned: true })).toBe("a");
    expect(doomedBy({ win: "b", cardPinned: false, sectionPinned: true })).toBe("b");
    expect(doomedBy({ win: "a", cardPinned: true, sectionPinned: true })).toBeNull();
    expect(doomedBy({ win: "a", cardPinned: false, sectionPinned: false })).toBeNull();
  });
});

describe("the position store, free pins on", () => {
  beforeEach(() => {
    resetRailMoveForTests();
    resetRailSlotsForTests();
    publishTaskHeadUnified({ enabled: true });
    setRailFreePinsForTests(true);
    setAPinned("task:k1");
    setFavPinned(null);
  });
  afterEach(async () => {
    setAPinned(null);
    setFavPinned(null);
    // The marks' own settle (last-chat `settlePos`, a 0ms timer) runs here,
    // not in whatever suite comes next in this process.
    await new Promise((r) => setTimeout(r, 0));
    resetRailMoveForTests();
    resetRailFreePinsFlagForTests();
    resetTaskHeadUnifiedFlagForTests();
  });

  it("is only on with the unified head's switch", () => {
    expect(railFreePinsNow()).toBe(true);
    publishTaskHeadUnified({ enabled: false });
    expect(railFreePinsNow()).toBe(false);
  });
  it("the pin's press freezes the non-pin card's box: position and size", () => {
    publishWindowBox("a", "list", box(972, 60, 420, 640));
    expect(freezeFromDock("a")).toBe(true);
    expect(windowPos("a")).toEqual({ left: 972, top: 60 });
    expect(windowSize("a")).toEqual({ width: 420, height: 640 });
  });
  it("freezes nothing with no card up", () => {
    expect(freezeFromDock("a")).toBe(false);
    expect(windowPos("a")).toBeNull();
  });
  it("tells a window's pin from its non-pin", () => {
    publishWindowBox("a", "pin", box(100, 100, 400, 500), true);
    publishWindowBox("a", "list", box(972, 60, 420, 640));
    expect(dockBox("a")).toEqual(box(972, 60, 420, 640));
    expect(windowBox("a")).toEqual(box(100, 100, 400, 500));
    // A floating card lines up with the other section's pin, a docked one with its non-pin.
    expect(magnetBox("a", true)).toEqual(box(100, 100, 400, 500));
    expect(magnetBox("a", false)).toEqual(box(972, 60, 420, 640));
    expect(magnetBox("b", true)).toBeNull();
  });
  it("the card being pinned stops pushing at the freeze: the pin is not drawn clear of its own old box", () => {
    // The non-pin, opened by a click: beside the rail, and a pusher.
    publishWindowBox("a", "list", box(972, 60, 420, 640));
    publishPusher("a", "list", box(972, 60, 420, 640));
    // …while the other section's non-pin stands too.
    publishPusher("b", "fav", box(1012, 720, 380, 200));
    freezeFromDock("a");
    // Frozen exactly where it stood — not pushed left of the box it had.
    expect(windowPos("a")).toEqual({ left: 972, top: 60 });
    // The other section's non-pin still pushes what it covers.
    commitWindowBox("b", box(1000, 700, 380, 200));
    expect(windowPos("b")).toEqual({ left: 1012 - PUSH_GAP - 380, top: 700 });
  });
  it("a clicked-open non-pin pushes the pin, and its going puts it back home", () => {
    commitWindowBox("a", box(972, 60, 420, 640));
    const home = windowPos("a");
    publishPusher("a", "list", box(972, 60, 420, 640));
    expect(windowPos("a")).toEqual({ left: 972 - PUSH_GAP - 420, top: 60 });
    // The same answer is the same object: readers re-render on a real change only.
    expect(windowPos("a")).toBe(windowPos("a"));
    // Nothing was written: the size and the home are as frozen.
    expect(windowSize("a")).toEqual({ width: 420, height: 640 });
    publishPusher("a", "list", null);
    expect(windowPos("a")).toBe(home);
  });
  it("a drag is never pushed, and where it ends is home — the size rides on", () => {
    commitWindowBox("a", box(972, 60, 420, 640));
    publishPusher("a", "list", box(972, 60, 420, 640));
    setLiveWindowPos("a", { left: 990, top: 70 });
    expect(windowPos("a")).toEqual({ left: 990, top: 70 });
    commitWindowPos("a", { left: 40, top: 70 });
    expect(windowPos("a")).toEqual({ left: 40, top: 70 });
    expect(windowSize("a")).toEqual({ width: 420, height: 640 });
  });
  it("a pin with no size in its mark is pushed by its card's live box", () => {
    commitWindowPos("a", { left: 992, top: 80 });
    expect(windowSize("a")).toBeNull();
    publishPusher("a", "list", box(972, 60, 420, 640));
    // No size known: it cannot be told whether it lies under the non-pin.
    expect(windowPos("a")).toEqual({ left: 992, top: 80 });
    publishWindowBox("a", "pin", box(992, 80, 400, 500), true);
    publishPusher("a", "list", box(972, 60, 420, 641));
    expect(windowPos("a")).toEqual({ left: 972 - PUSH_GAP - 400, top: 80 });
  });
  it("pinning freezes the whole picture: a pin this card pushed aside stays where it is drawn", () => {
    // Window A's pin, tall, by the rail; window B's non-pin opens over its foot and pushes it.
    commitWindowBox("a", box(972, 60, 420, 800));
    publishWindowBox("b", "fav", box(1012, 440, 380, 480));
    publishPusher("b", "fav", box(1012, 440, 380, 480));
    const pushed = { left: 1012 - PUSH_GAP - 420, top: 60 };
    expect(windowPos("a")).toEqual(pushed);
    // The favourite is pinned: it stops pushing — and window A's pin does NOT slide back under it.
    freezeFromDock("b");
    expect(windowPos("a")).toEqual(pushed);
    expect(windowSize("a")).toEqual({ width: 420, height: 800 });
    expect(windowPos("b")).toEqual({ left: 1012, top: 440 });
  });
  it("…but a pin pushed by its OWN section's non-pin still goes home when that closes", () => {
    commitWindowBox("b", box(1012, 440, 380, 480));
    publishPusher("b", "fav", box(1012, 440, 380, 480));
    publishWindowBox("a", "list", box(972, 60, 420, 300));
    publishPusher("a", "list", box(972, 60, 420, 300));
    // Window A's card is pinned: only what IT pushed is frozen — window B's pin is pushed by B's own non-pin.
    freezeFromDock("a");
    publishPusher("b", "fav", null);
    expect(windowPos("b")).toEqual({ left: 1012, top: 440 });
  });
  it("a pin with a position and no size gets one, once; a sized mark is left alone", () => {
    commitWindowPos("a", { left: 300, top: 80 });
    expect(windowSize("a")).toBeNull();
    sizeWindowMark("a", { width: 420, height: 640 });
    expect(windowPos("a")).toEqual({ left: 300, top: 80 });
    expect(windowSize("a")).toEqual({ width: 420, height: 640 });
    sizeWindowMark("a", { width: 500, height: 500 });
    expect(windowSize("a")).toEqual({ width: 420, height: 640 });
    // No pin, nothing to size.
    sizeWindowMark("b", { width: 380, height: 520 });
    expect(windowPos("b")).toBeNull();
  });
  it("a size-less pin's card box arriving after the non-pin is heard: the pin is pushed then", () => {
    commitWindowPos("a", { left: 992, top: 80 });
    publishPusher("a", "list", box(972, 60, 420, 640));
    let heard = 0;
    const off = subscribeWindowPos(() => heard++);
    publishWindowBox("a", "pin", box(992, 80, 400, 500), true);
    expect(heard).toBe(1);
    expect(windowPos("a")).toEqual({ left: 972 - PUSH_GAP - 400, top: 80 });
    // The same publication again says nothing.
    publishWindowBox("a", "pin", box(992, 80, 400, 500), true);
    expect(heard).toBe(1);
    off();
  });
  it("turning the unified head off is heard too: the store re-reads", () => {
    commitWindowBox("a", box(972, 60, 420, 640));
    publishPusher("a", "list", box(972, 60, 420, 640));
    expect(windowPos("a")).toEqual({ left: 972 - PUSH_GAP - 420, top: 60 });
    publishTaskHeadUnified({ enabled: false });
    expect(windowPos("a")).toEqual({ left: 972, top: 60 });
    expect(windowSize("a")).toBeNull();
  });
  it("off, nothing is pushed and no size is read", () => {
    commitWindowBox("a", box(972, 60, 420, 640));
    publishPusher("a", "list", box(972, 60, 420, 640));
    setRailFreePinsForTests(false);
    expect(windowPos("a")).toEqual({ left: 972, top: 60 });
    expect(windowSize("a")).toBeNull();
    publishWindowBox("a", "pin", box(1, 1, 400, 500), true);
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

describe("the list card's two slots in the shell's host", () => {
  beforeEach(() => {
    resetRailSlotsForTests();
    resetListHostForTests();
  });
  it("a rail going takes its non-pin and leaves the pinned slot; only the last publisher counts", () => {
    const railA = {};
    const railB = {};
    const hear = renderHookless();
    publishListCards(railA, { x: "free-A", y: "pin" });
    expect(hear.cards()).toEqual({ x: "free-A", y: "pin" });
    expect(hear.orphaned()).toBe(false);
    // The next page's rail publishes before the last one's cleanup runs.
    publishListCards(railB, { x: "free-B", y: "pin" });
    withdrawListCards(railA);
    expect(hear.cards()).toEqual({ x: "free-B", y: "pin" });
    // …and a page with no rail: the free slot (x) goes, the pinned one (y) stays, orphaned.
    withdrawListCards(railB);
    expect(hear.cards()).toEqual({ x: null, y: "pin" });
    expect(hear.orphaned()).toBe(true);
  });
  it("the pinned slot is whichever is not the free one", () => {
    const rail = {};
    const hear = renderHookless();
    setFreeSlot("a", "y");
    publishListCards(rail, { x: "pin", y: "free" });
    withdrawListCards(rail);
    expect(hear.cards()).toEqual({ x: "pin", y: null });
  });
});

describe("what the four windows say to each other", () => {
  beforeEach(() => resetRailSlotsForTests());
  it("marks one pinned window, and lets go", () => {
    setDoomedWindow("a");
    expect(doomedWindow()).toBe("a");
    setDoomedWindow(null);
    expect(doomedWindow()).toBeNull();
  });
  it("a pinned window's name is its host's to take back", () => {
    setPinTitle("b", "task", "One");
    setPinTitle("b", "bot", "Helper");
    setPinTitle("b", "task", null);
    expect(pinTitle("b")).toBe("Helper");
    setPinTitle("b", "bot", null);
    expect(pinTitle("b")).toBeNull();
  });
  it("a chat window's two slots swap roles: the pin makes the other one free, the unpin swaps back", () => {
    expect(freeSlot("a")).toBe("x");
    expect(otherSlot("x")).toBe("y");
    // The pin's press on the free slot's card: the OTHER slot is the non-pin now.
    setFreeSlot("a", otherSlot(freeSlot("a")));
    expect(freeSlot("a")).toBe("y");
    // The unpin on the pinned slot's card (x): it is the free one again.
    setFreeSlot("a", "x");
    expect(freeSlot("a")).toBe("x");
    // Each kind of card has its own pair.
    setFreeSlot("fav", "y");
    expect(freeSlot("fav")).toBe("y");
    expect(freeSlot("bot")).toBe("x");
  });
  it("the keyboard lands in the pinned window that registered", () => {
    expect(focusPin("a")).toBe(false);
    const off = registerPinFocus("a", () => true);
    expect(focusPin("a")).toBe(true);
    expect(focusPin("b")).toBe(false);
    off();
    expect(focusPin("a")).toBe(false);
  });
});
