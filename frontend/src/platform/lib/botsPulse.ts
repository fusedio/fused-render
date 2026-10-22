import { useEffect, useState } from "react";
import { getJson } from "./api";

// What the sidebar's Bots entry says about the page behind it — the pure half,
// so the derivation is testable without a DOM or a poll (the shape
// tasks-lib.tasksPulse / pulseTitle set for the Tasks row).
//
// One bot row here is the SMALL shape `GET /api/bots/pulse` answers with (and
// the Bots page's own `/api/bots` rows satisfy it too, since they carry the
// same four fields): status, the newest event seq, the read mark and the hidden
// flag. Nothing else about a bot is the sidebar's business.
// `status` is a plain string here: platform may not import apps/bots, and the
// sidebar only ever compares it to the three words below.

export interface BotPulseRow {
  id: string;
  status: string;
  seq: number;
  /** The newest MESSAGE row's seq (bot.py news_seq) — system notes and delivery rows past it are bookkeeping, not
   *  news. Absent (an older server): `seq` stands in. */
  news_seq?: number | null;
  /** The seq the reader had read up to when they last opened the bot
   *  (`Bot.seen_seq`, D1386). Absent = never opened = nothing to catch up on,
   *  the same contract apps/bots/lib/unread.unreadOf keeps. */
  seen_seq?: number | null;
  hidden?: boolean;
}

export interface BotsPulse {
  /** Bots stopped on an approval / question card — `status === "waiting"`.
   *  The red half of the readout, like a blocked task. */
  waiting: number;
  /** Bots mid-task. */
  running: number;
  /** Bots that ended their last task in an error. Tooltip only. */
  error: number;
  /** Bots with events past the reader's mark — a COUNT OF BOTS, not of
   *  messages, the way the Tasks chip counts rows. */
  unread: number;
}

export const EMPTY_BOTS_PULSE: BotsPulse = { waiting: 0, running: 0, error: 0, unread: 0 };

/** Is there news in this bot the reader has not opened the bot to see? A bot
 *  never opened has nothing to catch up on (unread.ts's rule: `seen == null`
 *  → 0), so a fresh install does not light a chip over every seeded bot.
 *  Compared at the newest MESSAGE row (`news_seq`), not the raw `seq`: the
 *  page's unreadOf skips system and delivery rows, and until 2026-10-22 this
 *  did not — an idle-sleep note ("Browser closed after 20 minutes idle")
 *  appended after you had read everything lit the dot green over a bot whose
 *  list row showed nothing new, and nothing short of reopening it cleared it. */
export function botHasUnread(b: Pick<BotPulseRow, "seq" | "seen_seq" | "news_seq">): boolean {
  const newest = typeof b.news_seq === "number" ? b.news_seq : b.seq || 0;
  return typeof b.seen_seq === "number" && newest > b.seen_seq;
}

/** Hidden bots are off every list (the Bots page, the Home widget), so they are
 *  off this one too: a dot about a bot the reader cannot see would point at
 *  nothing. */
export function botsPulse(rows: BotPulseRow[]): BotsPulse {
  const p: BotsPulse = { ...EMPTY_BOTS_PULSE };
  for (const b of rows) {
    if (b.hidden) continue;
    if (b.status === "waiting") p.waiting += 1;
    else if (b.status === "running") p.running += 1;
    else if (b.status === "error") p.error += 1;
    if (botHasUnread(b)) p.unread += 1;
  }
  return p;
}

export function sameBotsPulse(a: BotsPulse, b: BotsPulse): boolean {
  return a.waiting === b.waiting && a.running === b.running && a.error === b.error && a.unread === b.unread;
}

/** "1 waiting" — the red readout. The word is the bot status's own
 *  (`BotStatus` "waiting", statusLabel "waiting for you"): one name for one
 *  state on the row, in the rail and on the page, the rule the Tasks row's
 *  "blocked" follows. No singular/plural fork, for the same reason. */
export function botsWaitingLabel(n: number): string {
  return `${n} waiting`;
}

/** "2 running" — the yellow readout, worded exactly like the Tasks row's. */
export function botsRunningLabel(n: number): string {
  return `${n} running`;
}

/** The collapsed dot's tooltip and the expanded words' — the sidebar's ONE
 *  sentence about the Bots page. Waiting first: it is the only part that asks
 *  the reader to act. Errors and unread bots are said here and nowhere else. */
export function botsPulseTitle(p: BotsPulse): string {
  const parts: string[] = [];
  if (p.waiting > 0) parts.push(botsWaitingLabel(p.waiting));
  if (p.running > 0) parts.push(botsRunningLabel(p.running));
  if (p.error > 0) parts.push(`${p.error} error`);
  if (p.unread > 0) parts.push(`${p.unread} unread`);
  return parts.join(" · ");
}

// ---- the poll ---------------------------------------------------------------
//
// Shaped like shell/tasksPulse, for the same reason:
//
// ONE POLL, TWO READERS. The Bots page already polls `/api/bots` every 1.5–3 s
// while it is mounted (apps/bots/state/store.ts `loop`), and those rows carry
// everything this module needs. So while the page is up it PUBLISHES each
// answer here (`publishBots`) and this module does not call the server at all;
// with the page gone, the sidebar asks `GET /api/bots/pulse` itself — a few
// bytes per bot, no events, no usage, no side effects — every 30 s, or 10 s
// while a bot is running. Two readers, one source, no beat where they disagree.
//
// WHAT IS NOT HERE: the route. "The reader is on /bots" is the sidebar's fact
// (it suppresses the dot there, as it does for Tasks); this store only counts.

const ACTIVE_MS = 10_000;
const IDLE_MS = 30_000;

let pulse: BotsPulse = EMPTY_BOTS_PULSE;
let timer: number | null = null;
let inFlight = false;
/** Bumped by every publish; a self-poll that departed before a fresher
 *  publish landed must lose, not overwrite (the tasksPulse rule). */
let generation = 0;
/** Owners feeding this store from their OWN poll (the Bots page). While there
 *  is one, this module never polls. */
let feeders = 0;
const listeners = new Set<(p: BotsPulse) => void>();

function recompute(rows: BotPulseRow[]) {
  const next = botsPulse(rows);
  if (sameBotsPulse(pulse, next)) return;
  pulse = next;
  for (const l of listeners) l(next);
}

/** Bounded: a request that never settles would leave `inFlight` true and the
 *  timer unarmed for the life of the document — stale counts until a reload
 *  (CodeRabbit, #1796). The owner answers in milliseconds; a forwarded call
 *  to a wedged owner is what the bound is for. */
const REQUEST_MS = 15_000;

export function getBotsPulse(): Promise<{ bots: BotPulseRow[] }> {
  return getJson<{ bots: BotPulseRow[] }>("/api/bots/pulse", { signal: AbortSignal.timeout(REQUEST_MS) });
}

async function poll() {
  if (inFlight) return;
  inFlight = true;
  const departed = generation;
  try {
    const r = await getBotsPulse();
    if (feeders === 0 && generation === departed) publishBots(r.bots ?? []);
  } catch {
    // Bots off, no owner (503), or a restart mid-poll: not news. The row keeps
    // its last answer rather than blanking a dot over one lost request; the
    // Bots page explains an unreachable worker itself.
  } finally {
    inFlight = false;
    schedule();
  }
}

/** Arm the next self-poll — or, with a feeder up or nobody reading, do not. */
function schedule() {
  if (timer !== null) window.clearTimeout(timer);
  timer = null;
  if (listeners.size === 0 || feeders > 0) return;
  timer = window.setTimeout(poll, pulse.running > 0 ? ACTIVE_MS : IDLE_MS);
}

/** An answer landed — the page's own or this module's. */
export function publishBots(rows: BotPulseRow[]) {
  generation += 1;
  recompute(rows);
  schedule();
}

/** "I poll `/api/bots` myself; take my answers and make no calls of your own."
 *  Returns the release. The hook below holds one for the Bots page's mount;
 *  this is the hook's body as a plain function so a test can hold and release
 *  one without React. */
export function feedBots(): () => void {
  feeders++;
  schedule();
  return () => {
    feeders--;
    // The page just left. Its last rows stand, but the next change would wait
    // out a whole idle tick (30 s) before the rail heard of it — ask now
    // instead, if anyone is still reading. poll() re-arms the timer itself.
    if (feeders === 0 && listeners.size > 0) void poll();
    else schedule();
  };
}

/** Held by the Bots page for exactly as long as it is mounted. */
export function useBotsFeeder() {
  useEffect(() => feedBots(), []);
}

/** Test seam: how many owners are feeding the store right now. */
export function botsPulseFeeders(): number {
  return feeders;
}

/** Subscribe to the summary. Polling starts with the first reader and stops
 *  with the last — nothing polls for a sidebar nobody has mounted. */
export function useBotsPulse(): BotsPulse {
  const [current, setCurrent] = useState<BotsPulse>(pulse);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(pulse);
    if (feeders === 0) void poll();
    else schedule();
    return () => {
      listeners.delete(setCurrent);
      schedule();
    };
  }, []);
  return current;
}

/** Test seam: forget every answer and owner. */
export function resetBotsPulseForTests() {
  pulse = EMPTY_BOTS_PULSE;
  generation = 0;
  feeders = 0;
  inFlight = false;
  if (timer !== null && typeof window !== "undefined") window.clearTimeout(timer);
  timer = null;
  listeners.clear();
}
