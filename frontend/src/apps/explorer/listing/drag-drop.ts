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

// A candidate drop target — a listing row, the listing's own background (the
// current folder), a sidebar bookmark, or a breadcrumb crumb. One shape for all
// four, so there is one rule and not four. The crumb is the only one that can
// name a folder ABOVE the listing, and it needs no special case to do it: see
// the crumb block in drag-drop.test.ts for which refusals it can reach.
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
// the one rule that separates them. The ITEM is its icon+name handle; the rest
// of the row — the dead space beside the name, the size and modified cells —
// is marquee ground, same as the background:
//
//   ┌────────────────────────────────────────┬──────────────────────────┐
//   │ press lands on…                        │ press-and-move does…     │
//   ├────────────────────────────────────────┼──────────────────────────┤
//   │ the icon+name HANDLE of any row        │ MOVE-DRAG that row       │
//   │ anywhere inside an ALREADY-SELECTED row│ MOVE-DRAG the selection  │
//   │ an unselected row's dead space/size/…  │ SWEEP                    │
//   │ the background                         │ SWEEP                    │
//   │ any of the above, Shift or Mod held    │ SWEEP, additive          │
//   └────────────────────────────────────────┴──────────────────────────┘
//
// `false` is not "nothing happens": everywhere else SWEEPS, selecting the rows
// the pointer crosses. useMarquee reads this function BACKWARDS to know where a
// sweep may start, which is why there is a function at all — one rule read two
// ways can't disagree with itself, and two gestures can't claim one pixel.
//
// A MODIFIED press never drags, whatever it lands on. Shift and Mod mean
// "change my selection" — extend the range, toggle this row — and that request
// gets the additive sweep, never a move: a modifier that also picked up files
// would be two very different gestures sharing one keychord.
//
// THE SNAPSHOT STILL MATTERS FOR `rowWasSelected`, exactly as before: it is the
// selection AS IT STOOD BEFORE THIS PRESS, not as it stands while the pointer
// is moving. The press itself selects the row it lands on, so the live flag is
// contaminated by the very gesture it is being asked about: read live, EVERY
// press on an unselected row looks like a press on a selected one a moment
// later, and every sweep across rows turns into a move-drag. That is precisely
// what a `draggable` attribute is — a flag the browser reads later — and it is
// why the native drag API had to go rather than be re-tuned. The snapshot is
// taken once, in the capture phase of pointerdown, before any handler can
// change the selection (useMarquee), and the answer never changes mid-gesture.
// `onHandle` needs no such snapshot: the handle a press lands on is a fact
// about the DOM at pointerdown, not about state the press itself could change.
//
// The icon-and-name handle was removed once and is now back, and the reversal
// is not a return to the bug that removal fixed. The bug was that EVERY row's
// entire width doubled as a drag source, so a sweep begun anywhere — including
// across a name — grabbed the one file under the press and moved it instead of
// selecting what the pointer crossed. Shrinking the item to its name cell fixes
// that without giving the handle back up: drag-to-select no longer needs the
// row's whole width, because it no longer competes with the handle for the
// SAME pixels, only for the space around them. A sweep can still start from any
// row's dead space, its size or modified cell, or the background — everywhere
// the handle is not.
//
// The cost is real and worth stating: sweeping a range that starts ON a
// file's name now has to start a few pixels to its right, or from the row
// above's dead space — Shift+click covers the same ground and always did.
// A drag from the handle of an unselected row still moves JUST that row —
// `dragPathsFor`'s own header explains why: the move-drag's snapshot of the
// selection (taken via `useRowDrag`'s `selRef`, itself only current as of the
// LAST render) still excludes the just-pressed row, so it is that function's
// "not in the selection" branch, not `selectOnly`, that keeps the payload a
// single row.
//
// Either way, a press that never travels the sweep's 4px slop is neither
// gesture: it is the press that selects one row (selection's rowPressAction).
// There is exactly ONE threshold for all three outcomes.
export function pressStartsDrag(press: {
  onHandle: boolean;
  rowWasSelected: boolean;
  modified: boolean;
}): boolean {
  return !press.modified && (press.onHandle || press.rowWasSelected);
}

// Whether a press this soon after a navigation-opening release must be read
// as landing on NOTHING, the same way Listing's own OPEN_SUPPRESS_MS window
// (onRowPointerDown) reads it. That guard exists for the habitual second
// press of a double-click into a folder: the release navigates, the listing
// remounts with new rows underneath the same screen position, and the second
// press lands on a row of the NEW folder that nothing has selected or
// highlighted. `onRowPointerDown` returns early for it, doing nothing at all.
//
// The press arbiter (useMarquee) runs in the CAPTURE phase, before that
// bubble-phase guard ever fires, so it has to honour the same window itself —
// otherwise a press the row treats as inert still reaches the arbiter with
// `onHandle: true` and starts a real move-drag of a file the row never
// selected. `path === null` (the background) is excluded: the row-level guard
// has nothing to say about a press that landed on no row at all, and the
// background's own sweep is unaffected by a navigation the background did not
// just cause.
export function pressIsSuppressed(path: string | null, now: number, suppressUntil: number): boolean {
  return path !== null && now < suppressUntil;
}

// What a press on `path` picks up. The standard file-manager rule: a row that
// is part of the current selection drags the WHOLE selection, and a row outside
// it drags only itself.
//
// BOTH branches are live from the listing, and which one runs turns on timing
// that is easy to get backwards. `startMoveDrag` (useRowDrag) is invoked from
// the CAPTURE-phase arbiter (useMarquee), which runs BEFORE the row's own
// bubble-phase pointerdown — the handler that calls `selectOnly` — has fired
// at all. And even once `selectOnly` has run, `useRowDrag`'s `selRef` only
// picks up the new selection on the NEXT render; this function is called
// synchronously, inside the same pointerdown, against the selection as it
// stood before the press. So a handle press on an UNSELECTED row reaches here
// with `selected` still excluding `path`, `selected.includes(path)` is false,
// and the SECOND branch (`[path]`) is what runs — the row's own drag payload,
// matching the highlight the row already carries. The FIRST branch is what
// runs for a press inside an ALREADY-selected row, where `selected` (last
// render's) already contains `path`. Deleting either branch on the belief that
// it is unreachable would make some handle drag carry the wrong payload.
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

// Does a refusal owe the user WORDS, or was it already said by the cursor?
//
// Every refusal above is painted before the release — the no-drop cursor and
// the reject highlight come from this same verdict on every pointermove — with
// exactly one exception: a target that could not say what it was while the
// pointer was over it. Only the sidebar's bookmarks are in that position (the
// server has to answer for them), so they are treated optimistically as folders
// and probed; a release there can flip from "folder" to "file" at the last
// instant, and that is the one refusal a user can reach without seeing it come.
//
// `targetDeclaredKind` is that distinction, and it is the target's OWN
// declaration (row-drag's data-fs-drop-dir) rather than the answer: a listing
// row that declared "0" spent the whole hover wearing the reject highlight, so
// a toast on release would be telling the user something the row already told
// them — one refused drop, said twice.
export function refusalNeedsToast(
  reason: DropRejection,
  targetDeclaredKind: boolean,
): boolean {
  return reason === "not-a-folder" && !targetDeclaredKind;
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
