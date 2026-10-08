import { expect, test } from "bun:test";
import { toItem } from "./widgets/BotsWidget";

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
