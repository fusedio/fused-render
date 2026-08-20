// THE SEAM GESTURE, as arithmetic: what a pointer dragging a panel's edge means,
// for every panel in the window that has one. Pure functions, no DOM — the two
// surfaces that own a draggable seam (the global sidebar on the left,
// platform/ui/sidebar/SidebarFrame; the file preview's companion column on the
// right, apps/explorer/PreviewSidebar) each keep their own pointer plumbing and
// read the meaning from here, so "drag past the floor and it shuts" is ONE rule
// with one pair of numbers rather than two implementations that drift.
//
// WHAT CHANGED, AND WHY IT NEEDED A MODULE. Both seams already resized, and both
// clamped at their floor: drag inward past the minimum and the edge simply
// stopped, which says "this is as narrow as it goes" and nothing else. But the
// panel CAN go narrower than its floor — all the way to shut — and the only way
// to say so was a button somewhere else in the chrome. So the floor was lying by
// omission: the gesture that means "less of this" dead-ended a few pixels short
// of the state the user was reaching for. Past the floor now CLOSES.
//
// The whole design is two thresholds and one piece of resistance.

/** The single sign convention every function here speaks: IMPLIED WIDTH — how
    wide the pointer is asking this panel to be, in pixels, measured from the
    panel's own outer edge inward. The callers do the mirroring (a left panel
    grows with clientX, a right panel shrinks with it), so nothing below has to
    know which side of the window it is on. */
export type ImpliedWidth = number;

/** How far PAST the floor the pointer must travel before the panel shuts:
    HALF THE PANEL'S OWN FLOOR, not a constant.
 *
 * The rule is borrowed, deliberately. The Fused workbench reaches the same
 * gesture through `allotment` (VS Code's SplitView), and its snap threshold is
 * `Math.floor(minimumSize / 2)` of travel past the floor — so the two apps'
 * sidebars now resist by the same law, and a hand that learned one has learned
 * the other. What is NOT borrowed is the workbench's reopen, which is a floating
 * chevron rather than a gesture; see `OPEN_PULL`.
 *
 * Scaling with the floor rather than fixing a number is the part worth keeping.
 * The two panels here have different floors (180px for the global sidebar, 280px
 * for the preview's companion column) because they hold different things, and a
 * shared constant would mean the wider panel resisted proportionally less — the
 * bigger the panel, the cheaper it would be to lose. Half the floor keeps the
 * overshoot in proportion to what is at stake: 90px on the sidebar, 140px on the
 * column. Both are past the reach of an accident and inside one continuous drag,
 * which is the whole specification.
 */
export function closeOverdrag(min: number): number {
  return Math.floor(min / 2);
}

/** How far the pointer must pull OFF a shut panel's edge before it opens.
 *
 * 32px flat, and deliberately much smaller than `closeOverdrag`. The workbench's
 * library makes the two symmetric (the same half-min on the way back out) and
 * that is the one part of its behaviour not copied, for two reasons. Its
 * collapsed pane keeps a live sash inside a running drag, so a symmetric
 * threshold there costs nothing; here the shut panel offers a narrow strip the
 * user had to aim at, and aiming at it is already most of the deliberateness a
 * threshold is for. And the two directions are not the same act: closing takes
 * away what is on screen, opening only puts it back. Guarding a restore as
 * heavily as a loss is symmetry for its own sake, and it makes a shut panel feel
 * stuck rather than safe.
 */
export const OPEN_PULL = 32;

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/** WHAT A FINISHED DRAG RECORDS as the panel's remembered width. `outcome` is
    what the last pointermove decided — a width, or `null` for "this gesture shut
    the panel"; `preGesture` is what the panel remembered before the pointer went
    down (itself possibly `null`, meaning "no choice made, use the layout's
    share").
 *
 * CLOSING NEVER RECORDS A WIDTH. That is the rule the whole module is built
 * around and the one that is easiest to lose in the plumbing, because a close is
 * reached by dragging THROUGH the resistance band — so the last width the seam
 * actually rendered is the floor, and a naive "remember where the drag ended"
 * writes that floor down. Shut a 520px column and it would come back at 280.
 * Shutting a panel and narrowing it are different acts; one drag must not do
 * both, so a close hands back exactly what was remembered before the gesture —
 * including `null`, where the answer is the container's share and not any pixel
 * count at all.
 *
 * The global sidebar reaches the same rule a different way (`restoreWidth` is
 * carried into the state it writes mid-drag, SidebarFrame), because it publishes
 * the collapsed state as it happens rather than at pointer-up. Same law, two
 * plumbings; this is the one for a seam that only commits when the pointer lifts.
 */
export function committedWidth(
  outcome: number | null,
  preGesture: number | null
): number | null {
  return outcome === null ? preGesture : outcome;
}

/** WHAT A DRAG ON AN OPEN PANEL'S SEAM MEANS. Answers the width to render, or
    `null` for "this gesture has closed the panel".
 *
 * The middle case is the one worth naming: between the floor and
 * `floor - closeOverdrag(floor)` the panel STICKS at the floor and the cursor walks
 * away from the edge it is holding. That gap is not a dead zone, it is the
 * feature — resistance is how the seam says "you have reached the bottom, and
 * there is one more thing past it". Without it the panel would either snap shut
 * the instant it touched the floor (no floor at all, and every narrow drag ends
 * in an accident) or shrink continuously to nothing (a sidebar 30px wide is a
 * rendering fault, not a layout). The visible stall is what makes the close
 * feel chosen rather than fallen into.
 *
 * Above the floor this is the ordinary clamp both seams already did.
 */
export function resizeWidth(
  implied: ImpliedWidth,
  min: number,
  max: number
): number | null {
  if (implied >= min) return clamp(implied, min, max);
  return implied > min - closeOverdrag(min) ? min : null;
}

/** WHAT A DRAG OFF A SHUT PANEL'S EDGE MEANS. Answers the width to render once
    it has opened, or `null` for "still shut, keep dragging".
 *
 * `closedWidth` is what the panel occupies while shut, and it differs by
 * surface: the global sidebar collapses to a 44px icon RAIL (it is still there,
 * just wearing a different shape), while the preview's companion column goes to
 * 0 and the content takes the room. Passing it in is what lets one function
 * serve both — the pull is measured from where the panel's edge actually is, not
 * from the window frame.
 *
 * On opening, the panel appears at its floor even though the pointer has only
 * travelled `OPEN_PULL`: the clamp does that, and it is right. A panel that grew
 * continuously from 0 would spend the first 150px of the gesture in widths it is
 * never allowed to rest at. It opens at the narrowest LEGAL width and the edge
 * meets the cursor once the cursor has earned it.
 */
export function reopenWidth(
  implied: ImpliedWidth,
  closedWidth: number,
  min: number,
  max: number
): number | null {
  if (implied < closedWidth + OPEN_PULL) return null;
  return clamp(implied, min, max);
}
