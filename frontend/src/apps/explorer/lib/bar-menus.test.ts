// The explorer's grouped menu builders (lib/bar-menus). No DOM and no React
// renderer in this suite — the builders return plain data, which is the reason
// they are builders (see the module header).
import { expect, test } from "bun:test";

import type { MenuEntry, MenuItem } from "@platform/ui/ContextMenu";

// bar-menus reaches the router (via fs-actions, for dirname/normDir), which
// reads `location` at module scope, so the stub has to precede the (therefore
// dynamic) import — same trade as fs-actions.test.ts.
(globalThis as { location?: unknown }).location = new URL("http://x/");
const { crumbMenu, entryMenu, finderMenu, splitItems, canRenameBase } = await import(
  "@apps/explorer/lib/bar-menus"
);

// Labels in order, with separators spelled out — the whole point of these tests
// is the SHAPE of the list, so a divider is part of the expectation.
const labels = (items: MenuEntry[]): string[] =>
  items.map((i) => (i === "separator" ? "—" : i.label));

const item = (items: MenuEntry[], label: string): MenuItem => {
  const found = items.find((i): i is MenuItem => i !== "separator" && i.label === label);
  if (!found) throw new Error(`no "${label}" item in [${labels(items).join(", ")}]`);
  return found;
};

const row = (label: string, onClick?: () => void): MenuItem => ({ label, icon: null, onClick });

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

// -- entryMenu -----------------------------------------------------------------
// ONE builder for the folder's three surfaces (kebab, background right-click,
// crumb bar) and the file's two (kebab, crumb bar). These pin the group order
// and the divider rule; the rows are stand-ins, passed in, never restated.

test("entryMenu over a FOLDER: app → create → subject → open → copy → embed, one divider between", () => {
  const items = entryMenu({
    copy: [row("Copy Path")],
    embed: [row("Open in embed")],
    app: [row("App Doctor"), row("Share…")],
    open: [row("Open in New Tab"), row("Split right")],
    create: [row("New Folder…"), { label: "Paste", disabled: true }],
    subject: [row("Rename…"), row("Refresh")],
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
    "Copy Path",
    "—",
    "Open in embed",
  ]);
  // Passed through untouched, disabled state included (Paste with an empty
  // clipboard is a listed-but-dead row, not a missing one).
  expect(item(items, "Paste").disabled).toBe(true);
});

test("entryMenu over a FILE: no create, share between open and copy, embed last and alone", () => {
  const called: string[] = [];
  const items = entryMenu({
    app: [row("App Doctor"), row("Share…"), row("Set Current View as Preview", () => called.push("shot"))],
    subject: [row("Rename…", () => called.push("rename"))],
    open: [
      row("Reveal in Finder", () => called.push("reveal")),
      row("Open in New Tab", () => called.push("newtab")),
      ...splitItems((dir) => called.push("split:" + dir)),
    ],
    share: [row("Share file…", () => called.push("sharefile"))],
    copy: [row("Copy Path", () => called.push("copy")), row("Copy Claude session command", () => called.push("claude"))],
    embed: [row("Open in embed")],
  });
  // The shared rows sit in the FOLDER fill's order (useFileOps.folderGroups):
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
    "Share file…",
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
    "Share file…",
    "Copy Path",
    "Copy Claude session command",
  ]) {
    item(items, label).onClick?.();
  }
  item(items, "Split down").onClick?.();
  expect(called).toEqual(["shot", "rename", "reveal", "newtab", "sharefile", "copy", "claude", "split:col"]);
});

test("entryMenu draws no rule for an empty or absent group, at either end or between", () => {
  // A plain folder (no app rows) in a panel pane (nothing extra to open).
  expect(labels(entryMenu({ app: [], create: [row("New File…")], copy: [row("Copy Path")] }))).toEqual([
    "New File…",
    "—",
    "Copy Path",
  ]);
  expect(labels(entryMenu({ subject: [row("Refresh")] }))).toEqual(["Refresh"]);
  // A plain file (no app rows, nothing to photograph) in a pane (no splits):
  // what usePreviewFileMenu's groups alone produce.
  const file = entryMenu({
    app: [],
    subject: [row("Rename…")],
    open: [row("Reveal in Finder"), row("Open in New Tab")],
    copy: [row("Copy Path"), row("Copy Claude session command")],
  });
  expect(labels(file)).toEqual([
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
  expect(file[file.length - 1]).not.toBe("separator");
  // The kebab over a directory previewed in a non-listing mode, or over a file
  // in a pane: the app rows alone, no file groups at all.
  expect(labels(entryMenu({ app: [row("App Doctor")], embed: [row("Open in embed")] }))).toEqual([
    "App Doctor",
    "—",
    "Open in embed",
  ]);
  expect(entryMenu({})).toEqual([]);
});

// -- finderMenu ----------------------------------------------------------------
// The listing row's and the pane header's right-click: Finder order, the
// destructive verbs included. Same builder, the pane header fills less.

test("finderMenu: open → delete → edit → clip → copy, the listing row's full fill", () => {
  const items = finderMenu({
    copy: [row("Copy Path"), row("Reveal in Finder"), row("Copy Claude session command")],
    clip: [row("Cut"), row("Copy"), { label: "Paste", disabled: true }],
    edit: [row("Rename…"), row("Duplicate"), row("Compress"), row("Share…")],
    delete: [row("Delete")],
    open: [row("Open"), row("Open in New Tab"), row("Open With")],
  });
  expect(labels(items)).toEqual([
    "Open",
    "Open in New Tab",
    "Open With",
    "—",
    "Delete",
    "—",
    "Rename…",
    "Duplicate",
    "Compress",
    "Share…",
    "—",
    "Cut",
    "Copy",
    "Paste",
    "—",
    "Copy Path",
    "Reveal in Finder",
    "Copy Claude session command",
  ]);
  expect(item(items, "Paste").disabled).toBe(true);
});

test("finderMenu: the pane header's smaller fill keeps the same structure", () => {
  // No Open / Open in New Tab (already viewing it), no Paste (nothing to paste
  // INTO from a single file), no Compress, no Claude command (owner,
  // 2026-09-22: keep as is).
  const items = finderMenu({
    open: [row("Open With")],
    delete: [row("Delete")],
    edit: [row("Rename…"), row("Duplicate")],
    clip: [row("Cut"), row("Copy")],
    copy: [row("Copy Path"), row("Reveal in Finder")],
  });
  expect(labels(items)).toEqual([
    "Open With",
    "—",
    "Delete",
    "—",
    "Rename…",
    "Duplicate",
    "—",
    "Cut",
    "Copy",
    "—",
    "Copy Path",
    "Reveal in Finder",
  ]);
  expect(finderMenu({})).toEqual([]);
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

// -- canRenameBase -------------------------------------------------------------
// The folder's entry menu (kebab, background right-click, and the crumb bar
// over the current folder) gains a "Rename…" item — this pins the guard that
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
