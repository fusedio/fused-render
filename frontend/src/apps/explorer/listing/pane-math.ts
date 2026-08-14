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
export const PANE_DEFAULT_FRAC = 0.5;

// The width breakpoints the UNDRAGGED split steps through: 30% of a container
// that only just has room for two panes, 50% at a normal window, 70% once the
// window is wide. A single 50% for every width was wrong at both ends — half of
// 720px is a preview too narrow to read beside a listing that has had to shed
// its columns for it, and half of a 1920px window is 960px of file names.
//
// STEPS, not a continuous ramp: the pane is a persistent piece of furniture, so
// its width wants to be predictable and recognisable ("this is the wide
// layout"), and a fraction that slides with every pixel of a window drag is
// neither. The tiers are the standard laptop/desktop widths.
export const PANE_MID_W = 1000;
export const PANE_WIDE_W = 1440;

// The width at which the listing splits. Above it the pane is there; below it
// the listing has the container to itself.
//
// Comfortably above the 280px both floors technically fit in (PANE_MIN_W +
// LIST_MIN_W), because "can it be laid out" was never the question — "is a
// half-width listing beside a half-width preview still worth reading" is. At
// 700px both halves are around 350px: a listing that still shows a name and a
// size, and a preview big enough to recognise what it is showing. Under it,
// splitting produces two panes that are each too small to do their job, which
// is why the pane used to need a toggle at all.
export const PANE_SPLIT_MIN_W = 700;

// Whether a container of this width shows the preview pane. The WHOLE of the
// pane's on/off decision: there is no user toggle any more, no URL param and
// no saved on/off state — the split is a property of the room available, so it
// is right on a wide window, right in a narrow embed, and right the moment
// either is resized, with nothing to restore and nothing to get stale.
//
// A NaN width (an unattached or display:none container) reads as "no" via the
// comparison, which is the safe answer: painting a pane on a guess and tearing
// it away on the first real measurement is the one visible failure here.
export function shouldShowPane(containerW: number): boolean {
  return containerW >= PANE_SPLIT_MIN_W;
}

// The fraction an UNDRAGGED pane takes in a container this wide (above), used
// for rendering and never recorded — pane-store holds only a fraction the user
// chose by dragging, so the session never remembers a number the layout picked
// for it, and a pane with no chosen width keeps following the window.
//
// Floored at the pane's own PANE_MIN_W: at the narrow end 30% is under the
// 220px floor CSS enforces anyway (0.3 × 700 = 210), and a flex-basis the
// min-width silently overrides is a layout that disagrees with its own
// arithmetic — the divider would sit where the fraction says it doesn't.
//
// An unmeasured container (0, NaN — see shouldShowPane) has no tier to pick, so
// it answers PANE_DEFAULT_FRAC. Nothing renders a pane at that width, and the
// first real measurement arrives before paint (useLayoutEffect, pane.ts).
export function defaultPaneFrac(containerW: number): number {
  if (!(containerW > 0)) return PANE_DEFAULT_FRAC;
  const step = containerW >= PANE_WIDE_W ? 0.7 : containerW >= PANE_MID_W ? PANE_DEFAULT_FRAC : 0.3;
  return Math.max(step, PANE_MIN_W / containerW);
}

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
