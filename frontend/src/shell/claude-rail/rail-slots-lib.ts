// FREE PINS: WINDOWS OFF THE RAIL (owner, 2026-11-18; MANY PINS, 2026-11-24).
// Behind Rail extras (`rail_extras_enabled`, rail-extras-flag.ts): off, every
// card keeps the rules of rail-pin-lib and rail-move-lib as they were.
//
// THE RAIL HAS TWO SECTIONS (owner: "section a which opens window a → between
// bots, tasks, + icon, tasks and git icon. section b → between fav tasks and
// pin bots"), and EACH SECTION HAS ONE NON-PIN AND ANY NUMBER OF PINS:
//
//   non-pin  the window a hover or a click opens. It stands beside the rail,
//            always ("non pin window position is fixed next to the rail"), and
//            the two sections' non-pins share the rail's ONE COLUMN, their
//            heights adjusted to fit ("if unpinned we make sure that both
//            windows show in same column, we adjust height etc"). One per
//            section: the next ring's click swaps what it shows ("in normal
//            click preview", 2026-11-24 — a plain click never pins).
//   pins     the windows the pin icon made — or a drag of a non-pin's head
//            (owner, 2026-11-24: "if i move the window we pin it, like from
//            the topbar … when i click and drag the whole topbar, that should
//            pin it"). THE PIN IS THE TASK'S, not the window's: a click on any
//            other ring opens that task in the section's NON-PIN and leaves
//            every pin alone ("when we click on other tasks we open it next
//            to rail in non pin form"). AS MANY AS THE READER MAKES (owner,
//            2026-11-24: "we should allow multiple pins"; cap: "No cap"; which
//            cards: "Everything" — any list task, favourite and bot, and the
//            Tasks, Bots, Git, each mode door's and the + New task card, each
//            once). Pinning never closes or replaces another pin (until
//            2026-11-24 a section had one pin, and a pin's press replaced it).
//
// A CARD IS PINNED AT MOST ONCE ("one chat, one window"): a ring's or a door's
// click on what is pinned already RAISES that pinned window and puts the
// keyboard in it (`slotClick` "raise", `pinOf`) — in whichever section it was
// pinned; a starred chat has a ring in both.
//
// A PIN IS FROZEN WHERE IT STOOD ("when we pin it stores that exact state as
// position and height, so even if i close other window it doesn't expand or
// move"; "we pin we don't resize"): the non-pin window's box at the press
// becomes the pin's box — position AND size, kept under the pin's own id for
// as long as the pin (rail-move-store `commitWindowBox`, platform/lib
// last-chat `pinPosRaw`). It remembers nothing from an earlier pin ("we pin
// we loose memory of size and position"), and nothing moves or resizes it
// afterwards but the reader's own drag ("for pinned windows we don't adjust
// position height width unless non pin windows interact, we leave it to
// user"). Its size is its own ("pin should have loose size"): a pin's resize
// never writes the shared `canvasRailPeek` / `canvasRailFav` sizes, which
// stay the non-pins', nor another pin's box. Each pin has its own drag, its
// own resize and its own ×.
//
// PINS NEVER PUSH EACH OTHER ("two pins don't push each other, they will
// overlap"): LAST PRESSED ON TOP, the others keeping their order among
// themselves (rail-move-store `pinOrder`), the guides between them as before.
//
// A NON-PIN PUSHES A PIN OUT OF ITS WAY ("non pin windows will push the
// pins"), ON A CLICK ONLY (owner's answer, 2026-11-18: a hover's preview lies
// over the pins and moves nothing — sweeping the rail must not shake them):
// while a clicked-open non-pin stands, a pin under it is DRAWN clear of it
// (`pushedPos`) — UP in its own column first, cut shorter if it must be
// (owner, 2026-11-20), and to its LEFT when there is no way up. Nothing is
// stored: the pin's own position — its
// home — is untouched, so when the non-pin closes the pin is drawn at home
// again ("they will be pushed back when non pins are closed by click
// outside"). With no room to its left the pin stays and the non-pin lies over
// it. A pushed pin the reader drags is put where the drag ends: that is its
// home now. Every pin at once (`pushedPins`): nearest the rail first, and a
// pin the push moved is in the way of the ones after it.
//
// A PINNED WINDOW CLOSES BY ITS × (owner, 2026-11-19: "unpin should close it as
// well, so instead of unpin filled pin icon it should be a 'x' button"): the
// pin icon's seat, on a pinned window, is a × — the pin goes and the window
// with it, fading out where it stands. (Until then the unpin put it back
// beside the rail as the non-pin.) On a non-pin the icon says "Pin" and
// nothing else: with no cap, a pin's press replaces nothing, and the red
// border and "replaces …" caption that warned of it are gone (2026-11-24).
//
// WITH ANY WINDOW OPEN, A HOVER SAYS THE TITLE ONLY (owner, 2026-11-19: "when
// we have a window already open pinned/non-pinned let's not show the full
// overlay hover effect. rather … darker scrim and showing title tooltip
// thing"): a pin, or a non-pin a click opened, in either section, and no ring,
// face or door previews a card on a hover — a ring shows its row instead.
//
// A PIN GOES WITH ITS RING (owner, 2026-11-19: "when we have a task ring item
// or git that isn't in next view in rail we should close it even if pinned"):
// a view whose rail has no ring for a pinned list task, or no door for a
// pinned Tasks / Bots / Git / mode / + card, closes THAT pin (ClaudeRail),
// every other one staying. A favourite's ring and a bot's face stand in every
// rail, so section B's pins stay.
//
// A RING'S HOVER ROW DIMS ONLY THE NON-PIN (owner's answer: "do this only for
// non pin windows because that is the one going to be replace"): a pin is
// not what a click replaces.
//
// NOTHING ON SCREEN MOVES UNLESS ITS RULE SAYS SO (owner, 2026-11-18: "when i
// click pin the whole window moves … it should be smooth, no flicker,
// jumping"). The pin's press changes a card's ROLE, never the card: a chat
// window is a list of mounted cards of one kind — its free slot and one slot
// per pin — and the press hands the free slot's card the pin's role, a fresh
// free slot taking its place (rail-slots-store "THE SLOTS OF A CHAT WINDOW");
// a door's card and the + pin where they are; and the list's slots are drawn
// by the shell (rail-list-host-store) so a page hop does not remount them.
// So: a pin's press leaves every pixel of its card where it was; its × fades
// it out where it is (useCardMove keeps the box it last drew while its mark
// goes); a push glides, and glides back.
//
// PINNING FREEZES THE WHOLE PICTURE (owner, 2026-11-19: "when we pin it shifts
// back in and overlap happens on them"). A non-pin that is pinned stops
// pushing — and a pin it had pushed aside would slide home, under the new
// pin. So the pin's press freezes every window where it is DRAWN: each pin it
// pushed has its pushed position made its home (rail-move-store
// `freezeFromDock`).
// The list's non-pin shares the rail's column with window B's NON-PIN only
// (owner, 2026-11-19, of a build that kept it short above a pinned favourite
// too: "this limiting on 50% height is bad"): over a pin it stands at its
// full height, and pushes it.
//
// Everything here needs no DOM, so each rule is proved on its own
// (rail-slots-lib.test.ts).
import {
  FAV_HEIGHT,
  FAV_WIDTH,
  PEEK_GAP,
  PEEK_INSET,
  PEEK_MIN_HEIGHT,
  PEEK_MAX_HEIGHT,
  PEEK_WIDTH,
  peekPlacement,
  type Box,
  type PeekWish,
} from "./rail-peek-lib";
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

/** WHERE A PIN WITH NO BOX OF ITS OWN STANDS: a mark the window's storage
 *  kept with no position beside it — a card pinned where it docked before the
 *  switch came on, or a pin's press that found no card to freeze. A pinned
 *  window must never be a mark with nothing drawn (its ring would raise a
 *  window nobody can see, and its unpin would be out of reach), so it takes
 *  the place its section's non-pin has: beside the rail — window A's at the
 *  top of the rail at the list card's default size, window B's on the rail's
 *  foot at the favourite card's — inside the viewport. `rail`: the rail's box,
 *  or null on a page with none (the viewport's right edge then). */
export function defaultPinBox(win: RailWindow, rail: Box | null, viewport: Size): Box {
  const vw = Number.isFinite(viewport.width) ? viewport.width : 1280;
  const vh = Number.isFinite(viewport.height) ? viewport.height : 800;
  const width = Math.min(win === "a" ? PEEK_WIDTH : FAV_WIDTH, Math.max(0, vw - 2 * PEEK_INSET));
  const height = Math.min(win === "a" ? PEEK_MAX_HEIGHT : FAV_HEIGHT, Math.max(0, vh - 2 * PEEK_INSET));
  const right = (rail ? rail.left : vw) - PEEK_GAP;
  const left = Math.max(0, Math.min(right - width, vw - width));
  const railTop = rail ? rail.top : 0;
  const railBottom = rail ? rail.top + rail.height : vh;
  const want = win === "a" ? railTop + PEEK_INSET : railBottom - PEEK_INSET - height;
  const top = Math.max(0, Math.min(want, vh - height));
  return { left: Math.round(left), top: Math.round(top), width: Math.round(width), height: Math.round(height) };
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

/** The least height a pin is shrunk to before it is pushed aside instead: a
 *  chat card's own floor (rail-peek-lib). */
export const PUSH_MIN_HEIGHT = PEEK_MIN_HEIGHT;

/** Where a pushed pin is drawn: its position, and its height when the push
 *  cut it (`pushedPos` "UP FIRST"). No `height`: its own. */
export interface PushedPos extends Pos {
  height?: number;
}

/** WHERE A PIN IS DRAWN WITH THE NON-PINS UP (header "A NON-PIN PUSHES A
 *  PIN"): `home` its own position, `size` its size, `docks` what stands in
 *  its way — the clicked-open non-pin windows (never a hover's preview), and,
 *  for the second of two pins, the first one where its push put it
 *  (`pushedPins`). A pin clear of them all stands at home.
 *  UP FIRST, THEN LEFT (owner, 2026-11-20: "we try to push window up first,
 *  then we push it to left"). A pin a non-pin covers from below — a
 *  favourite's or a bot's window opening at the rail's foot — steps UP in its
 *  own column until it is clear, the gap kept; one too tall to clear it whole
 *  goes to the top (`floor`) and is drawn SHORTER, down to PUSH_MIN_HEIGHT
 *  ("it will remember the size of pin and shrink and push it up … the only
 *  case where it shrinks"). The cut is drawn, never stored: the pin's size is
 *  its own again the moment nothing cuts it.
 *  With no room above — the non-pin stands at the top of the column, or a
 *  second one does — it moves LEFT at its full height, until it is clear of
 *  every one; a move can land it under another, so it is asked again, at most
 *  once per obstacle.
 *  WITH NO ROOM TO CLEAR THEM it goes as far left as the viewport lets it
 *  (owner, 2026-11-19: "otherwise … the user would lose window and task"):
 *  part of it shows beside the non-pin, where staying put left it wholly
 *  covered.
 *  `keepOff`: boxes the step UP must not land on — the other pin, where the
 *  reader did not lay the two over each other (`pushedPins`). Up onto one of
 *  them is no way up: left. */
export function pushedPos(
  home: Pos,
  size: Size,
  docks: readonly Box[],
  gap = PUSH_GAP,
  floor = PEEK_INSET,
  keepOff: readonly Box[] = [],
): PushedPos {
  if (docks.length === 0) return home;
  const own: Box = { left: home.left, top: home.top, width: size.width, height: size.height };
  // COVERED, not merely near (owner, 2026-11-19: a pin beside a window that
  // does not cover it "moves … i don't think so"): only a real overlap
  // pushes; the gap is how far clear the pushed pin then stands.
  const hits = docks.filter((d) => overlaps(own, d));
  if (hits.length === 0) return home;
  // UP: its foot above the LOWEST window that covers it — the one at the
  // rail's foot. One standing higher is then in the way of the step, and
  // sends it left from there (below).
  const foot = Math.max(...hits.map((d) => d.top)) - gap;
  const top = Math.max(Math.min(floor, home.top), Math.min(home.top, foot - size.height));
  const height = Math.min(size.height, foot - top);
  // LEFT keeps its own top — unless there IS room above and only another
  // non-pin stands in it (owner, 2026-11-20, of a pin under the foot window
  // with the top one opened too: "it shifts left + down"): it is then pushed
  // left from where the step up put it, at its own height again.
  let leftTop = home.top;
  // …and at the height the step up left it: it then clears only what stands
  // in the step's way, not the foot window it already stands above (owner,
  // 2026-11-21: "it should only shift how much space was needed by window
  // a").
  let leftHeight = size.height;
  if (height >= Math.min(size.height, PUSH_MIN_HEIGHT)) {
    const up: Box = { left: home.left, top, width: size.width, height };
    const blocked = docks.some((d) => overlaps(up, d));
    if (!blocked && !keepOff.some((d) => overlaps(up, d))) {
      const at: PushedPos = { left: home.left, top: Math.round(top) };
      if (height < size.height) at.height = Math.round(height);
      return at;
    }
    if (blocked) {
      leftTop = Math.round(top);
      leftHeight = height;
    }
  }
  let left = home.left;
  for (let i = 0; i <= docks.length; i++) {
    const at = { left, top: leftTop, width: size.width, height: leftHeight };
    const hit = docks.find((d) => overlaps(at, d));
    if (!hit) break;
    left = hit.left - gap - size.width;
    if (left <= 0) {
      left = Math.min(0, home.left);
      break;
    }
  }
  left = Math.round(left);
  // WHERE IT LANDS IT IS AS TALL AS THE ROOM LETS IT BE, up to its own height
  // ("if it shifts all the way and … there is space below then it can
  // expand"): down to the first non-pin under it in that column.
  let room = size.height;
  for (const d of docks) {
    if (d.left < left + size.width && left < d.left + d.width && d.top >= leftTop + leftHeight) room = Math.min(room, d.top - gap - leftTop);
  }
  const drawn = Math.max(leftHeight, Math.round(room));
  if (left === home.left && leftTop === home.top && drawn === size.height) return home;
  const at: PushedPos = { left, top: leftTop };
  if (drawn < size.height) at.height = drawn;
  return at;
}

/** A pin as the solver reads it: where it stands and how big it is. */
export interface PinAt {
  pos: Pos;
  size: Size;
}

/** EVERY PIN AT ONCE (owner, 2026-11-19, of a pushed pin landing on the other
 *  one: the user "would lose window and task"; any number of pins since
 *  2026-11-24). Each pin is pushed clear of the non-pins as `pushedPos` says —
 *  the one standing nearest the rail first (its right edge furthest right; a
 *  tie in the order given); and a pin that push MOVED is in the way of the
 *  ones after it, so no two end up stacked by the system's own doing. Pins
 *  the reader laid over each other stay as they are ("two pins don't push
 *  each other"): only a pin a non-pin moved pushes, and everything is back
 *  home when the non-pin goes. Keyed by anything — a pin's id; every key
 *  given comes back (null for one given as null). */
export function pushedPins<K extends string>(
  pins: Partial<Record<K, PinAt | null>>,
  docks: readonly Box[],
  gap = PUSH_GAP,
): Record<K, PushedPos | null> {
  const out = {} as Record<K, PushedPos | null>;
  const keys = Object.keys(pins) as K[];
  for (const k of keys) out[k] = null;
  const order = keys
    .filter((w) => !!pins[w])
    .sort((p, q) => pins[q]!.pos.left + pins[q]!.size.width - (pins[p]!.pos.left + pins[p]!.size.width));
  // The pins already placed, each with the box it had at home.
  const placed: { win: K; home: Box; at: Box; moved: boolean }[] = [];
  for (const w of order) {
    const pin = pins[w]!;
    const boxAt = (pos: PushedPos): Box => ({ left: pos.left, top: pos.top, width: pin.size.width, height: pos.height ?? pin.size.height });
    // The other pins, but one the reader laid this one over: a step up must
    // not land this one on any of them.
    const keepOff: Box[] = [];
    for (const o of order) {
      if (o === w) continue;
      const done = placed.find((p) => p.win === o);
      const theirs: Box = done ? done.at : { ...pins[o]!.pos, ...pins[o]!.size };
      const home: Box = done ? done.home : theirs;
      if (!overlaps(boxAt(pin.pos), home)) keepOff.push(theirs);
    }
    let pos = pushedPos(pin.pos, pin.size, docks, gap, PEEK_INSET, keepOff);
    // A PIN THE PUSH MOVED IS IN THIS ONE'S WAY ONLY WHERE IT REALLY LANDS ON
    // IT (owner, 2026-11-19, of two pins stacked one above the other, the
    // lower one clear of the non-pin: "it moves one way off than the other …
    // i don't think so"): the two boxes must truly cover each other — two
    // that merely touch, a gap's width apart, do not — and must not have done
    // so at home, where the reader laid them. A pin nothing covers stays.
    for (const other of placed) {
      if (!other.moved) continue;
      if (overlaps(boxAt(pin.pos), other.home)) continue;
      if (!overlaps(boxAt(pos), other.at)) continue;
      // Asked again from home, the moved pin one more thing in its way.
      pos = pushedPos(pin.pos, pin.size, [...docks, other.at], gap, PEEK_INSET, keepOff);
    }
    out[w] = pos;
    placed.push({ win: w, home: boxAt(pin.pos), at: boxAt(pos), moved: pos !== pin.pos });
  }
  return out;
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

/** WHAT THE PINS SHOW, as their marks spell them (platform/lib last-chat):
 *  section A's words (`aPins`: `task:<key>`, `plus`, `tasks`, `bots`, `git`,
 *  `mode:<m>`), section B's favourites' keys (`favPins`) and bots' ids
 *  (`botPins`). */
export interface PinMarks {
  aWords: readonly string[];
  favKeys: readonly string[];
  botIds: readonly string[];
}

/** The task key a section A pin's word shows, or null (a door's card). */
export function aPinTask(aWord: string | null): string | null {
  return aWord && aWord.startsWith("task:") && aWord.length > 5 ? aWord.slice(5) : null;
}

/** Every task key section A's pins show, in their order. */
export function aPinTasks(aWords: readonly string[]): string[] {
  const out: string[] = [];
  for (const w of aWords) {
    const k = aPinTask(w);
    if (k !== null) out.push(k);
  }
  return out;
}

/** WHICH PINNED WINDOW ALREADY SHOWS THIS TASK (header "A CARD IS PINNED AT
 *  MOST ONCE"): a ring's click on a pinned task raises its pin — in whichever
 *  section it was pinned; a starred chat has a ring in both — and opens no
 *  second window on the same chat (one chat, one composer). Null: none. */
export function pinOf(key: string, marks: Pick<PinMarks, "aWords" | "favKeys">): RailWindow | null {
  if (marks.aWords.includes("task:" + key)) return "a";
  if (marks.favKeys.includes(key)) return "b";
  return null;
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

/** THE PIN ICON'S CAPTION (header "A PINNED WINDOW CLOSES BY ITS ×"): on a
 *  pinned window — its seat is the × there — "Close"; on a non-pin, "Pin".
 *  With no cap a pin's press replaces nothing, so it names nothing it would
 *  close (until 2026-11-24: "Pin — replaces …"). */
export function pinHint(o: { pinned: boolean }): string {
  return o.pinned ? "Close" : "Pin";
}
