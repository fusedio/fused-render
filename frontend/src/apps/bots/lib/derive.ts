// Per-bot derived values shared by the list, the chat header, the preview and the live view (OpenBot core.js
// helpers). Pure: each takes the bot and, where needed, its merged event list from the store.
import type { Bot, BotEvent, Handoff, Routine } from "./api";
import { fmtWhenShort } from "./format";

/** The newest event of `role`'s seq, or -1. */
export const lastSeqOf = (evs: BotEvent[], role: BotEvent["role"]): number => {
  for (let i = evs.length - 1; i >= 0; i--) if (evs[i].role === role) return evs[i].seq;
  return -1;
};

/** A bot-initiated hand-over's ask, rewritten for the rail card / notification: the live question event (`waiting_on`)
 *  without a hand-off's "<Bot> needs you at the laptop:" lead or the login tool's "I've paused…" tail
 *  (channels.login_text). "" when there is no such event (a take-over of yours, or the event is not loaded). */
export const handoverAsk = (b: Bot, evs: BotEvent[]): string => {
  if (b.waiting_on == null) return "";
  const e = evs.find((x) => x.seq === b.waiting_on && x.role === "question");
  return e ? e.text.replace(/^.+? needs you at the laptop:\s*/, "").replace(/\s*I've paused (and opened my browser|with my browser handed) .*$/s, "").trim() : "";
};

// Routines run while you look elsewhere; the sidebar has to say so.
export const activeRoutines = (b: Bot): Routine[] => (b.routines || []).filter((r) => r.enabled);
export const routineFailing = (b: Bot): boolean => activeRoutines(b).some((r) => (r.fails || 0) > 0 || r.last_result === "error");

/** The ⟳ glyph after a bot's name: null when it has no active routine. */
export function routineGlyph(b: Bot): { warn: boolean; title: string } | null {
  const n = activeRoutines(b).length;
  if (!n) return null;
  const warn = routineFailing(b);
  return { warn, title: `${n} scheduled routine${n > 1 ? "s" : ""}${warn ? " · last run failed" : ""}` };
}

/** The idle subtitle's routine suffix: " · routine failed ×N" (warn) or " · next run 9:00 AM"; null when none. */
export function routineNote(b: Bot): { warn: true; text: string } | { warn: false; text: string } | null {
  const rs = activeRoutines(b);
  if (!rs.length) return null;
  const next = Math.min(...rs.map((r) => r.next || Infinity));
  const bad = rs.find((r) => (r.fails || 0) > 0 || r.last_result === "error");
  if (bad) return { warn: true, text: `routine failed ×${bad.fails || 1}` };
  return isFinite(next) ? { warn: false, text: `next run ${fmtWhenShort(next)}` } : null;
}

/** Super Bot's newest hand-off still in flight (received / working / blocked), or null: the list row says "waiting on <Bot>". */
export function activeHandoff(b: Pick<Bot, "handoffs">): Handoff | null {
  let best: Handoff | null = null;
  for (const h of b.handoffs || [])
    if ((h.state === "received" || h.state === "working" || h.state === "blocked") && (!best || (h.created_at || 0) >= (best.created_at || 0))) best = h;
  return best;
}

/** "running · step 3" ("running · step 3/60" with a step cap), "waiting for you", "idle · browser off", … */
export const statusLabel = (b: Bot): string =>
  ({ running: `running · step ${b.step || 0}`, waiting: "waiting for you", paused: "paused", error: "error",
     idle: b.browser?.running ? "idle" : "idle · browser off" } as Record<string, string>)[b.status] || b.status;

// Recency skips system notes: a browser going to sleep is not activity and must not reorder the list.
export const lastTs = (evs: BotEvent[]): number => {
  for (let i = evs.length - 1; i >= 0; i--) if (evs[i].role !== "system") return evs[i].ts;
  return 0;
};
// List order follows what you last did, not what the bot did, so rows stay put while agents work. lastTs still drives the row time + "New" marker.
export const lastUserTs = (evs: BotEvent[]): number => {
  for (let i = evs.length - 1; i >= 0; i--) if (evs[i].role === "user") return evs[i].ts;
  return 0;
};
// Card subtitle: the bot's latest message (not yours, not system noise), flattened to one plain line.
export const BOT_ROLES: BotEvent["role"][] = ["thought", "done", "question", "error"];
export const lastBotMsg = (evs: BotEvent[]): string => {
  for (let i = evs.length - 1; i >= 0; i--)
    if (BOT_ROLES.includes(evs[i].role) && evs[i].text) return evs[i].text.replace(/[*_`#>]+/g, "").replace(/\s+/g, " ").trim().slice(0, 160);
  return "";
};
