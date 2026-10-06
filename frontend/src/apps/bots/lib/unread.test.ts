import { describe, expect, test } from "bun:test";
import type { BotEvent } from "./api";
import { newMarkFor, unreadOf, unviewedOf, waitingUnread } from "./unread";
import type { Bot } from "./api";

const ev = (seq: number, role: BotEvent["role"] = "thought"): BotEvent => ({ seq, ts: seq, role, text: "t" + seq });

describe("unread", () => {
  test("never opened: nothing unread", () => {
    expect(unreadOf(undefined, [ev(1), ev(2)], { seq: 2 })).toBe(0);
  });
  test("counts events past seen, system notes excluded", () => {
    expect(unreadOf(1, [ev(1), ev(2), ev(3, "system"), ev(4, "done")], { seq: 4 })).toBe(2);
    expect(unreadOf(4, [ev(1), ev(4)], { seq: 4 })).toBe(0);
  });
  test("trimmed history falls back to the seq gap", () => {
    expect(unreadOf(5, [ev(1), ev(2)], { seq: 9 })).toBe(4);
    expect(unreadOf(9, [], { seq: 5 })).toBe(0);
  });
  test("unviewed: past base, not user/system, not yet seen", () => {
    const evs = [ev(1), ev(2, "user"), ev(3), ev(4, "system"), ev(5, "done")];
    expect(unviewedOf(undefined, undefined, evs)).toEqual([]);
    expect(unviewedOf(1, undefined, evs).map((e) => e.seq)).toEqual([3, 5]);
    expect(unviewedOf(1, new Set([3]), evs).map((e) => e.seq)).toEqual([5]);
  });
  test("newMarkFor: first open sets base to seq and no rule; later opens mark the old seen", () => {
    expect(newMarkFor(undefined, { id: "a", seq: 7 })).toEqual({ base: 7, newMark: null });
    expect(newMarkFor(3, { id: "a", seq: 7 })).toEqual({ base: 3, newMark: { id: "a", seq: 3 } });
    expect(newMarkFor(7, { id: "a", seq: 7 })).toEqual({ base: 7, newMark: null });
  });
  test("waitingUnread", () => {
    const b = { status: "waiting" } as Bot;
    expect(waitingUnread(b, undefined, 0)).toBe(true);
    expect(waitingUnread(b, 3, 0)).toBe(false);
    expect(waitingUnread(b, 3, 2)).toBe(true);
    expect(waitingUnread({ status: "idle" } as Bot, undefined, 5)).toBe(false);
  });
});
