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
// actually gets stored and rendered.
//
// `null` means THIS CONTAINER CANNOT EXPRESS A SPLIT, and the caller must
// neither move the pane nor record anything. Two cases, one rule:
//   • no width at all (unmeasurable, zero-sized);
//   • narrower than both floors together (PANE_MIN_W + LIST_MIN_W = 280px — a
//     panel-split grid, a zoomed-in window). There the clamp returns
//     PANE_MIN_W whatever the cursor does, so the fraction it yields describes
//     the CONTAINER'S narrowness and not the user's choice — at 220px wide it
//     is exactly 1.0, "the pane takes everything", which no wider window can
//     honour: re-opening the folder on a normal screen left the list at its
//     60px sliver, permanently, from one drag in a narrow pane. A number that
//     is not a choice must not be persisted as one, and capping it just below
//     1 would still persist a proportion nobody picked.
// Above 280px the clamp is well-behaved and the fraction can never reach 1 on
// its own: the ceiling is (W − LIST_MIN_W) / W, which is always < 1.
export function dragPaneFrac(containerW: number, rawPx: number): number | null {
  if (!(containerW >= PANE_MIN_W + LIST_MIN_W)) return null;
  return clampPaneWidth(containerW, rawPx) / containerW;
}

// Parse the `panew` viewstate value. It holds a FRACTION of the split
// container ("0.42"); null = nothing saved, so the caller uses
// PANE_DEFAULT_FRAC and treats the width as unchosen.
//
// Anything at or above 1 is ignored as if absent, and the folder re-opens at
// the default until it is dragged again. Two kinds of value land there and
// neither should be honoured:
//   • LEGACY PIXEL widths from the previous model — not translated, because
//     the pixels were measured against a container this window may not have,
//     which is the whole reason for the fraction;
//   • exactly 1, "the pane takes the whole container" — which the drag can no
//     longer produce (see dragPaneFrac) but an older build could, and which
//     leaves the list nothing but its CSS floor on every window.
export function parsePaneFrac(raw: string | null): number | null {
  const f = parseFloat(raw || "");
  if (!Number.isFinite(f) || f <= 0 || f >= 1) return null;
  return f;
}
