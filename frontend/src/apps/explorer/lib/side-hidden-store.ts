// Whether the user has SHUT the companion sidebar — one boolean, shared by BOTH
// surfaces the sidebar has: the file view (`Preview.tsx`, via `lib/preview-side`)
// and the folder listing's pane (`Listing.tsx`, via `listing/pane-side`). Sibling
// to `side-store.ts` (the shared WIDTH, memory only) and `side-tab-store.ts` (the
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
// so it is a preference now: stored in `localStorage` under `SIDE_HIDDEN_KEY`,
// seeded once at module load (both consumers read it in a `useState`
// initializer, so it has to be there before the first render), and written
// through on every set — the same pattern `side-store.ts` used for the width
// before the width went back to memory only. Only the open/closed bit is a
// stored preference: the width and the selected tab are deliberately NOT, and
// reset on a reload (see those modules).
//
// Every storage access is wrapped — a private window, cleared site data, or a
// browser set to block storage makes the accessor itself throw — and a failure
// costs the persistence, never the flag: the module variable is still the live
// answer.
//
// The flag is a LOSING signal, not a command: an explicit `_side` in the URL — a
// deep link, a carried-in link from the other surface, a legacy `_mode` bookmark
// — always wins over it (see `parseSide` / `parsePaneSide`). This store only
// answers the question "the URL is silent about `_side` — did the user shut it?",
// and reopening on EITHER surface (a click, or a deep link that opens: see
// `sideReopenedByUrl`) clears it so the next silent URL opens the sidebar again.

/** The key the flag is stored under (`fused-render:` namespace, like every other
 *  key this app writes). Stored as "1" while shut; the key is absent when open. */
export const SIDE_HIDDEN_KEY = "fused-render:explorer-side-hidden";

function readStored(): boolean {
  try {
    return localStorage.getItem(SIDE_HIDDEN_KEY) === "1";
  } catch {
    return false; // blocked storage: the module variable is the whole answer
  }
}

function writeStored(next: boolean): void {
  try {
    if (next) localStorage.setItem(SIDE_HIDDEN_KEY, "1");
    else localStorage.removeItem(SIDE_HIDDEN_KEY);
  } catch {
    // Costs the persistence, never the flag.
  }
}

let hidden = readStored();

export function getSideHidden(): boolean {
  return hidden;
}

export function setSideHidden(next: boolean): void {
  hidden = next;
  writeStored(next);
}
