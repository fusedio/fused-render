// The item lists for the explorer's kebab and CRUMB BAR right-click menus, in
// the two states the bar has: over a folder (the listing owns it) and over a
// single file (the preview owns it).
//
// Plain builders taking their actions as callbacks, for the reason every other
// menu in the explorer is built this way (listing/useFileOps, lib/fs-actions'
// buildOpenWithItems): there are no React component tests in this repo, so a
// menu's shape is only checkable when the list is a function of its inputs
// rather than JSX inline in a handler. bar-menus.test.ts is the whole argument.
//
// THE FOLDER MENU IS ONE LIST ON THREE SURFACES — the listing's kebab (`⋮` in
// the search row), a right-click on the listing's empty background, and a
// right-click on the crumb bar over the folder. They used to be three lists:
// the kebab carried the app rows (Share, Open as project, MCP, App Doctor,
// embed) and then the folder ops and the splits; the background right-click
// carried the folder ops alone, in a different order; the bar carried the
// folder ops and the splits. Same folder, three menus that disagreed about
// what you could do to it. `folderMenu` below is the ONE builder: every
// surface hands it the same grouped input and shows the same rows in the same
// order. The rows themselves are not written out here — the folder ops come
// from useFileOps (clipboard, dialogs, refetch it owns), the app rows from
// EntryActionsMenu's hook — so no surface can grow a private copy.
//
// THE FILE MENU IS THE SAME ARRANGEMENT ON TWO SURFACES — the file preview's
// kebab (`⋮` after the mode control) and a right-click on the crumb bar over
// the open file. They used to be two lists: the kebab carried the app rows
// alone, the bar carried Rename, Reveal, the copies and the splits, and nothing
// showed both. `fileMenu` below is the file's ONE builder, the folder menu's
// groups minus `create` (there is nothing to put INTO a file): the app rows
// come from EntryActionsMenu's hook, the file ops from Preview's
// usePreviewFileMenu (its rename dialog, its clipboard, its preview shot).
import { createElement } from "react";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { SplitDownIcon, SplitRightIcon } from "@platform/ui/SplitIcons";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";

export type SplitDir = "row" | "col";

// The pieces of config a folder-rename decision needs — both optional because
// the guard must fail CLOSED (no Rename offered) before /api/config has
// answered, rather than briefly show a Rename that then can't act on the real
// home/mounts paths. Backslashes are the caller's job to normalize (both
// fields come from `Config`, same as every other consumer of `config.home`).
export interface RenameBaseGuard {
  home?: string;
  mountsRoot?: string;
}

// Whether the CURRENT folder (not a row inside it) may be renamed from the
// crumb bar / folder background menu. False for:
//   - the filesystem/drive root (its own parent, per fs-actions.dirname)
//   - the home folder (~) — the sidebar, bookmarks and countless "~/…" paths
//     assume it never moves
//   - a mount root (one level under `mounts_root` — every mount lives at
//     `${mounts_root}/<name>`, so a dir whose PARENT is mounts_root IS one)
// Nothing else is special-cased: an ordinary folder anywhere else, including
// one nested inside a mount, is rename-able like any other.
function stripSlash(p: string): string {
  return p.length > 1 ? p.replace(/\/+$/, "") : p;
}

export function canRenameBase(dir: string, guard: RenameBaseGuard): boolean {
  const norm = normDir(dir);
  const parent = dirname(norm);
  if (parent === norm) return false; // filesystem/drive root
  // FAILS CLOSED until the config has answered (bugbot, PR #1049): with no
  // home and no mounts root known, this cannot tell a renameable folder from
  // the two it must never move, so it offers nothing rather than everything.
  if (guard.home === undefined || guard.mountsRoot === undefined) return false;
  // Compared with trailing slashes stripped on BOTH sides: `dirname` strips
  // them, a config value might carry one (review, PR #1049).
  const home = stripSlash(guard.home);
  const mounts = stripSlash(guard.mountsRoot);
  if (norm === home) return false;
  // The mounts root, and every mount directly under it: renaming either breaks
  // every mount at once.
  if (norm === mounts || parent === mounts) return false;
  return true;
}

// The two split-entry rows, with the same glyphs the panel bar uses. One
// definition for the file bar's menu and the folder menu's `open` group,
// because "Split right" that means `row` in one menu and `col` in another is
// the kind of bug nobody re-checks.
export function splitItems(onSplit: (dir: SplitDir) => void): MenuEntry[] {
  return [
    {
      label: "Split right",
      icon: createElement(SplitRightIcon, { size: 16 }),
      onClick: () => onSplit("row"),
    },
    {
      label: "Split down",
      icon: createElement(SplitDownIcon, { size: 16 }),
      onClick: () => onSplit("col"),
    },
  ];
}

// The folder menu's GROUPS, in the order they are shown. A group is a run of
// rows with a separator either side; an empty or absent group draws nothing,
// not a stray rule.
//
//   app     what this folder IS, when it is an app: Share…, Open as
//           project, MCP config, App Doctor. First because it is the reason the
//           kebab carries a status dot, and absent on a plain folder.
//   create  things that put something new in this folder: New Folder…,
//           New File…, Paste. The verbs a hand reaches for a background menu
//           for, so they lead once the app rows are out of the way.
//   folder  the folder itself: Rename…, Refresh.
//   open    the same folder somewhere else: Reveal in Finder, Open in New
//           Tab, Split right, Split down. One group because they all answer
//           "show me this elsewhere"; the splits are not a special case of
//           anything, just two more elsewheres.
//   copy    text to the clipboard: Copy path, Copy Claude session command.
//   embed   Open in embed, LAST and alone (owner, 2026-09-20: "move open in
//           embed to the bottom"): it leaves the explorer for the chrome-free
//           view, so it closes the list rather than sitting among the rows
//           that keep you here.
//
// Each surface fills what it may offer (a panel pane cannot split or embed; a
// folder that is not an app has no `app` rows) and gets the same shape back
// for what it did fill — which is how the three surfaces show one menu.
export interface FolderMenuGroups {
  app?: MenuEntry[];
  create?: MenuEntry[];
  folder?: MenuEntry[];
  open?: MenuEntry[];
  copy?: MenuEntry[];
  embed?: MenuEntry[];
}

const FOLDER_GROUP_ORDER: (keyof FolderMenuGroups)[] = [
  "app",
  "create",
  "folder",
  "open",
  "copy",
  "embed",
];

// Groups → one flat list, a separator between consecutive NON-EMPTY groups and
// never at either end. Shared by the folder and file builders so the two menus
// cannot drift in how they draw a divider.
function groupedMenu<G extends string>(
  order: readonly G[],
  groups: Partial<Record<G, MenuEntry[]>>,
): MenuEntry[] {
  const out: MenuEntry[] = [];
  for (const key of order) {
    const rows = groups[key];
    if (!rows || rows.length === 0) continue;
    if (out.length) out.push("separator");
    out.push(...rows);
  }
  return out;
}

export function folderMenu(groups: FolderMenuGroups): MenuEntry[] {
  return groupedMenu(FOLDER_GROUP_ORDER, groups);
}

export interface CrumbActions {
  onReveal: () => void;
  onOpenInNewTab: () => void;
}

// Right-click on an ANCESTOR crumb in the path strip — a folder you are not in.
// Deliberately just two items: the only things that make sense on a folder you
// are pointing at rather than standing in are "open it elsewhere" and "hand it
// to the OS". A crumb is a navigation handle, not a row you selected, so the
// editing verbs (Rename/Cut/Paste/Delete) have no business here — they belong
// on the listing rows, which do carry the full menu (useFileOps.rowMenu).
// What this must NOT be is the bar's own folder menu — that list acts on
// the CURRENT directory (New File, Paste, Refresh), so on an ancestor crumb it
// answered about the wrong folder entirely, which is the bug this fixes.
//
// The current folder's crumb keeps the bar menu: there the two are the same
// folder, and the full list is right.
export function crumbMenu(actions: CrumbActions): MenuEntry[] {
  return [
    { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: actions.onReveal },
    { label: "Open in New Tab", icon: MenuIcons.newTab, onClick: actions.onOpenInNewTab },
  ];
}

// The file menu's GROUPS, in the order they are shown — the folder menu's
// groups without `create`, so a file and its folder read as one menu family:
//
//   app   what the file's folder IS, when the file is its entry page: Share…,
//         Open as project, MCP config, App Doctor, and Set Current View as
//         Preview — the one row that photographs the app rather than acting on
//         the file. First for the folder menu's reason: it carries the status
//         dot, and it is absent on a plain file.
//   file  the file itself: Rename…. Deliberately NOT the preview header's full
//         Finder menu (Preview's buildMenu): no Open With (the mode control is
//         two inches away in the same bar), no Bin/Duplicate/Cut/Copy — a top
//         bar is not where a file gets destroyed.
//   open  the same file somewhere else: Reveal in Finder, Open in New Tab,
//         Split right, Split down — the folder menu's `open` row for row, so
//         the shared pair never swaps places between the two bars (they are
//         one surface to the user).
//   copy  text to the clipboard: Copy Path, Copy Claude session command.
//   embed Open in embed, last and alone — the folder menu's reason.
//
// Each surface fills what it may offer (a pane cannot split; a directory
// previewed in a non-listing mode has no file rows at all) and gets the same
// shape back for what it did fill.
export interface FileMenuGroups {
  app?: MenuEntry[];
  file?: MenuEntry[];
  open?: MenuEntry[];
  copy?: MenuEntry[];
  embed?: MenuEntry[];
}

const FILE_GROUP_ORDER: (keyof FileMenuGroups)[] = ["app", "file", "open", "copy", "embed"];

export function fileMenu(groups: FileMenuGroups): MenuEntry[] {
  return groupedMenu(FILE_GROUP_ORDER, groups);
}
