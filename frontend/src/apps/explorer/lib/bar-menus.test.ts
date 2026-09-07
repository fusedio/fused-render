// The crumb bar's right-click menus (lib/bar-menus). No DOM and no React
// renderer in this suite — the builders return plain data, which is the reason
// they are builders (see the module header).
import { expect, test } from "bun:test";

import type { MenuEntry, MenuItem } from "@platform/ui/ContextMenu";

// bar-menus now reaches the router (via fs-actions, for dirname/normDir),
// which reads `location` at module scope, so the stub has to precede the
// (therefore dynamic) import — same trade as fs-actions.test.ts.
(globalThis as { location?: unknown }).location = new URL("http://x/");
const {
  crumbMenu,
  fileBarMenu,
  folderBarMenu,
  splitItems,
  canRenameBase,
  withFolderRename,
} = await import("@apps/explorer/lib/bar-menus");

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

test("fileBarMenu offers Set Current View as Preview only on an app entry, in its own group", () => {
  const base = {
    onRename: () => {},
    onOpenInClaude: () => {},
    onCopyPath: () => {},
    onReveal: () => {},
    onOpenInNewTab: () => {},
  };
  // A plain file: no preview verb, and no orphan separator for it.
  expect(labels(fileBarMenu(base))).not.toContain("Set Current View as Preview");
  let shot = 0;
  const items = fileBarMenu({ ...base, onSetPreview: () => shot++, onSplit: () => {} });
  expect(labels(items)).toEqual([
    "Rename…",
    "—",
    "Reveal in Finder",
    "Open in New Tab",
    "Copy Path",
    "Copy Claude session command",
    "—",
    "Set Current View as Preview",
    "—",
    "Split right",
    "Split down",
  ]);
  item(items, "Set Current View as Preview").onClick?.();
  expect(shot).toBe(1);
  // Without splits the preview group still closes the list cleanly.
  expect(labels(fileBarMenu({ ...base, onSetPreview: () => {} })).slice(-2)).toEqual([
    "—",
    "Set Current View as Preview",
  ]);
});

// -- canRenameBase / withFolderRename ----------------------------------------
// The folder background menu (and, through folderBarMenu, the crumb bar over
// the current folder) gains a "Rename…" item — this pins the guard that
// decides when, and the shape it produces.

test("canRenameBase allows an ordinary folder anywhere, including inside a mount", () => {
  const guard = { home: "/Users/x", mountsRoot: "/Users/x/.fused-render/mounts" };
  expect(canRenameBase("/Users/x/Projects", guard)).toBe(true);
  expect(canRenameBase("/Users/x/Projects/sub", guard)).toBe(true);
  // A folder nested INSIDE a mount (not the mount root itself) is ordinary.
  expect(canRenameBase("/Users/x/.fused-render/mounts/bucket/inner", guard)).toBe(true);
});

test("canRenameBase refuses the filesystem root", () => {
  const guard = {};
  expect(canRenameBase("/", guard)).toBe(false);
  expect(canRenameBase("", guard)).toBe(false); // "" normalizes to "/"
});

test("canRenameBase refuses the home folder", () => {
  const guard = { home: "/Users/x", mountsRoot: "/Users/x/.fused-render/mounts" };
  expect(canRenameBase("/Users/x", guard)).toBe(false);
  expect(canRenameBase("/Users/x/Documents", guard)).toBe(true);
});

test("canRenameBase refuses a mount root but not what's inside or beside it", () => {
  const guard = { home: "/Users/x", mountsRoot: "/Users/x/.fused-render/mounts" };
  expect(canRenameBase("/Users/x/.fused-render/mounts/bucket", guard)).toBe(false);
  expect(canRenameBase("/Users/x/.fused-render/mounts/bucket/inner", guard)).toBe(true);
  // The mounts_root directory itself is refused too: renaming it breaks every
  // mount at once (review, PR #1049). Trailing slashes in the config are fine.
  expect(canRenameBase("/Users/x/.fused-render/mounts", guard)).toBe(false);
  const slashed = { home: "/Users/x/", mountsRoot: "/Users/x/.fused-render/mounts/" };
  expect(canRenameBase("/Users/x", slashed)).toBe(false);
  expect(canRenameBase("/Users/x/.fused-render/mounts/bucket", slashed)).toBe(false);
  expect(canRenameBase("/Users/x/Projects", slashed)).toBe(true);
});

test("canRenameBase fails closed while config hasn't loaded (home/mountsRoot undefined)", () => {
  // Nothing is renameable until BOTH are known (bugbot, PR #1049): an empty
  // guard used to allow home and mount roots through.
  expect(canRenameBase("/", {})).toBe(false);
  expect(canRenameBase("/Users/x", {})).toBe(false);
  expect(canRenameBase("/Users/x/Projects", {})).toBe(false);
  expect(canRenameBase("/Users/x/Projects", { home: "/Users/x" })).toBe(false);
  expect(canRenameBase("/Users/x/Projects", { home: "/Users/x", mountsRoot: "/m" })).toBe(true);
});

test("withFolderRename leads the list with Rename… + a separator when allowed", () => {
  const rest: MenuEntry[] = [{ label: "New Folder…" }, "separator", { label: "Refresh" }];
  let renamed = false;
  const items = withFolderRename(rest, "/Users/x/Projects", { home: "/Users/x", mountsRoot: "/m" }, () => {
    renamed = true;
  });
  expect(labels(items)).toEqual(["Rename…", "—", "New Folder…", "—", "Refresh"]);
  item(items, "Rename…").onClick?.();
  expect(renamed).toBe(true);
  expect(item(items, "Rename…").icon).not.toBeNull();
});

test("withFolderRename hands the list back untouched when the guard refuses", () => {
  const rest: MenuEntry[] = [{ label: "New Folder…" }, "separator", { label: "Refresh" }];
  const items = withFolderRename(rest, "/Users/x", { home: "/Users/x" }, () => {});
  expect(items).toBe(rest); // same array, not just same shape
  expect(labels(items)).toEqual(["New Folder…", "—", "Refresh"]);
});
