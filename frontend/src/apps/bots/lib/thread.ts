// The thread's pure rules (OpenBot chat.js render()'s thread half): session dividers, the "New" rule's slot, which
// approval/question cards are live, the answer an old card keeps ticked, and the search predicate. No DOM here.
import type { Bot, BotEvent } from "./api";
import { lastSeqOf } from "./derive";

/** Silence that starts a new "session" and earns a centered timestamp (seconds). */
export const SESSION_GAP_S = 15 * 60;

/** A hand-off line (docs §11): Super Bot's "Asked <Bot> …" card, or the target's "Sent to Super Bot: …". Shown in the
 *  thread though some are `system` rows, and never an ask Super Bot itself waits on. */
export const isHandoff = (e: BotEvent): boolean => e.source === "handoff";

/** Rows that render nothing of their own: system lines (they surface as toasts; an empty .ev wrapper keeps the row
 *  count aligned), and delivery rows (D12), which the thread joins to the bubble they refer to. Hand-off lines show. */
export const isNoise = (e: BotEvent): boolean => (e.role === "system" && !isHandoff(e)) || e.role === "delivery";

/** A `.day` divider goes above `e`: the first event, or one after more than SESSION_GAP_S of silence. `prev` is the raw previous event (system notes included). */
export const sessionBreak = (prev: BotEvent | undefined, e: BotEvent): boolean => !prev || e.ts - prev.ts > SESSION_GAP_S;

/** Index of the first shown message past the "New" mark; -1 when there is no mark or nothing past it. */
export const firstNewIndex = (evs: BotEvent[], mark: number | null | undefined): number =>
  mark == null ? -1 : evs.findIndex((e) => e.seq > mark && !isNoise(e));

/** The first message you sent after the card at `seq` (your answer to it), if any. */
export const answerTo = (evs: BotEvent[], seq: number): BotEvent | undefined => evs.find((x) => x.seq > seq && x.role === "user");

/**
 * The approval / question cards that are interactive right now. Only the one the bot waits on is live; an unanswered
 * app offer stays clickable once the bot has moved on (idle with `pending_offer` naming it): a click sends its label,
 * which send() settles without a model call.
 *
 * With `waiting_on` set, the backend names the card outright and it stays live even after a message of yours: an
 * instruction typed mid-task while an approval is up does not answer it (the backend says so with a `note`). The
 * backend clears `waiting_on` the moment its wait ends, so it is trusted whatever the status says (send() flips the
 * status to running before the engine has read the message; the card must not settle for that poll), except on an
 * idle or errored bot, where a leftover seq means a task that ended without its reset.
 * Without `waiting_on` (older backend), the live card is the last ask nobody has answered yet.
 * Hand-off questions ("<Bot> needs you at the laptop") are the other bot's ask, answered in its own thread: never live here.
 * Build notices ("The build of … is waiting for your OK") are Claude's ask, answered under Builds: never live here either.
 */
export function liveCards(all: BotEvent[], b: Pick<Bot, "status" | "pending_offer" | "waiting_on">): Set<number> {
  const evs = all.some((e) => isHandoff(e) || e.source === "build") ? all.filter((e) => !isHandoff(e) && e.source !== "build") : all;
  if (typeof b.waiting_on === "number" && b.status !== "idle" && b.status !== "error") {
    const w = b.waiting_on;
    return new Set(evs.some((e) => e.seq === w && (e.role === "approval" || e.role === "question")) ? [w] : []);
  }
  const lastAsk = Math.max(lastSeqOf(evs, "approval"), lastSeqOf(evs, "question"));
  const out = new Set<number>();
  for (const e of evs) {
    if (e.role !== "approval" && e.role !== "question") continue;
    const answered = !!answerTo(evs, e.seq);
    if ((b.status === "waiting" && e.seq === lastAsk && !answered) || (b.status === "idle" && !answered && b.pending_offer?.seq === e.seq)) out.add(e.seq);
  }
  return out;
}

/** The answer an answered question keeps ticked: your reply, trimmed and lower-cased (compare with optionKey). Null when unanswered. */
export function chosenOption(evs: BotEvent[], seq: number): string | null {
  const a = answerTo(evs, seq);
  return a ? optionKey(a.text) : null;
}
/** How an option label and an answer are compared. */
export const optionKey = (s: string | null | undefined): string => String(s ?? "").trim().toLowerCase();

// ---- search: filters the rendered thread in place ----
/** The query as applySearch compares it. */
export const searchQuery = (raw: string): string => raw.trim().toLowerCase();
/** A row (its textContent) matches the normalized query. */
export const searchHit = (text: string | null | undefined, q: string): boolean => String(text ?? "").toLowerCase().includes(q);
/** "3 matches", "1 match", "No matches". */
export const searchCountText = (n: number): string => (n ? `${n} match${n === 1 ? "" : "es"}` : "No matches");

/**
 * React keys for the rows: bot + seq, made unique when a seq repeats. Two writers on one events.jsonl (two server
 * processes on one app home, a botsend.py) can hand out the same seq, and React leaves one of two same-keyed rows
 * orphaned in the DOM when the list is swapped for another bot's — a stale bubble at the top of the next thread.
 */
export function rowKeys(evs: readonly BotEvent[], botId: string): string[] {
  const seen = new Map<number, number>();
  return evs.map((e) => {
    const n = (seen.get(e.seq) || 0) + 1;
    seen.set(e.seq, n);
    return n === 1 ? `${botId}:${e.seq}` : `${botId}:${e.seq}#${n}`;
  });
}
