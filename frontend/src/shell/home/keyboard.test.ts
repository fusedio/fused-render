import { expect, test } from "bun:test";
import { moveByArrow, type HomeLayout } from "./layout";

const w = (id: string, size: any, x: number, y: number) => ({ id, source: "apps" as const, size, format: "cards" as const, x, y });
const layout: HomeLayout = { version: 3, widgets: [w("full", "4x1", 0, 0), w("a", "1x1", 0, 1), w("b", "1x1", 1, 1)] };
const pos = (l: HomeLayout, id: string) => {
  const x = l.widgets.find((o) => o.id === id)!;
  return [x.x, x.y];
};

test("Alt+Right skips over a blocked cell and lands on the next free one", () => {
  expect(pos(moveByArrow(layout, "a", "ArrowRight"), "a")).toEqual([2, 1]);
});

test("Alt+Up into a full row is a no-op; Alt+Down opens a new row", () => {
  expect(moveByArrow(layout, "a", "ArrowUp")).toBe(layout);
  expect(pos(moveByArrow(layout, "a", "ArrowDown"), "a")).toEqual([0, 2]);
});

test("Alt+Left at x=0 is a no-op; a non-arrow key returns the same object", () => {
  expect(moveByArrow(layout, "a", "ArrowLeft")).toBe(layout);
  expect(moveByArrow(layout, "a", "Enter")).toBe(layout);
});

test("Alt+Down from a widget alone in the last row opens a new row below it", () => {
  const alone: HomeLayout = { version: 3, widgets: [w("full", "4x1", 0, 0), w("a", "1x1", 0, 1)] };
  expect(pos(moveByArrow(alone, "a", "ArrowDown"), "a")).toEqual([0, 2]);
});
