// THE ROW CATALOGUE — every verb the explorer offers on an entry, spelled ONCE.
//
// A row is a label, a glyph and a click. Before this file the same Rename…
// was typed out in four places (useFileOps.folderGroups, useFileOps.rowMenu,
// usePreviewFileMenu.fileGroups, usePreviewFileMenu.buildMenu) and the copy
// row had drifted to two spellings ("Copy path" on the folder menu, "Copy
// Path" everywhere else). Each factory below is the one place its label and
// icon live; the menus in lib/bar-menus.ts arrange them, the two action hooks
// (listing/useFileOps, Preview's usePreviewFileMenu) supply the click.
//
// PURE FUNCTIONS TAKING CALLBACKS, not paths, for three reasons:
//   - The two hooks do different things AFTER the same verb (the listing
//     refetches and re-anchors its selection, the preview navigates), and
//     their error toasts name the entry differently. The row does not care;
//     it takes whatever the hook hands it.
//   - Nothing here closes over state at module scope, so a menu that is
//     rebuilt per open (Paste's enabled flag tracks the clipboard, Rename
//     targets the row under the pointer) stays correct by construction.
//   - No import from fs-actions or the router, so entry-rows.test.ts needs no
//     `location` stub and the catalogue is checkable as plain data.
//
// What is NOT here, on purpose: the multi-select rows ("Delete 3 items" —
// labels with counts), the folder's Share…/Export App File pair (a flag
// switch already written once in useFileOps), the file's shareRow (already a
// factory in share-file.ts), the app rows (EntryActionsMenu's hook), and the
// split pair (bar-menus' splitItems).
import type { MenuEntry, MenuItem } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";

// Open the entry in place — the listing row's first verb. Absent from every
// bar menu (the bar is already showing it) and from the pane header.
export function openRow(onOpen: () => void): MenuItem {
  return { label: "Open", icon: MenuIcons.open, onClick: onOpen };
}

export function openInNewTabRow(onOpen: () => void): MenuItem {
  return { label: "Open in New Tab", icon: MenuIcons.newTab, onClick: onOpen };
}

// A lazy submenu: the loader resolves the view list on hover, not on menu
// open (fs-actions' buildOpenWithItems is the usual loader).
export function openWithRow(loader: () => Promise<MenuEntry[]>): MenuItem {
  return { label: "Open With", icon: MenuIcons.openWith, submenu: loader };
}

export function deleteRow(onDelete: () => void): MenuItem {
  return { label: "Delete", icon: MenuIcons.trash, onClick: onDelete };
}

export function renameRow(onRename: () => void): MenuItem {
  return { label: "Rename…", icon: MenuIcons.rename, onClick: onRename };
}

export function duplicateRow(onDuplicate: () => void): MenuItem {
  return { label: "Duplicate", icon: MenuIcons.duplicate, onClick: onDuplicate };
}

// Folders only, a lazy submenu of archive formats (fs-actions'
// buildCompressItems through the hook's loader).
export function compressRow(loader: () => Promise<MenuEntry[]>): MenuItem {
  return { label: "Compress", icon: MenuIcons.compress, submenu: loader };
}

export function cutRow(onCut: () => void): MenuItem {
  return { label: "Cut", icon: MenuIcons.cut, onClick: onCut };
}

export function copyRow(onCopy: () => void): MenuItem {
  return { label: "Copy", icon: MenuIcons.copy, onClick: onCopy };
}

// Listed-but-dead when there is nothing to paste: a disabled row, never a
// missing one (bar-menus.test.ts pins the folder menu on exactly this).
export function pasteRow(enabled: boolean, onPaste: () => void): MenuItem {
  return { label: "Paste", icon: MenuIcons.paste, disabled: !enabled, onClick: onPaste };
}

export function copyPathRow(onCopy: () => void): MenuItem {
  return { label: "Copy Path", icon: MenuIcons.copyPath, onClick: onCopy };
}

export function revealRow(onReveal: () => void): MenuItem {
  return { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: onReveal };
}

// Copies the command that starts Claude Code on this entry's folder — a
// clipboard hand-off, not a launch (fs-actions' claudeTerminalCommand). Shares
// Open With's glyph, as it always has.
export function claudeCommandRow(onCopy: () => void): MenuItem {
  return { label: "Copy Claude session command", icon: MenuIcons.openWith, onClick: onCopy };
}

export function refreshRow(onRefresh: () => void): MenuItem {
  return { label: "Refresh", icon: MenuIcons.refresh, onClick: onRefresh };
}

export function newFolderRow(onNew: () => void): MenuItem {
  return { label: "New Folder…", icon: MenuIcons.newFolder, onClick: onNew };
}

export function newFileRow(onNew: () => void): MenuItem {
  return { label: "New File…", icon: MenuIcons.newFile, onClick: onNew };
}

// Photograph what the frame is showing and write it as the app's preview.png.
// Only ever offered on an app's entry page (usePreviewFileMenu gates it on
// `isEntry`); the confirm-before-overwrite flow is the callback's.
export function setPreviewRow(onShoot: () => void): MenuItem {
  return { label: "Set Current View as Preview", icon: MenuIcons.camera, onClick: onShoot };
}

// Every factory, built with inert arguments — the test's inventory, so a new
// factory that is added above but not here fails the "labels are unique"
// argument the moment someone asks it. Kept here rather than in the test so
// the catalogue itself says what it contains.
export function allRows(): MenuItem[] {
  const noop = () => {};
  const none = () => Promise.resolve([] as MenuEntry[]);
  return [
    openRow(noop),
    openInNewTabRow(noop),
    openWithRow(none),
    deleteRow(noop),
    renameRow(noop),
    duplicateRow(noop),
    compressRow(none),
    cutRow(noop),
    copyRow(noop),
    pasteRow(true, noop),
    copyPathRow(noop),
    revealRow(noop),
    claudeCommandRow(noop),
    refreshRow(noop),
    newFolderRow(noop),
    newFileRow(noop),
    setPreviewRow(noop),
  ];
}
