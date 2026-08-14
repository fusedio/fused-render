// The crumb bar's right-click owner registry (topbar-menu.ts). Same shape of
// test as node-slot.test.ts: the store outlives any component, so the swap
// orderings are what matter.
import { afterEach, expect, test } from "bun:test";

import {
  openTopbarMenu,
  publishTopbarMenu,
  resetTopbarMenu,
} from "@apps/explorer/topbar-menu";

afterEach(resetTopbarMenu);

test("no owner: the caller is told, so it can leave the native menu alone", () => {
  expect(openTopbarMenu(3, 4)).toBe(false);
});

test("the owner is opened at the cursor", () => {
  const seen: Array<[number, number]> = [];
  publishTopbarMenu((x, y) => seen.push([x, y]));
  expect(openTopbarMenu(12, 34)).toBe(true);
  expect(seen).toEqual([[12, 34]]);
});

test("newest publisher wins for as long as it is mounted", () => {
  const seen: string[] = [];
  const releaseA = publishTopbarMenu(() => seen.push("a"));
  const releaseB = publishTopbarMenu(() => seen.push("b"));
  openTopbarMenu(0, 0);
  // The OUTGOING view releases after the incoming one published (the common
  // commit order for a folder→file swap): the bar must not fall back to it.
  releaseA();
  openTopbarMenu(0, 0);
  releaseB();
  expect(openTopbarMenu(0, 0)).toBe(false);
  expect(seen).toEqual(["b", "b"]);
  releaseA(); // idempotent, and does not resurrect anything
  expect(openTopbarMenu(0, 0)).toBe(false);
});

test("release order reversed: the survivor answers", () => {
  const seen: string[] = [];
  publishTopbarMenu(() => seen.push("a"));
  const releaseB = publishTopbarMenu(() => seen.push("b"));
  releaseB();
  openTopbarMenu(0, 0);
  expect(seen).toEqual(["a"]);
});
