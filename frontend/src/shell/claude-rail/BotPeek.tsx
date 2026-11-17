// THE BOT CARD OFF THE CLAUDE RAIL (owner, 2026-11-05: "a small overlay that
// just displays the chat exchange of the bot, similar as we have for tasks —
// the floating box appears with the same exact behavior as the task floating
// box; the only difference is that this has the bot's chat instead. No
// browser, just the chat UI"). The rail's PINNED BOTS (ClaudeRail
// `.claude-rail-bots`) used to be plain doors to the bot's page; now each
// ring opens this card the way a list ring opens the rail's card (RailPeek):
// a hover previews it, a press pins it (ClaudeRail's `botIntent`, the same
// PeekIntent machine), × / Escape / an outside press close it, and the
// head's Open is the door to the bot's page. Behind `canvas_rail_peek_enabled`
// like the rail's card; the pinned bots stand only with the favourites on.
//
// WHAT IT IS: the bot's chat itself — apps/bots `BotChat`: the Bots page's
// thread and composer for one bot, fed without the page — in the card the
// rail's own card draws: the same classes (`.claude-rail-peek`, claude-
// rail.css), the same two-frame show, the same fade, the same bridge and
// safe cone, the same inert-while-hidden rule. The chat chunk is lazy
// (`BotChat` behind React.lazy): the Bots page's thread and its markdown
// stack are not the shell's to carry until a bot's ring is hovered
// (`preloadBotChat`, the intent's `warm`).
//
// IT IS WINDOW B (owner, 2026-11-06: "in FAV make the bot and the chat the
// same — sharing the same window B; whether you open a fav task or a bot,
// both share the same window; the UI inside may differ, but no resize, no
// jitter, no overlap"): it stands where the favourite's card stands
// (FavouritePeek) — its bottom on the rail's, bottom right, the rail itself
// the anchor (rail-peek-lib `peekPlacement`, `align: "bottom"`) — at that
// card's size (`FAV_WIDTH` × `FAV_HEIGHT` until dragged; the drag lands in
// the favourite card's own `canvasRailFav`, so the two are one size from
// then on; never taller than half the window, `favCardWish`), hovered or
// pinned. One of the two is up at a time (ClaudeRail; fav-peek-store
// `setBotCard`), and while this one is PINNED it publishes its box as window
// B's (`publishFavBox`, owner "bot"), which the rail's own card keeps clear
// of, as it does the favourite's. Until 2026-11-06 it hung off its ring at
// the list card's size.
//
// ONE MOUNT, AND NONE WHILE CLOSED: the shown bot's chat, kept through the
// fade-out and UNMOUNTED once that is done (`chatKey`, CHAT_LINGER_MS past
// the close) — the store's quiet feed stops with it, and no hidden thread
// goes on marking the bot read behind a closed card (Bugbot on #2143). The
// Bots page's store holds one thread's scroll bookkeeping (pinned, the new
// count), so this card never keeps a second bot's thread mounted behind the
// shown one the way the rail's card keeps chats warm. ON THE BOTS PAGE TOO
// (owner, 2026-11-06: "change the view only when we click Open"): the card
// stands beside the page's own thread, and keeps its scroll bookkeeping to
// itself (BotChat `sharedScroll` false) so the two never fight over the
// store's one pin. THE READ MARK is the pin's (BotChat `markRead`): a hover's
// glance over the card leaves the rail's unread dot alone; a press that
// pinned the card is the reader reading.
//
// The head is ONE row: the ×, the bot's face and name, its status in the
// rail's words (claude-rail-lib `botStatusWords`), and Open.
import {
  forwardRef,
  lazy,
  Suspense,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import type { BotPulseRow } from "@platform/lib/botsPulse";
import { getUiLayout, patchUiLayout, subscribeUiLayout } from "@platform/lib/ui-layout";
import { ChatChunkBoundary } from "@apps/claude/ChatMount";
import { Face } from "@apps/bots/components/Face";
import type { FaceSubject } from "@apps/bots/lib/face";
import { botStatusWords } from "./claude-rail-lib";
import { isBotsPath } from "@apps/bots/routes";
import { publishFavBox } from "./fav-peek-store";
import { RailPinButton } from "./RailPin";
import { setSnapGuide, windowABox } from "./card-snap";
import {
  FLOAT_GRIPS,
  RAIL_HEAD_SELECTOR,
  floatPlacement,
  floatResize,
  isDockEdge,
  viewportOf,
  wishSize,
  type MoveEdge,
} from "./rail-move-lib";
import { useWindowBox, windowPos } from "./rail-move-store";
import { useCardMove } from "./useCardMove";
import {
  FAV_HEIGHT,
  FAV_WIDTH,
  PEEK_MAX_WIDTH,
  PEEK_MIN_HEIGHT,
  PEEK_MIN_WIDTH,
  dragMaxWidth,
  dragResize,
  favCardWish,
  favMaxHeight,
  inSafeCone,
  peekPlacement,
  rowOf,
  snapWidth,
  type Box,
  type PeekWish,
  type ResizeEdge,
} from "./rail-peek-lib";

const BotChat = lazy(() => import("@apps/bots/components/BotChat"));

/** The chat chunk, ahead of the first show (ClaudeRail's bot intent `warm`). */
export function preloadBotChat(): void {
  void import("@apps/bots/components/BotChat");
}

export const BOT_PEEK_ID = "claude-rail-bot-peek";

/** How long the chat stays mounted past the close: the card's fade
 *  (claude-rail.css `--dur-med`), with room to spare. */
export const CHAT_LINGER_MS = 400;

/** The bot's page: the head's Open door, and the ring's own href. */
export function botHref(id: string): string {
  return "/bots/" + encodeURIComponent(id);
}

/** Window B's size (header): the favourite card's fixed wish, its bottom on
 *  the rail's (FavouritePeek `FAV_SIZE`). */
const FAV_SIZE: PeekWish = { width: FAV_WIDTH, maxHeight: FAV_HEIGHT, align: "bottom" };

/** The favourite card's remembered size (RailPeek `storedPeekSize`,
 *  ui-layout `canvasRailFav`): this card is that size — a bot's card dragged
 *  wider is the favourite's card dragged wider (header). */
function storedPeekSize(): { width: number; height: number } | null {
  const s = getUiLayout().canvasRailFav;
  if (!s) return null;
  return {
    width: Math.min(PEEK_MAX_WIDTH, Math.max(PEEK_MIN_WIDTH, s.width)),
    height: Math.max(PEEK_MIN_HEIGHT, s.height),
  };
}

const ALL_GRIPS: readonly ResizeEdge[] = ["l", "t", "b", "tl", "bl"];

export interface BotPeekHandle {
  /** The keyboard into the card: the composer, else its first button. */
  focusChat(): boolean;
  /** The keyboard into the composer and nowhere else; false until it has
   *  mounted (the chat chunk may still be loading). */
  focusComposer(): boolean;
}

export interface BotPeekProps {
  /** The card's DOM id — what the ring's `aria-controls` names. */
  id: string;
  /** The bot the card shows, or null while closed (the last one stays drawn
   *  through the fade-out). */
  shown: BotPulseRow | null;
  /** The ring element for a bot (its anchor), or null if it is not drawn. */
  anchorOf: (id: string) => HTMLElement | null;
  /** The rail: the card's anchor (its bottom on the rail's, header), its
   *  edge, and the row it stands in. */
  railEl: HTMLElement | null;
  onPointerEnter: () => void;
  onPointerLeave: () => void;
  /** In (`true`) or out (`false`) of the safe cone / the bridge. */
  onCone: (inside: boolean) => void;
  /** A pointer went down inside the card (React's capture phase, across the
   *  chat's portals). */
  onPressInside: () => void;
  /** The card took (`true`) or lost (`false`) the keyboard. */
  onHold: (on: boolean) => void;
  /** Escape, the ×, or the Open door (`"door"`). */
  onDismiss: (why?: "door") => void;
  /** No room for a card in the row (rail-peek-lib `peekPlacement` null). */
  onNoRoom: () => void;
  /** The Open door: the bot's page. */
  onOpen: (href: string) => void;
  /** HELD (a press pinned it, favourites on): only its × or Open let go. */
  held: boolean;
  onHeld: (held: boolean) => void;
  /** PINNED by a press (PeekIntent), not a passing hover. */
  pinned: boolean;
  /** HOVER / TEMP / PIN (rail-pin-lib; the unified-head switch on): the
   *  head's pin icon, its first button (no × with it) — `pinned` the card's pin, `onToggle` its
   *  press. Given, Escape closes a hover and a temp card; only a pinned one
   *  ignores it. Absent: the card as it was. */
  pinCtl?: { pinned: boolean; onToggle: () => void } | null;
}

export const BotPeek = forwardRef<BotPeekHandle, BotPeekProps>(function BotPeek(
  { id, shown, anchorOf, railEl, onPointerEnter, onPointerLeave, onCone, onPressInside, onHold, onDismiss, onNoRoom, onOpen, held, onHeld, pinned, pinCtl = null },
  ref,
) {
  const cardRef = useRef<HTMLDivElement | null>(null);
  // A PINNED WINDOW MOVES (rail-move-lib; owner, 2026-11-17): this card is
  // window B's, as the favourite's is. Pinned and dragged by its head, it
  // FLOATS at the window's position — kept through a swap to a favourite.
  const move = useCardMove({
    win: "b",
    on: !!pinCtl,
    movable: !!pinCtl?.pinned,
    headSelector: RAIL_HEAD_SELECTOR,
    shown: shown?.id ?? null,
  });
  const floatingRef = useRef(false);
  floatingRef.current = move.floating;
  // The last bot shown, so the fade-out has something to fade.
  const last = useRef<BotPulseRow | null>(null);
  if (shown) last.current = shown;
  const view = shown ?? last.current;
  const [box, setBox] = useState<Box | null>(null);
  const [on, setOn] = useState(false);
  const shownKey = shown?.id ?? null;
  // The chat mounted (header): the shown bot's at once; none, CHAT_LINGER_MS
  // after the close.
  const [chatKey, setChatKey] = useState<string | null>(null);
  useEffect(() => {
    if (shownKey) {
      setChatKey(shownKey);
      return;
    }
    const id = window.setTimeout(() => setChatKey(null), CHAT_LINGER_MS);
    return () => window.clearTimeout(id);
  }, [shownKey]);

  // A PRESS INSIDE (ClaudeRail's outside-press rule reads the flag).
  const onPressInsideCapture = () => {
    move.notePress();
    onPressInside();
  };
  // ONE SIZE, hovered or pinned, the favourite card's (header).
  const wish = () => favCardWish(FAV_SIZE, storedPeekSize(), window.innerHeight);
  const railLeft = useRef(0);
  const row = useMemo(() => rowOf(railEl), [railEl]);
  // WHERE IT STANDS (header): the rail is the anchor — with `align:
  // "bottom"` the card's bottom lands on the rail's, clamped inside the row
  // — bottom right, whichever ring opened it (FavouritePeek `anchorOf`).
  const place = (key: string): Box | null => {
    // FLOATING: at the window's position, its size, inside the viewport;
    // never "no room" (rail-move-lib).
    const at = floatingRef.current ? windowPos("b") : null;
    if (at) return floatPlacement(at, wishSize(wish()), viewportOf(window));
    if (!anchorOf(key) || !railEl || !row) return null;
    const rail = railEl.getBoundingClientRect();
    railLeft.current = rail.left;
    return peekPlacement(rail, rail, row.getBoundingClientRect(), wish());
  };

  // THE GRIPS AND THEIR DRAG (RailPeek; the favourite card's key,
  // `canvasRailFav` — header).
  const [resizing, setResizing] = useState<MoveEdge | null>(null);
  const resizingRef = useRef<MoveEdge | null>(null);
  resizingRef.current = resizing;
  const dragStart = useRef<{
    x: number;
    y: number;
    box: Box;
    pointerId: number;
    corner: MoveEdge;
    el: HTMLElement;
    floating: boolean;
    floor: number | undefined;
    cap: number;
  } | null>(null);
  const dragLast = useRef<Box | null>(null);
  const onGripDown = (corner: MoveEdge) => (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || !box) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    const floating = move.floating;
    // The card stands on the rail's bottom: a bottom drag stops there, and
    // the height never passes half the window (RailPeek, the favourite's).
    // Floating: the viewport's bottom.
    const floor = !floating && railEl ? railEl.getBoundingClientRect().bottom : undefined;
    const cap = favMaxHeight(window.innerHeight);
    dragStart.current = {
      x: e.clientX,
      y: e.clientY,
      box,
      pointerId: e.pointerId,
      corner,
      el: e.currentTarget,
      floating,
      floor,
      cap,
    };
    dragLast.current = null;
    setResizing(corner);
  };
  const onGripMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId) return;
    // FLOATING (rail-move-lib `floatResize`): inside the viewport, no magnet.
    if (start.floating || !isDockEdge(start.corner)) {
      const fl = floatResize(start.corner, start.box, e.clientX - start.x, e.clientY - start.y, viewportOf(window), {
        minWidth: PEEK_MIN_WIDTH,
        minHeight: PEEK_MIN_HEIGHT,
        maxWidth: PEEK_MAX_WIDTH,
        maxHeight: start.cap,
      });
      dragLast.current = fl;
      setBox((prev) => (prev && prev.left === fl.left && prev.top === fl.top && prev.width === fl.width && prev.height === fl.height ? prev : fl));
      return;
    }
    if (!row) return;
    const bounds = row.getBoundingClientRect();
    const asked = dragResize(start.corner, start.box, e.clientX - start.x, e.clientY - start.y, bounds, start.floor, start.cap);
    // THE WIDTH MAGNET (card-snap.ts): to window A's card.
    const snapped = snapWidth(asked, start.corner, windowABox(), PEEK_MIN_WIDTH, dragMaxWidth(start.box, bounds));
    setSnapGuide(snapped.guide);
    const next = snapped.box;
    dragLast.current = next;
    setBox((prev) => (prev && prev.left === next.left && prev.top === next.top && prev.width === next.width && prev.height === next.height ? prev : next));
  };
  const endDrag = (save: boolean) => {
    const start = dragStart.current;
    dragStart.current = null;
    setResizing(null);
    setSnapGuide(null);
    const lastBox = dragLast.current;
    dragLast.current = null;
    if (start && start.el.hasPointerCapture?.(start.pointerId)) start.el.releasePointerCapture(start.pointerId);
    // A FLOATING card's left or top grip moved its corner: the window stands
    // there now (rail-move-lib).
    if (save && start?.floating && lastBox && (lastBox.left !== start.box.left || lastBox.top !== start.box.top))
      move.setPos({ left: lastBox.left, top: lastBox.top });
    if (!save || !start || !lastBox || !shownKey || (lastBox.width === start.box.width && lastBox.height === start.box.height)) return;
    // Only a dimension the drag changed, and only past the grips' minimums
    // (RailPeek `endDrag`); the other keeps what was remembered.
    const prev = storedPeekSize();
    const pick = (changed: boolean, got: number, min: number, kept: number | undefined, fallback: number) =>
      changed && got >= min ? got : (kept ?? fallback);
    const width = pick(lastBox.width !== start.box.width, lastBox.width, PEEK_MIN_WIDTH, prev?.width, FAV_WIDTH);
    const height = pick(lastBox.height !== start.box.height, lastBox.height, PEEK_MIN_HEIGHT, prev?.height, FAV_HEIGHT);
    patchUiLayout((d) =>
      d.canvasRailFav?.width === width && d.canvasRailFav?.height === height ? d : { ...d, canvasRailFav: { width, height } },
    );
  };
  const onGripUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId) return;
    endDrag(true);
  };
  const visible = on && box !== null && shownKey !== null;
  const gripsOn = visible;
  // WINDOW B'S BOX (header): published while PINNED — what the rail's own
  // card keeps clear of; a hover overlaps, as the favourite's does. As the
  // bot owner: a close here never clears a box the favourite's card holds.
  // …but not where it FLOATS: window A places itself as if it were not there.
  useEffect(() => {
    publishFavBox(visible && pinned && !move.floating ? box : null, "bot");
  }, [visible, pinned, box, move.floating]);
  useEffect(() => () => publishFavBox(null, "bot"), []);
  // Its live box for window A's head drag (rail-move-store `windowBox`).
  useWindowBox("b", !!pinCtl && visible ? box : null);
  const dragKey = useRef<string | null>(null);
  useEffect(() => {
    if (dragStart.current && (!gripsOn || dragKey.current !== shownKey)) endDrag(false);
    dragKey.current = shownKey;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gripsOn, shownKey]);
  const [, setLayoutTick] = useState(0);
  useEffect(() => subscribeUiLayout(() => setLayoutTick((n) => n + 1)), []);

  // PLACED, THEN SHOWN (RailPeek header): the box on this frame, the class
  // on the next; a swap glides under the class; a close keeps the box.
  useLayoutEffect(() => {
    if (!shownKey) {
      setOn(false);
      return;
    }
    const next = place(shownKey);
    if (!next) {
      onNoRoom();
      return;
    }
    setBox(next);
    const raf = requestAnimationFrame(() => setOn(true));
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey]);
  // FOLLOW THE RING while up: a resize, the rail's sections scrolling, the
  // row changing shape; plus every render, cheaply.
  useEffect(() => {
    if (!shownKey || !railEl) return;
    const re = () => {
      if (resizingRef.current) return;
      const next = place(shownKey);
      if (!next) {
        onNoRoom();
        return;
      }
      setBox((prev) => (prev && next.left === prev.left && next.top === prev.top && next.width === prev.width && next.height === prev.height ? prev : next));
    };
    window.addEventListener("resize", re);
    railEl.addEventListener("scroll", re, true);
    const ro = row && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (row && ro) ro.observe(row);
    return () => {
      window.removeEventListener("resize", re);
      railEl.removeEventListener("scroll", re, true);
      ro?.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey, railEl]);
  useEffect(() => {
    if (!shownKey || resizing) return;
    const next = place(shownKey);
    if (next && box && (next.top !== box.top || next.left !== box.left || next.width !== box.width || next.height !== box.height)) setBox(next);
  });

  // THE SAFE CONE (RailPeek; rail-peek-lib `inSafeCone`).
  const coneOn = useRef(false);
  const setCone = (inside: boolean) => {
    if (coneOn.current === inside) return;
    coneOn.current = inside;
    onCone(inside);
  };
  useEffect(() => {
    // None for a FLOATING card: it is pinned, and stands nowhere near its ring.
    if (!on || !box || !shownKey || move.floating) return;
    const anchor = anchorOf(shownKey);
    if (!anchor) return;
    const r = anchor.getBoundingClientRect();
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [on, box, shownKey, move.floating]);

  // INERT WHILE HIDDEN (RailPeek).
  useLayoutEffect(() => {
    cardRef.current?.toggleAttribute("inert", !visible);
  }, [visible]);

  const composerOf = () => cardRef.current?.querySelector<HTMLTextAreaElement>(".bots-peek textarea") ?? null;
  useImperativeHandle(
    ref,
    () => ({
      focusChat() {
        if (!shownKey) return false;
        const target = composerOf() ?? cardRef.current?.querySelector<HTMLElement>("button");
        if (!target) return false;
        target.focus({ preventScroll: true });
        return true;
      },
      focusComposer() {
        const ta = shownKey ? composerOf() : null;
        if (!ta) return false;
        ta.focus({ preventScroll: true });
        return true;
      },
    }),
    [shownKey],
  );

  // WHICH FOCUS HOLDS THE CARD (RailPeek): an editable, a popup of the
  // chat's, or focus the keyboard brought.
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
    onHold(editable || popup || keyboard);
  };
  const onBlurCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget as Node | null;
    if (next && e.currentTarget.contains(next)) return;
    onHold(false);
  };
  const heldRef = useRef(held);
  // …and with the pin (`pinCtl`) only a PINNED card: temp closes as a hover
  // does (owner, 2026-11-16: "a consistent behaviour").
  heldRef.current = pinCtl ? pinCtl.pinned : held;
  // ESCAPE closes an unheld card: on the head, and in the chat's own thread
  // and composer once they have let it through (the composer prevents the
  // default when Escape was its own — a reply quote dropped, the command
  // popup closed). Never from a popup inside the card (a picker, a menu):
  // that one's Escape is its own.
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape" || e.defaultPrevented || heldRef.current) return;
    const t = e.target as HTMLElement;
    const pop = t.closest("[role='menu'], [role='listbox'], [role='dialog'], [role='alertdialog']");
    if (pop && pop !== cardRef.current) return;
    e.preventDefault();
    onDismiss();
  };

  const name = view ? view.name || view.id : "";
  const subject = useMemo(
    (): FaceSubject | null =>
      view ? { id: view.id, name: view.name || view.id, face: (view.face ?? null) as FaceSubject["face"], status: view.status as FaceSubject["status"] } : null,
    [view],
  );
  const words = view ? botStatusWords(view.status) : "";
  // THE BRIDGE over the gap to the rail (RailPeek).
  // None while FLOATING.
  const bridge =
    !move.floating && box && railLeft.current > box.left + box.width + 1
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
        id={id}
        className={"claude-rail-peek claude-rail-bot-peek favs-on" + (visible ? " is-on" : "") + (resizing ? " is-resizing" : "") + move.className}
        role="dialog"
        aria-label={view ? `${name} — chat` : "Bot chat"}
        style={box ? { left: box.left, top: box.top, width: box.width, height: box.height } : undefined}
        onPointerEnter={onPointerEnter}
        onPointerLeave={onPointerLeave}
        onPointerDownCapture={onPressInsideCapture}
        onPointerDown={move.onPointerDown}
        onDoubleClick={move.onDoubleClick}
        onFocusCapture={onFocusCapture}
        onBlurCapture={onBlurCapture}
        onKeyDown={onKeyDown}
      >
        {gripsOn &&
          // …and, FLOATING, the right edge and corners too (rail-move-lib).
          (move.floating ? FLOAT_GRIPS : ALL_GRIPS).map((corner) => (
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
        {gripsOn &&
          (move.floating ? FLOAT_GRIPS : ALL_GRIPS).map((e) => <div key={"ring-" + e} className={"rail-grip-ring is-" + e} aria-hidden="true" />)}
        <div className="claude-rail-peek-card">
          {view && subject && (
            <div className="claude-rail-peek-head">
              <div className="claude-rail-peek-row">
                {/* With the pin: no × (a click outside closes a temp card). */}
                {!pinCtl && (
                  <button
                    type="button"
                    className="claude-rail-peek-btn claude-rail-peek-close"
                    aria-label="Close the preview"
                    title="Close the preview"
                    onClick={() => {
                      onHeld(false);
                      onDismiss();
                    }}
                  >
                    <X size={14} aria-hidden="true" />
                  </button>
                )}
                {pinCtl && <RailPinButton variant="legacy" pinned={pinCtl.pinned} onToggle={pinCtl.onToggle} />}
                <span className="claude-rail-peek-botface" aria-hidden="true">
                  <Face b={subject} />
                </span>
                <span className="claude-rail-peek-botname" title={name}>
                  {name}
                </span>
                <span className="claude-rail-peek-gap" aria-hidden="true" />
                <span className={"claude-rail-peek-words" + (view.status === "waiting" ? " is-waiting" : "")}>{words}</span>
                <button
                  type="button"
                  className="btn claude-rail-peek-open"
                  title="Open this bot's page"
                  onClick={() => {
                    onOpen(botHref(view.id));
                    onHeld(false);
                    onDismiss("door");
                  }}
                >
                  Open
                </button>
              </div>
            </div>
          )}
          <div className="claude-rail-peek-body">
            {chatKey && (
              <div key={chatKey} className="claude-rail-peek-chat is-shown">
                {/* A chunk that fails to load (an app updated under this window) fails HERE — the rail card's own
                    boundary, with its reload card — not at the shell's, which would replace the whole page; the
                    head and its Open door stand above it (CodeRabbit on #2143). */}
                <ChatChunkBoundary>
                  <Suspense fallback={<p className="claude-rail-peek-note">Loading…</p>}>
                    <BotChat id={chatKey} markRead={pinned} sharedScroll={!isBotsPath(location.pathname)} />
                  </Suspense>
                </ChatChunkBoundary>
              </div>
            )}
          </div>
        </div>
      </div>
    </>,
    document.body,
  );
});
