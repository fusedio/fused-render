// What the sidebar's Bots entry says about the page behind it — the Tasks row's
// marks and words, read off bot rows instead of task rows (sidebar-tasks.test.ts
// is the sibling). The DERIVATION is pure and exercised directly; the WIRING is
// read out of the source, the way that suite holds claims a DOM-less test
// cannot otherwise hold.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  EMPTY_BOTS_PULSE,
  botHasUnread,
  botsPulseFeeders,
  feedBots,
  botsPulse,
  botsPulseTitle,
  botsRunningLabel,
  botsWaitingLabel,
  sameBotsPulse,
} from "@platform/lib/botsPulse";
import type { BotPulseRow } from "@platform/lib/botsPulse";

const SHELL = new URL(".", import.meta.url).pathname;
const SIDEBAR = readFileSync(join(SHELL, "GlobalSidebar.tsx"), "utf8");
const STORE = readFileSync(join(SHELL, "../platform/lib/botsPulse.ts"), "utf8");
const BOTS_STORE = readFileSync(join(SHELL, "../apps/bots/state/store.ts"), "utf8");
const BOTS_APP = readFileSync(join(SHELL, "../apps/bots/App.tsx"), "utf8");
const ROUTES = readFileSync(join(SHELL, "../../../fused_render/bots/routes.py"), "utf8");

function bot(over: Partial<BotPulseRow> = {}): BotPulseRow {
  return { id: "b1", status: "idle", seq: 0, ...over };
}

describe("botsPulse", () => {
  it("counts waiting, running and error bots apart", () => {
    const p = botsPulse([
      bot({ id: "a", status: "waiting" }),
      bot({ id: "b", status: "running" }),
      bot({ id: "c", status: "running" }),
      bot({ id: "d", status: "error" }),
      bot({ id: "e", status: "paused" }),
      bot({ id: "f", status: "idle" }),
    ]);
    expect(p).toEqual({ waiting: 1, running: 2, error: 1, unread: 0 });
  });

  it("counts BOTS with unread news, never a bot that was never opened", () => {
    // A seeded bot nobody has opened has `seen_seq` absent (or null, from the
    // page's own rows): nothing to catch up on, so a fresh install's chip is 0.
    expect(botHasUnread(bot({ seq: 9 }))).toBe(false);
    expect(botHasUnread(bot({ seq: 9, seen_seq: null }))).toBe(false);
    expect(botHasUnread(bot({ seq: 9, seen_seq: 9 }))).toBe(false);
    expect(botHasUnread(bot({ seq: 9, seen_seq: 4 }))).toBe(true);
    // Twelve new messages in one bot are one unread BOT — the chip counts rows.
    const p = botsPulse([bot({ id: "a", seq: 20, seen_seq: 8 }), bot({ id: "b", seq: 3, seen_seq: 2 })]);
    expect(p.unread).toBe(2);
  });

  it("skips hidden bots entirely", () => {
    const p = botsPulse([bot({ status: "waiting", seq: 5, seen_seq: 1, hidden: true })]);
    expect(p).toEqual(EMPTY_BOTS_PULSE);
  });

  it("a waiting bot with unread news is both waiting and unread", () => {
    const p = botsPulse([bot({ status: "waiting", seq: 5, seen_seq: 1 })]);
    expect(p).toEqual({ waiting: 1, running: 0, error: 0, unread: 1 });
  });

  it("sameBotsPulse is a field-wise compare", () => {
    expect(sameBotsPulse(EMPTY_BOTS_PULSE, { ...EMPTY_BOTS_PULSE })).toBe(true);
    expect(sameBotsPulse(EMPTY_BOTS_PULSE, { ...EMPTY_BOTS_PULSE, error: 1 })).toBe(false);
  });
});

describe("the words", () => {
  it("use the status's own word with no plural fork, like the Tasks row", () => {
    expect(botsWaitingLabel(1)).toBe("1 waiting");
    expect(botsWaitingLabel(3)).toBe("3 waiting");
    expect(botsRunningLabel(1)).toBe("1 running");
  });

  it("tooltip: waiting first, then running, error, unread; empty when quiet", () => {
    expect(botsPulseTitle({ waiting: 1, running: 2, error: 1, unread: 3 })).toBe(
      "1 waiting · 2 running · 1 error · 3 unread",
    );
    expect(botsPulseTitle({ waiting: 0, running: 1, error: 0, unread: 0 })).toBe("1 running");
    expect(botsPulseTitle(EMPTY_BOTS_PULSE)).toBe("");
  });
});

describe("wiring", () => {
  it("the Bots row wears the Tasks row's dot, words and chip — the same classes", () => {
    const row = SIDEBAR.slice(SIDEBAR.indexOf("const botsPulse = useBotsPulse()"), SIDEBAR.indexOf("const menuEntries"));
    // One dot, red over yellow over green — the Tasks precedence.
    expect(row.indexOf('"sidebar-rail-dot is-attention"')).toBeLessThan(row.indexOf('"sidebar-rail-dot is-running"'));
    expect(row.indexOf('"sidebar-rail-dot is-running"')).toBeLessThan(row.indexOf('"sidebar-rail-dot is-unread"'));
    // Waiting words replace running words; the chip is the shared count chip.
    expect(row).toContain('"sidebar-running is-attention"');
    expect(row).toContain("botsWaitingLabel(botsPulse.waiting)");
    expect(row).toContain("botsRunningLabel(botsPulse.running)");
    expect(row).toContain('"sidebar-count-chip"');
    // Only the green dot is suppressed on /bots itself, as only `unseen` is on /tasks.
    expect(row).toContain("const botsNews = botsActive ? 0 : botsPulse.unread;");
    expect(row).toContain("botsNews > 0 ?");
    expect(row).not.toContain("botsActive ? undefined");
  });

  it("both widths show it: the rail badge + tooltip, the expanded extra + trailing", () => {
    expect(SIDEBAR).toContain("label: botsTip ? `Bots — ${botsTip}` : \"Bots\"");
    expect(SIDEBAR).toContain("badge: botsDot,");
    expect(SIDEBAR).toMatch(/id="bots-link"[\s\S]*?extra=\{botsDot\}[\s\S]*?trailing=\{botsTrailing\}[\s\S]*?title=\{botsTip\}/);
  });

  it("one poll, two readers: the Bots page feeds the store and the store stands down", () => {
    expect(BOTS_APP).toContain("useBotsFeeder();");
    // The page publishes its MERGED read mark, not the server's raw one.
    expect(BOTS_STORE).toContain("seen_seq: S.seen[b.id]");
    // ...and republishes on every local mark, not only on a poll.
    const markSeen = BOTS_STORE.slice(BOTS_STORE.indexOf("export function markSeen"), BOTS_STORE.indexOf("function publishPulse"));
    expect(markSeen).toContain("publishPulse();");
    // A request that never settles is bounded, or the timer would never re-arm.
    expect(STORE).toContain("AbortSignal.timeout(REQUEST_MS)");
    // A feeder held and released leaves the store free to poll again (a
    // duplicated increment here froze the rail for the life of the document
    // once — reviewer, 2026-10-20).
    const release = feedBots();
    expect(botsPulseFeeders()).toBe(1);
    release();
    expect(botsPulseFeeders()).toBe(0);
    // The self-poll never runs beside a feeder, and never for no reader.
    expect(STORE).toContain("if (listeners.size === 0 || feeders > 0) return;");
    // A stale self-poll that lost to a fresher publish is dropped, not applied.
    expect(STORE).toContain("if (feeders === 0 && generation === departed) publishBots(");
  });

  it("the endpoint is the small one, with no side effects", () => {
    expect(STORE).toContain('"/api/bots/pulse"');
    expect(ROUTES).toContain('@router.get("/api/bots/pulse")');
    const fn = ROUTES.slice(ROUTES.indexOf("def bots_pulse"), ROUTES.indexOf('@router.post("/api/bots")'));
    expect(fn).not.toContain("_status_bot(");
    expect(fn).not.toContain("usage_summary");
  });
});
