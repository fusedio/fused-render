// RAIL CARDS: HOVER / TEMP PIN / PIN (owner, 2026-11-16; design.md "Rail
// cards"). Behind the unified-head switch (`task_head_unified_enabled`,
// shell/task-head-flag): off, every card keeps the rules it had.
//
// Every card off the rail — window A (a task ring, a draft ring, the +, Tasks,
// Bots, Git) and window B (a favourite's ring, a bot's ring) — is in one of
// four states:
//
//   closed   nothing up;
//   hover    a preview: goes when the pointer leaves the ring, the card and
//            the cone between them;
//   temp     a click made it (a ring's click, any press inside the card): it
//            stays through the pointer leaving, and ONE press outside closes it;
//   pinned   the pin icon (or a double-click on the ring) made it: outside
//            presses, Escape and its own ring's click are ignored; only the
//            pin icon (back to temp) lets it go — or its Open (a hand-off).
//
// THE PIN IS THE WINDOW'S, NOT THE CARD'S (owner, 2026-11-16: "when we pin we
// are pinning the window rather than the task … window A/B will not go away
// unless clicked on the unpin action"): another ring or door of a pinned
// window swaps what it shows and the window stays pinned. And no card has a
// × (same day: "we don't need a × button — a click outside closes a temp
// one, and a pinned one we can unpin and click outside").
//
// The machines already had two words, and the states are spelled with them:
// `pinned` (a press happened — PeekIntent's, the stores') and `held` (ignores
// an outside press). temp = pinned && !held; pinned = held.
//
// Everything here is the part that needs no DOM, so each rule can be proved
// on its own (rail-pin-lib.test.ts): the transition table row by row, the
// ring click's reading (a double-click is the click event's `detail`), and
// what an outside press is.

export type CardMode = "closed" | "hover" | "temp" | "pinned";

/** The state a card's two words spell (header). `up`: a card is showing. */
export function cardMode(o: { up: boolean; pinned: boolean; held: boolean }): CardMode {
  if (!o.up) return "closed";
  if (o.held) return "pinned";
  return o.pinned ? "temp" : "hover";
}

/** The events of the transition table (design.md "Transitions"). */
export type CardEvent =
  /** The pointer (or the keyboard focus) arrived on the ring or door. */
  | "hover"
  /** The pointer left the ring, the card and the cone (after the grace). */
  | "leave"
  /** A single click on THIS card's ring or door. */
  | "click-ring"
  /** The second click of a double-click on the ring (`detail >= 2`). */
  | "dblclick-ring"
  /** Any press inside the card: its body, a grip, a head button, its menus. */
  | "press-inside"
  /** The pin icon in the head. */
  | "pin-icon"
  /** One press outside (header: what counts is `pressVerdict`'s). */
  | "press-outside"
  /** The × in the head. */
  | "close"
  /** Open ↗ (the card goes once the hop went). */
  | "open"
  /** The page changed under the card. */
  | "hop";

/** THE TRANSITION TABLE (design.md), one card's view of it. A row the table
 *  does not name leaves the state as it is. */
export function cardStep(mode: CardMode, event: CardEvent): CardMode {
  switch (event) {
    case "hover":
      return mode === "closed" ? "hover" : mode;
    case "leave":
      return mode === "hover" ? "closed" : mode;
    case "click-ring":
      // Its own ring closes a temp card; a pinned window ignores it.
      return mode === "temp" ? "closed" : mode === "pinned" ? "pinned" : "temp";
    case "dblclick-ring":
      // The double-click is the pin's toggle: it pins, and unpins a pinned one.
      return mode === "pinned" ? "temp" : "pinned";
    case "press-inside":
      return mode === "hover" ? "temp" : mode;
    case "pin-icon":
      return mode === "pinned" ? "temp" : mode === "closed" ? "closed" : "pinned";
    case "press-outside":
      return mode === "temp" ? "closed" : mode;
    case "close":
    case "open":
      return "closed";
    case "hop":
      return hopKeeps(mode) ? mode : "closed";
  }
}

/** WHAT A CLICK ON A RING (OR DOOR) DOES. `own`: the ring is the one whose
 *  card is up (or showing its hover). `detail`: the click event's own count
 *  — 0 for the keyboard's Enter, 1 a single click, 2 the second of a
 *  double-click (the platform's own double-click timing, not ours).
 *  `windowPinned`: the ring's WINDOW (A or B) is pinned now, on any card.
 *    - "pin":  the second click of a double-click on a window that is not
 *              pinned on this ring — opening the card pinned if the first
 *              click closed it; it never closes (owner, 2026-11-16) — and any
 *              click that opens or swaps a card in a pinned window: the pin
 *              is the window's;
 *    - "unpin": the second click of a double-click on the ring a pinned
 *              window shows (same day: "double click to unpin as well") — the
 *              card stays, temp;
 *    - "keep": a single click on the ring of the card a pinned window shows —
 *              nothing: only the unpin lets a pinned window go — and any
 *              click past the second of a burst;
 *    - "close": a single click on the ring of a TEMP card — the toggle;
 *    - "open": anything else — the card opens (or a hover's card stays) as
 *              temp. */
export type RingAct = "open" | "pin" | "unpin" | "close" | "keep";

export function ringClick(o: { mode: CardMode; own: boolean; detail: number; windowPinned?: boolean }): RingAct {
  if (o.detail > 2) return "keep";
  if (o.detail === 2) return o.own && o.mode === "pinned" ? "unpin" : "pin";
  if (o.own && o.mode === "pinned") return "keep";
  if (o.own && o.mode === "temp") return "close";
  return o.windowPinned ? "pin" : "open";
}

/** THE PIN ICON: hover or temp → pinned, pinned → temp; nothing for a card
 *  that is not up. */
export function pinToggle(mode: CardMode): "pin" | "unpin" | null {
  if (mode === "closed") return null;
  return mode === "pinned" ? "unpin" : "pin";
}

/** A PAGE HOP keeps only a pinned card (design.md rows 13–15). */
export function hopKeeps(mode: CardMode): boolean {
  return mode === "pinned";
}

/** A PRESS INTO A FRAME (the canvas or app iframe — heard as the window's
 *  blur): it counts as outside for a temp card and is ignored by a pinned
 *  one, every card alike. */
export function frameBlurCloses(mode: CardMode): boolean {
  return mode === "temp";
}

/** The parts of an element the classifier reads (an Element in the browser;
 *  a stub in a test). */
export interface PressTarget {
  closest(selector: string): unknown;
}

/** WHERE A PRESS LANDED, for one card (design.md "Outside"):
 *    - "inside": the card itself, its hover bridge, or anything the card
 *      opened — a menu, a dialog, a picker portaled out of it. The DOM cannot
 *      answer that last one (a portal is the body's child), so React's own
 *      capture phase does: `pressedInside`, set by the card's capture
 *      handler for any press that started in its tree.
 *    - "own": the card's own ring or door — its click is the toggle (or,
 *      doubled, the pin), so the press itself closes nothing.
 *    - "outside": everything else — the page, the left sidebar, the rail's
 *      blank, a terminal, a pane, a toast, the OTHER window's card and rings.
 *  `cardSelectors`: the card's own boxes (its id, its overlay); `ownSelectors`:
 *  its ring or door (`data-rail-door`, a ring's `data-task-key`). */
export type PressWhere = "inside" | "own" | "outside";

export const BRIDGE_SELECTOR = ".claude-rail-peek-bridge";

export function pressVerdict(
  el: PressTarget | null,
  o: { pressedInside: boolean; cardSelectors: readonly string[]; ownSelectors: readonly string[] },
): PressWhere {
  if (o.pressedInside) return "inside";
  if (!el) return "outside";
  for (const s of o.cardSelectors) if (el.closest(s)) return "inside";
  if (el.closest(BRIDGE_SELECTOR)) return "inside";
  for (const s of o.ownSelectors) if (el.closest(s)) return "own";
  return "outside";
}

/** Does this press close the card? Only a temp card, only an outside press. */
export function outsideCloses(mode: CardMode, where: PressWhere): boolean {
  return mode === "temp" && where === "outside";
}

/** The data attribute every rail door carries, for the classifier's "own
 *  door" (ClaudeRail): `tasks`, `bots`, `git`, `plus`. */
export const RAIL_DOOR_ATTR = "data-rail-door";
export function doorSelector(door: string): string {
  return `[${RAIL_DOOR_ATTR}="${door}"]`;
}

/** A task ring's selector by key (ClaudeRail's `data-task-key`), inside the
 *  section it lives in (`.claude-rail-chats`, `.claude-rail-favs`). */
export function ringSelector(section: string, key: string): string {
  return `${section} [data-task-key="${cssString(key)}"]`;
}

/** A bot ring's selector by id (ClaudeRail's `data-bot-id`). */
export function botRingSelector(id: string): string {
  return `[data-bot-id="${cssString(id)}"]`;
}

/** A value quoted for a CSS attribute selector (no `CSS.escape` in a test). */
function cssString(v: string): string {
  return v.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

/** THE PIN BUTTON'S WORDS (design.md "Pin icon"). */
export function pinWords(pinned: boolean): string {
  return pinned ? "Unpin (double-click the ring)" : "Pin (double-click the ring)";
}

/** WHERE A PINNED WINDOW A LANDS when the view changed under the task it
 *  shows — a page hop, a page with no rail in between, a reload (owner,
 *  2026-11-16: "if the same task item is present in the rail in the next view
 *  we don't change anything"; and, the same day, of a task the next view does
 *  not list: "we just close the window"):
 *    - "ring":  its ring is in the list — the card stays on it, pinned;
 *    - "close": no ring — the window goes, the pin with it. */
export function pinnedTaskLands(o: { inList: boolean }): "ring" | "close" {
  return o.inList ? "ring" : "close";
}

/** WHAT A PINNED WINDOW A SHOWS, as the window's mark spells it
 *  (platform/lib/last-chat `aPinned`): a task's card, the + New task card
 *  (a draft's ring is the same card), or the Tasks / Bots / Git card. */
export type APin = { kind: "task"; key: string } | { kind: "plus" | "tasks" | "bots" | "git" };

export function aPinWord(p: APin): string {
  return p.kind === "task" ? "task:" + p.key : p.kind;
}

export function readAPin(word: string | null): APin | null {
  if (!word) return null;
  if (word.startsWith("task:")) return word.length > 5 ? { kind: "task", key: word.slice(5) } : null;
  return word === "plus" || word === "tasks" || word === "bots" || word === "git" ? { kind: word } : null;
}

/** A RELOAD BRINGS BACK ONLY A PINNED WINDOW B CARD (design.md row 19): the
 *  open favourite the window's storage kept from before this document loaded
 *  (`openedAt` earlier than `loadedAt`, or unknown), with no pin on it, is
 *  dropped. One opened since the load (by this document) is not a reload's. */
export function reloadDropsOpen(o: {
  openKey: string | null;
  pinnedKey: string | null;
  openedAt: number | null;
  loadedAt: number;
}): boolean {
  if (!o.openKey || o.pinnedKey === o.openKey) return false;
  return o.openedAt === null || o.openedAt < o.loadedAt;
}

/** A PAGE HOP UNDER A WINDOW B CARD (design.md rows 13, 15): the URL's path
 *  changed, and the card open now is the one that was open before it, not
 *  pinned — it goes. A card opened BY the hop (the where-chip's hand-over
 *  lands with the push) is new, and stays. */
export function hopDropsOpen(o: {
  was: { path: string; key: string | null };
  now: { path: string; key: string | null };
  pinnedKey: string | null;
}): boolean {
  if (o.was.path === o.now.path || !o.now.key) return false;
  return o.now.key === o.was.key && o.pinnedKey !== o.now.key;
}
