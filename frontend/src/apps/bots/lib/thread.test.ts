import { describe, expect, test } from "bun:test";
import type { Bot, BotEvent } from "./api";
import {
  SESSION_GAP_S, chosenOption, firstNewIndex, isNoise, liveCards, optionKey, rowKeys, searchCountText, searchHit, searchQuery, sessionBreak,
} from "./thread";

const ev = (seq: number, role: BotEvent["role"] = "thought", text = "t" + seq, ts = seq): BotEvent => ({ seq, ts, role, text });
const bot = (status: Bot["status"], pending_offer: Bot["pending_offer"] = null) => ({ status, pending_offer });

describe("thread rules", () => {
  test("isNoise: only system notes", () => {
    expect(isNoise(ev(1, "system"))).toBe(true);
    expect(isNoise(ev(1, "user"))).toBe(false);
    expect(isNoise(ev(1, "action"))).toBe(false);
  });
  test("sessionBreak: first event and gaps over 15 min", () => {
    expect(sessionBreak(undefined, ev(1))).toBe(true);
    expect(sessionBreak(ev(1, "thought", "a", 100), ev(2, "thought", "b", 100 + SESSION_GAP_S))).toBe(false);
    expect(sessionBreak(ev(1, "thought", "a", 100), ev(2, "thought", "b", 101 + SESSION_GAP_S))).toBe(true);
  });
  test("firstNewIndex skips system notes and needs a mark", () => {
    const evs = [ev(1), ev(2), ev(3, "system"), ev(4)];
    expect(firstNewIndex(evs, null)).toBe(-1);
    expect(firstNewIndex(evs, 2)).toBe(3);
    expect(firstNewIndex(evs, 0)).toBe(0);
    expect(firstNewIndex(evs, 4)).toBe(-1);
  });
  test("liveCards: only the last ask while waiting", () => {
    const evs = [ev(1, "approval"), ev(2, "user", "approve"), ev(3, "question"), ev(4, "action")];
    expect([...liveCards(evs, bot("waiting"))]).toEqual([3]);
    expect([...liveCards(evs, bot("running"))]).toEqual([]);
    expect([...liveCards([...evs, ev(5, "user", "ok")], bot("waiting"))]).toEqual([]);
  });
  test("liveCards: waiting_on names the live card, even after a mid-task message", () => {
    const evs = [ev(1, "approval"), ev(2, "user", "approve"), ev(3, "approval"), ev(4, "user", "also check prices"), ev(5, "note")];
    // Without waiting_on the instruction at 4 looks like the answer: nothing live.
    expect([...liveCards(evs, bot("waiting"))]).toEqual([]);
    expect([...liveCards(evs, { ...bot("waiting"), waiting_on: null })]).toEqual([]);
    expect([...liveCards(evs, { ...bot("waiting"), waiting_on: 3 })]).toEqual([3]);
    // Exactly that seq, not the last ask.
    const two = [ev(1, "question"), ev(2, "approval")];
    expect([...liveCards(two, { ...bot("waiting"), waiting_on: 1 })]).toEqual([1]);
    // Not a card, or trimmed out of history: nothing live.
    expect([...liveCards(evs, { ...bot("waiting"), waiting_on: 5 })]).toEqual([]);
    expect([...liveCards(evs, { ...bot("waiting"), waiting_on: 99 })]).toEqual([]);
    // Still live while send() has flipped the status to running (the engine clears waiting_on itself), and while paused
    // mid-question; a leftover seq on an idle or errored bot is stale.
    expect([...liveCards(evs, { ...bot("running"), waiting_on: 3 })]).toEqual([3]);
    expect([...liveCards(evs, { ...bot("paused"), waiting_on: 3 })]).toEqual([3]);
    expect([...liveCards(evs, { ...bot("idle"), waiting_on: 3 })]).toEqual([]);
    expect([...liveCards(evs, { ...bot("error"), waiting_on: 3 })]).toEqual([]);
  });
  test("isNoise: notes show in the thread", () => {
    expect(isNoise(ev(1, "note"))).toBe(false);
    expect(firstNewIndex([ev(1), ev(2, "note")], 1)).toBe(1);
  });
  test("liveCards: a pending offer stays live while idle, until answered", () => {
    const evs = [ev(1, "question"), ev(2, "done")];
    expect([...liveCards(evs, bot("idle", { seq: 1 }))]).toEqual([1]);
    expect([...liveCards(evs, bot("idle", { seq: 9 }))]).toEqual([]);
    expect([...liveCards([...evs, ev(3, "user", "Not now")], bot("idle", { seq: 1 }))]).toEqual([]);
  });
  test("chosenOption: the next user message, normalized", () => {
    const evs = [ev(1, "question"), ev(2, "action"), ev(3, "user", "  Use It "), ev(4, "user", "later")];
    expect(chosenOption(evs, 1)).toBe("use it");
    expect(chosenOption(evs, 4)).toBeNull();
    expect(optionKey("Use it")).toBe(chosenOption(evs, 1) ?? "");
  });
});

describe("search filter", () => {
  test("query is trimmed and lower-cased; match is case-insensitive substring", () => {
    const q = searchQuery("  LinkedIn ");
    expect(q).toBe("linkedin");
    expect(searchHit("Opened LINKEDIN feed", q)).toBe(true);
    expect(searchHit("nothing here", q)).toBe(false);
    expect(searchHit(null, q)).toBe(false);
  });
  test("count text", () => {
    expect(searchCountText(0)).toBe("No matches");
    expect(searchCountText(1)).toBe("1 match");
    expect(searchCountText(4)).toBe("4 matches");
  });
});

describe("rowKeys", () => {
  test("bot + seq, and a repeated seq (two writers on one log) still gets a distinct key", () => {
    const evs = [ev(1), ev(2), ev(2, "system"), ev(2)];
    const keys = rowKeys(evs, "b1");
    expect(keys[0]).toBe("b1:1");
    expect(keys[1]).toBe("b1:2");
    expect(new Set(keys).size).toBe(4);
  });
});
