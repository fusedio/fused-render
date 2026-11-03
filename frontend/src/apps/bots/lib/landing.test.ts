import { expect, test } from "bun:test";
import { landingBot } from "./landing";

const b = (id: string, o: Record<string, unknown> = {}) => ({ id, name: id, status: "idle", ...o }) as any;
const none = () => false;

test("landingBot: a waiting bot beats an unread one and the current one", () => {
  const bots = [b("cur", { t: 9 }), b("news", { t: 8 }), b("wait", { status: "waiting", t: 1 })];
  expect(landingBot(bots, (x) => x.id === "news", "cur", (x: any) => x.t)).toBe("wait");
});
test("landingBot: unread beats current", () => {
  const bots = [b("cur", { t: 9 }), b("news", { t: 1 })];
  expect(landingBot(bots, (x) => x.id === "news", "cur", (x: any) => x.t)).toBe("news");
});
test("landingBot: two unread — the list's order (pinned, then recency) breaks the tie", () => {
  const bots = [b("old", { t: 1 }), b("new", { t: 9 }), b("pin", { pinned: true, t: 2 })];
  expect(landingBot(bots, () => true, null, (x: any) => x.t)).toBe("pin");
  expect(landingBot(bots.filter((x) => !x.pinned), () => true, null, (x: any) => x.t)).toBe("new");
});
test("landingBot: nothing to point at keeps the current bot", () => {
  expect(landingBot([b("a"), b("cur")], none, "cur")).toBe("cur");
});
test("landingBot: no current — Super Bot, else the first visible", () => {
  expect(landingBot([b("a"), b("sup", { kind: "super" })], none, null)).toBe("sup");
  expect(landingBot([b("a"), b("c")], none, null)).toBe("a");
});
test("landingBot: hidden bots are never picked, even waiting, even current", () => {
  const bots = [b("hid", { status: "waiting", hidden: true }), b("a")];
  expect(landingBot(bots, none, "hid")).toBe("a");
  expect(landingBot([b("only", { hidden: true })], none, null)).toBe("only");
  expect(landingBot([], none, null)).toBeNull();
});
