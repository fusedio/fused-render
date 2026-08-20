// The crumb bar's right-click menus (lib/bar-menus). No DOM and no React
// renderer in this suite — the builders return plain data, which is the reason
// they are builders (see the module header).
import { expect, test } from "bun:test";

import type { MenuEntry, MenuItem } from "@platform/ui/ContextMenu";
import { crumbMenu, fileBarMenu, folderBarMenu, splitItems } from "@apps/explorer/lib/bar-menus";

// Labels in order, with separators spelled out — the whole point of these tests
// is the SHAPE of the list, so a divider is part of the expectation.
const labels = (items: MenuEntry[]): string[] =>
  items.map((i) => (i === "separator" ? "—" : i.label));

const item = (items: MenuEntry[], label: string): MenuItem => {
  const found = items.find((i): i is MenuItem => i !== "separator" && i.label === label);
  if (!found) throw new Error(`no "${label}" item in [${labels(items).join(", ")}]`);
  return found;
};

test("splitItems maps right/down onto the row/col directions", () => {
  const seen: string[] = [];
  const items = splitItems((dir) => seen.push(dir));
  expect(labels(items)).toEqual(["Split right", "Split down"]);
  item(items, "Split right").onClick?.();
  item(items, "Split down").onClick?.();
  expect(seen).toEqual(["row", "col"]);
});

test("splitItems rows carry a glyph, so the menu is not half-iconed", () => {
  for (const i of splitItems(() => {})) {
    expect(i === "separator" ? null : i.icon).not.toBeNull();
  }
});

test("folderBarMenu is the folder's own menu plus the splits", () => {
  // Stand-in for useFileOps.backgroundMenu() — the folder list is NOT restated
  // here, it is passed in, and this test is what pins that contract.
  const background: MenuEntry[] = [
    { label: "New Folder…" },
    { label: "New File…" },
    "separator",
    { label: "Paste", disabled: true },
  ];
  const items = folderBarMenu(background, () => {});
  expect(labels(items)).toEqual([
    "New Folder…",
    "New File…",
    "—",
    "Paste",
    "—",
    "Split right",
    "Split down",
  ]);
  // Passed through untouched, disabled state included (Paste with an empty
  // clipboard is a listed-but-dead row, not a missing one).
  expect(item(items, "Paste").disabled).toBe(true);
});

test("crumbMenu is exactly the two ancestor items, in the row menu's order", () => {
  const called: string[] = [];
  const items = crumbMenu({
    onReveal: () => called.push("reveal"),
    onOpenInNewTab: () => called.push("newtab"),
  });
  // Two items and NOTHING else — no New File/Paste/Refresh (they act on the
  // current folder, not the crumb) and no splits.
  expect(labels(items)).toEqual(["Reveal in Finder", "Open in New Tab"]);
  item(items, "Reveal in Finder").onClick?.();
  item(items, "Open in New Tab").onClick?.();
  expect(called).toEqual(["reveal", "newtab"]);
  for (const i of items) expect(i === "separator" ? null : i.icon).not.toBeNull();
});

test("fileBarMenu lists rename, Claude, the path pair and the splits", () => {
  const called: string[] = [];
  const items = fileBarMenu({
    onRename: () => called.push("rename"),
    onOpenInClaude: () => called.push("claude"),
    onCopyPath: () => called.push("copy"),
    onReveal: () => called.push("reveal"),
    onOpenInNewTab: () => called.push("newtab"),
    onSplit: (dir) => called.push("split:" + dir),
  });
  // The shared trio sits in the FOLDER menu's order (backgroundMenu):
  // Reveal → Open in New Tab → Copy Path → Claude Code. Two bars, one surface.
  expect(labels(items)).toEqual([
    "Rename…",
    "—",
    "Reveal in Finder",
    "Open in New Tab",
    "Copy Path",
    "Copy Claude session command",
    "—",
    "Split right",
    "Split down",
  ]);
  for (const label of [
    "Rename…",
    "Reveal in Finder",
    "Open in New Tab",
    "Copy Path",
    "Copy Claude session command",
  ]) {
    item(items, label).onClick?.();
  }
  item(items, "Split down").onClick?.();
  expect(called).toEqual(["rename", "reveal", "newtab", "copy", "claude", "split:col"]);
});

test("fileBarMenu drops the splits AND their separator when it can't split", () => {
  const items = fileBarMenu({
    onRename: () => {},
    onOpenInClaude: () => {},
    onCopyPath: () => {},
    onReveal: () => {},
    onOpenInNewTab: () => {},
  });
  expect(labels(items)).toEqual([
    "Rename…",
    "—",
    "Reveal in Finder",
    "Open in New Tab",
    "Copy Path",
    "Copy Claude session command",
  ]);
  // No trailing divider: a menu that ends in a separator reads as a menu with
  // something missing.
  expect(items[items.length - 1]).not.toBe("separator");
});
