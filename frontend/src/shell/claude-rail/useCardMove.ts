// THE HEAD DRAG THAT MOVES A PINNED RAIL WINDOW (rail-move-lib header; owner,
// 2026-11-17). ONE copy for the four chassis — RailPeek (the list's card and
// the favourite's), DoorCard (Tasks / Bots / Git), BotPeek and the + card's
// Modal popover (through ClaudeRail) — so the grab cannot drift between them.
//
// The breakout window's drag (shell/breakout/BreakoutCards `FloatingCard`):
// a press on the head (not on a control) arms it, the pointer is captured,
// moves are coalesced to one write per animation frame, the box is clamped
// fully inside the viewport. Ours waits
// for MOVE_THRESHOLD_PX of travel before the card leaves its dock, so a plain
// click or double-click on the head never moves it, and swallows the click a
// real drag ends with. While a drag is on, the page's frames take no pointer
// (`.rail-card-moving` on <html>; the app page seam's own trick, app-page.css)
// and nothing selects.
//
// The handlers go on the card's BOX (the fixed element that moves): a press
// is read against the head by selector (rail-move-lib `headPress`), and only
// a press inside the box's own DOM counts — React carries a portaled menu's
// press up the card's tree, the DOM does not.
//
// THE EDGE MAGNET (owner, 2026-11-17; design.md "Round 3 rulings" B9): within
// EDGE_SNAP_PX the dragged box lands flush with an edge of the other window's
// card (docked or floating, while it is up — rail-move-store `windowBox`) or
// of the viewport — or centred on either (owner, 2026-11-18: "top bottom
// left right middle everywhere") — each axis on its own, with the dashed
// guides along every line it stands on (card-snap) until the release.
//
// "BACK WHERE I LEFT IT" (owner, 2026-11-17, B10): the stored position is the
// spot the reader dragged to, and a window that shrinks does NOT rewrite it —
// each chassis draws `floatPlacement(pos, size, viewport)` again as the
// window resizes, squeezed while the room is short and back at its spot when
// the room returns. A drag that starts from a squeezed box starts from the
// box as drawn, so its release commits that.
import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type { Size } from "./rail-move-lib";
import {
  clampPos,
  headDocks,
  headPress,
  isFloating,
  pastThreshold,
  raised,
  snapEdges,
  viewportOf,
  type MoveTarget,
  type Pos,
  type RailWindow,
} from "./rail-move-lib";
import {
  commitWindowPos,
  noteFrontWindow,
  setLiveWindowPos,
  subscribeWindowPos,
  useFrontWindow,
  magnetBox,
  pinEpoch,
  windowPos,
  windowSize,
} from "./rail-move-store";
import { setSnapGuides } from "./card-snap";
import { useRailFreePins } from "./rail-free-pins-flag";
import { useDoomedWindow } from "./rail-slots-store";

/** On <html> while a card is being moved: frames take no pointer, nothing
 *  selects (claude-rail.css). */
export const MOVING_CLASS = "rail-card-moving";

export interface CardMove {
  /** Pinned AND placed by the reader: the card stands at `pos`, not beside
   *  the rail. */
  floating: boolean;
  /** The window's position while floating, else null. */
  pos: Pos | null;
  /** A drag is moving the card now. */
  dragging: boolean;
  /** For the box: ` is-movable` (pinned: the head grabs), ` is-floating`,
   *  ` is-dragging`, ` is-front` (pressed last, rail-move-lib `raised`) —
   *  "" while none applies, so a card with nothing to move renders as it was. */
  className: string;
  /** The box's pointerdown (bubble phase). */
  onPointerDown(e: ReactPointerEvent<HTMLElement>): void;
  /** The box's double-click: a floating card docks, and stays pinned. */
  onDoubleClick(e: ReactMouseEvent<HTMLElement>): void;
  /** Any press inside the card (its capture handler): this window is on top. */
  notePress(): void;
  /** Where the window now stands for good (a left or top resize moved it),
   *  or docked (`null`). */
  setPos(pos: Pos | null): void;
  /** WHERE THIS CARD'S WINDOW FLOATS, read live (a placement outside render:
   *  a resize, a scroll) — the window's position, or, for a pin that has been
   *  REPLACED (rail-move-store `pinEpoch`), the spot it had: it is on its way
   *  out and must not follow the pin that took its window. */
  at(): Pos | null;
  /** …and its frozen size (free pins), the same way. */
  size(): Size | null;
}

interface Drag {
  sx: number;
  sy: number;
  left: number;
  top: number;
  width: number;
  height: number;
  pointerId: number;
  el: HTMLElement;
  moved: boolean;
  latest: Pos | null;
  raf: number;
}

/** The element a press landed on (a text node's parent). */
function elementOf(t: EventTarget | null): (MoveTarget & Node) | null {
  const n = t as (Node & Partial<MoveTarget>) | null;
  if (!n) return null;
  if (typeof n.closest === "function") return n as MoveTarget & Node;
  const p = n.parentElement as (Node & Partial<MoveTarget>) | null | undefined;
  return p && typeof p.closest === "function" ? (p as MoveTarget & Node) : null;
}

function rootClass(on: boolean): void {
  const cl = typeof document === "undefined" ? null : document.documentElement?.classList;
  if (!cl) return;
  if (on) cl.add(MOVING_CLASS);
  else cl.remove(MOVING_CLASS);
}

/** A real drag ends with a click on whatever the pointer came up over (the
 *  head's title, a seat): that click was the drag's, not a press. */
function swallowNextClick(): void {
  if (typeof window === "undefined") return;
  const swallow = (e: Event) => {
    e.stopPropagation();
    e.preventDefault();
    off();
  };
  const off = () => window.removeEventListener("click", swallow, true);
  window.addEventListener("click", swallow, true);
  // No click came (the pointer came up off the card): forget it.
  window.setTimeout(off, 0);
}

export function useCardMove(o: {
  win: RailWindow;
  /** The unified-head switch is on (the pin exists): presses count for the
   *  stacking. Off, the hook does nothing at all. */
  on: boolean;
  /** The window is PINNED (held) on this card: its head grabs. */
  movable: boolean;
  /** The card's head (rail-move-lib `RAIL_HEAD_SELECTOR`,
   *  `POPOVER_HEAD_SELECTOR`). */
  headSelector: string;
  /** What the card shows while it is up (any stable key), null while it is
   *  down: a card COMING UP, or swapping what it shows, takes the front — a
   *  hover's preview included, which otherwise opened beside the rail UNDER a
   *  window floating there (owner, 2026-11-17: "if there are two windows …
   *  they overlap"). */
  shown?: string | null;
}): CardMove {
  const { win, on, headSelector } = o;
  // FREE PINS (rail-slots-lib): a pinned window is frozen where it stood, so
  // it always floats and nothing docks it but its unpin.
  const freePins = useRailFreePins() && on;
  const doomed = useDoomedWindow();
  const shown = on ? (o.shown ?? null) : null;
  useEffect(() => {
    if (shown !== null) noteFrontWindow(win);
  }, [shown, win]);
  const movable = on && o.movable;
  // THE PIN THIS CARD IS (rail-move-store `pinEpoch`): the count its window
  // was pinned at when this card took the pin. A later count: it was
  // replaced, and reads the last position and size it had instead of the
  // window's.
  const epoch = useRef<number | null>(null);
  if (!movable) epoch.current = null;
  else if (epoch.current === null) epoch.current = pinEpoch(win);
  const had = useRef<{ pos: Pos | null; size: Size | null }>({ pos: null, size: null });
  const at = (): Pos | null => {
    if (epoch.current !== null && epoch.current !== pinEpoch(win)) return had.current.pos;
    const pos = windowPos(win);
    had.current = { pos, size: windowSize(win) };
    return pos;
  };
  const size = (): Size | null => (epoch.current !== null && epoch.current !== pinEpoch(win) ? had.current.size : windowSize(win));
  // Read live only while this card can float: a drag's every frame would
  // otherwise re-render every card of both windows. The other window counts
  // only as "does it float" (the stacking).
  const otherWin: RailWindow = win === "a" ? "b" : "a";
  const pos = useSyncExternalStore(
    subscribeWindowPos,
    () => (movable ? at() : null),
    () => null,
  );
  const otherFloats = useSyncExternalStore(
    subscribeWindowPos,
    () => on && windowPos(otherWin) !== null,
    () => false,
  );
  const front = useFrontWindow();
  const floating = isFloating({ movable, pos });
  const [dragging, setDragging] = useState(false);
  const drag = useRef<Drag | null>(null);
  const stop = useRef<(() => void) | null>(null);
  const now = useRef({ movable, floating, headSelector, freePins });
  now.current = { movable, floating, headSelector, freePins };

  const finish = (commit: boolean) => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    stop.current?.();
    stop.current = null;
    if (d.raf) cancelAnimationFrame(d.raf);
    if (!d.moved) return;
    // The magnet's guides go with the drag: release, cancel or unmount.
    setSnapGuides([], []);
    rootClass(false);
    setDragging(false);
    try {
      if (d.el.hasPointerCapture?.(d.pointerId)) d.el.releasePointerCapture(d.pointerId);
    } catch {
      // The box left the page under the drag: nothing holds the pointer.
    }
    if (commit && d.latest) {
      commitWindowPos(win, d.latest);
      swallowNextClick();
    } else setLiveWindowPos(win, null);
  };
  const finishRef = useRef(finish);
  finishRef.current = finish;

  const onPointerDown = (e: ReactPointerEvent<HTMLElement>) => {
    if (!now.current.movable || e.button !== 0 || drag.current) return;
    const box = e.currentTarget;
    const el = elementOf(e.target);
    if (!el || !box.contains(el) || headPress(el, now.current.headSelector) !== "head") return;
    const r = box.getBoundingClientRect();
    const d: Drag = {
      sx: e.clientX,
      sy: e.clientY,
      left: r.left,
      top: r.top,
      width: r.width,
      height: r.height,
      pointerId: e.pointerId,
      el: box,
      moved: false,
      latest: null,
      raf: 0,
    };
    drag.current = d;
    const move = (ev: PointerEvent) => {
      if (ev.pointerId !== d.pointerId || drag.current !== d) return;
      const dx = ev.clientX - d.sx;
      const dy = ev.clientY - d.sy;
      if (!d.moved) {
        if (!pastThreshold(dx, dy)) return;
        d.moved = true;
        setDragging(true);
        rootClass(true);
        try {
          d.el.setPointerCapture?.(d.pointerId);
        } catch {
          // A pointer the browser no longer tracks: the window listeners
          // still follow it.
        }
        try {
          window.getSelection?.()?.removeAllRanges();
        } catch {
          // No selection API: nothing was selected.
        }
      }
      const size = { width: d.width, height: d.height };
      const view = viewportOf(window);
      // THE EDGE MAGNET (header): flush with the other window's card or the
      // viewport — an edge or the middle — within EDGE_SNAP_PX, the guides
      // along every line the box now stands on.
      const snapped = snapEdges(clampPos({ left: d.left + dx, top: d.top + dy }, size, view), size, magnetBox(otherWin, true), view);
      d.latest = snapped.pos;
      setSnapGuides(snapped.x, snapped.y);
      if (!d.raf) {
        d.raf = requestAnimationFrame(() => {
          d.raf = 0;
          if (drag.current === d && d.latest) setLiveWindowPos(win, d.latest);
        });
      }
    };
    const up = (ev: PointerEvent) => {
      if (ev.pointerId !== d.pointerId) return;
      finishRef.current(ev.type !== "pointercancel" || d.moved);
    };
    const lost = () => finishRef.current(true);
    window.addEventListener("pointermove", move, true);
    window.addEventListener("pointerup", up, true);
    window.addEventListener("pointercancel", up, true);
    box.addEventListener("lostpointercapture", lost);
    stop.current = () => {
      window.removeEventListener("pointermove", move, true);
      window.removeEventListener("pointerup", up, true);
      window.removeEventListener("pointercancel", up, true);
      box.removeEventListener("lostpointercapture", lost);
    };
  };

  const onDoubleClick = (e: ReactMouseEvent<HTMLElement>) => {
    // A free pin has no dock to go back to (rail-slots-lib): its unpin does that.
    if (!now.current.floating || now.current.freePins) return;
    const el = elementOf(e.target);
    if (!el || !e.currentTarget.contains(el) || !headDocks(el, now.current.headSelector)) return;
    commitWindowPos(win, null);
  };

  // The pin went under the drag (the pin icon by keyboard, a close): the drag
  // goes with it, unsaved. And an unmount ends it the same way.
  useEffect(() => {
    if (!movable) finishRef.current(false);
  }, [movable]);
  useEffect(() => () => finishRef.current(false), []);

  // NOT RE-CLAMPED AS THE WINDOW RESIZES (owner, 2026-11-17: "back where I
  // left it"; header): the stored spot stands, and each chassis draws the box
  // squeezed inside the viewport (rail-move-lib `floatPlacement`) until the
  // room returns.

  const isFront = on && raised({ win, front, anyFloating: floating || otherFloats });
  return {
    floating,
    pos: floating ? pos : null,
    dragging,
    className:
      (movable ? " is-movable" : "") +
      (floating ? " is-floating" : "") +
      (dragging ? " is-dragging" : "") +
      (isFront ? " is-front" : "") +
      // A FREE PIN (rail-slots-lib): under the non-pins, never dimmed by a
      // ring's hover row, and the one a red border marks (claude-rail.css).
      (freePins && floating ? " is-free-pin" : "") +
      // …the pin a non-pin's pin icon is about to replace (rail-slots-store).
      (freePins && floating && doomed === win ? " is-doomed" : ""),
    onPointerDown,
    onDoubleClick,
    notePress: () => {
      if (on) noteFrontWindow(win);
    },
    setPos: (p) => commitWindowPos(win, p),
    at,
    size,
  };
}
