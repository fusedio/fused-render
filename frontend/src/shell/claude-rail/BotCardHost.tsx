// THE PINNED BOT'S CARD, MOUNTED ONCE IN THE SHELL (bot-card-store header;
// owner, 2026-11-08): beside FavouritePeek and TasksPeek, so a page's view
// switch — the page's rail going down and up — leaves it standing, where it
// used to flicker out and back. It stands beside whichever rail is registered
// (fav-peek-store `rail`), level with that rail's ring for the bot (the
// store's rings). The rail keeps every rule about its rings (hover, press,
// toggle, outside press, Escape, the frame's blur, window B's exclusivity);
// this host draws the card and answers its own events.
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { useBotsAvailable } from "@platform/lib/bots-available";
import { botsPulseAnswered, botsRowsNow, useBotsRows } from "@platform/lib/botsPulse";
import { navigateUrl } from "@platform/lib/router";
import { BotPeek, BOT_PEEK_ID, preloadBotChat, type BotPeekHandle } from "./BotPeek";
import {
  botCardIntent,
  botPress,
  botPressed,
  botRing,
  registerBotCardHandle,
  setBotCardHeld,
  setBotCardNoRoom,
  setBotCardWarm,
  useBotCardState,
} from "./bot-card-store";
import { pinnedBots } from "./claude-rail-lib";
import { useFavPeekState } from "./fav-peek-store";
import { viewPress } from "./rail-pin-lib";
import { useUrlVersion } from "@platform/lib/hooks";
import { addBotPin, botPinId, dropBotPin, useBotPins } from "@platform/lib/last-chat";
import { useRailExtras } from "./rail-extras-flag";
import { freezeFromDock } from "./rail-move-store";
import { pinSlot, registerPinFocus, syncSlots, useSlots, type Slot } from "./rail-slots-store";
import { PIN_BOT_ID, pinDomId, usePinHasBox } from "./use-pin-box";

export { BOT_PEEK_ID };

/** THE BOT'S WINDOW, ONE CARD PER SLOT (rail-slots-store "THE SLOTS OF A CHAT
 *  WINDOW"): with free pins on, the free slot and one slot per pinned bot —
 *  as many as the reader pinned (owner, 2026-11-24) — each keyed by its
 *  slot's id, so a pin's press never remounts the card it pins. The slots
 *  follow this window's pinned bots (`botPins`). Off, the one card. */
const FREE_X: Slot = { id: "x", pin: null, closing: false };
export function BotCardHostSlots() {
  const freePins = useRailExtras();
  const slots = useSlots("bot");
  const ids = useBotPins();
  useLayoutEffect(() => {
    if (freePins) syncSlots("bot", ids);
  }, [freePins, ids]);
  if (!freePins) return <BotCardHost key="x" slot={FREE_X} />;
  return (
    <>
      {slots.map((s) => (
        <BotCardHost key={s.id} slot={s} />
      ))}
    </>
  );
}

const noop = () => {};
const noRing = () => null;

export function BotCardHost({ slot = FREE_X }: { slot?: Slot } = {}) {
  const offered = useBotsAvailable(true);
  const rows = useBotsRows(offered);
  const bots = useMemo(() => pinnedBots(rows), [rows]);
  const { rail } = useFavPeekState();
  const card = useBotCardState();
  const intent = botCardIntent();
  const ref = useRef<BotPeekHandle | null>(null);
  const freePins = useRailExtras();
  // ONE SLOT, BY ROLE (rail-slots-store): the FREE slot is this host as it
  // always was — the store's card, the rings' hovers and presses; a pinned
  // slot shows ITS pinned bot (`slot.pin`, one of last-chat `botPins`) and
  // takes no part in the non-pin's rules. The pin's press hands the free
  // slot the pin: the card the reader pinned is never remounted.
  const isFree = !freePins || slot.pin === null;
  const pinRole = !isFree;
  const pinnedId = pinRole ? slot.pin : null;
  const pinId = pinnedId !== null ? botPinId(pinnedId) : null;
  // A PAGE HOP keeps only a pinned card (rail-pin-lib `hopKeeps`): a temp one
  // goes with it. The page is the URL's path (a view switch on one page keeps
  // it, as it keeps the favourite's).
  useUrlVersion();
  const path = typeof location === "undefined" ? "" : location.pathname;
  const pathWas = useRef(path);
  useEffect(() => {
    if (pathWas.current === path) return;
    pathWas.current = path;
    if (isFree && card.key && card.pinned && !card.held) intent.dismiss();
  }, [path, card.key, card.pinned, card.held, intent, isFree]);

  useEffect(() => setBotCardWarm(preloadBotChat), []);
  // The rail's way into the NON-PIN card is the free slot's.
  useEffect(() => {
    if (!isFree) return;
    return registerBotCardHandle({
      focusComposer: () => !!ref.current?.focusComposer(),
      focusChat: () => !!ref.current?.focusChat(),
    });
  }, [isFree]);
  // THE PINNED BOT (the pin role): gone once the bots — ANSWERED — no longer
  // list it (unpinned from the rail, removed), and the pin goes with it.
  // Judged off the feed itself, not this render's rows: an empty list is
  // "none left" only once an answer has landed in this document (CodeRabbit
  // on #2349: the LAST rail bot removed left the mark standing, with no
  // window, and the rail quiet for good) — before that it is what the last
  // session remembered, or nothing.
  const liveBot = useMemo(
    () => (pinnedId !== null && !slot.closing ? (bots.find((b) => b.id === pinnedId) ?? null) : null),
    [pinnedId, slot.closing, bots],
  );
  useEffect(() => {
    if (pinnedId === null || slot.closing || !offered || !botsPulseAnswered()) return;
    if (!pinnedBots(botsRowsNow()).some((b) => b.id === pinnedId)) dropBotPin(pinnedId);
  }, [pinnedId, slot.closing, offered, bots]);
  // A PINNED SLOT LET GO FADES OUT WHERE IT STANDS (rail-slots-lib): the bot
  // it showed, drawn hidden until the slot goes.
  const fadeBot = useRef<typeof liveBot>(null);
  if (liveBot) fadeBot.current = liveBot;
  const freeShown = useMemo(
    () => (isFree && card.key ? (bots.find((b) => b.id === card.key) ?? null) : null),
    [isFree, card.key, bots],
  );
  const placed = usePinHasBox("b", pinId, !!liveBot, rail);
  const shown = pinRole ? (placed ? liveBot : null) : freeShown;
  // The keyboard's way into the pinned window, by its pin's id.
  const pinUp = pinId !== null && !!shown;
  useEffect(() => {
    if (!pinUp || pinId === null) return;
    return registerPinFocus(pinId, () => !!ref.current?.focusComposer() || !!ref.current?.focusChat());
  }, [pinUp, pinId]);
  if (bots.length === 0) return null;
  // THE PINNED WINDOW: the same card, frozen where its pin left it, until its
  // × closes it — and it fades out where it stands.
  if (pinRole) {
    if (!liveBot && !(slot.closing && fadeBot.current)) return null;
    return (
      <BotPeek
        ref={ref}
        id={pinDomId(PIN_BOT_ID, slot.id)}
        pinId={pinId}
        pinSlot
        shown={shown}
        anchorOf={noRing}
        railEl={rail}
        onPointerEnter={noop}
        onPointerLeave={noop}
        onCone={noop}
        onPressInside={() => {
          viewPress.inside = true;
        }}
        onHold={noop}
        // Its Open door is a hand-off — the bot's page has the chat — ONCE THE
        // HOP WENT (the favourite's rule, FavouritePeek `openChat`; CodeRabbit
        // on #2349): a leave prompt the reader declines keeps the page, and
        // the pinned window with it. The chassis' own word for the door
        // (`onDismiss("door")`, said at the press) lets nothing go.
        onDismiss={noop}
        onNoRoom={noop}
        onOpen={(href) => {
          const id = pinnedId;
          void navigateUrl(href).then((ok) => {
            if (ok !== false && id !== null) dropBotPin(id);
          });
        }}
        held
        onHeld={noop}
        pinned
        pinCtl={{
          pinned: true,
          // Its × (rail-slots-lib "A PINNED WINDOW CLOSES BY ITS ×"): this
          // pin alone.
          onToggle: () => {
            if (pinnedId !== null) dropBotPin(pinnedId);
          },
        }}
      />
    );
  }
  return (
    <BotPeek
      ref={ref}
      id={BOT_PEEK_ID}
      shown={shown}
      anchorOf={botRing}
      // The rail between pages (none registered for a beat): the card keeps
      // the place it has (BotPeek re-places only on a new bot, or with a
      // rail and a ring to place against).
      railEl={rail}
      onPointerEnter={() => intent.enterCard()}
      onPointerLeave={() => intent.leaveCard()}
      onCone={(inside) => (inside ? intent.enterCone() : intent.leaveCone())}
      onPressInside={() => {
        botPress.inside = true;
        viewPress.inside = true;
        intent.pressStart();
      }}
      onHold={(held) => intent.hold(held)}
      onDismiss={() => {
        const key = intent.current;
        intent.dismiss(true);
        if (key) botRing(key)?.focus({ preventScroll: true });
      }}
      // NO ROOM (the list card's `onNoRoom`): the rings are doors until the
      // row grows (ClaudeRail clears it), and a press that found none opens
      // the bot's page.
      onNoRoom={() => {
        intent.dismiss();
        setBotCardNoRoom(true);
        const href = botPressed.href;
        botPressed.href = null;
        if (href) void navigateUrl(href);
      }}
      onOpen={(href) => void navigateUrl(href)}
      held={card.held}
      onHeld={setBotCardHeld}
      pinned={card.pinned}
      pinCtl={{
        pinned: card.held,
        onToggle: () => {
          // FREE PINS (rail-slots-lib): the pin's press freezes the
          // window where it stands, and this slot TAKES THE PIN — this
          // card, as it is, becomes one more of window B's pinned windows
          // (every other pin stays: many pins, 2026-11-24); the store's
          // card, the non-pin, is a fresh slot's from here on and is put
          // down.
          const id = botCardIntent().current;
          if (freePins) {
            if (!id) return;
            freezeFromDock("b", botPinId(id));
            pinSlot("bot", slot.id, id);
            addBotPin(id);
            botCardIntent().dismiss();
            return;
          }
          if (card.held) {
            setBotCardHeld(false);
            return;
          }
          // The pin from the keyboard pins too (a pointer's press inside the
          // card already did, `onPressInside`), so the reload mark follows —
          // the task card's pin, the same way.
          if (id && !botCardIntent().isPinned) botCardIntent().press(id);
          setBotCardHeld(true);
        },
      }}
    />
  );
}
