// A PINNED RAIL WINDOW MOVES (owner, 2026-11-17; design.md "Move a pinned rail
// window"). Behind the unified-head switch (`task_head_unified_enabled`): the
// pin only exists with it on, and only a pinned window moves.
//
// A pinned window (A: the list's chat card, the Tasks / Bots / Git cards, the
// + New task card; B: the favourite's card, the bot's card) can be dragged by
// its head to anywhere in the viewport and stays there — FLOATING — until a
// double-click on its head (docks, stays pinned) or its unpin (docks; the temp
// rules then apply). Hover and temp cards never move.
//
// THE GRAB AND THE BOUNDS ARE THE BREAKOUT WINDOW'S (owner, 2026-11-17: "what
// do we follow in breakout window, same logic"; shell/breakout/BreakoutCards
// `FloatingCard`): a press on the head starts the drag unless it landed on a
// control; the card stays fully inside the viewport, 0 inset. Ours also waits
// for a few pixels of travel (`MOVE_THRESHOLD_PX`), so a plain click or a
// double-click on the head never moves it.
//
// THE POSITION IS THE WINDOW'S (owner, 2026-11-17), not the task's or the
// card's: another ring or door of the same window swaps what it shows and
// keeps the floating position. Kept beside the pin marks (platform/lib
// last-chat `windowPosMark`) for exactly as long as the pin.
//
// Everything here needs no DOM, so each rule is proved on its own
// (rail-move-lib.test.ts).
import { PEEK_MAX_HEIGHT, PEEK_MAX_WIDTH, PEEK_WIDTH, type Box, type PeekWish, type ResizeEdge } from "./rail-peek-lib";

/** The rail's two windows: A (the top of the rail) and B (its foot). */
export type RailWindow = "a" | "b";

/** Where a floating window's top-left corner stands (viewport px). */
export interface Pos {
  left: number;
  top: number;
}

export interface Size {
  width: number;
  height: number;
}

/** How far the pointer travels before a head press becomes a drag. */
export const MOVE_THRESHOLD_PX = 4;

/** The viewport as a window reports it. A host without one (a test, a shim)
 *  is unbounded: nothing is clamped against a size nobody knows. */
export function viewportOf(w: { innerWidth?: number; innerHeight?: number } | undefined): Size {
  const width = w && typeof w.innerWidth === "number" && w.innerWidth > 0 ? w.innerWidth : Infinity;
  const height = w && typeof w.innerHeight === "number" && w.innerHeight > 0 ? w.innerHeight : Infinity;
  return { width, height };
}

/** FULLY INSIDE THE VIEWPORT, 0 inset (the breakout's bounds): `left` in
 *  `0 … width - w`, `top` in `0 … height - h`. A card bigger than the
 *  viewport stands at its top-left corner. */
export function clampPos(pos: Pos, size: Size, viewport: Size): Pos {
  return {
    left: Math.round(Math.max(0, Math.min(pos.left, viewport.width - size.width))),
    top: Math.round(Math.max(0, Math.min(pos.top, viewport.height - size.height))),
  };
}

/** Has a head press travelled far enough to be a drag? */
export function pastThreshold(dx: number, dy: number, px = MOVE_THRESHOLD_PX): boolean {
  return dx * dx + dy * dy >= px * px;
}

/** A stored position, or null for anything that is not one (nothing, a
 *  hand-edited value, an older format). */
export function parsePos(raw: string | null): Pos | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as { left?: unknown; top?: unknown } | null;
    if (!v || typeof v.left !== "number" || typeof v.top !== "number") return null;
    if (!Number.isFinite(v.left) || !Number.isFinite(v.top)) return null;
    return { left: v.left, top: v.top };
  } catch {
    return null;
  }
}

/** The stored form of a position (`parsePos` reads it back); null clears. */
export function serialisePos(pos: Pos | null): string | null {
  return pos ? JSON.stringify({ left: Math.round(pos.left), top: Math.round(pos.top) }) : null;
}

/** WHAT A HEAD PRESS MAY NOT GRAB (owner, 2026-11-17, the breakout's "unless
 *  it landed on a control"): the head's buttons and links (a `role="button"`
 *  seat too), a field (the title while it is being renamed is one), an
 *  editable, a menu. */
export const MOVE_CONTROL_SELECTOR =
  'button, a, input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="button"], [role="menu"], [role="menuitem"]';

/** A title whose own double-click renames it (TaskPeekWho, RailPeek's old
 *  head): that double-click is the rename's, not the dock's. */
export const OWN_DBLCLICK_SELECTOR = ".task-side-peek-title, .claude-rail-peek-title";

/** WHAT GRABS: THE HEAD'S FIRST ROW ONLY (owner, 2026-11-17: "instead of whole
 *  header draggable now the colored part 1st of the header is draggable for
 *  pinned chats") — the row a pinned window draws on the hover row's ground
 *  (task-head.css "A PINNED RAIL WINDOW'S FIRST ROW"): the shared head's row 1
 *  (`.task-head-row`: the list's card, a favourite's, the Tasks card, the +)
 *  and a one-row head's row (`.claude-rail-peek-row`: the Bots and Git cards,
 *  a bot's card). The title's row under it does not grab — it selects and
 *  renames. Never a row inside the card's body. */
export const RAIL_HEAD_SELECTOR =
  ":is(.task-head-row, .claude-rail-peek-head > .claude-rail-peek-row:first-child):not(.claude-rail-peek-body *)";

/** The + New task popover's: the same first row, in the chassis' head or the
 *  caller's slot (Modal `head`), never in its body. */
export const POPOVER_HEAD_SELECTOR = ".task-head-row:not(.modal-body *)";

/** The parts of an element a press verdict reads (an Element in the browser;
 *  a stub in a test). */
export interface MoveTarget {
  closest(selector: string): unknown;
}

/** WHERE A PRESS LANDED, for the move: on the head's grab area (`head`), on a
 *  control inside the head (`control`), or not on the head at all. */
export function headPress(el: MoveTarget | null, headSelector: string): "head" | "control" | "none" {
  if (!el || !el.closest(headSelector)) return "none";
  return el.closest(MOVE_CONTROL_SELECTOR) ? "control" : "head";
}

/** A double-click on the head that DOCKS a floating window: on the head's
 *  grab area, and not on a title whose double-click renames it. */
export function headDocks(el: MoveTarget | null, headSelector: string): boolean {
  return !!el && headPress(el, headSelector) === "head" && !el.closest(OWN_DBLCLICK_SELECTOR);
}

/** FLOATING: the window is pinned (held) and has a position. A hover or temp
 *  card is always docked, whatever is stored (owner, 2026-11-17: "hover and
 *  temp cards never move"). */
export function isFloating(o: { movable: boolean; pos: Pos | null }): boolean {
  return o.movable && o.pos !== null;
}

/** The size a card asks for (rail-peek-lib `PeekWish`): its width (the
 *  default card's, under the grips' cap) and its height. */
export function wishSize(wish: PeekWish): Size {
  return {
    width: Math.min(wish.width ?? PEEK_WIDTH, PEEK_MAX_WIDTH),
    height: wish.maxHeight ?? PEEK_MAX_HEIGHT,
  };
}

/** WHERE A FLOATING CARD STANDS: at its position, at the size it asks for
 *  (never bigger than the viewport), clamped fully inside the viewport. Never
 *  null — "no room" does not apply to a floating card (design.md). */
export function floatPlacement(pos: Pos, size: Size, viewport: Size): Box {
  const width = Math.round(Math.max(0, Math.min(size.width, viewport.width)));
  const height = Math.round(Math.max(0, Math.min(size.height, viewport.height)));
  const at = clampPos(pos, { width, height }, viewport);
  return { left: at.left, top: at.top, width, height };
}

/** THE GRIPS OF A FLOATING CARD (owner, 2026-11-17: "same five grips plus
 *  right edge and right corners — the right edge is free now"). The docked
 *  card's five first; corners last so they paint over the edges' ends. */
export type MoveEdge = ResizeEdge | "r" | "tr" | "br";
export const FLOAT_GRIPS: readonly MoveEdge[] = ["l", "r", "t", "b", "tl", "bl", "tr", "br"];
/** The grips only a floating card wears. */
export const RIGHT_GRIPS: readonly MoveEdge[] = ["r", "tr", "br"];

/** One of the docked card's five grips (rail-peek-lib `dragResize`'s). */
export function isDockEdge(edge: MoveEdge): edge is ResizeEdge {
  return !RIGHT_GRIPS.includes(edge);
}

export interface FloatLimits {
  minWidth: number;
  minHeight: number;
  maxWidth: number;
  /** A ceiling on the height (the favourite's card: half the window). */
  maxHeight?: number;
}

/** A FLOATING CARD'S RESIZE: `edge` the grip held, `start` the box when the
 *  pointer went down, `dx`/`dy` the travel since. A left grip moves the left
 *  edge (the right stays), a right grip the right edge; a top grip the top
 *  (the bottom stays), a bottom grip the bottom. The docked card's minimums
 *  and maximums, clamped to the viewport instead of the row — the viewport
 *  wins over the minimums, as the docked drag's row does. */
export function floatResize(edge: MoveEdge, start: Box, dx: number, dy: number, viewport: Size, limits: FloatLimits): Box {
  const clamp = (want: number, min: number, max: number) => Math.round(Math.min(Math.max(want, Math.min(min, max)), max));
  const right = start.left + start.width;
  const bottom = start.top + start.height;
  const leftGrip = edge === "l" || edge === "tl" || edge === "bl";
  const rightGrip = edge === "r" || edge === "tr" || edge === "br";
  const topGrip = edge === "t" || edge === "tl" || edge === "tr";
  const bottomGrip = edge === "b" || edge === "bl" || edge === "br";
  let left = start.left;
  let width = start.width;
  if (leftGrip) {
    width = clamp(start.width - dx, limits.minWidth, Math.min(limits.maxWidth, right));
    left = right - width;
  } else if (rightGrip) {
    width = clamp(start.width + dx, limits.minWidth, Math.min(limits.maxWidth, viewport.width - start.left));
  }
  let top = start.top;
  let height = start.height;
  const cap = limits.maxHeight ?? Infinity;
  if (topGrip) {
    height = clamp(start.height - dy, limits.minHeight, Math.min(cap, bottom));
    top = bottom - height;
  } else if (bottomGrip) {
    height = clamp(start.height + dy, limits.minHeight, Math.min(cap, viewport.height - start.top));
  }
  return { left: Math.round(left), top: Math.round(top), width, height };
}

/** THE EDGE MAGNET (owner, 2026-11-17; design.md "Round 3 rulings" B9:
 *  "overlap allowed, plus an edge magnet: while a window is dragged by its
 *  head, within 12px its edges snap flush to the other window's edges and to
 *  the app window's edges, with the dashed guide. Neither window is resized
 *  by it"). The width magnet's reach (rail-peek-lib SNAP_PX).
 *
 *  …AND THE GUIDES EVERYWHERE (owner, 2026-11-18: "guidelines are not on top
 *  and bottom and in left and right and in middle — it should work all the
 *  time … for both windows … top bottom left right middle everywhere"):
 *    - the MIDDLE is a line too: the dragged box's centre takes the other
 *      card's centre, or the app window's, on either axis;
 *    - the app window's own edges and centre draw their guide (they drew
 *      none), along the whole window;
 *    - every line the box ends up on is drawn, not only the one that pulled
 *      it (two cards of one width share a left AND a right edge);
 *    - a line that would fall past the app window is drawn just inside it;
 *    - a RESIZE has the same magnet on the edge its grip holds
 *      (`snapResize`), floating or docked. */
export const EDGE_SNAP_PX = 12;

/** A guide along a snapped edge: a vertical line at `x` from `top` to
 *  `bottom`, or a horizontal one at `y` from `left` to `right` (card-snap
 *  `setSnapGuides`). */
export interface EdgeGuideX {
  x: number;
  top: number;
  bottom: number;
}
export interface EdgeGuideY {
  y: number;
  left: number;
  right: number;
}

/** One span along an axis: where it starts and how long it is. */
interface Span {
  lo: number;
  len: number;
}

/** A line the dragged box may stand on, on one axis: `at` its coordinate,
 *  `mid` a centre line (met by the box's centre, never by an edge), `view`
 *  the app window's own (else the other card's). */
interface AxisLine {
  at: number;
  mid: boolean;
  view: boolean;
}

/** The lines of one axis: the other card's two edges and its centre, then the
 *  app window's (when its size is known, and — for a move — only while the
 *  box has room to travel on this axis: a card as long as the window is held
 *  against both edges by the clamp, and would draw a line on every drag). */
function axisLines(other: Span | null, view: number, viewOn: boolean): AxisLine[] {
  const lines: AxisLine[] = [];
  if (other) {
    lines.push({ at: other.lo, mid: false, view: false });
    lines.push({ at: other.lo + other.len, mid: false, view: false });
    lines.push({ at: other.lo + other.len / 2, mid: true, view: false });
  }
  if (viewOn && Number.isFinite(view)) {
    lines.push({ at: 0, mid: false, view: true });
    lines.push({ at: view, mid: false, view: true });
    lines.push({ at: view / 2, mid: true, view: true });
  }
  return lines;
}

/** Two coordinates that draw on the same pixel (a centre may sit on a half). */
const LEVEL = 0.5;

/** One axis of the MOVE magnet: the dragged box's edges (`lo`, `lo + len`)
 *  against the edge lines, its centre against the centre lines. The nearest
 *  within `px` wins — the other card's before the app window's on a tie, an
 *  edge before a centre. Null: nothing in reach. */
function snapAxis(lo: number, len: number, lines: AxisLine[], px: number): number | null {
  let best: number | null = null;
  for (const l of lines) {
    for (const mine of l.mid ? [lo + len / 2] : [lo, lo + len]) {
      const delta = l.at - mine;
      if (Math.abs(delta) > px) continue;
      if (best === null || Math.abs(delta) < Math.abs(best)) best = delta;
    }
  }
  return best === null ? null : lo + best;
}

/** THE LINES A BOX STANDS ON, on one axis, once placed: every edge line one
 *  of its `edges` meets and every centre line its centre meets. A centre line
 *  is left out when an edge line of the same owner is met too (two cards of
 *  one size: the two shared edges say it, the middle would be a third line
 *  for the same fact). Each line once, the other card's first. */
function metLines(lo: number, len: number, lines: AxisLine[], edges: number[], centre: boolean): AxisLine[] {
  const met: AxisLine[] = [];
  const has = (l: AxisLine) => met.some((m) => Math.abs(m.at - l.at) <= LEVEL);
  for (const l of lines) {
    if (l.mid) continue;
    if (edges.some((e) => Math.abs(e - l.at) <= LEVEL) && !has(l)) met.push(l);
  }
  if (!centre) return met;
  for (const l of lines) {
    if (!l.mid || Math.abs(lo + len / 2 - l.at) > LEVEL) continue;
    if (met.some((m) => !m.mid && m.view === l.view) || has(l)) continue;
    met.push(l);
  }
  return met;
}

/** Where a line is DRAWN: on its pixel, and just inside the app window when
 *  it is the window's far edge (or the other card's edge standing on it) — a
 *  1px line at `view` is past the last pixel. */
function drawnAt(at: number, view: number): number {
  const px = Math.round(at);
  return Number.isFinite(view) ? Math.max(0, Math.min(px, view - 1)) : px;
}

/** A line's reach across the other axis: over both cards for the other
 *  card's line, the whole app window for the window's own. */
function reach(l: AxisLine, mine: Span, other: Span | null, view: number): { lo: number; hi: number } {
  if (l.view && Number.isFinite(view)) return { lo: 0, hi: view };
  if (!other) return { lo: mine.lo, hi: mine.lo + mine.len };
  return { lo: Math.min(mine.lo, other.lo), hi: Math.max(mine.lo + mine.len, other.lo + other.len) };
}

/** The guides for a box as it stands (`box`), given the lines of both axes
 *  and which of its edges count on each (a move: both and the centre; a
 *  resize: the one edge the grip holds). */
function guidesFor(
  box: Box,
  other: Box | null,
  viewport: Size,
  x: { lines: AxisLine[]; edges: number[]; centre: boolean },
  y: { lines: AxisLine[]; edges: number[]; centre: boolean },
): { x: EdgeGuideX[]; y: EdgeGuideY[] } {
  const mineX: Span = { lo: box.left, len: box.width };
  const mineY: Span = { lo: box.top, len: box.height };
  const otherX: Span | null = other ? { lo: other.left, len: other.width } : null;
  const otherY: Span | null = other ? { lo: other.top, len: other.height } : null;
  const xs = metLines(box.left, box.width, x.lines, x.edges, x.centre).map((l) => {
    const r = reach(l, mineY, otherY, viewport.height);
    return { x: drawnAt(l.at, viewport.width), top: r.lo, bottom: r.hi };
  });
  const ys = metLines(box.top, box.height, y.lines, y.edges, y.centre).map((l) => {
    const r = reach(l, mineX, otherX, viewport.width);
    return { y: drawnAt(l.at, viewport.height), left: r.lo, right: r.hi };
  });
  return { x: xs, y: ys };
}

/** WHERE A HEAD DRAG LANDS WITH THE MAGNET: `pos` the dragged box's top-left
 *  as the drag asks (already inside the viewport), `size` its size, `other`
 *  the other window's card while it is up (docked or floating; null: none).
 *  Each axis on its own: within `px`, the nearest of left↔left, left↔right,
 *  right↔left, right↔right, centre↔centre (and top / bottom / middle the
 *  same) — against the other card or the app window — wins, and the box
 *  moves onto it. The result is clamped to the viewport again. `x` / `y`:
 *  every line the box now stands on (none: no guide), whether the magnet
 *  pulled it there or the drag put it there itself. */
export function snapEdges(
  pos: Pos,
  size: Size,
  other: Box | null,
  viewport: Size,
  px = EDGE_SNAP_PX,
): { pos: Pos; x: EdgeGuideX[]; y: EdgeGuideY[] } {
  const xLines = axisLines(other ? { lo: other.left, len: other.width } : null, viewport.width, size.width < viewport.width);
  const yLines = axisLines(other ? { lo: other.top, len: other.height } : null, viewport.height, size.height < viewport.height);
  const sx = snapAxis(pos.left, size.width, xLines, px);
  const sy = snapAxis(pos.top, size.height, yLines, px);
  const at = clampPos({ left: sx ?? pos.left, top: sy ?? pos.top }, size, viewport);
  const box = { left: at.left, top: at.top, width: size.width, height: size.height };
  const g = guidesFor(
    box,
    other,
    viewport,
    { lines: xLines, edges: [box.left, box.left + box.width], centre: true },
    { lines: yLines, edges: [box.top, box.top + box.height], centre: true },
  );
  return { pos: at, x: g.x, y: g.y };
}

/** A RESIZE'S MAGNET (owner, 2026-11-18: the guides "everywhere"): the edge
 *  the grip holds takes an edge of the other card, or of the app window,
 *  within `px` — each axis on its own, a corner grip both. `resize(dx, dy)`
 *  is the chassis' own drag (rail-peek-lib `dragResize`, `floatResize`) with
 *  every clamp it has; the magnet only ever asks it for a little more or
 *  less travel, so a snap the clamps refuse (a minimum, the row, the gap
 *  over window B) does not happen and draws nothing. The guides: every edge
 *  line the held edge ends up on.
 *
 *  `other` IS THE CARD AS IT STOOD WHEN THE GRIP WENT DOWN — the caller reads
 *  it once, at the press, never per move: a docked window A re-places itself
 *  from window B's box, so a live target would move as the dragged edge
 *  neared it and the magnet would chase it (reviewer, 2026-11-18). */
export function snapResize(
  edge: MoveEdge,
  dx: number,
  dy: number,
  resize: (dx: number, dy: number) => Box,
  other: Box | null,
  viewport: Size,
  px = EDGE_SNAP_PX,
): { box: Box; x: EdgeGuideX[]; y: EdgeGuideY[] } {
  const xGrip = edge === "l" || edge === "tl" || edge === "bl" ? "lo" : edge === "r" || edge === "tr" || edge === "br" ? "hi" : null;
  const yGrip = edge === "t" || edge === "tl" || edge === "tr" ? "lo" : edge === "b" || edge === "bl" || edge === "br" ? "hi" : null;
  const xLines = axisLines(other ? { lo: other.left, len: other.width } : null, viewport.width, true).filter((l) => !l.mid);
  const yLines = axisLines(other ? { lo: other.top, len: other.height } : null, viewport.height, true).filter((l) => !l.mid);
  const heldX = (b: Box) => (xGrip === "lo" ? b.left : b.left + b.width);
  const heldY = (b: Box) => (yGrip === "lo" ? b.top : b.top + b.height);
  const asked = resize(dx, dy);
  // The nearest line to the held edge the chassis' clamps let it reach, as
  // extra travel: a nearer one they refuse does not hide the next in reach.
  const pull = (held: number, lines: AxisLine[], at: (extra: number) => number): number => {
    const deltas = lines
      .map((l) => l.at - held)
      .filter((d) => Math.abs(d) <= px)
      .sort((a, b) => Math.abs(a) - Math.abs(b));
    for (const d of deltas) {
      if (d === 0) return 0;
      if (Math.abs(at(d) - (held + d)) <= LEVEL) return d;
    }
    return 0;
  };
  const ex = xGrip ? pull(heldX(asked), xLines, (extra) => heldX(resize(dx + extra, dy))) : 0;
  const ey = yGrip ? pull(heldY(asked), yLines, (extra) => heldY(resize(dx, dy + extra))) : 0;
  const box = ex || ey ? resize(dx + ex, dy + ey) : asked;
  const g = guidesFor(
    box,
    other,
    viewport,
    { lines: xLines, edges: xGrip ? [heldX(box)] : [], centre: false },
    { lines: yLines, edges: yGrip ? [heldY(box)] : [], centre: false },
  );
  return { box, x: g.x, y: g.y };
}

/** LAST PRESSED ON TOP (owner, 2026-11-17: "overlap allowed, last pressed on
 *  top"): window B already stands over window A (claude-rail.css, 47 over
 *  45/46), so only a window pressed after the other one is raised — and only
 *  while either one floats; docked, the two keep the stacking they had. */
export function raised(o: { win: RailWindow; front: RailWindow | null; anyFloating: boolean }): boolean {
  return o.anyFloating && o.front === o.win;
}

/** WHICH WINDOW A PRESS ON THE RAIL OPENS OR NAMES (owner, 2026-11-17: two
 *  pinned windows may overlap): window B's doors are the favourites' rings and
 *  the pinned bots' faces; every other ring or door — the list's rings, the
 *  Tasks / Bots / Git doors, the + — is window A's. Its press brings that
 *  window to the front, so a window lying under the other is one click on its
 *  own ring away, the way a launcher raises the window it already opened. */
export const WINDOW_B_DOORS = ".claude-rail-favs, .claude-rail-bots";
export const WINDOW_A_DOORS = ".claude-rail-chats, .claude-rail-nav";
export function railPressWindow(el: MoveTarget | null): RailWindow | null {
  if (!el) return null;
  if (el.closest(WINDOW_B_DOORS)) return "b";
  if (el.closest(WINDOW_A_DOORS)) return "a";
  return null;
}

