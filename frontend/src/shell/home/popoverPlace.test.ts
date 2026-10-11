import { expect, test } from "bun:test";
import { placePopover } from "./popoverPlace";

const bounds = { left: 200, top: 0, right: 1200, bottom: 800 };
const size = { width: 440, height: 500 };

test("opens below, right-aligned, when it fits", () => {
  const anchor = { left: 700, top: 100, right: 800, bottom: 130 };
  const p = placePopover({ anchor, bounds, size });
  expect(p).toMatchObject({ left: 360, top: 136, width: 440 });
});

test("an anchor near the content's left edge never reaches behind the sidebar", () => {
  const anchor = { left: 210, top: 100, right: 300, bottom: 130 };
  const p = placePopover({ anchor, bounds, size });
  expect(p.left).toBeGreaterThanOrEqual(208);
});

test("flips above when below does not fit but above does", () => {
  const anchor = { left: 700, top: 600, right: 800, bottom: 630 };
  const p = placePopover({ anchor, bounds, size });
  expect(p.top).toBe(600 - 6 - 500);
});

test("clamps with a max height when neither side fits", () => {
  const anchor = { left: 700, top: 300, right: 800, bottom: 330 };
  const p = placePopover({ anchor, bounds, size: { width: 440, height: 900 } });
  expect(p.maxHeight).toBe(784);
  expect(p.top).toBe(8);
});

test("a narrow area shrinks the width and keeps the margin on both sides", () => {
  const p = placePopover({ anchor: { left: 250, top: 10, right: 300, bottom: 40 }, bounds: { left: 200, top: 0, right: 500, bottom: 800 }, size });
  expect(p.width).toBe(284);
  expect(p.left).toBe(208);
});

test("alignLeft hangs from the anchor's left edge", () => {
  const p = placePopover({ anchor: { left: 400, top: 10, right: 500, bottom: 40 }, bounds, size, alignLeft: true });
  expect(p.left).toBe(400);
});
