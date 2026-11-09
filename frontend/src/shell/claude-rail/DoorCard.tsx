// WINDOW A'S CHASSIS FOR A NAV DOOR'S CARD (owner, 2026-11-07 / 2026-11-09):
// the Tasks card (TasksPeek) and the Bots card (BotsPeek) are one kind of
// thing — a page's content in window A's frame, off one of the rail's nav
// doors — and this is the frame: everything that is not the content. It was
// the Tasks card's body until the Bots card wanted the very same behaviour
// ("make sure window A behaviour is like Tasks' behaviour, and you cover all
// edge cases"): one code path, so the two cannot drift.
//
// WHAT IT DOES, for the store it is given (door-card-store):
//   - registers a card while the rail flag is on (and `enabled` — a Bots card
//     only where the server offers Bots), and says whether it has ROOM beside
//     the rail; none, and the door is a plain link until the window, the rail
//     or the row changes shape (FavouritePeek's rule);
//   - a PREVIEW never outlives its rail; a pinned card follows the reader to
//     the next page's rail;
//   - stands left of the rail, its top level with the door, at WINDOW A'S
//     SIZE — the remembered `canvasRailPeek` (rail-peek-lib `railCardSize`),
//     which its grips write too — clamped inside the row and keeping CLEAR of
//     window B's box (`favBox`, rail-peek-lib `placeClear`), never closing it;
//   - the rail card's classes (`.claude-rail-peek`, claude-rail.css), its
//     two-frame show and fade, bridge and safe cone, inert while hidden;
//   - a press inside pins a preview (PeekIntent `pressStart`) and is the
//     content's to claim (`onClaim`); which focus holds the card (an
//     editable, a popup, the keyboard); Escape closes a PREVIEW (a pinned
//     card closes by its ×, the door or Open only), on the card and, with the
//     focus nowhere, on the document;
//   - THE KEYBOARD IS NEVER LEFT ON THE BODY: a card that shuts with the
//     focus inside hands it to the door; a sub-view (a chat) opening over the
//     list puts it on Back (`backRef`); Back puts it on the × (`closeRef`);
//     the door's Enter lands it on Back, else the × (the store's focuser).
//
// THE CONTENT draws the head and body through `children(api)`: `visible`,
// `pinned`, `close`, `openPage`, the two focus refs, and `onSubEscape` for a
// chat's own Escape. Its hooks live in the content component, which reads
// the store itself for `up` (the open chat resets on any close, and the
// content mounts on the first opening and stays).
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { navigateUrl } from "@platform/lib/router";
import { getUiLayout, patchUiLayout, subscribeUiLayout } from "@platform/lib/ui-layout";
import type { DoorCardStore } from "./door-card-store";
import { useFavPeekState } from "./fav-peek-store";
import { useCanvasRailPeek } from "./rail-peek-flag";
import {
  PEEK_MIN_HEIGHT,
  PEEK_MIN_WIDTH,
  bottomClear,
  dragResize,
  inSafeCone,
  placeClear,
  railCardSize,
  rowOf,
  type Box,
  type ResizeEdge,
} from "./rail-peek-lib";

const ALL_GRIPS: readonly ResizeEdge[] = ["l", "t", "b", "tl", "bl"];

/** Window A's size as remembered (header). */
function cardSize(): { width: number; height: number } {
  return railCardSize(getUiLayout().canvasRailPeek ?? null);
}

export interface DoorCardApi {
  /** Drawn and shown (placed, on, with room). */
  visible: boolean;
  /** Pinned by a press (the door's or one inside). */
  pinned: boolean;
  /** The ×: hands the keyboard to the door and puts the card down. */
  close(): void;
  /** Open: the page itself (`href` unless another is given); the card goes
   *  once the hop went (a leave guard may refuse it — FavouritePeek's rule). */
  openPage(href?: string): void;
  /** The head's × — the keyboard's seat with the list up. */
  closeRef: RefObject<HTMLButtonElement>;
  /** The chat head's Back — the keyboard's seat with a chat up. */
  backRef: RefObject<HTMLButtonElement>;
  /** A chat's own Escape, let through: closes a preview, never a pinned card. */
  onSubEscape(): void;
}

export interface DoorCardProps {
  store: DoorCardStore;
  /** The door's page: where a press with no room goes, Open's default. */
  href: string;
  /** The card's own class beside `.claude-rail-peek` (its CSS hook). */
  className: string;
  ariaLabel: string;
  /** Only where the content can exist at all (default true). Off: no card is
   *  registered and the door stays a plain link. */
  enabled?: boolean;
  /** A sub-view (a chat) is open over the list: the keyboard's seat is Back. */
  subOpen: boolean;
  /** A press, click or key inside (React's capture phase, across portals):
   *  the content's claim (task-peek-store `notePressInCard`). */
  onClaim?: () => void;
  /** Shown or hidden, as it changes (the content's read marks, its chat's
   *  `smallCardShown`). */
  onVisible?: (visible: boolean) => void;
  children: (api: DoorCardApi) => ReactNode;
}

export function DoorCard({ store, href, className, ariaLabel, enabled = true, subOpen, onClaim, onVisible, children }: DoorCardProps) {
  const on = useCanvasRailPeek() && enabled;
  const card = store.useCard();
  // …and window B's box (`favBox`): this card is window A's and keeps CLEAR
  // of the favourite's or the bot's card — above it, else to its left
  // (rail-peek-lib `placeClear`) — never closes it (owner, 2026-11-08).
  const { rail, favBox } = useFavPeekState();
  const intent = store.intent();

  // A card stands behind every rail's door while the flag is on (the store's
  // `ready`); off, nothing shows.
  useEffect(() => (on ? store.registerCard() : undefined), [on, store]);
  useEffect(() => {
    if (!on) store.close();
  }, [on, store]);

  // NO ROOM beside this rail: the door is a plain link until the window, the
  // rail or the row it stands in changes shape (FavouritePeek's rule).
  const [noRoom, setNoRoom] = useState(false);
  useEffect(() => store.setRoom(!noRoom), [noRoom, store]);
  useEffect(() => {
    setNoRoom(false);
    const re = () => setNoRoom(false);
    window.addEventListener("resize", re);
    const ro = rail && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (rail && ro) {
      ro.observe(rail);
      const r = rowOf(rail);
      if (r) ro.observe(r);
    }
    return () => {
      window.removeEventListener("resize", re);
      ro?.disconnect();
    };
  }, [rail]);
  // …and when window B moves or goes (its box is part of the room; ClaudeRail
  // does the same for the list's card).
  useEffect(() => {
    setNoRoom(false);
  }, [favBox]);
  // A PREVIEW NEVER OUTLIVES ITS RAIL; a pinned card follows the reader to the
  // next page's rail (header).
  const railWas = useRef(rail);
  useEffect(() => {
    if (railWas.current === rail) return;
    railWas.current = rail;
    if (!store.state().pinned) store.close();
  }, [rail, store]);

  const up = on && card.up;

  // WHERE IT STANDS (header): left of the rail, its top level with the rail's
  // door, at window A's size, clamped inside the row.
  const cardRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const backRef = useRef<HTMLButtonElement | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  const [shownOn, setShownOn] = useState(false);
  const row = useMemo(() => rowOf(rail), [rail]);
  const railLeft = useRef(0);
  const door = store.doorOf(rail);
  const want = up && !!rail && !!door && !noRoom;
  const place = (): Box | null => {
    const d = store.doorOf(rail);
    if (!rail || !d || !row) return null;
    const r = rail.getBoundingClientRect();
    railLeft.current = r.left;
    const size = cardSize();
    return placeClear(d.getBoundingClientRect(), r, row.getBoundingClientRect(), favBox, {
      width: size.width,
      maxHeight: size.height,
    });
  };
  // No room: the card goes and the door is a link (above) — and a PRESS that
  // found none takes the link's own way to the page.
  const lostRoom = (atOpen: boolean) => {
    const pressed = atOpen && store.state().pinned;
    store.close();
    setNoRoom(true);
    if (pressed) void navigateUrl(href);
  };

  // THE GRIPS AND THEIR DRAG (RailPeek's; window A's key, `canvasRailPeek`).
  const [resizing, setResizing] = useState<ResizeEdge | null>(null);
  const resizingRef = useRef<ResizeEdge | null>(null);
  resizingRef.current = resizing;
  const dragStart = useRef<{ x: number; y: number; box: Box; pointerId: number; corner: ResizeEdge; el: HTMLElement } | null>(null);
  const dragLast = useRef<Box | null>(null);
  const onGripDown = (corner: ResizeEdge) => (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || !box) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragStart.current = { x: e.clientX, y: e.clientY, box, pointerId: e.pointerId, corner, el: e.currentTarget };
    dragLast.current = null;
    setResizing(corner);
  };
  const onGripMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId || !row) return;
    // Never over window B's card under it (RailPeek's own floor,
    // rail-peek-lib `bottomClear`; owner, 2026-11-08).
    const next = dragResize(
      start.corner,
      start.box,
      e.clientX - start.x,
      e.clientY - start.y,
      row.getBoundingClientRect(),
      bottomClear(start.box, favBox),
    );
    dragLast.current = next;
    setBox((prev) => (prev && sameBox(prev, next) ? prev : next));
  };
  const endDrag = (save: boolean) => {
    const start = dragStart.current;
    dragStart.current = null;
    setResizing(null);
    const last = dragLast.current;
    dragLast.current = null;
    if (start && start.el.hasPointerCapture?.(start.pointerId)) start.el.releasePointerCapture(start.pointerId);
    if (!save || !start || !last || (last.width === start.box.width && last.height === start.box.height)) return;
    // Only a dimension the drag changed, past the grips' minimums (RailPeek
    // `endDrag`); the other keeps what was remembered.
    const prev = cardSize();
    const width = last.width !== start.box.width && last.width >= PEEK_MIN_WIDTH ? last.width : prev.width;
    const height = last.height !== start.box.height && last.height >= PEEK_MIN_HEIGHT ? last.height : prev.height;
    patchUiLayout((d) =>
      d.canvasRailPeek?.width === width && d.canvasRailPeek?.height === height ? d : { ...d, canvasRailPeek: { width, height } },
    );
  };
  const onGripUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId) return;
    endDrag(true);
  };
  const visible = shownOn && box !== null && want;
  useEffect(() => {
    if (dragStart.current && !visible) endDrag(false);
  }, [visible]);
  const onVisibleRef = useRef(onVisible);
  onVisibleRef.current = onVisible;
  useEffect(() => {
    onVisibleRef.current?.(visible);
  }, [visible]);
  // A ui-layout hydrate (or another card's drag) lands a new size: re-render.
  const [, setLayoutTick] = useState(0);
  useEffect(() => subscribeUiLayout(() => setLayoutTick((n) => n + 1)), []);

  // PLACED, THEN SHOWN (RailPeek header): the box on this frame, the class on
  // the next; a close keeps the box, so the fade has somewhere to fade.
  useLayoutEffect(() => {
    if (!want) {
      setShownOn(false);
      return;
    }
    const next = place();
    if (!next) {
      lostRoom(true);
      return;
    }
    setBox(next);
    const raf = requestAnimationFrame(() => setShownOn(true));
    return () => cancelAnimationFrame(raf);
  }, [want, rail]);
  // FOLLOW THE DOOR while up: a resize, the row changing shape; plus every
  // render, cheaply.
  useEffect(() => {
    if (!want || !rail) return;
    const re = () => {
      if (resizingRef.current) return;
      const next = place();
      if (!next) {
        lostRoom(false);
        return;
      }
      setBox((prev) => (prev && sameBox(prev, next) ? prev : next));
    };
    window.addEventListener("resize", re);
    const ro = row && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (row && ro) ro.observe(row);
    return () => {
      window.removeEventListener("resize", re);
      ro?.disconnect();
    };
  }, [want, rail, row, favBox]);
  useEffect(() => {
    if (!want || resizing || !box) return;
    const next = place();
    if (next && !sameBox(next, box)) setBox(next);
  });

  // THE SAFE CONE from the door to the card's near edge, and the BRIDGE over
  // the gap to the rail (RailPeek; rail-peek-lib `inSafeCone`).
  const coneOn = useRef(false);
  const setCone = (inside: boolean) => {
    if (coneOn.current === inside) return;
    coneOn.current = inside;
    if (inside) intent.enterCone();
    else intent.leaveCone();
  };
  useEffect(() => {
    if (!shownOn || !box || !door) return;
    const r = door.getBoundingClientRect();
    const apex = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    const onMove = (e: PointerEvent) => {
      const inCard = cardRef.current?.contains(e.target as Node) ?? false;
      setCone(!inCard && inSafeCone({ x: e.clientX, y: e.clientY }, apex, box));
    };
    document.addEventListener("pointermove", onMove, true);
    return () => {
      document.removeEventListener("pointermove", onMove, true);
      setCone(false);
    };
  }, [shownOn, box, door]);

  // A PRESS INSIDE pins a preview (PeekIntent `pressStart`), and is the
  // content's to claim (`onClaim`) — React's capture phase, so a menu or
  // modal the views portal out counts.
  useEffect(() => {
    const upNow = () => intent.pressEnd();
    window.addEventListener("pointerup", upNow, true);
    window.addEventListener("pointercancel", upNow, true);
    return () => {
      window.removeEventListener("pointerup", upNow, true);
      window.removeEventListener("pointercancel", upNow, true);
    };
  }, [intent]);
  const claim = () => onClaim?.();

  // INERT WHILE HIDDEN (RailPeek).
  useLayoutEffect(() => {
    cardRef.current?.toggleAttribute("inert", !visible);
  }, [visible]);

  // THE KEYBOARD IS NEVER LEFT ON THE BODY (header).
  const handBack = useCallback(() => {
    const a = document.activeElement;
    if (a && cardRef.current?.contains(a)) store.doorOf(rail)?.focus({ preventScroll: true });
  }, [rail, store]);
  const close = useCallback(() => {
    handBack();
    store.close(true);
  }, [handBack, store]);
  const subWas = useRef(false);
  useEffect(() => {
    const was = subWas.current;
    subWas.current = subOpen;
    if (!visible) return;
    const a = document.activeElement;
    const lost = !a || a === document.body || !!cardRef.current?.contains(a);
    if (subOpen && !was && lost) backRef.current?.focus({ preventScroll: true });
    else if (!subOpen && was && lost) closeRef.current?.focus({ preventScroll: true });
  }, [subOpen, visible]);

  // THE DOOR'S ENTER LANDS THE KEYBOARD HERE (the store's `focusCard`): on
  // Back over a chat, else on the ×.
  useEffect(
    () =>
      store.registerFocus(() => {
        (backRef.current ?? closeRef.current)?.focus({ preventScroll: true });
        return !!cardRef.current?.contains(document.activeElement);
      }),
    [store],
  );

  // WHICH FOCUS HOLDS THE CARD (RailPeek): an editable, a popup of its own,
  // or focus the keyboard brought.
  const lastKeyAt = useRef(0);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" || e.key.startsWith("Arrow")) lastKeyAt.current = Date.now();
    };
    const onPointer = () => {
      lastKeyAt.current = 0;
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onPointer, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onPointer, true);
    };
  }, []);
  const onFocusCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const t = e.target as HTMLElement;
    const editable = t.matches("textarea, input, select, [contenteditable=''], [contenteditable='true']");
    const pop = t.closest("[role='menu'], [role='listbox'], [role='dialog'], [role='alertdialog']");
    const popup = !!pop && pop !== cardRef.current;
    const keyboard = Date.now() - lastKeyAt.current < 400;
    intent.hold(editable || popup || keyboard);
  };
  const onBlurCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget as Node | null;
    if (next && e.currentTarget.contains(next)) return;
    intent.hold(false);
  };
  // ESCAPE closes a PREVIEW (a pinned card closes by its ×, the door or Open
  // only); never from a popup inside the card, whose Escape is its own.
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape" || e.defaultPrevented || card.pinned) return;
    const t = e.target as HTMLElement;
    const pop = t.closest("[role='menu'], [role='listbox'], [role='dialog'], [role='alertdialog']");
    if (pop && pop !== cardRef.current) return;
    e.preventDefault();
    close();
  };
  // …and with the focus nowhere (a hover's preview: the pointer brought it,
  // the keyboard is on the body), the document's Escape does the same.
  const previewing = visible && !card.pinned;
  useEffect(() => {
    if (!previewing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (e.target !== document.body && e.target !== document.documentElement) return;
      e.preventDefault();
      store.close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [previewing, store]);
  const onSubEscape = useCallback(() => {
    if (!store.state().pinned) close();
  }, [close, store]);
  // OPEN: the page itself; the card goes once the hop went (a leave guard
  // may refuse it — FavouritePeek's rule).
  const openPage = useCallback(
    (to = href) => {
      void navigateUrl(to).then((went) => {
        if (went !== false) close();
      });
    },
    [close, href],
  );

  const api = useMemo<DoorCardApi>(
    () => ({ visible, pinned: card.pinned, close, openPage, closeRef, backRef, onSubEscape }),
    [visible, card.pinned, close, openPage, onSubEscape],
  );

  if (!on) return null;
  const bridge =
    box && railLeft.current > box.left + box.width + 1
      ? { left: box.left + box.width, top: box.top, width: Math.max(0, railLeft.current - 3 - (box.left + box.width)), height: box.height }
      : null;

  return createPortal(
    <>
      {bridge && (
        <div
          className={"claude-rail-peek-bridge" + (visible ? " is-on" : "")}
          aria-hidden="true"
          style={bridge}
          onPointerEnter={() => setCone(true)}
          onPointerLeave={() => setCone(false)}
        />
      )}
      <div
        ref={cardRef}
        id={store.id}
        className={"claude-rail-peek claude-rail-door-peek " + className + " favs-on" + (visible ? " is-on" : "") + (resizing ? " is-resizing" : "")}
        role="dialog"
        aria-label={ariaLabel}
        style={box ? { left: box.left, top: box.top, width: box.width, height: box.height } : undefined}
        onPointerEnter={() => intent.enterCard()}
        onPointerLeave={() => intent.leaveCard()}
        onPointerDownCapture={() => {
          claim();
          intent.pressStart();
        }}
        onClickCapture={claim}
        onKeyDownCapture={claim}
        onFocusCapture={onFocusCapture}
        onBlurCapture={onBlurCapture}
        onKeyDown={onKeyDown}
      >
        {visible &&
          ALL_GRIPS.map((corner) => (
            <div
              key={corner}
              className={"claude-rail-peek-grip is-" + corner + (resizing === corner ? " is-active" : "")}
              aria-hidden="true"
              onPointerDown={onGripDown(corner)}
              onPointerMove={onGripMove}
              onPointerUp={onGripUp}
              onPointerCancel={onGripUp}
              onLostPointerCapture={() => dragStart.current && endDrag(true)}
            />
          ))}
        <div className="claude-rail-peek-card">{children(api)}</div>
      </div>
    </>,
    document.body,
  );
}

function sameBox(a: Box, b: Box): boolean {
  return a.left === b.left && a.top === b.top && a.width === b.width && a.height === b.height;
}
