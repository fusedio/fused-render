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

// **THE COMPANION COLUMN'S SHARE — one rule, both surfaces** (D283, amending
// D282/D281): **30% normally, 50% in a container of 1000px or less.** The file
// view's sidebar and the listing's preview pane read the same function, which is
// why it lives here and `listing/pane-math.ts` imports it rather than spelling its
// own. The owner's words were "they are the same concept now" — after D280/D281 a
// folder's pane is the same companion column a file gets — and two literals are
// exactly how they came to differ (the pane was on 50%, tiering to 70%).
//
// **The small-screen step is BACK, and the argument that removed it was wrong.**
// D282 deleted it claiming "the FLOOR below already says so", i.e. that at 720px
// the 280px floor reached the same answer a 50% step would. **It does not: 280 of
// 720 is 39%, not 50%** — the floor stops a column being unusably narrow, it does
// not give a cramped layout the half it wants. The owner reported the gap from the
// case that shows it, a small browser window, where a 30% companion is a column you
// cannot read beside content that has little to show at that width either.
//
// **This is one step, not the tier ladder that went with it.** What D282 deleted
// and must stay deleted: the 30/50/70% ladder on 1000px/1440px breakpoints, the
// constants behind it, `defaultPaneFrac`, and — separately — the 700px
// `shouldShowPane` gate. **This changes the pane's SHARE and never whether it
// exists**; a small container still gets a pane, now a usable one. *The step below
// reuses the ladder's FIRST BOUNDARY (1000px) and none of its behaviour: one
// comparison with two outcomes, where the ladder had two comparisons, three
// outcomes and a floor folded into the fraction. Sharing a number with something
// deleted is not the same as reviving it — and picking the boundary this codebase
// already reasoned about beats inventing a third one.*
//
// **The threshold is 1000px** — raised from 720 on the owner's "make it 50% for a
// viewport a bit bigger than the current one", looking at a window that was getting
// 30% and wanted half. 1000 is not a fresh invention: it is the old `PANE_MID_W`,
// the first rung of the deleted tier ladder, so the boundary is one this codebase
// already had. **1000 itself IS small** (`<=`), matching CSS `(max-width: 1000px)`
// semantics. *It was 720 for one commit — `SMALL_W`'s old value, chosen because
// `preview.css` calls that width narrow. That coincidence is worth less than the
// owner's actual window, so the two numbers no longer agree and this one is right
// for this question: how much of a container a companion column should take.*
//
// An UNMEASURED container (0, NaN — detached, `display:none`, or a state
// initialiser running before layout) is deliberately NOT small: 30% is the general
// case, and guessing 50% would open a wide window's column at half and snap it to a
// third on the first real measurement.
export const COMPANION_FRAC = 0.3;
export const COMPANION_SMALL_FRAC = 0.5;
export const COMPANION_SMALL_W = 1000;

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

// WHAT THE COLUMN SHOULD BE ON A CONTAINER RESIZE — the whole of the ResizeObserver's
// rule, here rather than inside the component so the two things it has to get right
// at once can be stated and tested:
//
//   never starve the content column   the column gives way when the window does,
//                                     down to the CONTENT_MIN_W floor.
//   never lose the dragged width      `chosen` (lib/side-store, null when nothing
//                                     was dragged) is the STANDING choice, so
//                                     widening the window back returns to it.
//
// It was one-way before — `w > max ? max : w`, narrowing only — and that made the
// rendered width and the stored one disagree: a window narrowed and widened again
// left the column narrow while the store still held the dragged number, and the next
// file→file remount (which re-seeds from the store) snapped it back. Reading the
// store here is what makes the two agree at every width, and it is why this takes
// `chosen` rather than just the current width.
//
// NOT a "fill the room" rule: with nothing dragged, `current` is the width the
// layout measured (`defaultSideWidth` at mount) and a wider window keeps it —
// widening never re-applies the share, which is the deliberate posture the mount-only
// layout effect already had.
//
// A container with no room for BOTH floors is left alone entirely: the CSS
// min-widths hold there, and acting would describe the container rather than a
// choice (the listing pane's rule, FS-12). An unmeasured container (0, NaN) is that
// same case.
export function clampSideWidth(
  current: number,
  chosen: number | null,
  containerW: number
): number {
  const max = containerW - CONTENT_MIN_W;
  if (!(containerW > 0) || max < MIN_W) return current;
  return Math.max(MIN_W, Math.min(chosen ?? current, max));
}

// THE WIDTH THE COLUMN OPENS AT, decided ONCE against the measured container —
// `null` for "the container cannot be measured, keep whatever the caller seeded".
//
// Both inputs to the question in one place: a dragged width from an earlier view in
// this document (lib/side-store) if there is one, else the container's share. The
// difference from reading the store directly is the CLAMP, and it is a pre-paint
// bug fix: the mount effect used to return early whenever a stored width existed, so
// the column painted at the raw stored pixels and only met the floors in the
// post-paint resize effect. Drag wide, navigate to a folder (the column unmounts),
// shrink the window, open a file — and the sidebar overflowed the split for a frame
// before snapping back. A width chosen in one window is not a width that fits in the
// next one.
//
// It never WIDENS a stored width to the share: the drag is the standing answer to
// "how wide", and the share only answers it when nothing was dragged. And it writes
// nothing back to the store — opening in a smaller window is not the user choosing a
// narrower column, the same rule the resize clamp follows.
export function openingSideWidth(chosen: number | null, containerW: number): number | null {
  if (!(containerW > 0)) return null;
  return chosen === null ? defaultSideWidth(containerW) : clampSideWidth(chosen, chosen, containerW);
}
