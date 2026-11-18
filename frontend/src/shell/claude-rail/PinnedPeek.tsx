// THE PINNED WINDOWS OF THE RAIL'S TWO SECTIONS (rail-slots-lib.ts header;
// owner, 2026-11-18). Behind `rail_free_pins_enabled`: off, nothing here
// draws.
//
// With free pins the pin is the TASK'S, and a section has two windows at
// once: its non-pin beside the rail — the rail's own list card, the
// favourite's card (FavouritePeek), the bot's (BotCardHost), each exactly the
// component it was — and its PIN, drawn here. The pin's press on a non-pin
// card freezes the window's box (rail-move-store `freezeFromDock`), writes
// the section's mark (platform/lib last-chat: `aPinned` `task:<key>`,
// `favPinned`, `botOpen`) and lets the non-pin go; these hosts read the marks
// and draw the task or the bot in the SAME chassis (RailPeek, BotPeek),
// pinned from its first frame, floating at the frozen box.
//
// Mounted once, in the shell, beside the favourite's card and for its reason
// (FavouritePeek header): a hop between pages never remounts a pinned chat.
// A pinned window needs no ring to stand on — it floats — so it stands on
// pages with no rail too.
//
// WHAT LETS A PIN GO:
//   - its pin icon (the unpin): the mark is cleared and the section's non-pin
//     is asked to open on the same chat (rail-slots-store `askOpen`) — the
//     window is back beside the rail (owner's answer, 2026-11-18);
//   - its Open door, once the hop went (FavouritePeek's rule);
//   - another card of the section taking the pin: the mark names that one;
//   - its task gone — archived, erased, unstarred (a favourite's). NOT a view
//     that does not list it: the window floats and needs no ring.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Task, TaskPulseTask } from "@platform/lib/api";
import { useBotsAvailable } from "@platform/lib/bots-available";
import { useBotsRows } from "@platform/lib/botsPulse";
import { useUrlVersion } from "@platform/lib/hooks";
import {
  aPinned,
  botOpen,
  favPinned,
  lastChatId,
  setAPinned,
  setBotOpen,
  setFavPinned,
  setLastChat,
  setLastFav,
  useAPinned,
  useBotOpen,
  useFavPinned,
} from "@platform/lib/last-chat";
import { navigateUrl } from "@platform/lib/router";
import { opensOnThisPage } from "@shell/useAppPageClaudePane";
import { readListing, refreshListing, subscribeListing, useTasksPulseRows } from "@shell/tasksPulse";
import { cardTitleLine } from "@shell/tasks-lib";
import { BotPeek, type BotPeekHandle } from "./BotPeek";
import { RailPeek, type RailPeekHandle } from "./RailPeek";
import { ownPlace, pinnedBots, type RailPlace, type RailRow } from "./claude-rail-lib";
import { railDoor, railHere, useFavPeekState } from "./fav-peek-store";
import { useRailFreePins } from "./rail-free-pins-flag";
import type { RailWindow } from "./rail-move-lib";
import { FAV_HEIGHT, FAV_WIDTH, favLike, type PeekWish } from "./rail-peek-lib";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { aPinWord, viewPress } from "./rail-pin-lib";
import { aPinTask } from "./rail-slots-lib";
import { askOpen, registerPinFocus, setPinTitle } from "./rail-slots-store";

export const PIN_A_ID = "claude-rail-pin-a";
export const PIN_B_ID = "claude-rail-pin-b";
export const PIN_BOT_ID = "claude-rail-pin-bot";

/** The favourite card's wish (FavouritePeek `FAV_SIZE`), for a window B pin
 *  whose mark has no size of its own (one from before the switch). */
const FAV_SIZE: PeekWish = { width: FAV_WIDTH, maxHeight: FAV_HEIGHT, align: "bottom" };

const noop = () => {};
const noRing = () => null;

/** The place a pinned chat says it is on (FavouritePeek `placeOf`): its own
 *  canvas, else its project folder — a pinned window belongs to no page. */
function placeOf(t: TaskPulseTask, row: Task | null): RailPlace {
  return ownPlace({ ...t, canvas: t.canvas ?? row?.canvas ?? null });
}

/** A press inside a pinned window (its portaled menus included) is a press
 *  in the rail's view: it closes no non-pin (rail-pin-lib `viewPress`). */
function pressInside(): void {
  viewPress.inside = true;
}

/** SECTION `win`'S PINNED TASK: window A's (`aPinned` `task:<key>`) or window
 *  B's (`favPinned`). */
function PinnedTask({ win }: { win: RailWindow }) {
  const aWord = useAPinned();
  const favKey = useFavPinned();
  const key = win === "a" ? aPinTask(aWord) : favKey;
  const rows = useTasksPulseRows(key !== null);
  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  useEffect(() => {
    if (key === null) return;
    setListing(readListing());
    return subscribeListing((ev) => {
      if (!ev.failed) setListing(ev.rows);
    });
  }, [key]);
  const { rail, page } = useFavPeekState();
  const pulse = useMemo(() => (key ? (rows.find((t) => t.key === key) ?? null) : null), [key, rows]);
  // ITS TASK GONE (header): archived, erased — or, a favourite's, unstarred —
  // once the pulse has answered (an empty list before it says nothing).
  // Judged off the rows this render HOLDS, never the feed's own "loaded": the
  // hook's rows land a render after the feed's flag on a reload.
  const gone = key !== null && rows.length > 0 && (!pulse || pulse.status === "archived" || (win === "b" && !favLike(pulse)));
  const letGo = useCallback(() => {
    if (!key) return;
    if (win === "a") {
      if (aPinned() === aPinWord({ kind: "task", key })) setAPinned(null);
    } else if (favPinned() === key) setFavPinned(null);
  }, [win, key]);
  useEffect(() => {
    if (gone) letGo();
  }, [gone, letGo]);
  const seat = useMemo((): RailRow | null => {
    if (!pulse || gone) return null;
    return { pulse, row: listing?.find((t) => t.key === pulse.key) ?? null };
  }, [pulse, gone, listing]);
  const kept = useMemo(() => (seat ? [seat] : []), [seat]);
  const place = useMemo(() => (seat ? placeOf(seat.pulse, seat.row) : null), [seat]);
  const here = useCallback(() => railHere(rail), [rail]);
  const ref = useRef<RailPeekHandle | null>(null);
  const up = seat !== null;
  // The keyboard's way in (a ring's click on the pinned task), and the name
  // the non-pin's pin icon says it would replace.
  useEffect(() => {
    if (!up) return;
    return registerPinFocus(win, () => !!ref.current?.focusComposer() || !!ref.current?.focusChat());
  }, [win, up]);
  const title = seat ? cardTitleLine({ ...seat.pulse, ...(seat.row ?? {}) } as Task).text || seat.pulse.title || "Chat" : null;
  useEffect(() => {
    setPinTitle(win, "task", title);
    return () => setPinTitle(win, "task", null);
  }, [win, title]);
  // THE UNPIN (header): back beside the rail, as the section's non-pin. The
  // mark first — the non-pin opens no chat a pin still holds.
  const unpin = useCallback(() => {
    if (!key) return;
    letGo();
    askOpen({ win, kind: "task", key } as Parameters<typeof askOpen>[0]);
  }, [win, key, letGo]);
  // THE OPEN DOOR (FavouritePeek `openChat`): the page rail's door — the pane
  // takes the chat — else the href itself; the pin goes once the hop went.
  const openChat = useCallback(
    (href: string) => {
      if (seat) (win === "a" ? setLastChat : setLastFav)(lastChatId(seat.pulse));
      const door = railDoor(rail);
      const went = door ? door(href) : navigateUrl(href);
      void Promise.resolve(went).then((ok) => {
        if (ok !== false) letGo();
      });
      return went;
    },
    [seat, rail, win, letGo],
  );
  // The ×, Escape and the Open door's own word: a pinned window answers only
  // to its unpin and to `openChat` above; an unstar here lets a favourite's go.
  const onDismiss = useCallback(
    (why?: "door" | "unstar") => {
      if (why === "unstar") letGo();
    },
    [letGo],
  );
  useUrlVersion();
  const openIcon = (href: string) => !opensOnThisPage(href, location.pathname);
  if (!seat || !place) return null;
  return (
    <RailPeek
      ref={ref}
      id={win === "a" ? PIN_A_ID : PIN_B_ID}
      win={win}
      shown={seat}
      kept={kept}
      anchorOf={noRing}
      railEl={rail}
      place={place}
      {...(win === "b" ? { size: FAV_SIZE, resizeKey: "canvasRailFav" as const, dismissOnUnstar: true } : { oneSize: true })}
      onReload={refreshListing}
      onPointerEnter={noop}
      onPointerLeave={noop}
      onCone={noop}
      onPressInside={pressInside}
      onHold={noop}
      onDismiss={onDismiss}
      openIcon={openIcon}
      onNoRoom={noop}
      onOpenChat={openChat}
      here={here}
      pageRoot={page?.root ?? null}
      onPin={noop}
      held
      onHeld={noop}
      pinned
      pinCtl={{ pinned: true, onToggle: unpin }}
    />
  );
}

/** SECTION B'S PINNED BOT (`botOpen`, a pinned bot's mark with free pins). */
function PinnedBot() {
  const id = useBotOpen();
  const offered = useBotsAvailable(id !== null);
  const rows = useBotsRows(id !== null && offered);
  const bots = useMemo(() => pinnedBots(rows), [rows]);
  const { rail } = useFavPeekState();
  const bot = useMemo(() => (id ? (bots.find((b) => b.id === id) ?? null) : null), [id, bots]);
  // Its bot gone (unpinned from the rail, removed), once the bots answered.
  const gone = id !== null && bots.length > 0 && !bot;
  useEffect(() => {
    if (gone && botOpen() === id) setBotOpen(null);
  }, [gone, id]);
  const ref = useRef<BotPeekHandle | null>(null);
  const up = bot !== null;
  useEffect(() => {
    if (!up) return;
    return registerPinFocus("b", () => !!ref.current?.focusComposer() || !!ref.current?.focusChat());
  }, [up]);
  const name = bot ? bot.name || "Bot" : null;
  useEffect(() => {
    setPinTitle("b", "bot", name);
    return () => setPinTitle("b", "bot", null);
  }, [name]);
  const letGo = useCallback(() => {
    if (id && botOpen() === id) setBotOpen(null);
  }, [id]);
  const unpin = useCallback(() => {
    if (!id) return;
    letGo();
    askOpen({ win: "b", kind: "bot", key: id });
  }, [id, letGo]);
  if (!bot) return null;
  return (
    <BotPeek
      ref={ref}
      id={PIN_BOT_ID}
      pinSlot
      shown={bot}
      anchorOf={noRing}
      railEl={rail}
      onPointerEnter={noop}
      onPointerLeave={noop}
      onCone={noop}
      onPressInside={pressInside}
      onHold={noop}
      // Its Open door is a hand-off: the bot's page has the chat now.
      onDismiss={(why) => {
        if (why === "door") letGo();
      }}
      onNoRoom={noop}
      onOpen={(href) => void navigateUrl(href)}
      held
      onHeld={noop}
      pinned
      pinCtl={{ pinned: true, onToggle: unpin }}
    />
  );
}

/** Every pinned window, with free pins on (and the rail's card itself). */
export function PinnedPeeks() {
  const freePins = useRailFreePins();
  const peekOn = useCanvasRailPeek();
  if (!freePins || !peekOn) return null;
  return (
    <>
      <PinnedTask win="a" />
      <PinnedTask win="b" />
      <PinnedBot />
    </>
  );
}
