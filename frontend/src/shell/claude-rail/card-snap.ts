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
// here too, drawn as lines on <body> (`setSnapGuides`).
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

/** THE GUIDE LINES, elements on <body> for every rail and card (the cards
 *  live in portals of their own, some in the shell, some in a page): kept
 *  once made, `.is-on` fading each in and out (claude-rail.css
 *  `.rail-snap-guide`), its last place kept through the fade-out. One pool
 *  per direction — a drag may stand on several lines at once (owner,
 *  2026-11-18: the guides "top bottom left right middle everywhere"; two
 *  cards of one width share a left and a right edge). */
let guides: SnapGuide[] = [];
let guidesH: SnapGuideH[] = [];
const lines: HTMLDivElement[] = [];
const linesH: HTMLDivElement[] = [];

const GUIDE_OVERSHOOT = 6;

/** A HORIZONTAL GUIDE, for the edge magnet of a head drag (rail-move-lib
 *  `snapEdges`; owner, 2026-11-17): a line at viewport y `y`, from `left` to
 *  `right` — the vertical one's twin (`.rail-snap-guide.is-h`), drawn beside
 *  it when both axes snapped. */
export interface SnapGuideH {
  y: number;
  left: number;
  right: number;
}

function sameV(a: SnapGuide[], b: SnapGuide[]): boolean {
  return a.length === b.length && a.every((g, i) => g.x === b[i].x && g.top === b[i].top && g.bottom === b[i].bottom);
}
function sameH(a: SnapGuideH[], b: SnapGuideH[]): boolean {
  return a.length === b.length && a.every((g, i) => g.y === b[i].y && g.left === b[i].left && g.right === b[i].right);
}

/** Is there a page to draw on (not a test, not a partial shim)? */
function canDraw(): boolean {
  return typeof document !== "undefined" && typeof document.createElement === "function" && !!document.body;
}

/** The `i`th line of a pool, made on first use (and again if something took
 *  it off the page). */
function lineAt(pool: HTMLDivElement[], i: number, cls: string): HTMLDivElement {
  let el = pool[i];
  if (!el || !el.isConnected) {
    el = document.createElement("div");
    el.className = cls;
    el.setAttribute("aria-hidden", "true");
    document.body.appendChild(el);
    pool[i] = el;
  }
  return el;
}

/** THE GUIDES FOR THE DRAG ON NOW — every vertical line (`v`) and every
 *  horizontal one (`h`) the dragged card stands on; empty lists fade them
 *  out. Each line overshoots what it runs along a little, so it reads as a
 *  ruler laid along the cards rather than a border of either. */
export function setSnapGuides(v: readonly SnapGuide[], h: readonly SnapGuideH[]): void {
  const nextV = [...v];
  const nextH = [...h];
  const vSame = sameV(guides, nextV);
  const hSame = sameH(guidesH, nextH);
  if (vSame && hSame) return;
  guides = nextV;
  guidesH = nextH;
  // No DOM to draw on: the state alone.
  if (!canDraw()) return;
  if (!vSame) {
    nextV.forEach((g, i) => {
      const el = lineAt(lines, i, "rail-snap-guide");
      el.style.left = `${g.x}px`;
      el.style.top = `${g.top - GUIDE_OVERSHOOT}px`;
      el.style.height = `${g.bottom - g.top + 2 * GUIDE_OVERSHOOT}px`;
      el.classList.add("is-on");
    });
    for (let i = nextV.length; i < lines.length; i++) lines[i]?.classList.remove("is-on");
  }
  if (!hSame) {
    nextH.forEach((g, i) => {
      const el = lineAt(linesH, i, "rail-snap-guide is-h");
      el.style.top = `${g.y}px`;
      el.style.left = `${g.left - GUIDE_OVERSHOOT}px`;
      el.style.width = `${g.right - g.left + 2 * GUIDE_OVERSHOOT}px`;
      el.classList.add("is-on");
    });
    for (let i = nextH.length; i < linesH.length; i++) linesH[i]?.classList.remove("is-on");
  }
}

/** One vertical guide (the width magnet's, `snapWidth`), or null to fade the
 *  vertical ones out. The horizontal ones stay as they are. */
export function setSnapGuide(next: SnapGuide | null): void {
  setSnapGuides(next ? [next] : [], guidesH);
}

/** One horizontal guide, or null to fade the horizontal ones out. */
export function setSnapGuideH(next: SnapGuideH | null): void {
  setSnapGuides(guides, next ? [next] : []);
}

/** The first vertical guide as it stands (tests). */
export function snapGuide(): SnapGuide | null {
  return guides[0] ?? null;
}

/** The first horizontal guide as it stands (tests). */
export function snapGuideH(): SnapGuideH | null {
  return guidesH[0] ?? null;
}

/** Every guide as it stands (tests). */
export function snapGuides(): { v: SnapGuide[]; h: SnapGuideH[] } {
  return { v: guides, h: guidesH };
}

/** Test seam. */
export function resetCardSnap(): void {
  aBoxes.clear();
  guides = [];
  guidesH = [];
  for (const el of [...lines, ...linesH]) el?.remove();
  lines.length = 0;
  linesH.length = 0;
}
