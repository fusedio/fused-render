// THE HOST'S ROWS IN THE ⋮ (`extraItems`; shell/TaskHead): the shared task head
// folds its ↗ Open into this menu, and a task whose folder is gone shows the
// door greyed. READ OFF THE SOURCE, for the reason useTaskId.test.tsx gives:
// base-ui's `DropdownMenuContent` mounts no item until a real pointer event has
// opened it, so there is nothing to click under a renderer with no DOM.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const src = readFileSync(join(import.meta.dir, "Kebab.tsx"), "utf8");
const menu = src.slice(src.indexOf("<DropdownMenuContent"), src.indexOf("</DropdownMenuContent>"));

test("the host's rows sit in the FIRST group, after Full screen and Breakout", () => {
  const at = (needle: string) => {
    const i = menu.indexOf(needle);
    expect(i).toBeGreaterThan(-1);
    return i;
  };
  const order = [
    at("fullScreen.onToggle"),
    at("breakout.onToggle"),
    at("extraItems?.map("),
    // The group's own separator, then the hand-offs.
    at('<DropdownMenuSeparator className="c-kebab-sep" />'),
    at("TASK_ICON_TERMINAL"),
  ];
  expect(order).toEqual([...order].sort((a, b) => a - b));
});

test("the group exists for host rows alone, and its separator still needs a group after it", () => {
  expect(menu).toContain("{fullScreen || breakout || extraItems?.length ? (");
  expect(menu).toContain("{showHandoffs || showFiling ? <DropdownMenuSeparator");
});

test("a host row is a real menu item: its label, its mark, disabled with a reason, danger in red", () => {
  const row = menu.slice(menu.indexOf("extraItems?.map("), menu.indexOf("))}", menu.indexOf("extraItems?.map(")));
  expect(row).toContain('className={"c-kebab-opt" + (item.danger ? " is-danger" : "")}');
  expect(row).toContain("disabled={!!item.disabled}");
  expect(row).toContain("title={item.title}");
  expect(row).toContain('<span className="c-kebab-icon">{item.icon}</span>');
  expect(row).toContain("{item.label}");
  expect(row).toContain("onClick={() => item.onClick()}");
});
