// The rail's zone, watched (rail-zone.ts): the document's pointer crossings
// feed the one zone while a rail stands; with none, every machine keeps its
// usual wait.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { RAIL_COLD_OPEN_MS, RAIL_HOT_OPEN_MS } from "./rail-zone-lib";
import { RAIL_ZONE_SELECTOR, createRailZoneWatch, inRailZone, installRailZone, railPace, railZoneForTests, resetRailZoneForTests, setRailZoneClockForTests } from "./rail-zone";

type Fn = (e: PointerEvent) => void;

function fakeDoc() {
  const on = new Map<string, Set<Fn>>();
  return {
    addEventListener(type: string, fn: Fn, capture: boolean) {
      expect(capture).toBe(true);
      if (!on.has(type)) on.set(type, new Set());
      on.get(type)!.add(fn);
    },
    removeEventListener(type: string, fn: Fn) {
      on.get(type)?.delete(fn);
    },
    fire(type: string, e: Partial<PointerEvent>) {
      for (const fn of on.get(type) ?? []) fn({ pointerType: "mouse", ...e } as PointerEvent);
    },
    count: () => [...on.values()].reduce((n, s) => n + s.size, 0),
  };
}

/** An element `closest` answers for: in the zone or not. */
const el = (inZone: boolean) => ({ closest: (s: string) => (s === RAIL_ZONE_SELECTOR && inZone ? {} : null) }) as unknown as EventTarget;

const offs: (() => void)[] = [];
// The zone's clock stands still unless a test moves it: no real stall can
// eat into a cold wait.
let now = 0;
beforeEach(() => {
  now = 0;
  setRailZoneClockForTests(() => now);
});
afterEach(() => {
  for (const off of offs.splice(0)) off();
  resetRailZoneForTests();
  setRailZoneClockForTests(null);
});

describe("installRailZone", () => {
  it("unwatched, the pace is the machine's own wait", () => {
    expect(railPace.openMs(150)).toBe(150);
    railPace.shown();
    expect(railPace.openMs(80)).toBe(80);
  });

  it("feeds the zone from the document's crossings: cold in, hot after a card, cold again on leaving", () => {
    const doc = fakeDoc();
    offs.push(installRailZone(doc));
    doc.fire("pointerover", { target: el(true) });
    expect(railZoneForTests().isInside).toBe(true);
    expect(railPace.openMs(150)).toBe(RAIL_COLD_OPEN_MS);
    railPace.shown();
    expect(railPace.openMs(150)).toBe(RAIL_HOT_OPEN_MS);
    // Over the page: out (and the heat cools off after its grace).
    doc.fire("pointerover", { target: el(false) });
    expect(railZoneForTests().isInside).toBe(false);
    expect(railZoneForTests().isHot).toBe(true);
    // A touch moves nothing.
    doc.fire("pointerover", { target: el(true), pointerType: "touch" });
    expect(railZoneForTests().isInside).toBe(false);
    // Back in, then out of the window entirely.
    doc.fire("pointerover", { target: el(true) });
    doc.fire("pointerout", { target: el(true), relatedTarget: el(false) });
    expect(railZoneForTests().isInside).toBe(true);
    doc.fire("pointerout", { target: el(true), relatedTarget: null });
    expect(railZoneForTests().isInside).toBe(false);
  });

  it("many rails, one watcher; the zone outlives a hop (one rail's unwatch, the next one's watch)", () => {
    const doc = fakeDoc();
    const a = installRailZone(doc);
    const b = installRailZone(doc);
    expect(doc.count()).toBe(3);
    doc.fire("pointerover", { target: el(true) });
    railPace.shown();
    a();
    a();
    expect(doc.count()).toBe(3);
    expect(railPace.openMs(150)).toBe(RAIL_HOT_OPEN_MS);
    b();
    expect(doc.count()).toBe(0);
    // Unwatched: every machine keeps its usual wait…
    expect(railPace.openMs(150)).toBe(150);
    // …and the next rail's watch finds the rail as hot as it was.
    offs.push(installRailZone(doc));
    expect(doc.count()).toBe(3);
    expect(railPace.openMs(150)).toBe(RAIL_HOT_OPEN_MS);
    doc.fire("pointerover", { target: el(false) });
    expect(railZoneForTests().isInside).toBe(false);
  });

  it("a press in the zone heats it; a press outside, or a touch, does not", () => {
    const doc = fakeDoc();
    offs.push(installRailZone(doc));
    doc.fire("pointerdown", { target: el(false) });
    doc.fire("pointerdown", { target: el(true), pointerType: "touch" });
    expect(railZoneForTests().isHot).toBe(false);
    doc.fire("pointerover", { target: el(true) });
    expect(railPace.openMs(150)).toBe(RAIL_COLD_OPEN_MS);
    doc.fire("pointerdown", { target: el(true) });
    expect(railPace.openMs(150)).toBe(RAIL_HOT_OPEN_MS);
  });

  it("time in the zone heats it, off any ring", () => {
    const doc = fakeDoc();
    offs.push(installRailZone(doc));
    doc.fire("pointerover", { target: el(true) });
    now += RAIL_COLD_OPEN_MS - 1;
    expect(railZoneForTests().isHot).toBe(false);
    now += 1;
    expect(railPace.openMs(150)).toBe(RAIL_HOT_OPEN_MS);
  });

  it("inRailZone asks the element's closest, and a target without one is outside", () => {
    expect(inRailZone(el(true))).toBe(true);
    expect(inRailZone(el(false))).toBe(false);
    expect(inRailZone(null)).toBe(false);
    expect(inRailZone({} as EventTarget)).toBe(false);
  });
});

describe("createRailZoneWatch: a zone per rail (the left sidebar's own, shell/left-rail)", () => {
  it("each watch reads its own selector and its own heat", () => {
    const left = createRailZoneWatch("#left");
    left.setClockForTests(() => now);
    const doc = fakeDoc();
    offs.push(installRailZone(doc), left.install(doc));
    const leftEl = { closest: (s: string) => (s === "#left" ? {} : null) } as unknown as EventTarget;
    // Into the left sidebar: its zone is entered, the Claude rail's is not.
    doc.fire("pointerover", { target: leftEl });
    expect(left.zoneForTests().isInside).toBe(true);
    expect(railZoneForTests().isInside).toBe(false);
    left.pace.shown();
    expect(left.pace.openMs(150)).toBe(RAIL_HOT_OPEN_MS);
    // The Claude rail stays cold: a left card heats nothing there.
    doc.fire("pointerover", { target: el(true) });
    expect(railPace.openMs(150)).toBe(RAIL_COLD_OPEN_MS);
    left.resetForTests();
  });
  it("the Claude rail's zone is not the left sidebar's cards", () => {
    expect(RAIL_ZONE_SELECTOR).toContain(".claude-rail-peek:not(.is-left)");
    expect(RAIL_ZONE_SELECTOR).toContain(".claude-rail-peek-bridge:not(.is-left)");
  });
});
