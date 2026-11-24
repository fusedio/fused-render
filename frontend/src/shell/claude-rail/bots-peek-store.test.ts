// The Bots card off the rail's Bots door (owner, 2026-11-09): the Tasks
// card's store on another key — hover previews, a press pins, a press on the
// pinned door closes, a modified press is the link's own, and the two stores
// are two: one's card up says nothing to the other.
import { beforeEach, describe, expect, it } from "bun:test";
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, type Timers } from "./rail-peek-lib";
import {
  BOTS_CARD_KEY,
  BOTS_PEEK_ID,
  botsCardIntent,
  botsCardState,
  botsDoorAction,
  closeBotsCard,
  focusBotsCard,
  railBotsDoor,
  registerBotsCard,
  registerBotsCardFocus,
  registerBotsDoor,
  resetBotsCardForTests,
  setBotsCardRoom,
} from "./bots-peek-store";
import { TASKS_CARD_KEY, TASKS_PEEK_ID, resetTasksCardForTests, tasksCardIntent, tasksCardState } from "./tasks-peek-store";

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
beforeEach(() => {
  clock = new Clock();
  resetBotsCardForTests(clock);
  resetTasksCardForTests(clock);
});

describe("botsDoorAction", () => {
  it("a modified press is the link's own; no card: the link; else a toggle", () => {
    expect(botsDoorAction({ modified: true, ready: true, pinned: false })).toBe("native");
    expect(botsDoorAction({ modified: false, ready: false, pinned: false })).toBe("navigate");
    expect(botsDoorAction({ modified: false, ready: true, pinned: false })).toBe("pin");
    expect(botsDoorAction({ modified: false, ready: true, pinned: true })).toBe("close");
  });
});

describe("the Bots card's intent", () => {
  it("a hover previews after window A's dwell and goes on the grace", () => {
    const i = botsCardIntent();
    i.enterRing(BOTS_CARD_KEY);
    clock.tick(PEEK_OPEN_MS - 1);
    expect(botsCardState().up).toBe(false);
    clock.tick(1);
    expect(botsCardState()).toMatchObject({ up: true, pinned: false, seq: 1 });
    i.leaveRing();
    clock.tick(PEEK_CLOSE_MS);
    expect(botsCardState().up).toBe(false);
  });
  it("a press pins; the close puts it down and unpins; the next opening bumps seq", () => {
    const i = botsCardIntent();
    i.press(BOTS_CARD_KEY);
    expect(botsCardState()).toMatchObject({ up: true, pinned: true, seq: 1 });
    i.leaveRing();
    clock.tick(10_000);
    expect(botsCardState().up).toBe(true);
    closeBotsCard();
    expect(botsCardState()).toMatchObject({ up: false, pinned: false, seq: 1 });
    i.press(BOTS_CARD_KEY);
    expect(botsCardState().seq).toBe(2);
  });
  it("is its own store: the Tasks card's press leaves it alone, and the two ids and keys differ", () => {
    tasksCardIntent().press(TASKS_CARD_KEY);
    expect(tasksCardState().up).toBe(true);
    expect(botsCardState().up).toBe(false);
    expect(BOTS_CARD_KEY).not.toBe(TASKS_CARD_KEY);
    expect(BOTS_PEEK_ID).not.toBe(TASKS_PEEK_ID);
  });
});

describe("ready, doors and the keyboard", () => {
  it("ready only with a card mounted AND room beside the rail", () => {
    expect(botsCardState().ready).toBe(false);
    const off = registerBotsCard();
    expect(botsCardState().ready).toBe(true);
    setBotsCardRoom(false);
    expect(botsCardState().ready).toBe(false);
    setBotsCardRoom(true);
    expect(botsCardState().ready).toBe(true);
    off();
    off();
    expect(botsCardState().ready).toBe(false);
  });
  it("a rail's door is registered by rail and taken off by its own answer only", () => {
    const rail = {} as HTMLElement;
    const a = {} as HTMLElement;
    const b = {} as HTMLElement;
    const offA = registerBotsDoor(rail, a);
    expect(railBotsDoor(rail)).toBe(a);
    registerBotsDoor(rail, b);
    offA();
    expect(railBotsDoor(rail)).toBe(b);
    expect(railBotsDoor(null)).toBeNull();
  });
  it("the card says how the keyboard lands in it", () => {
    expect(focusBotsCard()).toBe(false);
    let landed = 0;
    const off = registerBotsCardFocus(() => {
      landed += 1;
      return true;
    });
    expect(focusBotsCard()).toBe(true);
    expect(landed).toBe(1);
    off();
    expect(focusBotsCard()).toBe(false);
  });
});
