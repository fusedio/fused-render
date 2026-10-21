// Unread bookkeeping (OpenBot chat.js), as pure functions over plain maps; the store owns the maps and wraps these.
//   seen   id → last event seq as of opening that bot (the server's `Bot.seen_seq`, POST /api/bots/<id>/seen; D1386)
//   base   id → the seq past which messages count toward the "N new messages" pill (in memory)
//   viewed id → seqs whose bubble has scrolled into view at least once (in memory)
import type { Bot, BotEvent } from "./api";

/** import-only (D1386): the old localStorage home of the seen map. The desktop app walks ports 1777..1787 then an
 *  ephemeral one, and each port is a new origin with empty storage, so a relaunch on another port lit every bot's
 *  unread state afresh. "Seen" is now `seen_seq` on the bot row; store.ts hydrateBotsPrefs pushes a leftover map to
 *  the server once and removes the key. */
export const SEEN_KEY = "browser-bot.seen";

/** The seen map off the server's bot rows (Bot.seen_seq; absent = never opened). For a reader without the Bots
 *  store, e.g. the Home widget, which polls /api/bots itself. */
export function seenFromBots(bots: Pick<Bot, "id" | "seen_seq">[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const b of bots) if (typeof b.seen_seq === "number") out[b.id] = b.seen_seq;
  return out;
}

/** import-only (D1386): the old `{id: seq}` map, finite numbers only; {} when absent or unreadable. */
export function parseSeenMap(raw: string | null): Record<string, number> {
  try {
    const v = JSON.parse(raw || "{}") as unknown;
    if (!v || typeof v !== "object" || Array.isArray(v)) return {};
    const out: Record<string, number> = {};
    for (const [id, seq] of Object.entries(v as Record<string, unknown>)) if (id && typeof seq === "number" && Number.isFinite(seq)) out[id] = seq;
    return out;
  } catch { return {}; }
}

/** Events past "seen", system notes excluded; 0 for a bot never opened. A hand-off result (role done, docs §11) counts like
 *  any done; the hand-off "Asked …" / "Sent to Super Bot" lines are system rows and do not. Falls back to the raw seq gap when trimmed history no longer covers the range. */
export function unreadOf(seen: number | undefined, evs: BotEvent[], b: Pick<Bot, "seq">): number {
  if (seen == null) return 0;  // never opened: nothing to catch up on
  // Delivery rows (docs §10 D12) are bookkeeping joined to a bubble, not a message: a texted reply is one new item, not two.
  return evs.some((e) => e.seq > seen) ? evs.filter((e) => e.seq > seen && e.role !== "system" && e.role !== "delivery").length : Math.max(0, b.seq - seen);
}

/** Your own messages are never "new" to you; system notes never show in the thread. */
export const isNoiseEv = (e: BotEvent): boolean => e.role === "system" || e.role === "user" || e.role === "delivery";

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
