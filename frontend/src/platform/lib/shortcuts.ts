// The single source of truth for the keyboard cheat sheet (ShortcutsOverlay).
//
// DATA ONLY — nothing here listens for or handles an event. The handlers live
// with the components that own the behaviour (Listing, Breadcrumb, App); this
// module exists so the documented list can't drift into a second hand-written
// copy inside the overlay's JSX.
//
// `keys` are already display-formatted glyphs (["⌘", "C"] on macOS,
// ["Ctrl", "C"] elsewhere) via the *_LABEL constants in lib/platform. How those
// glyphs are joined — ⌘C on macOS vs Ctrl+C on Windows/Linux — is a rendering
// decision and stays in the overlay, not here.
import {
  BACKSPACE_LABEL,
  DELETE_LABEL,
  ENTER_LABEL,
  MOD_LABEL,
  SHIFT_LABEL,
  isMac,
} from "@platform/lib/platform";

export type Shortcut = { keys: string[]; label: string };
export type ShortcutGroup = { title: string; items: Shortcut[] };

// Group titles in render order. Adding a group means adding it here plus one
// entry line below.
const GROUP_ORDER = ["Navigation", "Selection", "File operations", "View"] as const;
type GroupTitle = (typeof GROUP_ORDER)[number];

type Entry = Shortcut & {
  group: GroupTitle;
  // Platform gate: omit an entry where the chord doesn't exist on that OS
  // (bare Backspace = parent folder only off macOS, where ⌘⌫ means trash).
  only?: "mac" | "other";
};

export function shortcutGroups(): ShortcutGroup[] {
  // One flat declarative list — adding a shortcut is a one-line change.
  const entries: Entry[] = [
    // ---- Navigation ------------------------------------------------------
    { group: "Navigation", keys: ["↑"], label: "Move selection up" },
    { group: "Navigation", keys: ["↓"], label: "Move selection down" },
    { group: "Navigation", keys: ["Home"], label: "Go to first item" },
    { group: "Navigation", keys: ["End"], label: "Go to last item" },
    { group: "Navigation", keys: ["PageUp"], label: "Move selection up a page" },
    { group: "Navigation", keys: ["PageDown"], label: "Move selection down a page" },
    { group: "Navigation", keys: [ENTER_LABEL], label: "Open selected" },
    // The mouse half of the one click model (listing/selection rowClickAction):
    // a single click only ever selects, so the sheet has to say what opens.
    { group: "Navigation", keys: ["Double-click"], label: "Open" },
    { group: "Navigation", keys: [MOD_LABEL, "↓"], label: "Open selected" },
    { group: "Navigation", keys: [MOD_LABEL, "↑"], label: "Go to parent folder" },
    { group: "Navigation", keys: [MOD_LABEL, "["], label: "Back in history" },
    { group: "Navigation", keys: [MOD_LABEL, "]"], label: "Forward in history" },
    { group: "Navigation", keys: [MOD_LABEL, "L"], label: "Edit path" },
    {
      group: "Navigation",
      keys: [BACKSPACE_LABEL],
      label: "Go to parent folder",
      only: "other",
    },
    { group: "Navigation", keys: ["A–Z"], label: "Type any letter to jump to search" },

    // ---- Selection -------------------------------------------------------
    { group: "Selection", keys: [MOD_LABEL, "A"], label: "Select all" },
    { group: "Selection", keys: [SHIFT_LABEL, "↑"], label: "Extend selection up" },
    { group: "Selection", keys: [SHIFT_LABEL, "↓"], label: "Extend selection down" },
    { group: "Selection", keys: [SHIFT_LABEL, "Home"], label: "Extend selection to first item" },
    { group: "Selection", keys: [SHIFT_LABEL, "End"], label: "Extend selection to last item" },
    { group: "Selection", keys: [SHIFT_LABEL, "PageUp"], label: "Extend selection up a page" },
    { group: "Selection", keys: [SHIFT_LABEL, "PageDown"], label: "Extend selection down a page" },
    { group: "Selection", keys: ["Click"], label: "Select" },
    { group: "Selection", keys: [SHIFT_LABEL, "Click"], label: "Select range" },
    { group: "Selection", keys: [MOD_LABEL, "Click"], label: "Add or remove from selection" },
    // Escape is ranked: App's capture-phase handler cancels a pending copy/cut
    // first (and only then does Listing's branch clear the selection), so the
    // two entries are listed with that precedence spelled out rather than as a
    // single "Clear selection" that silently loses to the clipboard.
    { group: "Selection", keys: ["Esc"], label: "Clear selection (once nothing is on the clipboard)" },

    // ---- File operations -------------------------------------------------
    { group: "File operations", keys: [MOD_LABEL, "C"], label: "Copy" },
    { group: "File operations", keys: [MOD_LABEL, "X"], label: "Cut" },
    { group: "File operations", keys: [MOD_LABEL, "V"], label: "Paste" },
    { group: "File operations", keys: ["Esc"], label: "Cancel a pending copy or cut" },
    { group: "File operations", keys: [MOD_LABEL, "D"], label: "Duplicate" },
    { group: "File operations", keys: ["F2"], label: "Rename" },
    {
      group: "File operations",
      keys: [MOD_LABEL, BACKSPACE_LABEL],
      label: "Move to trash",
      only: "mac",
    },
    {
      group: "File operations",
      keys: [DELETE_LABEL],
      label: "Move to trash",
      only: "other",
    },
    { group: "File operations", keys: [SHIFT_LABEL, MOD_LABEL, "N"], label: "New folder" },
    // NOTE: no "Show info" entry. The shell has no info/stat surface to open
    // (nothing but the listing's own Size/Modified columns), so Mod+I is
    // deliberately unbound — listing it here would document a dead key.

    // ---- View ------------------------------------------------------------
    { group: "View", keys: [MOD_LABEL, "R"], label: "Refresh" },
    { group: "View", keys: [MOD_LABEL, "K"], label: "Show this shortcut list" },
    { group: "View", keys: ["Esc"], label: "Close overlay" },
  ];

  const platform = isMac ? "mac" : "other";
  return GROUP_ORDER.map((title) => ({
    title,
    items: entries
      .filter((e) => e.group === title && (e.only === undefined || e.only === platform))
      .map(({ keys, label }) => ({ keys, label })),
  })).filter((g) => g.items.length > 0);
}
