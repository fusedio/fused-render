import type { Bot } from "./api";
import { botOrder } from "./order";

/** Which bot the bare `/bots` door opens (the sidebar's Bots row, the Dock, a remount) — the one the sidebar's dot
 *  was about (owner, 2026-10-22: "when click on bot from sidebar, it should go to the bot with unread messages if
 *  any; if any bot with blocker then that opens; blocker > unread"):
 *    1. a bot WAITING on you (`status === "waiting"`, the red dot's own predicate — an approval or question card;
 *       the same word the rail prints, so a red row always lands on a red bot);
 *    2. else a bot with unread news (`unread`, the green dot's predicate over the caller's read mark);
 *    3. else the bot that was open (`current`), if it is still a visible row;
 *    4. else Super Bot (seeded on first run), else the first visible bot, else anything.
 *  Ties inside 1 and 2 go by the list's own order (lib/order.ts: pinned, then recency), so the bot opened is the
 *  one the list shows first with that mark. Hidden bots are never picked while any visible bot exists: they are off
 *  every list, and landing on one would open a chat the rail has no row for. `null` only for no bots at all. */
export function landingBot(bots: Bot[], unread: (b: Bot) => boolean, current: string | null, recency: (b: Bot) => number = (b) => b.last_user_ts || b.created || 0): string | null {
  const visible = bots.filter((b) => !b.hidden).sort(botOrder(() => false, recency));
  const waiting = visible.find((b) => b.status === "waiting");
  if (waiting) return waiting.id;
  const news = visible.find(unread);
  if (news) return news.id;
  if (current && visible.some((b) => b.id === current)) return current;
  return visible.find((b) => b.kind === "super")?.id ?? visible[0]?.id ?? bots[0]?.id ?? null;
}
