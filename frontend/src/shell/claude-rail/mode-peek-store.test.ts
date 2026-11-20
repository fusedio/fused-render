// The mode card off the rail's mode doors (owner, 2026-11-20): the Git card's
// store with SEVERAL DOORS ON ONE CARD — one intent key per mode, the card
// standing level with the door of the mode it shows — and the rail's facts
// by rail.
import { beforeEach, describe, expect, it } from "bun:test";
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, PEEK_SWAP_MS, type Timers } from "./rail-peek-lib";
import {
  closeModeCard,
  forgetModeCardRail,
  modeCardIntent,
  modeCardRail,
  modeCardState,
  modeCardStore,
  modeDoorAction,
  modeKey,
  modeOfKey,
  modeShown,
  publishRailModes,
  railModesAnyRail,
  railModesOf,
  registerModeDoor,
  resetModeCardForTests,
  setModeCardRail,
  type RailModes,
} from "./mode-peek-store";
import { publishTaskHeadUnified, resetTaskHeadUnifiedFlagForTests } from "@shell/task-head-flag";

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
  resetModeCardForTests(clock);
  resetTaskHeadUnifiedFlagForTests();
});

describe("modeKey", () => {
  it("one key per mode, read back; anything else is no mode", () => {
    expect(modeKey("code")).toBe("mode:code");
    expect(modeOfKey(modeKey("_render"))).toBe("_render");
    expect(modeOfKey("git")).toBeNull();
    expect(modeOfKey("mode:")).toBeNull();
    expect(modeOfKey(null)).toBeNull();
  });
});

describe("modeDoorAction", () => {
  it("no card: the page's own switch", () => {
    expect(modeDoorAction({ ready: false, own: false, pinned: false })).toBe("open");
    expect(modeDoorAction({ ready: false, own: true, pinned: true })).toBe("open");
  });
  it("its own card up by a click: a toggle; anything else shows this mode by a click", () => {
    expect(modeDoorAction({ ready: true, own: true, pinned: true })).toBe("close");
    expect(modeDoorAction({ ready: true, own: true, pinned: false })).toBe("pin");
    expect(modeDoorAction({ ready: true, own: false, pinned: true })).toBe("pin");
    expect(modeDoorAction({ ready: true, own: false, pinned: false })).toBe("pin");
  });
});

describe("the mode card's intent", () => {
  it("a hover previews its mode; a hover on the next door swaps it, the opening counted once", () => {
    const i = modeCardIntent();
    i.enterRing(modeKey("code"));
    clock.tick(PEEK_OPEN_MS);
    expect(modeCardState()).toMatchObject({ up: true, pinned: false, seq: 1 });
    expect(modeShown()).toBe("code");
    i.leaveRing();
    i.enterRing(modeKey("reader"));
    clock.tick(PEEK_SWAP_MS);
    expect(modeShown()).toBe("reader");
    // A swap between its doors is not an opening (ClaudeRail's one-card rule
    // and the chassis key on `seq`).
    expect(modeCardState()).toMatchObject({ up: true, pinned: false, seq: 1 });
    i.leaveRing();
    clock.tick(PEEK_CLOSE_MS);
    expect(modeCardState().up).toBe(false);
    // Kept through the close: the card fades out on it.
    expect(modeShown()).toBe("reader");
  });

  it("a press pins; a pinned card moves for no hover, only for another door's press", () => {
    const i = modeCardIntent();
    i.press(modeKey("code"));
    expect(modeCardState()).toMatchObject({ up: true, pinned: true, seq: 1 });
    i.enterRing(modeKey("reader"));
    clock.tick(PEEK_OPEN_MS + PEEK_SWAP_MS);
    expect(modeShown()).toBe("code");
    i.press(modeKey("reader"));
    expect(modeShown()).toBe("reader");
    expect(modeCardState()).toMatchObject({ up: true, pinned: true, seq: 1 });
    closeModeCard();
    expect(modeCardState()).toMatchObject({ up: false, pinned: false, held: false });
  });
});

describe("the card's door", () => {
  it("is the door of the mode it shows, on that rail; none once that door is gone", () => {
    const rail = el();
    const code = el();
    const reader = el();
    const offCode = registerModeDoor(rail, "code", code);
    registerModeDoor(rail, "reader", reader);
    expect(modeCardStore.doorOf(rail)).toBeNull();
    modeCardIntent().press(modeKey("code"));
    expect(modeCardStore.doorOf(rail)).toBe(code);
    modeCardIntent().press(modeKey("reader"));
    expect(modeCardStore.doorOf(rail)).toBe(reader);
    modeCardIntent().press(modeKey("code"));
    offCode();
    expect(modeCardStore.doorOf(rail)).toBeNull();
    expect(modeCardStore.doorOf(el())).toBeNull();
    expect(modeCardStore.doorOf(null)).toBeNull();
  });
});

describe("the rail that opened it", () => {
  it("its rail gone: a preview closes, the opener is forgotten", () => {
    const rail = el();
    setModeCardRail(rail);
    modeCardIntent().enterRing(modeKey("code"));
    clock.tick(PEEK_OPEN_MS);
    forgetModeCardRail(el());
    expect(modeCardState().up).toBe(true);
    forgetModeCardRail(rail);
    expect(modeCardState().up).toBe(false);
    expect(modeCardRail()).toBeNull();
  });

  it("with the pin, only a held card outlives its rail", () => {
    publishTaskHeadUnified({ enabled: true });
    const rail = el();
    setModeCardRail(rail);
    modeCardIntent().press(modeKey("code"));
    forgetModeCardRail(rail);
    expect(modeCardState().up).toBe(false);
    setModeCardRail(rail);
    modeCardIntent().press(modeKey("code"));
    modeCardStore.setHeld(true);
    forgetModeCardRail(rail);
    expect(modeCardState()).toMatchObject({ up: true, held: true });
  });
});

describe("the rails' facts", () => {
  it("by rail; the latest rail with doors; gone when taken off", () => {
    const a = el();
    const b = el();
    expect(railModesAnyRail()).toBeNull();
    publishRailModes(a, facts("/x/a.html", ["code"]));
    publishRailModes(b, facts("/x/b.md", ["code", "reader"]));
    expect(railModesOf(a)?.path).toBe("/x/a.html");
    expect(railModesOf(b)?.doors.map((d) => d.mode)).toEqual(["code", "reader"]);
    expect(railModesOf(b)?.src("reader")).toBe("/explorer/embed/x/b.md?_mode=reader");
    expect(railModesAnyRail()).toBe(b);
    publishRailModes(b, null);
    expect(railModesAnyRail()).toBe(a);
    publishRailModes(a, null);
    expect(railModesOf(a)).toBeNull();
    expect(railModesAnyRail()).toBeNull();
  });
});
