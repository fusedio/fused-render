// The rail's hover card, the parts without a DOM (rail-peek-lib): where it
// stands, when it opens and closes, and what stays mounted.
import { describe, expect, it } from "bun:test";
import {
  PEEK_CLOSE_MS,
  PEEK_CONE_MS,
  inSafeCone,
  PEEK_GAP,
  PEEK_INSET,
  PEEK_KEEP,
  PEEK_MAX_HEIGHT,
  PEEK_MAX_WIDTH,
  PEEK_MIN_HEIGHT,
  PEEK_MIN_WIDTH,
  PEEK_OPEN_MS,
  PEEK_SWAP_MS,
  PEEK_WARM_MS,
  PEEK_WIDTH,
  PeekIntent,
  STAR_MARK_MS,
  FAV_MARK_MS,
  bottomClear,
  dragResize,
  favOpenMark,
  favOpenStep,
  keepAlive,
  openFavouriteFlow,
  peekPlacement,
  placeClear,
  railOnStarred,
  starMarkStep,
  starOutcome,
  type Box,
  type StarMark,
  type Timers,
  rowOf,
} from "./rail-peek-lib";
import type { TaskPulseTask } from "@platform/lib/api";

const ROW = { left: 0, top: 40, width: 1400, height: 900 };
const RAIL = { left: 1352, top: 40, width: 48, height: 900 };
const ring = (top: number) => ({ left: 1362, top, width: 28, height: 28 });

describe("peekPlacement", () => {
  it("stands a gap off the rail, its top level with the ring's top, at full size on a big row", () => {
    const box = peekPlacement(ring(60), RAIL, ROW)!;
    expect(box.width).toBe(PEEK_WIDTH);
    expect(box.height).toBe(PEEK_MAX_HEIGHT);
    expect(box.left + box.width).toBe(RAIL.left - PEEK_GAP);
    expect(box.top).toBe(60);
    // A shorter card (a draft's) aligns the same way.
    expect(peekPlacement(ring(300), RAIL, ROW, 220)!.top).toBe(300);
  });

  it("rises only as far as the row's bottom needs, never above its top", () => {
    const low = peekPlacement(ring(700), RAIL, ROW)!;
    expect(low.top + low.height).toBe(ROW.top + ROW.height - PEEK_INSET);
    expect(low.top).toBeLessThan(700);
    const high = peekPlacement(ring(42), RAIL, ROW)!;
    expect(high.top).toBe(ROW.top + PEEK_INSET);
  });

  it("shrinks to what a short, narrow row leaves", () => {
    const row = { left: 0, top: 0, width: 440, height: 400 };
    const rail = { left: 392, top: 0, width: 48, height: 400 };
    const box = peekPlacement(ring(100), rail, row)!;
    expect(box.height).toBe(400 - 2 * PEEK_INSET);
    expect(box.width).toBe(rail.left - PEEK_GAP - PEEK_INSET);
    expect(box.left).toBe(PEEK_INSET);
  });

  it("answers null when there is no room for a card at all", () => {
    const row = { left: 0, top: 0, width: 160, height: 400 };
    const rail = { left: 112, top: 0, width: 48, height: 400 };
    expect(peekPlacement(ring(100), rail, row)).toBeNull();
  });

  it("answers null under the chat's floor: a card that would clip its chat is no card", () => {
    // 336px of room: under PEEK_MIN_WIDTH, the chat column's 340px floor.
    const row = { left: 0, top: 0, width: 400, height: 400 };
    const rail = { left: 352, top: 0, width: 48, height: 400 };
    expect(peekPlacement(ring(100), rail, row)).toBeNull();
    expect(PEEK_MIN_WIDTH).toBeGreaterThanOrEqual(342);
  });
});


describe("peekPlacement aligned on the ring's bottom (the favourite's card)", () => {
  it("puts its bottom on the ring's bottom, and never below the row's inset", () => {
    const r = ring(700);
    const box = peekPlacement(r, RAIL, ROW, { width: 380, maxHeight: 520, align: "bottom" })!;
    expect(box.width).toBe(380);
    expect(box.height).toBe(520);
    expect(box.top + box.height).toBe(r.top + r.height);
    const high = peekPlacement(ring(42), RAIL, ROW, { maxHeight: 520, align: "bottom" })!;
    expect(high.top).toBe(ROW.top + PEEK_INSET);
  });
});

describe("peekPlacement with a wish (a pinned card's remembered size)", () => {
  it("takes the asked width and height, right edge still against the rail", () => {
    const box = peekPlacement(ring(60), RAIL, ROW, { width: 600, maxHeight: 800 })!;
    expect(box.width).toBe(600);
    expect(box.height).toBe(800);
    expect(box.left + box.width).toBe(RAIL.left - PEEK_GAP);
    expect(box.top).toBe(60);
  });
  it("never asks for more width than the chat's reading column", () => {
    expect(peekPlacement(ring(60), RAIL, ROW, { width: 2000 })!.width).toBe(PEEK_MAX_WIDTH);
    expect(peekPlacement(ring(60), RAIL, ROW, { width: 600 })!.width).toBe(600);
  });
  it("clamps the asked size to the row", () => {
    const row = { left: 0, top: 0, width: 900, height: 500 };
    const rail = { left: 852, top: 0, width: 48, height: 500 };
    const box = peekPlacement(ring(100), rail, row, { width: 2000, maxHeight: 2000 })!;
    expect(box.width).toBe(Math.min(PEEK_MAX_WIDTH, rail.left - PEEK_GAP - PEEK_INSET));
    expect(box.height).toBe(500 - 2 * PEEK_INSET);
    expect(box.top).toBe(PEEK_INSET);
  });
});

describe("dragResize", () => {
  const start = { left: 924, top: 200, width: 420, height: 640 };
  it("top-left: the pointer moving up and left grows the card, bottom and right edges fixed", () => {
    const box = dragResize("tl", start, -100, -50, ROW);
    expect(box).toEqual({ left: 824, top: 150, width: 520, height: 690 });
  });
  it("bottom-left: the pointer moving down and left grows the card, top and right edges fixed", () => {
    const box = dragResize("bl", start, -100, 50, ROW);
    expect(box).toEqual({ left: 824, top: 200, width: 520, height: 690 });
  });
  it("never goes under the minimums", () => {
    const tl = dragResize("tl", start, 1000, 1000, ROW);
    expect(tl.width).toBe(PEEK_MIN_WIDTH);
    expect(tl.height).toBe(PEEK_MIN_HEIGHT);
    expect(tl.top + tl.height).toBe(start.top + start.height);
    const bl = dragResize("bl", start, 1000, -1000, ROW);
    expect(bl.width).toBe(PEEK_MIN_WIDTH);
    expect(bl.height).toBe(PEEK_MIN_HEIGHT);
    expect(bl.top).toBe(start.top);
  });
  it("a row smaller than the minimums wins over them", () => {
    const row = { left: 0, top: 0, width: 400, height: 300 };
    // Right edge at 250: 242px of room, under PEEK_MIN_WIDTH.
    const small = { left: 100, top: 8, width: 150, height: 200 };
    const grown = dragResize("tl", small, -900, -900, row);
    expect(grown).toEqual({ left: PEEK_INSET, top: PEEK_INSET, width: 242, height: 200 });
    // Shrinking in that row stops at the room too, not at the (bigger) minimum.
    const shrunk = dragResize("bl", small, 900, -900, row);
    expect(shrunk.width).toBe(242);
    expect(shrunk.height).toBe(PEEK_MIN_HEIGHT);
  });
  it("stops widening at the chat's reading column, and never leaves the row", () => {
    const tl = dragResize("tl", start, -5000, -5000, ROW);
    expect(tl.top).toBe(ROW.top + PEEK_INSET);
    expect(tl.width).toBe(PEEK_MAX_WIDTH);
    expect(tl.left).toBe(start.left + start.width - PEEK_MAX_WIDTH);
    // A row too narrow for the column: the row's inset is the stop.
    const narrow = { left: 900, top: 40, width: 500, height: 900 };
    expect(dragResize("tl", start, -5000, 0, narrow).left).toBe(narrow.left + PEEK_INSET);
    const bl = dragResize("bl", start, 0, 5000, ROW);
    expect(bl.top + bl.height).toBe(ROW.top + ROW.height - PEEK_INSET);
  });
});

describe("dragResize, the edges", () => {
  const start = { left: 924, top: 200, width: 420, height: 500 };
  const right = start.left + start.width;
  const bottom = start.top + start.height;
  it("left: the width alone — grows left, shrinks right, the right edge and the height fixed", () => {
    expect(dragResize("l", start, -100, 77, ROW)).toEqual({ left: 824, top: 200, width: 520, height: 500 });
    expect(dragResize("l", start, 40, -77, ROW)).toEqual({ left: 964, top: 200, width: 380, height: 500 });
  });
  it("left: clamps at the minimum, the reading column and the row's inset", () => {
    expect(dragResize("l", start, 5000, 0, ROW).width).toBe(PEEK_MIN_WIDTH);
    expect(dragResize("l", start, -5000, 0, ROW)).toEqual({ left: right - PEEK_MAX_WIDTH, top: 200, width: PEEK_MAX_WIDTH, height: 500 });
    const narrow = { left: 900, top: 40, width: 500, height: 900 };
    expect(dragResize("l", start, -5000, 0, narrow).left).toBe(narrow.left + PEEK_INSET);
  });
  it("top: the height alone, growing up — the bottom, the left and the width fixed", () => {
    expect(dragResize("t", start, -90, -60, ROW)).toEqual({ left: 924, top: 140, width: 420, height: 560 });
    expect(dragResize("t", start, 90, 60, ROW)).toEqual({ left: 924, top: 260, width: 420, height: 440 });
  });
  it("top: clamps at the row's top and at the minimum height", () => {
    const up = dragResize("t", start, 0, -5000, ROW);
    expect(up.top).toBe(ROW.top + PEEK_INSET);
    expect(up.top + up.height).toBe(bottom);
    const down = dragResize("t", start, 0, 5000, ROW);
    expect(down.height).toBe(PEEK_MIN_HEIGHT);
    expect(down.top + down.height).toBe(bottom);
  });
  it("bottom: the height alone, growing down — the top, the left and the width fixed", () => {
    expect(dragResize("b", start, -90, 60, ROW)).toEqual({ left: 924, top: 200, width: 420, height: 560 });
    expect(dragResize("b", start, 90, -60, ROW)).toEqual({ left: 924, top: 200, width: 420, height: 440 });
  });
  it("bottom: clamps at the row's bottom, at the minimum, and at a floor short of it", () => {
    const down = dragResize("b", start, 0, 5000, ROW);
    expect(down.top + down.height).toBe(ROW.top + ROW.height - PEEK_INSET);
    expect(dragResize("b", start, 0, -5000, ROW).height).toBe(PEEK_MIN_HEIGHT);
    // The favourite's card below: the floor is its top less the gap.
    const floored = dragResize("b", start, 0, 5000, ROW, 760);
    expect(floored).toEqual({ left: 924, top: 200, width: 420, height: 560 });
    // …and the bottom-left corner honours it too.
    expect(dragResize("bl", start, -50, 5000, ROW, 760).height).toBe(560);
    // A floor past the row's own changes nothing.
    expect(dragResize("b", start, 0, 5000, ROW, 99_999)).toEqual(down);
  });
});

describe("bottomClear", () => {
  const fav = { left: 964, top: 600, width: 380, height: 332 };
  it("a card standing above the favourite's stops where placeClear would end it: a gap and the inset over its top", () => {
    expect(bottomClear({ left: 924, top: 100, width: 420, height: 400 }, fav)).toBe(600 - PEEK_GAP - PEEK_INSET);
    // …the same bottom placeClear gives a card above the favourite's.
    const placed = placeClear(ring(48), RAIL, { left: 0, top: 0, width: 1400, height: 900 }, fav, PEEK_MAX_HEIGHT)!;
    expect(placed.top + placed.height).toBeLessThanOrEqual(600 - PEEK_GAP - PEEK_INSET);
  });
  it("none beside it, over it, or with no favourite's card", () => {
    expect(bottomClear({ left: 400, top: 100, width: 420, height: 800 }, fav)).toBeUndefined();
    expect(bottomClear({ left: 924, top: 100, width: 420, height: 800 }, fav)).toBeUndefined();
    expect(bottomClear({ left: 924, top: 100, width: 420, height: 400 }, null)).toBeUndefined();
  });
});

describe("favOpenStep", () => {
  const T0 = 1_000_000;
  it("holds the pressed key over a pulse still saying what it said at the press", () => {
    const m = favOpenMark("B", null, null, T0);
    expect(favOpenStep(m, null, true, T0 + 100)).toBe(m);
  });
  it("lets go once the pulse agrees", () => {
    const m = favOpenMark("B", "A", null, T0);
    expect(favOpenStep(m, "B", true, T0 + 100)).toBeNull();
  });
  it("follows the pulse when it names a key the mark is not waiting out (another window won)", () => {
    const m = favOpenMark("B", null, null, T0);
    expect(favOpenStep(m, "C", true, T0 + 100)).toBeNull();
  });
  it("a quick A-then-B waits out a pulse still saying A", () => {
    const a = favOpenMark("A", null, null, T0);
    const b = favOpenMark("B", null, a, T0 + 50);
    expect(b.stale).toEqual([null, "A"]);
    expect(favOpenStep(b, "A", true, T0 + 200)).toBe(b);
    expect(favOpenStep(b, "B", true, T0 + 300)).toBeNull();
  });
  it("lets go when the key is no longer a live favourite, or past FAV_MARK_MS", () => {
    const m = favOpenMark("B", null, null, T0);
    expect(favOpenStep(m, null, false, T0 + 100)).toBeNull();
    expect(favOpenStep(m, null, true, T0 + FAV_MARK_MS - 1)).toBe(m);
    expect(favOpenStep(m, null, true, T0 + FAV_MARK_MS)).toBeNull();
  });
});

describe("inSafeCone", () => {
  const card = { left: 924, top: 48, width: 420, height: 640 };
  const apex = { x: 1376, y: 100 }; // the ring's centre, in the rail
  it("a diagonal path from the ring toward the card's far corner is inside", () => {
    expect(inSafeCone({ x: 1360, y: 140 }, apex, card)).toBe(true);
    expect(inSafeCone({ x: 1350, y: 400 }, apex, card)).toBe(true);
    expect(inSafeCone({ x: 1346, y: 650 }, apex, card)).toBe(true);
  });
  it("the rail column and the card itself are not the cone's", () => {
    expect(inSafeCone({ x: 1380, y: 200 }, apex, card)).toBe(false);
    expect(inSafeCone({ x: 1300, y: 200 }, apex, card)).toBe(false);
  });
  it("a point far off the card's padded edge is outside", () => {
    expect(inSafeCone({ x: 1360, y: 850 }, apex, card)).toBe(false);
    expect(inSafeCone({ x: 1360, y: -100 }, apex, card)).toBe(false);
  });
});

describe("keepAlive", () => {
  it("puts the newest first and caps the set", () => {
    let kept: readonly string[] = [];
    for (const k of ["a", "b", "c", "d"]) kept = keepAlive(kept, k);
    expect(kept).toEqual(["d", "c", "b"]);
    expect(kept.length).toBe(PEEK_KEEP);
  });
  it("moves a known key to the front instead of doubling it", () => {
    expect(keepAlive(["a", "b", "c"], "b")).toEqual(["b", "a", "c"]);
  });
  it("the front key is the same array (a no-op state write)", () => {
    const kept = ["a", "b"];
    expect(keepAlive(kept, "a")).toBe(kept);
  });
  it("a cap of zero still keeps the one just previewed", () => {
    expect(keepAlive(["a"], "b", 0)).toEqual(["b"]);
  });
});

/** A hand-cranked clock for the intent's timers. */
class Clock implements Timers {
  now = 0;
  private next = 1;
  private due = new Map<number, { at: number; fn: () => void }>();
  set(fn: () => void, ms: number): number {
    const id = this.next++;
    this.due.set(id, { at: this.now + ms, fn });
    return id;
  }
  clear(id: number): void {
    this.due.delete(id);
  }
  tick(ms: number): void {
    const until = this.now + ms;
    for (;;) {
      const ready = [...this.due.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at);
      if (ready.length === 0) break;
      const [id, t] = ready[0]!;
      this.due.delete(id);
      this.now = t.at;
      t.fn();
    }
    this.now = until;
  }
}

function rig() {
  const clock = new Clock();
  const shown: (string | null)[] = [];
  const warmed: string[] = [];
  const intent = new PeekIntent({ show: (k) => shown.push(k), warm: (k) => warmed.push(k) }, clock);
  return { clock, shown, warmed, intent };
}

describe("PeekIntent", () => {
  it("warms after a short dwell, shows after PEEK_OPEN_MS", () => {
    const { clock, shown, warmed, intent } = rig();
    intent.enterRing("a");
    expect(warmed).toEqual([]);
    clock.tick(PEEK_WARM_MS);
    expect(warmed).toEqual(["a"]);
    expect(shown).toEqual([]);
    clock.tick(PEEK_OPEN_MS - PEEK_WARM_MS - 1);
    expect(shown).toEqual([]);
    clock.tick(1);
    expect(shown).toEqual(["a"]);
    expect(intent.current).toBe("a");
  });

  it("a focus arrival warms at once (deliberate), a pointer sweep warms nothing", () => {
    const { clock, warmed, intent } = rig();
    intent.enterRing("a", "focus");
    expect(warmed).toEqual(["a"]);
    intent.leaveRing("focus");
    intent.enterRing("b");
    clock.tick(PEEK_WARM_MS - 1);
    intent.leaveRing();
    clock.tick(1000);
    expect(warmed).toEqual(["a"]);
  });

  it("a pass-through hover shows nothing", () => {
    const { clock, shown, intent } = rig();
    intent.enterRing("a");
    clock.tick(PEEK_OPEN_MS / 2);
    intent.leaveRing();
    clock.tick(1000);
    expect(shown).toEqual([]);
  });

  it("a pointer leaving a PENDING ring while another is shown keeps the shown one", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    intent.leaveRing();
    intent.enterRing("b");
    clock.tick(PEEK_SWAP_MS / 2);
    intent.leaveRing();
    clock.tick(PEEK_CLOSE_MS - 1);
    expect(shown).toEqual(["a"]);
    clock.tick(1);
    expect(shown).toEqual(["a", null]);
  });

  it("swaps to another ring after the shorter wait, never closing between", () => {
    const { clock, shown, intent } = rig();
    intent.enterRing("a");
    clock.tick(PEEK_OPEN_MS);
    intent.leaveRing();
    intent.enterRing("b");
    clock.tick(PEEK_SWAP_MS);
    expect(shown).toEqual(["a", "b"]);
  });

  it("leaving ring and card closes after the grace; coming back cancels it", () => {
    const { clock, shown, intent } = rig();
    intent.enterRing("a");
    clock.tick(PEEK_OPEN_MS);
    intent.leaveRing();
    clock.tick(PEEK_CLOSE_MS - 1);
    intent.enterCard();
    clock.tick(1000);
    expect(shown).toEqual(["a"]);
    intent.leaveCard();
    clock.tick(PEEK_CLOSE_MS);
    expect(shown).toEqual(["a", null]);
    expect(intent.current).toBeNull();
  });

  it("a card holding focus does not close on a pointer leave; releasing it does", () => {
    const { clock, shown, intent } = rig();
    intent.enterRing("a");
    clock.tick(PEEK_OPEN_MS);
    intent.leaveRing();
    intent.enterCard();
    intent.hold(true);
    intent.leaveCard();
    clock.tick(10_000);
    expect(shown).toEqual(["a"]);
    intent.hold(false);
    clock.tick(PEEK_CLOSE_MS);
    expect(shown).toEqual(["a", null]);
  });

  it("showNow opens with no wait and dismiss closes with none", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    expect(shown).toEqual(["a"]);
    intent.hold(true);
    intent.dismiss();
    expect(shown).toEqual(["a", null]);
    // Held state is cleared by dismiss: a later leave closes normally.
    intent.enterRing("b");
    clock.tick(PEEK_OPEN_MS);
    intent.leaveRing();
    clock.tick(PEEK_CLOSE_MS);
    expect(shown).toEqual(["a", null, "b", null]);
  });

  it("an outside-press dismiss leaves focus live: the next Tab onto a ring opens", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    intent.dismiss();
    intent.enterRing("b", "focus");
    clock.tick(PEEK_OPEN_MS);
    expect(shown).toEqual(["a", null, "b"]);
  });

  it("a dismiss with the pointer on the card forgets the card: the next card closes on leave", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    intent.enterCard();
    intent.dismiss();
    intent.enterRing("b");
    clock.tick(PEEK_OPEN_MS);
    intent.leaveRing();
    clock.tick(PEEK_CLOSE_MS);
    expect(shown).toEqual(["a", null, "b", null]);
  });

  it("prune forgets rings that left the rail and closes a card whose chat did", () => {
    const { clock, shown, intent } = rig();
    intent.enterRing("a");
    clock.tick(PEEK_OPEN_MS);
    // Ring a unmounts under the pointer: no leave fires. Prune stands in.
    intent.prune(new Set(["b"]));
    expect(shown).toEqual(["a", null]);
    intent.enterRing("b");
    clock.tick(PEEK_OPEN_MS);
    intent.leaveRing();
    clock.tick(PEEK_CLOSE_MS);
    expect(shown).toEqual(["a", null, "b", null]);
  });

  it("prune with the hovered ring gone but the shown chat alive closes on the grace", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    intent.leaveRing();
    intent.enterRing("b");
    // b unmounts mid-pending: the pointer rests on nothing now.
    intent.prune(new Set(["a"]));
    clock.tick(PEEK_CLOSE_MS);
    expect(shown).toEqual(["a", null]);
  });

  it("focus coming back to the ring after an Escape dismiss does not reopen; Tabbing on does", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    // Escape: the host dismisses and hands focus back to ring a.
    intent.dismiss(true);
    intent.enterRing("a", "focus");
    clock.tick(10_000);
    expect(shown).toEqual(["a", null]);
    // Tab to the next ring: focus leaves a, arrives on b — that opens.
    intent.leaveRing("focus");
    intent.enterRing("b", "focus");
    clock.tick(PEEK_OPEN_MS);
    expect(shown).toEqual(["a", null, "b"]);
  });

  it("a pointer arrival after a dismiss opens as usual", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    intent.dismiss(true);
    intent.enterRing("a", "focus");
    intent.enterRing("a", "pointer");
    clock.tick(PEEK_OPEN_MS);
    expect(shown).toEqual(["a", null, "a"]);
  });

  it("the pointer leaving a pending ring while focus rests on another cancels that open", () => {
    const { clock, shown, intent } = rig();
    intent.enterRing("a", "focus");
    clock.tick(PEEK_OPEN_MS);
    intent.enterRing("b", "pointer");
    intent.leaveRing("pointer");
    clock.tick(10_000);
    expect(shown).toEqual(["a"]);
  });

  it("prune closing the shown chat drops a keyboard hold", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    intent.hold(true);
    intent.prune(new Set(["b"]));
    expect(shown).toEqual(["a", null]);
    intent.enterRing("b");
    clock.tick(PEEK_OPEN_MS);
    intent.leaveRing();
    clock.tick(PEEK_CLOSE_MS);
    expect(shown).toEqual(["a", null, "b", null]);
  });

  it("in the cone, crossing another ring does not swap and the card holds; leaving the cone on that ring swaps", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    intent.leaveRing();
    intent.enterCone();
    intent.enterRing("b");
    intent.leaveRing();
    clock.tick(PEEK_CLOSE_MS + 50);
    expect(shown).toEqual(["a"]);
    // Still in the cone, now resting on b: leaving the cone there swaps to b.
    intent.enterRing("b");
    intent.leaveCone();
    clock.tick(PEEK_SWAP_MS);
    expect(shown).toEqual(["a", "b"]);
  });

  it("hanging in the cone past PEEK_CONE_MS lets the grace close run", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    intent.leaveRing();
    intent.enterCone();
    clock.tick(PEEK_CONE_MS - 1);
    expect(shown).toEqual(["a"]);
    clock.tick(1 + PEEK_CLOSE_MS);
    expect(shown).toEqual(["a", null]);
  });

  it("reaching the card from the cone keeps it, with no clock running", () => {
    const { clock, shown, intent } = rig();
    intent.showNow("a");
    intent.leaveRing();
    intent.enterCone();
    intent.enterCard();
    clock.tick(PEEK_CONE_MS + PEEK_CLOSE_MS + 1000);
    expect(shown).toEqual(["a"]);
  });

  it("re-entering the shown ring is a no-op (no second show, no close)", () => {
    const { clock, shown, intent } = rig();
    intent.enterRing("a");
    clock.tick(PEEK_OPEN_MS);
    intent.leaveRing();
    intent.enterRing("a");
    clock.tick(10_000);
    expect(shown).toEqual(["a"]);
  });

  describe("a click pins it (press, pressStart)", () => {
    it("a press shows at once, warms and pins; pressing it again keeps it", () => {
      const { clock, shown, warmed, intent } = rig();
      intent.press("a");
      expect(shown).toEqual(["a"]);
      expect(warmed).toEqual(["a"]);
      expect(intent.isPinned).toBe(true);
      intent.press("a");
      intent.leaveRing();
      clock.tick(10_000);
      expect(shown).toEqual(["a"]);
    });

    it("a press over a hover card of the same ring pins it without a flash", () => {
      const { clock, shown, intent } = rig();
      intent.enterRing("a");
      clock.tick(PEEK_OPEN_MS);
      intent.press("a");
      intent.leaveRing();
      clock.tick(10_000);
      expect(shown).toEqual(["a"]);
    });

    it("a press inside a hover card pins it", () => {
      const { clock, shown, intent } = rig();
      intent.showNow("a");
      intent.enterCard();
      intent.pressStart();
      intent.pressEnd();
      intent.leaveCard();
      clock.tick(10_000);
      expect(shown).toEqual(["a"]);
      expect(intent.isPinned).toBe(true);
    });

    it("a press on another ring swaps without closing between", () => {
      const { shown, intent } = rig();
      intent.press("a");
      intent.press("b");
      expect(shown).toEqual(["a", "b"]);
      expect(intent.isPinned).toBe(true);
    });

    it("pinned: no pointer leave, focus loss, press end or seat change closes it", () => {
      const { clock, shown, intent } = rig();
      intent.press("a");
      intent.enterCard();
      intent.leaveCard();
      intent.hold(true);
      intent.hold(false);
      intent.pressStart();
      intent.pressEnd();
      intent.enterCone();
      clock.tick(PEEK_CONE_MS);
      intent.prune(new Set(["a", "b"]));
      clock.tick(10_000);
      expect(shown).toEqual(["a"]);
    });

    it("pinned: a hover or a focus on another ring moves nothing (only a press swaps)", () => {
      const { clock, shown, warmed, intent } = rig();
      intent.press("a");
      intent.leaveRing();
      intent.enterRing("b");
      intent.enterRing("c", "focus");
      clock.tick(10_000);
      expect(shown).toEqual(["a"]);
      expect(warmed).toEqual(["a"]);
    });

    it("tells the sink when it pins and unpins", () => {
      const clock = new Clock();
      const pins: boolean[] = [];
      const intent = new PeekIntent({ show: () => {}, warm: () => {}, pin: (on) => pins.push(on) }, clock);
      intent.press("a");
      intent.press("b");
      intent.dismiss();
      intent.showNow("c");
      intent.pressStart();
      intent.prune(new Set());
      expect(pins).toEqual([true, false, true, false]);
    });

    it("dismiss unpins: a later hover-style leave closes on the grace as before", () => {
      const { clock, shown, intent } = rig();
      intent.press("a");
      intent.dismiss();
      intent.showNow("b");
      intent.leaveRing();
      clock.tick(PEEK_CLOSE_MS);
      expect(shown).toEqual(["a", null, "b", null]);
    });

    it("a ring Escape handed the focus back to does not hold another ring's hover card", () => {
      const { clock, shown, intent } = rig();
      intent.showNow("a", true);
      intent.dismiss(true);
      intent.enterRing("a", "focus");
      intent.enterRing("b");
      clock.tick(PEEK_OPEN_MS);
      intent.leaveRing();
      clock.tick(PEEK_CLOSE_MS);
      expect(shown).toEqual(["a", null, "b", null]);
    });

    it("a pinned card whose chat left the rail closes, and unpins", () => {
      const { clock, shown, intent } = rig();
      intent.press("a");
      intent.prune(new Set(["b"]));
      expect(shown).toEqual(["a", null]);
      intent.showNow("b");
      intent.leaveCard();
      clock.tick(PEEK_CLOSE_MS);
      expect(shown).toEqual(["a", null, "b", null]);
    });
  });
});

// ---- The favourite's card beside the rail's (placeClear) -------------------

describe("placeClear", () => {
  // The favourite's card: 380 wide, its bottom on the rail's foot.
  const fav = (top: number): Box => ({ left: RAIL.left - PEEK_GAP - 380, top, width: 380, height: ROW.top + ROW.height - PEEK_INSET - top });
  const hit = (a: Box, b: Box) =>
    a.left < b.left + b.width && b.left < a.left + a.width && a.top < b.top + b.height && b.top < a.top + a.height;

  it("is the plain placement with no favourite's card up", () => {
    expect(placeClear(ring(60), RAIL, ROW, null, PEEK_MAX_HEIGHT)).toEqual(peekPlacement(ring(60), RAIL, ROW));
  });

  it("stands above the favourite's card when the room there holds a usable card", () => {
    const avoid = fav(500);
    const box = placeClear(ring(60), RAIL, ROW, avoid, PEEK_MAX_HEIGHT)!;
    expect(box.left + box.width).toBe(RAIL.left - PEEK_GAP);
    expect(box.top + box.height).toBeLessThanOrEqual(avoid.top - PEEK_GAP);
    expect(hit(box, avoid)).toBe(false);
  });

  it("stands to the favourite's LEFT, level with its ring, when the room above is under a card (a ~700px window)", () => {
    const row = { left: 0, top: 40, width: 1400, height: 640 };
    const rail = { ...RAIL, height: 640 };
    const avoid: Box = { left: RAIL.left - PEEK_GAP - 380, top: 160, width: 380, height: 512 };
    const box = placeClear(ring(80), rail, row, avoid, PEEK_MAX_HEIGHT)!;
    expect(box.left + box.width).toBe(avoid.left - PEEK_GAP);
    expect(box.width).toBe(PEEK_WIDTH);
    // The usual ring alignment: level with the ring, pushed up only as far as
    // the row's bottom needs (a 640 card in a 640 row).
    expect(box.top).toBe(row.top + PEEK_INSET);
    expect(placeClear(ring(80), rail, row, avoid, 400)!.top).toBe(80);
    expect(hit(box, avoid)).toBe(false);
  });

  it("overlaps only as the last resort: no room above, none to the left", () => {
    const row = { left: 600, top: 40, width: 800, height: 500 };
    const avoid: Box = { left: RAIL.left - PEEK_GAP - 380, top: 100, width: 380, height: 432 };
    const box = placeClear(ring(60), RAIL, row, avoid, PEEK_MAX_HEIGHT)!;
    expect(box).toEqual(peekPlacement(ring(60), RAIL, row)!);
  });
});

// ---- The star (starMarkStep, starOutcome, railOnStarred) --------------------

const pulseRow = (starred: boolean, key = "k1"): TaskPulseTask => ({ key, starred }) as unknown as TaskPulseTask;

describe("starMarkStep", () => {
  const set = (on: boolean, at: TaskPulseTask): StarMark => ({ key: at.key, on, pulse: at, seen: 0, seq: 1 });

  it("ends when the pulse agrees, and when the row leaves", () => {
    const mark = set(true, pulseRow(false));
    expect(starMarkStep(mark, pulseRow(true))).toBeNull();
    expect(starMarkStep(mark, undefined)).toBeNull();
  });

  it("holds over the row it was set on and over ONE disagreeing pulse (it may have left before the write)", () => {
    const at = pulseRow(false);
    const mark = set(true, at);
    expect(starMarkStep(mark, at)).toBe(mark);
    const first = pulseRow(false);
    const next = starMarkStep(mark, first)!;
    expect(next).not.toBeNull();
    expect(next.on).toBe(true);
    expect(next.seen).toBe(1);
    // The same row again is no new pulse.
    expect(starMarkStep(next, first)).toBe(next);
  });

  it("ends on the SECOND distinct disagreeing pulse: another window flipped the star, the pulse is the truth", () => {
    const mark = set(true, pulseRow(false));
    const after1 = starMarkStep(mark, pulseRow(false))!;
    expect(starMarkStep(after1, pulseRow(false))).toBeNull();
  });

  it("has a clock of its own for a pulse that never comes", () => {
    expect(STAR_MARK_MS).toBe(6000);
  });
});

describe("starOutcome (dismissOnUnstar)", () => {
  it("an unstar shuts only the favourite's own card, and only while it is on that chat", () => {
    expect(starOutcome(false, "k1", "k1", true).dismiss).toBe(true);
    expect(starOutcome(false, "k1", "k2", true).dismiss).toBe(false);
  });
  it("the rail's card is never shut by an unstar (it let go on the star)", () => {
    expect(starOutcome(false, "k1", "k1", false).dismiss).toBe(false);
  });
  it("a star never shuts a card here (the rail lets go through onStarred)", () => {
    expect(starOutcome(true, "k1", "k1", true).dismiss).toBe(false);
    expect(starOutcome(true, "k1", "k1", false).dismiss).toBe(false);
  });
});

describe("railOnStarred (onStarred wiring)", () => {
  it("a star pressed on lets the rail's card go of that chat at once, favourites on", () => {
    const gone: string[] = [];
    railOnStarred(() => true, (k) => gone.push(k))("k1", true);
    expect(gone).toEqual(["k1"]);
  });
  it("an unstar, or favourites off, lets go of nothing", () => {
    const gone: string[] = [];
    railOnStarred(() => true, (k) => gone.push(k))("k1", false);
    railOnStarred(() => false, (k) => gone.push(k))("k1", true);
    expect(gone).toEqual([]);
  });
  it("reads the switch at the answer, not at the wiring", () => {
    let on = false;
    const gone: string[] = [];
    const cb = railOnStarred(() => on, (k) => gone.push(k));
    on = true;
    cb("k1", true);
    expect(gone).toEqual(["k1"]);
  });
});

// ---- A favourite's ring pressed (openFavouriteFlow) -------------------------

describe("openFavouriteFlow", () => {
  const run = (post: () => Promise<{ starred: boolean; open: boolean }>) => {
    const log: string[] = [];
    const done = openFavouriteFlow("k1", {
      post: async (key) => {
        log.push(`post:${key}`);
        return post();
      },
      letGo: (key) => log.push(`letGo:${key}`),
      fallback: () => log.push("press"),
      fail: (m) => log.push(`fail:${m}`),
    });
    return { log, done };
  };

  it("writes FIRST and lets go only once the server says open", async () => {
    const { log, done } = run(async () => ({ starred: true, open: true }));
    // Nothing is let go before the answer.
    expect(log).toEqual(["post:k1"]);
    expect(await done).toBe(true);
    expect(log).toEqual(["post:k1", "letGo:k1"]);
  });

  it("a refused write (409, 404, offline) says so once and falls back to the ring's press", async () => {
    const { log, done } = run(async () => {
      throw new Error("Task is archived");
    });
    expect(await done).toBe(false);
    expect(log).toEqual(["post:k1", "fail:Task is archived", "press"]);
  });

  it("says the server's own detail, not the transport's bare HTTP status", async () => {
    const { log, done } = run(async () => {
      throw Object.assign(new Error("HTTP 409"), { status: 409, body: { detail: "archived: reopen refused" } });
    });
    await done;
    expect(log).toContain("fail:archived: reopen refused");
  });

  it("an answer that is not open — unstarred elsewhere — falls back too, never lets go", async () => {
    const { log, done } = run(async () => ({ starred: false, open: false }));
    expect(await done).toBe(false);
    expect(log.filter((l) => l.startsWith("letGo"))).toEqual([]);
    expect(log.filter((l) => l.startsWith("fail:"))).toHaveLength(1);
    expect(log[log.length - 1]).toBe("press");
  });
});

describe("rowOf", () => {
  it("the rail's nearest ancestor with a box: a display:contents slot is skipped", () => {
    const real = globalThis.getComputedStyle;
    const split = { display: "flex", parentElement: null } as unknown as HTMLElement;
    const slot = { display: "contents", parentElement: split } as unknown as HTMLElement;
    const rail = { parentElement: slot } as unknown as HTMLElement;
    (globalThis as { getComputedStyle: unknown }).getComputedStyle = (el: { display: string }) => ({
      display: el.display,
    });
    try {
      expect(rowOf(rail)).toBe(split);
      expect(rowOf({ parentElement: split } as unknown as HTMLElement)).toBe(split);
      expect(rowOf(null)).toBeNull();
    } finally {
      (globalThis as { getComputedStyle: unknown }).getComputedStyle = real;
    }
  });
});
