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

// ---- step fold: the bot's process folds behind the exchange (the cloud transcript's "show more", design.md §A) ----
/**
 * One STRETCH of the thread that folds: consecutive `thought` / `action` / `note` rows between two BOUNDARIES. A
 * boundary is any row that is part of the exchange and stays visible: your message, an approval / question card, the
 * `done` / `error` that ends a task, the Clear marker, a hand-off or build card, and a thought or note that carries an
 * app card (an outcome, bot.py `Here's <app>:`), a reaction, or a reply quoting it (hiding it hides your own mark).
 * So a task with three back-and-forths is three folds, each opening only the steps between that question and its
 * answer, not one lid over the whole task (owner, 2026-10-13).
 *
 * WHERE THE TRIGGER SITS: always a muted line of its own IN PLACE of the stretch, drawn by the stretch's first hidden
 * row (Thread.tsx Row). The cloud transcript seats its two words on the prose that ends the run; here that was tried
 * and dropped (owner, 2026-10-13): a thread of bubbles has the steps BETWEEN the bubbles, and a word hanging off the
 * end of a result sentence read as two kinds of control for one thing.
 * `key` is the seq of the first hidden row: the fold's identity, stable across appends and the 600-row trim.
 */
export interface FoldStretch {
  key: number;
  /** Indices (into `evs`) hidden while folded. The first one draws the trigger line and is never hidden itself. */
  hidden: number[];
  /** How many `action` rows hide, and the seconds the stretch spans: the trigger's hover. */
  steps: number;
  secs: number;
}

/** The `done` / `error` bubble a task ends on. A retried `error` ("Model call failed", no `trace`: the engine carries
 *  on) is a step, not an end; only one with a `trace` (process death, three strikes) closes the task. Cards (hand-off
 *  result, build notice) and the seeded greeting are not closers. */
export const isTaskEnd = (e: BotEvent): boolean =>
  (e.role === "done" || (e.role === "error" && e.trace != null)) && !isHandoff(e) && e.source !== "build" && e.source !== "seed";

/** The one `error` that is a step: a model call the engine retries past (agent_engine.py `Model call failed`,
 *  steps_engine.py `Model returned no usable JSON; retrying`). Every other error (a channel rejection, "Busy with
 *  another task", a playbook that would not learn) is a line to you and stays visible. */
const isRetryError = (e: BotEvent): boolean =>
  e.role === "error" && e.trace == null && /^Model (call failed|returned no usable JSON)/.test(e.text || "");

/** A row the fold may hide: the bot's process, unless something made it part of the exchange. `keep` is the thread's
 *  own extra test (a note whose text links an app the thread draws a card for, apps.ts `appFromText`). */
const hideable = (e: BotEvent, rx: Record<number, string>, quoted: Set<number>, keep: (e: BotEvent) => boolean): boolean =>
  (e.role === "thought" || e.role === "action" || e.role === "note" || isRetryError(e))
  // The Clear marker is a `note` too (source "clear"): the other fold's own boundary, never a step.
  && e.source !== "clear" && !e.app?.dir && !e.build && !isHandoff(e) && !rx[e.seq] && !quoted.has(e.seq) && !keep(e);

export interface FoldOpts {
  reactions?: Record<number, string>;
  /** The bot is mid-task, so a stretch that runs to the END of the list is still growing and is left alone (its
   *  progress is the point; a lid that goes on per step comes straight back off). */
  live: boolean;
  /** Rows the thread draws something for beyond their text (an app card inferred from the text): never hidden. */
  keep?: (e: BotEvent) => boolean;
}

/** The fold stretches of a thread. */
export function foldStretches(evs: readonly BotEvent[], { reactions, live, keep = () => false }: FoldOpts): FoldStretch[] {
  const quoted = new Set<number>();
  for (const e of evs) if (e.reply?.seq != null) quoted.add(e.reply.seq);
  const rx = reactions || {};
  const out: FoldStretch[] = [];
  let run: number[] = [];
  const close = (end: number) => {
    if (!run.length) return;
    const first = evs[run[0]], last = evs[end >= 0 ? end : run[run.length - 1]];
    out.push({
      key: first.seq, hidden: run,
      steps: run.filter((j) => evs[j].role === "action").length, secs: Math.max(0, last.ts - first.ts),
    });
    run = [];
  };
  for (let i = 0; i < evs.length; i++) {
    const e = evs[i];
    if (isNoise(e)) continue;  // draws nothing; neither hides nor bounds
    if (hideable(e, rx, quoted, keep)) { run.push(i); continue; }
    close(i);
  }
  if (!live) close(-1);
  return out;
}

/** The trigger's hover: "12 steps · 48s", "1 step", "3 messages · 5s". */
export function foldHint(t: FoldStretch): string {
  const n = t.steps ? `${t.steps} step${t.steps === 1 ? "" : "s"}` : `${t.hidden.length} message${t.hidden.length === 1 ? "" : "s"}`;
  return t.secs >= 1 ? `${n} · ${fmtSecsShort(t.secs)}` : n;
}
const fmtSecsShort = (s: number): string => (s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);

