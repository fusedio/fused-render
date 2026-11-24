// WHETHER THE USER HAS SHUT THE CLAUDE COLUMN — one boolean for every page
// that has one (owner, 2026-11-03: "if the sidebar is made global the
// collapsed and opened state of it should persist across views as well"):
// the explorer's companion sidebar (apps/explorer/lib/side-hidden-store.ts,
// where this bit was born and whose comment has the history), the canvas
// page's right pane (CanvasWorkspace `sideOpenFromUrl`) and the app page's
// (shell/useAppPageClaudePane). Shutting it on one page shuts it on the next;
// opening it anywhere opens it everywhere. Kept in the server-held layout
// document (`explorerSide.hidden`, ui-layout.ts, D1386), read synchronously
// off that module's cache, which main.tsx fills before the first render.
//
// A LOSING SIGNAL, not a command: an explicit `side=off` / `_side` in the URL
// — a deep link, a carried-in link — always wins; this only answers "the URL
// is silent — did the user shut it?". In `platform/` so that an app
// (apps/canvases) and the shell can read it without crossing into the
// explorer (scripts/check-boundaries.mjs).
import { getUiLayout, patchUiLayout } from "./ui-layout";

export function getSideHidden(): boolean {
  return getUiLayout().explorerSide?.hidden === true;
}

export function setSideHidden(next: boolean): void {
  if (getSideHidden() === next) return;
  patchUiLayout((d) => ({ ...d, explorerSide: { ...d.explorerSide, hidden: next } }));
}
