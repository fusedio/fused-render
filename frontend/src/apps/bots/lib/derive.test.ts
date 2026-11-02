import { describe, expect, test } from "bun:test";
import type { Bot, BotEvent } from "./api";
import { BOT_ROLES, lastTs, runningStatusText, statusLabel } from "./derive";

const bot = (p: Partial<Bot>): Bot => ({ id: "b", name: "b", model: "sonnet", effort: "low", status: "idle", seq: 0, events: [], ...p });

describe("statusLabel", () => {
  test("running shows the step, and the cap when there is one", () => {
    expect(statusLabel(bot({ status: "running", step: 12 }))).toBe("running · step 12");
    expect(statusLabel(bot({ status: "running", step: 12, step_cap: 60 }))).toBe("running · step 12");  // step_cap is a safety limit, not a plan: never shown
    expect(statusLabel(bot({ status: "running" }))).toBe("running · step 0");
  });
  test("other states ignore the cap", () => {
    expect(statusLabel(bot({ status: "waiting", step_cap: 60 }))).toBe("waiting for you");
    expect(statusLabel(bot({ status: "idle" }))).toBe("idle · browser off");
    expect(statusLabel(bot({ status: "idle", browser: { running: true } }))).toBe("idle");
  });
});

describe("notes", () => {
  const ev = (seq: number, role: BotEvent["role"]): BotEvent => ({ seq, ts: seq, role, text: "t" });
  test("a note is not a card subtitle but counts as activity, like an action", () => {
    expect(BOT_ROLES.includes("note")).toBe(false);
    expect(lastTs([ev(1, "thought"), ev(5, "note"), ev(9, "system")])).toBe(5);
  });
});

describe("runningStatusText", () => {
  const now = 1_000_000_000_000;
  const run = (p: Partial<Bot>) => bot({ status: "running", step: 2, ...p });
  test("shows what the bot is doing, with the time once it is 5 s or more", () => {
    expect(runningStatusText(run({ doing: "thinking", doing_since: now / 1000 - 2 }), 0, now)).toBe("running · step 2 · thinking");
    expect(runningStatusText(run({ doing: "thinking", doing_since: now / 1000 - 90 }), 0, now)).toBe("running · step 2 · thinking · 1 min");
    expect(runningStatusText(run({ doing: "writing", doing_since: now / 1000 - 12 }), 0, now)).toBe("running · step 2 · writing a reply · 12s");
    expect(runningStatusText(run({ step: 3, doing: "search the web for RL", doing_since: now / 1000 - 7 }), 0, now)).toBe("running · step 3 · search the web for RL · 7s");
  });
  test("falls back to last step when the engine reports no doing", () => {
    expect(runningStatusText(run({}), now / 1000 - 180, now)).toBe("running · step 2 · last step 3 min ago");
    expect(runningStatusText(run({}), now / 1000 - 10, now)).toBe("running · step 2");
  });
  test("calls out queued messages", () => {
    expect(runningStatusText(run({ doing: "thinking", inbox_n: 1 }), 0, now)).toBe("running · step 2 · thinking · your message is queued for its next step");
    expect(runningStatusText(run({ inbox_n: 3 }), 0, now)).toBe("running · step 2 · 3 messages are queued for its next step");
  });
  test("waiting and idle keep their own text", () => {
    expect(runningStatusText(bot({ status: "waiting", doing: "thinking" }), 0, now)).toBe("Waiting for you · answer, sign in or approve above");
    expect(runningStatusText(bot({ status: "idle" }), 0, now)).toBe("");
  });
});
