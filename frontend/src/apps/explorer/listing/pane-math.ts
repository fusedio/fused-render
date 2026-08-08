// The preview pane's width arithmetic, as pure functions: the pixel clamps,
// the divider drag's px→fraction step, and the `panew` parse. The stateful
// half — the hook, the URL writes, the viewstate — lives in pane.ts.
//
// It is a separate module for a testability reason, not a tidiness one:
// pane.ts imports @platform/lib/router, which reads `location` at MODULE INIT
// (its embed-prefix constant), so merely importing pane.ts in a DOM-free bun
// test throws. Splitting the math out is the cleaner of the two fixes —
// deferring the router's read would make a genuinely load-time constant lazy
// for every caller in the app to suit a test, whereas this arithmetic never
// wanted the router in the first place.
//
// Width is a FRACTION of the split container, never a pixel count: that is
// what keeps the pane proportional when the window resizes (see pane.ts).
// Pixels appear here only as the floors, and only for the duration of a drag.

const PANE_MIN_W = 220;
const LIST_MIN_W = 60;
export const PANE_DEFAULT_FRAC = 0.5;

// The one place the pixel clamps live, so the drag cannot disagree with the
// CSS floors: the pane keeps at least PANE_MIN_W, and the list keeps at least
// LIST_MIN_W (a sliver — the columns shed themselves via container queries as
// it narrows). PANE_MIN_W is applied last: in the degenerate case (a container
// too small for both minimums) the pane keeps its floor and the list scrolls.
// CSS mirrors both floors (.listing-pane-slot / .listing-main min-width),
// which is what holds them on a window resize — the stored fraction is
// deliberately proportional and knows nothing about pixels.
export function clampPaneWidth(containerW: number, width: number): number {
  return Math.max(PANE_MIN_W, Math.min(containerW - LIST_MIN_W, width));
}

// The divider drag, in one pure step: the cursor's distance from the
// container's right edge is the pane's wanted PIXEL width, clamped by the
// shared floors and then divided back out into the fraction that is what
// actually gets stored and rendered. A container with no width (unmeasurable,
// zero-sized) has no meaningful fraction, so the caller keeps what it had.
//
// The final `min(1, …)` is the degenerate container again, seen from the
// fraction's side: a container NARROWER than PANE_MIN_W (a panel-split grid, a
// zoomed-in window) makes the clamp's floor bigger than the container itself,
// which would produce a fraction above 1 — a `flexBasis: 110%` the CSS floor
// then silently corrects, and a `panew` value the next open would reject as a
// legacy pixel width. 1 is the honest answer: the pane wants the whole
// container, and .listing-main's own min-width is what still holds the list
// on screen.
export function dragPaneFrac(containerW: number, rawPx: number): number | null {
  if (!(containerW > 0)) return null;
  return Math.min(1, clampPaneWidth(containerW, rawPx) / containerW);
}

// Parse the `panew` viewstate value. It holds a FRACTION of the split
// container ("0.42"); null = nothing saved, so the caller uses
// PANE_DEFAULT_FRAC and treats the width as unchosen.
//
// Values greater than 1 are LEGACY PIXEL widths from the previous model and
// are ignored as if absent — not translated, because the pixels were measured
// against a container this window may not have (that mismatch is the whole
// reason for the fraction), and the folder simply re-opens at the default
// until the user drags it again.
export function parsePaneFrac(raw: string | null): number | null {
  const f = parseFloat(raw || "");
  if (!Number.isFinite(f) || f <= 0 || f > 1) return null;
  return f;
}
