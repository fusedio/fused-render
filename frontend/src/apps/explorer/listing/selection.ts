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

// The row a freshly opened folder previews when nothing else claims the
// selection (Listing's auto-select, FS-16): the FIRST row in the RENDERED
// order — file or directory — so the choice follows the active sort and is the
// row the eye is already on. Selecting a directory previews it as a PEEK (the
// pane's directory case), not a navigation, so landing on one is as harmless
// as landing on a file. null only when there is nothing to select at all (an
// empty folder), which leaves the pane on its self target exactly as before.
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

// What counts as a PAGE for the auto-selection below: a file whose extension
// renders as itself in the preview (`_render`, PT-12).
//
// `.htm` counts, and that is the OPPOSITE call from `lib/folder-app.ts`, which
// accepts `.html` only. The two questions are different and the divergence is
// the answer to each rather than an inconsistency: folder-app decides whether a
// folder IS AN APP, a claim the server also makes (`app_listing.app_entry`) and
// which two surfaces must agree on to the letter — there, a stray `.htm` was a
// bug. This decides which row a pane opens on, where being wrong costs one
// keystroke, and the registry binds `.html` and `.htm` to the same mode list, so
// a `.htm` previews exactly like an `.html`. Refusing it would land a folder
// whose only page is `page.htm` on some adjacent text file instead — a worse
// answer for no gain in correctness.
function isPageRow(name: string, isDir: boolean): boolean {
  return !isDir && /\.html?$/i.test(name);
}

// What a freshly opened folder should select for its preview pane, or null when
// there is nothing to select (FS-16). The caller owns the TIMING — one shot per
// mount, at the first settled non-search listing with the pane on, all of which
// are conditions on WHEN to ask, not on the answer (see Listing). This owns the
// decision.
//
// **The first PAGE in rendered order, else the first row** (D263, superseding
// D240's answer). The pane exists to show something, and in an ordinary folder
// the page is the one row that renders as itself rather than as a listing of a
// directory or a dump of text — a folder holding an `index.html` is opened to
// look at that page far more often than at whatever the sort put on row one.
// The fallback is untouched: no page, first entry, directories included.
//
// In RENDERED order, and deliberately not "index.html if there is one". The
// order is the one the user is looking at, so the row that wins is the one
// nearest the top of their table and re-sorting re-answers the question; a
// favoured name would instead pick a row that may be scrolled off screen, and
// would need its own tie-break story the moment a folder had two of them.
//
// The KIND is checked, never just the name: `build.html` as a DIRECTORY is a
// real shape (an exported site tree), and selecting it would peek a folder in
// place of the page the user can see.
//
// The decision takes NO reading of the URL, and deliberately so. It used to
// defer to the `?sel` param itself, resolving "what does this folder open on"
// in two places at once. It does not need to: a `?sel=` on the URL is SEEDED
// INTO THE SELECTION at mount (useListingSelection), so by the time this is
// asked the param has already become an ordinary claim on the selection and
// the yield below covers it (D240, the half that stands).
//
// That rationale was about the URL, never about the user: **auto-select fills
// an EMPTY selection, it never replaces one.** A row the user clicked in the
// pre-stat provisional listing rides across the swap (recallSelection), and
// overwriting it would undo a click the user had already made and seen. That
// yield is a condition on WHEN to ask, not on the answer, so it lives in
// Listing's effect (`selectionClaimed`) and this stays pure.
export function autoSelectPath(
  rows: string[],
  byPath: ReadonlyMap<string, { isDir: boolean }>,
): string | null {
  for (const path of rows) {
    // `byPath.has` is the same "is it on screen" gate firstEntryPath applies —
    // a page with no rendered row is not a candidate, and the fallback below
    // re-walks from the top rather than settling for it.
    const row = byPath.get(path);
    if (row && isPageRow(path.slice(path.lastIndexOf("/") + 1), row.isDir)) return path;
  }
  return firstEntryPath(rows, byPath);
}

// The row a SEARCH should select, or null to leave the selection alone.
//
// Same intent as the folder auto-select above — land on something so the pane
// has content and Enter has a target — but a different shape, because search
// results are not a folder. A folder's rows settle once per navigation, so
// that one is a single shot. Results re-rank on every keystroke, on every
// stream flush and on every slice the scan publishes, so this is asked
// repeatedly and has to answer three different situations:
//
//   * nobody has claimed the selection -> the top hit, and it FOLLOWS the
//     ranking as it refines. Pinning row one of a ranking the user has since
//     typed past would leave the pane on a result that is no longer the best
//     answer, which is worse than not selecting at all.
//   * the user moved the selection -> leave it. Auto-select fills a selection
//     nobody chose; it never overrules one somebody did (`selectionClaimed`
//     makes the same call for folders).
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
    // Cleared on purpose (Escape) — the folder shot honours the same thing.
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

// Does something already own the selection? The one question the auto-select
// effect asks before it spends its shot (FS-16): a non-empty selection at that
// moment was put there by the user — a click in the provisional scaffold,
// carried over by recallSelection; a `?sel=` on the URL they reloaded or were
// sent — or by the reconcile clamping onto a surviving row, and any of those
// outranks "row one, because the folder just opened". Anchor/lead are not consulted: `paths` is what is highlighted.
export function selectionClaimed(sel: Selection): boolean {
  return sel.paths.length > 0;
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
