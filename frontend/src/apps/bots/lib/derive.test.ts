import { describe, expect, test } from "bun:test";
import type { Bot, BotEvent } from "./api";
import { BOT_ROLES, lastTs, statusLabel } from "./derive";

const bot = (p: Partial<Bot>): Bot => ({ id: "b", name: "b", model: "sonnet", effort: "low", status: "idle", seq: 0, events: [], ...p });

describe("statusLabel", () => {
  test("running shows the step, and the cap when there is one", () => {
    expect(statusLabel(bot({ status: "running", step: 12 }))).toBe("running · step 12");
    expect(statusLabel(bot({ status: "running", step: 12, step_cap: 60 }))).toBe("running · step 12/60");
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
