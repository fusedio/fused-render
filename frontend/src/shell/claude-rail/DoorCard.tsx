// WINDOW A'S CHASSIS FOR A NAV DOOR'S CARD (owner, 2026-11-07 / 2026-11-09):
// the Tasks card (TasksPeek), the Bots card (BotsPeek) and the Git card
// (GitPeek, 2026-11-10) are one kind of
// thing — a page's content in window A's frame, off one of the rail's nav
// doors — and this is the frame: everything that is not the content. It was
// the Tasks card's body until the Bots card wanted the very same behaviour
// ("make sure window A behaviour is like Tasks' behaviour, and you cover all
// edge cases"): one code path, so the two cannot drift.
//
// WHAT IT DOES, for the store it is given (door-card-store):
//   - registers a card while `enabled` (a Bots card only where the server
//     offers Bots), and says whether it has ROOM beside
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
// EITHER SIDE (`side`; owner, 2026-11-27, "Left sidebar preview"): the collapsed
// LEFT sidebar's icons open this same card (shell/left-rail LeftPeek), to the
// RIGHT of that rail. `side: "left"` places it there by mirroring the boxes
// (rail-side-lib), gives it the right-hand grips, a bridge and cone on that
// side — and keeps it OUT of the Claude rail's windows: no window B to keep
// clear of, no window A box published, no pusher, no magnet, no pin mark
// (its store has no `pinWord`), its press its own view's (`viewFlag`), its
// move and size its own (`ownMove`, `sizeStore`). The right side is untouched.
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
import { publishWindowABox, setSnapGuides } from "./card-snap";
import { viewPress } from "./rail-pin-lib";
import { usePanelRailOn } from "./rail-panel";
import { PinWindowA } from "./RailPin";
import {
  FLOAT_GRIPS,
  RAIL_HEAD_SELECTOR,
  floatPlacement,
  floatResize,
  isDockEdge,
  snapResize,
  viewportOf,
  type MoveEdge,
} from "./rail-move-lib";
import { freezeFromDock, magnetBox, usePusher, useWindowBox } from "./rail-move-store";
import { drawnBox, useCardMove, useRailResizing } from "./useCardMove";
import { useOwnCardMove, type OwnMoveStore } from "./useOwnCardMove";
import { bridgeBox, inSafeConeBeside, placeBeside, type RailSide } from "./rail-side-lib";
import type { PressFlag } from "./RailPin";
import { railExtrasNow, useRailExtras } from "./rail-extras-flag";
import { placeColumn } from "./rail-slots-lib";
import { watchRailWidth } from "./rail-width-watch";
import {
  PEEK_MAX_WIDTH,
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
  type StoredSize,
} from "./rail-peek-lib";

const ALL_GRIPS: readonly ResizeEdge[] = ["l", "t", "b", "tl", "bl"];
/** A LEFT card's docked grips: its left edge stands against the rail. */
const LEFT_GRIPS: readonly MoveEdge[] = ["r", "t", "b", "tr", "br"];

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
   *  once the hop went (a leave guard may refuse it — FavouritePeek's rule).
   *  Resolves false when refused. */
  openPage(href?: string): Promise<boolean>;
  /** The head's × — the keyboard's seat with the list up. */
  closeRef: RefObject<HTMLButtonElement>;
  /** The chat head's Back — the keyboard's seat with a chat up. */
  backRef: RefObject<HTMLButtonElement>;
  /** A chat's own Escape, let through: closes a preview, never a pinned card. */
  onSubEscape(): void;
  /** THE PIN ICON (rail-pin-lib): the card's pin (`held`) and its toggle,
   *  for the content's head (RailPinButton). */
  pin: { pinned: boolean; onToggle: () => void };
}

export interface DoorCardProps {
  store: DoorCardStore;
  /** The door's page: where a press with no room goes, Open's default. */
  href?: string;
  /** A door with no page of its own (the Git door): what a press that found
   *  no room does instead of navigating to `href` (the page's Git panel). */
  onNoRoomPress?: () => void;
  /** The card's own class beside `.claude-rail-peek` (its CSS hook). */
  className: string;
  ariaLabel: string;
  /** The rail the card stands beside, where the content says (the Git card:
   *  the rail whose door opened it). Absent: the top rail
   *  (`useFavPeekState().rail`), and a PREVIEW never outlives it. Given, the
   *  content owns that rule (git-peek-store `forgetGitCardRail`). */
  rail?: HTMLElement | null;
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
  /** Which rail the card stands beside (header "EITHER SIDE"): the Claude
   *  rail's (`right`, the default) or the collapsed left sidebar's. */
  side?: RailSide;
  /** LEFT: where the card floats once pinned and moved (header). */
  ownMove?: OwnMoveStore;
  /** LEFT: the card's own remembered size (window A's `canvasRailPeek` is
   *  the Claude rail's; a left card reads it until it has its own). */
  sizeStore?: { get(): StoredSize | null; set(size: StoredSize): void };
  /** The flag a press inside raises for its view's outside-press watchers
   *  (rail-pin-lib `viewPress`, the Claude rail's, unless given). */
  viewFlag?: PressFlag;
  /** LEFT: the scroll box the door stands in, if any — the card holds at
   *  its edge while the door is scrolled out of it (rail-peek-lib
   *  `clampAnchor`). */
  scrollerOf?: (door: HTMLElement) => HTMLElement | null;
  /** LEFT: a card whose content is short (the "+ New app" composer) stands no
   *  taller than this while the reader has dragged no size of their own. */
  wishHeight?: number;
  children: (api: DoorCardApi) => ReactNode;
}

export function DoorCard({
  store,
  href,
  onNoRoomPress,
  rail: railProp,
  className,
  ariaLabel,
  enabled = true,
  subOpen,
  onClaim,
  onVisible,
  side = "right",
  ownMove,
  sizeStore,
  viewFlag = viewPress,
  scrollerOf,
  wishHeight,
  children,
}: DoorCardProps) {
  const on = enabled;
  const left = side === "left";
  const panelOn = usePanelRailOn();
  const card = store.useCard();
  // …and window B's box (`favBox`): this card is window A's and keeps CLEAR
  // of the favourite's or the bot's card — above it, else to its left
  // (rail-peek-lib `placeClear`) — never closes it (owner, 2026-11-08).
  // A LEFT card is no window of the Claude rail's: nothing to keep clear of.
  const fav = useFavPeekState();
  const favBox = left ? null : fav.favBox;
  const sizeRef = useRef(sizeStore);
  sizeRef.current = sizeStore;
  /** This card's size: window A's (header), or a left card's own. */
  const cardSizeNow = (): { width: number; height: number } =>
    left ? railCardSize(sizeRef.current?.get() ?? getUiLayout().canvasRailPeek ?? null) : cardSize();
  const rail = railProp === undefined ? fav.rail : railProp;
  const railGiven = railProp !== undefined;
  const intent = store.intent();
  // HOVER / TEMP / PIN (rail-pin-lib): a press makes the card temp and only
  // its pin holds it.

  // A card stands behind every rail's door while enabled (the store's
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
  // next page's rail (header). With the pin (rail-pin-lib `hopKeeps`), only a
  // HELD card follows: a temp one goes with the hop too.
  const railWas = useRef(rail);
  useEffect(() => {
    if (railWas.current === rail) return;
    railWas.current = rail;
    const s = store.state();
    if (!railGiven && !s.held) store.close();
  }, [rail, store, railGiven]);

  const up = on && card.up;
  // A PINNED WINDOW MOVES (rail-move-lib; owner, 2026-11-17): every door card
  // is window A's. Pinned and dragged by its head, it FLOATS at the window's
  // position — kept through a swap to a ring, another door or the + card.
  // FREE PINS (many pins, 2026-11-24): at ITS OWN pin's box, under its word
  // (the store's `pinWord`) — every door card a pin of its own.
  const togglePinRef = useRef<() => void>(() => {});
  const railMove = useCardMove({
    win: "a",
    pinId: left ? null : store.pinWord,
    movable: left ? false : card.held,
    headSelector: RAIL_HEAD_SELECTOR,
    shown: up && !left ? String(card.seq) : null,
    // A non-pin's head drag pins it (useCardMove `dragPins`): the pin icon's
    // own press (`togglePin`, below).
    // …never beside the workspace's panel (rail-panel.ts): A does not pin.
    dragPins: !left && !card.held && up && !panelOn ? () => togglePinRef.current() : null,
  });
  // A LEFT card moves through its own store (header "EITHER SIDE").
  const ownCardMove = useOwnCardMove({
    store: left ? (ownMove ?? null) : null,
    key: store.key,
    movable: left && card.held,
    headSelector: RAIL_HEAD_SELECTOR,
    dragPins: left && !card.held && up ? () => togglePinRef.current() : null,
  });
  const move = left ? ownCardMove : railMove;
  const floatingRef = useRef(false);
  floatingRef.current = move.floating;
  // FREE PINS (rail-slots-lib): pinned, the card is frozen at the box its own
  // pin's id holds (its word; many pins, 2026-11-24); not pinned, it shares the rail's column with window
  // B's non-pin and pushes the pins it would cover.
  const freePins = useRailExtras();
  const freePinsRef = useRef(false);
  freePinsRef.current = freePins;

  // WHERE IT STANDS (header): left of the rail, its top level with the rail's
  // door, at window A's size, clamped inside the row.
  const cardRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const backRef = useRef<HTMLButtonElement | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  const boxRef = useRef<Box | null>(null);
  boxRef.current = box;
  const [shownOn, setShownOn] = useState(false);
  const row = useMemo(() => rowOf(rail), [rail]);
  // THE EXPANDABLE RAIL (rail-width-watch; Rail extras, the free pins' own
  // switch): its edge moves with no window resize and no row reshaping — the
  // card follows it too.
  // …and the LEFT sidebar's edge moves the same way (collapsed ↔ expanded,
  // its width dragged): a left card always follows it.
  const followRailWidth = left || freePins;
  const railLeft = useRef(0);
  const door = store.doorOf(rail);
  // A LEFT card that FLOATS stands with no rail beside it (the sidebar
  // expanded under a pinned card): it needs no door to stand by.
  // …and a HELD left card whose icon or row the sidebar's other layout does
  // not draw keeps the box it last stood in.
  const want = up && !noRoom && ((left && move.floating) || (!!rail && (!!door || (left && card.held && box !== null))));
  const place = (): Box | null => {
    // FLOATING: at the window's position, window A's size, inside the
    // viewport; never "no room" (rail-move-lib).
    const at = floatingRef.current ? move.at() : null;
    if (at) return floatPlacement(at, (freePinsRef.current || left ? move.size() : null) ?? cardSizeNow(), viewportOf(window));
    const d = store.doorOf(rail);
    if (left && rail && !d && store.state().held && boxRef.current) return boxRef.current;
    if (!rail || !d || !row) return null;
    const r = rail.getBoundingClientRect();
    if (left) {
      // Beside the left sidebar: the mirror of the Claude rail's placement
      // (rail-side-lib `placeBeside`); the near edge is the rail's right.
      railLeft.current = r.left + r.width;
      const size = cardSizeNow();
      const scroller = scrollerOf?.(d) ?? null;
      return placeBeside("left", {
        door: d.getBoundingClientRect(),
        rail: r,
        row: leftRowBox(row),
        avoid: null,
        wish: {
          width: size.width,
          maxHeight: wishHeight !== undefined && !sizeRef.current?.get() ? Math.min(size.height, wishHeight) : size.height,
        },
        scroller: scroller ? scroller.getBoundingClientRect() : null,
        viewportWidth: window.innerWidth,
      });
    }
    railLeft.current = r.left;
    const size = cardSize();
    return (freePinsRef.current ? placeColumn : placeClear)(d.getBoundingClientRect(), r, row.getBoundingClientRect(), favBox, {
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
    // A left door's click is its own navigation (shell/left-rail), never this.
    if (!pressed || left) return;
    if (onNoRoomPress) onNoRoomPress();
    else if (href) void navigateUrl(href);
  };

  // THE GRIPS AND THEIR DRAG (RailPeek's; window A's key, `canvasRailPeek`).
  const [resizing, setResizing] = useState<MoveEdge | null>(null);
  const resizingRef = useRef<MoveEdge | null>(null);
  resizingRef.current = resizing;
  useRailResizing(!left && resizing !== null);
  const dragStart = useRef<{
    x: number;
    y: number;
    box: Box;
    pointerId: number;
    corner: MoveEdge;
    el: HTMLElement;
    floating: boolean;
    /** Window B's card as it stood at the press, for the held edge's magnet
     *  (rail-move-lib `snapResize`): read once, never per move. */
    other: Box | null;
  } | null>(null);
  const dragLast = useRef<Box | null>(null);
  const onGripDown = (corner: MoveEdge) => (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || !box) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragStart.current = {
      x: e.clientX,
      y: e.clientY,
      box,
      pointerId: e.pointerId,
      corner,
      el: e.currentTarget,
      floating: move.floating,
      other: left ? null : magnetBox("b", move.floating, { self: move.key, box }),
    };
    dragLast.current = null;
    setResizing(corner);
  };
  const onGripMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId) return;
    const dx = e.clientX - start.x;
    const dy = e.clientY - start.y;
    // FLOATING (rail-move-lib `floatResize`): inside the viewport, with the
    // held edge's magnet to window B's card as it stands and to the app
    // window (`snapResize`; owner, 2026-11-18).
    if (start.floating || !isDockEdge(start.corner)) {
      const lim = { minWidth: PEEK_MIN_WIDTH, minHeight: PEEK_MIN_HEIGHT, maxWidth: PEEK_MAX_WIDTH };
      const view = viewportOf(window);
      const pulled = snapResize(start.corner, dx, dy, (x, y) => floatResize(start.corner, start.box, x, y, view, lim), start.other, view);
      setSnapGuides(pulled.x, pulled.y);
      const fl = pulled.box;
      dragLast.current = fl;
      setBox((prev) => (prev && sameBox(prev, fl) ? prev : fl));
      return;
    }
    if (!row) return;
    // Never over window B's card under it (RailPeek's own floor,
    // rail-peek-lib `bottomClear`; owner, 2026-11-08).
    const bounds = left ? leftRowBox(row) : row.getBoundingClientRect();
    const dock = start.corner;
    const floor = bottomClear(start.box, favBox);
    let next: Box;
    // WITH THE PIN (window B's card may float): the held edge's magnet on
    // either axis (`snapResize`) — the width magnet is its left↔left case.
    const pulled = snapResize(dock, dx, dy, (x, y) => dragResize(dock, start.box, x, y, bounds, floor), start.other, viewportOf(window));
    setSnapGuides(pulled.x, pulled.y);
    next = pulled.box;
    dragLast.current = next;
    setBox((prev) => (prev && sameBox(prev, next) ? prev : next));
  };
  const endDrag = (save: boolean) => {
    const start = dragStart.current;
    dragStart.current = null;
    setResizing(null);
    setSnapGuides([], []);
    const last = dragLast.current;
    dragLast.current = null;
    if (start && start.el.hasPointerCapture?.(start.pointerId)) start.el.releasePointerCapture(start.pointerId);
    // A FREE PIN'S RESIZE IS THE PIN'S ALONE (rail-slots-lib): the window's
    // frozen box, never the shared size. A LEFT card's floating box is its
    // own store's, whichever pin model.
    if (start?.floating && (freePinsRef.current || left)) {
      if (save && last && !sameBox(last, start.box)) move.commitBox(last);
      return;
    }
    // A FLOATING card's left or top grip moved its corner: the window stands
    // there now (rail-move-lib).
    if (save && start?.floating && last && (last.left !== start.box.left || last.top !== start.box.top))
      move.setPos({ left: last.left, top: last.top });
    if (!save || !start || !last || (last.width === start.box.width && last.height === start.box.height)) return;
    // Only a dimension the drag changed, past the grips' minimums (RailPeek
    // `endDrag`); the other keeps what was remembered.
    const prev = cardSizeNow();
    const width = last.width !== start.box.width && last.width >= PEEK_MIN_WIDTH ? last.width : prev.width;
    const height = last.height !== start.box.height && last.height >= PEEK_MIN_HEIGHT ? last.height : prev.height;
    // A LEFT card's size is its own (never window A's).
    if (left) {
      sizeRef.current?.set({ width, height });
      return;
    }
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
  // A card that leaves the page under a resize takes its guides with it (no
  // pointerup will come to clear them; reviewer, 2026-11-18).
  useEffect(
    () => () => {
      if (dragStart.current) setSnapGuides([], []);
    },
    [],
  );
  // Its live box for window B's head drag (rail-move-store `windowBox`).
  // None of it for a LEFT card: it is no window of the Claude rail's.
  useWindowBox("a", visible && !left ? box : null, freePins && move.floating && !left ? move.key : null);
  // A clicked-open non-pin pushes the pins it covers (rail-slots-lib).
  usePusher("a", !left && freePins && visible && !move.floating && card.pinned ? box : null);
  // Window A's box, for window B's grips to snap to (card-snap.ts).
  // …but not where it FLOATS: window B places itself as if it were not there.
  useEffect(() => {
    if (left || !visible || !box || move.floating) return;
    publishWindowABox("door:" + className, box);
    return () => publishWindowABox("door:" + className, null);
  }, [visible, box, className, move.floating, left]);
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
    const unwatch = followRailWidth ? watchRailWidth(rail, re) : null;
    // A LEFT door in the rail's scroll run moves with its scroll (no render
    // follows a scroll): the card follows it, held at the run's edge.
    if (left) rail.addEventListener("scroll", re, true);
    return () => {
      window.removeEventListener("resize", re);
      ro?.disconnect();
      unwatch?.();
      if (left) rail.removeEventListener("scroll", re, true);
    };
  }, [want, rail, row, favBox, followRailWidth, left]);
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
    // None for a FLOATING card: it is pinned, and stands nowhere near its door.
    if (!shownOn || !box || !door || move.floating) return;
    const r = door.getBoundingClientRect();
    const apex = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    const onMove = (e: PointerEvent) => {
      const inCard = cardRef.current?.contains(e.target as Node) ?? false;
      setCone(
        !inCard &&
          (left ? inSafeConeBeside("left", { x: e.clientX, y: e.clientY }, apex, box, window.innerWidth) : inSafeCone({ x: e.clientX, y: e.clientY }, apex, box)),
      );
    };
    document.addEventListener("pointermove", onMove, true);
    return () => {
      document.removeEventListener("pointermove", onMove, true);
      setCone(false);
    };
  }, [shownOn, box, door, move.floating, left]);

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
  // WITH THE PIN (owner, 2026-11-16: "a consistent behaviour"): a hover and a
  // TEMP card close on Escape, only a pinned (held) one ignores it.
  const escapeKeeps = card.held;
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape" || e.defaultPrevented || escapeKeeps) return;
    const t = e.target as HTMLElement;
    const pop = t.closest("[role='menu'], [role='listbox'], [role='dialog'], [role='alertdialog']");
    if (pop && pop !== cardRef.current) return;
    e.preventDefault();
    close();
  };
  // …and with the focus nowhere (a hover's preview: the pointer brought it,
  // the keyboard is on the body), the document's Escape does the same.
  const previewing = visible && !escapeKeeps;
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
    const s = store.state();
    if (!s.held) close();
  }, [close, store]);
  // OPEN: the page itself; the card goes once the hop went (a leave guard
  // may refuse it — FavouritePeek's rule).
  const openPage = useCallback(
    (to = href): Promise<boolean> =>
      to === undefined
        ? Promise.resolve(false)
        : navigateUrl(to).then((went) => {
            if (went !== false) close();
            return went;
          }),
    [close, href],
  );

  const held = card.held;
  const togglePin = useCallback(() => {
    const held = store.state().held;
    // FREE PINS: a pinned card's seat is its × — it closes (rail-slots-lib "A
    // PINNED WINDOW CLOSES BY ITS ×").
    // The card's own close (`close`), not the store's alone: it hands the
    // keyboard back to the door first (CodeRabbit on #2349 — the × pressed
    // by keyboard left the focus in a card gone inert).
    if (held && railExtrasNow()) {
      close();
      return;
    }
    // …and the pin's press freezes the window where it stands, before the
    // hold makes it float (rail-move-store `freezeFromDock`).
    // A LEFT card freezes in its own store, at the box it stands in now.
    if (!held && railExtrasNow()) {
      if (left) {
        const b = boxRef.current;
        if (b && ownMove) {
          ownMove.setSize(store.key, { width: b.width, height: b.height });
          ownMove.setPos(store.key, { left: b.left, top: b.top });
        }
      } else if (store.pinWord) freezeFromDock("a", store.pinWord);
    }
    store.setHeld(!held);
  }, [store, close, left, ownMove]);
  togglePinRef.current = togglePin;
  const api = useMemo<DoorCardApi>(
    () => ({
      visible,
      pinned: card.pinned,
      close,
      openPage,
      closeRef,
      backRef,
      onSubEscape,
      pin: { pinned: held, onToggle: togglePin },
    }),
    [visible, card.pinned, close, openPage, onSubEscape, held, togglePin],
  );

  if (!on) return null;
  // None while FLOATING.
  const bridge = left
    ? !move.floating && box
      ? bridgeBox("left", box, railLeft.current)
      : null
    : !move.floating && box && railLeft.current > box.left + box.width + 1
      ? { left: box.left + box.width, top: box.top, width: Math.max(0, railLeft.current - 3 - (box.left + box.width)), height: box.height }
      : null;

  // A FREE PIN IS DRAWN WHERE ITS WINDOW STANDS THIS RENDER (owner, 2026-11-19:
  // a pushed pin trailed the edge of the window being resized): `box` follows
  // the window's position through a placement effect, a render or two behind;
  // the position itself (`move.pos`, pushed and all) is in hand now. The size
  // is the box's. Not while this card's own grip is held: the grip moves it.
  const drawn = (freePins || left) && move.floating && !resizing ? drawnBox(box, move.pos) : box;
  return createPortal(
    <>
      {bridge && (
        <div
          className={"claude-rail-peek-bridge" + (left ? " is-left" : "") + (visible ? " is-on" : "")}
          aria-hidden="true"
          style={bridge}
          onPointerEnter={() => setCone(true)}
          onPointerLeave={() => setCone(false)}
        />
      )}
      <div
        ref={cardRef}
        id={store.id}
        className={
          "claude-rail-peek claude-rail-door-peek " +
          className +
          (left ? " is-left" : "") +
          " favs-on" +
          (visible ? " is-on" : "") +
          (resizing ? " is-resizing" : "") +
          move.className
        }
        role="dialog"
        aria-label={ariaLabel}
        // A free pin stands in the pins' own stacking (useCardMove `z`).
        style={drawn ? { left: drawn.left, top: drawn.top, width: drawn.width, height: drawn.height, ...(move.z !== null ? { zIndex: move.z } : {}) } : undefined}
        onPointerEnter={() => intent.enterCard()}
        onPointerLeave={() => intent.leaveCard()}
        onPointerDownCapture={() => {
          claim();
          // The rail's outside-press watcher reads it (rail-pin-lib).
          store.press.inside = true;
          viewFlag.inside = true;
          move.notePress();
          intent.pressStart();
        }}
        onPointerDown={move.onPointerDown}
        onDoubleClick={move.onDoubleClick}
        onClickCapture={claim}
        onKeyDownCapture={claim}
        onFocusCapture={onFocusCapture}
        onBlurCapture={onBlurCapture}
        onKeyDown={onKeyDown}
      >
        {visible &&
          // …and, FLOATING, the right edge and corners too (rail-move-lib).
          (move.floating ? FLOAT_GRIPS : left ? LEFT_GRIPS : ALL_GRIPS).map((corner) => (
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
        {visible &&
          (move.floating ? FLOAT_GRIPS : left ? LEFT_GRIPS : ALL_GRIPS).map((e) => <div key={"ring-" + e} className={"rail-grip-ring is-" + e} aria-hidden="true" />)}
        <div className="claude-rail-peek-card">
          {/* A door card on the rail is window A's: no pin beside the
              workspace's panel (RailPin `PinWindowA`). */}
          <PinWindowA.Provider value={!left}>{children(api)}</PinWindowA.Provider>
        </div>
      </div>
    </>,
    document.body,
  );
}

/** A LEFT card's row: the sidebar's row (`#app`) across, but only as tall as
 *  the page's own row (`.shell-main-row`) — the sidebar runs the window's
 *  whole height, past the status bar and the terminal drawer, and a card
 *  must not stand over those. */
function leftRowBox(row: HTMLElement): Box {
  const r = row.getBoundingClientRect();
  const main = row.querySelector(".shell-main-row")?.getBoundingClientRect();
  if (!main || main.height <= 0) return { left: r.left, top: r.top, width: r.width, height: r.height };
  return { left: r.left, top: main.top, width: r.width, height: main.height };
}

function sameBox(a: Box, b: Box): boolean {
  return a.left === b.left && a.top === b.top && a.width === b.width && a.height === b.height;
}
