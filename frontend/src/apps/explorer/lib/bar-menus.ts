// The GROUPED menu builders for an entry in the explorer — one for the bars,
// one for the Finder-style right-clicks — plus the ancestor-crumb menu, the
// split pair and the folder-rename guard.
//
// Plain builders taking their rows as input, for the reason every other menu
// in the explorer is built this way (listing/useFileOps, lib/fs-actions'
// buildOpenWithItems): there are no React component tests in this repo, so a
// menu's shape is only checkable when the list is a function of its inputs
// rather than JSX inline in a handler. bar-menus.test.ts is the whole argument.
//
// THE ENTRY MENU IS ONE LIST ON FIVE SURFACES — over a folder: the listing's
// kebab (`⋮` in the search row), a right-click on the listing's empty
// background, a right-click on the crumb bar; over a file: the preview's
// kebab (`⋮` after the mode control) and a right-click on the crumb bar. They
// used to be five lists, then two builders (`folderMenu`, `fileMenu`) that
// were the same loop over two group interfaces differing in one key.
// `entryMenu` below is the ONE builder: every surface hands it the same
// grouped input and shows the same rows in the same order. The rows themselves
// are not written out here — they come from lib/entry-rows (label + glyph,
// once); the folder ops' clicks from useFileOps, the file ops' from Preview's
// usePreviewFileMenu, the app rows from EntryActionsMenu's hook — so no
// surface can grow a private copy.
//
// THE FINDER MENU IS THE SAME ARRANGEMENT ON TWO SURFACES — a right-click on a
// listing row and on a pane preview's header. These carry the destructive
// verbs (Delete, Cut) the bar menus deliberately do not: a top bar is not
// where a file gets destroyed. They used to be two inline arrays that spelled
// the Finder rows twice with slightly different sets; `finderMenu` is their
// one builder. The pane header fills fewer groups than the row (no Open, no
// Paste, no Compress, no Claude command — owner, 2026-09-22: keep as is) and
// gets the same separator structure back for what it did fill.
import { createElement } from "react";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { SplitDownIcon, SplitRightIcon } from "@platform/ui/SplitIcons";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";
import { openInNewTabRow, revealRow } from "@apps/explorer/lib/entry-rows";

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
// definition for every menu's `open` group, because "Split right" that means
// `row` in one menu and `col` in another is the kind of bug nobody re-checks.
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

// The entry menu's GROUPS, in the order they are shown. A group is a run of
// rows with a separator either side; an empty or absent group draws nothing,
// not a stray rule.
//
//   app      what this entry IS, when it is an app (or an app's entry page):
//            Share…, Open as project, MCP config, App Doctor, and on the entry
//            page Set Current View as Preview — the one row that photographs
//            the app rather than acting on the file. First because it is the
//            reason the kebab carries a status dot, and absent on a plain
//            folder or file.
//   create   things that put something new INTO this folder: New Folder…,
//            New File…, Paste. The verbs a hand reaches for a background menu
//            for, so they lead once the app rows are out of the way. Always
//            empty for a file (there is nothing to put into one).
//   subject  the entry itself: Rename…, and for a folder Refresh. Deliberately
//            NOT the Finder menu's Bin/Duplicate/Cut/Copy — a top bar is not
//            where a file gets destroyed.
//   open     the same entry somewhere else: Reveal in Finder, Open in New Tab,
//            Split right, Split down. One group because they all answer "show
//            me this elsewhere"; the splits are not a special case of anything,
//            just two more elsewheres.
//   share    Share… for a FILE itself (share-any-file-plan.md task 7) — its
//            own group so it reads as one decision, not folded into `open` or
//            `copy`. A folder's Share… is the app sheet, in `app`; only a file
//            ever fills this one.
//   copy     text to the clipboard: Copy Path, Copy Claude session command.
//   embed    Open in embed, LAST and alone (owner, 2026-09-20: "move open in
//            embed to the bottom"): it leaves the explorer for the chrome-free
//            view, so it closes the list rather than sitting among the rows
//            that keep you here.
//
// Each surface fills what it may offer (a panel pane cannot split or embed; a
// folder that is not an app has no `app` rows; a file has no `create`) and
// gets the same shape back for what it did fill — which is how five surfaces
// show one menu.
export interface EntryMenuGroups {
  app?: MenuEntry[];
  create?: MenuEntry[];
  subject?: MenuEntry[];
  open?: MenuEntry[];
  share?: MenuEntry[];
  copy?: MenuEntry[];
  embed?: MenuEntry[];
}

const ENTRY_GROUP_ORDER: (keyof EntryMenuGroups)[] = [
  "app",
  "create",
  "subject",
  "open",
  "share",
  "copy",
  "embed",
];

// Groups → one flat list, a separator between consecutive NON-EMPTY groups and
// never at either end. Shared by both builders so the two menu families
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

export function entryMenu(groups: EntryMenuGroups): MenuEntry[] {
  return groupedMenu(ENTRY_GROUP_ORDER, groups);
}

// The Finder menu's GROUPS, macOS Finder order — the structure both the
// listing row's and the pane header's right-click already had, now named:
//
//   open    Open, Open in New Tab, Open With. The pane header fills only Open
//           With (it is already showing the file).
//   delete  Delete, alone between rules, as Finder sets Move to Bin.
//   edit    Rename…, Duplicate, then folders only: Compress, Share…/Export App
//           File.
//   clip    Cut, Copy, Paste. The pane header has no Paste (nothing to paste
//           INTO from a single file).
//   copy    Copy Path, Reveal in Finder, Copy Claude session command.
export interface FinderMenuGroups {
  open?: MenuEntry[];
  delete?: MenuEntry[];
  edit?: MenuEntry[];
  clip?: MenuEntry[];
  copy?: MenuEntry[];
}

const FINDER_GROUP_ORDER: (keyof FinderMenuGroups)[] = ["open", "delete", "edit", "clip", "copy"];

export function finderMenu(groups: FinderMenuGroups): MenuEntry[] {
  return groupedMenu(FINDER_GROUP_ORDER, groups);
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
// What this must NOT be is the bar's own entry menu — that list acts on
// the CURRENT directory (New File, Paste, Refresh), so on an ancestor crumb it
// answered about the wrong folder entirely, which is the bug this fixes.
//
// The current folder's crumb keeps the bar menu: there the two are the same
// folder, and the full list is right.
export function crumbMenu(actions: CrumbActions): MenuEntry[] {
  return [revealRow(actions.onReveal), openInNewTabRow(actions.onOpenInNewTab)];
}
