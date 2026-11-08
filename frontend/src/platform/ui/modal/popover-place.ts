// WHERE A POPOVER-MODE DIALOG STANDS (Modal `popover`; owner, 2026-11-07: the
// rail's + opens the New task card "like the popover for task chats" — the
// rail's floating chat card, beside the rail, not a centred modal).
//
// To the LEFT of `beside` (the rail's own left edge), a gap away; its top
// level with `anchor` (the + itself); clamped inside the viewport by an inset
// on every side. The width gives way first (never under `MIN_WIDTH`, where it
// would rather overlap the rail than be unusable); the top slides up when the
// card is taller than the room under the anchor. Pure, so it is tested without
// a DOM.

export interface PopoverBox {
  left: number;
  top: number;
  width: number;
  /** Set only for a SIZED card (`popoverPlacement`'s `height`): its height,
   *  clamped inside the viewport. Absent, the card is its content's height. */
  height?: number;
}

interface Rect {
  left: number;
  top: number;
}

export const POPOVER_GAP = 8;
export const POPOVER_INSET = 8;
const MIN_WIDTH = 320;

/** A SIZED CARD (owner, 2026-11-07: the +'s popover "same width/height as
 *  window A"): `height` is the height it asks for, not its content's — it is
 *  given that height, never more than the viewport less an inset on each side,
 *  and its content scrolls inside (deploy.css `.modal-popover .is-sized`). */
export function popoverPlacement(
  anchor: Rect,
  beside: Rect,
  want: { width: number; height: number },
  viewport: { width: number; height: number },
  sized = false,
): PopoverBox {
  const room = beside.left - POPOVER_GAP - POPOVER_INSET;
  const width = Math.max(0, Math.min(want.width, viewport.width - 2 * POPOVER_INSET, Math.max(room, MIN_WIDTH)));
  const leftWanted = beside.left - POPOVER_GAP - width;
  const left = Math.max(POPOVER_INSET, Math.min(leftWanted, viewport.width - POPOVER_INSET - width));
  const height = sized ? Math.max(0, Math.min(want.height, viewport.height - 2 * POPOVER_INSET)) : want.height;
  const maxTop = viewport.height - POPOVER_INSET - height;
  const top = Math.max(POPOVER_INSET, Math.min(anchor.top, maxTop));
  const box: PopoverBox = { left: Math.round(left), top: Math.round(top), width: Math.round(width) };
  return sized ? { ...box, height: Math.round(height) } : box;
}

/** Which grip a resize drag of a sized popover holds (Modal `popover.resize`;
 *  owner, 2026-11-08: the +'s card "is supposed to be window A, but resize is
 *  not working for that modal"): the card's two LEFT corners, or its left, top
 *  or bottom edge — the rail's card's own grips (claude-rail's RailPeek). The
 *  right edge stands against the rail: no grip there. */
export type PopoverEdge = "tl" | "bl" | "l" | "t" | "b";

export interface PopoverLimits {
  minWidth: number;
  minHeight: number;
  maxWidth: number;
}

/** The box a resize drag asks for: `edge` the grip held, `start` the box when
 *  the pointer went down, `dx`/`dy` how far it has moved since. The right edge
 *  stays put (a drag holding a left grip moves the left edge); a drag holding
 *  the top keeps the bottom where it was, one holding the bottom the top, and
 *  one holding no left (`t`, `b`) the width. Width and height are clamped to
 *  the limits and to the viewport less its inset — the viewport wins over the
 *  minimums, so a window with less room than they ask is never overrun. */
export function popoverDrag(
  edge: PopoverEdge,
  start: PopoverBox & { height: number },
  dx: number,
  dy: number,
  viewport: { width: number; height: number },
  limits: PopoverLimits,
  /** A floor (viewport y) the bottom may not pass short of the viewport's
   *  own — window B's card's top, for the rail's popover standing above it
   *  (rail-peek-lib `bottomClear`). */
  maxBottom?: number,
): PopoverBox & { height: number } {
  const clamp = (want: number, min: number, max: number) => Math.round(Math.min(Math.max(want, Math.min(min, max)), max));
  // The right edge as the drag began — or the viewport's inset, if the
  // window narrowed under the drag (CodeRabbit on #2189).
  const right = Math.min(start.left + start.width, viewport.width - POPOVER_INSET);
  const maxWidth = Math.min(limits.maxWidth, right - POPOVER_INSET);
  const width = edge === "t" || edge === "b" ? start.width : clamp(start.width - dx, limits.minWidth, maxWidth);
  const minTop = POPOVER_INSET;
  const floor = Math.min(viewport.height - POPOVER_INSET, maxBottom ?? Infinity);
  let top: number;
  let height: number;
  if (edge === "tl" || edge === "t") {
    const bottom = start.top + start.height;
    height = clamp(start.height - dy, limits.minHeight, bottom - minTop);
    top = bottom - height;
  } else if (edge === "bl" || edge === "b") {
    height = clamp(start.height + dy, limits.minHeight, floor - start.top);
    top = start.top;
  } else {
    height = start.height;
    top = start.top;
  }
  return { left: Math.round(right - width), top: Math.round(top), width, height };
}
