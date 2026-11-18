// THE FAVOURITE'S CARD (owner, 2026-11-03): the open favourite's chat, as a
// card of its own beside the rail's foot — NOT the rail's hover/click card
// (RailPeek inside ClaudeRail), which goes on previewing the list's rings
// above it while this one stands. "The favourite window will stay open
// regardless of other tasks' hover or click to open a preview in the page:
// not the same preview — two different previews, two different sections."
//
// MOUNTED ONCE, IN THE SHELL, above every page ("when I change views why
// doesn't it stay as is — it flickers and reloads every time"): a page's
// rail mounts and unmounts with the page, and a card inside it would
// remount its chat on every hop. This one is the shell's: the chat mounts
// once and stays; the card stands beside whatever rail is on the page now
// (fav-peek-store, where each rail registers itself and its favourite
// rings), and while no rail is up — a page with no rail, a rail hidden for
// having nothing to draw (ClaudeRail `shown`) — it hides, the chat kept
// alive, and shows again on the next rail. The rail stands beside the open
// pane too (owner, 2026-11-05), so the card keeps its corner pane open or
// shut — and a page with no rail of its own draws the shell's fallback
// (FallbackRail: the favourites alone), so with a favourite open there is
// always a rail to stand beside. Never taller than half the window
// (RailPeek, rail-peek-lib `favCardWish`). A bit
// smaller than the list's card (`FAV_SIZE`), and STUCK TO THE BOTTOM RIGHT
// (owner: "it should stick to bottom so we can have it at bottom right all
// the time and we can close and open it"): its bottom on the rail's, not on
// its ring — the one place it is always found. Over the page's own header
// bars (z-index, claude-rail.css), so its × is always reachable.
//
// WHICH favourite is THIS WINDOW'S (owner, 2026-11-04: "if I have multiple
// windows open, the open favourite shouldn't sync; the list items (stars) do
// sync"): platform/lib/last-chat `useFavOpen`, sessionStorage — a favourite
// ring's press in this window opens it (ClaudeRail `openFavourite`), a star
// pressed on in this window's rail card opens it (`railOnStarred`), the ×
// here closes it. Nothing is written to the server for any of them: the
// server's `starred_open` is kept for older clients and followed by no card.
// Local state is instant — no pulse to wait for, no other window to lose to.
// The STARS stay the server's: an unstar, an erase or an archive of the open
// chat, in any window, closes it here once the pulse says so (rail-peek-lib
// `favOpenVerdict`); an archived favourite never shows a card. Nothing else
// closes it — not an outside press, not Escape, not a place change — BUT
// ITS OPEN DOOR (owner, 2026-11-06: "if I have a fav window and I click
// Open, the sidebar should expand and show me the chat, and close the fav
// window; if the sidebar is expanded it should open the chat, whatever
// chat it is in; and when I close that sidebar it should open the fav
// popup again"): Open goes through the page rail's own door (fav-peek-store
// `railDoor` — the page's pane door, which expands the pane and puts the
// chat in it, in place of whatever it showed), closes this card, and leaves
// the key for the pane's collapse to give back (`setFavRestore`; ClaudeRail
// opens it again on the turn from open to shut). A rail with no room for
// it (a window shrunk to nothing) hides it until the window, the rail or the
// row it stands in changes shape, or the next rail stands.
//
// ONE WINDOW B WITH THE BOTS (owner, 2026-11-06: "treat fav bots and fav
// tasks as one window"): a pinned bot's card (BotPeek) stands exactly here,
// at this card's size, and the two never stand together — while a bot's
// card holds the window (fav-peek-store `useBotCard`) this one shows no
// hover, and a bot's press closes this window's open favourite (ClaudeRail).
//
// A HOVER MODE (owner, 2026-11-05: two sections, two windows): with NO
// favourite open, resting on a favourite's ring previews it HERE — bottom
// right, this card's own size — never in the rail's card. The hover key is
// local and not persisted (fav-peek-store `favHoverIntent`, the list's own
// timings: dwell, grace, cone); leaving the ring and the card closes it; a
// press on the ring or inside the card converts it to the open key and it
// stays. With one open, a hover on another favourite's ring is its caption.
//
// AN UNSTAR HERE shuts the card at once (RailPeek `dismissOnUnstar`).
//
// THE KEYBOARD IS NEVER LEFT ON THE BODY: a card that shuts or hides with
// the focus inside it hands it to the rail — the chat's own ring, else the
// rail itself, never another chat's ring, and quietly: a hand-off opens no
// hover card (fav-peek-store `focusRail`). And a
// keyboard press on a favourite's ring lands on this card's × once it
// stands (fav-peek-store `pendingFocus`), however long it took to draw.
//
// ONE CHAT, ONE COMPOSER: while another composer on the page is MOUNTED on
// this favourite's session — a pane chat open on it (the Open door taken),
// or the Tasks side peek showing it; each says so itself
// (platform/lib/peek-session) — that one holds the chat: the card unmounts
// its own and says where it is, the window's open key kept, and mounts it
// again when that one goes. A URL still naming the session with the pane
// shut is not a composer: the card keeps its chat (owner, 2026-11-04).
//
// ITS WHERE-CHIP is the same door as the rail card's (RailPeek, claude-rail-
// lib `chipDoor`): the chat's own canvas, node, folder or file — and on the
// canvas page, a node of THAT canvas is selected in place, the page's canvas
// read off the rail this card stands beside (fav-peek-store `railHere`).
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { cardTitleLine } from "@shell/tasks-lib";
import { type Task, type TaskPulseTask } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { useUrlVersion } from "@platform/lib/hooks";
import { opensOnThisPage } from "@shell/useAppPageClaudePane";
import { readListing, refreshListing, subscribeListing, useTasksPulseRows } from "@shell/tasksPulse";
import {
  clearFavOpen,
  favOpen,
  favOpenedAtMs,
  favPinned,
  lastChatId,
  setFavOpen,
  setFavPinned,
  setLastFav,
  useFavOpen,
  useFavPinned,
} from "@platform/lib/last-chat";
import { useTaskHeadUnified } from "@shell/task-head-flag";
import { setBotOpen, useAPinned } from "@platform/lib/last-chat";
import { aPinTask } from "./rail-slots-lib";
import { useRailFreePins } from "./rail-free-pins-flag";
import { freezeFromDock } from "./rail-move-store";
import { otherSlot, registerPinFocus, setFreeSlot, setPinTitle, useFreeSlot, type Slot } from "./rail-slots-store";
import { PIN_B_ID, usePinHasBox } from "./use-pin-box";
import { RailPeek, type RailPeekHandle } from "./RailPeek";
import {
  dismissFavHover,
  favHoverIntent,
  favPress,
  focusRail,
  publishFavBox,
  publishFavBoxFor,
  railDoor,
  railHere,
  setFavRestore,
  takeFavFocus,
  useBotCard,
  useFavHoverKey,
  useFavOpenRow,
  useFavPeekState,
  withdrawFavFocusOn,
} from "./fav-peek-store";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { useTaskFavourites } from "@platform/lib/favourites-flag";
import { ownPlace, type RailPlace, type RailRow } from "./claude-rail-lib";
import { hopDropsOpen, reloadDropsOpen, viewPress } from "./rail-pin-lib";
import {
  FAV_HEIGHT,
  FAV_WIDTH,
  favHoverRow,
  rowOf,
  type PeekWish,
  favLike,
} from "./rail-peek-lib";

export const FAV_PEEK_ID = "claude-rail-fav-peek";

/** When this document loaded: an open favourite whose card was opened before
 *  it is one a RELOAD found in the window's storage (the reload rule below). */
const LOADED_AT = Date.now();
/** The reload rule runs once per document. */
let reloadChecked = false;
export function resetFavReloadCheckForTests(): void {
  reloadChecked = false;
}

/** A bit under the list card's 420×640 (owner: "by default the size of
 *  that preview could be a bit smaller"); the bottom on the rail's. The
 *  reader's drag from its left, top and bottom edges and its left corners
 *  replaces the width and height (RailPeek `resizeKey`, ui-layout
 *  `canvasRailFav`); a bottom drag keeps the top, and on release the card
 *  stands on the rail's bottom again at its new height. */
const FAV_SIZE: PeekWish = { width: FAV_WIDTH, maxHeight: FAV_HEIGHT, align: "bottom" };

/** The place the card says the chat is on: the row's own canvas, else its
 *  project folder (claude-rail-lib `ownPlace`) — this card belongs to no
 *  page. The listing's canvas stands in while the pulse row lacks one. */
function placeOf(t: TaskPulseTask, row: Task | null): RailPlace {
  return ownPlace({ ...t, canvas: t.canvas ?? row?.canvas ?? null });
}

const noop = () => {};
/** How long a press's focus waits for the favourite chat's composer to mount
 *  before settling for the × — WALL-CLOCK, as ClaudeRail's FOCUS_WAIT_MS: a
 *  cold chat chunk loads in time, not frames, and a frame count is a shorter
 *  wait on a faster display (Bugbot on #2170). */
const FAV_COMPOSER_WAIT_MS = 2500;

/** The second slot of the favourite's window (rail-slots-store "THE TWO SLOTS
 *  OF A CHAT WINDOW"): mounted only with free pins on — off, the one card
 *  below is all there is, as it always was. */
export function FavouritePeekSlotY() {
  return useRailFreePins() ? <FavouritePeek slot="y" /> : null;
}

export function FavouritePeek({ slot = "x" }: { slot?: Slot } = {}) {
  const peekOn = useCanvasRailPeek();
  const favsOn = useTaskFavourites();
  const on = peekOn && favsOn;
  const rows = useTasksPulseRows(on);
  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  useEffect(() => {
    // Off, no feed: this stands in every shell, and the listing's long-poll
    // is not a cost a reader who never starred anything should pay.
    if (!on) return;
    setListing(readListing());
    return subscribeListing((ev) => {
      if (!ev.failed) setListing(ev.rows);
    });
  }, [on]);
  const { rail, page, pendingFocus } = useFavPeekState();
  // HOVER / TEMP / PIN (rail-pin-lib; the unified-head switch on): the open
  // card is TEMP until its pin (or a double-click on its ring) pins it — this
  // window's pin mark (last-chat `favPinned`) naming the open key.
  const pinModel = useTaskHeadUnified();
  const openKey = useFavOpen();
  const pinnedKey = useFavPinned();
  // FREE PINS (rail-slots-lib): the favourite's window is two slots — its
  // non-pin and its pin (below) — so the pin's key and the open key are two
  // different chats.
  const freePins = useRailFreePins();
  // ONE OF TWO SLOTS, BY ROLE (rail-slots-store): the FREE slot is this card
  // as it always was — the open key, the hover, every rule below; the other
  // shows the PINNED favourite (`favPinned`) and nothing else, and none of
  // the non-pin's rules run in it. The pin's press swaps the two roles, so
  // the card the reader pinned is this very card, never remounted.
  const freeNow = useFreeSlot("fav");
  const isFree = !freePins || slot === freeNow;
  const pinRole = !isFree;
  const cardId = pinRole ? PIN_B_ID : FAV_PEEK_ID;
  // A pin mark on a key no longer open (closed, or another favourite took
  // the window: a swap is temp, the pin is per card) goes.
  useEffect(() => {
    if (pinModel && !freePins && pinnedKey !== null && pinnedKey !== openKey) setFavPinned(null);
  }, [pinModel, freePins, pinnedKey, openKey]);
  // …and never one chat in both (a hand-over or a restore that opened a
  // pinned key): the pinned window has it — this section's, or section A's
  // (a starred chat pinned off the list).
  const aPinKey = aPinTask(useAPinned());
  useEffect(() => {
    if (freePins && isFree && openKey !== null && (openKey === pinnedKey || openKey === aPinKey)) clearFavOpen(openKey);
  }, [freePins, isFree, openKey, pinnedKey, aPinKey]);
  // A RELOAD BRINGS BACK ONLY A PINNED CARD (design.md row 19): an open key
  // the storage kept from before this document loaded, with no pin on it, is
  // closed — once, as the switch's answer lands.
  useEffect(() => {
    if (!pinModel || reloadChecked) return;
    reloadChecked = true;
    const key = favOpen();
    if (key && reloadDropsOpen({ openKey: key, pinnedKey: favPinned(), openedAt: favOpenedAtMs(key), loadedAt: LOADED_AT })) {
      clearFavOpen(key);
    }
  }, [pinModel]);
  // No room beside this rail (RailPeek `onNoRoom`): hidden until the window
  // or the rail changes shape, or another rail stands.
  const [noRoom, setNoRoom] = useState(false);
  useEffect(() => {
    setNoRoom(false);
    const re = () => setNoRoom(false);
    window.addEventListener("resize", re);
    // The rail's own box (a pane shutting: the rail grows back into room)
    // and the ROW it stands in, the box RailPeek measures (`rowOf`): a
    // sidebar folding widens the row while the 48px rail and the window
    // stay the same size.
    const ro = rail && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (rail && ro) {
      ro.observe(rail);
      const row = rowOf(rail);
      if (row) ro.observe(row);
    }
    return () => {
      window.removeEventListener("resize", re);
      ro?.disconnect();
    };
  }, [rail]);
  // THIS WINDOW'S OPEN FAVOURITE (header), checked against the pulse's
  // stars — closed (the key cleared) once an unstar, an erase or an archive
  // reaches the pulse (fav-peek-store `useFavOpenRow`).
  const openRow = useFavOpenRow(on && isFree, rows);
  const open = isFree ? openRow : null;
  // THE PINNED FAVOURITE (the pin role): its row off the pulse; gone once the
  // pulse — the rows this render holds — no longer has it starred (unstarred
  // elsewhere, archived, erased), and the pin goes with it.
  const pinRow = useMemo(() => (pinRole && pinnedKey ? (rows.find((t) => t.key === pinnedKey) ?? null) : null), [pinRole, pinnedKey, rows]);
  const pinGone = pinRole && pinnedKey !== null && rows.length > 0 && (!pinRow || pinRow.status === "archived" || !favLike(pinRow));
  useEffect(() => {
    if (pinGone && favPinned() === pinnedKey) setFavPinned(null);
  }, [pinGone, pinnedKey]);
  // …or, with none open, the one a hover wants (header): a live favourite
  // only — and not while a bot's card holds the window (header).
  const hoverKey = useFavHoverKey();
  const botUp = useBotCard() !== null;
  const hoverRow = useMemo(
    () => (on && isFree && !open && !botUp ? favHoverRow(hoverKey, null, rows) : null),
    [on, isFree, open, botUp, hoverKey, rows],
  );
  const hovering = isFree && !open && !!hoverRow;
  const shownRow = pinRole ? (pinGone ? null : pinRow) : (open ?? hoverRow);
  const seat = useMemo((): RailRow | null => {
    if (!shownRow) return null;
    return { pulse: shownRow, row: listing?.find((t) => t.key === shownRow.key) ?? null };
  }, [shownRow, listing]);
  // A hover never outlives its cause: the open key taking over (a press),
  // the feature going off, or another rail standing.
  useEffect(() => {
    if (open || !on) dismissFavHover();
  }, [open, on]);
  // A card that opens is the window moving on: a restore left by an earlier
  // door is forgotten (header). The door itself sets it AFTER closing.
  useEffect(() => {
    if (open) setFavRestore(null);
  }, [open]);
  useEffect(() => {
    dismissFavHover();
  }, [rail]);
  // A hover on a key the pulse no longer holds (unstarred, gone): forgotten.
  const hoverLive = useMemo(() => new Set(rows.filter((r) => favLike(r)).map((r) => r.key)), [rows]);
  useEffect(() => {
    if (on) favHoverIntent().prune(hoverLive);
  }, [on, hoverLive]);
  // THE CARD STANDS WHATEVER THE PANE SHOWS (owner, 2026-11-05: "we can
  // have the same task open as the expanded view and as a window B pin
  // view"): a favourite's chat in the pane is no reason for its card not
  // to mount it too — the rule of 2026-11-05 morning (a caption and a door
  // instead) is gone.
  // A pinned window floats: it needs no rail to stand beside, only a box
  // (use-pin-box).
  const placed = usePinHasBox("b", pinRole && on && !!seat, rail);
  const shown = on && seat && (pinRole ? placed : rail && !noRoom) ? seat : null;
  const kept = useMemo(() => (seat ? [seat] : []), [seat]);

  // FOCUS NEVER FALLS TO THE BODY (header). Whether the keyboard was last
  // inside the card is followed document-wide: a card that unmounts takes
  // its focus with it and no event says where it was.
  const focusIn = useRef(false);
  useEffect(() => {
    const inCard = (n: EventTarget | null) => !!n && !!document.getElementById(cardId)?.contains(n as Node);
    const onIn = (e: FocusEvent) => {
      focusIn.current = inCard(e.target);
      // A keyboard press's pending wish for the × (fav-peek-store) is
      // withdrawn once the reader moves to another control meanwhile.
      withdrawFavFocusOn(e.target, document.getElementById(cardId));
    };
    // A press on the card's (or the page's) bare background sends the
    // keyboard to the body with no focusin: a focusout leaving for nowhere,
    // or for outside the card, clears it too.
    const onOut = (e: FocusEvent) => {
      if (!inCard(e.relatedTarget)) focusIn.current = false;
    };
    document.addEventListener("focusin", onIn, true);
    document.addEventListener("focusout", onOut, true);
    return () => {
      document.removeEventListener("focusin", onIn, true);
      document.removeEventListener("focusout", onOut, true);
    };
  }, [cardId]);
  const shownKey = shown?.pulse.key ?? null;
  const lastShown = useRef<string | null>(null);
  // Shut by an unstar: its foot ring goes with the next pulse, so the
  // keyboard goes to its ring back in the list, else the rail itself.
  const unstarred = useRef<string | null>(null);
  useLayoutEffect(() => {
    const was = lastShown.current;
    lastShown.current = shownKey;
    if (!was || was === shownKey || !focusIn.current) return;
    // The keyboard must still be in the card — or, with the card gone from
    // the page (a removed node takes its focus with no focusout), on the
    // body it fell to.
    const a = document.activeElement;
    const card = document.getElementById(cardId);
    const held = card ? !!a && card.contains(a) : !a || a === document.body;
    if (!held) return;
    focusIn.current = false;
    const gone = unstarred.current === was;
    unstarred.current = null;
    focusRail(rail, was, gone ? "list" : "foot", gone);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey, rail]);
  // …and a keyboard press on a favourite's ring lands on the ×, once the
  // card stands on that key and is drawn (`.is-on` lands a frame after the
  // show; a hidden or inert box takes no focus).
  // Taking the wish clears it in the store, which re-runs this effect: the
  // wait is a ref, not this run's cleanup, so that does not cancel it.
  const focusWait = useRef(0);
  // The running wait's stop — its frame AND its document listeners — so a
  // new wait supersedes it and an unmount ends it (CodeRabbit on #2170).
  const focusStop = useRef<(() => void) | null>(null);
  useEffect(
    () => () => {
      cancelAnimationFrame(focusWait.current);
      focusStop.current?.();
    },
    [],
  );
  // THE COMPOSER FIRST (Akshil, 2026-11-07: the list card's press lands in
  // its composer, so the favourite's does too): retried by frame while the
  // chat mounts, for FAV_COMPOSER_WAIT_MS at most; the × only when no composer
  // came (a slow chat). Over the moment the reader presses or types anything
  // themselves, or the keyboard goes somewhere outside the card.
  useEffect(() => {
    // The pending wish is a ring's press on the NON-PIN's key.
    if (!isFree || !shownKey || pendingFocus !== shownKey) return;
    if (!takeFavFocus(shownKey)) return;
    cancelAnimationFrame(focusWait.current);
    focusStop.current?.();
    const until = performance.now() + FAV_COMPOSER_WAIT_MS;
    let live = true;
    const stop = () => {
      live = false;
      cancelAnimationFrame(focusWait.current);
      document.removeEventListener("pointerdown", stop, true);
      document.removeEventListener("keydown", stop, true);
      if (focusStop.current === stop) focusStop.current = null;
    };
    focusStop.current = stop;
    document.addEventListener("pointerdown", stop, true);
    document.addEventListener("keydown", stop, true);
    const take = (el: HTMLElement | null) => {
      if (!el || el.closest("[inert]")) return false;
      el.focus({ preventScroll: true });
      return document.activeElement === el;
    };
    const attempt = () => {
      if (!live) return;
      const card = document.querySelector<HTMLElement>(`#${cardId}.is-on`);
      const a = document.activeElement;
      if (a && a !== document.body && card && !card.contains(a) && !a.closest(".claude-rail")) return stop();
      const box = card?.querySelector<HTMLElement>(".claude-rail-peek-chat.is-shown form.c-composer textarea:not([disabled])") ?? null;
      if (take(box)) return stop();
      if (performance.now() < until) {
        focusWait.current = requestAnimationFrame(attempt);
        return;
      }
      take(card?.querySelector<HTMLElement>(".claude-rail-peek-close") ?? null);
      stop();
    };
    focusWait.current = requestAnimationFrame(attempt);
    // No cleanup: taking the wish clears it in the store, which re-runs this
    // effect — the wait must outlive that run (it ends itself, above).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey, pendingFocus, isFree]);
  // The rail itself is the anchor, hover or open: with `align: "bottom"` the
  // card's bottom lands on the rail's, clamped inside the row — the bottom
  // right, always, so a press never moves it (`favCardWish` for both).
  const anchorOf = useCallback(() => rail, [rail]);
  // A hover's cone starts at its own ring.
  const coneOf = useCallback(
    (key: string) => rail?.querySelector<HTMLElement>(`.claude-rail-favs [data-task-key="${CSS.escape(key)}"]`) ?? rail,
    [rail],
  );
  // The page's canvas, off the rail this card stands beside (header).
  const here = useCallback(() => railHere(rail), [rail]);
  const place = useMemo(() => (seat ? placeOf(seat.pulse, seat.row) : null), [seat]);
  // The × (RailPeek `onDismiss`) and an unstar here: this window's card
  // closes, nothing is written (header). The Open door closes it too — but
  // from `openChat` below, once the hop went: RailPeek's `onDismiss("door")`
  // is a no-op here.
  const close = useCallback(
    (why?: "door" | "unstar") => {
      // THE PINNED WINDOW answers only to its unpin and its Open door
      // (`openChat`); an unstar here lets the pin go with the star.
      if (pinRole) {
        if (why === "unstar" && pinnedKey && favPinned() === pinnedKey) setFavPinned(null);
        return;
      }
      if (hovering) {
        dismissFavHover();
        return;
      }
      if (!open || why === "door") return;
      if (why === "unstar") unstarred.current = open.key;
      clearFavOpen(open.key);
    },
    [open, hovering, pinRole, pinnedKey],
  );
  // THE OPEN DOOR (header): the favourites' own last mark, then the page
  // rail's door — the pane expands on the chat, whatever it showed — or,
  // with no rail door (the shell's fallback), the href itself. THE CARD
  // CLOSES ONCE THE HOP WENT (Bugbot on #2154): a door behind the
  // composer's leave guard answers false when the reader cancelled the
  // prompt, and the card then stays as it was — closing on the click would
  // have lost the window to a cancelled hop, its restore unconfirmed and
  // expiring. A door that answers nothing (void) is taken as gone.
  const openChat = useCallback(
    (href: string) => {
      if (seat) setLastFav(lastChatId(seat.pulse));
      const door = railDoor(rail);
      const went = door ? door(href) : navigateUrl(href);
      // The pinned window's door is a hand-off: the pin goes once the hop went.
      if (pinRole) {
        const pinned = pinnedKey;
        void Promise.resolve(went).then((ok) => {
          if (ok !== false && pinned && favPinned() === pinned) setFavPinned(null);
        });
        return went;
      }
      const key = open?.key ?? null;
      if (key) {
        void Promise.resolve(went).then((ok) => {
          if (ok === false) return;
          clearFavOpen(key);
          setFavRestore(key);
        });
      }
      return went;
    },
    [seat, rail, open, pinRole, pinnedKey],
  );
  // A press inside the hover card keeps it: the hover becomes the open key.
  const pressInside = useCallback(() => {
    // The rail's outside-press watcher for a temp card (rail-pin-lib).
    favPress.inside = true;
    viewPress.inside = true;
    if (pinRole || !hovering || !hoverRow) return;
    setFavOpen(hoverRow.key);
    dismissFavHover();
  }, [hovering, hoverRow, pinRole]);
  // THE PIN ICON (rail-pin-lib `pinToggle`): a hover's card opens pinned, an
  // open one pins or goes back to temp. Read off the window's marks at the
  // press — the press inside has just turned a hover into the open key.
  const togglePin = useCallback(() => {
    // THE UNPIN (free pins; rail-slots-lib "UNPINNING PUTS THE WINDOW BACK BY
    // THE RAIL"): this card becomes the free slot again, open on the same
    // chat — it docks itself, and the non-pin that stood there gives way.
    if (pinRole) {
      if (!pinnedKey) return;
      setFreeSlot("fav", slot);
      setFavOpen(pinnedKey);
      setFavPinned(null);
      return;
    }
    const key = favOpen() ?? hoverRow?.key ?? null;
    if (!key) return;
    // FREE PINS: the pin's press freezes the window where it stands, and the
    // two slots SWAP ROLES — this card, as it is, becomes window B's pinned
    // window (replacing a favourite or a bot pinned there), and the other
    // slot is the non-pin from here on.
    if (freePins) {
      freezeFromDock("b");
      dismissFavHover();
      setBotOpen(null);
      setFavPinned(key);
      setFreeSlot("fav", otherSlot(slot));
      clearFavOpen(key);
      return;
    }
    if (favPinned() === key) {
      setFavPinned(null);
      return;
    }
    if (favOpen() !== key) setFavOpen(key);
    dismissFavHover();
    setFavPinned(key);
  }, [hoverRow, freePins, pinRole, pinnedKey, slot]);
  // A PAGE HOP KEEPS ONLY A PINNED CARD (rail-pin-lib `hopKeeps`): the open
  // favourite that was already open before the URL's path changed, and is not
  // pinned, closes. A key opened BY the hop (the where-chip's hand-over, its
  // open landing with the push) is new, and stays.
  const path = typeof location === "undefined" ? "" : location.pathname;
  const hopSeen = useRef({ path, key: openKey });
  useEffect(() => {
    const was = hopSeen.current;
    hopSeen.current = { path, key: openKey };
    if (!pinModel || !isFree) return;
    if (openKey && hopDropsOpen({ was, now: { path, key: openKey }, pinnedKey: favPinned() })) clearFavOpen(openKey);
  }, [path, openKey, pinModel, isFree]);
  // Only an OPEN card makes the rail's own card give way (`avoid`); a hover
  // overlaps it and publishes nothing.
  // …and the pinned window none at all: it floats, and its silence must not
  // wipe the box the non-pin's slot says.
  const onBox = useCallback(
    (b: Parameters<typeof publishFavBox>[0]) => {
      if (!pinRole) publishFavBoxFor(hovering, b, "fav");
    },
    [hovering, pinRole],
  );
  // Open's ↗ only when the door leaves the page on screen (RailPeek
  // `openIcon`): read against the URL now — a hop re-renders this card.
  useUrlVersion();
  const openIcon = (href: string) => !opensOnThisPage(href, location.pathname);
  useEffect(
    () => () => {
      if (slot === "x") publishFavBox(null, "fav");
    },
    [slot],
  );
  // The keyboard's way into the pinned window (a ring's click on the pinned
  // favourite), and the name the non-pin's pin icon says it would replace.
  const peekRef = useRef<RailPeekHandle | null>(null);
  const pinUp = pinRole && !!shown;
  useEffect(() => {
    if (!pinUp) return;
    return registerPinFocus("b", () => !!peekRef.current?.focusComposer() || !!peekRef.current?.focusChat());
  }, [pinUp]);
  const pinTitleText = pinUp && seat ? cardTitleLine({ ...seat.pulse, ...(seat.row ?? {}) } as Task).text || seat.pulse.title || "Chat" : null;
  useEffect(() => {
    if (!pinRole) return;
    setPinTitle("b", "fav-" + slot, pinTitleText);
    return () => setPinTitle("b", "fav-" + slot, null);
  }, [pinRole, slot, pinTitleText]);
  if (!on || !seat || !place || (pinRole && !placed)) return null;
  return (
    <RailPeek
      ref={peekRef}
      id={cardId}
      win="b"
      shown={shown}
      kept={kept}
      anchorOf={anchorOf}
      coneOf={coneOf}
      railEl={rail}
      place={place}
      size={FAV_SIZE}
      resizeKey="canvasRailFav"
      onBox={onBox}
      onReload={refreshListing}
      onPointerEnter={hovering ? () => favHoverIntent().enterCard() : noop}
      onPointerLeave={hovering ? () => favHoverIntent().leaveCard() : noop}
      onCone={hovering ? (inside) => (inside ? favHoverIntent().enterCone() : favHoverIntent().leaveCone()) : noop}
      onPressInside={pressInside}
      onHold={hovering ? (held) => favHoverIntent().hold(held) : noop}
      onDismiss={close}
      dismissOnUnstar
      openIcon={openIcon}
      onNoRoom={() => setNoRoom(true)}
      onOpenChat={openChat}
      here={here}
      pageRoot={page?.root ?? null}
      onPin={noop}
      held={!hovering}
      onHeld={noop}
      pinned={false}
      pinCtl={
        pinModel ? { pinned: pinRole || (!freePins && !hovering && !!open && pinnedKey === open.key), onToggle: togglePin } : null
      }
    />
  );
}
