// THE MESSAGES THIS CHAT IS STILL WAITING TO SEND — drawn from the SERVER, in
// the transcript, in the order they were typed (the project queue, prefs
// `queue.enabled`).
//
// WHAT THIS REPLACES, and why it is a different shape. Until 2026-09-12 a queued
// send was a CHIP: a card the chat minted at admission time, holding its own copy
// of the words, living in client state for as long as the entry stayed pending.
// Two things were wrong with it and both were structural rather than cosmetic:
//
//   1. A RELOAD SHOWED A DIFFERENT CONVERSATION. The chips were client state, so
//      a refresh — or a nav away and back, or the very session adoption this
//      feature causes — dropped every one of them while the entries were still
//      sitting in the line. The reader's messages were safe on the server and
//      invisible on screen, which is the worst of the two possible failures.
//   2. IT WAS A SECOND KIND OF THING IN A TRANSCRIPT. A conversation is a column
//      of bubbles; a card with a ring and two buttons parked under the last one
//      read as chrome about the chat rather than as part of it.
//
// So the rule is now: EVERYTHING IS WRITTEN TO THE SERVER IN THE ORDER TYPED, AND
// THE CHAT DRAWS WHAT THE SERVER HOLDS. A waiting message is a user bubble with a
// dashed border at its place in the transcript, and one muted line under it. The
// optimistic copy drawn the instant a send is admitted is THE SAME ROW, minted
// early from the admission's answer and replaced by the server's own on the next
// poll — same id, so it is never two rows.
//
// TWO ADDRESSES, ONE PICTURE. A chat WITH a session reads the poll's
// session-filtered list (`schedPendingHere` → `useSchedule.waitingHere`); a chat
// with NO session yet — its first message was queued, so nothing has run and
// there is no session to filter by — reads the unfiltered pending rows through
// its leader entry (`schedFollowing`). Both are server facts, and that is what
// makes the reload parity above true rather than aspirational.
import { useEffect, useMemo, useState } from "react";
import type { QueueFacts } from "@platform/lib/queue";
import type { SchedEntry } from "./scheduled";

/**
 * WHAT AN ADMISSION ANSWERED, kept only until a poll says the same thing.
 *
 * The seed exists for one window: between the server creating the entry and the
 * next schedule tick listing it. Without it the bubble a reader just pressed
 * Enter on would not appear for up to fifteen seconds, which reads as a message
 * that went nowhere — the single most alarming thing a chat can do.
 *
 * It carries the WORDS because the entry the server stores holds the COMPOSED
 * message (attachment markers and all) and the reader typed a line. Where both
 * exist the typed line wins, so the row does not silently change its text under
 * the reader when the poll lands.
 */
export interface WaitingSeed {
  entryId: string;
  /** The typed line. EMPTY IS A REAL VALUE: a wordless send (pictures or notes
   *  alone) has no line of its own, and the row then falls back to whatever the
   *  server stored. */
  text: string;
  /** ISO, as the admission answered it — always "now" for a chat send, which is
   *  what makes its state word `queued` rather than `scheduled`. */
  due: string;
}

/** One waiting message, ready to draw. */
export interface WaitingRowData {
  /** The scheduler entry — this row's identity, its liveness, and the name
   *  `delete` spends. */
  entryId: string;
  /** What the bubble says. "" draws no bubble: a wordless send's real bubble is
   *  markers only the controller can compose. */
  text: string;
  /** ISO. The `scheduled` state word's second half is built from it. */
  due: string;
  /** Its state word — `queued` once it is due, `scheduled` before. */
  word: WaitingWord;
  /** True while this row is the admission's own copy and no poll has listed the
   *  entry yet. Nothing is drawn differently for it; it is here so a test can
   *  assert the swap happened, and so the reconciliation can count laps. */
  optimistic: boolean;
}

export type WaitingWord = "queued" | "scheduled";

/** The words under a waiting bubble, in the order they are read. */
export const WAITING_DELETE = "delete";

/**
 * THE STATE WORD: what is true of this message right now.
 *
 * `queued` — its time has come and it is in a line. `scheduled` — its time has
 * not come, so nothing is holding it up and there is nothing to be behind. The
 * split is the DUE STAMP and nothing else, which is why one rule serves both
 * kinds of entry: a chat send is admitted with `due = now` and is therefore
 * always `queued`, and a calendar entry crosses from one word to the other at
 * its own due time without anything about it changing.
 */
export function waitingWord(due: string, now: number): WaitingWord {
  const at = Date.parse(String(due || ""));
  if (!Number.isFinite(at)) return "queued";
  return at > now ? "scheduled" : "queued";
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/**
 * "Sat 13 Sep, 05:12" — when a `scheduled` row is going to run.
 *
 * SPELLED OUT RATHER THAN `toLocaleString`d, and that is a decision: the row is
 * one line in a transcript, so the format has to be short, has to be the same
 * length every day of the week, and has to be the same string in a test as on
 * screen. A locale formatter gives none of the three — the same instant is "Sat,
 * Sep 13" in one locale and "13/09/2026" in another, and the row's measure is
 * budgeted for neither.
 *
 * ABSOLUTE, NOT RELATIVE. "in 3 days" is the wrong register for a thing the
 * reader is being asked to accept or delete: they are deciding whether they still
 * want a message to go out at a particular time, and the time is the fact.
 * Unparseable answers "" and the caller then prints the word alone, which is the
 * honest reading of a stamp nobody can make sense of.
 */
export function waitingWhen(due: string, now: Date = new Date()): string {
  const d = new Date(String(due || ""));
  if (Number.isNaN(d.getTime())) return "";
  void now;
  return (
    `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}, ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  );
}

/**
 * The one line under a waiting bubble, as its pieces — `queued · behind TASK-038`
 * or `scheduled · Sat 13 Sep, 05:12`, with `delete` after either.
 *
 * THE SECOND PIECE IS NOT THE SAME KIND OF FACT IN THE TWO CASES, which is why
 * this is one function and not two: before the due time the interesting fact is
 * WHEN, after it the interesting fact is WHAT IS IN THE WAY — and once the folder
 * is free there is nothing in the way, so the line is the word alone. Every
 * branch is a true sentence rather than a padded one.
 */
export function waitingLine(
  row: Pick<WaitingRowData, "word" | "due">,
  behind: string,
  now: Date = new Date(),
): string[] {
  if (row.word === "scheduled") {
    const when = waitingWhen(row.due, now);
    return when ? ["scheduled", when] : ["scheduled"];
  }
  return behind ? ["queued", behind] : ["queued"];
}

/**
 * THE MERGE: the server's rows, plus the seeds it has not listed yet.
 *
 * ONE ROW PER ENTRY ID, and the server wins. A seed whose id the server has
 * published is dropped outright — that is the swap, and it is what stops the
 * reader seeing their own message twice for a poll interval. Its WORDS survive
 * the swap (see `WaitingSeed.text`), so nothing visible changes at the moment it
 * happens.
 *
 * ORDER IS THE SERVER'S, THEN THE SEEDS. The server's list is already sorted by
 * due (`schedPendingHere` / `schedFollowing`), which for a chat's own sends is
 * the order they were typed; a seed the server has not listed yet is by
 * construction the newest thing in the conversation, so it goes last.
 *
 * `dropped` is the ids a `delete` has taken back. They leave on the press rather
 * than on the next poll — a row that stayed for fifteen seconds after a
 * successful delete reads as a button that did nothing — and they keep filtering
 * the SERVER's list too, because that list is a photograph taken up to a lap
 * before the press.
 */
export function waitingRows(
  server: readonly SchedEntry[],
  seeds: readonly WaitingSeed[],
  dropped: ReadonlySet<string>,
  now: number = Date.now(),
): WaitingRowData[] {
  const out: WaitingRowData[] = [];
  const seen = new Set<string>();
  const words = new Map(seeds.map((s) => [s.entryId, s.text] as const));
  for (const entry of server) {
    const id = String(entry.id || "");
    if (!id || seen.has(id) || dropped.has(id)) continue;
    seen.add(id);
    const due = String(entry.due || "");
    out.push({
      entryId: id,
      text: words.get(id) ?? String(entry.message || ""),
      due,
      word: waitingWord(due, now),
      optimistic: false,
    });
  }
  for (const seed of seeds) {
    const id = seed.entryId;
    if (!id || seen.has(id) || dropped.has(id)) continue;
    seen.add(id);
    out.push({
      entryId: id,
      text: seed.text,
      due: seed.due,
      word: waitingWord(seed.due, now),
      optimistic: true,
    });
  }
  return out;
}

// ── WHEN A SEED STOPS BEING WORTH DRAWING ────────────────────────────────────
//
// A seed is a claim about an entry the server has not confirmed. It is retired
// the moment the server DOES list it (the merge above drops it), and that covers
// every ordinary case — but not the one where the folder frees a second after the
// admission and the entry is dispatched before any poll ever lists it. Then the
// id is never seen pending, and a seed held for the life of the page would be a
// message claiming to be waiting while its reply streams in above it.
//
// SO AN ID IS UNSEEN UNTIL A POLL HAS ONCE CONTAINED IT, and only from then on
// does its absence mean anything. Counted in POLL ANSWERS, not wall-clock
// seconds: a hidden tab has its timers throttled to about one lap a minute, so
// thirty seconds can pass with zero answers from the server and a clock-based
// bound then expires a row on evidence nobody gathered.

/** How many poll answers may miss a seed before its row comes down. Two laps is
 *  long enough to cover a tick that was already in flight when the send was
 *  admitted, which is the race this whole section is about. */
export const WAITING_UNSEEN_POLLS = 2;

export interface SeedWatch {
  /** Entry ids a poll has confirmed pending at least once. */
  seen: ReadonlySet<string>;
  /** Per still-unseen id, how many poll answers have now missed it. */
  missed: ReadonlyMap<string, number>;
  /** The pending set those counts were taken against — the poller publishes a
   *  fresh Set on every successful tick, so a change of identity IS a lap, and
   *  a re-render for some other reason cannot charge one poll twice. */
  poll: ReadonlySet<string> | null;
}

export const EMPTY_SEED_WATCH: SeedWatch = {
  seen: new Set<string>(),
  missed: new Map<string, number>(),
  poll: null,
};

export interface SeedLiveness {
  live: WaitingSeed[];
  /** The memory to carry to the next poll. Returned unchanged (the same object)
   *  when nothing moved, so a caller holding it in state does not re-render for
   *  a no-op. */
  watch: SeedWatch;
}

/**
 * Which seeds still deserve a row.
 *
 *   * `pendingIds === null` — nothing has polled yet: every seed stays.
 *   * in the set — waiting, and now SEEN: its absence from here on is real.
 *   * not in the set, never seen, fewer than `WAITING_UNSEEN_POLLS` answers have
 *     missed it — the poll is simply older than the send. Keep it.
 *   * not in the set, and either seen before or missed by that many answers —
 *     gone.
 *
 * IDEMPOTENT FOR ONE POLL: called again with the same `pendingIds` and the watch
 * the first call returned, it answers the same thing. That is what lets the
 * render compute `live` and the effect store `watch` without the two ever
 * disagreeing about a row.
 */
export function reconcileSeeds(
  seeds: readonly WaitingSeed[],
  pendingIds: ReadonlySet<string> | null,
  watch: SeedWatch,
): SeedLiveness {
  if (!pendingIds || !seeds.length) return { live: seeds.slice(), watch };
  const fresh = pendingIds !== watch.poll;
  const seen = new Set(watch.seen);
  const missed = new Map(watch.missed);
  const live: WaitingSeed[] = [];
  for (const seed of seeds) {
    const id = seed.entryId;
    if (pendingIds.has(id)) {
      seen.add(id);
      missed.delete(id);
      live.push(seed);
      continue;
    }
    if (seen.has(id)) continue;
    const n = (missed.get(id) ?? 0) + (fresh ? 1 : 0);
    missed.set(id, n);
    if (n < WAITING_UNSEEN_POLLS) live.push(seed);
  }
  // …and the ids of seeds this chat no longer holds go with them: the watch is a
  // memory of the rows on screen, not a log of everything ever queued.
  const held = new Set(seeds.map((s) => s.entryId));
  for (const id of [...seen]) if (!held.has(id)) seen.delete(id);
  for (const id of [...missed.keys()]) if (!held.has(id)) missed.delete(id);
  const same =
    watch.poll === pendingIds && sameSet(seen, watch.seen) && sameMap(missed, watch.missed);
  return { live, watch: same ? watch : { seen, missed, poll: pendingIds } };
}

function sameSet(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

function sameMap(a: ReadonlyMap<string, number>, b: ReadonlyMap<string, number>): boolean {
  if (a.size !== b.size) return false;
  for (const [k, v] of a) if (b.get(k) !== v) return false;
  return true;
}

/**
 * The same rule, as the chat holds it: the watch in state, grown by an effect.
 *
 * GROWN IN AN EFFECT AND NOT DURING THE RENDER, because a render that also writes
 * the memory it read is a render whose answer depends on how many times React ran
 * it. The render's own call already charges the current poll (`fresh`), so the
 * two agree about every row before the effect has stored anything.
 */
export function useLiveSeeds(
  seeds: readonly WaitingSeed[],
  pendingIds: ReadonlySet<string> | null,
): WaitingSeed[] {
  const [watch, setWatch] = useState<SeedWatch>(EMPTY_SEED_WATCH);
  useEffect(() => {
    setWatch((cur) => reconcileSeeds(seeds, pendingIds, cur).watch);
  }, [seeds, pendingIds]);
  return useMemo(
    () => reconcileSeeds(seeds, pendingIds, watch).live,
    [seeds, pendingIds, watch],
  );
}

// ── THE ROWS A DELETE TOOK BACK ──────────────────────────────────────────────
//
// A delete drops its row on the SERVER'S ANSWER rather than on the next poll.
// That removal has a second effect nobody asked for: the poll's own list is a
// photograph taken up to a lap before the press, and it still carries the entry —
// so without a memory the row would POP BACK for a poll interval, over the very
// message the reader had just deleted.
//
// So a deleted id is remembered and keeps filtering the merge. Nothing expires on
// a clock: a page that is not polling keeps the dismissal, which is the safe
// direction (a row that is not drawn, over an entry that is not there).

export const NO_DROPPED: ReadonlySet<string> = new Set<string>();

/**
 * The dropped ids still worth remembering, given the last poll's pending set.
 *
 * `null` (nobody has polled) keeps every id. Otherwise an id the poll no longer
 * lists is forgotten — the server agrees the entry is gone, so nothing can draw
 * it and the filter has nothing left to do. Returns the SAME SET when nothing
 * moved, so a caller holding it in state does not re-render four times a minute
 * for a memory that did not change.
 */
export function pruneDropped(
  dropped: ReadonlySet<string>,
  pendingIds: ReadonlySet<string> | null,
): ReadonlySet<string> {
  if (!pendingIds || !dropped.size) return dropped;
  let lost = false;
  const next = new Set<string>();
  for (const id of dropped) {
    if (pendingIds.has(id)) next.add(id);
    else lost = true;
  }
  return lost ? next : dropped;
}

/**
 * THE QUEUE FACTS EVERY ROW IN THIS CHAT SHARES.
 *
 * "behind TASK-038" is a fact about a FOLDER and the TASK holding it, not about
 * one message — so the chat asks it once and every waiting row says the same
 * thing, which is also what a reader would expect from three bubbles sitting in
 * one line. The answer comes from the `/api/tasks` row for this conversation
 * (`useSchedule.rec`), which is the server's own and therefore survives a reload;
 * the admission's answer is the fallback for the window before that row has been
 * read, and for a chat with no session, which has no row at all.
 */
export function waitingFacts(
  row: QueueFacts | null | undefined,
  fallback: QueueFacts | null | undefined,
): QueueFacts {
  const from = row && (row.queue_ahead || row.queue_position || row.queue_priority)
    ? row
    : (fallback ?? {});
  return {
    status: "queued",
    queue_position: from.queue_position ?? 0,
    queue_ahead: from.queue_ahead ?? "",
    queue_ahead_title: from.queue_ahead_title ?? "",
    queue_ahead_session: from.queue_ahead_session ?? "",
    queue_ahead_target: from.queue_ahead_target ?? "",
    queue_priority: from.queue_priority === true,
  };
}
