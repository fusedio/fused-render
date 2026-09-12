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
// ONE ADDRESS, ONE PICTURE. Which messages are this conversation's is asked of
// the WHOLE listing, by session and by `follow_of` together (`waitingFor` →
// `useSchedule.waitingHere`) — because a chat can be both at once: a new chat
// whose first message queued has no session to filter by, and the instant its
// leader runs it has one while its followers still name nothing. Two lists
// swapped on that fact dropped every row in the swap; one rule does not.
import { useEffect, useMemo, useState } from "react";
import type { QueueFacts } from "@platform/lib/queue";
import { schedIsRepeat, schedIsWaiting, schedStopTarget } from "./scheduled";
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
  /** Its state word — `queued` once it is due, `scheduled` before, `starting`
   *  for the second the scheduler has claimed it in. */
  word: WaitingWord;
  /** A RECURRING occurrence. `delete` on one of these is not a delete: the
   *  template arms the next run the moment this one is skipped, so the row says
   *  `skip this run` and carries a second, armed action for the repeat itself. */
  repeat: boolean;
  /** What "stop repeating" posts — the TEMPLATE (`schedStopTarget`), never this
   *  occurrence, which would move the repeat rather than stop it. "" for an
   *  ordinary message. */
  stopId: string;
  /** True while this row is the admission's own copy and no poll has listed the
   *  entry yet. Nothing is drawn differently for it; it is here so a test can
   *  assert the swap happened, and so the reconciliation can count laps. */
  optimistic: boolean;
}

export type WaitingWord = "queued" | "scheduled" | "starting";

/** The words under a waiting bubble, in the order they are read. */
export const WAITING_DELETE = "delete";
/** …AND THE SAME PLACE'S VERB FOR A REPEAT'S OCCURRENCE, which is a different
 *  act: the template arms the next run as this one is skipped, so calling it
 *  `delete` promised the reader something it does not do (Bugbot PR #1124). */
export const WAITING_SKIP = "skip this run";
/** The other half of that row — the thing `delete` used to be mistaken for, and
 *  the only way to stop a repeating message from inside the chat now that the
 *  block draws nothing under the flag. ARMED, because it spends every future
 *  run: the same two presses the banner's stop has always asked for. */
export const WAITING_STOP = "stop repeating";
export const WAITING_STOP_ARMED = "stop every future run";

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
  // CLAIMED: nothing is in front of it any more and its time is not a fact about
  // the future, so the word is the whole line.
  if (row.word === "starting") return ["starting"];
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
 * due (`waitingFor`), which for a chat's own sends is the order they were typed; a seed the server has not listed yet is by
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
      // THE CLAIM OUTRANKS THE CLOCK. A `sending` entry is past its due stamp by
      // construction, so the due-time rule would call it `queued` — and it is
      // not in the line any more, it is being spawned.
      word: entry.state === "sending" ? "starting" : waitingWord(due, now),
      optimistic: false,
      repeat: schedIsRepeat(entry),
      stopId: schedIsRepeat(entry) ? schedStopTarget(entry) : "",
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
      // A SEED IS ALWAYS A CHAT SEND — the admission minted it out of a composer
      // — and nothing the composer sends repeats.
      repeat: false,
      stopId: "",
    });
  }
  return out;
}

/**
 * THE WAITING MESSAGES OF THIS CONVERSATION — one rule, both addresses, and the
 * group that survives the adoption.
 *
 * THE BUG THIS REPLACES (Bugbot PR #1124). There were two lists and the chat
 * swapped between them on one fact: with no session it read its leader's
 * followers, with a session it read the session-filtered pending list
 * (`schedPendingHere`). Adoption flips exactly that fact — the
 * leader runs, the chat opens the session its run created — and in that instant
 * BOTH lists miss the followers: the session filter because the server fills a
 * follower's `session_id` only when it CLAIMS it, and the leader list because
 * the leader id is a client memory that a reload (or the adoption's own clearing
 * rule) drops. So the reader's queued messages disappeared from the transcript
 * while sitting perfectly safely in the line — the exact failure the move to
 * server-drawn rows was made to end.
 *
 * SO THE QUESTION IS ASKED OF THE GROUP, NOT OF ONE FIELD. An entry belongs to
 * this conversation when it NAMES the session, or when it is connected — through
 * `follow_of`, in either direction and as many hops as it takes — to an entry
 * that does. The leader is the hinge: once it has run it is the only row in the
 * schedule carrying both the session id and the id its followers name, and that
 * is why this is asked of the WHOLE listing rather than the pending slice.
 * `leaderId` is still taken, for the chat whose leader has not run yet and which
 * therefore has no session for anything to name.
 *
 * ONLY WAITING ROWS COME BACK (`schedIsWaiting` — pending, or claimed), deduped
 * by id and in due order, which for a chat's own sends is the order they were
 * typed.
 */
export function waitingFor(
  entries: readonly SchedEntry[] | null | undefined,
  sessionId: string,
  leaderId: string,
): SchedEntry[] {
  const rows = (entries || []).filter((e): e is SchedEntry => !!e);
  const ours = new Set<string>();
  if (leaderId) ours.add(leaderId);
  if (sessionId) {
    for (const e of rows) {
      if (e.session_id === sessionId || e.claude_session_id === sessionId) {
        ours.add(String(e.id || ""));
      }
    }
  }
  // GROWN UNTIL IT STOPS GROWING, and in BOTH directions along `follow_of`: from
  // a leader down to its followers, and from a follower that has been claimed
  // (the server filled its session) back up to the leader and out to its
  // siblings. Bounded by the number of rows, because every lap that changes
  // nothing ends it and every lap that changes something adds at least one id.
  for (let lap = 0; lap <= rows.length; lap += 1) {
    let grew = false;
    for (const e of rows) {
      const id = String(e.id || "");
      const of = String(e.follow_of || "");
      if (!of || !id) continue;
      if (ours.has(id) && !ours.has(of)) {
        ours.add(of);
        grew = true;
      } else if (ours.has(of) && !ours.has(id)) {
        ours.add(id);
        grew = true;
      }
    }
    if (!grew) break;
  }
  const out: SchedEntry[] = [];
  const seen = new Set<string>();
  for (const e of rows) {
    const id = String(e.id || "");
    if (!id || seen.has(id) || !ours.has(id) || !schedIsWaiting(e)) continue;
    seen.add(id);
    out.push(e);
  }
  // ISO stamps with one offset spelling, so a string sort is a time sort.
  out.sort((a, b) => String(a.due || "").localeCompare(String(b.due || "")));
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
 * read.
 *
 * THE ROW WINS OUTRIGHT ONCE THERE IS ONE, empty fields and all (Bugbot
 * PR #1124). It used to win only when it carried a queue field, and that reads
 * the one answer that matters backwards: a row with nothing in front of it is
 * not a row that failed to say — it is the server saying THE FOLDER IS FREE NOW,
 * and falling back to the admission's minutes-old "behind TASK-038" left the
 * card naming a task that had long since finished, with a Run next button under
 * it that could do nothing.
 *
 * `claimedNext` IS THE ONE THING THAT OUTRANKS BOTH, and only until the next row
 * lands. Run next has just been accepted by the server; the row in hand was read
 * before the press, so believing it would take the reader's own press back off
 * the screen. It is a claim with a deadline, not a state: the caller drops it the
 * moment a fresher row arrives, and that row then decides — including when it
 * says `queue_priority: false` because the server refused after all.
 */
export function waitingFacts(
  row: QueueFacts | null | undefined,
  fallback: QueueFacts | null | undefined,
  claimedNext: boolean = false,
): QueueFacts {
  const from = row ?? fallback ?? {};
  return {
    status: "queued",
    queue_position: from.queue_position ?? 0,
    queue_ahead: from.queue_ahead ?? "",
    queue_ahead_title: from.queue_ahead_title ?? "",
    queue_ahead_session: from.queue_ahead_session ?? "",
    queue_ahead_target: from.queue_ahead_target ?? "",
    queue_ahead_key: from.queue_ahead_key ?? "",
    queue_priority: claimedNext || from.queue_priority === true,
  };
}

/**
 * THE NUMBER AT THE TOP OF A CHAT — "TASK-057" — from the freshest of the three
 * places that can know it.
 *
 * A QUEUED NEW CHAT HAS ONE FROM THE FIRST SECOND, and used to show none for up
 * to a poll interval (Akshil, 2026-09-12). The entry IS the task: the admission
 * that queued the message creates it and names it back in the same answer. But
 * the header read `/api/tasks` and nothing else, so a reader watched their own
 * conversation sit numberless while the id they would need to find it again on
 * the Tasks page was already in hand.
 *
 * THE ORDER IS BY FRESHNESS, not by preference:
 *
 *   1. `fromTasks` — `ui/Kebab.useTaskId`, a listing read keyed on this
 *      conversation (its session, or `pending:<leader>` before it has one). The
 *      most recent answer there is, and the one that survives a rekey. TAKEN
 *      ONLY WHEN IT IS SHAPED LIKE A TASK ID: that read FAILS OPEN to a
 *      truncated session hash, which is a fine label for the topbar to fall back
 *      to on its own (`ui/Topbar`) and a terrible one to rank ABOVE the row and
 *      the admission here, both of which hold the real number.
 *   2. `fromRow` — the schedule hook's own row for this chat (`useSchedule.rec`),
 *      which the pane is already paying for and which lands on its own cadence.
 *   3. `fromAdmit` — what the admission said. Minted before either listing
 *      existed and never stale in the way the others can be: a task id is
 *      allocated once and never reused, so this can only be right or absent.
 *
 * "" when none of them has answered, which is the landing and every chat that is
 * not a task.
 */
export function headerTaskId(
  fromTasks: string | null | undefined,
  fromRow: string | number | null | undefined,
  fromAdmit: string | null | undefined,
): string {
  const row = fromRow === null || fromRow === undefined ? "" : String(fromRow);
  return listedTaskId(fromTasks) || row || String(fromAdmit || "");
}

/** `TASK-057` and nothing else. The listing read hands back either a real task
 *  id or its own fallback (a session hash, or — before this was fixed — the word
 *  `pending:`), and only the first of those is an answer to "what number is this
 *  chat" (Akshil, 2026-09-12). */
function listedTaskId(value: string | null | undefined): string {
  const v = String(value || "").trim();
  return /^TASK-/.test(v) ? v : "";
}
