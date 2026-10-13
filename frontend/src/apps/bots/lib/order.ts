import type { Bot } from "./api";

/** Bot list order: pinned first, then bots waiting on you, then newest recency first. */
export function botOrder(waiting: (b: Bot) => boolean, recency: (b: Bot) => number): (a: Bot, b: Bot) => number {
  return (a, b) => (Number(!!b.pinned) - Number(!!a.pinned)) || (Number(waiting(b)) - Number(waiting(a))) || (recency(b) - recency(a));
}
