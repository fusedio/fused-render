# Explorer top bar context menus, breadcrumb hitboxes, and click-to-deselect

Date: 2026-08-12
Status: approved (Akshil, in-session)

## 1. Top-bar right-click menus

Right-clicking anywhere on the `#breadcrumb` top bar (bookmark star, path crumbs,
empty space, search area) opens the shared `ContextMenu` (same component the
listing rows use), positioned at the cursor.

**Directory view:** menu items are identical to the middle-panel kebab (header
menu) — reuse the `backgroundMenu()` builder from `useFileOps` plus the split
entries: New Folder…, New File…, Paste, Refresh, Reveal in Finder, Copy path,
Open in Claude Code, separator, Split right, Split down. The kebab button stays.

**File view** (a single file like .png/.html is open): menu is
Rename…, Open in Claude Code, separator, Copy Path, Reveal in Finder,
separator, Split right, Split down. Rename reuses the existing rename flow,
applied to the open file. The `PathOverflow` ellipsis kebab is removed in file
mode (its items now live in the right-click menu).

**Exceptions:** right-click on the path-edit `<input>` or the search input keeps
the native browser menu (copy/paste). Everywhere else on the bar shows the
custom menu.

## 2. Breadcrumb hitboxes

- Path segment links (`.path-crumb`) get a full-height clickable area (the
  48px bar), not the current ~2px-padded text box. The hover pill keeps its
  current visual size — grow the hitbox via padding compensated by negative
  margin (or an equivalent technique), not by growing the visible pill.
- Clicking the empty space between the crumbs and the search box enters path
  edit mode (today only clicks inside the narrow `.crumbs` strip do).
  Bookmark star, kebab/overflow buttons, and the search box are excluded.

## 3. Click-to-deselect in the listing

- Left-click on empty listing background clears the current selection.
- Left-click on the header row (Name / Size / Modified) also clears the
  selection, in addition to any existing header behavior (e.g. sorting).

## Implementation notes

- Key files: `frontend/src/apps/explorer/Breadcrumb.tsx` (bar, crumbs, edit
  mode, PathOverflow slot), `frontend/src/apps/explorer/BarMenu.tsx`
  (PathOverflow builder), `frontend/src/styles/explorer.css` (bar/crumb
  hitboxes), `frontend/src/apps/explorer/Listing.tsx` (selection, header/
  background menus), `frontend/src/apps/explorer/useFileOps.ts` (menu
  builders), `frontend/src/apps/explorer/Preview.tsx` (file-mode context).
- Menu-item construction should stay in plain testable builders (follow the
  `useFileOps` pattern); add/extend unit tests where builders change.
- Right-click while path edit mode is active: native input menu wins.
