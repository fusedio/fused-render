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
}

interface Rect {
  left: number;
  top: number;
}

export const POPOVER_GAP = 8;
export const POPOVER_INSET = 8;
const MIN_WIDTH = 320;

export function popoverPlacement(
  anchor: Rect,
  beside: Rect,
  want: { width: number; height: number },
  viewport: { width: number; height: number },
): PopoverBox {
  const room = beside.left - POPOVER_GAP - POPOVER_INSET;
  const width = Math.max(0, Math.min(want.width, viewport.width - 2 * POPOVER_INSET, Math.max(room, MIN_WIDTH)));
  const leftWanted = beside.left - POPOVER_GAP - width;
  const left = Math.max(POPOVER_INSET, Math.min(leftWanted, viewport.width - POPOVER_INSET - width));
  const maxTop = viewport.height - POPOVER_INSET - want.height;
  const top = Math.max(POPOVER_INSET, Math.min(anchor.top, maxTop));
  return { left: Math.round(left), top: Math.round(top), width: Math.round(width) };
}
