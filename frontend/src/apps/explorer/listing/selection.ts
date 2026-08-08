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

// What a freshly opened folder should select for its preview pane, or null for
// "leave the selection alone" (FS-16). The caller owns the TIMING — one shot
// per mount, at the first settled non-search listing with the pane on, all of
// which are conditions on WHEN to ask, not on the answer (see Listing). This
// owns the decision.
//
// `urlSel` is the `?sel` param, and it is deliberately the ONLY reading of the
// current selection taken here. Two reasons, and the second is the subtle one:
//   • A `?sel` seed is a claim on the selection that must win, and
//     useListingSelection applies it in an effect registered BEFORE the
//     caller's — so on the commit this runs, its setSel has not landed yet and
//     the component's `sel` is still the pre-seed value. The URL is what is
//     already true.
//   • A BARE url means the selection is about to be (or already is) empty,
//     whatever `sel` currently holds: the seeding effect CLEARS a selection
//     recalled across a remount when the URL carries no `sel`, and after the
//     seed the mirror effect keeps `?sel` in step with the lead. Reading the
//     component's `sel` here instead let a recalled-then-discarded selection —
//     browse into a file and come back to its folder, whose URL no longer
//     carries `sel` — burn the one shot and leave the pane empty for the whole
//     mount.
export function autoSelectPath(
  urlSel: string | null,
  rows: string[],
  byPath: ReadonlyMap<string, unknown>,
): string | null {
  if (urlSel) return null;
  return firstEntryPath(rows, byPath);
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
