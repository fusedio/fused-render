// Whether the user has SHUT the companion sidebar — one boolean, shared by BOTH
// surfaces the sidebar has: the file view (`Preview.tsx`, via `lib/preview-side`)
// and the folder listing's pane (`Listing.tsx`, via `listing/pane-side`). Sibling
// to `side-store.ts` (the shared WIDTH, also persisted) and `side-tab-store.ts` (the
// selected companion, memory only).
//
// WHY THIS EXISTS: `_side` normally rides the URL alone (both `preview-side.ts`
// and `pane-side.ts` say so at length), and `navigate()` (platform/lib/router)
// drops the whole query string on most hops, re-adding `_side` only on a
// folder→folder one. So a close on a file, followed by a hop that touches a
// file at either end (file→file, file→folder, folder→file), lost the "shut"
// request the instant the URL that carried it was replaced, and the sidebar
// popped back open — the exact bug this store exists to close.
//
// PERSISTED ACROSS RELOADS, AT THE OWNER'S REQUEST. This module used to be memory
// only, on the argument that a refresh clearing it was the escape hatch from a
// sidebar that otherwise stays shut. The owner asked to remember the open/closed
// state globally instead ("lets globally remember the sidebar open/close state"),
// so it is a preference now: `explorerSide.hidden` in the server-held layout
// document (platform/lib/ui-layout.ts, D1386), read synchronously off that
// module's cache — which main.tsx fills before the first render, so the two
// consumers' `useState` initializers see it. It was the localStorage key
// `fused-render:explorer-side-hidden` until the desktop app's port walk (a new
// origin, so empty storage, on most relaunches) was found resetting it; that
// key is imported once by ui-layout.ts. The open/closed bit and the width are
// stored preferences; the selected tab is deliberately NOT, and resets on a
// reload (see `side-tab-store.ts`).
//
// The flag is a LOSING signal, not a command: an explicit `_side` in the URL — a
// deep link, a carried-in link from the other surface, a legacy `_mode` bookmark
// — always wins over it (see `parseSide` / `parsePaneSide`). This store only
// answers the question "the URL is silent about `_side` — did the user shut it?",
// and reopening on EITHER surface (a click, or a deep link that opens: see
// `sideReopenedByUrl`) clears it so the next silent URL opens the sidebar again.

import { getUiLayout, patchUiLayout } from "@platform/lib/ui-layout";

export function getSideHidden(): boolean {
  return getUiLayout().explorerSide?.hidden === true;
}

export function setSideHidden(next: boolean): void {
  if (getSideHidden() === next) return;
  patchUiLayout((d) => ({ ...d, explorerSide: { ...d.explorerSide, hidden: next } }));
}
