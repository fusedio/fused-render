import { expect, test } from "bun:test";
import { moveByArrow, resizeByArrow, type HomeLayout } from "./layout";

const w = (id: string, size: any, x: number, y: number) => ({ id, source: "apps" as const, size, format: "cards" as const, x, y });
const layout: HomeLayout = { version: 4, widgets: [w("full", "4x1", 0, 0), w("a", "1x1", 0, 2), w("b", "1x1", 2, 2)] };
const pos = (l: HomeLayout, id: string) => {
  const x = l.widgets.find((o) => o.id === id)!;
  return [x.x, x.y];
};

test("Alt+Right moves a 1x1 one unit (half a cell)", () => {
  const solo: HomeLayout = { version: 4, widgets: [w("a", "1x1", 0, 0)] };
  expect(pos(moveByArrow(solo, "a", "ArrowRight"), "a")).toEqual([1, 0]);
});

test("Alt+Right skips over a blocked cell and lands on the next free one", () => {
  expect(pos(moveByArrow(layout, "a", "ArrowRight"), "a")).toEqual([4, 2]);
});

test("Alt+Up into a full row is a no-op; Alt+Down opens a new row", () => {
  expect(moveByArrow(layout, "a", "ArrowUp")).toBe(layout);
  expect(pos(moveByArrow(layout, "a", "ArrowDown"), "a")).toEqual([0, 3]);
});

test("Alt+Left at x=0 is a no-op; a non-arrow key returns the same object", () => {
  expect(moveByArrow(layout, "a", "ArrowLeft")).toBe(layout);
  expect(moveByArrow(layout, "a", "Enter")).toBe(layout);
});

test("Alt+Down from a widget alone in the last row opens a new row below it", () => {
  const alone: HomeLayout = { version: 4, widgets: [w("full", "4x1", 0, 0), w("a", "1x1", 0, 2)] };
  expect(pos(moveByArrow(alone, "a", "ArrowDown"), "a")).toEqual([0, 3]);
});

test("Alt+Shift+Right grows a widget by one unit; a blocked grow is a no-op", () => {
  const one: HomeLayout = { version: 4, widgets: [w("a", "1x1", 0, 0)] };
  expect(resizeByArrow(one, "a", "ArrowRight").widgets[0].cols).toBe(3);
  const blocked: HomeLayout = { version: 4, widgets: [w("a", "1x1", 0, 0), w("b", "1x1", 2, 0)] };
  expect(resizeByArrow(blocked, "a", "ArrowRight")).toBe(blocked);
});
