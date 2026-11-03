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
  dragResize,
  keepAlive,
  peekPlacement,
  type Timers,
} from "./rail-peek-lib";

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
    const row = { left: 0, top: 0, width: 400, height: 400 };
    const rail = { left: 352, top: 0, width: 48, height: 400 };
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
