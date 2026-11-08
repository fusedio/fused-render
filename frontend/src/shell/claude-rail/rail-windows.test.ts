// The rail's two windows (owner, 2026-11-05): A (the list's card) and B (the
// favourites'), their independence and B's hover mode. The parts without a
// DOM. (The pane collapsing gave A back until 2026-11-06; it gives nothing
// of A back now — claude-rail-lib.)
import { describe, expect, it } from "bun:test";
import type { TaskPulseTask } from "@platform/lib/api";
import { ringMarks, windowAHovers, type RingMarkInput } from "./claude-rail-lib";
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, PeekIntent, favHoverRow, listCardWish, favCardWish, type Timers } from "./rail-peek-lib";
import { dismissFavHover, favHoverKey, resetFavHoverForTests } from "./fav-peek-store";

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

function window_() {
  const clock = new Clock();
  const shown: (string | null)[] = [];
  const intent = new PeekIntent({ show: (k) => shown.push(k), warm: () => {} }, clock);
  return { clock, shown, intent };
}

const row = (o: Partial<TaskPulseTask>): TaskPulseTask => ({ key: "f", starred: true, status: "done", session_id: "s", ...o }) as TaskPulseTask;

describe("window B hover (FavouritePeek hover mode)", () => {
  it("opens on the dwell, a press pins it, and the pin holds through leaves", () => {
    const { clock, shown, intent } = window_();
    intent.enterRing("f", "pointer");
    clock.tick(PEEK_OPEN_MS);
    expect(shown).toEqual(["f"]);
    expect(intent.isPinned).toBe(false);
    intent.press("f");
    intent.leaveRing();
    clock.tick(10_000);
    expect(shown).toEqual(["f"]);
    expect(intent.isPinned).toBe(true);
  });

  it("leaving the ring and the card closes it on the usual grace", () => {
    const { clock, shown, intent } = window_();
    intent.enterRing("f", "pointer");
    clock.tick(PEEK_OPEN_MS);
    intent.leaveRing();
    clock.tick(PEEK_CLOSE_MS - 1);
    expect(shown).toEqual(["f"]);
    clock.tick(1);
    expect(shown).toEqual(["f", null]);
  });

  it("two windows, two intents: A pinned is untouched by B's hover, and B's close", () => {
    const a = window_();
    const b = window_();
    a.intent.press("a");
    b.intent.enterRing("f", "pointer");
    b.clock.tick(PEEK_OPEN_MS);
    b.intent.leaveRing();
    b.clock.tick(PEEK_CLOSE_MS);
    a.clock.tick(10_000);
    expect(b.shown).toEqual(["f", null]);
    expect(a.shown).toEqual(["a"]);
    expect(a.intent.isPinned).toBe(true);
  });

  it("the hover row shows only with no open favourite, for a live star with a conversation", () => {
    const rows = [row({ key: "f" }), row({ key: "u", starred: false }), row({ key: "z", status: "archived" }), row({ key: "n", session_id: "" })];
    expect(favHoverRow("f", null, rows)?.key).toBe("f");
    // B open: another ring's hover is its tooltip only.
    expect(favHoverRow("f", "g", rows)).toBeNull();
    for (const k of ["u", "z", "n", "gone", null]) expect(favHoverRow(k, null, rows)).toBeNull();
  });

  it("the store's hover is local state: dismiss clears it", () => {
    resetFavHoverForTests();
    expect(favHoverKey()).toBeNull();
    dismissFavHover();
    expect(favHoverKey()).toBeNull();
  });
});

describe("tooltip-only rules per window", () => {
  const base: Omit<RingMarkInput, "key" | "foot"> = {
    favsOn: true,
    peekOn: true,
    peekKey: null,
    pinned: false,
    held: false,
    openFavKey: null,
    sessionHere: false,
    isLast: false,
    activeList: null,
    activeFav: null,
  };
  it("B open: favourite rings are tooltip-only, list rings still hover (A)", () => {
    expect(ringMarks({ ...base, key: "g", foot: true, openFavKey: "f" }).hovers).toBe(false);
    expect(ringMarks({ ...base, key: "a", foot: false, openFavKey: "f" }).hovers).toBe(true);
  });
  it("another window A card pinned (+ popover, Tasks card, a bot's card): list rings are tooltip-only, favourites still hover (B)", () => {
    expect(ringMarks({ ...base, key: "a", foot: false, windowAHeld: true }).titleOnly).toBe(true);
    expect(ringMarks({ ...base, key: "f", foot: true, windowAHeld: true }).titleOnly).toBe(false);
    expect(ringMarks({ ...base, key: "a", foot: false, windowAHeld: false }).titleOnly).toBe(false);
  });
  it("A pinned: a favourite's ring still hovers (B); another list ring is tooltip-only", () => {
    const a = { peekKey: "a", pinned: true };
    expect(ringMarks({ ...base, ...a, key: "f", foot: true }).titleOnly).toBe(false);
    expect(ringMarks({ ...base, ...a, key: "b", foot: false }).titleOnly).toBe(true);
  });
});

describe("sizes per window", () => {
  it("A: hover and click share one remembered size (oneSize); B keeps its own, capped at half the window", () => {
    const stored = { width: 500, height: 700 };
    const hover = listCardWish({ hasSession: true, stored, grown: false, oneSize: true });
    const click = listCardWish({ hasSession: true, stored, grown: true, oneSize: true });
    expect(hover).toEqual(click);
    expect(hover).toEqual({ width: 500, maxHeight: 700 });
    // B's size is not A's: the same call with B's stored size, height capped.
    const b = favCardWish({ width: 380, maxHeight: 520, align: "bottom" }, { width: 420, height: 900 }, 1000);
    expect(b.width).toBe(420);
    expect(b.maxHeight).toBe(500);
  });
});


describe("after a click, only a click changes window A (owner, 2026-11-08)", () => {
  const open = { listPinned: false, popPinned: false, tasksPinned: false, modalCard: false };
  it("with nothing pinned, every window A door previews on a hover", () => {
    expect(windowAHovers(open)).toBe(true);
  });
  it("a pinned list card silences the +, the draft rings and the Tasks door", () => {
    expect(windowAHovers({ ...open, listPinned: true })).toBe(false);
  });
  it("a pinned New task popover silences the list's rings and the Tasks door", () => {
    expect(windowAHovers({ ...open, popPinned: true })).toBe(false);
  });
  it("a pinned Tasks card silences the list's rings, the + and the draft rings", () => {
    expect(windowAHovers({ ...open, tasksPinned: true })).toBe(false);
  });
  it("a pinned bot's card (window A since 2026-11-08) silences the list's rings, the +, the drafts and the Tasks door", () => {
    expect(windowAHovers({ ...open, botPinned: true })).toBe(false);
  });
  it("a draft's modal card (the popover's flag off) silences them too", () => {
    expect(windowAHovers({ ...open, modalCard: true })).toBe(false);
  });
});
