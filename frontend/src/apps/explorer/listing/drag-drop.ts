// Dragging entries onto a folder to MOVE them: what a press picks up, where it
// may land, and the module-level store that holds the drag while it is in
// flight. Pure rules + one store, no DOM — the wiring is useRowDrag.ts.
//
// MOVE ONLY. There is no copy modifier and no import of files dragged in from
// the OS. Both are real features, and neither is this one: a modifier that
// silently turns a move into a copy is a thing you find out about afterwards,
// and an OS drop is an upload with its own progress, conflict and permission
// story. A drag inside the explorer means "put these there", every time.
//
// The MECHANISM is the browser's own drag-and-drop (draggable rows +
// dataTransfer), not a pointer-tracked ghost: it gives the move cursor and the
// drop-refused cursor for free, it applies its own click-vs-drag threshold (so
// single-click-select and double-click-open are untouched), and it survives the
// listing REMOUNTING mid-drag, which the spring-loaded breadcrumb makes
// routine.
//
// What the mechanism does NOT give is the payload: `dataTransfer.getData` is
// blacked out during dragover (a privacy rule — a page must not read a drag it
// has not been given), and every drop target needs the dragged paths BEFORE
// the release to decide whether to light up. Hence the in-flight store below;
// the dataTransfer copy is what makes the drag start at all (Firefox) and what
// `types` gating reads.
//
// Nothing is imported here, deliberately — the same testability constraint
// pane-math.ts documents. lib/fs-actions (where dirname/normDir live) reaches
// the API layer and from there the router, which reads `location` at MODULE
// INIT, so importing it would make this file unloadable in a DOM-free bun
// test. Callers already hold a RowCtx with its `parentDir` filled in; the one
// place that doesn't (the sidebar) derives it with the real dirname and hands
// a DragSource in.

// The private MIME that marks a drag as ours. A drop target can ask for this
// in `dataTransfer.types` mid-drag even though it cannot read the value, which
// is exactly the gate every handler needs: our own drag, not a text selection
// from another pane and not a file dragged in from the OS.
export const FS_DRAG_MIME = "application/x-fused-fs-paths";

// One dragged entry: the path on the move and the folder it is leaving. The
// parent is what makes "drop onto the folder it is already in" a no-op rather
// than a move, and it is already on every RowCtx.
export interface DragSource {
  path: string;
  parentDir: string;
}

// A candidate drop target — a listing row, the listing's own background
// (the current folder), or a sidebar bookmark. One shape for all three, so
// there is one rule and not three.
export interface DropTarget {
  path: string;
  isDir: boolean;
}

// Why a drop was refused. Carried out to the UI so the rejected target can
// explain itself in a title/tooltip rather than only refusing to light up.
export type DropRejection =
  | "empty" // nothing is being dragged
  | "not-a-folder" // a file row: entries go INTO folders, never onto files
  | "self" // the target is one of the entries on the move
  | "descendant" // a folder into its own subtree — it would contain itself
  | "already-there"; // every entry is in this folder: a move with nothing to move

export type DropVerdict = { ok: true; dir: string } | { ok: false; reason: DropRejection };

// Trailing-slash-blind path identity. The same folder reaches this as "/w" from
// a row and "/w/" from the listing's own fsPath, and the root as "/" or "";
// a comparison that can't see through that would let the background target
// "move" every row into the folder it is already in.
const canon = (p: string): string => p.replace(/\/+$/, "");

// Should a `dragleave` on `leaving` cancel the spring-loaded crumb that is
// currently armed (Breadcrumb)? Only when the crumb being left IS the armed
// one.
//
// Asking at all is the whole point. Cancelling unconditionally looks obviously
// right and silently disables the feature, because of the order the DOM fires
// drag events in: moving from one crumb to the next raises `dragenter` on the
// NEW target BEFORE `dragleave` on the old one. So arming on enter and
// disarming on any leave runs enter(docs) → arm docs → leave(/w) → disarm, and
// kills the timer that was armed a moment earlier. Spring-loading then only
// ever worked if the pointer entered the strip from outside and never crossed a
// second crumb — which is not how anyone drags along a path.
//
// Comparing against the armed target is correct under EITHER ordering, which is
// why it is the fix rather than a re-ordering of the handlers: if leave came
// first the armed crumb would be the one being left and it would disarm, and if
// enter comes first the armed crumb is already the new one and the stale leave
// is ignored.
export function springDisarms(leaving: string, armed: string | null): boolean {
  return armed !== null && armed === leaving;
}

// --- where a drag may start from ---------------------------------------------
//
// Not everywhere on a row, because the listing has TWO press-and-move gestures
// and this is the one rule that separates them. The drag handle is the part of
// the row that IS the file — its icon and its name — plus the whole of any row
// that is ALREADY SELECTED. `false` is not "nothing happens": everywhere else
// SWEEPS, selecting the rows the pointer crosses (useMarquee reads this
// function backwards to know where a sweep may start, so the two gestures
// cannot claim the same pixel).
//
// Dragging a file into another folder is a destructive-ish, irreversible-ish
// act (it moves it), and rows span the full width of the listing. With the
// whole row live, the size column, the modified column and the empty gutter
// after a short name are all several hundred pixels of "start moving this file"
// sitting under a pointer that is usually just picking rows. Grabbing a file by
// its name is both how every file manager behaves and the smallest target that
// still reads as deliberate.
//
// The selected-row case is the exception that makes multi-drag bearable: having
// said which rows they mean, the user should not have to find one particular
// row's name to move all five. Selection outranks hit region — the only case
// where the two clauses could disagree.
//
// Either way a press that never travels the sweep's 4px slop is neither
// gesture: it is the plain click that selects one row.
export function pressStartsDrag(press: { onHandle: boolean; rowSelected: boolean }): boolean {
  return press.onHandle || press.rowSelected;
}

// What a press on `path` picks up. The standard file-manager rule: a row that
// is part of the current selection drags the WHOLE selection, and a row outside
// it drags only itself (the press is also a click, and a click selects — the
// caller collapses the selection onto the row before the drag starts).
// `selected` arrives in rendered order, so a batch move processes rows
// top-to-bottom however they were clicked.
export function dragPathsFor(path: string, selected: readonly string[]): string[] {
  return selected.includes(path) ? [...selected] : [path];
}

// May these entries be dropped here, and if so into which folder? The one
// place the answer lives — the row highlight, the sidebar highlight, the
// background target and the drop handler all ask this, so what lights up and
// what actually moves cannot disagree.
//
// The rejections are checked outermost-first: a target that isn't a folder is
// never a target at all, then the two structural impossibilities (dropping
// something onto itself, or a folder inside itself), then the no-op.
//
// "already-there" is decided over the WHOLE batch, not per entry: a search
// selection can hold hits from several folders, and one of them already living
// in the target is no reason to refuse the others. The mover skips the entries
// that are already home (as a same-folder paste does).
export function dropIsValid(dragged: readonly DragSource[], target: DropTarget): DropVerdict {
  if (dragged.length === 0) return { ok: false, reason: "empty" };
  if (!target.isDir) return { ok: false, reason: "not-a-folder" };
  const dir = canon(target.path);
  for (const d of dragged) {
    const src = canon(d.path);
    if (src === dir) return { ok: false, reason: "self" };
    // The separator is what makes containment: "/w/docs2" is a sibling of
    // "/w/docs", not something inside it.
    if (dir.startsWith(src + "/")) return { ok: false, reason: "descendant" };
  }
  if (dragged.every((d) => canon(d.parentDir) === dir)) {
    return { ok: false, reason: "already-there" };
  }
  return { ok: true, dir: target.path };
}

// --- the payload on the wire -------------------------------------------------

// The paths, as the string dataTransfer carries. JSON rather than newline-
// joined text: a path may contain anything a filesystem allows, newlines
// included.
export function encodeDragPaths(paths: readonly string[]): string {
  return JSON.stringify(paths);
}

// The reverse, and deliberately paranoid: dataTransfer holds whatever the drag
// SOURCE put there, and a drag from another page or another app can arrive
// under a type we asked for. Anything that isn't a list of strings decodes to
// nothing at all rather than to a path we would then try to move.
export function decodeDragPaths(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.every((p) => typeof p === "string") ? (parsed as string[]) : [];
  } catch {
    return [];
  }
}

// Is this drag ours? The only question a dragover handler can ask about the
// payload (see the module header on getData).
export function carriesFsDrag(types: readonly string[]): boolean {
  return types.includes(FS_DRAG_MIME);
}

// --- the in-flight drag ------------------------------------------------------
//
// Module-level, like the cut/copy clipboard and the cross-remount selection
// stash next door, and for the same reason as the latter: the Listing REMOUNTS
// during a drag (spring-loading a breadcrumb navigates with the drag still
// held), so component state cannot be where the dragged entries live. A drag is
// singular by construction — one pointer, one payload — so one slot is enough.

let inFlight: DragSource[] = [];

export function startFsDrag(items: DragSource[]): void {
  inFlight = items;
}

export function clearFsDrag(): void {
  inFlight = [];
}

// The entries currently being dragged, or [] when nothing is. Returning the
// empty case as an empty list rather than null is what lets a drop target hand
// it straight to dropIsValid, which already spells "nothing to drop" as a
// refusal ("empty").
export function fsDragInFlight(): DragSource[] {
  return inFlight;
}
