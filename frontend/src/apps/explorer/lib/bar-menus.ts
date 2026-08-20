// The item lists for the CRUMB BAR's right-click menu, in the two states the
// bar has: over a folder (the listing owns it) and over a single file.
//
// Plain builders taking their actions as callbacks, for the reason every other
// menu in the explorer is built this way (listing/useFileOps, lib/fs-actions'
// buildOpenWithItems): there are no React component tests in this repo, so a
// menu's shape is only checkable when the list is a function of its inputs
// rather than JSX inline in a handler. bar-menus.test.ts is the whole argument.
//
// The FOLDER list is not written out here at all — it is the listing's own
// background menu plus the splits, and the background menu is
// useFileOps.backgroundMenu(). Duplicating those seven items so the bar could
// have its own copy is exactly the drift the header `⋮` was consolidated to
// stop (see Listing's openHeaderMenu), so `folderBarMenu` takes them.
import { createElement } from "react";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { SplitDownIcon, SplitRightIcon } from "@platform/ui/SplitIcons";

export type SplitDir = "row" | "col";

// The two split-entry rows, with the same glyphs the panel bar uses. One
// definition, three callers (the bar's two menus and the listing's header `⋮`
// through folderBarMenu), because "Split right" that means `row` in one menu
// and `col` in another is the kind of bug nobody re-checks.
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

// Right-click on the bar over a FOLDER: the middle panel's header `⋮` menu,
// item for item — the folder's own actions (`background`), then the splits.
// The header button and the bar's right-click are two surfaces on one list.
export function folderBarMenu(
  background: MenuEntry[],
  onSplit: (dir: SplitDir) => void
): MenuEntry[] {
  return [...background, "separator", ...splitItems(onSplit)];
}

export interface FileBarActions {
  onRename: () => void;
  onOpenInClaude: () => void;
  onCopyPath: () => void;
  onReveal: () => void;
  // Omitted where the surface cannot split (an embedded pane already IS a
  // split, and a directory's preview has no file to split on) — the separator
  // goes with it, so the menu never ends in a divider.
  onSplit?: (dir: SplitDir) => void;
}

// Right-click on the bar over a single open FILE. A short list on purpose: it
// replaces the path `⋮` (whose two items are the middle pair here) and adds the
// three things the bar was otherwise silent about — renaming the file you are
// looking at, copying the command that starts a Claude session there, and the
// splits that used to be naked
// glyphs at the far right of the window.
//
// Deliberately NOT the preview header's full file menu (Preview's buildMenu):
// no Open With (the mode control is two inches away in this same bar), no
// Bin/Duplicate/Cut/Copy — a top bar is not where a file gets destroyed.
export function fileBarMenu(actions: FileBarActions): MenuEntry[] {
  // Reveal → Copy Path → Copy Claude session command, in exactly the folder
  // menu's
  // order (useFileOps.backgroundMenu) — the two bars are one surface to the
  // user, and the shared trio must not swap places between them.
  return [
    { label: "Rename…", icon: MenuIcons.rename, onClick: actions.onRename },
    "separator",
    { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: actions.onReveal },
    { label: "Copy Path", icon: MenuIcons.copyPath, onClick: actions.onCopyPath },
    {
      label: "Copy Claude session command",
      icon: MenuIcons.openWith,
      onClick: actions.onOpenInClaude,
    },
    ...(actions.onSplit ? (["separator", ...splitItems(actions.onSplit)] as MenuEntry[]) : []),
  ];
}
