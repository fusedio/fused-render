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

/** Index of the last Clear context marker (source "clear"); -1 when the context was never cleared, or the marker has
 *  fallen off the page's event cap (then everything held is after it). Rows before it fold into "earlier conversation". */
export const clearIndex = (evs: readonly BotEvent[]): number => {
  for (let i = evs.length - 1; i >= 0; i--) if (evs[i].source === "clear") return i;
  return -1;
};

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

// ---- turn fold: a settled task's steps fold behind its result (the cloud transcript's "show more", design.md §A) ----
/**
 * One task's stretch of the thread, as the fold sees it. The stream has no turn object: a turn is derived from its
 * CLOSER, the `done` / `error` row that ends it (routine and hand-off tasks have no `user` opener, only a noise
 * `system "Task started"` line, so openers are no boundary). `hidden` are the indices of the rows between the previous
 * closer and this one that fold away: the bot's process. What stays visible, in the order the thread draws it:
 *   - approval / question cards and the `user` answer under them (the exchange is part of the conversation);
 *   - a `thought` or `note` carrying an app card (an outcome, bot.py `Here's <app>:`), one with a reaction, or one a
 *     later message quotes (`reply.seq`): hiding it hides the reader's own mark;
 *   - hand-off cards, build cards, the Clear marker, "Sent to Super Bot" lines: cross-bot state with its own door.
 * A closer with nothing to hide above it is not a turn (no entry, no trigger drawn).
 */
export interface TurnFold {
  /** Index of the `done` / `error` row that carries the trigger. */
  closer: number;
  /** Indices (into `evs`) hidden while the turn is folded. */
  hidden: number[];
  /** How many `action` rows the fold hides, and the seconds from the first hidden row to the closer: the trigger's hover. */
  steps: number;
  secs: number;
}

/** Is `e` the row a task ends on? A `done` / `error` drawn as a bubble; the cards (hand-off result, build notice) and
 *  the seeded greeting are not closers. */
const isCloser = (e: BotEvent): boolean =>
  (e.role === "done" || e.role === "error") && !isHandoff(e) && e.source !== "build" && e.source !== "seed";

export function turnFolds(evs: readonly BotEvent[], reactions: Record<number, string> | undefined): TurnFold[] {
  const quoted = new Set<number>();
  for (const e of evs) if (e.reply?.seq != null) quoted.add(e.reply.seq);
  const rx = reactions || {};
  const out: TurnFold[] = [];
  let start = 0;
  for (let i = 0; i < evs.length; i++) {
    const e = evs[i];
    if (e.source === "clear") { start = i + 1; continue; }  // the Clear marker is the other fold's boundary
    if (!isCloser(e)) continue;
    const hidden: number[] = [];
    let steps = 0;
    for (let j = start; j < i; j++) {
      const m = evs[j];
      if (m.role !== "thought" && m.role !== "action" && m.role !== "note") continue;
      if (m.app?.dir || m.build || isHandoff(m) || rx[m.seq] || quoted.has(m.seq)) continue;
      hidden.push(j);
      if (m.role === "action") steps++;
    }
    if (hidden.length) out.push({ closer: i, hidden, steps, secs: Math.max(0, e.ts - evs[hidden[0]].ts) });
    start = i + 1;
  }
  return out;
}

/** The trigger's hover: "12 steps · 48s", "1 step", "3 messages · 5s". */
export function turnFoldHint(t: TurnFold): string {
  const n = t.steps ? `${t.steps} step${t.steps === 1 ? "" : "s"}` : `${t.hidden.length} message${t.hidden.length === 1 ? "" : "s"}`;
  return t.secs >= 1 ? `${n} · ${fmtSecsShort(t.secs)}` : n;
}
const fmtSecsShort = (s: number): string => (s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);
