// The mode cards off the rail's mode doors (owner, 2026-11-20): one Git-card
// store PER MODE — its own card, pin and mark — read together for the rail's
// rules, and the rail's facts by rail.
import { beforeEach, describe, expect, it } from "bun:test";
import { aPinned, setAPinned } from "@platform/lib/last-chat";
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, type Timers } from "./rail-peek-lib";
import {
  closeModeCard,
  closeModeCards,
  flushThenOpen,
  forgetModeCardRail,
  modeCardRail,
  modeCardStore,
  modeCards,
  modeDoorAction,
  modeDoorSelector,
  modeKey,
  modeOfKey,
  modePeekId,
  publishRailModes,
  railModesOf,
  railWithMode,
  resetModeCardsForTests,
  setModeCardRail,
  type RailModes,
} from "./mode-peek-store";
import { aPinWord, readAPin } from "./rail-pin-lib";

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

const el = () => ({}) as unknown as HTMLElement;
const facts = (path: string, modes: string[]): RailModes => ({
  path,
  doors: modes.map((mode) => ({ mode, title: mode, icon: null })),
  src: (mode) => `/explorer/embed${path}?_mode=${mode}`,
  onOpen: () => {},
});

let clock: Clock;
beforeEach(() => {
  clock = new Clock();
  resetModeCardsForTests(clock);
  setAPinned(null);
});

describe("modeKey", () => {
  it("one key per mode, read back; anything else is no mode", () => {
    expect(modeKey("code")).toBe("mode:code");
    expect(modeOfKey(modeKey("_render"))).toBe("_render");
    expect(modeOfKey("git")).toBeNull();
    expect(modeOfKey("mode:")).toBeNull();
    expect(modeOfKey(null)).toBeNull();
  });
  it("is window A's mark for that mode's pin (rail-pin-lib)", () => {
    expect(aPinWord({ kind: "mode", mode: "code" })).toBe(modeKey("code"));
    expect(readAPin("mode:reader")).toEqual({ kind: "mode", mode: "reader" });
    expect(readAPin("mode:")).toBeNull();
    expect(readAPin("git")).toEqual({ kind: "git" });
  });
  it("a card id and a door selector per mode", () => {
    expect(modePeekId("code")).toBe("claude-rail-mode-peek-code");
    expect(modePeekId("_render")).toBe("claude-rail-mode-peek-_render");
    expect(modePeekId("a b")).toBe("claude-rail-mode-peek-a_b");
    expect(modeDoorSelector("code")).toBe('[data-rail-door="mode"][data-rail-mode="code"]');
  });
});

describe("modeDoorAction", () => {
  it("no card: the page's own switch; else a toggle (the Git door's)", () => {
    expect(modeDoorAction({ ready: false, pinned: false })).toBe("open");
    expect(modeDoorAction({ ready: true, pinned: false })).toBe("pin");
    expect(modeDoorAction({ ready: true, pinned: true })).toBe("close");
  });
});

describe("a card per mode", () => {
  it("each mode has its own store, id and intent; one's hover and pin move no other", () => {
    const code = modeCardStore("code");
    const reader = modeCardStore("reader");
    expect(modeCardStore("code")).toBe(code);
    expect(code).not.toBe(reader);
    expect([code.id, reader.id]).toEqual(["claude-rail-mode-peek-code", "claude-rail-mode-peek-reader"]);
    code.intent().press(code.key);
    expect(code.state()).toMatchObject({ up: true, pinned: true, seq: 1 });
    expect(reader.state().up).toBe(false);
    // A pinned Code card: the Reader door still previews its own card.
    reader.intent().enterRing(reader.key);
    clock.tick(PEEK_OPEN_MS);
    expect(reader.state()).toMatchObject({ up: true, pinned: false });
    expect(code.state()).toMatchObject({ up: true, pinned: true });
    reader.intent().leaveRing();
    clock.tick(PEEK_CLOSE_MS);
    expect(reader.state().up).toBe(false);
    expect(code.state().up).toBe(true);
  });

  it("read together, and told of any one's change", () => {
    const code = modeCardStore("code");
    modeCardStore("reader");
    const before = modeCards();
    expect(before.modes).toEqual(["code", "reader"]);
    expect(before.of("code").up).toBe(false);
    expect(before.of("nope")).toMatchObject({ up: false, ready: false });
    code.intent().press(code.key);
    const after = modeCards();
    expect(after).not.toBe(before);
    expect(after.of("code")).toMatchObject({ up: true, pinned: true });
    expect(modeCards()).toBe(after);
  });

  it("a pin's mark is that mode's, and goes with its card", () => {
    const code = modeCardStore("code");
    const reader = modeCardStore("reader");
    code.intent().press(code.key);
    code.setHeld(true);
    expect(aPinned()).toBe("mode:code");
    // Another mode's card pinned takes the mark; the first one going leaves it.
    reader.intent().press(reader.key);
    reader.setHeld(true);
    expect(aPinned()).toBe("mode:reader");
    closeModeCard("code");
    expect(aPinned()).toBe("mode:reader");
    closeModeCard("reader");
    expect(aPinned()).toBeNull();
  });

  it("closeModeCards closes every card up that is not asked to stand", () => {
    const code = modeCardStore("code");
    const reader = modeCardStore("reader");
    code.intent().press(code.key);
    reader.intent().press(reader.key);
    closeModeCards((m) => m === "reader");
    expect([code.state().up, reader.state().up]).toEqual([false, true]);
    closeModeCards();
    expect(reader.state().up).toBe(false);
  });
});

describe("the rail that opened a card", () => {
  it("its rail gone: a preview closes, the opener is forgotten; another mode's is untouched", () => {
    const rail = el();
    const other = el();
    const code = modeCardStore("code");
    const reader = modeCardStore("reader");
    setModeCardRail("code", rail);
    setModeCardRail("reader", other);
    code.intent().enterRing(code.key);
    reader.intent().enterRing(reader.key);
    clock.tick(PEEK_OPEN_MS);
    forgetModeCardRail(rail);
    expect(code.state().up).toBe(false);
    expect(modeCardRail("code")).toBeNull();
    expect(reader.state().up).toBe(true);
    expect(modeCardRail("reader")).toBe(other);
  });

  it("only a held card outlives its rail", () => {
    const rail = el();
    const code = modeCardStore("code");
    setModeCardRail("code", rail);
    code.intent().press(code.key);
    forgetModeCardRail(rail);
    expect(code.state().up).toBe(false);
    setModeCardRail("code", rail);
    code.intent().press(code.key);
    code.setHeld(true);
    forgetModeCardRail(rail);
    expect(code.state()).toMatchObject({ up: true, held: true });
  });
});

describe("the rails' facts", () => {
  it("by rail; a store stands behind every door; the latest rail with a mode's door", () => {
    const a = el();
    const b = el();
    expect(railWithMode("code")).toBeNull();
    publishRailModes(a, facts("/x/a.html", ["code"]));
    publishRailModes(b, facts("/x/b.md", ["code", "reader"]));
    expect(modeCards().modes).toEqual(expect.arrayContaining(["code", "reader"]));
    expect(railModesOf(a)?.path).toBe("/x/a.html");
    expect(railModesOf(b)?.doors.map((d) => d.mode)).toEqual(["code", "reader"]);
    expect(railModesOf(b)?.src("reader")).toBe("/explorer/embed/x/b.md?_mode=reader");
    expect(railWithMode("code")).toBe(b);
    expect(railWithMode("reader")).toBe(b);
    publishRailModes(b, null);
    expect(railWithMode("code")).toBe(a);
    expect(railWithMode("reader")).toBeNull();
    publishRailModes(a, null);
    expect(railModesOf(a)).toBeNull();
  });
});

describe("flushThenOpen", () => {
  const run = async (o: { flush?: () => Promise<{ ok: boolean } | undefined>; still?: boolean; timeoutMs?: number }) => {
    let went = 0;
    const ok = await flushThenOpen({
      ...(o.flush ? { flush: o.flush } : {}),
      still: () => o.still ?? true,
      go: () => went++,
      ...(o.timeoutMs !== undefined ? { timeoutMs: o.timeoutMs } : {}),
    });
    return { ok, went };
  };
  it("no flush to ask, or one that wrote: opens", async () => {
    expect(await run({})).toEqual({ ok: true, went: 1 });
    expect(await run({ flush: async () => ({ ok: true }) })).toEqual({ ok: true, went: 1 });
    // The page's own reading (Preview `doSetMode`): only an explicit no refuses.
    expect(await run({ flush: async () => undefined })).toEqual({ ok: true, went: 1 });
  });
  it("a flush that failed, threw or did not answer in time: no open, the card stays", async () => {
    expect(await run({ flush: async () => ({ ok: false }) })).toEqual({ ok: false, went: 0 });
    expect(
      await run({
        flush: async () => {
          throw new Error("write failed");
        },
      }),
    ).toEqual({ ok: false, went: 0 });
    expect(await run({ flush: () => new Promise(() => {}), timeoutMs: 5 })).toEqual({ ok: false, went: 0 });
  });
  it("the preview changed or closed while the flush was out: no open", async () => {
    expect(await run({ flush: async () => ({ ok: true }), still: false })).toEqual({ ok: false, went: 0 });
  });
});
