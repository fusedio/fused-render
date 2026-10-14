import { expect, test } from "bun:test";
import { BOTS_PREFIX, RESERVED, appsHref, botHref, isBotId, isBotsPath, parseBotsPath, prefsHref, tasksHref } from "./routes";

test("every page round-trips through its own href", () => {
  expect(parseBotsPath(botHref("859e80cc"))).toEqual({ kind: "bot", id: "859e80cc" });
  expect(parseBotsPath(tasksHref())).toEqual({ kind: "tasks", task: null });
  expect(parseBotsPath("/bots/tasks", "?task=pending%3Ae1")).toEqual({ kind: "tasks", task: "pending:e1" });
  expect(parseBotsPath(appsHref())).toEqual({ kind: "apps", folder: null });
  expect(parseBotsPath(appsHref("invoice tracker"))).toEqual({ kind: "apps", folder: "invoice tracker" });
  expect(parseBotsPath(prefsHref())).toEqual({ kind: "prefs" });
  expect(parseBotsPath(BOTS_PREFIX)).toEqual({ kind: "list" });
  expect(parseBotsPath(BOTS_PREFIX + "/")).toEqual({ kind: "list" });
});

test("reserved names are pages, never bot ids", () => {
  for (const r of RESERVED) expect(isBotId(r)).toBe(false);
  expect(isBotId("859e80cc")).toBe(true);
  expect(isBotId("imported-bot_1")).toBe(true);
  expect(isBotId("a/b")).toBe(false);
  expect(isBotId("")).toBe(false);
});

test("unknown shapes land on the nearest page instead of erroring", () => {
  expect(parseBotsPath("/bots/859e80cc/extra")).toEqual({ kind: "bot", id: "859e80cc" });
  expect(parseBotsPath("/bots/tasks/x")).toEqual({ kind: "tasks", task: null });
  expect(parseBotsPath("/bots/%2F")).toEqual({ kind: "list" });
  expect(parseBotsPath("/home")).toEqual({ kind: "list" });
});

test("isBotsPath is the prefix, not the bare path", () => {
  expect(isBotsPath("/bots")).toBe(true);
  expect(isBotsPath("/bots/859e80cc")).toBe(true);
  expect(isBotsPath("/botsy")).toBe(false);
  expect(isBotsPath("/home")).toBe(false);
});
