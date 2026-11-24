// The +'s New task card (owner, 2026-11-07: "hover on plus icon should show
// the card as well, and click does cement it unless you click on the cross
// icon"): hover previews, a press pins, the grace closes a preview only.
import { describe, expect, it } from "bun:test";
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, type Timers } from "./rail-peek-lib";
import { PLUS_DOOR, PlusPopIntent } from "./plus-pop-lib";

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
      seed: (door) => log.push(door === PLUS_DOOR ? "seed" : `seed ${door}`),
      cancelSeed: () => log.push("cancel"),
      open: (door, pinned) => log.push((pinned ? "open pinned" : "open preview") + (door === PLUS_DOOR ? "" : ` ${door}`)),
      pin: () => log.push("pin"),
      close: () => log.push("close"),
    },
    clock,
  );
  return { clock, log, intent };
}

describe("PlusPopIntent", () => {
  it("cancelPreview drops a hover's pending read, never a press's", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    intent.cancelPreview();
    intent.seeded(PLUS_DOOR);
    clock.tick(PEEK_OPEN_MS + 1);
    expect(log).toEqual(["seed", "cancel"]);
    expect(intent.state).toBe("closed");
    intent.press();
    intent.cancelPreview();
    intent.seeded(PLUS_DOOR);
    expect(log[log.length - 1]).toBe("open pinned");
  });
  it("a hover reads at once and shows the preview after the delay, once the read is in", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    expect(log).toEqual(["seed"]);
    intent.seeded(PLUS_DOOR);
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
    intent.seeded(PLUS_DOOR);
    expect(log[log.length - 1]).toBe("open preview");
  });

  it("leaving the + before it opens opens nothing and drops the read", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    clock.tick(PEEK_OPEN_MS - 10);
    intent.leavePlus();
    intent.seeded(PLUS_DOOR);
    clock.tick(PEEK_OPEN_MS);
    expect(log).toEqual(["seed", "cancel"]);
    expect(intent.state).toBe("closed");
  });

  it("leaving both the + and the card closes a preview after the grace; crossing into the card does not", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    intent.seeded(PLUS_DOOR);
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
    intent.seeded(PLUS_DOOR);
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
    intent.seeded(PLUS_DOOR);
    expect(log).toEqual(["seed", "open pinned"]);
    expect(intent.state).toBe("pinned");
  });

  it("a press during the hover's delay skips the wait and opens pinned", () => {
    const { log, intent } = plus();
    intent.enterPlus();
    intent.seeded(PLUS_DOOR);
    intent.press();
    expect(log).toEqual(["seed", "open pinned"]);
  });

  it("acting inside a preview pins it; the grace then closes nothing", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    intent.seeded(PLUS_DOOR);
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
    intent.seeded(PLUS_DOOR);
    intent.closed();
    intent.enterPlus();
    intent.seeded(PLUS_DOOR);
    clock.tick(PEEK_OPEN_MS);
    expect(log).toEqual(["seed", "open pinned", "seed", "open preview"]);
  });

  it("a draft ring is a door like the +: hover previews its card once its record is read, a click pins it", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus("d1");
    intent.seeded("d1");
    clock.tick(PEEK_OPEN_MS);
    expect(log).toEqual(["seed d1", "open preview d1"]);
    intent.press("d1");
    expect(intent.state).toBe("pinned");
    expect(intent.current).toBe("d1");
    intent.leavePlus();
    clock.tick(PEEK_CLOSE_MS * 4);
    expect(log).toEqual(["seed d1", "open preview d1", "pin"]);
  });

  it("a hover on another door swaps the preview once that card is ready; leaving it first keeps the one up on its grace", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus();
    intent.seeded(PLUS_DOOR);
    clock.tick(PEEK_OPEN_MS);
    intent.leavePlus();
    intent.enterPlus("d1");
    clock.tick(PEEK_CLOSE_MS * 2);
    expect(log).toEqual(["seed", "open preview", "seed d1"]);
    intent.seeded("d1");
    expect(log[log.length - 1]).toBe("open preview d1");
    expect(intent.current).toBe("d1");
    intent.enterPlus(PLUS_DOOR);
    intent.leavePlus();
    expect(log[log.length - 1]).toBe("cancel");
    clock.tick(PEEK_CLOSE_MS);
    expect(log[log.length - 1]).toBe("close");
  });

  it("a draft whose record is gone shows nothing on hover; a press waiting on it is told so (its fallback door)", () => {
    const { clock, log, intent } = plus();
    intent.enterPlus("d1");
    expect(intent.failed("d1")).toBe(false);
    clock.tick(PEEK_OPEN_MS * 2);
    expect(intent.state).toBe("closed");
    intent.press("d2");
    expect(intent.failed("d2")).toBe(true);
    expect(log).toEqual(["seed d1", "seed d2"]);
  });

  it("a pinned card: a hover on another door shows nothing; that door's press replaces it, pinned", () => {
    const { clock, log, intent } = plus();
    intent.press();
    intent.seeded(PLUS_DOOR);
    intent.enterPlus("d1");
    clock.tick(PEEK_OPEN_MS * 2);
    expect(log).toEqual(["seed", "open pinned"]);
    intent.press("d1");
    intent.seeded("d1");
    expect(log).toEqual(["seed", "open pinned", "seed d1", "open pinned d1"]);
    expect(intent.current).toBe("d1");
  });
});
