// The rail's two windows (owner, 2026-11-05): A (the list's card) and B (the
// favourites'), their independence, B's hover mode, and the pane collapsing
// giving A back. The parts without a DOM.
import { afterEach, describe, expect, it } from "bun:test";
import type { TaskPulseTask } from "@platform/lib/api";
import { collapseRestore, paneHoldAfter, paneWas, ringMarks, type RingMarkInput } from "./claude-rail-lib";
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

describe("the pane collapsing gives window A back", () => {
  const was = (o: Partial<Parameters<typeof paneWas>[0]>) =>
    paneWas({ paneOpen: true, paneKey: null, anySession: false, remembered: null, holdUntil: 0, now: 100, ...o });
  it("a pane on a list chat remembers it; a pane on another chat remembers nothing", () => {
    expect(was({ paneKey: "A" })).toBe("A");
    expect(was({ anySession: true, remembered: "A" })).toBeNull();
  });
  it("a pane on home forgets — except while a taken chat is still mounting; shut keeps", () => {
    expect(was({ remembered: "A" })).toBeNull();
    expect(was({ remembered: "A", holdUntil: 200 })).toBe("A");
    expect(was({ paneOpen: false, remembered: "A" })).toBe("A");
  });
  const restore = (o: Partial<Parameters<typeof collapseRestore>[0]>) =>
    collapseRestore({ wasOpen: true, open: false, favsOn: true, key: "A", isList: true, ...o });
  it("restores on the open -> shut turn only, for a list chat; home restores nothing", () => {
    expect(restore({})).toBe("A");
    expect(restore({ wasOpen: false })).toBeNull();
    expect(restore({ open: true })).toBeNull();
    expect(restore({ key: null })).toBeNull();
    expect(restore({ favsOn: false })).toBeNull();
    expect(restore({ isList: false })).toBeNull();
  });
  it("pin -> expand -> collapse -> expand -> home -> collapse restores nothing", () => {
    // Expand takes pinned A: held 4 s while its chat mounts.
    let st = { key: "A" as string | null, hold: 4100 };
    const step = (o: { paneKey: string | null; anySession: boolean; now: number }) => {
      st.key = was({ ...o, remembered: st.key, holdUntil: st.hold });
      st.hold = paneHoldAfter(st.hold, o.anySession);
    };
    step({ paneKey: "A", anySession: true, now: 200 }); // chat mounted on A
    expect(st).toEqual({ key: "A", hold: 0 });
    expect(restore({ key: st.key })).toBe("A"); // collapse gives A back
    st = { key: "A", hold: 4100 }; // expand again, A taken again
    step({ paneKey: "A", anySession: true, now: 300 });
    step({ paneKey: null, anySession: false, now: 400 }); // Back to chats, within 4 s
    expect(st.key).toBeNull();
    expect(restore({ key: st.key })).toBeNull();
  });
  afterEach(() => resetFavHoverForTests());
});
