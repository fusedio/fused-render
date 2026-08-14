// Selection model + cross-remount stash for the listing's keyboard/mouse
// selection.
//
// Keyboard selection (the arrow-key row highlight) survives the Listing
// component's per-folder remount. Opening a folder first paints App's pre-stat
// scaffold with a PROVISIONAL Listing, then swaps it for the resolved one when
// stat lands ~1s later — a remount that would otherwise wipe an in-progress
// arrow selection mid-keystroke (press Down during the open → the highlight
// vanishes). Like fs-clipboard, the state lives just outside the remount
// boundary. A single entry keyed by fsPath is enough (only one Listing is
// mounted at a time): it bridges the scaffold→resolved swap and restores the
// highlight if you browse back to the same folder.
//
// The stash carries the WHOLE multi-selection, not just one path, so a
// Shift-range built in the provisional Listing survives too.
export interface Selection {
  // Selected row paths, in the order they entered the selection (a Shift-range
  // enters in rendered row order, and so does a sweep — an ADDITIVE sweep keeps
  // the rows it started with in front of the ones it collects).
  // Empty = nothing selected.
  paths: string[];
  // Where a Shift-range extends FROM: the last row selected by a plain or
  // Mod-click / plain arrow move. null when nothing has been selected yet.
  anchor: string | null;
  // The focused row: what arrows move from, what Enter opens, what F2 renames,
  // and what a paste/new-file targets. null = no selection.
  lead: string | null;
}

export const EMPTY_SELECTION: Selection = { paths: [], anchor: null, lead: null };

// Collapse to exactly one row — the plain-click / plain-arrow / re-anchor case.
export function oneSelected(path: string): Selection {
  return { paths: [path], anchor: path, lead: path };
}

let lastSelection: { fsPath: string; sel: Selection } | null = null;
export function recallSelection(fsPath: string): Selection {
  return lastSelection && lastSelection.fsPath === fsPath ? lastSelection.sel : EMPTY_SELECTION;
}
export function rememberSelection(fsPath: string, sel: Selection): void {
  lastSelection = { fsPath, sel };
}

// What the selection becomes when the LEAD's row is NOT among the rendered rows
// (D277). The reconcile in useListingSelection owns WHEN this is asked — it has
// already waited out a still-loading listing and a pending rename target — and
// this owns the answer.
//
// `lastIndex` is the last slot the lead was seen occupying in these rows, or
// **-1 for a lead that has never been seen in them at all**, and that number is
// the whole decision:
//
//   * seen before, and now gone -> RE-ANCHOR to whatever occupies its slot
//     (clamped to the last row when the folder shrank past it). This is a row
//     the user was demonstrably on and something took it away — an external
//     delete, a move to the bin, a rename the user made themselves — and landing
//     the highlight on the neighbour is what every file manager does; going
//     empty there would punish the user for an edit they asked for.
//   * NEVER seen -> select NOTHING. The only way to get here is a lead that was
//     SEEDED rather than chosen: a `?sel=` from a bookmark, a shared link or the
//     upward hop, naming a file that has since been deleted or renamed. There is
//     no slot to re-anchor to, because the selection never had one — the old
//     clamp read the -1 as "row zero" and selected the folder's first row, which
//     is a file nobody named and a preview iframe nobody asked for. That is
//     precisely the guess D276 removed from the folder open, arriving by another
//     door: a link that misses is a link that missed, not a request for row one.
//
// An EMPTY row list is empty either way — there is no neighbour to fall back to,
// however well anchored the selection was.
export function selectionAfterVanish(rows: string[], lastIndex: number): Selection {
  if (rows.length === 0 || lastIndex < 0) return EMPTY_SELECTION;
  return oneSelected(rows[Math.min(lastIndex, rows.length - 1)]);
}

// The first row in the RENDERED order that is actually ON SCREEN, or null when
// there is none. The order is the one the user is looking at, so the answer
// follows the active sort, and a directory is an ordinary candidate.
//
// Its only caller now is the SEARCH auto-select below ("the top hit"). It was
// written for the folder auto-select — the row a freshly opened folder previewed
// — which D276 deleted: opening a folder now selects nothing at all (FS-16).
export function firstEntryPath(
  rows: string[],
  byPath: ReadonlyMap<string, unknown>,
): string | null {
  for (const path of rows) {
    // A path with no rendered row can't be selected — it isn't on screen.
    if (byPath.has(path)) return path;
  }
  return null;
}

// The row a SEARCH should select, or null to leave the selection alone.
//
// A query is itself a request to look at something, so landing on the top hit
// gives the pane content and Enter a target. THIS IS THE ONLY AUTO-SELECTION
// LEFT: opening a folder selects nothing (FS-16/D276, see Listing). It is also a
// different shape from that one was, because search results are not a folder. A
// folder's rows settle once per navigation, so that was a single shot. Results
// re-rank on every keystroke, on every stream flush and on every slice the scan
// publishes, so this is asked repeatedly and has to answer three different
// situations:
//
//   * nobody has claimed the selection -> the top hit, and it FOLLOWS the
//     ranking as it refines. Pinning row one of a ranking the user has since
//     typed past would leave the pane on a result that is no longer the best
//     answer, which is worse than not selecting at all.
//   * the user moved the selection -> leave it. Auto-select fills a selection
//     nobody chose; it never overrules one somebody did.
//   * the user's row left the results -> the top hit again. There is nothing
//     left to respect: the row is not on screen, so keeping the selection
//     there previews a path the user cannot see.
//
// `userOwned` is the caller's to determine — it is a fact about what happened,
// not about the rows (see Listing, which reads it as "the lead is not where
// auto-select last put it"). Returning null for a path already selected keeps
// the caller from writing state on every re-rank; each of those writes
// remounts the preview iframe.
export function searchAutoSelectPath(
  rows: string[],
  byPath: ReadonlyMap<string, unknown>,
  sel: Selection,
  userOwned: boolean,
): string | null {
  const first = firstEntryPath(rows, byPath);
  if (first === null) return null; // nothing matched; nothing to select
  if (userOwned) {
    // Cleared on purpose (Escape) — that is a choice too, so it stands.
    if (sel.lead === null) return null;
    if (byPath.has(sel.lead) && rows.includes(sel.lead)) return null; // still here
  }
  return first === sel.lead ? null : first;
}

// Whose selection the search results are currently showing.
//
// This is the half that has to REMEMBER, and it lives here rather than in a
// pair of refs inside Listing because it was wrong there and untestable there
// — the record was reset on every query change, which reclassified the user's
// own selection as the app's guess and let the next re-rank overwrite it.
//
// `autoPlaced` is the path this decision last wrote. Comparing the lead
// against it is how a user gesture is detected without instrumenting every
// click, arrow key and marquee path: if the lead is not where auto-select put
// it, somebody else moved it.
//
// `userClaimed` then PERSISTS ACROSS QUERY CHANGES, which is the whole point.
// A new query re-ranks the rows; it does not revoke the user's choice. So a
// claimed selection survives every re-rank and every retyped query for as long
// as its row is still among the results, and only a drop-out hands the
// decision back. An auto-placed one, by contrast, is re-made against whatever
// the new ranking put first — it was never more than a guess.
export interface SearchSelectState {
  autoPlaced: string | null;
  userClaimed: boolean;
}

export const INITIAL_SEARCH_SELECT: SearchSelectState = {
  autoPlaced: null,
  userClaimed: false,
};

export function nextSearchSelection(
  state: SearchSelectState,
  rows: string[],
  byPath: ReadonlyMap<string, unknown>,
  sel: Selection,
): { state: SearchSelectState; select: string | null } {
  // A query change empties the results for a commit and the listing reconcile
  // clears the selection with them. Neither is a user gesture, so the empty
  // commit gets no judgment at all — deciding there read the reconcile's clear
  // as Escape and permanently blocked auto-select after the first refine.
  if (firstEntryPath(rows, byPath) === null) return { state, select: null };
  // A lead that is not where this decision left it was moved by the user —
  // including moved to nothing, which is Escape and is equally theirs. Only
  // meaningful once something HAS been placed: before that, an empty selection
  // is "the results just arrived", not "the user cleared it".
  let claimed =
    state.userClaimed || (state.autoPlaced !== null && sel.lead !== state.autoPlaced);
  // A claim hangs on a row. With the lead empty that row is the one auto-select
  // last wrote — and if it is no longer a result, the "clear" was the reconcile
  // dropping a vanished path, not the user: the claim dies with its anchor.
  const anchor = sel.lead ?? state.autoPlaced;
  const anchorPresent = anchor !== null && byPath.has(anchor) && rows.includes(anchor);
  if (claimed && sel.lead === null && !anchorPresent) claimed = false;
  const select = searchAutoSelectPath(rows, byPath, sel, claimed);
  if (select === null) return { state: { ...state, userClaimed: claimed }, select };
  // Writing a selection makes it ours again: a claim only ever ends because
  // the row it was on stopped being a result.
  return { state: { autoPlaced: select, userClaimed: false }, select };
}

// --- the `?sel=` URL param ---------------------------------------------------
//
// The primary (lead) selection, mirrored into the folder's URL, so a reload or
// a shared link comes back to the same row with the same thing in the preview
// pane. Relative to the folder, never absolute: `?sel=notes.md`, which is short
// enough to read in the address bar and keeps the fs path out of a link that
// already carries it in its pathname.
//
// One rule covers both view modes, because a search hit's row path is
// `base + "/" + entry.rel` exactly like a plain row's is `base + "/" + name` —
// so the param is always "the part after this folder", whether that is a name
// or a relative path several levels down.
//
// A MULTI-selection does not round-trip: only the lead is written. The lead is
// what the pane previews and what Enter opens, which is the whole of what a
// restored link needs; a range is a working state, not a destination.

// The param value for the current lead, or null when there is nothing to
// write. Also null for a lead that isn't in this folder at all — which happens
// for a beat mid-navigation, and writing it would put another folder's row on
// this folder's URL.
export function selParam(base: string, path: string | null): string | null {
  if (path === null) return null;
  const prefix = base + "/";
  return path.startsWith(prefix) ? path.slice(prefix.length) : null;
}

// The reverse: the row path a `?sel=` value names in this folder, or null when
// it names nothing usable. The value comes from a URL, so it is arbitrary
// input — an absolute value, or one climbing out of the folder with `..`, must
// not turn into a path the preview pane will stat. A leading dot is fine: only
// a whole segment of exactly ".." is a climb, so `.gitignore` and `..hidden`
// are ordinary names.
export function pathFromSelParam(base: string, raw: string | null): string | null {
  if (!raw || raw.startsWith("/")) return null;
  if (raw.split("/").includes("..")) return null;
  return base + "/" + raw;
}

// The `?sel=` value an UPWARD hop should seed: the child of `dest` that the
// user is coming out of, or null when there is no such child.
//
// Every file manager does this — go up (crumb click, Mod+Up, Backspace) and
// the folder you just left is the highlighted row, so the eye finds where it
// was and a second Up carries on from there. The explorer's `?sel=` param
// already IS "which row this folder opens on" (seeded at mount by
// useListingSelection, scrolled into view by its effect), so the whole feature
// is deciding the value — hence a pure function, and hence ONE of them for
// both the crumbs (Breadcrumb.tsx) and the keyboard (useListingShortcuts).
//
// The answer is the IMMEDIATE child, not the remainder: a crumb three levels
// up owns only its own rows, and `sub/deep/leaf` there would name a row that
// folder does not render. (`selParam`, the write side, keeps whole relative
// paths because a SEARCH hit really is a row of the folder it was found from.)
//
// `dest` and `from` are fs paths in the shell's canonical form; a trailing
// slash on either is tolerated (the fs root arrives as "/" from the crumbs and
// as "" from Listing's `base`, and a Windows drive root is "C:/").
export function cameFromSelParam(dest: string, from: string): string | null {
  const root = dest.replace(/\/+$/, "");
  const rest = from.replace(/\/+$/, "");
  if (!rest.startsWith(root + "/")) return null;
  const seg = rest.slice(root.length + 1).split("/")[0];
  return seg.length > 0 ? seg : null;
}

// What a press on a row does TO THE SELECTION — and nothing else, which is the
// point.
//
// The explorer has ONE press model: a press selects, a double click opens, on
// every row in every view mode. It used to have two, chosen by whether the
// preview pane happened to be showing — pane off, a single click selected AND
// opened; pane on, it only selected and the double click opened. That was
// defensible while the pane was a thing the user switched on, and stopped being
// so the moment the split became a measurement of the window (listing/pane.ts):
// the same click in the same folder would open a file or not depending on how
// wide the window was when you clicked it.
//
// THIS IS DECIDED ON POINTERDOWN, not on click, and that is not a detail. Rows
// are drag sources, and on a `draggable` element WebKit does not reliably
// deliver the `click` that would have followed the press — which is how
// Shift/Cmd-click went silently dead once, and how a plain click on parts of a
// row could fail to select at all. Deciding on the press removes the entire
// failure class rather than working around it, and it is what Finder and
// Explorer do: the highlight lands while the button is still down. Opening is
// untouched — `dblclick` fires independently of any of this.
//
// The four answers:
//
//   toggle  Mod down: add/remove this row, re-anchoring on it.
//   extend  Shift down: the range from the anchor to this row.
//   select  the ordinary press: this row alone, immediately.
//   defer   a plain press on a row that is ALREADY part of a MULTI-selection.
//           The one case that cannot be answered on the press: collapsing to
//           the pressed row there would make a multi-row drag impossible, since
//           every drag begins with a press on one of the rows being dragged.
//           So it waits for the release, and collapses only if the press never
//           became a drag or a sweep. A press on a row that is the ONLY
//           selected row needs no deferral — "select" is already what it is.
//
// `mod` is the caller's isMod(e) verdict rather than the raw event, so the rule
// stays pure and platform-free (isMod is exclusive per-platform by design; see
// lib/platform). Mod outranks Shift when both are down: the toggle is the more
// precise gesture, and a stray Shift while Mod-picking rows must not replace
// the picks with a range. Both outrank `inMultiSelection`, because a modified
// press is never the start of a plain drag of the selection.
export type RowPressAction = "extend" | "toggle" | "select" | "defer";

export function rowPressAction(gesture: {
  mod: boolean;
  shift: boolean;
  inMultiSelection: boolean;
}): RowPressAction {
  if (gesture.mod) return "toggle";
  if (gesture.shift) return "extend";
  return gesture.inMultiSelection ? "defer" : "select";
}

// A contiguous range of rendered rows, inclusive, in row order. `rows` is the
// live navRows order (the SORTED/rendered order, never the raw fs order).
export function rangeBetween(rows: string[], from: string, to: string): string[] {
  const a = rows.indexOf(from);
  const b = rows.indexOf(to);
  if (a === -1 || b === -1) return b === -1 ? [] : [to];
  return a <= b ? rows.slice(a, b + 1) : rows.slice(b, a + 1);
}

// How many rows a PageUp/PageDown moves: one viewport of rows minus one, so the
// row you were on stays visible as context. Measured from the live DOM (scroller
// height / row height) and falls back to a sane constant before first paint.
const PAGE_ROWS_FALLBACK = 12;
export function pageRows(): number {
  const scroller = document.querySelector(".listing-scroll") as HTMLElement | null;
  const row = document.querySelector("table.listing-table tr.row") as HTMLElement | null;
  const rowH = row?.offsetHeight ?? 0;
  if (!scroller || rowH <= 0) return PAGE_ROWS_FALLBACK;
  return Math.max(1, Math.floor(scroller.clientHeight / rowH) - 1);
}
