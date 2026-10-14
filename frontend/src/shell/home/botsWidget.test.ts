import { expect, test } from "bun:test";
import { toItem } from "./widgets/BotsWidget";
import { homeBots } from "./bots";

const bot = (o: Record<string, unknown>) => ({ id: "a b", name: "Ada", status: "running", ...o }) as any;

test("toItem: row opens that bot's chat", () => {
  expect(toItem(bot({})).href).toBe("/bots/a%20b");
});
test("toItem: empty sub falls back to a status phrase", () => {
  expect(toItem(bot({ status: "running" })).sub).toBe("Working");
  expect(toItem(bot({ status: "waiting" })).sub).toBe("Waiting on you");
  expect(toItem(bot({ status: "error" })).sub).toBe("Something went wrong");
  expect(toItem(bot({ status: "paused" })).sub).toBe("Paused");
  expect(toItem(bot({ status: "idle" })).sub).toBe("Idle");
});
test("toItem: note text wins over the phrase", () => {
  expect(toItem(bot({ status: "error", note: "Login failed" })).sub).toBe("Login failed");
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
  expect(homeBots(input, {}).map((x) => x.id)).toEqual(["c", "e", "a", "d"]);
  expect(input.map((x: any) => x.id)).toEqual(before);
});
test("toItem: an idle bot drops the stale step and page title", () => {
  expect(toItem(bot({ status: "idle", step: 1, step_cap: 60, title: "Feed | LinkedIn" })).sub).toBe("Idle");
});
test("toItem: a running bot shows its step and page title", () => {
  expect(toItem(bot({ status: "running", step: 1, step_cap: 60, title: "Feed | LinkedIn" })).sub).toBe("step 1/60 · Feed | LinkedIn");
});
test("homeBots: recency is last_user_ts, not updated", () => {
  const input = [
    bot({ id: "a", updated: 99, last_user_ts: 10 }),
    bot({ id: "b", updated: 5, last_user_ts: 20 }),
    bot({ id: "c", updated: 50, created: 15 }),
  ];
  expect(homeBots(input, {}).map((x) => x.id)).toEqual(["b", "c", "a"]);
});
test("homeBots: a waiting bot jumps ahead only when never opened or carrying unread", () => {
  const input = [
    bot({ id: "a", last_user_ts: 100 }),
    bot({ id: "w", status: "waiting", last_user_ts: 1, seq: 5 }),
  ];
  expect(homeBots(input, { w: 5 }).map((x) => x.id)).toEqual(["a", "w"]);
  expect(homeBots(input, {}).map((x) => x.id)).toEqual(["w", "a"]);
  expect(homeBots(input, { w: 3 }).map((x) => x.id)).toEqual(["w", "a"]);
});
test("toItem: a just-started running bot shows its note, not the last run's page", () => {
  expect(toItem(bot({ status: "running", step: 0, step_cap: 60, title: "Old page", note: "Downloading model… 40%" })).sub).toBe("Downloading model… 40%");
});
test("toItem: paused shows only the phrase, waiting shows its note", () => {
  expect(toItem(bot({ status: "paused", note: "stale note" })).sub).toBe("Paused");
  expect(toItem(bot({ status: "waiting", note: "Need your login" })).sub).toBe("Need your login");
});
