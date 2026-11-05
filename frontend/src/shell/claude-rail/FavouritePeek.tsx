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
// closes it — not an outside press, not Escape, not a place change. The Open
// door opens the pane; the card stays this window's open favourite (RailPeek
// `onDismiss("door")`) and is back when the rail is. A rail with no room for
// it (a window shrunk to nothing) hides it until the window, the rail or the
// row it stands in changes shape, or the next rail stands.
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
import { type Task, type TaskPulseTask } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { readListing, refreshListing, subscribeListing, useTasksPulseRows } from "@shell/tasksPulse";
import { clearFavOpen, setFavOpen } from "@platform/lib/last-chat";
import { RailPeek } from "./RailPeek";
import {
  dismissFavHover,
  favHoverIntent,
  focusRail,
  publishFavBox,
  publishFavBoxFor,
  railHere,
  takeFavFocus,
  useFavHoverKey,
  useFavOpenRow,
  useFavPeekState,
  withdrawFavFocusOn,
} from "./fav-peek-store";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { useTaskFavourites } from "@platform/lib/favourites-flag";
import { ownPlace, type RailPlace, type RailRow } from "./claude-rail-lib";
import {
  FAV_HEIGHT,
  FAV_WIDTH,
  favChatElsewhere,
  favHoverRow,
  rowOf,
  type PeekWish,
} from "./rail-peek-lib";
import { usePaneSessions, usePeekSession } from "@platform/lib/peek-session";

export const FAV_PEEK_ID = "claude-rail-fav-peek";

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

export function FavouritePeek() {
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
  const open = useFavOpenRow(on, rows);
  // …or, with none open, the one a hover wants (header): a live favourite only.
  const hoverKey = useFavHoverKey();
  const hoverRow = useMemo(() => (on && !open ? favHoverRow(hoverKey, null, rows) : null), [on, open, hoverKey, rows]);
  const hovering = !open && !!hoverRow;
  const shownRow = open ?? hoverRow;
  const seat = useMemo((): RailRow | null => {
    if (!shownRow) return null;
    return { pulse: shownRow, row: listing?.find((t) => t.key === shownRow.key) ?? null };
  }, [shownRow, listing]);
  // A hover never outlives its cause: the open key taking over (a press),
  // the feature going off, or another rail standing.
  useEffect(() => {
    if (open || !on) dismissFavHover();
  }, [open, on]);
  useEffect(() => {
    dismissFavHover();
  }, [rail]);
  // A hover on a key the pulse no longer holds (unstarred, gone): forgotten.
  const hoverLive = useMemo(() => new Set(rows.filter((r) => r.starred).map((r) => r.key)), [rows]);
  useEffect(() => {
    if (on) favHoverIntent().prune(hoverLive);
  }, [on, hoverLive]);
  const shown = on && seat && rail && !noRoom ? seat : null;
  // A mounted pane chat on this very session (header), or the Tasks side
  // peek showing it: no chat here. Never the URL alone — a shut pane leaves
  // `session_id` in it and shows nothing.
  const paneSids = usePaneSessions();
  const peekSid = usePeekSession();
  const elsewhere = favChatElsewhere(seat?.pulse ?? null, paneSids, peekSid);
  const kept = useMemo(() => (seat && !elsewhere ? [seat] : []), [seat, elsewhere]);

  // FOCUS NEVER FALLS TO THE BODY (header). Whether the keyboard was last
  // inside the card is followed document-wide: a card that unmounts takes
  // its focus with it and no event says where it was.
  const focusIn = useRef(false);
  useEffect(() => {
    const inCard = (n: EventTarget | null) => !!n && !!document.getElementById(FAV_PEEK_ID)?.contains(n as Node);
    const onIn = (e: FocusEvent) => {
      focusIn.current = inCard(e.target);
      // A keyboard press's pending wish for the × (fav-peek-store) is
      // withdrawn once the reader moves to another control meanwhile.
      withdrawFavFocusOn(e.target, document.getElementById(FAV_PEEK_ID));
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
  }, []);
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
    const card = document.getElementById(FAV_PEEK_ID);
    const held = card ? !!a && card.contains(a) : !a || a === document.body;
    if (!held) return;
    focusIn.current = false;
    const gone = unstarred.current === was;
    unstarred.current = null;
    focusRail(rail, was, gone ? "list" : "foot", gone);
  }, [shownKey, rail]);
  // …and a keyboard press on a favourite's ring lands on the ×, once the
  // card stands on that key and is drawn (`.is-on` lands a frame after the
  // show; a hidden or inert box takes no focus).
  // Taking the wish clears it in the store, which re-runs this effect: the
  // wait is a ref, not this run's cleanup, so that does not cancel it.
  const focusWait = useRef(0);
  useEffect(() => () => cancelAnimationFrame(focusWait.current), []);
  useEffect(() => {
    if (!shownKey || pendingFocus !== shownKey) return;
    if (!takeFavFocus(shownKey)) return;
    cancelAnimationFrame(focusWait.current);
    let tries = 0;
    const attempt = () => {
      const btn = document.querySelector<HTMLElement>(`#${FAV_PEEK_ID}.is-on .claude-rail-peek-close`);
      if (btn && !btn.closest("[inert]")) {
        btn.focus({ preventScroll: true });
        if (document.activeElement === btn) return;
      }
      if (++tries < 30) focusWait.current = requestAnimationFrame(attempt);
    };
    focusWait.current = requestAnimationFrame(attempt);
  }, [shownKey, pendingFocus]);
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
  // The × (RailPeek `onDismiss`), and an unstar here: this window's card
  // closes, nothing is written (header).
  const close = useCallback(
    (why?: "door" | "unstar") => {
      // The Open door is not a close: the pane takes over, the card stays open.
      if (hovering) {
        dismissFavHover();
        return;
      }
      if (why === "door" || !open) return;
      if (why === "unstar") unstarred.current = open.key;
      clearFavOpen(open.key);
    },
    [open, hovering],
  );
  // A press inside the hover card keeps it: the hover becomes the open key.
  const pressInside = useCallback(() => {
    if (!hovering || !hoverRow) return;
    setFavOpen(hoverRow.key);
    dismissFavHover();
  }, [hovering, hoverRow]);
  // Only an OPEN card makes the rail's own card give way (`avoid`); a hover
  // overlaps it and publishes nothing.
  const onBox = useCallback((b: Parameters<typeof publishFavBox>[0]) => publishFavBoxFor(hovering, b), [hovering]);
  useEffect(() => () => publishFavBox(null), []);
  if (!on || !seat || !place) return null;
  return (
    <RailPeek
      id={FAV_PEEK_ID}
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
      onHold={hovering ? (on) => favHoverIntent().hold(on) : noop}
      onDismiss={close}
      dismissOnUnstar
      bodyNote={elsewhere === "peek" ? "Open in the Tasks peek" : elsewhere ? "Open in the Claude panel" : null}
      onNoRoom={() => setNoRoom(true)}
      onOpenChat={(href) => navigateUrl(href)}
      here={here}
      pageRoot={page?.root ?? null}
      onPin={noop}
      held={!hovering}
      onHeld={noop}
      pinned={false}
    />
  );
}
