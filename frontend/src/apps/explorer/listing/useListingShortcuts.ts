// Keyboard shortcuts scoped to the listing: file operations on the selection
// plus the folder-level navigation chords. Registered once (empty deps); the
// handler is re-assigned each render so it always reads fresh state/closures.
// Separate from the nav handler (useListingSelection), and non-overlapping
// with it by construction: everything here carries the primary modifier or is
// a key that handler ignores (F2, Delete, Backspace — its printable-key branch
// only fires for single-character keys, and its arrow branch bails when
// isMod(e)).
import { useEffect, useRef } from "react";
import { navigate } from "@platform/lib/router";
import { isMac, isMod } from "@platform/lib/platform";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";
import { setClipboard, type Clipboard } from "@apps/explorer/lib/fs-clipboard";
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
    const mod = isMod(e);
    const key = e.key.toLowerCase();
    // The parent folder, for Mod+Up / bare Backspace. Equal to the current
    // folder at the filesystem (or drive) root, where there's nowhere to go.
    const here = normDir(base);
    const parent = dirname(here);
    const goParent = () => {
      if (parent !== here) navigate(parent, { isDir: true });
    };
    // With focus in the search box, Cmd+C/X/V must keep their native text
    // clipboard meaning — only the non-text shortcuts stay live there.
    if (inSearch && mod && (key === "c" || key === "x" || key === "v")) return;
    if (mod && key === "c") {
      if (!rows.length) return;
      e.preventDefault();
      setClipboard({ paths: rows.map((r) => r.path), op: "copy" });
    } else if (mod && key === "x") {
      if (!rows.length) return;
      e.preventDefault();
      setClipboard({ paths: rows.map((r) => r.path), op: "cut" });
    } else if (mod && key === "v") {
      if (!clipboard) return;
      e.preventDefault();
      // Paste is single-TARGET: into the lead row's folder (or itself, if it's a
      // directory), else the folder being listed.
      doPaste(row ? targetDirOf(row) : base);
    } else if (mod && key === "d") {
      if (!rows.length) return;
      e.preventDefault();
      doDuplicate(rows);
    } else if (mod && e.key === "ArrowDown") {
      // Open the lead row — the same gesture as Enter (macOS Cmd+Down).
      if (!row) return;
      e.preventDefault();
      navigate(row.path, { isDir: row.isDir });
    } else if (mod && e.key === "ArrowUp") {
      e.preventDefault();
      goParent();
    } else if (mod && (e.key === "[" || e.key === "]")) {
      // Back / forward. The router only ever pushes, so this drives the browser
      // history directly — popstate is what the shell listens to anyway
      // (useNavEpoch), so the view remounts exactly as it does for a Back click.
      e.preventDefault();
      if (e.key === "[") history.back();
      else history.forward();
    } else if (mod && e.shiftKey && key === "n") {
      e.preventDefault();
      startNewFolder(base);
    } else if (mod && e.key === "Backspace") {
      // macOS trash chord. Windows/Linux use the Delete key below instead.
      // Never while typing in the search box: Cmd+Delete is the standard macOS
      // "clear to start of line" chord, so trashing there would be a foot-gun.
      if (!isMac || inSearch || !rows.length) return;
      e.preventDefault();
      doTrash(rows);
      // Bare key only: because isMod() is exclusive, `!mod` alone is still true
      // when the OTHER modifier is held (Super+Backspace on Linux), so test the
      // raw flags instead.
    } else if (e.key === "Backspace" && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
      // Windows/Linux: bare Backspace goes up a folder. On macOS it must stay
      // inert — Cmd+Backspace is trash there and a bare Backspace navigating
      // away would be a foot-gun. Never while typing in the search box.
      if (isMac || inSearch) return;
      e.preventDefault();
      goParent();
    } else if (e.key === "Delete" && !e.metaKey && !e.ctrlKey) {
      // Windows/Linux trash key. On macOS the Delete (⌦) key is not the trash
      // gesture — Cmd+Backspace above is. Raw-flag test rather than `!mod` so a
      // held Super key can't slip a destructive key through (see Backspace).
      if (isMac || inSearch || !rows.length) return;
      e.preventDefault();
      doTrash(rows);
    } else if (e.key === "F2") {
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
