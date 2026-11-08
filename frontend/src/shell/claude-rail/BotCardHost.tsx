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

export { BOT_PEEK_ID };

export function BotCardHost() {
  const on = useCanvasRailPeek();
  const offered = useBotsAvailable(on);
  const rows = useBotsRows(on && offered);
  const bots = useMemo(() => pinnedBots(rows), [rows]);
  const { rail } = useFavPeekState();
  const card = useBotCardState();
  const intent = botCardIntent();
  const ref = useRef<BotPeekHandle | null>(null);

  useEffect(() => setBotCardWarm(preloadBotChat), []);
  useEffect(
    () =>
      registerBotCardHandle({
        focusComposer: () => !!ref.current?.focusComposer(),
        focusChat: () => !!ref.current?.focusChat(),
      }),
    [],
  );
  // The flag off: nothing to show.
  useEffect(() => {
    if (!on) intent.dismiss();
  }, [on, intent]);

  const shown = useMemo(() => (on && card.key ? (bots.find((b) => b.id === card.key) ?? null) : null), [on, card.key, bots]);
  if (!on || bots.length === 0) return null;
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
    />
  );
}
