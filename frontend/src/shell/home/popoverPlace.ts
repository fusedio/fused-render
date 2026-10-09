// Where the Change popover goes: fixed coordinates that keep it inside the
// visible content area (not the bare viewport), flipping above the anchor when
// it does not fit below and clamping with a scroll as the last resort.

export interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface Placement {
  left: number;
  top: number;
  /** Cap for the popover's own height; it scrolls inside when content is taller. */
  maxHeight: number;
  width: number;
}

const GAP = 6;
const MARGIN = 8;

/**
 * `size` is the popover's natural size (width cap and full content height),
 * `anchor` the element it hangs from, `bounds` the visible content area.
 * `alignLeft` hangs the card from the anchor's left edge instead of its right.
 */
export function placePopover(o: {
  anchor: Box;
  bounds: Box;
  size: { width: number; height: number };
  alignLeft?: boolean;
}): Placement {
  const m = MARGIN;
  const { anchor: a, bounds: b } = o;
  const availW = Math.max(0, b.right - b.left - 2 * m);
  const availH = Math.max(0, b.bottom - b.top - 2 * m);
  const width = Math.min(o.size.width, availW);
  const height = Math.min(o.size.height, availH);

  const wantLeft = o.alignLeft || a.right - width < b.left + m;
  const rawLeft = wantLeft ? a.left : a.right - width;
  const left = Math.max(b.left + m, Math.min(rawLeft, b.right - m - width));

  const below = a.bottom + GAP;
  const above = a.top - GAP - height;
  let top: number;
  if (below + height <= b.bottom - m) top = below;
  else if (above >= b.top + m) top = above;
  else top = Math.max(b.top + m, Math.min(below, b.bottom - m - height));
  return { left, top, maxHeight: availH, width };
}
