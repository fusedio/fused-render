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

// **THE COMPANION COLUMN'S SHARE — one rule, both surfaces** (D282, amending
// D281/D280): **30% normally, 50% in a container of 720px or less.** The file
// view's sidebar and the listing's preview pane read the same function, which is
// why it lives here and `listing/pane-math.ts` imports it rather than spelling its
// own. The owner's words were "they are the same concept now" — after D279/D280 a
// folder's pane is the same companion column a file gets — and two literals are
// exactly how they came to differ (the pane was on 50%, tiering to 70%).
//
// **The small-screen step is BACK, and the argument that removed it was wrong.**
// D281 deleted it claiming "the FLOOR below already says so", i.e. that at 720px
// the 280px floor reached the same answer a 50% step would. **It does not: 280 of
// 720 is 39%, not 50%** — the floor stops a column being unusably narrow, it does
// not give a cramped layout the half it wants. The owner reported the gap from the
// case that shows it, a small browser window, where a 30% companion is a column you
// cannot read beside content that has little to show at that width either.
//
// **This is one step, not the tier ladder that went with it.** What D281 deleted
// and must stay deleted: the 30/50/70% ladder on 1000px/1440px breakpoints, the
// constants behind it, `defaultPaneFrac`, and — separately — the 700px
// `shouldShowPane` gate. **This changes the pane's SHARE and never whether it
// exists**; a small container still gets a pane, now a usable one.
//
// The threshold is 720px, the value the deleted `SMALL_W` carried, kept rather than
// re-invented: it is also `preview.css`'s own narrow breakpoint, so the preview
// chrome and this column call the same width small. **720 itself IS small** (`<=`),
// matching CSS `(max-width: 720px)` semantics.
//
// An UNMEASURED container (0, NaN — detached, `display:none`, or a state
// initialiser running before layout) is deliberately NOT small: 30% is the general
// case, and guessing 50% would open a wide window's column at half and snap it to a
// third on the first real measurement.
export const COMPANION_FRAC = 0.3;
export const COMPANION_SMALL_FRAC = 0.5;
export const COMPANION_SMALL_W = 720;

export function companionFrac(containerW: number): number {
  return containerW > 0 && containerW <= COMPANION_SMALL_W
    ? COMPANION_SMALL_FRAC
    : COMPANION_FRAC;
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
  return Math.round(Math.min(max, Math.max(MIN_W, containerW * companionFrac(containerW))));
}
