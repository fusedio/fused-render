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
// rings), and while no rail is up — the pane expanded, a page with no rail —
// it hides, the chat kept alive, and shows again on the next rail. A bit
// smaller than the list's card (`FAV_SIZE`), and STUCK TO THE BOTTOM RIGHT
// (owner: "it should stick to bottom so we can have it at bottom right all
// the time and we can close and open it"): its bottom on the rail's, not on
// its ring — the one place it is always found. Over the page's own header
// bars (z-index, claude-rail.css), so its × is always reachable.
//
// WHICH favourite is the server's (`starred_open`, one at a time): the ×
// here writes it closed, a favourite ring's press on any rail writes it open
// (ClaudeRail `openFavourite`). Nothing else closes it — not an outside
// press, not Escape, not a place change. The Open door opens the pane; the
// card stays the recorded open favourite (RailPeek `onDismiss("door")`) and
// is back when the rail is. A rail with no room for it (a window shrunk to
// nothing) hides it until the window or the rail changes shape, or the next
// rail stands.
//
// A RING'S PRESS SHOWS AT ONCE (owner, 2026-11-04: "click on the pin
// doesn't open it and switch it" — on a loaded Mac the pulse carrying
// `starred_open` is seconds behind): the server's "open" answer
// (fav-peek-store `onFavOpened`) puts the card up on that key, or switches
// it there, without waiting for the pulse; the pulse takes over once it
// agrees, or once it names a favourite another window opened since
// (`useFavOpenMark`).
//
// THE × HIDES AT ONCE (`closing`): the record's "closed" is a write and a
// pulse away; the card goes on the press and comes back only if the write
// is refused. An UNSTAR here shuts the card the same way and writes nothing
// more — the server dropped the record with the star (RailPeek
// `dismissOnUnstar`).
//
// THE KEYBOARD IS NEVER LEFT ON THE BODY: a card that shuts or hides with
// the focus inside it hands it to the rail — the favourite's foot ring, else
// the rail's first ring, else the rail (fav-peek-store `focusRail`). And a
// keyboard press on a favourite's ring lands on this card's × once it
// stands (fav-peek-store `pendingFocus`), however long the write and the
// pulse took.
//
// ONE CHAT, ONE COMPOSER: while the page's URL names this favourite's
// session (the pane open on it, the Open door taken), the pane holds the
// chat — the card unmounts its own and says where it is, the record and the
// card's place kept, and mounts it again when the URL moves on.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { setTaskFavourite, type Task, type TaskPulseTask } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { notify } from "@platform/lib/notifications";
import { pokeTasks, readListing, refreshListing, subscribeListing, useTasksPulseRows } from "@shell/tasksPulse";
import { RailPeek } from "./RailPeek";
import { focusRail, onFavOpened, publishFavBox, takeFavFocus, useFavOpenMark, useFavPeekState } from "./fav-peek-store";
import { useUrlVersion } from "@platform/lib/hooks";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { useTaskFavourites } from "@platform/lib/favourites-flag";
import type { RailPlace, RailRow } from "./claude-rail-lib";
import { FAV_HEIGHT, FAV_WIDTH, errorWords, type PeekWish } from "./rail-peek-lib";

export const FAV_PEEK_ID = "claude-rail-fav-peek";

/** A bit under the list card's 420×640 (owner: "by default the size of
 *  that preview could be a bit smaller"); the bottom on the rail's. The
 *  reader's drag from its top and left edges replaces the width and height
 *  (RailPeek `resizeKey`, ui-layout `canvasRailFav`). */
const FAV_SIZE: PeekWish = { width: FAV_WIDTH, maxHeight: FAV_HEIGHT, align: "bottom" };

/** The place the card says the chat is on: the row's own canvas, else its
 *  project folder — this card belongs to no page. */
function placeOf(t: TaskPulseTask, row: Task | null): RailPlace {
  const canvas = t.canvas ?? row?.canvas ?? null;
  if (canvas) {
    return { kind: "canvas", env: canvas.env, name: canvas.name, dir: t.project || null, file: canvas.file || null };
  }
  return { kind: "folder", dir: t.project || "", word: "Folder" };
}

const noop = () => {};

/** The session the page's URL names (`session_id`), or null. */
function urlSession(): string | null {
  try {
    return new URLSearchParams(location.search).get("session_id");
  } catch {
    return null;
  }
}

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
  const { rail, pendingFocus } = useFavPeekState();
  // No room beside this rail (RailPeek `onNoRoom`): hidden until the window
  // or the rail changes shape, or another rail stands.
  const [noRoom, setNoRoom] = useState(false);
  useEffect(() => {
    setNoRoom(false);
    const re = () => setNoRoom(false);
    window.addEventListener("resize", re);
    // The rail's own box: a pane shutting, a sidebar folding — the window
    // stays the same size while the rail grows back into room.
    const ro = rail && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (rail) ro?.observe(rail);
    return () => {
      window.removeEventListener("resize", re);
      ro?.disconnect();
    };
  }, [rail]);
  // THE PRESSED FAVOURITE SHOWS AT ONCE (header): the server's "open"
  // answer to a ring press (`onFavOpened`) stands over the pulse's
  // `starred_open` until the pulse agrees, or says another window moved it
  // since (fav-peek-store `useFavOpenMark`). A refused write never marks.
  const pulseOpen = useMemo(() => rows.find((t) => t.starred_open && t.status !== "archived") ?? null, [rows]);
  const liveFav = useCallback(
    (key: string) => rows.some((t) => t.key === key && t.starred && t.status !== "archived"),
    [rows],
  );
  const opened = useFavOpenMark(pulseOpen?.key ?? null, liveFav);
  const open = useMemo(
    () => (opened.key ? (rows.find((t) => t.key === opened.key) ?? pulseOpen) : pulseOpen),
    [opened.key, rows, pulseOpen],
  );
  const seat = useMemo((): RailRow | null => {
    if (!open) return null;
    return { pulse: open, row: listing?.find((t) => t.key === open.key) ?? null };
  }, [open, listing]);
  // Shut by the × or an unstar, ahead of the pulse (header): the key it was
  // shut on, dropped once the record moves on.
  const [closing, setClosing] = useState<string | null>(null);
  const openKey = open?.key ?? null;
  useEffect(() => {
    if (closing && closing !== openKey) setClosing(null);
  }, [closing, openKey]);
  // …or once an "open" on that key is answered open: × then the same ring
  // again before the pulse ever said closed leaves `openKey` unchanged.
  const markOpened = opened.mark;
  useEffect(
    () =>
      onFavOpened((key) => {
        markOpened(key);
        setClosing((c) => (c === key ? null : c));
      }),
    [markOpened],
  );
  const shown = on && seat && rail && !noRoom && closing !== seat.pulse.key ? seat : null;
  // The pane (or the Open door) on this very session (header): no chat here.
  useUrlVersion();
  const sid = urlSession();
  const elsewhere = !!seat && !!sid && (sid === seat.pulse.session_id || sid === seat.pulse.key);
  const kept = useMemo(() => (seat && !elsewhere ? [seat] : []), [seat, elsewhere]);

  // FOCUS NEVER FALLS TO THE BODY (header). Whether the keyboard was last
  // inside the card is followed document-wide: a card that unmounts takes
  // its focus with it and no event says where it was.
  const focusIn = useRef(false);
  useEffect(() => {
    const inCard = (n: EventTarget | null) => !!n && !!document.getElementById(FAV_PEEK_ID)?.contains(n as Node);
    const onIn = (e: FocusEvent) => {
      focusIn.current = inCard(e.target);
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
  // keyboard goes to its ring back in the list, else the rail's first ring.
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
  // The rail itself is the anchor: with `align: "bottom"` the card's bottom
  // lands on the rail's, clamped inside the row — the bottom right, always.
  const anchorOf = useCallback(() => rail, [rail]);
  const place = useMemo(() => (seat ? placeOf(seat.pulse, seat.row) : null), [seat]);
  // The × (RailPeek `onDismiss`): the card goes now, the record says closed;
  // the pulse then takes `open` away. A refused write brings the card back.
  const close = useCallback(
    (why?: "door" | "unstar") => {
      // The Open door is not a close: the pane takes over, the record stands.
      if (why === "door" || !open) return;
      const key = open.key;
      setClosing(key);
      // An unstar dropped the record with the star: nothing more to write.
      if (why === "unstar") {
        unstarred.current = key;
        return;
      }
      setTaskFavourite(key, null, false).then(pokeTasks, (err: unknown) => {
        setClosing((c) => (c === key ? null : c));
        notify({ title: errorWords(err, "The favourite could not be closed"), tone: "error" });
      });
    },
    [open],
  );
  useEffect(() => () => publishFavBox(null), []);
  if (!on || !seat || !place) return null;
  return (
    <RailPeek
      id={FAV_PEEK_ID}
      shown={shown}
      kept={kept}
      anchorOf={anchorOf}
      railEl={rail}
      place={place}
      size={FAV_SIZE}
      resizeKey="canvasRailFav"
      onBox={publishFavBox}
      onReload={refreshListing}
      onPointerEnter={noop}
      onPointerLeave={noop}
      onCone={noop}
      onPressInside={noop}
      onHold={noop}
      onDismiss={close}
      dismissOnUnstar
      bodyNote={elsewhere ? "Open in the Claude panel" : null}
      onNoRoom={() => setNoRoom(true)}
      onOpenChat={(href) => navigateUrl(href)}
      onPin={noop}
      held
      onHeld={noop}
      pinned={false}
    />
  );
}
