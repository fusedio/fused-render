// A RAIL CARD ON EITHER SIDE (owner, 2026-11-27: "Left sidebar preview" — the
// collapsed LEFT sidebar's icons open the Claude rail's window A card, the
// same card, standing to the RIGHT of that rail). The placement maths in
// rail-peek-lib is written for the Claude rail, whose cards stand to its
// LEFT. Rather than a second copy of it, a left card's boxes are MIRRORED
// about the viewport's width (`left' = W - left - width`), handed to the very
// same functions, and the answer mirrored back. What a mirror cannot express
// (nothing so far) would get its own function here.
//
// Pure: no DOM, so each rule is proved without a browser (rail-side-lib.test).
import { clampAnchor, inSafeCone, placeClear, type Box, type PeekWish, type Point } from "./rail-peek-lib";

/** Which side of its rail a card stands on: `right` is the Claude rail's
 *  (the card left of the rail at the window's right edge), `left` the left
 *  sidebar's (the card right of the rail at the window's left edge). Named
 *  for the RAIL, as DoorCard's `side` prop is. */
export type RailSide = "right" | "left";

/** `box` mirrored about a viewport `width` wide. Its own inverse. */
export function mirrorBox(box: Box, width: number): Box {
  return { left: width - box.left - box.width, top: box.top, width: box.width, height: box.height };
}

/** `p` mirrored about a viewport `width` wide. */
export function mirrorPoint(p: Point, width: number): Point {
  return { x: width - p.x, y: p.y };
}

/** WHERE A CARD STANDS BESIDE ITS RAIL, either side: `placeClear` (the
 *  Claude rail's own placement, window B's box `avoid` kept clear) — for the
 *  left side on the mirrored boxes, with nothing to avoid (a left card keeps
 *  clear of no other window). `scroller`: the box of the scroll run the door
 *  stands in, if any — a door scrolled out of it anchors at the box's edge
 *  (rail-peek-lib `clampAnchor`). Null when the row leaves no room. */
export function placeBeside(
  side: RailSide,
  o: { door: Box; rail: Box; row: Box; avoid: Box | null; wish: number | PeekWish; scroller?: Box | null; viewportWidth: number },
): Box | null {
  const anchor = clampAnchor(o.door, o.scroller ?? null);
  if (side === "right") return placeClear(anchor, o.rail, o.row, o.avoid, o.wish);
  const W = o.viewportWidth;
  const at = placeClear(mirrorBox(anchor, W), mirrorBox(o.rail, W), mirrorBox(o.row, W), null, o.wish);
  return at ? mirrorBox(at, W) : null;
}

/** THE SAFE CONE from the door to the card's near edge, either side
 *  (rail-peek-lib `inSafeCone`, mirrored for the left). */
export function inSafeConeBeside(side: RailSide, p: Point, apex: Point, card: Box, viewportWidth: number): boolean {
  if (side === "right") return inSafeCone(p, apex, card);
  const W = viewportWidth;
  return inSafeCone(mirrorPoint(p, W), mirrorPoint(apex, W), mirrorBox(card, W));
}

/** How far the bridge stands off the rail's edge (DoorCard's 3px). */
export const BRIDGE_INSET = 3;

/** THE BRIDGE over the gap between the rail's near edge and the card (a
 *  pointer resting in it is "on the card"), or null where the card touches
 *  the rail or stands past it. `railEdge`: the x of the rail's edge the card
 *  faces — its left for the Claude rail, its right for the left sidebar. */
export function bridgeBox(side: RailSide, box: Box, railEdge: number): Box | null {
  if (side === "right") {
    const right = box.left + box.width;
    return railEdge > right + 1
      ? { left: right, top: box.top, width: Math.max(0, railEdge - BRIDGE_INSET - right), height: box.height }
      : null;
  }
  const from = railEdge + BRIDGE_INSET;
  return box.left > railEdge + 1 ? { left: from, top: box.top, width: Math.max(0, box.left - from), height: box.height } : null;
}
