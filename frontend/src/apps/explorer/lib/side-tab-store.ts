// The companion TAB (`git` / `claude`) the user last picked in the sidebar — one
// string, held in a module variable and written to no storage at all, shared by
// BOTH surfaces: the file view (`Preview.tsx`, via `lib/preview-side`) and the
// folder listing's pane (`Listing.tsx`, via `listing/pane-side`). Sibling to
// `side-store.ts` (the shared width, persisted) and `side-hidden-store.ts`
// (the open/closed flag, also persisted across reloads); the tab is the one
// piece of sidebar state that is NOT.
//
// WHY THIS EXISTS: the chosen tab used to ride only the `_side` URL param and,
// where the URL was silent, fell back to the file's/folder's default companion.
// `navigate()` drops the query on most hops, so a Git pick was forgotten on the
// next file or folder. This store keeps it for the lifetime of the DOCUMENT; a
// refresh clears it (a module variable cannot survive one), unlike the width and the
// open/closed flag, which are persisted.
//
// A LOSING SIGNAL, like the hidden flag: an explicit `_side=<mode>` in the URL
// always wins, and the remembered tab is used only where the URL names no mode
// AND that mode is actually available on the current subject (a real, settled
// companion — see `resolveSide` / `activePaneSide`); otherwise the usual default
// applies, and the memory is left alone for the next subject that does offer it.
// Only an explicit user switch writes it — closing the sidebar, a deep link, and
// the default resolving do not.
//
// CLEARED ON HOME: mounting the Home page (`/home`) or the explorer homepage
// (`/explorer`) calls `setSideTab(null)` (`shell/App.tsx`), so a fresh page opened
// from home starts on the default companion (Claude), not a remembered tab. The
// open/closed flag and the width are global and are NOT touched.
let tab: string | null = null;

export function getSideTab(): string | null {
  return tab;
}

export function setSideTab(next: string | null): void {
  tab = next;
}
