import { expect, test } from "bun:test";
import { toItem } from "./widgets/BotsWidget";
import { homeBots } from "./data";

const bot = (o: Record<string, unknown>) => ({ id: "a b", name: "Ada", status: "running", ...o }) as any;

test("toItem: row opens that bot's chat", () => {
  expect(toItem(bot({})).href).toBe("/bots?bot=a%20b");
});
test("toItem: empty sub falls back to a status phrase", () => {
  expect(toItem(bot({ status: "running" })).sub).toBe("Working");
  expect(toItem(bot({ status: "waiting" })).sub).toBe("Waiting on you");
  expect(toItem(bot({ status: "error" })).sub).toBe("Something went wrong");
  expect(toItem(bot({ status: "paused" })).sub).toBe("Paused");
  expect(toItem(bot({ status: "idle" })).sub).toBe("Idle");
});
test("toItem: title text wins over the phrase", () => {
  expect(toItem(bot({ title: "Fix it" })).sub).toBe("Fix it");
});
test("homeBots: pinned first despite the oldest update, hidden dropped, input untouched", () => {
  const input = [
    bot({ id: "a", updated: 50 }),
    bot({ id: "b", updated: 40, hidden: true }),
    bot({ id: "c", updated: 10, pinned: true }),
    bot({ id: "d", updated: 30 }),
    bot({ id: "e", updated: 60, status: "waiting" }),
  ];
  const before = input.map((x: any) => x.id);
  expect(homeBots(input).map((x) => x.id)).toEqual(["c", "e", "a", "d"]);
  expect(input.map((x: any) => x.id)).toEqual(before);
});
test("toItem: an idle bot drops the stale step and page title", () => {
  expect(toItem(bot({ status: "idle", step: 1, step_cap: 60, title: "Feed | LinkedIn" })).sub).toBe("Idle");
});
test("toItem: a running bot shows its step and page title", () => {
  expect(toItem(bot({ status: "running", step: 1, step_cap: 60, title: "Feed | LinkedIn" })).sub).toBe("step 1/60 · Feed | LinkedIn");
});
