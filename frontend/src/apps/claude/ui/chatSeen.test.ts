import { beforeEach, describe, expect, test } from "bun:test";

import type { Turn } from "../protocol/controller-api";
import { _setUiLayoutForTest, getUiLayout, sanitizeUiLayout } from "@platform/lib/ui-layout";
import {
  firstNewIndex,
  getChatSeen,
  seenMarkOf,
  setChatSeen,
  newsBefore,
  newsIndex,
  newRuleIndex,
} from "./chatSeen";

const u = (uuid?: string): Turn => ({ role: "user", key: "u" + (uuid ?? Math.random()), text: "hi", ...(uuid ? { uuid } : {}) }) as Turn;
const a = (uuid?: string): Turn => ({ role: "assistant", key: "a" + (uuid ?? Math.random()), text: "ok", ...(uuid ? { uuid } : {}) }) as Turn;
const note = (): Turn => ({ role: "note", key: "n" + Math.random(), text: "Stopped." }) as unknown as Turn;

const live = (): Turn => ({ role: "assistant", key: "live", text: "…", streaming: true }) as Turn;
const pending = (): Turn => ({ role: "user", key: "p", text: "later", pending: true }) as unknown as Turn;
const interrupt = (): Turn => ({ role: "user", key: "i", text: "[Request interrupted by user]" }) as Turn;

describe("the seen mark (k:d:r)", () => {
  test("counts your sent messages and the replies after the last one", () => {
    expect(seenMarkOf([u(), a(), u(), a(), a()], true)).toBe("2:1:2");
    expect(seenMarkOf([u(), a(), u(), live()], false)).toBe("2:0:1");
    expect(seenMarkOf([u(), a(), interrupt(), pending()], true)).toBe("1:1:1");
    expect(seenMarkOf([], true)).toBe("0:1:0");
  });
});

describe("the NEW rule's place", () => {
  test("the reply to a later message of yours", () => {
    const turns = [u(), a(), u(), note(), a(), a()];
    expect(firstNewIndex(turns, "1:1:1")).toBe(4);
  });
  test("left mid-reply: that same reply is the news", () => {
    expect(firstNewIndex([u(), a(), u(), a()], "2:0:0")).toBe(3);
    expect(firstNewIndex([u(), a(), u(), a(), a()], "2:0:2")).toBe(4);
  });
  test("settled, nothing of yours since: a reply that came on its own", () => {
    expect(firstNewIndex([u(), a(), a()], "1:1:1")).toBe(2);
    expect(firstNewIndex([u(), a()], "1:1:1")).toBe(-1);
  });
  test("never seen, a mark this log cannot hold, or nothing past it: no rule", () => {
    const turns = [u(), a()];
    expect(firstNewIndex(turns, null)).toBe(-1);
    expect(firstNewIndex(turns, "junk")).toBe(-1);
    expect(firstNewIndex(turns, "5:1:0")).toBe(-1);
    expect(firstNewIndex([u(), a(), u()], "1:1:1")).toBe(-1);
  });
  test("the same mark reads the same on a live log and its restored copy", () => {
    const liveLog = [u(), a(), u(), a()];
    const restored = [u("x1"), a("y1"), interrupt(), u("x2"), a("y2")];
    expect(firstNewIndex(liveLog, "1:1:1")).toBe(3);
    expect(firstNewIndex(restored, "1:1:1")).toBe(4);
  });
});

describe("the NEW rule sits above your message", () => {
  test("the message the first new reply answers, notes stepped over", () => {
    const turns = [u(), a(), u(), note(), a(), a()];
    expect(newRuleIndex(turns, 4)).toBe(2);
  });
  test("a reply that came on its own after a seen reply: on the reply", () => {
    expect(newRuleIndex([u(), a(), a()], 2)).toBe(2);
  });
  test("a not-sent line is not yours; no rule stays no rule", () => {
    expect(newRuleIndex([u(), a(), pending(), a()], 3)).toBe(3);
    expect(newRuleIndex([a()], 0)).toBe(0);
    expect(newRuleIndex([u(), a()], -1)).toBe(-1);
  });
  test("the n-th reply's index", () => {
    const turns = [u(), a(), u(), note(), a()];
    expect(newsIndex(turns, 2)).toBe(4);
    expect(newsIndex(turns, 3)).toBe(-1);
  });
});

describe("the pill counts replies, not turn indices", () => {
  test("replies before an index, notes and your lines skipped", () => {
    const turns = [u(), a(), u(), a(), note(), a()];
    expect(newsBefore(turns)).toBe(3);
    expect(newsBefore(turns, 3)).toBe(1);
  });
  test("an error row is not a reply (the live log retires it)", () => {
    const err = { role: "error", key: "e", text: "boom", kind: "api" } as unknown as Turn;
    expect(newsBefore([u(), err, a()])).toBe(1);
  });
});

describe("the seen mark, in the layout document", () => {
  beforeEach(() => _setUiLayoutForTest({ v: 1 }));
  test("set, read back, moved to the end on re-set", () => {
    setChatSeen("s1", "x1");
    setChatSeen("s2", "y1");
    setChatSeen("s1", "x2");
    expect(getChatSeen("s1")).toBe("x2");
    expect(Object.keys(getUiLayout().chatSeen ?? {})).toEqual(["s2", "s1"]);
  });
  test("sanitize keeps well-formed entries, newest 300", () => {
    const many = Object.fromEntries(Array.from({ length: 305 }, (_, i) => [`s${i}`, `u${i}`]));
    const out = sanitizeUiLayout({ v: 1, chatSeen: { ...many, "": "x", bad: 3 } });
    const keys = Object.keys(out.chatSeen ?? {});
    expect(keys.length).toBe(300);
    expect(keys[0]).toBe("s5");
    expect(keys.includes("bad")).toBe(false);
  });
});
