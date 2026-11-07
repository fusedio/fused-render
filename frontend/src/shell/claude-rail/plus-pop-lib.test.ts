// The +'s New task card (owner, 2026-11-07: "hover on plus icon should show
// the card as well, and click does cement it unless you click on the cross
// icon"): hover previews, a press pins, the grace closes a preview only.
import { describe, expect, it } from "bun:test";
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, type Timers } from "./rail-peek-lib";
import { PlusPopIntent } from "./plus-pop-lib";

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

function plus() {
  const clock = new Clock();
  const log: string[] = [];
  const intent = new PlusPopIntent(
    {
      seed: () => log.push("seed"),
      cancelSeed: () => log.push("cancel"),
      open: (pinned) => log.push(pinned ? "open pinned" : "open preview"),
      pin: () => log.push("pin"),
      close: () => log.push("close"),
    },
    clock,
  );
  return { clock, log, intent };
}

describe("PlusPopIntent", () => {
  it("a hover reads at once and shows the preview after the delay, once the read is in", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    expect(log).toEqual(["seed"]);
    intent.seeded();
    clock.tick(PEEK_OPEN_MS - 1);
    expect(intent.state).toBe("pending");
    clock.tick(1);
    expect(log).toEqual(["seed", "open preview"]);
    expect(intent.state).toBe("preview");
  });

  it("a slow read: the preview waits for it", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    clock.tick(PEEK_OPEN_MS * 3);
    expect(intent.state).toBe("pending");
    intent.seeded();
    expect(log[log.length - 1]).toBe("open preview");
  });

  it("leaving the + before it opens opens nothing and drops the read", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    clock.tick(PEEK_OPEN_MS - 10);
    intent.leavePlus();
    intent.seeded();
    clock.tick(PEEK_OPEN_MS);
    expect(log).toEqual(["seed", "cancel"]);
    expect(intent.state).toBe("closed");
  });

  it("leaving both the + and the card closes a preview after the grace; crossing into the card does not", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    intent.seeded();
    clock.tick(PEEK_OPEN_MS);
    intent.leavePlus();
    clock.tick(PEEK_CLOSE_MS - 50);
    intent.enterCard();
    clock.tick(PEEK_CLOSE_MS * 4);
    expect(log).not.toContain("close");
    intent.leaveCard();
    clock.tick(PEEK_CLOSE_MS - 1);
    expect(log).not.toContain("close");
    clock.tick(1);
    expect(log[log.length - 1]).toBe("close");
    intent.closed();
    expect(intent.state).toBe("closed");
  });

  it("a press on the + pins the preview in place: no second read, no close on leave; a further press keeps it", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    intent.seeded();
    clock.tick(PEEK_OPEN_MS);
    intent.press();
    expect(intent.state).toBe("pinned");
    intent.leavePlus();
    clock.tick(PEEK_CLOSE_MS * 10);
    intent.press();
    expect(log).toEqual(["seed", "open preview", "pin"]);
  });

  it("a press with nothing up opens pinned as soon as the read is in", () => {
    const { log, intent } = plus();
    intent.press();
    expect(log).toEqual(["seed"]);
    intent.leavePlus();
    intent.seeded();
    expect(log).toEqual(["seed", "open pinned"]);
    expect(intent.state).toBe("pinned");
  });

  it("a press during the hover's delay skips the wait and opens pinned", () => {
    const { log, intent } = plus();
    intent.enterPlus();
    intent.seeded();
    intent.press();
    expect(log).toEqual(["seed", "open pinned"]);
  });

  it("acting inside a preview pins it; the grace then closes nothing", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    intent.seeded();
    clock.tick(PEEK_OPEN_MS);
    intent.leavePlus();
    intent.enterCard();
    intent.pinInside();
    intent.leaveCard();
    clock.tick(PEEK_CLOSE_MS * 10);
    expect(log).toEqual(["seed", "open preview", "pin"]);
  });

  it("after the card closes, the next hover starts over", () => {
    const { clock, log, intent } = plus();
    intent.press();
    intent.seeded();
    intent.closed();
    intent.enterPlus();
    intent.seeded();
    clock.tick(PEEK_OPEN_MS);
    expect(log).toEqual(["seed", "open pinned", "seed", "open preview"]);
  });

  it("a draft's card (another door) is adopted as pinned: a hover shows nothing, a press keeps it, a + read still out is dropped", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    intent.adopt();
    expect(intent.state).toBe("pinned");
    intent.seeded();
    clock.tick(PEEK_OPEN_MS * 2);
    intent.enterPlus();
    intent.press();
    expect(log).toEqual(["seed", "cancel"]);
    intent.closed();
    expect(intent.state).toBe("closed");
  });
});
