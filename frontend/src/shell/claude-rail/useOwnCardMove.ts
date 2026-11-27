// THE HEAD DRAG OF A CARD THAT IS NOT ONE OF THE CLAUDE RAIL'S WINDOWS (owner,
// 2026-11-27: the collapsed LEFT sidebar's cards, shell/left-rail). useCardMove
// moves window A and window B: their positions are the windows' marks
// (platform/lib last-chat), their pins the rail's pins, their stacking and
// magnets each other's, and its pushes move pins. A left card is none of
// those — it must never move, re-place or un-pin anything on the Claude rail,
// nor be moved by it — so it moves through a store of its own (`OwnMoveStore`,
// the left-peek-store's, in memory) with the same grab:
//   - a press on the head's grab row (rail-move-lib `headPress`, the same
//     `RAIL_HEAD_SELECTOR`) arms it; MOVE_THRESHOLD_PX of travel before the
//     card leaves its dock, so a click or double-click never moves it;
//   - one write per animation frame, the box kept fully inside the viewport
//     and flush with its edges within EDGE_SNAP_PX (no other window to snap
//     to, and the app window draws no guide), the release's click swallowed;
//   - `.rail-card-moving` on <html> under the drag (frames take no pointer);
//   - a NON-PIN'S HEAD DRAG PINS IT with free pins on (`dragPins`), as the
//     Claude rail's cards do; a double-click on a floating head docks it with
//     free pins off;
//   - stacking among its own store's cards only: the last pressed is
//     `is-front`.
// Returns the same `CardMove` shape as useCardMove, so DoorCard reads either.
import { useEffect, useRef, useState, useSyncExternalStore, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import { flushSync } from "react-dom";
import { clampPos, headDocks, headPress, pastThreshold, snapEdges, viewportOf, type Pos, type Size } from "./rail-move-lib";
import type { Box } from "./rail-peek-lib";
import { MOVING_CLASS, type CardMove } from "./useCardMove";
import { useRailExtras } from "./rail-extras-flag";

/** Where a store's cards float, by key — memory, never the Claude rail's. */
export interface OwnMoveStore {
  /** Where `key` floats, or null (docked). */
  pos(key: string): Pos | null;
  /** Its own size while it floats (a free pin's frozen size), or null. */
  size(key: string): Size | null;
  setPos(key: string, pos: Pos | null): void;
  setSize(key: string, size: Size | null): void;
  /** The card pressed last, of this store's. */
  front(): string | null;
  raise(key: string): void;
  subscribe(fn: () => void): () => void;
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

/** No store (a Claude rail card): nothing to hear. */
const none = (): (() => void) => () => {};

function elementOf(t: EventTarget | null): (Element & { closest(s: string): Element | null }) | null {
  const n = t as Node | null;
  if (!n) return null;
  if (n instanceof Element) return n;
  return n.parentElement;
}

function rootClass(on: boolean): void {
  const cl = typeof document === "undefined" ? null : document.documentElement?.classList;
  if (!cl) return;
  if (on) cl.add(MOVING_CLASS);
  else cl.remove(MOVING_CLASS);
}

function swallowNextClick(): void {
  if (typeof window === "undefined") return;
  const swallow = (e: Event) => {
    e.stopPropagation();
    e.preventDefault();
    off();
  };
  const off = () => window.removeEventListener("click", swallow, true);
  window.addEventListener("click", swallow, true);
  window.setTimeout(off, 0);
}

export function useOwnCardMove(o: {
  store: OwnMoveStore | null;
  key: string;
  /** Pinned (held): the head grabs. */
  movable: boolean;
  headSelector: string;
  /** The card's pin press, for a non-pin's head drag (free pins only). */
  dragPins?: (() => void) | null;
}): CardMove {
  const { store, key, headSelector } = o;
  const freePins = useRailExtras();
  const movable = !!store && o.movable;
  const pos = useSyncExternalStore(
    store ? store.subscribe : none,
    () => (store && movable ? store.pos(key) : null),
    () => null,
  );
  const front = useSyncExternalStore(
    store ? store.subscribe : none,
    () => (store ? store.front() : null),
    () => null,
  );
  const floating = movable && pos !== null;
  const [dragging, setDragging] = useState(false);
  const drag = useRef<Drag | null>(null);
  const stop = useRef<(() => void) | null>(null);
  const dragPins = store && freePins && !movable ? (o.dragPins ?? null) : null;
  const now = useRef({ movable, floating, freePins, dragPins });
  now.current = { movable, floating, freePins, dragPins };

  const finish = (commit: boolean) => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    stop.current?.();
    stop.current = null;
    if (d.raf) cancelAnimationFrame(d.raf);
    if (!d.moved) return;
    rootClass(false);
    setDragging(false);
    try {
      if (d.el.hasPointerCapture?.(d.pointerId)) d.el.releasePointerCapture(d.pointerId);
    } catch {
      // The box left the page under the drag: nothing holds the pointer.
    }
    if (commit && d.latest && store) {
      store.setPos(key, d.latest);
      swallowNextClick();
    }
  };
  const finishRef = useRef(finish);
  finishRef.current = finish;

  const onPointerDown = (e: ReactPointerEvent<HTMLElement>) => {
    if (!store || (!now.current.movable && !now.current.dragPins) || e.button !== 0 || drag.current) return;
    const box = e.currentTarget;
    const el = elementOf(e.target);
    if (!el || !box.contains(el) || headPress(el, headSelector) !== "head") return;
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
        // A NON-PIN LEAVING ITS DOCK IS PINNED FIRST (header): its own pin
        // press freezes it where it stands, in one synchronous pass.
        if (!now.current.movable) {
          const pin = now.current.dragPins;
          if (!pin) {
            finishRef.current(false);
            return;
          }
          flushSync(pin);
          if (!now.current.movable) {
            finishRef.current(false);
            return;
          }
        }
        d.moved = true;
        setDragging(true);
        rootClass(true);
        try {
          d.el.setPointerCapture?.(d.pointerId);
        } catch {
          // The window listeners still follow it.
        }
        try {
          window.getSelection?.()?.removeAllRanges();
        } catch {
          // No selection API.
        }
      }
      const size = { width: d.width, height: d.height };
      const view = viewportOf(window);
      // Flush with the app window's edges, silently (no other window here).
      const asked = clampPos({ left: d.left + dx, top: d.top + dy }, size, view);
      d.latest = snapEdges(asked, size, null, view).pos;
      if (!d.raf) {
        d.raf = requestAnimationFrame(() => {
          d.raf = 0;
          if (drag.current === d && d.latest) store.setPos(key, d.latest);
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
    // With free pins a pin has no dock: its × closes it.
    if (!store || !now.current.floating || now.current.freePins) return;
    const el = elementOf(e.target);
    if (!el || !e.currentTarget.contains(el) || !headDocks(el, headSelector)) return;
    store.setPos(key, null);
  };

  // The pin went under the drag, or the card unmounted: the drag goes unsaved.
  useEffect(() => {
    if (!movable) finishRef.current(false);
  }, [movable]);
  useEffect(() => () => finishRef.current(false), []);

  const isFront = !!store && front === key;
  return {
    floating,
    pos: floating ? pos : null,
    dragging,
    className:
      (movable || dragPins ? " is-movable" : "") +
      (floating ? " is-floating" : "") +
      (dragging ? " is-dragging" : "") +
      (isFront ? " is-front" : ""),
    z: null,
    key,
    onPointerDown,
    onDoubleClick,
    notePress: () => {
      store?.raise(key);
    },
    setPos: (p) => store?.setPos(key, p),
    at: () => (store ? store.pos(key) : null),
    size: () => (store ? store.size(key) : null),
    commitBox: (box: Box) => {
      if (!store) return;
      store.setSize(key, { width: box.width, height: box.height });
      store.setPos(key, { left: box.left, top: box.top });
    },
  };
}
