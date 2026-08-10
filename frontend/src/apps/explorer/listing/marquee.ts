// SWEEP TO SELECT: press on a row's dead space or on the listing background,
// drag, and every row the swept region crosses becomes selected. Pure geometry
// — the pointer wiring is useMarquee.ts.
//
// The region is a HIT TEST, not a picture. Nothing is drawn: the rows light up
// as the pointer crosses them, and that is the whole of the feedback the
// gesture gets (a rubber band over the top of them was tried and removed as
// saying the same thing twice). So `marqueeBox` exists to be intersected with,
// never to be positioned — which is why nothing here knows about pixels on
// screen, only about the scroller's content space.
//
// The gesture is split from the row drag by WHERE THE PRESS LANDS — decided
// once, at pointerdown, from the selection as it stood BEFORE the press
// (drag-drop's pressStartsDrag) and never re-asked. A row that was already
// selected starts a move-drag; EVERYTHING else sweeps — any part of an
// unselected row, and the background. Neither can turn into the other
// mid-gesture. A press that never travels MARQUEE_DRAG_SLOP is neither: it is
// the press that selects one row (selection's rowPressAction), and a double
// press still opens. The same slop decides for the move-drag too, so there is
// one threshold in the listing and not three.
//
// Router-free and DOM-free for the same reason pane-math.ts is: these are the
// only decisions the wiring makes, and a headless test can see them only if
// they import nothing that reads `location` at module init.

export interface Point {
  x: number;
  y: number;
}

export interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

// A row's box in the SCROLLER'S CONTENT coordinates (i.e. including scrollTop),
// not viewport coordinates. Content coordinates are what survive the listing
// scrolling underneath a live drag — the auto-scroll below does exactly that,
// and viewport rects measured at press time would be wrong a frame later.
export interface RowBand extends Box {
  path: string;
}

// How far a press must travel before it is a drag at all. Four pixels is the
// usual system slop: below it every ordinary click wobbles, above it nothing
// the hand meant as a click reaches.
export const MARQUEE_DRAG_SLOP = 4;

// The region two points describe, whichever way round they are. A sweep up and
// to the left has to name the same box as the same sweep down and right, or it
// would have negative extent and intersect nothing.
export function marqueeBox(a: Point, b: Point): Box {
  return {
    left: Math.min(a.x, b.x),
    top: Math.min(a.y, b.y),
    right: Math.max(a.x, b.x),
    bottom: Math.max(a.y, b.y),
  };
}

export function passedDragSlop(from: Point, to: Point): boolean {
  return (
    Math.abs(to.x - from.x) >= MARQUEE_DRAG_SLOP || Math.abs(to.y - from.y) >= MARQUEE_DRAG_SLOP
  );
}

// Half-open overlap: a band owns its top edge and not its bottom one. Rows are
// laid end to end, so a box that stops exactly on the seam between two of them
// must claim one, not both — the same convention that keeps a zero-height drag
// on a boundary from selecting a row above AND below.
function overlaps(box: Box, band: RowBand): boolean {
  const vertical = box.top <= band.bottom && box.bottom >= band.top;
  const horizontal = box.left <= band.right && box.right >= band.left;
  if (!vertical || !horizontal) return false;
  // On the seam (the box collapsed onto a shared edge) the LOWER band wins.
  if (box.bottom === band.top && box.top !== box.bottom) return false;
  return !(box.top === band.bottom && band.top !== band.bottom);
}

// Which rows the region selects, given what was already selected.
//
//   • by default the sweep REPLACES the selection — the gesture starts on dead
//     space, which reads as "start again from here";
//   • with Shift or Cmd/Ctrl held it UNIONS with it, so several sweeps can
//     build one selection.
//
// The union puts the pre-drag paths first: Selection.paths is ordered by when a
// path entered the selection (see selection.ts), and the swept rows are what
// just arrived. A base path with no row on screen is KEPT — the union is over
// the selection the user has, not over what happens to be rendered; pruning
// dead paths is the reconcile's job in useListingSelection.
export function marqueeHits(
  box: Box,
  bands: readonly RowBand[],
  { additive, base }: { additive: boolean; base: readonly string[] },
): string[] {
  // NO BANDS AT ALL is not "the sweep hit nothing", it is "this listing could
  // not be measured" — no rendered row to take geometry from (useMarquee's
  // measureBands), or a row that measured zero-height (rowBands below). Both
  // modules promised the caller would then leave the selection alone, and both
  // were describing code that did the opposite: `[]` fell through to the
  // non-additive return, so `selectPaths([])` collapsed a real selection to
  // nothing on a drag that could never have selected anything.
  //
  // Answered here rather than at the wiring because this is where the two
  // producers of `[]` meet, and the promise is in both their comments.
  if (!bands.length) return [...base];
  const swept = bands.filter((b) => overlaps(box, b)).map((b) => b.path);
  if (!additive) return swept;
  const held = new Set(base);
  return [...base, ...swept.filter((p) => !held.has(p))];
}

// The rows' bands, derived from the ROW MODEL and ONE measured row rather than
// from a DOM read per row. Two reasons, and the second is the load-bearing one:
//   • a folder can hold thousands of rows, and measuring each one on pointer-
//     down is a forced layout the size of the listing;
//   • a windowed/virtualized view has NO NODE AT ALL for the rows currently off
//     screen, and a marquee that auto-scrolls is precisely a gesture that
//     sweeps over them. Geometry taken only from rendered nodes would silently
//     skip every row the user scrolled past.
// The listing's rows are uniform height, so index × height is not an
// approximation of the layout — it is the layout.
export function rowBands(
  paths: readonly string[],
  metrics: { firstTop: number; height: number; left: number; right: number },
): RowBand[] {
  const { firstTop, height, left, right } = metrics;
  // A zero/unmeasurable row height would stack every row on one band, and a
  // one-pixel drag would select the whole folder. Nothing measured, nothing
  // selected — and `marqueeHits` reads the empty result as exactly that, so the
  // selection is left alone rather than cleared.
  if (!(height > 0)) return [];
  return paths.map((path, i) => ({
    path,
    left,
    right,
    top: firstTop + i * height,
    bottom: firstTop + (i + 1) * height,
  }));
}

// How fast the listing scrolls while the pointer sits near (or past) an edge
// during a drag, in pixels per animation frame. Positive scrolls down. Shared
// by the sweep and the row drag, so both feel the same at the edges.
//
// The point and the view are in VIEWPORT coordinates — the pointer's client
// position against the scroller's `getBoundingClientRect()` — unlike the row
// bands above, which are content-space. Scrolling is the one decision here that
// is about where the pointer is ON SCREEN.
//
// Proportional inside the edge zone so a sweep that just brushes the edge
// creeps and one pressed into it moves, and CAPPED at the edge rather than
// growing with the overshoot: the pointer leaves the scroller entirely on any
// long sweep, and a step that kept scaling would make the listing fly past
// whatever the user was reaching for.
//
// THE HORIZONTAL BOUNDS ARE PART OF THE RULE, not the caller's to remember.
// Both gestures pointer-capture, so the pointer keeps steering this loop after
// it has left the listing — parked over the preview pane or the sidebar, and
// incidentally within 28px of the top or bottom edge, the listing scrolled on
// and the sweep kept selecting rows the pointer was nowhere near. The row drag
// had the guard inline at its call site and the sweep did not; two copies of one
// rule is how the second one goes missing, so it lives here, where a third
// caller cannot forget it.
const EDGE_ZONE = 28;
const EDGE_MAX_STEP = 18;
export function autoScrollStep(at: Point, view: Box): number {
  if (at.x < view.left || at.x > view.right) return 0;
  const intoBottom = at.y - (view.bottom - EDGE_ZONE);
  if (intoBottom > 0) return Math.min(1, intoBottom / EDGE_ZONE) * EDGE_MAX_STEP;
  const intoTop = view.top + EDGE_ZONE - at.y;
  if (intoTop > 0) return -Math.min(1, intoTop / EDGE_ZONE) * EDGE_MAX_STEP;
  return 0;
}
