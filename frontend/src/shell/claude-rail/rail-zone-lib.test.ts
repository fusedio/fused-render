// The rail's cold and hot zones (owner, 2026-11-25: "the rail blocks
// stuff"): the zone alone, then the two hover machines that ask it — the
// chat cards' and doors' `PeekIntent` and the +'s `PlusPopIntent` — on one
// shared zone, as the rail runs them.
import { describe, expect, it } from "bun:test";
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, PEEK_SWAP_MS, PeekIntent, type Timers } from "./rail-peek-lib";
import { PlusPopIntent } from "./plus-pop-lib";
import { RAIL_COLD_OPEN_MS, RAIL_COOL_MS, RAIL_HOT_OPEN_MS, RailZone, usualPace, type HoverPace } from "./rail-zone-lib";

class Clock implements Timers {
  now = 0;
  private id = 0;
  private timers = new Map<number, { at: number; fn: () => void }>();
  set(fn: () => void, ms: number): number {
    const id = ++this.id;
    this.timers.set(id, { at: this.now + ms, fn });
    return id;
  }
  clear(id: number): void {
    this.timers.delete(id);
  }
  tick(ms: number): void {
    const end = this.now + ms;
    for (;;) {
      const next = [...this.timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      this.timers.delete(next[0]);
      this.now = next[1].at;
      next[1].fn();
    }
    this.now = end;
  }
}

/** One zone and its clock, and the pace the rail's machines read off it. */
function rail() {
  const clock = new Clock();
  const zone = new RailZone(() => clock.now);
  const pace: HoverPace = { openMs: () => zone.openMs(), shown: () => zone.heat() };
  /** A window (a `PeekIntent`) on this rail. */
  const win = () => {
    const shown: (string | null)[] = [];
    const warmed: string[] = [];
    const intent = new PeekIntent(
      {
        show: (k) => {
          // The mount always comes before the show.
          if (k !== null) expect(warmed).toContain(k);
          shown.push(k);
        },
        warm: (k) => warmed.push(k),
      },
      clock,
      pace,
    );
    return { intent, shown, warmed };
  };
  return { clock, zone, pace, win };
}

describe("RailZone", () => {
  it("is cold from outside, hot once heated inside, and a ring's arrival counts as inside", () => {
    let now = 0;
    const zone = new RailZone(() => now);
    expect(zone.isHot).toBe(false);
    // A card the keyboard opened with the pointer elsewhere heats nothing.
    zone.heat();
    expect(zone.isHot).toBe(false);
    // No watcher saw it come in, but a ring was entered: it is in.
    expect(zone.openMs()).toBe(RAIL_COLD_OPEN_MS);
    expect(zone.isInside).toBe(true);
    zone.heat();
    expect(zone.openMs()).toBe(RAIL_HOT_OPEN_MS);
    // Dwelling heats too: in the zone the cold wait long, anywhere in it.
    const dwell = new RailZone(() => now);
    dwell.enter();
    now += RAIL_COLD_OPEN_MS - 100;
    expect(dwell.isHot).toBe(false);
    // A ring reached partway waits out only what is left.
    expect(dwell.openMs()).toBe(100);
    now += 100;
    expect(dwell.isHot).toBe(true);
    expect(dwell.openMs()).toBe(RAIL_HOT_OPEN_MS);
    // A dwell-hot leave keeps the heat through the cool-off.
    dwell.leave();
    now += RAIL_COOL_MS - 1;
    expect(dwell.isHot).toBe(true);
  });

  it("keeps the heat through a brief leave, loses it after the cool-off", () => {
    let now = 0;
    const zone = new RailZone(() => now);
    zone.enter();
    zone.heat();
    zone.leave();
    now += RAIL_COOL_MS - 1;
    expect(zone.isHot).toBe(true);
    zone.enter();
    expect(zone.openMs()).toBe(RAIL_HOT_OPEN_MS);
    zone.leave();
    now += RAIL_COOL_MS;
    expect(zone.isHot).toBe(false);
    zone.enter();
    expect(zone.openMs()).toBe(RAIL_COLD_OPEN_MS);
  });

  it("a cold leave keeps nothing; enter and leave twice are one each", () => {
    let now = 0;
    const zone = new RailZone(() => now);
    zone.enter();
    zone.leave();
    now += 1;
    zone.enter();
    expect(zone.openMs()).toBe(RAIL_COLD_OPEN_MS);
    zone.heat();
    zone.enter();
    expect(zone.isHot).toBe(true);
    zone.leave();
    zone.leave();
    now += RAIL_COOL_MS - 1;
    expect(zone.isHot).toBe(true);
    zone.reset();
    expect(zone.isHot).toBe(false);
    expect(zone.isInside).toBe(false);
  });

  it("the usual pace is the machine's own wait", () => {
    expect(usualPace.openMs(PEEK_OPEN_MS)).toBe(PEEK_OPEN_MS);
    expect(usualPace.openMs(PEEK_SWAP_MS)).toBe(PEEK_SWAP_MS);
  });
});

describe("PeekIntent on the zone", () => {
  it("cold: from outside, the card waits the cold wait, then the rail is hot", () => {
    const { clock, zone, win } = rail();
    const a = win();
    zone.enter();
    a.intent.enterRing("x", "pointer");
    clock.tick(RAIL_COLD_OPEN_MS - 1);
    expect(a.shown).toEqual([]);
    clock.tick(1);
    expect(a.shown).toEqual(["x"]);
    expect(zone.isHot).toBe(true);
  });

  it("a pass-through opens nothing; a fast sweep opens nothing; a rest on the rail's empty area heats it", () => {
    const { clock, zone, win } = rail();
    const a = win();
    zone.enter();
    a.intent.enterRing("x", "pointer");
    clock.tick(RAIL_COLD_OPEN_MS - 1);
    a.intent.leaveRing("pointer");
    zone.leave();
    clock.tick(10_000);
    expect(a.shown).toEqual([]);
    // A fast sweep across the rail: in and out within the wait.
    zone.enter();
    for (const k of ["a", "b", "c"]) {
      a.intent.enterRing(k, "pointer");
      clock.tick(RAIL_COLD_OPEN_MS / 4);
      a.intent.leaveRing("pointer");
    }
    zone.leave();
    clock.tick(10_000);
    expect(a.shown).toEqual([]);
    // Parked on the rail's empty area past the wait: hot, the next ring is
    // at once.
    zone.enter();
    clock.tick(RAIL_COLD_OPEN_MS);
    expect(zone.isHot).toBe(true);
    a.intent.enterRing("e", "pointer");
    clock.tick(0);
    expect(a.shown).toEqual(["e"]);
  });

  it("hot: ring to ring is instant, mounted first — in every window on the rail", () => {
    const { clock, zone, win } = rail();
    const a = win();
    const b = win();
    zone.enter();
    a.intent.enterRing("x", "pointer");
    clock.tick(RAIL_COLD_OPEN_MS);
    a.intent.leaveRing("pointer");
    a.intent.enterRing("y", "pointer");
    expect(a.shown).toEqual(["x", "y"]);
    expect(a.warmed).toEqual(["x", "y"]);
    // Window B's ring (a favourite, a door): its own machine, the same zone.
    a.intent.leaveRing("pointer");
    b.intent.enterRing("f", "pointer");
    expect(b.shown).toEqual(["f"]);
    // Window A closes on its grace as ever.
    clock.tick(PEEK_CLOSE_MS);
    expect(a.shown).toEqual(["x", "y", null]);
  });

  it("a brief overshoot off the rail stays hot; a longer one is cold again", () => {
    const { clock, zone, win } = rail();
    const a = win();
    zone.enter();
    a.intent.enterRing("x", "pointer");
    clock.tick(RAIL_COLD_OPEN_MS);
    a.intent.leaveRing("pointer");
    zone.leave();
    clock.tick(RAIL_COOL_MS - 1);
    // The card went on its own grace; the rail is still hot.
    expect(a.shown).toEqual(["x", null]);
    zone.enter();
    a.intent.enterRing("y", "pointer");
    expect(a.shown).toEqual(["x", null, "y"]);
    a.intent.leaveRing("pointer");
    zone.leave();
    clock.tick(RAIL_COOL_MS);
    expect(a.shown).toEqual(["x", null, "y", null]);
    zone.enter();
    a.intent.enterRing("z", "pointer");
    clock.tick(RAIL_COLD_OPEN_MS - 1);
    expect(a.shown).toEqual(["x", null, "y", null]);
    clock.tick(1);
    expect(a.shown).toEqual(["x", null, "y", null, "z"]);
  });

  it("the keyboard keeps its own timing, and a card it opens from outside heats nothing", () => {
    const { clock, zone, win } = rail();
    const a = win();
    a.intent.enterRing("x", "focus");
    clock.tick(PEEK_OPEN_MS);
    expect(a.shown).toEqual(["x"]);
    expect(zone.isHot).toBe(false);
    a.intent.leaveRing("focus");
    a.intent.enterRing("y", "focus");
    clock.tick(PEEK_SWAP_MS);
    expect(a.shown).toEqual(["x", "y"]);
    // The pointer then comes in from outside: cold.
    a.intent.dismiss();
    zone.enter();
    a.intent.enterRing("z", "pointer");
    clock.tick(RAIL_COLD_OPEN_MS - 1);
    expect(a.shown).toEqual(["x", "y", null]);
  });

  it("the keyboard reaching a ring the cold pointer waits on takes its own timing; a second pointer arrival keeps the first's clock", () => {
    const { clock, zone, win } = rail();
    const a = win();
    zone.enter();
    a.intent.enterRing("x", "pointer");
    clock.tick(100);
    a.intent.enterRing("x", "pointer");
    clock.tick(RAIL_COLD_OPEN_MS - 100);
    expect(a.shown).toEqual(["x"]);
    a.intent.dismiss();
    a.intent.leaveRing("pointer");
    zone.leave();
    clock.tick(RAIL_COOL_MS);
    zone.enter();
    a.intent.enterRing("y", "pointer");
    a.intent.enterRing("y", "focus");
    clock.tick(PEEK_OPEN_MS);
    expect(a.shown).toEqual(["x", null, "y"]);
  });

  it("a press heats the rail; a pinned window still moves for no hover", () => {
    const { clock, zone, win } = rail();
    const a = win();
    const b = win();
    zone.enter();
    a.intent.press("x");
    expect(zone.isHot).toBe(true);
    a.intent.enterRing("y", "pointer");
    clock.tick(10_000);
    expect(a.shown).toEqual(["x"]);
    // Another window's ring is hot.
    b.intent.enterRing("f", "pointer");
    expect(b.shown).toEqual(["f"]);
  });
});

describe("PlusPopIntent on the zone", () => {
  function plus() {
    const r = rail();
    const opened: string[] = [];
    const intent = new PlusPopIntent(
      { seed: () => {}, cancelSeed: () => {}, open: (d) => opened.push(d), pin: () => {}, close: () => {} },
      r.clock,
      r.pace,
    );
    return { ...r, opened, intent };
  }

  it("cold: waits the cold wait and its read; hot: shows as soon as its read is in", () => {
    const { clock, zone, intent, opened } = plus();
    zone.enter();
    intent.enterPlus();
    intent.seeded("+");
    clock.tick(RAIL_COLD_OPEN_MS - 1);
    expect(opened).toEqual([]);
    clock.tick(1);
    expect(opened).toEqual(["+"]);
    expect(zone.isHot).toBe(true);
    intent.leavePlus();
    intent.enterPlus("draft-1");
    expect(opened).toEqual(["+"]);
    intent.seeded("draft-1");
    expect(opened).toEqual(["+", "draft-1"]);
  });

  it("a cold pass over the + opens nothing", () => {
    const { clock, zone, intent, opened } = plus();
    zone.enter();
    intent.enterPlus();
    intent.seeded("+");
    clock.tick(RAIL_COLD_OPEN_MS - 1);
    intent.leavePlus();
    clock.tick(10_000);
    expect(opened).toEqual([]);
  });
});
