// The preview pane's width arithmetic, as pure functions: the split threshold,
// the undragged breakpoints, the pixel clamps, and the divider drag's
// px→fraction step. The stateful half — the hook and the drag — lives in
// pane.ts, and the width a drag produces in pane-store.ts.
//
// There is no parse of a stored width here, and there was: a dragged fraction
// used to be serialised into the per-folder viewstate and read back, so it
// needed validating on the way in (legacy pixel values, a whole-container 1).
// Nothing is stored any more — the width is a module variable for the session —
// so every value this module sees comes straight from its own clamps.
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

// **THE PANE'S WIDTH: 30% of its container, full stop** (D281). Not a function of
// anything — the same share the file view's sidebar takes, imported from there so
// the two cannot drift apart again.
//
// Three pieces of responsive machinery are DELETED here, on the owner's
// instruction ("remove any complicated breakpoint logic"):
//
//   * the 30/50/70 TIERS (`defaultPaneFrac`), stepping on 1000px and 1440px
//     container breakpoints, with a `220/containerW` floor folded in;
//   * `PANE_SPLIT_MIN_W` / `shouldShowPane` — the **700px gate** that decided
//     whether there was a pane AT ALL;
//   * the `ResizeObserver` measurement both of those needed (`useSplitWidth` in
//     pane.ts), and with it the second consumer of the verdict (Preview's
//     `useSplitIsWide`).
//
// What replaces them is nothing, which is the point. The width is a constant and
// the pane's presence is a property of WHICH Listing this is (`paneEnabled` in
// Listing.tsx: not embedded, not a snapshot, not a panel pane) — a question about
// the surface, not about how many pixels it happens to have. The pixel FLOORS
// below stay: they are clamps a drag and the CSS must agree on, not conditions on
// the layout, and they are the only reason a 30% pane on a very narrow window is
// still a usable column.
export { COMPANION_FRAC as PANE_DEFAULT_FRAC } from "@apps/explorer/lib/side-width";

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
//     is not a choice must not be recorded as one, and capping it just below
//     1 would still keep a proportion nobody picked.
// Above 280px the clamp is well-behaved and the fraction can never reach 1 on
// its own: the ceiling is (W − LIST_MIN_W) / W, which is always < 1.
export function dragPaneFrac(containerW: number, rawPx: number): number | null {
  if (!(containerW >= PANE_MIN_W + LIST_MIN_W)) return null;
  return clampPaneWidth(containerW, rawPx) / containerW;
}
