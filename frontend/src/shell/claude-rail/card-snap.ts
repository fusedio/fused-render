// THE WIDTH MAGNET BETWEEN THE RAIL'S TWO WINDOWS (owner, 2026-11-10: "if two
// modal open, create a magnet alignment to match the width … a guide line or
// helper line shown when within vicinity and it snaps the window to close
// width"). Window A (the list's chat card, the Tasks / Bots card, the +
// popover) and window B (the favourite's card, a bot's card) stand in the
// rail's one column, right edges against the rail; a width drag on either
// that comes within SNAP_PX of the other's width takes the other's width
// exactly, and a guide line marks the left edge they now share.
//
// Window B's box is already published (fav-peek-store `favBox`); window A's
// is published here, by whichever of its cards is up. The guide is state
// here too, drawn as one line on <body> (`setSnapGuide`).
import type { Box } from "./rail-peek-lib";

/** Which window A card published a box — the list's, a door's (by its
 *  class), the + popover's: only one stands at a time, but a closing card's
 *  null must not wipe the opening one's box. */
export type WindowAOwner = string;

/** The guide line: a vertical line at viewport x `x`, from `top` to `bottom`. */
export interface SnapGuide {
  x: number;
  top: number;
  bottom: number;
}

const aBoxes = new Map<WindowAOwner, Box>();
let guide: SnapGuide | null = null;

/** Window A's card's box as `owner` now stands it, or null when it is down. */
export function publishWindowABox(owner: WindowAOwner, box: Box | null): void {
  if (box) aBoxes.set(owner, box);
  else aBoxes.delete(owner);
}

/** Window A's box: the card up (the newest publisher, should two overlap a
 *  frame while one fades). */
export function windowABox(): Box | null {
  let last: Box | null = null;
  for (const b of aBoxes.values()) last = b;
  return last;
}

function sameGuide(a: SnapGuide | null, b: SnapGuide | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.x === b.x && a.top === b.top && a.bottom === b.bottom;
}

/** THE GUIDE LINE, one element on <body> for every rail and card (the cards
 *  live in portals of their own, some in the shell, some in a page): kept
 *  once made, `.is-on` fading it in and out (claude-rail.css
 *  `.rail-snap-guide`), its last place kept through the fade-out. */
let line: HTMLDivElement | null = null;

/** The guide for the drag on now (`snapWidth`'s), or null to fade it out. */
export function setSnapGuide(next: SnapGuide | null): void {
  if (sameGuide(guide, next)) return;
  guide = next;
  // No DOM to draw on (a test, a partial shim): the state alone.
  if (typeof document === "undefined" || typeof document.createElement !== "function") return;
  if (!next) {
    line?.classList.remove("is-on");
    return;
  }
  if (!line || !line.isConnected) {
    line = document.createElement("div");
    line.className = "rail-snap-guide";
    line.setAttribute("aria-hidden", "true");
    document.body.appendChild(line);
  }
  // The line overshoots both cards a little, so it reads as a ruler laid
  // along them rather than a border of either.
  line.style.left = `${next.x}px`;
  line.style.top = `${next.top - GUIDE_OVERSHOOT}px`;
  line.style.height = `${next.bottom - next.top + 2 * GUIDE_OVERSHOOT}px`;
  line.classList.add("is-on");
}

const GUIDE_OVERSHOOT = 6;

/** The guide as it stands (tests). */
export function snapGuide(): SnapGuide | null {
  return guide;
}

/** Test seam. */
export function resetCardSnap(): void {
  aBoxes.clear();
  guide = null;
  line?.remove();
  line = null;
}
