import { expect, test } from "bun:test";
import { botOrder } from "./order";

const b = (id: string, o: Record<string, unknown> = {}) => ({ id, name: id, ...o }) as any;
const rec = (x: any) => x.t || 0;
const sorted = (bots: any[], waiting: (x: any) => boolean = () => false) => bots.slice().sort(botOrder(waiting, rec)).map((x) => x.id);

test("botOrder: pinned beats a newer unpinned bot", () => {
  expect(sorted([b("new", { t: 9 }), b("pin", { pinned: true, t: 1 })])).toEqual(["pin", "new"]);
});
test("botOrder: waiting beats non-waiting at equal pin", () => {
  expect(sorted([b("new", { t: 9 }), b("wait", { t: 1 })], (x) => x.id === "wait")).toEqual(["wait", "new"]);
});
test("botOrder: recency descending otherwise", () => {
  expect(sorted([b("a", { t: 1 }), b("c", { t: 3 }), b("m", { t: 2 })])).toEqual(["c", "m", "a"]);
});
