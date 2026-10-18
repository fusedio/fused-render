import type { Bot } from "@apps/bots/lib/api";
import { botOrder } from "@apps/bots/lib/order";
import { seenFromBots, waitingUnread } from "@apps/bots/lib/unread";

/** Bots for the Home widget: hidden ones dropped, ordered like the Bots list (pinned, waiting on you with news,
    then the last message you sent — `last_user_ts`, since `updated` is bumped by idle-sleep and download ticks).
    `seen` is the per-bot last-opened seq, which rides the bot row as `seen_seq` (server-side since D1384;
    the Home widget and the Bots list read the same fact). Returns a new array. */
export function homeBots(bots: Bot[], seen: Record<string, number> = seenFromBots(bots)): Bot[] {
  const waiting = (b: Bot) => {
    const s = seen[b.id];
    return waitingUnread(b, s, s == null ? 0 : Math.max(0, (b.seq || 0) - s));
  };
  return bots
    .filter((b) => !b.hidden)
    .sort(botOrder(waiting, (b) => b.last_user_ts || b.created || 0));
}
