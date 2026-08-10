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
// The gesture is split from the row drag by WHERE THE PRESS LANDS, not by any
// arbitration afterwards: the name/icon and any already-selected row start a
// move-drag (the browser's own drag-and-drop, see drag-drop's pressStartsDrag),
// everything else sweeps, and neither can turn into the other mid-gesture. A
// press that never travels MARQUEE_DRAG_SLOP is neither — it is the click the
// listing already had, and it must keep meaning select-this-row /
// open-this-row.
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
  // selected: the caller falls back to leaving the selection alone.
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
// during a marquee drag, in pixels per animation frame. Positive scrolls down.
//
// Proportional inside the edge zone so a sweep that just brushes the edge
// creeps and one pressed into it moves, and CAPPED at the edge rather than
// growing with the overshoot: the pointer leaves the scroller entirely on any
// long sweep, and a step that kept scaling would make the listing fly past
// whatever the user was reaching for.
const EDGE_ZONE = 28;
const EDGE_MAX_STEP = 18;
export function autoScrollStep(y: number, view: { top: number; bottom: number }): number {
  const intoBottom = y - (view.bottom - EDGE_ZONE);
  if (intoBottom > 0) return Math.min(1, intoBottom / EDGE_ZONE) * EDGE_MAX_STEP;
  const intoTop = view.top + EDGE_ZONE - y;
  if (intoTop > 0) return -Math.min(1, intoTop / EDGE_ZONE) * EDGE_MAX_STEP;
  return 0;
}
