// The Tasks card off the rail's Tasks door (owner, 2026-11-07): hover
// previews, a press pins, a press on the pinned door closes, a modified press
// is the link's own.
import { beforeEach, describe, expect, it } from "bun:test";
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, type Timers } from "./rail-peek-lib";
import {
  TASKS_CARD_KEY,
  closeTasksCard,
  focusTasksCard,
  railTasksDoor,
  registerTasksCard,
  registerTasksCardFocus,
  registerTasksDoor,
  resetTasksCardForTests,
  setTasksCardRoom,
  tasksCardIntent,
  tasksCardState,
  tasksDoorAction,
} from "./tasks-peek-store";

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
  resetTasksCardForTests(clock);
});

describe("tasksDoorAction", () => {
  it("a modified press is the link's own, card or none", () => {
    expect(tasksDoorAction({ modified: true, ready: true, pinned: false })).toBe("native");
    expect(tasksDoorAction({ modified: true, ready: false, pinned: true })).toBe("native");
  });
  it("no card to show: the door is the plain link to /tasks", () => {
    expect(tasksDoorAction({ modified: false, ready: false, pinned: false })).toBe("navigate");
  });
  it("a toggle: closed or previewing pins, pinned closes", () => {
    expect(tasksDoorAction({ modified: false, ready: true, pinned: false })).toBe("pin");
    expect(tasksDoorAction({ modified: false, ready: true, pinned: true })).toBe("close");
  });
});

describe("the Tasks card's intent", () => {
  it("a hover previews after window A's dwell and goes on the grace", () => {
    const i = tasksCardIntent();
    i.enterRing(TASKS_CARD_KEY);
    clock.tick(PEEK_OPEN_MS - 1);
    expect(tasksCardState().up).toBe(false);
    clock.tick(1);
    expect(tasksCardState()).toMatchObject({ up: true, pinned: false, seq: 1 });
    i.leaveRing();
    clock.tick(PEEK_CLOSE_MS);
    expect(tasksCardState().up).toBe(false);
  });
  it("a press pins; the pointer leaving keeps it; the close puts it down and unpins", () => {
    const i = tasksCardIntent();
    i.press(TASKS_CARD_KEY);
    expect(tasksCardState()).toMatchObject({ up: true, pinned: true, seq: 1 });
    i.leaveRing();
    clock.tick(PEEK_CLOSE_MS * 4);
    expect(tasksCardState().up).toBe(true);
    closeTasksCard();
    expect(tasksCardState()).toMatchObject({ up: false, pinned: false, seq: 1 });
  });
  it("a press inside a preview pins it, and the seq counts openings, not pins", () => {
    const i = tasksCardIntent();
    i.enterRing(TASKS_CARD_KEY);
    clock.tick(PEEK_OPEN_MS);
    i.pressStart();
    expect(tasksCardState()).toMatchObject({ up: true, pinned: true, seq: 1 });
    i.pressEnd();
    closeTasksCard();
    i.press(TASKS_CARD_KEY);
    expect(tasksCardState().seq).toBe(2);
  });
});

describe("ready", () => {
  it("only with a card mounted and room beside the rail", () => {
    expect(tasksCardState().ready).toBe(false);
    const off = registerTasksCard();
    expect(tasksCardState().ready).toBe(true);
    setTasksCardRoom(false);
    expect(tasksCardState().ready).toBe(false);
    setTasksCardRoom(true);
    off();
    off();
    expect(tasksCardState().ready).toBe(false);
  });
});

describe("the door registry", () => {
  it("one door per rail; a stale answer leaves a newer door alone", () => {
    const rail = {} as HTMLElement;
    const a = {} as HTMLElement;
    const b = {} as HTMLElement;
    const offA = registerTasksDoor(rail, a);
    expect(railTasksDoor(rail)).toBe(a);
    registerTasksDoor(rail, b);
    offA();
    expect(railTasksDoor(rail)).toBe(b);
    expect(railTasksDoor(null)).toBeNull();
  });
});

// The door's Enter puts the keyboard in the card (CodeRabbit, 2026-11-08):
// the card says how; with none mounted the ask is a no.
describe("focusTasksCard", () => {
  beforeEach(() => resetTasksCardForTests());
  it("asks the registered card, and is false with none", () => {
    expect(focusTasksCard()).toBe(false);
    let asked = 0;
    const off = registerTasksCardFocus(() => (asked += 1) > 0);
    expect(focusTasksCard()).toBe(true);
    expect(asked).toBe(1);
    off();
    expect(focusTasksCard()).toBe(false);
  });
});
