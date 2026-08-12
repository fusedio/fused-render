// Keyboard shortcuts scoped to the listing: file operations on the selection
// plus the folder-level navigation chords. Registered once (empty deps); the
// handler is re-assigned each render so it always reads fresh state/closures.
// Separate from the nav handler (useListingSelection), and non-overlapping
// with it by construction: everything here carries the primary modifier or is
// a key that handler ignores (F2, Delete, Backspace — its printable-key branch
// only fires for single-character keys, and its arrow branch bails when
// isMod(e)).
//
// WHICH chord is which lives in listing/shortcut-chord.ts (pure, tested); this
// file is the wiring and the "is there anything to act on" half. The split is
// what makes the exact-modifier rule testable — see that file for the
// Ctrl+Shift+C hijack it exists to prevent.
import { useEffect, useRef } from "react";
import { navigate } from "@platform/lib/router";
import { matchChord } from "@apps/explorer/listing/shortcut-chord";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";
import { setClipboard, type Clipboard } from "@apps/explorer/lib/fs-clipboard";
import { canRedo, canUndo } from "@apps/explorer/lib/fs-undo";
import { cameFromSelParam } from "@apps/explorer/listing/selection";
import type { RowCtx } from "@apps/explorer/listing/types";
import { targetDirOf } from "@apps/explorer/listing/row-utils";

export function useListingShortcuts({
  base,
  clipboard,
  selectedRows,
  leadRow,
  searchInputRef,
  overlayOpenRef,
  doPaste,
  doUndo,
  doRedo,
  doDuplicate,
  doTrash,
  startRename,
  startNewFolder,
  globalKeys = true,
}: {
  base: string;
  clipboard: Clipboard | null;
  // The selection as full rows, in rendered order; every file operation acts on
  // the whole selection.
  selectedRows: RowCtx[];
  // The lead row, for the single-entry operations (Rename, paste target).
  leadRow: RowCtx | undefined;
  searchInputRef: React.RefObject<HTMLInputElement>;
  overlayOpenRef: React.MutableRefObject<boolean>;
  doPaste: (dir: string) => void;
  doUndo: () => void;
  doRedo: () => void;
  doDuplicate: (rows: RowCtx[]) => void;
  doTrash: (rows: RowCtx[]) => void;
  startRename: (row: RowCtx) => void;
  startNewFolder: (dir: string) => void;
  // False for an embedded Listing (preview pane): the document-level file-op
  // chords belong to the host view's Listing.
  globalKeys?: boolean;
}) {
  const shortcutRef = useRef<(e: KeyboardEvent) => void>(() => {});
  shortcutRef.current = (e: KeyboardEvent) => {
    if (e.isComposing) return;
    // Same hard guard as the nav handler: while a context menu or dialog is
    // open (in this view OR a hosting one, e.g. Preview's header menu with this
    // Listing embedded), file-op shortcuts (Cmd+Backspace trash, Cmd+X cut, …)
    // must not fire on the row behind it.
    if (overlayOpenRef.current || isOverlayOpen()) return;
    const el = document.activeElement as HTMLElement | null;
    const inSearch = el === searchInputRef.current;
    const navActive = inSearch || !el || el === document.body || el === document.documentElement;
    if (!navActive) return;
    const rows = selectedRows;
    const row = leadRow;
    // WHICH chord this is — decided in listing/shortcut-chord.ts, on EXACT
    // modifiers. Everything below is only "can I act on it": a chord that
    // matches but has nothing to act on (no selection, an empty clipboard)
    // falls through WITHOUT preventDefault, exactly as an unmatched one does,
    // so the browser keeps whatever binding it had.
    // Asked at event time, never cached: a selection is made and dropped by the
    // same clicks that reach this handler. `toString()` rather than `isCollapsed`
    // because a collapsed range inside a shadow/contenteditable can still report
    // a non-null selection with nothing in it.
    const hasSelection = !!(window.getSelection()?.toString() || "").trim();
    const action = matchChord(e, { inSearch, hasSelection });
    if (action === null) return;
    // The parent folder, for Mod+Up / bare Backspace. Equal to the current
    // folder at the filesystem (or drive) root, where there's nowhere to go.
    const here = normDir(base);
    const parent = dirname(here);
    if (action === "copy" || action === "cut") {
      if (!rows.length) return;
      e.preventDefault();
      setClipboard({ paths: rows.map((r) => r.path), op: action });
    } else if (action === "paste") {
      if (!clipboard) return;
      e.preventDefault();
      // Paste is single-TARGET: into the lead row's folder (or itself, if it's a
      // directory), else the folder being listed.
      doPaste(row ? targetDirOf(row) : base);
    } else if (action === "duplicate") {
      if (!rows.length) return;
      e.preventDefault();
      doDuplicate(rows);
    } else if (action === "open") {
      if (!row) return;
      e.preventDefault();
      navigate(row.path, { isDir: row.isDir });
    } else if (action === "parent") {
      e.preventDefault();
      // Land on the folder we came out of, highlighted — the same rule the
      // crumb strip follows (Breadcrumb.tsx), through the same pure decision.
      if (parent !== here) navigate(parent, { isDir: true, sel: cameFromSelParam(parent, here) });
    } else if (action === "back" || action === "forward") {
      // The router only ever pushes, so this drives the browser history
      // directly — popstate is what the shell listens to anyway (useNavEpoch),
      // so the view remounts exactly as it does for a Back click.
      e.preventDefault();
      if (action === "back") history.back();
      else history.forward();
    } else if (action === "new-folder") {
      e.preventDefault();
      startNewFolder(base);
    } else if (action === "trash") {
      if (!rows.length) return;
      e.preventDefault();
      doTrash(rows);
    } else if (action === "undo" || action === "redo") {
      // Nothing on the stack means the chord was never ours: falling through
      // WITHOUT preventDefault leaves Cmd+Z to whatever else would have taken
      // it, which is the same courtesy an empty clipboard gets from paste.
      if (action === "undo" ? !canUndo() : !canRedo()) return;
      e.preventDefault();
      if (action === "undo") doUndo();
      else doRedo();
    } else if (action === "rename") {
      // Rename is single-entry: with several rows selected it renames the LEAD
      // row (what Windows Explorer does — F2 edits the focused item), not a
      // no-op and never a batch rename.
      if (!row) return;
      e.preventDefault();
      startRename(row);
    }
  };
  useEffect(() => {
    if (!globalKeys) return;
    const h = (e: KeyboardEvent) => shortcutRef.current(e);
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [globalKeys]);
}
