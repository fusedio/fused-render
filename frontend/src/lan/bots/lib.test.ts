import { describe, expect, test } from "bun:test";
import {
  answerAfter,
  approvalVerdict,
  escapeHtml,
  groupRows,
  listUnread,
  markdownLite,
  mergeEvents,
  multiAnswer,
  parseRoute,
  previewType,
  routePath,
  scaledSize,
  seenKey,
  statusOf,
  timeAgo,
  unreadCount,
} from "./lib";
import type { BotEvent } from "./types";

const ev = (seq: number, role: BotEvent["role"] = "thought", text = "t" + seq, extra: Partial<BotEvent> = {}): BotEvent => ({
  seq,
  ts: 1_700_000_000 + seq,
  role,
  text,
  ...extra,
});

describe("routes", () => {
  test("grid, list and chat", () => {
    expect(parseRoute("/")).toEqual({ name: "grid" });
    expect(parseRoute("")).toEqual({ name: "grid" });
    expect(parseRoute("/index.html")).toEqual({ name: "grid" });
    expect(parseRoute("/bots")).toEqual({ name: "bots" });
    expect(parseRoute("/bots/")).toEqual({ name: "bots" });
    expect(parseRoute("/bots/b_12-x")).toEqual({ name: "bot", id: "b_12-x" });
    expect(parseRoute("/bots/b1/")).toEqual({ name: "bot", id: "b1" });
  });
  test("ids with a slash or odd characters fall back to the grid", () => {
    expect(parseRoute("/bots/a/b")).toEqual({ name: "grid" });
    expect(parseRoute("/bots/a%2Fb")).toEqual({ name: "grid" });
    expect(parseRoute("/render")).toEqual({ name: "grid" });
  });
  test("round trip", () => {
    for (const p of ["/", "/bots", "/bots/abc"]) expect(routePath(parseRoute(p))).toBe(p);
  });
});

describe("status words", () => {
  const b = (status: "idle" | "running" | "waiting" | "paused" | "error", step = 0) => ({ status, step, waitingOn: null, control: false });
  test("each status", () => {
    expect(statusOf(b("idle"))).toEqual({ word: "Idle", tone: "idle" });
    expect(statusOf(b("running", 12))).toEqual({ word: "Working · step 12", tone: "busy" });
    expect(statusOf(b("running", 0)).word).toBe("Working");
    expect(statusOf(b("waiting", 3))).toEqual({ word: "Needs you", tone: "needs" });
    expect(statusOf(b("paused")).word).toBe("Paused");
    expect(statusOf(b("error")).tone).toBe("error");
  });
});

describe("unread", () => {
  test("key is per host and bot", () => {
    expect(seenKey("10.0.0.2:8765", "b1")).not.toBe(seenKey("10.0.0.3:8765", "b1"));
    expect(seenKey("h", "b1")).toContain("b1");
  });
  test("list badge: a readable last line after the seen seq", () => {
    const last = (seq: number, role: BotEvent["role"]) => ({ seq, role, text: "x", ts: 0 });
    expect(listUnread({ status: "idle", last: last(10, "done") }, 9)).toBe(true);
    expect(listUnread({ status: "idle", last: last(10, "done") }, 10)).toBe(false);
    expect(listUnread({ status: "idle", last: last(10, "user") }, 3)).toBe(false);
    expect(listUnread({ status: "idle", last: last(10, "done") }, null)).toBe(false);
    expect(listUnread({ status: "idle", last: null }, 0)).toBe(false);
    expect(listUnread({ status: "waiting", last: last(10, "approval") }, 4)).toBe(true);
  });
  test("thread count skips steps and user lines", () => {
    const evs = [ev(1, "user"), ev(2, "thought"), ev(3, "action"), ev(4, "done"), ev(5, "question"), ev(6, "error")];
    expect(unreadCount(evs, 1)).toBe(3);
    expect(unreadCount(evs, 4)).toBe(2);
    expect(unreadCount(evs, 6)).toBe(0);
    expect(unreadCount(evs, null)).toBe(0);
  });
});

describe("thread", () => {
  test("mergeEvents appends, dedupes and sorts", () => {
    const a = [ev(1), ev(2)];
    expect(mergeEvents(a, [])).toBe(a);
    expect(mergeEvents(a, [ev(2)])).toBe(a);
    expect(mergeEvents(a, [ev(4), ev(3)]).map((e) => e.seq)).toEqual([1, 2, 3, 4]);
    expect(mergeEvents([ev(5)], [ev(1), ev(2)]).map((e) => e.seq)).toEqual([1, 2, 5]);
  });
  test("groupRows folds consecutive steps under the first seq", () => {
    const evs = [ev(1, "user"), ev(2, "thought"), ev(3, "action"), ev(4, "thought"), ev(5, "done"), ev(6, "action")];
    const g = groupRows(evs);
    expect(g.map((x) => x.key)).toEqual(["e1", "s2", "e5", "s6"]);
    const steps = g[1];
    expect(steps.kind === "steps" && steps.events.map((e) => e.seq)).toEqual([2, 3, 4]);
  });
  test("a growing run keeps its key", () => {
    const k1 = groupRows([ev(1, "user"), ev(2)]).map((x) => x.key);
    const k2 = groupRows([ev(1, "user"), ev(2), ev(3, "action")]).map((x) => x.key);
    expect(k2).toEqual(k1);
  });
  test("delivery rows join their event", () => {
    const g = groupRows([ev(1, "done"), ev(2, "delivery", "sent", { ref: 1, channel: "imessage" })]);
    expect(g.length).toBe(1);
    expect(g[0].kind === "event" && g[0].deliveries.map((d) => d.seq)).toEqual([2]);
  });
  test("answerAfter and approval verdicts", () => {
    const evs = [ev(1, "approval"), ev(2, "action"), ev(3, "user", "approve"), ev(4, "approval"), ev(5, "user", "deny")];
    expect(answerAfter(evs, 1)?.seq).toBe(3);
    expect(answerAfter(evs, 5)).toBeNull();
    // An answer that lost the race to the Mac is written, then marked ignored: the next line settled it.
    const raced = [ev(1, "approval"), ev(2, "user", "approve"), ev(3, "system", "not applied", { ignored_seq: 2 }), ev(4, "user", "deny")];
    expect(answerAfter(raced, 1)?.seq).toBe(4);
    expect(approvalVerdict("approve")).toBe("Approved");
    expect(approvalVerdict("Yes please")).toBe("Approved");
    expect(approvalVerdict("deny")).toBe("Denied");
    expect(approvalVerdict("go to the page")).toBeNull();
    expect(approvalVerdict(null)).toBeNull();
  });
  test("multi answer: comma list in option order, None when empty", () => {
    expect(multiAnswer(["A", "B", "C"], new Set(["C", "A"]))).toBe("A, C");
    expect(multiAnswer(["A", "B"], new Set())).toBe("None");
  });
});

describe("time", () => {
  test("timeAgo buckets", () => {
    expect(timeAgo(1000, 1030)).toBe("now");
    expect(timeAgo(1000, 1000 + 5 * 60)).toBe("5m");
    expect(timeAgo(1000, 1000 + 3 * 3600)).toBe("3h");
    expect(timeAgo(1000, 1000 + 2 * 86400)).toBe("2d");
    expect(timeAgo(0, 5)).toBe("");
  });
});

describe("image scaling", () => {
  test("long edge capped at 1568, aspect kept, never upscaled", () => {
    expect(scaledSize(4032, 3024)).toEqual({ w: 1568, h: 1176 });
    expect(scaledSize(3024, 4032)).toEqual({ w: 1176, h: 1568 });
    expect(scaledSize(800, 600)).toEqual({ w: 800, h: 600 });
    expect(scaledSize(1568, 10)).toEqual({ w: 1568, h: 10 });
    expect(scaledSize(100000, 1)).toEqual({ w: 1568, h: 1 });
    expect(scaledSize(0, 0)).toEqual({ w: 1, h: 1 });
  });
  test("png stays png, the rest is jpeg", () => {
    expect(previewType("image/png")).toBe("image/png");
    expect(previewType("image/heic")).toBe("image/jpeg");
    expect(previewType("image/webp")).toBe("image/jpeg");
  });
});

describe("markdown-lite", () => {
  test("escapes HTML", () => {
    expect(escapeHtml(`<b a="1">&'`)).toBe("&lt;b a=&quot;1&quot;&gt;&amp;&#39;");
    expect(markdownLite("<script>alert(1)</script>")).toBe("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>");
  });
  test("paragraphs and line breaks", () => {
    expect(markdownLite("a\nb\n\nc")).toBe("<p>a<br>b</p><p>c</p>");
    expect(markdownLite("  ")).toBe("");
  });
  test("bold and inline code (nothing formatted inside code)", () => {
    expect(markdownLite("a **b** c")).toBe("<p>a <strong>b</strong> c</p>");
    expect(markdownLite("run `x **y** <z>`")).toBe("<p>run <code>x **y** &lt;z&gt;</code></p>");
  });
  test("links open in a new tab; trailing punctuation stays outside", () => {
    const a = (u: string) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`;
    expect(markdownLite("see https://x.com/a?b=1&c=2.")).toBe(`<p>see ${a("https://x.com/a?b=1&amp;c=2")}.</p>`);
    expect(markdownLite("(https://x.com)")).toBe(`<p>(${a("https://x.com")})</p>`);
    expect(markdownLite("[docs](https://d.io/x)")).toBe(
      `<p><a href="https://d.io/x" target="_blank" rel="noopener noreferrer">docs</a></p>`,
    );
    expect(markdownLite("javascript:alert(1)")).toBe("<p>javascript:alert(1)</p>");
    expect(markdownLite("[x](javascript:alert(1))")).toBe("<p>[x](javascript:alert(1))</p>");
  });
  test("list markers and headings become plain lines", () => {
    expect(markdownLite("# Title\n- one\n- two")).toBe("<p><strong>Title</strong><br>• one<br>• two</p>");
  });
});

describe("routines", () => {
  test("schedule words", async () => {
    const { routineWhen } = await import("./lib");
    expect(routineWhen({ kind: "interval", minutes: 30 })).toBe("Every 30 min");
    expect(routineWhen({ kind: "interval", minutes: 60 })).toBe("Every hour");
    expect(routineWhen({ kind: "interval", minutes: 120 })).toBe("Every 2 h");
    expect(routineWhen({ kind: "interval", minutes: 2880 })).toBe("Every 2 days");
    expect(routineWhen({ kind: "daily", time: "09:00", weekdays: [0, 1, 2, 3, 4, 5, 6] })).toBe("Daily at 09:00");
    expect(routineWhen({ kind: "daily", time: "08:30", weekdays: [4, 0, 1, 2, 3] })).toBe("Weekdays at 08:30");
    expect(routineWhen({ kind: "daily", time: "10:00", weekdays: [5, 6] })).toBe("Weekends at 10:00");
    expect(routineWhen({ kind: "daily", time: "18:00", weekdays: [0, 3] })).toBe("Mon, Thu at 18:00");
    expect(routineWhen({ kind: "once" })).toBe("Once");
  });
  test("datetime-local to epoch", async () => {
    const { localInputToEpoch } = await import("./lib");
    expect(Number.isNaN(localInputToEpoch(""))).toBe(true);
    expect(localInputToEpoch("2030-01-02T03:04")).toBe(Math.floor(new Date("2030-01-02T03:04").getTime() / 1000));
  });
});
