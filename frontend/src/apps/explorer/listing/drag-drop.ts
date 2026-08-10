// Dragging entries onto a folder to MOVE them: what a press picks up, where it
// may land, and the module-level store that holds the drag while it is in
// flight. Pure rules + one store, no DOM — the wiring is row-drag.ts.
//
// MOVE ONLY. There is no copy modifier and no import of files dragged in from
// the OS. Both are real features, and neither is this one: a modifier that
// silently turns a move into a copy is a thing you find out about afterwards,
// and an OS drop is an upload with its own progress, conflict and permission
// story. A drag inside the explorer means "put these there", every time.
//
// The MECHANISM IS POINTER EVENTS, and it used to be the browser's own
// drag-and-drop. HTML5 DnD gave a lot away for free — the move and no-drop
// cursors, its own click-vs-drag threshold, survival across the listing
// remounting mid-drag — and it cost the one thing this listing cannot give up:
// WE DO NOT OWN THE MOMENT IT DECIDES. The browser reads `draggable` when the
// movement actually begins, not when the button goes down, and a press on an
// unselected row SELECTS that row on pointerdown. So by the time the browser
// looked, the row it was standing on was selected, `draggable` had flipped to
// true a re-render ago, and every press on an unselected row armed a move-drag
// the sweep could never win. That is not fixable in `pressStartsDrag` — no
// rule stated here can matter if it is consulted after the fact — so the
// gesture is arbitrated at POINTERDOWN, once, from a snapshot (see below), and
// the native API is out of the row drag entirely.
//
// The in-flight store below outlives the Listing on purpose: spring-loading a
// breadcrumb navigates with the drag still held, remounting the listing under
// it, so the dragged entries cannot live in component state.
//
// Nothing is imported here, deliberately — the same testability constraint
// pane-math.ts documents. lib/fs-actions (where dirname/normDir live) reaches
// the API layer and from there the router, which reads `location` at MODULE
// INIT, so importing it would make this file unloadable in a DOM-free bun
// test. Callers already hold a RowCtx with its `parentDir` filled in; the one
// place that doesn't (the sidebar target) derives it with the real dirname.

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

// Should a leave of `leaving` cancel the spring-loaded crumb that is currently
// armed (Breadcrumb)? Only when the crumb being left IS the armed one.
//
// Asking at all is the whole point. Cancelling unconditionally looks obviously
// right and silently disables the feature, because of the order enter/leave
// arrive in: moving from one crumb to the next raises ENTER on the NEW target
// BEFORE LEAVE on the old one. The DOM's drag events did that, and the pointer
// drag that replaced them emits the pair in the same order for exactly this
// reason (row-drag.ts) — a re-ordering there would silently re-break the
// feature this guard exists to protect. So arming on enter and
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
// The listing has TWO press-and-move gestures over the same pixels, and this is
// the one rule that separates them. It has exactly one input:
//
//   ┌──────────────────────────────────┬──────────────────────────────┐
//   │ press lands on…                  │ press-and-move does…         │
//   ├──────────────────────────────────┼──────────────────────────────┤
//   │ a row that WAS ALREADY SELECTED  │ MOVE-DRAG the selection      │
//   │ any part of an unselected row    │ SWEEP                        │
//   │ the background                   │ SWEEP                        │
//   └──────────────────────────────────┴──────────────────────────────┘
//
// `false` is not "nothing happens": everywhere else SWEEPS, selecting the rows
// the pointer crosses. useMarquee reads this function BACKWARDS to know where a
// sweep may start, which is why there is a function at all — one rule read two
// ways can't disagree with itself, and two gestures can't claim one pixel.
//
// THE INPUT IS A SNAPSHOT, AND THAT IS THE WHOLE FIX. `rowWasSelected` is the
// selection AS IT STOOD BEFORE THIS PRESS, not as it stands while the pointer
// is moving. The press itself selects the row it lands on, so the live flag is
// contaminated by the very gesture it is being asked about: read live, EVERY
// press on an unselected row looks like a press on a selected one a moment
// later, and every sweep across rows turns into a move-drag. That is precisely
// what a `draggable` attribute is — a flag the browser reads later — and it is
// why the native drag API had to go rather than be re-tuned. The snapshot is
// taken once, in the capture phase of pointerdown, before any handler can
// change the selection (useMarquee), and the answer never changes mid-gesture.
//
// This used to also make each row's icon-and-name a permanent drag handle, so
// that a single unselected file could be moved in one gesture. That was wrong
// in the way the user kept reporting: starting a drag across rows from a name
// grabbed that one file and moved it instead of selecting the rows swept over.
// Dragging to select is the far more common gesture and it needs the row's
// whole width, so the drag source shrank to the one region where nothing
// competes for the pixel — a row the user has already picked.
//
// The cost is real and worth stating: moving a single unselected file is now
// TWO gestures, a click to select it and then a drag, where the handle made it
// one. That is the price of drag-to-select working on rows at all, and it is
// what select-then-drag has meant in every file manager — you can only drag
// what you can see is coming with you.
//
// Either way, a press that never travels the sweep's 4px slop is neither
// gesture: it is the press that selects one row (selection's rowPressAction).
// There is exactly ONE threshold for all three outcomes.
export function pressStartsDrag(press: { rowWasSelected: boolean }): boolean {
  return press.rowWasSelected;
}

// What a press on `path` picks up. The standard file-manager rule: a row that
// is part of the current selection drags the WHOLE selection, and a row outside
// it drags only itself.
//
// Under pressStartsDrag above only the first branch can be reached from the
// listing — a drag starts on a selected row or not at all. The second stays
// because it is the right answer to the question, not because something asks
// it today: it is what any future drag source would need, and a rule that
// silently dragged the wrong rows would be worse than one clause of slack.
//
// `selected` arrives in rendered order, so a batch move processes rows
// top-to-bottom however they were picked.
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

// --- what the ghost says -----------------------------------------------------

// The label on the ghost that follows the cursor. One entry is named; several
// are counted, because naming one of five would show exactly one of the things
// being moved and give no hint that the other four are coming.
//
// `basename` is not imported (see the module header on imports) — the caller
// passes the display name it already has.
export function dragGhostLabel(names: readonly string[]): string {
  return names.length === 1 ? names[0] : `${names.length} items`;
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
