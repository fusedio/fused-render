// The preview SIDEBAR's width arithmetic, as pure functions: the floors, the one
// share both companion columns take, and the width an unsized column opens at.
//
// Split out of PreviewSidebar.tsx for the same reason listing/pane-math.ts is
// split out of pane.ts — the arithmetic is the part worth testing, and a
// DOM-free bun test cannot import a component module.
//
// Nothing here is persisted. The column opens at the width this module computes
// EVERY time, and a drag only lasts as long as the page does: the width is a
// property of the room available, not a preference to be remembered, so a
// refresh gets the layout's answer rather than a number chosen for a window
// size the user may no longer be at.

// Floors, in PIXELS — unlike the listing's preview pane (a fraction of its
// container, listing/pane-math.ts) this is a sidebar: what it holds is a chat
// composer and a message column, whose legibility is a width in pixels and not
// a share of the window. The content pane keeps whatever is left, with its own
// floor so neither the default nor a drag can swallow it.
export const MIN_W = 280;
export const CONTENT_MIN_W = 320;

// **THE COMPANION COLUMN'S SHARE, and there is exactly one of it** (D280): 30%,
// for the file view's sidebar and for the listing's preview pane alike, which is
// why the constant lives here and `listing/pane-math.ts` imports it rather than
// spelling its own. The owner's words were "they are the same concept now" — after
// D278/D279 a folder's pane is the same companion column a file gets — and two
// literals are exactly how they came to differ (the pane was on 50%, tiering to
// 70% on a wide window).
//
// **No breakpoint decides it.** A `SMALL_FRAC` of 50% used to apply at or below a
// 720px container, on the argument that 30% of 720 is 216px and not a usable
// column. True, and the FLOOR below already says so — it answers 280px there — so
// the step was a second rule reaching the same place. It is gone with the pane's
// tiers; the floors are clamps on what a share is worth, not conditions on what
// the share IS.
export const COMPANION_FRAC = 0.3;

// The width an unsized column opens at inside a container this wide, with both
// floors applied: never below MIN_W, and never so wide that the content column
// drops under CONTENT_MIN_W.
//
// When the container has no room for both floors at once there is no split to
// express, so this answers MIN_W and the CSS min-widths take it from there —
// the same "no room for both floors" path the resize clamp leaves alone.
//
// An unmeasured container (0, NaN — a detached or display:none element) answers
// MIN_W as well: the caller is expected to fall back to the viewport before
// getting here, and a guessed-wide column that snaps narrow on the first real
// measurement is the one visible failure worth avoiding.
export function defaultSideWidth(containerW: number): number {
  if (!(containerW > 0)) return MIN_W;
  const max = containerW - CONTENT_MIN_W;
  if (max < MIN_W) return MIN_W;
  return Math.round(Math.min(max, Math.max(MIN_W, containerW * COMPANION_FRAC)));
}
