// Quick look in place, the window's half (rail-peek-lib `PreviewIntent`;
// owner, 2026-11-10; design.md "Quick look v2"): a preview key beside the
// pinned one — 80ms swap, 250ms grace, back to pinned on a clear or a prune —
// and the pinned chat never written.
import { beforeEach, describe, expect, it } from "bun:test";
import {
  PEEK_CLOSE_MS,
  PEEK_KEEP,
  PEEK_SWAP_MS,
  PeekIntent,
  PreviewIntent,
  keepAlivePinned,
  lookShown,
  quickLookPress,
  type Timers,
} from "./rail-peek-lib";

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

let clock: Clock;
let shown: (string | null)[];
let warmed: string[];
let look: PreviewIntent;
beforeEach(() => {
  clock = new Clock();
  shown = [];
  warmed = [];
  look = new PreviewIntent({ show: (k) => shown.push(k), warm: (k) => warmed.push(k) }, clock);
});

describe("PreviewIntent", () => {
  it("an aim shows after the swap delay, warming the chat at once", () => {
    look.aim("Y");
    expect(warmed).toEqual(["Y"]);
    clock.tick(PEEK_SWAP_MS - 1);
    expect(look.current).toBeNull();
    clock.tick(1);
    expect(look.current).toBe("Y");
    expect(shown).toEqual(["Y"]);
  });

  it("another ring swaps after the same delay; a ring crossed faster shows nothing", () => {
    look.aim("Y");
    clock.tick(PEEK_SWAP_MS);
    look.unaim();
    look.aim("Z");
    clock.tick(PEEK_SWAP_MS / 2);
    look.unaim();
    look.aim("W");
    clock.tick(PEEK_SWAP_MS);
    expect(shown).toEqual(["Y", "W"]);
  });

  it("ring → card keeps it; leaving both goes back to pinned after the grace", () => {
    look.aim("Y");
    clock.tick(PEEK_SWAP_MS);
    look.unaim();
    clock.tick(PEEK_CLOSE_MS - 10);
    look.enterCard();
    clock.tick(10_000);
    expect(look.current).toBe("Y");
    look.leaveCard();
    clock.tick(PEEK_CLOSE_MS - 1);
    expect(look.current).toBe("Y");
    clock.tick(1);
    expect(look.current).toBeNull();
  });

  it("back on the previewed ring inside the grace keeps it", () => {
    look.aim("Y");
    clock.tick(PEEK_SWAP_MS);
    look.unaim();
    clock.tick(PEEK_CLOSE_MS - 1);
    look.aim("Y");
    clock.tick(10_000);
    expect(look.current).toBe("Y");
  });

  it("un-aimed with the pointer already on the card, or leaving the card for a ring: it stays", () => {
    look.aim("Y");
    clock.tick(PEEK_SWAP_MS);
    look.enterCard();
    look.unaim();
    clock.tick(10_000);
    expect(look.current).toBe("Y");
    look.aim("Y");
    look.leaveCard();
    clock.tick(10_000);
    expect(look.current).toBe("Y");
  });

  it("clear (Shift up, Escape, a press) goes back now and calls off a waiting aim", () => {
    look.aim("Y");
    clock.tick(PEEK_SWAP_MS);
    look.clear();
    expect(look.current).toBeNull();
    look.aim("Z");
    look.clear();
    clock.tick(10_000);
    expect(shown).toEqual(["Y", null]);
  });

  it("a ring that unmounts goes back to pinned (no pointerleave ever comes)", () => {
    look.aim("Y");
    clock.tick(PEEK_SWAP_MS);
    look.prune(new Set(["X"]));
    expect(look.current).toBeNull();
    // …and a waiting aim on a gone ring never lands.
    look.aim("Z");
    look.prune(new Set(["X"]));
    clock.tick(10_000);
    expect(look.current).toBeNull();
  });

  it("the pinned chat is never written: window A's intent keeps its pin through a preview", () => {
    const pinned: (string | null)[] = [];
    const a = new PeekIntent({ show: (k) => pinned.push(k), warm: () => {} }, clock);
    a.press("X");
    look.aim("Y");
    clock.tick(PEEK_SWAP_MS);
    look.unaim();
    clock.tick(10_000);
    look.clear();
    expect(a.current).toBe("X");
    expect(a.isPinned).toBe(true);
    expect(pinned).toEqual(["X"]);
    a.dispose();
  });
});

describe("shown, presses and the warm set", () => {
  it("the window shows the preview over the pinned chat", () => {
    expect(lookShown("Y", "X")).toBe("Y");
    expect(lookShown(null, "X")).toBe("X");
  });

  it("a press while previewing: the pinned ring goes back, any other commits", () => {
    expect(quickLookPress({ previewing: null, pinned: "X", ring: "Y" })).toBe("press");
    expect(quickLookPress({ previewing: "Y", pinned: "X", ring: "Y" })).toBe("commit");
    expect(quickLookPress({ previewing: "Y", pinned: "X", ring: "Z" })).toBe("commit");
    expect(quickLookPress({ previewing: "Y", pinned: "X", ring: "X" })).toBe("revert");
  });

  it("previews never push the pinned chat out of the kept set", () => {
    let kept: readonly string[] = ["X"];
    for (const k of ["A", "B", "C", "D"]) kept = keepAlivePinned(kept, k, "X");
    expect(kept.length).toBe(PEEK_KEEP);
    expect(kept[0]).toBe("D");
    expect(kept).toContain("X");
    // A pinned chat not kept is not added.
    expect(keepAlivePinned([], "A", "X")).toEqual(["A"]);
  });
});
