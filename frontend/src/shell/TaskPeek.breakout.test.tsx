// The Tasks peek's ⋮ offers Breakout from the chat's own report (the peek's
// head hides the chat's kebab).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";

const { breakoutMenuEntries } = await import("./TaskPeek");

const ctl = (over: Partial<{ available: boolean; picking: boolean }> = {}) => {
  const calls: string[] = [];
  return { calls, ctl: { available: true, picking: false, toggle: () => void calls.push("toggle"), ...over } };
};
const first = (e: ReturnType<typeof breakoutMenuEntries>) => e[0] as { label: string; onClick: () => void };

test("available: a Breakout… item plus a separator, and clicking calls toggle", () => {
  const { ctl: c, calls } = ctl();
  const items = breakoutMenuEntries(c);
  expect(items).toHaveLength(2);
  expect(first(items).label).toBe("Breakout…");
  expect(items[1]).toBe("separator");
  first(items).onClick();
  expect(calls).toEqual(["toggle"]);
});

test("unavailable or absent: no item", () => {
  expect(breakoutMenuEntries(ctl({ available: false }).ctl)).toEqual([]);
  expect(breakoutMenuEntries(null)).toEqual([]);
});

test("picking: the item reads Cancel breakout pick", () => {
  expect(first(breakoutMenuEntries(ctl({ picking: true }).ctl)).label).toBe("Cancel breakout pick");
});
