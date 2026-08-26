// Whether the user has SHUT the companion sidebar this session — one boolean,
// held in a module variable and written to no storage at all, shared by BOTH
// surfaces the sidebar has: the file view (`Preview.tsx`, via `lib/preview-side`)
// and the folder listing's pane (`Listing.tsx`, via `listing/pane-side`). Sibling
// to `side-store.ts` (the shared WIDTH) and deliberately built the same way — see
// that file's header for the fuller argument; this one is the short version.
//
// WHY THIS EXISTS: `_side` normally rides the URL alone (both `preview-side.ts`
// and `pane-side.ts` say so at length), and `navigate()` (platform/lib/router)
// drops the whole query string on most hops, re-adding `_side` only on a
// folder→folder one. So a close on a file, followed by a hop that touches a
// file at either end (file→file, file→folder, folder→file), lost the "shut"
// request the instant the URL that carried it was replaced, and the sidebar
// popped back open — the exact bug this store exists to close.
//
// MEMORY ONLY, DELIBERATELY — the same policy `side-store.ts` states for width,
// and for the same reason:
//   • it survives the shell's pushState navigation, because nothing here reads
//     or writes the URL — a close recorded here stays recorded across every hop
//     until the user reopens the sidebar or the document reloads.
//   • a REFRESH clears it, because a module variable cannot survive one. That is
//     the escape hatch, not an oversight: a hidden sidebar otherwise holds for
//     the whole session, and the way back has to be something a user can find
//     without being told about a setting. Reloading the page is that.
// sessionStorage would survive the refresh and localStorage the browser, so
// neither is an option here — same as the width store, same reasoning.
//
// The flag is a LOSING signal, not a stored preference: an explicit `_side` in
// the URL — a deep link, a carried-in link from the other surface, a legacy
// `_mode` bookmark — always wins over it (see `parseSide` / `parsePaneSide`).
// This store only answers the question "the URL is silent about `_side` — did
// the user just shut it?", and reopening on EITHER surface clears it so the next
// silent URL opens the sidebar again.
let hidden = false;

export function getSideHidden(): boolean {
  return hidden;
}

export function setSideHidden(next: boolean): void {
  hidden = next;
}
