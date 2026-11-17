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

/** WHERE A HEAD DRAG STARTS FROM (CodeRabbit on #2329): `drawn` the card's
 *  box at the press, `pointerX` the press, `float` the size the card floats
 *  at (the chassis' own, the one its placement draws with) or null when the
 *  drawn box already is that size (a floating card). The drag clamps and
 *  snaps this box, so its first frame lands where the card is drawn and its
 *  guides run along edges the card has. A docked card that floats at another
 *  size keeps its top edge's offset to the pointer and the grabbed spot of
 *  its head row at the same share of the width — the row stays under the
 *  pointer, whatever the width becomes. */
export function dragStartBox(drawn: Box, pointerX: number, float: Size | null, viewport: Size): Box {
  if (!float) return drawn;
  const width = Math.round(Math.max(0, Math.min(float.width, viewport.width)));
  const height = Math.round(Math.max(0, Math.min(float.height, viewport.height)));
  const share = drawn.width > 0 ? (pointerX - drawn.left) / drawn.width : 0;
  return { left: Math.round(pointerX - share * width), top: drawn.top, width, height };
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
 *  wins over the minimums, as the docked drag's row does.
 *
 *  THE START BOX MEETS THE VIEWPORT AS IT IS NOW (CodeRabbit on #2329): a
 *  window shrunk under the drag must not leave a left or top grip keeping a
 *  right or bottom edge past it. Per axis: the edge the grip holds still
 *  follows the pointer; the opposite edge stays where it was while it is
 *  inside the viewport, else at the viewport's edge; an axis no grip holds is
 *  squeezed inside as the card is drawn (`floatPlacement`). */
export function floatResize(edge: MoveEdge, start: Box, dx: number, dy: number, viewport: Size, limits: FloatLimits): Box {
  const clamp = (want: number, min: number, max: number) => Math.round(Math.min(Math.max(want, Math.min(min, max)), max));
  // One axis: `lo`/`len` the start box's, `d` the travel, `view` the
  // viewport's length, `grip` which of its two edges is held (null: none).
  const axis = (
    lo: number,
    len: number,
    d: number,
    view: number,
    min: number,
    max: number,
    grip: "lo" | "hi" | null,
  ): { lo: number; len: number } => {
    if (grip === "lo") {
      const hi = Math.min(lo + len, view);
      const size = clamp(hi - lo - d, min, Math.min(max, hi));
      return { lo: hi - size, len: size };
    }
    if (grip === "hi") {
      // The opposite edge stays while it is inside; a card wholly past the
      // viewport comes back with its minimum in view.
      const at = lo < view ? Math.max(0, lo) : Math.max(0, view - Math.min(min, view));
      return { lo: at, len: clamp(lo + len + d - at, min, Math.min(max, view - at)) };
    }
    const size = Math.min(len, view);
    return { lo: Math.max(0, Math.min(lo, view - size)), len: size };
  };
  const leftGrip = edge === "l" || edge === "tl" || edge === "bl";
  const rightGrip = edge === "r" || edge === "tr" || edge === "br";
  const topGrip = edge === "t" || edge === "tl" || edge === "tr";
  const bottomGrip = edge === "b" || edge === "bl" || edge === "br";
  const x = axis(start.left, start.width, dx, viewport.width, limits.minWidth, limits.maxWidth, leftGrip ? "lo" : rightGrip ? "hi" : null);
  const y = axis(
    start.top,
    start.height,
    dy,
    viewport.height,
    limits.minHeight,
    limits.maxHeight ?? Infinity,
    topGrip ? "lo" : bottomGrip ? "hi" : null,
  );
  return { left: Math.round(x.lo), top: Math.round(y.lo), width: x.len, height: y.len };
}

/** THE EDGE MAGNET (owner, 2026-11-17; design.md "Round 3 rulings" B9:
 *  "overlap allowed, plus an edge magnet: while a window is dragged by its
 *  head, within 12px its edges snap flush to the other window's edges and to
 *  the app window's edges, with the dashed guide. Neither window is resized
 *  by it"). The width magnet's reach (rail-peek-lib SNAP_PX). */
export const EDGE_SNAP_PX = 12;

/** A guide along a snapped edge: a vertical line at `x` from `top` to
 *  `bottom`, or a horizontal one at `y` from `left` to `right` (card-snap
 *  `setSnapGuide`, `setSnapGuideH`). */
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

/** One axis of the magnet: the dragged box's two edges (`lo`, `lo + len`)
 *  against the other card's two (`other`, null: none up) and the viewport's
 *  (0 and `view`, when known). The nearest pair within `px` wins — the
 *  other card's first on a tie; the line is drawn just inside a viewport's
 *  far edge, where it would otherwise fall off the screen. */
function snapAxis(
  lo: number,
  len: number,
  other: { lo: number; len: number } | null,
  view: number,
  px: number,
): { lo: number; line: number; toOther: boolean } | null {
  const mine = [lo, lo + len];
  const targets: { at: number; line: number; toOther: boolean }[] = [];
  if (other) {
    targets.push({ at: other.lo, line: other.lo, toOther: true });
    targets.push({ at: other.lo + other.len, line: other.lo + other.len, toOther: true });
  }
  const pairs: { delta: number; line: number; toOther: boolean }[] = [];
  for (const t of targets) for (const e of mine) pairs.push({ delta: t.at - e, line: t.line, toOther: t.toOther });
  if (Number.isFinite(view)) {
    pairs.push({ delta: 0 - lo, line: 0, toOther: false });
    pairs.push({ delta: view - (lo + len), line: view - 1, toOther: false });
  }
  let best: { delta: number; line: number; toOther: boolean } | null = null;
  for (const p of pairs) {
    if (Math.abs(p.delta) > px) continue;
    if (!best || Math.abs(p.delta) < Math.abs(best.delta)) best = p;
  }
  return best ? { lo: lo + best.delta, line: best.line, toOther: best.toOther } : null;
}

/** WHERE A HEAD DRAG LANDS WITH THE MAGNET: `pos` the dragged box's top-left
 *  as the drag asks (already inside the viewport), `size` its size, `other`
 *  the other window's card while it is up (docked or floating; null: none).
 *  Each axis on its own: within `px`, the nearest of left↔left, left↔right,
 *  right↔left, right↔right (and top/bottom the same) — or a viewport edge —
 *  wins, and the box moves to be flush with it. The result is clamped to the
 *  viewport again; a snap the clamp undid draws no guide. The guides run
 *  along the shared edge over both cards (one card for a viewport edge). */
export function snapEdges(
  pos: Pos,
  size: Size,
  other: Box | null,
  viewport: Size,
  px = EDGE_SNAP_PX,
): { pos: Pos; x: EdgeGuideX | null; y: EdgeGuideY | null } {
  const sx = snapAxis(pos.left, size.width, other ? { lo: other.left, len: other.width } : null, viewport.width, px);
  const sy = snapAxis(pos.top, size.height, other ? { lo: other.top, len: other.height } : null, viewport.height, px);
  const want = { left: sx ? sx.lo : pos.left, top: sy ? sy.lo : pos.top };
  const at = clampPos(want, size, viewport);
  const xOk = !!sx && at.left === Math.round(want.left);
  const yOk = !!sy && at.top === Math.round(want.top);
  // THE GUIDE IS FOR THE OTHER WINDOW'S EDGE ONLY (owner, 2026-11-17): the
  // app window's own edge is its guide, and a tall card pressed against it
  // by the clamp would otherwise draw a line on every drag.
  let x: EdgeGuideX | null = null;
  if (xOk && sx && sx.toOther && other) {
    x = { x: sx.line, top: Math.min(at.top, other.top), bottom: Math.max(at.top + size.height, other.top + other.height) };
  }
  let y: EdgeGuideY | null = null;
  if (yOk && sy && sy.toOther && other) {
    y = { y: sy.line, left: Math.min(at.left, other.left), right: Math.max(at.left + size.width, other.left + other.width) };
  }
  return { pos: at, x, y };
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

