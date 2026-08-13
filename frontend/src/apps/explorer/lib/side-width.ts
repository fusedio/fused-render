// The preview SIDEBAR's width arithmetic, as pure functions: the floors, the
// small-screen breakpoint, and the width an unsized column opens at.
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

// The small-screen breakpoint, in container pixels. 720px is preview.css's own
// narrow-window breakpoint (the one that drops the rev badge's note), so the
// sidebar calls "small" exactly what the rest of the preview chrome already
// calls small, instead of introducing a second, nearby number that would make
// the preview area change shape twice on the way down.
export const SMALL_W = 720;

// A normal window gives the companion a third; a small one gives it half,
// because on a narrow container a third is not a usable column — 30% of 720px
// is 216px, under the floor CSS enforces anyway — and the thing the sidebar is
// beside has less to show at that width too.
export const DEFAULT_FRAC = 0.3;
export const SMALL_FRAC = 0.5;

// The share of a container this wide the column opens at. `<=` matches CSS
// media-query semantics: `(max-width: 720px)` includes 720.
export function defaultSideFrac(containerW: number): number {
  return containerW <= SMALL_W ? SMALL_FRAC : DEFAULT_FRAC;
}

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
  return Math.round(Math.min(max, Math.max(MIN_W, containerW * defaultSideFrac(containerW))));
}
