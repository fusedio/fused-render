// Unread bookkeeping (OpenBot chat.js), as pure functions over plain maps; the store owns the maps and wraps these.
//   seen   id → last event seq as of opening that bot (localStorage `browser-bot.seen`)
//   base   id → the seq past which messages count toward the "N new messages" pill (in memory)
//   viewed id → seqs whose bubble has scrolled into view at least once (in memory)
import type { Bot, BotEvent } from "./api";

export const SEEN_KEY = "browser-bot.seen";

export function loadSeen(): Record<string, number> {
  try { return JSON.parse(localStorage.getItem(SEEN_KEY) || "{}") || {}; } catch { return {}; }
}
export function saveSeen(seen: Record<string, number>): void {
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(seen)); } catch { /* storage blocked */ }
}

/** Events past "seen", system notes excluded; 0 for a bot never opened. Falls back to the raw seq gap when trimmed history no longer covers the range. */
export function unreadOf(seen: number | undefined, evs: BotEvent[], b: Pick<Bot, "seq">): number {
  if (seen == null) return 0;  // never opened: nothing to catch up on
  return evs.some((e) => e.seq > seen) ? evs.filter((e) => e.seq > seen && e.role !== "system").length : Math.max(0, b.seq - seen);
}

/** Your own messages are never "new" to you; system notes never show in the thread. */
export const isNoiseEv = (e: BotEvent): boolean => e.role === "system" || e.role === "user";

/** Messages past the opening baseline that were never scrolled into view. */
export function unviewedOf(base: number | undefined, viewed: Set<number> | undefined, evs: BotEvent[]): BotEvent[] {
  const seen = viewed || new Set<number>();
  return base == null ? [] : evs.filter((e) => e.seq > base && !isNoiseEv(e) && !seen.has(e.seq));
}

/** setNewMark's arithmetic: the pill baseline and the "New" rule for a bot being opened. */
export function newMarkFor(prevSeen: number | undefined, b: Pick<Bot, "id" | "seq">): { base: number; newMark: { id: string; seq: number } | null } {
  return { base: prevSeen ?? b.seq, newMark: prevSeen != null && b.seq > prevSeen ? { id: b.id, seq: prevSeen } : null };
}

/** The list's waiting-first rule: waiting on you and either never opened or carrying unread events. */
export const waitingUnread = (b: Bot, seen: number | undefined, unread: number): boolean =>
  b.status === "waiting" && (seen == null || unread > 0);
