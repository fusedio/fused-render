// FREE PINS: FOUR WINDOWS OFF THE RAIL (owner, 2026-11-18). Behind
// `rail_free_pins_enabled` (rail-free-pins-flag.ts), itself behind the unified
// head's switch: off, every card keeps the rules of rail-pin-lib and
// rail-move-lib as they were.
//
// THE RAIL HAS TWO SECTIONS (owner: "section a which opens window a → between
// bots, tasks, + icon, tasks and git icon. section b → between fav tasks and
// pin bots"), and EACH SECTION HAS TWO SLOTS:
//
//   non-pin  the window a hover or a click opens. It stands beside the rail,
//            always ("non pin window position is fixed next to the rail"), and
//            the two sections' non-pins share the rail's ONE COLUMN, their
//            heights adjusted to fit ("if unpinned we make sure that both
//            windows show in same column, we adjust height etc"). One per
//            section: the next ring's click swaps what it shows.
//   pin      the window the pin icon made. THE PIN IS THE TASK'S, not the
//            window's (until now another ring swapped what a pinned window
//            showed): a click on any other ring opens that task in the
//            section's NON-PIN slot and leaves the pin alone ("when we click
//            on other tasks we open it next to rail in non pin form"). One
//            per section, so four windows at most ("max 4 windows").
//
// A PIN IS FROZEN WHERE IT STOOD ("when we pin it stores that exact state as
// position and height, so even if i close other window it doesn't expand or
// move"; "we pin we don't resize"): the non-pin window's box at the press
// becomes the pin's box — position AND size, kept beside the pin's mark for as
// long as the pin (rail-move-store `commitWindowBox`). It remembers nothing
// from an earlier pin ("we pin we loose memory of size and position"), and
// nothing moves or resizes it afterwards but the reader's own drag ("for
// pinned windows we don't adjust position height width unless non pin windows
// interact, we leave it to user"). Its size is its own ("pin should have loose
// size"): a pin's resize never writes the shared `canvasRailPeek` /
// `canvasRailFav` sizes, which stay the non-pins'.
//
// TWO PINS NEVER PUSH EACH OTHER ("two pins don't push each other, they will
// overlap"): last pressed on top, the guides between them as before.
//
// A NON-PIN PUSHES A PIN OUT OF ITS WAY ("non pin windows will push the
// pins"), ON A CLICK ONLY (owner's answer, 2026-11-18: a hover's preview lies
// over the pins and moves nothing — sweeping the rail must not shake them):
// while a clicked-open non-pin stands, a pin under it is DRAWN to its left,
// clear of it (`pushedPos`). Nothing is stored: the pin's own position — its
// home — is untouched, so when the non-pin closes the pin is drawn at home
// again ("they will be pushed back when non pins are closed by click
// outside"). With no room to its left the pin stays and the non-pin lies over
// it. A pushed pin the reader drags is put where the drag ends: that is its
// home now.
//
// PINNING A NON-PIN REPLACES ITS SECTION'S PIN ("if i do non pin window and
// pin that window it replaces that window and pin start from non pin location
// and size, next to the rail"): the old pin closes. The reader sees it coming
// — the pin icon's hover on a non-pin wears the section's pinned window a red
// border, and the icon's caption names what goes (`pinHint`; a colour alone
// says nothing to a reader who cannot see it).
//
// UNPINNING PUTS THE WINDOW BACK BY THE RAIL (owner's answer, 2026-11-18): it
// becomes its section's non-pin, replacing one that is open — the mirror of
// the rule above.
//
// A RING'S HOVER ROW DIMS ONLY THE NON-PIN (owner's answer: "do this only for
// non pin windows because that is the one going to be replace"): the pin is
// not what a click replaces any more.
//
// Everything here needs no DOM, so each rule is proved on its own
// (rail-slots-lib.test.ts).
import { PEEK_GAP, PEEK_INSET, peekPlacement, type Box, type PeekWish } from "./rail-peek-lib";
import type { Pos, RailWindow, Size } from "./rail-move-lib";

/** A pin's frozen box as the window's mark keeps it: where it stands and —
 *  free pins only — how big it is. A mark with no size is the older format
 *  (a pinned window dragged with the switch off): position alone. */
export interface PinMark {
  pos: Pos;
  size: Size | null;
}

/** The smallest size a mark may say: under it the value is not a card's. */
const MARK_MIN = 40;

/** A stored mark, or null for anything that is not one. */
export function parsePinMark(raw: string | null): PinMark | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as { left?: unknown; top?: unknown; width?: unknown; height?: unknown } | null;
    if (!v || typeof v.left !== "number" || typeof v.top !== "number") return null;
    if (!Number.isFinite(v.left) || !Number.isFinite(v.top)) return null;
    const sized =
      typeof v.width === "number" &&
      typeof v.height === "number" &&
      Number.isFinite(v.width) &&
      Number.isFinite(v.height) &&
      v.width >= MARK_MIN &&
      v.height >= MARK_MIN;
    return {
      pos: { left: v.left, top: v.top },
      size: sized ? { width: v.width as number, height: v.height as number } : null,
    };
  } catch {
    return null;
  }
}

/** The stored form of a frozen box (`parsePinMark` reads it back). */
export function serialisePinBox(box: Box): string {
  return JSON.stringify({
    left: Math.round(box.left),
    top: Math.round(box.top),
    width: Math.round(box.width),
    height: Math.round(box.height),
  });
}

/** Do two boxes cover any of each other, counting a `gap` kept between them? */
export function overlaps(a: Box, b: Box, gap = 0): boolean {
  return (
    a.left < b.left + b.width + gap &&
    b.left < a.left + a.width + gap &&
    a.top < b.top + b.height + gap &&
    b.top < a.top + a.height + gap
  );
}

/** Between a pushed pin and the non-pin that pushed it: the gap the rail's
 *  own cards keep from the rail and from each other. */
export const PUSH_GAP = PEEK_GAP;

/** WHERE A PIN IS DRAWN WITH THE NON-PINS UP (header "A NON-PIN PUSHES A
 *  PIN"): `home` its own position, `size` its size, `docks` the clicked-open
 *  non-pin windows standing now (neither a hover's preview nor another pin is
 *  ever one). A pin clear of them all stands at home. One under a non-pin
 *  moves LEFT — the non-pins stand against the rail, at the right — until it
 *  is clear of every one, the gap kept; a move can land it under the other
 *  non-pin, so it is asked again, at most once per non-pin. With no room left
 *  of them (it would cross the viewport's left edge) it is not moved at all:
 *  it stays at home and the non-pin lies over it. Only `left` ever changes. */
export function pushedPos(home: Pos, size: Size, docks: readonly Box[], gap = PUSH_GAP): Pos {
  if (docks.length === 0) return home;
  let left = home.left;
  for (let i = 0; i <= docks.length; i++) {
    const at = { left, top: home.top, width: size.width, height: size.height };
    const hit = docks.find((d) => overlaps(at, d, gap));
    if (!hit) return left === home.left ? home : { left: Math.round(left), top: home.top };
    left = hit.left - gap - size.width;
    if (left < 0) return home;
  }
  return home;
}

/** The least height the section A non-pin keeps above the section B one
 *  before it gives up the column: under it a chat card shows a head and a
 *  composer and no transcript (rail-peek-lib `peekPlacement` draws down to
 *  120). */
export const COLUMN_MIN_HEIGHT = 160;

/** WHERE SECTION A'S NON-PIN STANDS WITH SECTION B'S UP (header "ONE
 *  COLUMN"; rail-peek-lib `placeClear` is the rule with the switch off):
 *  ABOVE it, in the rail's column, its height cut to what the column leaves —
 *  never beside it. Only when the column leaves less than COLUMN_MIN_HEIGHT
 *  does it stand over it (the last resort `placeClear` has too), rather than
 *  not at all. With no section B non-pin up, or one below the row: the plain
 *  placement. */
export function placeColumn(anchor: Box, rail: Box, row: Box, avoid: Box | null, wish: number | PeekWish): Box | null {
  if (avoid && avoid.top < row.top + row.height) {
    const above = avoid.top - PEEK_GAP - row.top;
    if (above >= COLUMN_MIN_HEIGHT + 2 * PEEK_INSET) {
      return peekPlacement(anchor, rail, { left: row.left, top: row.top, width: row.width, height: above }, wish);
    }
  }
  return peekPlacement(anchor, rail, row, wish);
}

/** WHAT A SECTION'S PIN SHOWS, as its marks spell it (platform/lib last-chat):
 *  section A's one word (`aPinned`: `task:<key>`, `plus`, `tasks`, `bots`,
 *  `git`), section B's two (`favPinned` a task's key, `botOpen` a bot's id). */
export interface PinMarks {
  aWord: string | null;
  favKey: string | null;
  botId: string | null;
}

/** The task key section A's pin shows, or null (no pin, or a door's card). */
export function aPinTask(aWord: string | null): string | null {
  return aWord && aWord.startsWith("task:") && aWord.length > 5 ? aWord.slice(5) : null;
}

/** WHICH PINNED WINDOW ALREADY SHOWS THIS TASK (header "THE PIN IS THE
 *  TASK'S"): a ring's click on a pinned task raises its pin — in whichever
 *  section it was pinned; a starred chat has a ring in both — and opens no
 *  second window on the same chat (one chat, one composer). Null: none. */
export function pinOf(key: string, marks: Pick<PinMarks, "aWord" | "favKey">): RailWindow | null {
  if (aPinTask(marks.aWord) === key) return "a";
  if (marks.favKey === key) return "b";
  return null;
}

/** Is section `win` pinned on anything? */
export function sectionPinned(win: RailWindow, marks: PinMarks): boolean {
  return win === "a" ? marks.aWord !== null : marks.favKey !== null || marks.botId !== null;
}

/** WHAT A RING'S CLICK DOES with free pins on (rail-pin-lib `ringClick` is
 *  the rule with them off):
 *    - "raise": the task is pinned — its pinned window comes to the front
 *               and takes the keyboard; nothing opens;
 *    - "keep":  the ring of the non-pin window that is already open on it;
 *    - "open":  anything else — the section's non-pin window opens on it
 *               (or swaps to it), never pinned: the pin is the task's. */
export type SlotAct = "raise" | "keep" | "open";

export function slotClick(o: { pinned: boolean; ownOpen: boolean }): SlotAct {
  if (o.pinned) return "raise";
  return o.ownOpen ? "keep" : "open";
}

/** THE PIN ICON'S CAPTION (header "PINNING A NON-PIN REPLACES"): on a pinned
 *  window, "Unpin"; on a non-pin whose section has no pin, "Pin"; on one
 *  whose section has, what the press closes — by name when it has one. */
export function pinHint(o: { pinned: boolean; replaces: string | null | false }): string {
  if (o.pinned) return "Unpin";
  if (o.replaces === false) return "Pin";
  const name = (o.replaces ?? "").trim();
  return name ? `Pin — replaces “${name}”` : "Pin — replaces the pinned window";
}

/** WHICH SECTION'S PINNED WINDOW A PIN ICON'S HOVER MARKS (the red border):
 *  the icon is on a NON-PIN card of section `win`, and that section has a pin
 *  — the one its press would close. A pinned window's own icon (its unpin)
 *  closes nothing. */
export function doomedBy(o: { win: RailWindow; cardPinned: boolean; sectionPinned: boolean }): RailWindow | null {
  return !o.cardPinned && o.sectionPinned ? o.win : null;
}
