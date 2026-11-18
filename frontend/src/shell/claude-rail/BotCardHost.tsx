// THE PINNED BOT'S CARD, MOUNTED ONCE IN THE SHELL (bot-card-store header;
// owner, 2026-11-08): beside FavouritePeek and TasksPeek, so a page's view
// switch — the page's rail going down and up — leaves it standing, where it
// used to flicker out and back. It stands beside whichever rail is registered
// (fav-peek-store `rail`), level with that rail's ring for the bot (the
// store's rings). The rail keeps every rule about its rings (hover, press,
// toggle, outside press, Escape, the frame's blur, window B's exclusivity);
// this host draws the card and answers its own events.
import { useEffect, useMemo, useRef } from "react";
import { useBotsAvailable } from "@platform/lib/bots-available";
import { useBotsRows } from "@platform/lib/botsPulse";
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
import { useCanvasRailPeek } from "./rail-peek-flag";
import { viewPress } from "./rail-pin-lib";
import { useTaskHeadUnified } from "@shell/task-head-flag";
import { useUrlVersion } from "@platform/lib/hooks";
import { botOpen, setBotOpen, setFavOpen, setFavPinned, useBotOpen } from "@platform/lib/last-chat";
import { useRailFreePins } from "./rail-free-pins-flag";
import { freezeFromDock } from "./rail-move-store";
import { otherSlot, registerPinFocus, setFreeSlot, setPinTitle, useFreeSlot, type Slot } from "./rail-slots-store";
import { PIN_BOT_ID, usePinHasBox } from "./use-pin-box";

export { BOT_PEEK_ID };

/** The second slot of the bot's window (rail-slots-store "THE TWO SLOTS OF A
 *  CHAT WINDOW"): mounted only with free pins on. */
export function BotCardHostSlotY() {
  return useRailFreePins() ? <BotCardHost slot="y" /> : null;
}

const noop = () => {};
const noRing = () => null;

export function BotCardHost({ slot = "x" }: { slot?: Slot } = {}) {
  const on = useCanvasRailPeek();
  const offered = useBotsAvailable(on);
  const rows = useBotsRows(on && offered);
  const bots = useMemo(() => pinnedBots(rows), [rows]);
  const { rail } = useFavPeekState();
  const card = useBotCardState();
  const intent = botCardIntent();
  const ref = useRef<BotPeekHandle | null>(null);
  // HOVER / TEMP / PIN (rail-pin-lib): the unified-head switch.
  const pinModel = useTaskHeadUnified();
  const freePins = useRailFreePins();
  // ONE OF TWO SLOTS, BY ROLE (rail-slots-store): the FREE slot is this host
  // as it always was — the store's card, the rings' hovers and presses; the
  // other shows the PINNED bot (last-chat `botOpen`, a pinned bot's mark with
  // free pins) and takes no part in the non-pin's rules. The pin's press
  // swaps the two roles: the card the reader pinned is never remounted.
  const freeNow = useFreeSlot("bot");
  const isFree = !freePins || slot === freeNow;
  const pinRole = !isFree;
  const pinnedId = useBotOpen();
  // A PAGE HOP keeps only a pinned card (rail-pin-lib `hopKeeps`): a temp one
  // goes with it. The page is the URL's path (a view switch on one page keeps
  // it, as it keeps the favourite's).
  useUrlVersion();
  const path = typeof location === "undefined" ? "" : location.pathname;
  const pathWas = useRef(path);
  useEffect(() => {
    if (pathWas.current === path) return;
    pathWas.current = path;
    if (isFree && pinModel && card.key && card.pinned && !card.held) intent.dismiss();
  }, [path, pinModel, card.key, card.pinned, card.held, intent, isFree]);

  useEffect(() => setBotCardWarm(preloadBotChat), []);
  // The rail's way into the NON-PIN card is the free slot's.
  useEffect(() => {
    if (!isFree) return;
    return registerBotCardHandle({
      focusComposer: () => !!ref.current?.focusComposer(),
      focusChat: () => !!ref.current?.focusChat(),
    });
  }, [isFree]);
  // The flag off: nothing to show.
  useEffect(() => {
    if (!on && isFree) intent.dismiss();
  }, [on, intent, isFree]);

  // THE PINNED BOT (the pin role): gone once the bots — answered — no longer
  // list it (unpinned from the rail, removed), and the pin goes with it.
  const pinBot = useMemo(() => (pinRole && pinnedId ? (bots.find((b) => b.id === pinnedId) ?? null) : null), [pinRole, pinnedId, bots]);
  const pinGone = pinRole && pinnedId !== null && bots.length > 0 && !pinBot;
  useEffect(() => {
    if (pinGone && botOpen() === pinnedId) setBotOpen(null);
  }, [pinGone, pinnedId]);
  const freeShown = useMemo(
    () => (on && isFree && card.key ? (bots.find((b) => b.id === card.key) ?? null) : null),
    [on, isFree, card.key, bots],
  );
  const placed = usePinHasBox("b", pinRole && on && !!pinBot, rail);
  const shown = pinRole ? (on && placed ? pinBot : null) : freeShown;
  // The keyboard's way into the pinned window, and its name for the
  // non-pin's pin icon (rail-slots-store).
  const pinUp = pinRole && !!shown;
  useEffect(() => {
    if (!pinUp) return;
    return registerPinFocus("b", () => !!ref.current?.focusComposer() || !!ref.current?.focusChat());
  }, [pinUp]);
  const pinName = pinUp && shown ? shown.name || "Bot" : null;
  useEffect(() => {
    if (!pinRole) return;
    setPinTitle("b", "bot-" + slot, pinName);
    return () => setPinTitle("b", "bot-" + slot, null);
  }, [pinRole, slot, pinName]);
  if (!on || bots.length === 0) return null;
  // THE PINNED WINDOW: the same card, frozen where its pin left it. Its unpin
  // makes this slot the free one again, open on the same bot — it docks
  // itself, and a favourite's non-pin standing there gives way (one window
  // B, as a ring's press).
  if (pinRole) {
    return (
      <BotPeek
        ref={ref}
        id={PIN_BOT_ID}
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
        // Its Open door is a hand-off: the bot's page has the chat now.
        onDismiss={(why) => {
          if (why === "door" && pinnedId && botOpen() === pinnedId) setBotOpen(null);
        }}
        onNoRoom={noop}
        onOpen={(href) => void navigateUrl(href)}
        held
        onHeld={noop}
        pinned
        pinCtl={{
          pinned: true,
          onToggle: () => {
            if (!pinnedId) return;
            setFreeSlot("bot", slot);
            setBotOpen(null);
            setFavOpen(null);
            intent.press(pinnedId);
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
      pinCtl={
        pinModel
          ? {
              pinned: card.held,
              onToggle: () => {
                // FREE PINS (rail-slots-lib): the pin's press freezes the
                // window where it stands, and the two slots SWAP ROLES — this
                // card, as it is, becomes window B's pinned window (replacing
                // a favourite or a bot pinned there); the store's card, the
                // non-pin, is the other slot's from here on and is put down.
                const id = botCardIntent().current;
                if (freePins) {
                  if (!id) return;
                  freezeFromDock("b");
                  setFavPinned(null);
                  setBotOpen(id);
                  setFreeSlot("bot", otherSlot(slot));
                  botCardIntent().dismiss();
                  return;
                }
                setBotCardHeld(!card.held);
              },
            }
          : null
      }
    />
  );
}
