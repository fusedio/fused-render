// Quick look in place, the window's half (rail-peek-lib `PreviewIntent`;
// owner, 2026-11-10; design.md "Quick look v2"): a preview key beside the
// pinned one — 80ms swap, 250ms grace, back to pinned on a clear or a prune —
// and the pinned chat never written.
import { beforeEach, describe, expect, it } from "bun:test";
import {
  PEEK_CLOSE_MS,
  PEEK_KEEP,
  PEEK_SWAP_MS,
  PreviewIntent,
  keepAlivePinned,
  lookShown,
  quickLookPress,
  type Timers,
} from "./rail-peek-lib";
import { liveLookRing, lookTarget, previewSink, previewable, shiftIsPlain, turnedAims } from "./quick-look-lib";
import type { RailRow } from "./claude-rail-lib";

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
  it("an aim shows after the swap delay, warming the chat as the swap lands", () => {
    look.aim("Y");
    expect(warmed).toEqual([]);
    clock.tick(PEEK_SWAP_MS - 1);
    expect(look.current).toBeNull();
    clock.tick(1);
    expect(look.current).toBe("Y");
    expect(shown).toEqual(["Y"]);
    expect(warmed).toEqual(["Y"]);
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
    // The ring crossed too fast was never mounted.
    expect(warmed).toEqual(["Y", "W"]);
  });

  it("the bridge or the cone holds a preview across the gap, whatever order card and cone report in", () => {
    look.aim("Y");
    clock.tick(PEEK_SWAP_MS);
    look.unaim();
    look.cone(true);
    clock.tick(10_000);
    expect(look.current).toBe("Y");
    // The card's enter, then the cone's leave: still on the card.
    look.enterCard();
    look.cone(false);
    clock.tick(10_000);
    expect(look.current).toBe("Y");
    // Back out through the bridge to nowhere: the grace.
    look.cone(true);
    look.leaveCard();
    look.cone(false);
    clock.tick(PEEK_CLOSE_MS);
    expect(look.current).toBeNull();
  });

  it("clear forgets the pointer on the card (an unmounted card sends no leave)", () => {
    look.aim("Y");
    clock.tick(PEEK_SWAP_MS);
    look.enterCard();
    look.clear();
    look.aim("Z");
    clock.tick(PEEK_SWAP_MS);
    look.unaim();
    clock.tick(PEEK_CLOSE_MS);
    expect(look.current).toBeNull();
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

describe("the wiring (quick-look-lib)", () => {
  const seat = (key: string, session: string | null): RailRow =>
    ({ pulse: { key, session_id: session ?? undefined }, row: null }) as unknown as RailRow;

  it("window A's sink writes the preview key and the kept set — never the pinned chat", () => {
    let preview = null as string | null;
    let kept: readonly string[] = ["X", "A", "B"];
    const pinned = { key: "X" };
    const writes: string[] = [];
    const a = new PreviewIntent(
      previewSink({
        setPreview: (k) => {
          writes.push("preview");
          preview = k;
        },
        setKept: (fn) => {
          writes.push("kept");
          kept = fn(kept);
        },
        pinned: () => pinned.key,
      }),
      clock,
    );
    for (const k of ["Y", "Z", "W"]) {
      a.aim(k);
      clock.tick(PEEK_SWAP_MS);
      a.unaim();
    }
    expect(preview).toBe("W");
    expect(lookShown(preview, pinned.key)).toBe("W");
    expect(kept).toContain("X");
    a.clear();
    expect(preview).toBeNull();
    expect(lookShown(preview, pinned.key)).toBe("X");
    expect(pinned.key).toBe("X");
    expect(new Set(writes)).toEqual(new Set(["preview", "kept"]));
  });

  it("one predicate: a window previews only while it draws a PINNED CHAT", () => {
    expect(previewable({ shown: seat("X", "s"), pinned: true })).toBe(true);
    expect(previewable({ shown: seat("X", "s"), pinned: false })).toBe(false);
    expect(previewable({ shown: seat("X", null), pinned: true })).toBe(false);
    expect(previewable({ shown: null, pinned: true })).toBe(false);
  });

  it("lookAt's gate: Shift held, a window's ring, that window previewable, the ring still drawn", () => {
    const base = { held: true, section: "a" as const, looks: true, live: true, key: "Y", pinned: "X" };
    expect(lookTarget(base)).toBe("aim");
    expect(lookTarget({ ...base, key: "X" })).toBe("clear");
    expect(lookTarget({ ...base, held: false })).toBeNull();
    expect(lookTarget({ ...base, section: null })).toBeNull();
    expect(lookTarget({ ...base, looks: false })).toBeNull();
    expect(lookTarget({ ...base, live: false })).toBeNull();
  });

  it("a rested ring that left its section is forgotten", () => {
    const ring = { key: "Y", section: "a" as const };
    expect(liveLookRing(ring, new Set(["Y"]), new Set())).toBe(ring);
    expect(liveLookRing(ring, new Set(["X"]), new Set(["Y"]))).toBeNull();
    expect(liveLookRing({ key: "F", section: "b" as const }, new Set(), new Set(["F"]))).not.toBeNull();
    expect(liveLookRing(null, new Set(), new Set())).toBeNull();
  });

  it("Shift pressed over a resting ring aims it — never with a field holding the caret", () => {
    expect(turnedAims({ turned: true, ring: true, editable: false })).toBe(true);
    expect(turnedAims({ turned: true, ring: true, editable: true })).toBe(false);
    expect(turnedAims({ turned: false, ring: true, editable: false })).toBe(false);
    expect(turnedAims({ turned: true, ring: false, editable: false })).toBe(false);
  });

  it("Shift+press is plain only while a window previews", () => {
    expect(shiftIsPlain({ held: true, previewing: true })).toBe(true);
    expect(shiftIsPlain({ held: true, previewing: false })).toBe(false);
    expect(shiftIsPlain({ held: false, previewing: false })).toBe(false);
  });
});
