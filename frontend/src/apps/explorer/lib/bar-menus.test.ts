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
  fileMenu,
  folderMenu,
  splitItems,
  canRenameBase,
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

test("folderMenu shows the groups in a fixed order with one separator between", () => {
  // Stand-ins for what the surfaces fill in — the rows are NOT restated here,
  // they are passed in, and this test is what pins that contract.
  const items = folderMenu({
    copy: [{ label: "Copy path" }],
    embed: [{ label: "Open in embed" }],
    app: [{ label: "App Doctor" }, { label: "Share…" }],
    open: [{ label: "Open in New Tab" }, { label: "Split right" }],
    create: [{ label: "New Folder…" }, { label: "Paste", disabled: true }],
    folder: [{ label: "Rename…" }, { label: "Refresh" }],
  });
  expect(labels(items)).toEqual([
    "App Doctor",
    "Share…",
    "—",
    "New Folder…",
    "Paste",
    "—",
    "Rename…",
    "Refresh",
    "—",
    "Open in New Tab",
    "Split right",
    "—",
    "Copy path",
    "—",
    "Open in embed",
  ]);
  // Passed through untouched, disabled state included (Paste with an empty
  // clipboard is a listed-but-dead row, not a missing one).
  expect(item(items, "Paste").disabled).toBe(true);
});

test("folderMenu draws no rule for an empty or absent group, at either end or between", () => {
  // A plain folder (no app rows) in a panel pane (nothing extra to open).
  expect(labels(folderMenu({ app: [], create: [{ label: "New File…" }], copy: [{ label: "Copy path" }] }))).toEqual([
    "New File…",
    "—",
    "Copy path",
  ]);
  expect(labels(folderMenu({ folder: [{ label: "Refresh" }] }))).toEqual(["Refresh"]);
  expect(folderMenu({})).toEqual([]);
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

// -- fileMenu ------------------------------------------------------------------
// The file preview's kebab and the crumb bar's right-click over the open file
// show ONE list, composed by Preview.tsx from useAppActionRows' groups and
// usePreviewFileMenu's. These pin the builder's arrangement: the folder menu's
// groups minus `create`, in the folder menu's order.

const row = (label: string, onClick?: () => void): MenuItem => ({ label, icon: null, onClick });

test("fileMenu lays the groups out app → file → open → copy → embed with one divider between", () => {
  const called: string[] = [];
  const items = fileMenu({
    app: [row("App Doctor"), row("Share…"), row("Set Current View as Preview", () => called.push("shot"))],
    file: [row("Rename…", () => called.push("rename"))],
    open: [
      row("Reveal in Finder", () => called.push("reveal")),
      row("Open in New Tab", () => called.push("newtab")),
      ...splitItems((dir) => called.push("split:" + dir)),
    ],
    copy: [row("Copy Path", () => called.push("copy")), row("Copy Claude session command", () => called.push("claude"))],
    embed: [row("Open in embed")],
  });
  // The shared rows sit in the FOLDER menu's order (useFileOps.folderGroups):
  // Reveal → Open in New Tab → the splits, then the copy pair, and Open in
  // embed closes the list on its own. Two bars, one surface.
  expect(labels(items)).toEqual([
    "App Doctor",
    "Share…",
    "Set Current View as Preview",
    "—",
    "Rename…",
    "—",
    "Reveal in Finder",
    "Open in New Tab",
    "Split right",
    "Split down",
    "—",
    "Copy Path",
    "Copy Claude session command",
    "—",
    "Open in embed",
  ]);
  for (const label of [
    "Set Current View as Preview",
    "Rename…",
    "Reveal in Finder",
    "Open in New Tab",
    "Copy Path",
    "Copy Claude session command",
  ]) {
    item(items, label).onClick?.();
  }
  item(items, "Split down").onClick?.();
  expect(called).toEqual(["shot", "rename", "reveal", "newtab", "copy", "claude", "split:col"]);
});

test("fileMenu draws no divider for an empty or absent group and never ends in one", () => {
  // A plain file (no app rows, nothing to photograph) in a pane (no splits):
  // what usePreviewFileMenu's groups alone produce.
  const items = fileMenu({
    app: [],
    file: [row("Rename…")],
    open: [row("Reveal in Finder"), row("Open in New Tab")],
    copy: [row("Copy Path"), row("Copy Claude session command")],
  });
  expect(labels(items)).toEqual([
    "Rename…",
    "—",
    "Reveal in Finder",
    "Open in New Tab",
    "—",
    "Copy Path",
    "Copy Claude session command",
  ]);
  // No trailing divider: a menu that ends in a separator reads as a menu with
  // something missing.
  expect(items[items.length - 1]).not.toBe("separator");
  // The kebab over a directory previewed in a non-listing mode, or over a file
  // in a pane: the app rows alone, no file groups at all.
  expect(labels(fileMenu({ app: [row("App Doctor")], embed: [row("Open in embed")] }))).toEqual([
    "App Doctor",
    "—",
    "Open in embed",
  ]);
  expect(fileMenu({})).toEqual([]);
});

// -- canRenameBase -------------------------------------------------------------
// The folder menu (kebab, background right-click, and the crumb bar over
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
