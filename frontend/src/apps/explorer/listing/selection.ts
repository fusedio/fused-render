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
  // enters in rendered row order). Empty = nothing selected.
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

// What a freshly opened folder should select for its preview pane, or null when
// there is nothing to select (FS-16). The caller owns the TIMING — one shot per
// mount, at the first settled non-search listing with the pane on, all of which
// are conditions on WHEN to ask, not on the answer (see Listing). This owns the
// decision.
//
// The decision takes NO reading of the URL, and deliberately so. It used to
// defer to the `?sel` param itself, resolving "what does this folder open on"
// in two places at once. It does not need to: a `?sel=` on the URL is SEEDED
// INTO THE SELECTION at mount (useListingSelection), so by the time this is
// asked the param has already become an ordinary claim on the selection and
// the yield below covers it. With no second claim to weigh, the ANSWER here is
// always the first entry (D240).
//
// That rationale was about the URL, never about the user: **auto-select fills
// an EMPTY selection, it never replaces one.** A row the user clicked in the
// pre-stat provisional listing rides across the swap (recallSelection), and
// overwriting it with row one would undo a click the user had already made and
// seen. That yield is a condition on WHEN to ask, not on the answer, so it
// lives in Listing's effect (`selectionClaimed`) and this stays pure.
export function autoSelectPath(
  rows: string[],
  byPath: ReadonlyMap<string, unknown>,
): string | null {
  return firstEntryPath(rows, byPath);
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

// What a mouse click on a row does TO THE SELECTION — and nothing else, which
// is the point.
//
// The explorer has ONE click model now: a single click selects, a double click
// opens, on every row in every view mode. It used to have two, chosen by
// whether the preview pane happened to be showing — pane off, a single click
// selected AND opened; pane on, it only selected and the double click opened.
// That was defensible while the pane was a thing the user switched on, and
// stopped being so the moment the split became a measurement of the window
// (listing/pane.ts): the same click in the same folder would open a file or
// not depending on how wide the window was when you clicked it.
//
// So: this function answers only which SELECTION a click means. Opening has no
// decision left to make — it is the row's onDoubleClick, unconditionally.
//
// `mod` is the caller's isMod(e) verdict rather than the raw event, so the
// rule stays pure and platform-free (isMod is exclusive per-platform by
// design; see lib/platform). Mod outranks Shift when both are down: the toggle
// is the more precise gesture, and a stray Shift while Mod-picking rows must
// not replace the picks with a range.
export type RowClickAction = "extend" | "toggle" | "select";

export function rowClickAction(gesture: { mod: boolean; shift: boolean }): RowClickAction {
  if (gesture.mod) return "toggle";
  if (gesture.shift) return "extend";
  return "select";
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
