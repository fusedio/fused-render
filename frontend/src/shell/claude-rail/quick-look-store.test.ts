// Quick look (owner, 2026-11-10; design.md "Space quick look"): held by
// Space, aimed by the pointer on a list ring, swapped after PEEK_SWAP_MS,
// carried across the gap by PEEK_CLOSE_MS, gone the moment Space is up.
import { beforeEach, describe, expect, it } from "bun:test";
import { PEEK_CLOSE_MS, PEEK_SWAP_MS, type Timers } from "./rail-peek-lib";
import {
  aimQuickLook,
  dismissQuickLook,
  enterQuickLookCard,
  holdQuickLook,
  leaveQuickLookCard,
  quickLookState,
  releaseQuickLook,
  resetQuickLookForTests,
  subscribeQuickLook,
  unaimQuickLook,
  type QuickLookAim,
} from "./quick-look-store";

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

const aim = (key: string, top = 0): QuickLookAim => ({
  anchor: { left: 900, top, width: 32, height: 32 },
  chat: { file: `/p/${key}`, sessionId: `s-${key}`, title: key, status: "done" },
});

let clock: Clock;
beforeEach(() => {
  clock = new Clock();
  resetQuickLookForTests(clock);
});

describe("quick look store", () => {
  it("does nothing unless Space is held", () => {
    aimQuickLook("a", aim("a"));
    clock.tick(10_000);
    expect(quickLookState().key).toBeNull();
    expect(quickLookState().held).toBe(false);
  });

  it("hold shows nothing; an aim opens after the swap delay", () => {
    holdQuickLook();
    expect(quickLookState()).toMatchObject({ held: true, key: null, seq: 0 });
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS - 1);
    expect(quickLookState().key).toBeNull();
    clock.tick(1);
    expect(quickLookState()).toMatchObject({ key: "a", seq: 1 });
    expect(quickLookState().chat?.sessionId).toBe("s-a");
  });

  it("another ring swaps the same card after the delay (no new opening)", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS);
    unaimQuickLook();
    aimQuickLook("b", aim("b", 40));
    clock.tick(PEEK_SWAP_MS - 1);
    expect(quickLookState().key).toBe("a");
    clock.tick(1);
    expect(quickLookState()).toMatchObject({ key: "b", seq: 1 });
    expect(quickLookState().anchor?.top).toBe(40);
  });

  it("a ring crossed before the delay opens nothing", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS / 2);
    unaimQuickLook();
    clock.tick(10_000);
    expect(quickLookState().key).toBeNull();
  });

  it("back on the shown ring calls off a pending swap", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS);
    unaimQuickLook();
    aimQuickLook("b", aim("b"));
    clock.tick(PEEK_SWAP_MS / 2);
    unaimQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(10_000);
    expect(quickLookState().key).toBe("a");
  });

  it("leaving the ring for the card keeps it; leaving both closes after the grace, held stays", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS);
    unaimQuickLook();
    clock.tick(PEEK_CLOSE_MS - 10);
    enterQuickLookCard();
    clock.tick(10_000);
    expect(quickLookState().key).toBe("a");
    leaveQuickLookCard();
    clock.tick(PEEK_CLOSE_MS - 1);
    expect(quickLookState().key).toBe("a");
    clock.tick(1);
    expect(quickLookState()).toMatchObject({ held: true, key: null });
  });

  it("release closes at once and drops the hold; a later aim needs a new hold", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS);
    releaseQuickLook();
    expect(quickLookState()).toMatchObject({ held: false, key: null, chat: null, anchor: null });
    aimQuickLook("b", aim("b"));
    clock.tick(10_000);
    expect(quickLookState().key).toBeNull();
  });

  it("release cancels a pending aim", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    releaseQuickLook();
    holdQuickLook();
    clock.tick(10_000);
    expect(quickLookState().key).toBeNull();
  });

  it("dismiss (Escape, a ring's press) closes but keeps the hold", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS);
    dismissQuickLook();
    expect(quickLookState()).toMatchObject({ held: true, key: null });
    aimQuickLook("b", aim("b"));
    clock.tick(PEEK_SWAP_MS);
    expect(quickLookState()).toMatchObject({ key: "b", seq: 2 });
  });

  it("un-aimed with the pointer already on the card: it stays", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS);
    enterQuickLookCard();
    unaimQuickLook();
    clock.tick(10_000);
    expect(quickLookState().key).toBe("a");
  });

  it("leaving the card for a ring the pointer rests on: it stays", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS);
    enterQuickLookCard();
    aimQuickLook("a", aim("a"));
    leaveQuickLookCard();
    clock.tick(10_000);
    expect(quickLookState().key).toBe("a");
  });

  it("a ring that left the DOM under the pointer no longer holds it: the grace closes it", () => {
    const ring = { isConnected: true } as HTMLElement;
    holdQuickLook();
    aimQuickLook("a", { ...aim("a"), ring });
    clock.tick(PEEK_SWAP_MS);
    enterQuickLookCard();
    // The ring unmounts: no pointerleave ever comes, `pointerKey` stays "a".
    (ring as { isConnected: boolean }).isConnected = false;
    leaveQuickLookCard();
    clock.tick(PEEK_CLOSE_MS);
    expect(quickLookState()).toMatchObject({ held: true, key: null, ring: null });
  });

  it("back on the shown ring during the grace calls the close off", () => {
    holdQuickLook();
    aimQuickLook("a", aim("a"));
    clock.tick(PEEK_SWAP_MS);
    unaimQuickLook();
    clock.tick(PEEK_CLOSE_MS - 1);
    aimQuickLook("a", aim("a"));
    clock.tick(10_000);
    expect(quickLookState().key).toBe("a");
  });

  it("tells subscribers only on change", () => {
    let n = 0;
    subscribeQuickLook(() => n++);
    holdQuickLook();
    holdQuickLook();
    expect(n).toBe(1);
    unaimQuickLook();
    dismissQuickLook();
    expect(n).toBe(1);
  });
});
