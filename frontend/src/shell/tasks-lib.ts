// The Tasks page's pure half — everything the List accordion and the Board
// decide, with no DOM and no React in it, so the rules are testable on their
// own (shell/tasks-lib.test.ts) and the components are left holding markup.
//
// The model:
//
//   PROJECT (a folder)
//   └─ TASK-002        one Claude session, one thread
//      ├─ MSG-003      newest first
//      ├─ MSG-002
//      └─ MSG-001
//
// The server hands us that shape already merged, already titled, already
// counted and already sorted (newest task first). Nothing here re-derives a
// status or re-titles a row: the one place those are decided is the server, and
// a client that guesses a second answer is a client that disagrees with itself
// on the next poll.
//
// ORDER HAS EXACTLY ONE EXCEPTION, and it is named: sortLane, applied by
// groupByColumn, which is the BOARD's alone. The List keeps the server's order
// untouched (filterTasks below filters and nothing else) because a flat list of
// every task has one honest question — what happened most recently — and the
// server already answers it with `last_active` descending.
//
// A LANE is a narrower question, and Upcoming's is the opposite one. A column of
// work that has not happened yet is read to find out what happens NEXT, and
// "most recently touched" is not that: a task edited an hour ago and due in
// October outranks the one firing in ten minutes (Akshil, 2026-08-17: "in
// upcoming the most recent tasks [close to current time] would be on top, in
// done and failed the recent runs will be on top"). So Upcoming runs SOONEST
// FIRST — ascending — and the settled lanes run most-recent-run first, which is
// the same instinct pointed at the past.
//
// This is a presentation of the same data, not a second opinion about it: no
// status is re-derived, no lane membership is re-decided (taskColumn still asks
// the server), and every key is a time the server itself sent.
import type { Task, TaskMessage } from "@platform/lib/api";
import { BOARD_COLUMNS, explorerUrl, isProjected, turnPhase } from "./schedule-lib";
import type { BoardColumn } from "./schedule-lib";

// How many messages the LISTING carries per task — the server's window, not a
// display cap: an expanded task draws every message it has (there is no Show more
// button since 2026-08-18), and this is how many arrive before the fetch does.
// The server sends exactly this many in `task.messages`; the constant is here
// so the cap and the "is there more?" test cannot drift apart.
export const PREVIEW_MESSAGES = 3;

// ---- small string helpers ----------------------------------------------------

/** The first non-empty line of a body. A prompt is routinely a paragraph — a
 * pasted URL, a blank line, then the instruction — and a row prints its opening
 * line while the whole thing stays the tooltip. */
export function firstLine(text: string): string {
  return text.split("\n").map((s) => s.trim()).find(Boolean) ?? text.trim();
}

/** A path as a person reads it: $HOME collapsed to "~". */
export function tildePath(path: string, home: string): string {
  if (!home) return path;
  const h = home.replace(/[\\/]+$/, "");
  if (path === h) return "~";
  if (path.startsWith(h + "/")) return "~/" + path.slice(h.length + 1);
  return path;
}

/** The last segment of a path — what the folder chip prints. "/" stays "/". */
export function basename(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

// ---- when a message happened -------------------------------------------------
// The spec's own wording: "09:00 today", "09:00 yesterday", "09:00 Monday".
// Deliberately NOT toLocaleString: the thread is read as a column of times, and
// a column only reads as one when every row is the same width — a locale that
// swaps between "9:00 AM" and "09:00" costs that alignment, and the day word is
// what carries the meaning anyway. 24h, zero-padded, then the day.

const DAY_NAMES = [
  "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
];
const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

const pad2 = (n: number) => String(n).padStart(2, "0");

/** Whole days between two instants, by LOCAL calendar day — not by elapsed
 * hours. 23:59 and 00:01 are a day apart to a reader and two minutes apart to a
 * clock, and the reader is right. */
function dayDelta(then: Date, now: Date): number {
  const a = new Date(then.getFullYear(), then.getMonth(), then.getDate()).getTime();
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.round((a - b) / 86400000);
}

/** The day half of a message stamp: today / yesterday / tomorrow, a weekday
 * name inside the surrounding week, an absolute date beyond it. */
export function dayLabel(at: Date, now: Date): string {
  const delta = dayDelta(at, now);
  if (delta === 0) return "today";
  if (delta === -1) return "yesterday";
  if (delta === 1) return "tomorrow";
  // Inside the week either side, the weekday alone is unambiguous and shorter.
  if (delta > -7 && delta < 7) return DAY_NAMES[at.getDay()];
  const date = `${at.getDate()} ${MONTH_NAMES[at.getMonth()]}`;
  return at.getFullYear() === now.getFullYear() ? date : `${date} ${at.getFullYear()}`;
}

/** "09:00 today". `at` is epoch SECONDS (the API's unit), not ms. */
export function messageTime(at: number, now: number = Date.now()): string {
  if (!at) return "";
  const d = new Date(at * 1000);
  if (Number.isNaN(d.getTime())) return "";
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())} ${dayLabel(d, new Date(now))}`;
}

/** The absolute stamp, for the row's tooltip — the relative label above is for
 * scanning, and "Monday" is not an answer to "which Monday?". */
export function messageStamp(at: number): string {
  if (!at) return "";
  const d = new Date(at * 1000);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

/** Under this, the two times are the same fact told twice — a send is never
 * instantaneous and a second of drift is not news. */
export const RAN_SKEW_SECONDS = 60;

/**
 * Whether this message RAN AT A DIFFERENT TIME than it was due: `at` is what was
 * asked for and never moves, `ran_at` is when the turn actually started, and they
 * part company two ways —
 *
 *   * late, when the app was shut at the due minute and the run was caught up,
 *   * early, when someone dragged the task into In Progress and ran it now.
 *
 * The early case is the whole reason run-now leaves `due` alone (§2 of this
 * round's fixes): the schedule keeps saying 09:00 and the row says it ran at
 * 07:12, which is the truth. Rewriting `due` to the moment of the drag would
 * have made the row read as if 07:12 had always been the plan.
 *
 * This used to FORMAT that second time as well ("ran 07:12 today", drawn beside
 * the row's own stamp), and the label is gone: 2026-08-17, Akshil, "I don't think
 * I need this as well, the RAND Today stuff" — a message row carrying two absolute
 * times was the same crowding the whole evening was spent trimming. The
 * distinction is not gone with it: it decides the tooltip below, which is where
 * both instants are still spelled out in full.
 */
export function ranOffSchedule(m: TaskMessage): boolean {
  // No `now` in the signature, and that is the shape of the answer rather than an
  // omission: this compares two stamps the server wrote against each other, so the
  // current time cannot change it. The clock only mattered while the helper
  // FORMATTED the label — "ran 07:12 today" has to know what today is.
  if (!m.at || !m.ran_at) return false;
  return Math.abs(m.ran_at - m.at) >= RAN_SKEW_SECONDS;
}

/** The time cell's tooltip, and since the label above went, the ONLY place a late
 * or early run is spelled out: the absolute stamp it was due at, plus the one it
 * actually ran at whenever the two are different facts. */
export function messageWhenTitle(m: TaskMessage): string {
  const due = messageStamp(m.at);
  if (!ranOffSchedule(m)) return due;
  return `Scheduled for ${due} · ran ${messageStamp(m.ran_at)}`;
}

// ---- "30m ago", "in 2h" -------------------------------------------------------

/** Under a minute in the PAST reads as this. A run that landed forty seconds ago
 * is news about now, and "0m ago" is not a thing anyone says. */
export const JUST_NOW = "just now";
/** ...and under a minute in the FUTURE cannot borrow that word: on an Upcoming row
 * "just now" would say the run has already happened. `<1m` keeps the direction. */
export const IMMINENT = "in <1m";

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
/** Calendar months are not equal, and a row that says "1mo ago" is not making a
 * claim that survives being exact — this is the same 30-day month
 * platform/lib/format.timeAgo has always used, so the two agree. */
const MONTH = 30 * DAY;
const YEAR = 12 * MONTH;

/**
 * How long ago, or how long until — ONE unit, always (Akshil, 2026-08-17: the row
 * ended in `15:53 29 Jul 🗀 ppt_builder` and "both the folder and the time with the
 * date, they are like too much for me to handle"; the reference they sent reads
 * `30m ago`, `31m ago`, `1mo ago`).
 *
 * Never two units: "1mo 3d ago" is a readout, and this is a glance. The exact
 * instant is never dropped, only moved — every caller puts it in the element's
 * `title`, so hovering still answers "when exactly?".
 *
 * BOTH DIRECTIONS, which the reference has no case for and this page is full of:
 * most of Upcoming is in the future, and "5m ago" on a run that has not happened
 * is simply false. So a future instant reads `in 5m`.
 *
 * FLOOR, not round, at every boundary — 89 minutes is `1h ago`, not `1h ago`
 * rounded up from something it never was, and 23h59m is `23h ago` rather than
 * jumping a day early. It is the rule platform/lib/format.timeAgo already uses,
 * and the one that cannot ever name a unit the instant has not reached.
 *
 * `now` is a PARAMETER and the clock is never read in here, which is what makes
 * every boundary above testable. That is also why neither existing helper could be
 * reused: format.timeAgo reads `Date.now()` itself and has no future direction at
 * all, and schedule-lib.relativeDue takes an ISO string, rounds rather than floors
 * and stops at days (no `mo`, no `y`). Both are still right for their own callers.
 */
export function relativeWhen(at: number, now: number = Date.now()): string {
  if (!at) return "";
  const secs = at - now / 1000;
  const ahead = secs > 0;
  const s = Math.abs(secs);
  if (s < MINUTE) return ahead ? IMMINENT : JUST_NOW;
  const say = (n: number, unit: string) => (ahead ? `in ${n}${unit}` : `${n}${unit} ago`);
  if (s < HOUR) return say(Math.floor(s / MINUTE), "m");
  if (s < DAY) return say(Math.floor(s / HOUR), "h");
  if (s < MONTH) return say(Math.floor(s / DAY), "d");
  if (s < YEAR) return say(Math.floor(s / MONTH), "mo");
  return say(Math.floor(s / YEAR), "y");
}

// ---- a message's own state ---------------------------------------------------
// The task carries a status decided server-side; a MESSAGE does not, and the
// ring beside each sub-row is the only place the thread says how a given run
// went. Two facts collapse into it — `state` (did it go out) and `turn` (how
// the session then answered) — exactly as schedule-lib.stateTone collapses the
// scheduler's pair, because reporting a dead turn as a clean send is the one
// mistake this row must not make.

export interface MessageTone {
  column: BoardColumn;
  /** Paint the ring red rather than the column's hue: settled, but not well. */
  failed: boolean;
  /** What the ring's tooltip says. */
  label: string;
}

/**
 * WHY THERE ARE TWO `messageTone`s (this one, and schedule-lib's).
 *
 * They answer different questions about the same message and neither is a
 * superset of the other:
 *
 *   - This one returns a BOARD COLUMN plus a failed flag and an English label —
 *     four buckets, because that is how many lanes the Board has.
 *   - schedule-lib's returns one of six CALENDAR TONES, which are CSS classes
 *     (`--missed` is amber, `--error` is red, `--skipped` is grey), and it
 *     draws distinctions this one has already collapsed: a missed one-off is
 *     `missed` there and `done`+failed here, a cancelled message is `skipped`
 *     there and `archived`/"Cancelled" here.
 *
 * Deriving either from the other would therefore lose a distinction the losing
 * view actually paints. A merge is also blocked mechanically: schedule-lib is
 * the LOWER module (this file imports explorerUrl and BoardColumn from it), so
 * schedule-lib importing back would be a cycle.
 *
 * What is genuinely shared is the reading of `turn` — the half that caused the
 * bug — and that now lives in exactly one place, schedule-lib.turnPhase, which
 * both call. Change the meaning of `turn` there and both views move together.
 */
export function messageTone(m: TaskMessage): MessageTone {
  switch (m.state) {
    case "pending":
      return { column: "upcoming", failed: false, label: "Scheduled" };
    case "sending":
      return { column: "in_progress", failed: false, label: "Sending…" };
    case "cancelled":
      return { column: "archived", failed: false, label: "Cancelled" };
    // A SKIPPED occurrence is routine (the next run is already coming), so it
    // is filed away rather than flagged. A missed ONE-OFF is a fault: the run
    // the user asked for never happened, and it must not paint the green ring.
    case "skipped":
      return { column: "archived", failed: false, label: "Skipped" };
    case "missed":
      return m.template_id
        ? { column: "archived", failed: false, label: "Skipped" }
        : { column: "done", failed: true, label: "Missed" };
    case "error":
      return { column: "done", failed: true, label: "Failed" };
    case "sent":
    default:
      // `sent` only means the turn STARTED. How it ended is the second fact,
      // and turnPhase is the one place that fact is read (schedule-lib).
      switch (turnPhase(m.turn)) {
        case "unreported":
          // Not "Running…": nothing is watching it any more, and saying
          // otherwise is the frozen-progress-bar lie.
          return { column: "done", failed: true, label: "Stopped reporting" };
        case "running":
          return { column: "in_progress", failed: false, label: "Running…" };
        // "done", "idle", or whatever a newer server writes — the turn ended.
        default:
          return { column: "done", failed: false, label: "Ran" };
      }
  }
}

/**
 * ARCHIVING A TASK ARCHIVES ITS THREAD — the message tone a row actually wears.
 *
 * `messageTone` above answers "what happened to this message", which is a fact
 * about the message and nothing else. It was also, until 2026-08-18, the only
 * thing the thread rows asked, and that made archiving look like it had half
 * worked: the task card moved to Archive and the ten rows underneath it stayed
 * green, amber and red, still reading as live work. A task is a thread, so
 * filing the task files the thread — one gesture, one outcome, everywhere
 * (design-principles §1).
 *
 * Archived is a PLACE, not an event, so the cascade changes only where a row is
 * filed and never what it says happened: the label is left exactly as it was, so
 * an archived thread can still be read back run by run. The `failed` flag does
 * go — archiving is the "I have dealt with this" gesture, and a filed task that
 * still flies a red mark is asking to be dealt with again.
 *
 * THE ONE EXCEPTION IS A TURN THAT IS STILL RUNNING. Filing something does not
 * stop it, and a running turn is the one fact on this page that is about the
 * present rather than the past — it stops being true on its own, in a minute or
 * two, and until it does, saying otherwise is a lie the reader can watch. So a
 * running message keeps its own tone, and the task keeps reading as In Progress
 * over the archive record until the turn ends (the server's `_status` makes that
 * half of the promise — see fused_render/server/routers/tasks.py). The archive
 * is not lost either way: it is still recorded, and the moment the turn is over
 * the task and its whole thread fall back into Archive.
 */
export function threadTone(task: Task, m: TaskMessage): MessageTone {
  const tone = messageTone(m);
  if (taskColumn(task) !== "archived") return tone;
  if (tone.column === "in_progress") return tone;
  return { ...tone, column: "archived", failed: false };
}

/** Is any message in this thread mid-turn? The client's half of the running
 * exception above — `Task.live` is the server's, computed from the transcript's
 * own tail, and the two are asked in different places rather than merged: this
 * one is about the rows on screen, that one about the row's status.
 *
 * The per-message reading is `isMessageRunning` (bottom of this file), the same
 * function the calendar's chip asks, so "running" is one rule here and not
 * three views' worth of `=== "in_progress"`. */
export function threadRunning(messages: TaskMessage[]): boolean {
  return messages.some(isMessageRunning);
}

/**
 * The task's own column, narrowed. The server decides it (Task.status); this
 * only keeps a value a newer server invented off the board's floor. An
 * unreadable status lands in Done, not Archive: a task the client cannot read
 * still HAPPENED, and filing it away hides it behind a collapsed lane.
 *
 * Checked against BOARD_COLUMNS rather than a hand-written list of four words,
 * so the board and this function cannot disagree about how many statuses there
 * are — the fifth, `failed`, arrived a round after the first four and a
 * hardcoded list is how that lane would have silently swallowed itself into
 * Done. Read as a plain string on purpose: the union this file was compiled
 * against is a snapshot of what the server said LAST time.
 */
export function taskColumn(task: Task): BoardColumn {
  const s: string = task.status;
  const known = BOARD_COLUMNS.find((c) => c.key === s);
  return known ? known.key : "done";
}

// ---- unread ------------------------------------------------------------------
// Unread is per MESSAGE (§7) and clicking through is what clears it. The click
// also posts to the server, but the dot has to go NOW — the list polls on a
// 20s interval, and a dot that survives its own click reads as a failed click.
// So reads are held locally as well, and merged over whatever the poll returns
// until the server's own answer catches up.
//
// EVERY ENTRY HERE IS AN OVERRIDE OF A KNOWN-STALE VALUE, never a fact. That is
// one sentence and it decides the whole shape of this section:
//
//   * an entry is GATED on the server still disagreeing. A per-message mark only
//     discounts a message the poll still calls unread (isUnread, taskUnread), so
//     it retires itself the moment the server agrees, and the set never has to be
//     pruned against a list that moves under us.
//   * an entry can be TAKEN BACK. A write that is refused has to give the dot
//     back, or the optimism has quietly become an assertion about a write that
//     never happened (unmarkRead, unmarkAllRead).
//   * the whole-task sentinel, which cannot name a message and therefore cannot
//     be gated per message, is stamped with the observation it overrides instead
//     (markObservation) and expires when that observation is replaced.

/** The local read-set's key. Message ids are per TASK ("MSG-001" exists in
 * every thread), so the task key has to be part of it.
 *
 * NUL is the joiner because it is the one character neither half can
 * contain, so two different pairs cannot flatten onto one key the way they
 * can under a space. Written as the ESCAPE `\u0000`, never as a literal
 * control byte: one raw NUL makes the whole source file `data` rather than
 * text, which silently breaks grep, diff and every line-oriented tool over
 * it -- including the greps this repo's own tests are built out of. */
export function readKey(taskKey: string, messageId: string): string {
  return `${taskKey}\u0000${messageId}`;
}

export function markRead(read: Set<string>, taskKey: string, messageId: string): Set<string> {
  const next = new Set(read);
  next.add(readKey(taskKey, messageId));
  return next;
}

/** Take one message's local mark back — the write it stood in for was refused,
 * so the dot it hid has to come back. The mirror of markRead, and the half the
 * optimism was missing on both paths: an override with no way back is not
 * optimism, it is an assertion. */
export function unmarkRead(
  read: Set<string>,
  taskKey: string,
  messageId: string,
): Set<string> {
  const next = new Set(read);
  next.delete(readKey(taskKey, messageId));
  return next;
}

/**
 * The message-id slot's stand-in for "all of them", for the ONE thing a
 * per-message mark cannot cover: the messages OUTSIDE the loaded window, whose
 * ids this client has never seen and cannot enumerate.
 *
 * `*` cannot collide with a real entry: a message id is always `MSG-nnn`
 * (tasks_store.format_message_id), so no thread can produce this one.
 *
 * IT IS NOT A CLAIM THAT THE TASK IS READ FOR EVER, and that is the correction
 * here. It used to be exactly that — a bare `taskKey \0 *` that isUnread and
 * taskUnread both read as absolute, with nothing that ever removed it — so a
 * REFUSED write left the row looking read with its own Mark read button gone
 * (no retry), a server still reporting unread was ignored, and a message
 * arriving afterwards was invisible until the List remounted. The comment above
 * claimed the next poll restored truth; nothing in the poll could, because a
 * poll only replaces `task.unread`, and the sentinel outranked it.
 *
 * What the local set is actually for is hiding the 20-second gap between a
 * press and the server's own answer — a SHORT-LIVED OVERRIDE OF A KNOWN-STALE
 * VALUE, not a fact of its own. So the sentinel now carries the observation it
 * overrides (markObservation) and applies only while the server is still
 * quoting that same value; the first poll that disagrees is a poll about a
 * value nobody is overriding, and the server wins.
 */
export const ALL_MESSAGES = "*";

/**
 * The server observation a whole-task mark overrides: the count it printed, and
 * the ids it still calls unread.
 *
 * Both halves earn their place. The COUNT is what the row draws and what the
 * mark zeroes. The IDS are what make an ARRIVAL visible without a remount: a
 * message that lands after the press changes the set even in the case where it
 * happens to leave the count where it was (one marked read, one arrived).
 *
 * Deliberately not a timestamp and not a nonce. A stamp would expire on its own
 * schedule, whether or not anything had changed; this expires exactly when the
 * value it corrects is replaced, which is the only event that means anything.
 *
 * Read off the LISTING row only, never the thread Show more fetched. The poll is
 * what replaces these numbers, and expanding a row is not a new answer from the
 * server about them — stamping the fuller list would retire a mark that is still
 * perfectly true the moment the reader opens the thread they just cleared.
 */
export function markObservation(task: Task): string {
  const ids = (task.messages ?? [])
    .filter((m) => m.unread)
    .map((m) => m.message_id)
    .sort();
  return `${task.unread}\u0000${ids.join(",")}`;
}

/** The one key a whole-task mark occupies — the sentinel, stamped with the
 * observation it is only true of. NUL-joined for readKey's own reason. */
export function allReadKey(taskKey: string, observation: string): string {
  return readKey(taskKey, `${ALL_MESSAGES}\u0000${observation}`);
}

/**
 * Everything in this task is read, locally, as of THIS observation.
 *
 * Two things are written, and the split is the fix:
 *
 *   * a concrete id for every unread message we actually HOLD — the very
 *     entries a click on each of those rows would have written, so the dots go
 *     out through the mechanism that was already sound: each entry is gated on
 *     the server still calling that message unread, and a message we have never
 *     held has no entry, so it still draws its own dot when it arrives;
 *   * ONE observation-stamped sentinel, for the only part arithmetic cannot
 *     reach — the messages outside the window, which are why the row must not go
 *     on saying "86" after the press that cleared all 89.
 *
 * THE TWO HALVES ANSWER DIFFERENT QUESTIONS, and reading them off the same list
 * was the bug after this one:
 *
 *   * the observation is about WHAT THE SERVER LAST TOLD US, so it comes off the
 *     listing row alone (markObservation reads `task.messages` and nothing
 *     else) — stamping the fuller thread Show more fetched would retire a mark
 *     that is still perfectly true;
 *   * the concrete ids are about WHAT IS ON SCREEN AND MUST STOP SHOWING A DOT,
 *     so they must cover everything currently HELD — which after Show more is
 *     the whole thread (heldMessages), not the three the listing carried. Ids
 *     off the window only, with a sentinel that zeroes the count, is a row
 *     saying 0 above 86 lit dots.
 */
export function markAllRead(
  read: Set<string>,
  task: Task,
  held?: TaskMessage[],
): Set<string> {
  const next = new Set(read);
  for (const m of held ?? task.messages ?? []) {
    if (m.unread) next.add(readKey(task.key, m.message_id));
  }
  next.add(allReadKey(task.key, markObservation(task)));
  return next;
}

/**
 * The same mark, carried onto messages that have only just come into our hands.
 *
 * Show more fetches the whole thread AFTER the press that cleared it, and that
 * reply is a read of a value the mark overrode: the server had not applied the
 * write yet (or the fetch crossed it), so 86 messages arrive flagged `unread`
 * and nothing in the poll ever refreshes them — `more` is false by then, so
 * there is no second fetch until the List remounts. Left alone, they light 86
 * dots under a row that says 0, on messages the reader marked read a second ago.
 *
 * The GATE is the sentinel's own observation, which is exactly the question
 * being asked: while it holds, the server is still quoting the value the press
 * overrode, so a thread it hands us is a thread the press covered, and the ids
 * are written the same way the press wrote its own. The moment a poll (or the
 * mark's own answer, or a rollback) retires that sentinel, this adopts nothing
 * and a message that is genuinely unread keeps its dot.
 *
 * That is NOT the sentinel leaking back into isUnread. It is consulted once, at
 * the moment a fetch lands, to decide whether the mark covers what the fetch
 * brought; the dots themselves still go out through concrete ids, each one gated
 * on the server still calling that message unread. A message arriving later is
 * named by nothing here and dots on its own.
 *
 * Idempotent: the sentinel markAllRead re-adds is the one the gate just matched,
 * so the observation is never widened — only the id half grows.
 */
export function carryMarkToHeld(
  read: Set<string>,
  task: Task,
  held: TaskMessage[],
): Set<string> {
  if (!isAllRead(read, task)) return read;
  return markAllRead(read, task, held);
}

/**
 * Take the whole-task mark back: the write was refused, or the server's own
 * answer said something is still unread.
 *
 * EVERY sentinel for the task goes, whatever observation it was stamped with. A
 * poll may have landed while the request was in flight, which leaves the mark
 * inert but still sitting there, and "inert" is not the same as "gone" the next
 * time that observation comes round.
 *
 * The concrete ids the press wrote go too — `held` is the list it wrote them
 * from — because restoring the count without restoring the dots would leave a
 * row saying "3 unread" above three rows that all look read.
 *
 * `held` is therefore EVERYTHING THE MARK WROTE, not just what was held when the
 * press went out: a Show more that lands while the write is in flight adopts the
 * mark onto the rest of the thread (carryMarkToHeld), and a rollback that cannot
 * see those ids is the same half-restored row wearing the other hat. Callers
 * pass both lists; a duplicate id deletes once and costs nothing.
 */
export function unmarkAllRead(
  read: Set<string>,
  taskKey: string,
  held: TaskMessage[] = [],
): Set<string> {
  const next = new Set(read);
  // Built by the one function that builds these keys, so a change to their
  // shape cannot leave this scan looking for the old one.
  const prefix = allReadKey(taskKey, "");
  for (const key of next) if (key.startsWith(prefix)) next.delete(key);
  for (const m of held) next.delete(readKey(taskKey, m.message_id));
  return next;
}

/**
 * What the server's OWN answer to the mark means for the local optimism.
 *
 * `POST /api/tasks/read {all: true}` replies with what is still unread after
 * the mark — 0 unless something arrived while the request was in flight. That
 * number used to be dropped on the floor. A non-zero one is the server saying
 * the press did not clear the row, so the optimism is void: the row goes back
 * to reporting what is there, and the button comes back with it.
 *
 * Over-reporting for one poll interval is the safe direction, and the reason
 * this rolls the whole mark back rather than trying to guess which messages the
 * answer is about: it can only show news that exists, never hide news that does.
 */
export function settleMarkAllRead(
  read: Set<string>,
  taskKey: string,
  held: TaskMessage[],
  answer: { unread: number },
): Set<string> {
  return answer.unread > 0 ? unmarkAllRead(read, taskKey, held) : read;
}

/** Whether the whole-task mark still speaks about the value the server is
 * quoting. False the moment a poll disagrees — which is the poll winning. */
export function isAllRead(read: Set<string>, task: Task): boolean {
  return read.has(allReadKey(task.key, markObservation(task)));
}

/**
 * Whether this message still reads as unread.
 *
 * Per message and nothing else: the whole-task sentinel is deliberately NOT
 * consulted here. It cannot name a message, so consulting it is precisely how a
 * message that arrived after the press stayed invisible. The whole-task mark
 * writes concrete ids for everything it can see (markAllRead) and those are what
 * this reads — and `m.unread` gating them is what retires each one on its own:
 * once the server agrees the message is read, the local entry stops mattering
 * rather than having to be pruned against a list that moves under us.
 *
 * Which puts the whole burden on "everything it can see" being the truth: every
 * gesture that marks a whole task has to write ids for everything HELD at the
 * time (heldMessages), and a fetch that hands us more of the thread while the
 * mark still stands has to be adopted into it (carryMarkToHeld). A narrower
 * write is a lit dot this function will never take back, because the only key
 * that could is one nobody wrote.
 */
export function isUnread(taskKey: string, m: TaskMessage, read: Set<string>): boolean {
  return m.unread && !read.has(readKey(taskKey, m.message_id));
}

/**
 * What the LEADING slot of a message row draws.
 *
 * The dot used to ride at the far right of the row, beside the time, with the
 * word "unread" after it — and it was missed entirely (Akshil, 2026-08-16: "on
 * the right hand I missed it, I did not even see it"). A thread is read as a
 * COLUMN, and a column is scanned down its left edge; a marker at the right end
 * has to be tracked across every row to be found at all.
 *
 * So the slot is at the START of the row and it is ALWAYS THERE — filled on an
 * unread message, blank on a read one. Always-there is the half that makes it
 * scannable: a slot that only exists on unread rows shifts every other cell of
 * that row right, and a ragged left edge is exactly the thing a scan cannot
 * follow.
 *
 * The word is gone. A dot that LEADS is already the whole signal, and "unread"
 * printed beside it was a caption for a symbol that no longer needs one. It
 * survives where it always mattered — `label` is rendered as the marker's
 * accessible name (role="img" + aria-label), so a screen reader still hears
 * "Unread" on exactly the rows that are.
 */
export interface UnreadMarker {
  /** Filled (a dot) or a blank spacer holding the column open. */
  unread: boolean;
  /** The accessible name, "" when there is nothing to announce. */
  label: string;
}

/** The word a screen reader hears in place of the dot. */
export const UNREAD_LABEL = "Unread";

export function unreadMarker(
  taskKey: string,
  m: TaskMessage,
  read: Set<string>,
): UnreadMarker {
  return isUnread(taskKey, m, read)
    ? { unread: true, label: UNREAD_LABEL }
    : { unread: false, label: "" };
}

/**
 * The count on the task row: what the server said, less the ones cleared here
 * since. Only messages we actually HOLD can be discounted — the badge counts
 * the whole thread, and a message outside the loaded window is one we know
 * nothing about beyond the server's total.
 *
 * `held` is heldMessages: the window before Show more, the whole thread after it,
 * and the same list the mark writes its ids from, so the count and the dots are
 * arithmetic over one set rather than two.
 */
export function taskUnread(
  task: Task,
  read: Set<string>,
  held?: TaskMessage[],
): number {
  // The one case that is NOT arithmetic over the messages we hold: a whole-task
  // mark cleared the ones outside the window too, and the server was told so in
  // the same breath. Discounting only the loaded three would leave a row saying
  // "86" after the press that cleared all 89.
  //
  // It is asked of the mark AND of this poll's own numbers (isAllRead), so it
  // answers 0 only while the server is still quoting the count the press
  // overrode. A poll that brings back anything else — a refused write the server
  // never applied, a message that arrived since — falls through to the
  // arithmetic below and the server's number is what the row draws.
  if (isAllRead(read, task)) return 0;
  const known = held ?? task.messages ?? [];
  // Once Show more has run we hold the WHOLE thread, and then the count is not
  // arithmetic at all — it is the dots, counted. Same predicate (isUnread), same
  // list the rows are drawn from, so the badge and the rail cannot say two
  // different things about one thread; the row saying 0 over 86 lit dots is
  // precisely the bug this arm closes.
  //
  // It is also the more accurate of the two. `task.unread` is deliberately
  // arithmetic on the server ("every message is unread unless marked read or not
  // yet happened", clamped at zero because a marked-then-cancelled message counts
  // twice) and its own docstring says the Show-more endpoint is the exact one. So
  // where we have the exact thread, we use it.
  if (known.length >= task.message_count) {
    return known.filter((m) => isUnread(task.key, m, read)).length;
  }
  const cleared = known.filter(
    (m) => m.unread && read.has(readKey(task.key, m.message_id)),
  ).length;
  return Math.max(0, task.unread - cleared);
}

/**
 * What a TASK row says about its whole thread — the counterpart of unreadMarker,
 * which speaks for one message.
 *
 * The two are drawn in the same place now (trailing the title) and in the SAME
 * MARK, and that is the last of four passes:
 *
 *   * The dot moved to the head of every MESSAGE row, because a thread is scanned
 *     down its left edge and a marker at the right end is missed entirely
 *     (Akshil, 2026-08-16: "on the right hand I missed it").
 *   * The task's count followed it to the left, so the rail would not start
 *     halfway down the node.
 *   * And that was wrong, because the two rows are not the same question. A list
 *     is scanned for its TITLES, and a number in front of every one of them
 *     announced the messages before the work they are about — it "breaks the
 *     reading priority" (Akshil, 2026-08-17). So both marks went to the end of
 *     their title.
 *   * And then the NUMBER went too (Akshil, 2026-08-17: "only show a single dot
 *     like the notification that we show"). A row said `211` and a card said `13`
 *     — a readout nobody acts on per unit, where the only decision it feeds is
 *     "is there anything new here?". That is one bit, so it is drawn as one bit,
 *     in the very mark the message rows already use.
 *   * And then the mark left the title altogether (Akshil, 2026-08-18). A dot
 *     after the words was a SECOND glyph on a row that already carries one — the
 *     status ring — and two marks a few characters apart, one saying "this
 *     finished" and one saying "you have not looked", is a row a reader has to
 *     decode rather than scan. So read-state moved INTO the ring: the centre dot
 *     that used to mean "settled" now means "settled AND unread", and a ring gone
 *     hollow is the whole of "you have seen this". Colour is untouched, so the
 *     ring still says WHICH terminal state in exactly the hue it always did, and
 *     the row is back to one mark carrying two orthogonal facts — shape and hue.
 *
 * So there is no pill, no digit and no trailing dot: a task with unread wears a
 * filled ring (ScheduleTaskViews.StatusIcon, `.schedule-ring--unread`), the same
 * mark its own unread messages wear one level down, and the same mark the board
 * lane's header wears one level up.
 *
 * THE COUNT IS NOT LOST, it is only unprinted — taskUnreadLabel below returns the
 * accessible name, and it names the real number. A reader who cannot see the ring
 * gets more than the sighted one, which is the right way round for a mark whose
 * whole visual job is to be noticed rather than read.
 */

/** The tooltip and accessible name a container wears when something inside it is
 * unread — a TASK row over its thread, a board LANE over its cards. Null when
 * there is nothing unread, and then nothing is said at all.
 *
 * "3 unread", not "3 unread messages" (2026-08-18). A lane's total counts TASKS
 * and a task's counts MESSAGES, and the mark that carries both is now one glyph
 * (StatusIcon's centre dot) — so the noun would have to change with the container
 * while the mark did not, which is two vocabularies for one fact again. The
 * count is the part a reader acts on; what it counts is whatever they are
 * hovering.
 *
 * Uncapped, and the same shape at one. The old pill printed "99+" past a cap
 * because a three-digit number does not fit a 16px chip; a tooltip has no such
 * constraint, and the name it carries is the whole of what the row knows.
 *
 * LEAVES DO NOT GET ONE. A single unread message's dot means exactly "unread"
 * and a hover saying "1 unread" over it is a caption for a symbol that needs
 * none (Akshil, 2026-08-18); only containers, whose dot stands for a number the
 * ink does not print, are named. */
export function taskUnreadLabel(count: number): string | null {
  if (count <= 0) return null;
  return `${count} unread`;
}

/**
 * How many of a LANE's tasks have something unread — what a kanban group header
 * says about the column under it.
 *
 * Counted in TASKS, not messages: the header stands over cards, and the question
 * a reader asks of a collapsed lane is "how many of these do I still have to
 * look at", which is one per card however long its thread is. (A task's own mark
 * counts messages, for the same reason at the other scale.)
 */
export function laneUnread(tasks: Task[], read: Set<string>): number {
  return tasks.filter((t) => taskUnread(t, read) > 0).length;
}

/**
 * A task whose work is still ahead of it — the List greys such a title, so a
 * column of rows reads as "these already happened" with the future set behind
 * them (Akshil, 2026-08-18).
 *
 * BOTH halves are required. The lane alone is not enough: an Upcoming task whose
 * time has already gone by is overdue, and fading it would mute the one row on
 * the page that most wants reading. And a future time alone is not enough
 * either — a Done task usually has a next run scheduled too, and its title is
 * history that HAS happened.
 */
export function isUpcomingTask(task: Task, now: number = Date.now()): boolean {
  return taskColumn(task) === "upcoming" && !isPastDue(nextRunAt(task), now);
}

// ---- the accordion -----------------------------------------------------------

export interface ThreadView {
  /** What the expanded task actually lists, newest first. */
  messages: TaskMessage[];
  /** Whether the thread is longer than what we hold — i.e. a fetch is OWED.
   *
   * This used to mean "offer the Show more button". There is no button since
   * 2026-08-18; expanding a task fetches the rest by itself, and this is the
   * predicate that decides whether the trip is needed at all. Same question, same
   * answer — only the thing that reads it changed. */
  more: boolean;
  /** How many are still missing. The loading line names it, so a reader looking at
   * three rows of a twenty-six-message thread can see that the other twenty-three
   * are on their way rather than absent. */
  hidden: number;
}

/**
 * EVERY MESSAGE OF THIS THREAD THIS CLIENT HOLDS, freshest copy of each — the
 * one answer to "what is in our hands", which is what a whole-task mark has to
 * cover (markAllRead) and what its count is arithmetic over (taskUnread).
 *
 * Two lists arrive on two schedules and neither is simply better than the other:
 *
 *   * the LISTING row (`task.messages`) is the three newest, replaced by every
 *     poll — so it is the freshest thing we have, and the only place a message
 *     that arrived a moment ago can appear at all;
 *   * the thread Show more FETCHED (`loaded`) is all of it, read once and never
 *     read again — `more` goes false, so nothing refetches it before a remount.
 *
 * So: the fetched thread for depth, the listing's copy of any message that is in
 * both for its state, and anything the listing has that the fetch does not is a
 * message that ARRIVED AFTER the fetch — it leads, because the order is newest
 * first. Without that last part an expanded thread is frozen at the instant it
 * was fetched: an arrival is invisible until the List remounts, which is the very
 * defect the whole-task sentinel was rebuilt to stop having.
 *
 * Deduped by message id, so nothing is ever drawn twice.
 */
export function heldMessages(task: Task, loaded?: TaskMessage[]): TaskMessage[] {
  const window = task.messages ?? [];
  if (!loaded) return window;
  const fresh = new Map(window.map((m) => [m.message_id, m]));
  const fetched = new Set(loaded.map((m) => m.message_id));
  return [
    ...window.filter((m) => !fetched.has(m.message_id)),
    ...loaded.map((m) => fresh.get(m.message_id) ?? m),
  ];
}

/**
 * What an expanded task shows.
 *
 * Until the fetch lands that is `task.messages` — the three newest, already ordered
 * by the server. After it, the full thread REPLACES those three rather than
 * appending to them, so a message can never appear twice: heldMessages merges
 * them by id, taking the listing's fresher copy of anything in both and leading
 * with whatever arrived after the fetch.
 *
 * THE THREE ARE A DATA WINDOW, NOT A DISPLAY CAP, and that distinction is the whole
 * of why this function still slices. The listing endpoint sends three messages per
 * row because it runs for every task on the page and a full transcript parse per
 * task would not survive a few hundred of them (server routers/tasks.py `_row`);
 * the rest are not in the client's hands to draw. Until 2026-08-18 a dashed
 * "Show N more" button was the press that went and got them, and it is gone —
 * expanding a task makes that trip by itself (ScheduleTaskViews.TasksList.toggle).
 * So this still reports a short list for the moment before the reply arrives, and
 * `more` is what sends for the rest rather than what draws a button.
 *
 * `message_count` is the server's total, and the honest source for "is there
 * more?": the preview list alone cannot tell a thread of exactly three from a
 * thread of three hundred.
 */
export function threadView(task: Task, loaded?: TaskMessage[]): ThreadView {
  if (loaded) return { messages: heldMessages(task, loaded), more: false, hidden: 0 };
  const messages = (task.messages ?? []).slice(0, PREVIEW_MESSAGES);
  const hidden = Math.max(task.message_count - messages.length, 0);
  return { messages, more: hidden > 0, hidden };
}

/**
 * Whether this task is an ACCORDION at all — i.e. whether expanding it would
 * reveal anything the row has not already said.
 *
 * A thread of one message is not a thread. Expanding it drew exactly one message
 * row, whose title is the same text the task row above it was already showing, so
 * the disclosure offered a press that told the reader nothing ("empty task (1 msg
 * only) should not have dropdown", Akshil, 2026-08-17). A task of ZERO — a pending
 * one that has never run — is the same case and the same answer.
 *
 * Asked of `message_count`, the SERVER's total, and never of the tail this client
 * happens to be holding. `task.messages` is a preview window (PREVIEW_MESSAGES),
 * so a busy thread can arrive with a short tail or none at all, and counting what
 * we hold would call a forty-message task unexpandable. It is the same number
 * threadView already trusts for "is there more?", so the chevron and the "Show N
 * more" button under it cannot disagree about how long the thread is.
 */
export function isExpandable(task: Task): boolean {
  return task.message_count > 1;
}

/**
 * The ONE message a leaf row is about, or null when there is not exactly one.
 *
 * Dropping the chevron from a one-message row left its click doing nothing at
 * all, which Akshil noticed and disliked (2026-08-17). With nothing to expand,
 * "open it" is the only thing a press on that row can sensibly mean — and what
 * it opens is this message, through the very path a MESSAGE row's own click
 * takes (openMessage → messageHref). So the row needs to name that message, and
 * this is where it is named.
 *
 * Both halves of the guard matter:
 *
 *   * NOT expandable (isExpandable, the server's `message_count`), so this can
 *     never answer for a row whose press is the accordion. A row of forty
 *     messages holding a window of one is still an accordion.
 *   * EXACTLY ONE message in hand. A task that has never run has none — no
 *     transcript, nothing to open — so it answers null and the row stays inert
 *     rather than navigating somewhere half-built.
 *
 * `held` is heldMessages, the same list the row's count and its marks are
 * arithmetic over, so the message the click opens is the message the row is
 * drawing a dot for.
 */
export function soleMessage(task: Task, held?: TaskMessage[]): TaskMessage | null {
  if (isExpandable(task)) return null;
  const messages = held ?? task.messages ?? [];
  return messages.length === 1 ? messages[0] : null;
}

/** Collapsed by default, so the expanded set is what is OPEN (an empty set is
 * the resting state and needs no seeding from a task list that changes on
 * every poll). */
export function toggleExpanded(expanded: Set<string>, key: string): Set<string> {
  const next = new Set(expanded);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

export function isExpanded(expanded: Set<string>, key: string): boolean {
  return expanded.has(key);
}

// ---- which view is up, in the URL --------------------------------------------
// The List/Board/Calendar choice was localStorage-only, which made it a fact
// about this browser rather than about this page: a link to the Tasks page
// opened whatever the recipient last looked at, and there was no way to send
// somebody the board. It lives in the URL now (`/tasks?view=board`), with the
// stored preference kept as the fallback for a bare `/tasks`.
//
// LIST OMITS THE PARAM, deliberately: it is the default, and `?view=list` is a
// second spelling of `/tasks` that would show up in every share and every
// bookmark while saying nothing at all.

/** Which of the page's three views is up. */
export type TaskView = "list" | "board" | "calendar";

/** The query key that carries it. */
export const VIEW_PARAM = "view";

/**
 * The view a URL asks for, or `fallback` when it asks for nothing this page
 * knows — an unrecognised value is a typo or a stale link, and the page it
 * should land on is the default one rather than an error.
 *
 * `search` is a raw query string, with or without its leading `?`.
 */
export function viewFromSearch(search: string, fallback: TaskView = "list"): TaskView {
  const raw = search.startsWith("?") ? search.slice(1) : search;
  const v = new URLSearchParams(raw).get(VIEW_PARAM);
  return v === "list" || v === "board" || v === "calendar" ? v : fallback;
}

/**
 * The same URL with the view switched — every OTHER param preserved, because
 * this page's query also carries the chat's deep-link handoff, and switching
 * between two views is not a reason to drop it.
 *
 * Returns path + query, ready for `history.replaceState`. Replace, not push:
 * the toggle is a way of READING this page, and a back button that first walked
 * back through six view switches before leaving would be a worse back button.
 */
export function viewUrl(pathname: string, search: string, view: TaskView): string {
  const raw = search.startsWith("?") ? search.slice(1) : search;
  const q = new URLSearchParams(raw);
  if (view === "list") q.delete(VIEW_PARAM);
  else q.set(VIEW_PARAM, view);
  const rest = q.toString();
  return pathname + (rest ? `?${rest}` : "");
}

// ---- a press that leaves this tab --------------------------------------------

/**
 * Does this click mean "somewhere else, not here" — a new tab, a new window, a
 * download — and must therefore be left to the browser?
 *
 * Every row on this page is a real `<a href>` so that ⌘-click, middle-click and
 * the context menu's "Open in new tab" all work without this page implementing
 * any of them. The one thing its handler must do is GET OUT OF THE WAY: a
 * modified click is never intercepted, never `preventDefault`ed, and never
 * marks anything read — the reader is not looking at that thread, they are
 * stacking it up for later, and a badge cleared for a tab nobody has read yet
 * is the one thing a background open must not do.
 *
 * `button` is the mouse button as React reports it (0 = primary); a middle
 * click reaches `onAuxClick` rather than `onClick`, and both ask this, so the
 * rule is written once.
 */
export function opensElsewhere(e: {
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  button?: number;
}): boolean {
  return Boolean(
    e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || (e.button !== undefined && e.button !== 0),
  );
}

// ---- what the List remembers between visits ----------------------------------
// Opening a task's chat LEAVES the Tasks page, and coming back used to hand the
// reader a fully collapsed list scrolled to the top — so reading three threads
// out of ninety meant re-finding the same row three times (Akshil, 2026-08-18).
// The page now remembers which rows were open and where the list stood.
//
// sessionStorage, not localStorage: this is "where I was a moment ago", which is
// true for this tab and this sitting only. A week-old scroll offset restored into
// a list whose rows have all changed is not a memory, it is a surprise.

/** The key the List's per-tab memory lives under. */
export const LIST_MEMORY_KEY = "fused-render:tasks-list-memory";

export type ListMemory = {
  /** Task keys the reader had open. Keys that no longer exist simply never match
   * a row, so a stale entry costs nothing and needs no pruning. */
  expanded: string[];
  /** scrollTop of the list's own scroller, in px. */
  scroll: number;
  /**
   * The task whose conversation the reader last opened FROM this list, or "".
   *
   * The third thing coming back to the page has to answer. Which rows were open
   * and where the list stood put the reader back in the right part of the list;
   * this puts them back on the right ROW. A list of ninety near-identical
   * three-line rows gives no clue which one you just came out of, so "let me
   * look at the next one" meant re-finding the last one first — the same
   * complaint the scroll memory was for, one level finer (Akshil, 2026-08-18).
   *
   * A key, not an index: rows re-sort on every poll (last_active), and an index
   * would highlight whichever row happened to land in that slot.
   *
   * One task, not a set. This is "where I just was", and a page that lit up
   * every row visited this sitting would be a highlight that means nothing by
   * the fourth one.
   */
  selected: string;
};

export const EMPTY_LIST_MEMORY: ListMemory = { expanded: [], scroll: 0, selected: "" };

/**
 * What came out of the store is a STRING WRITTEN BY SOMEONE ELSE — an older
 * build, a hand-edited devtools row — so every field is checked and anything
 * unrecognisable degrades to "remember nothing" rather than throwing during a
 * render.
 */
export function parseListMemory(raw: string | null): ListMemory {
  if (!raw) return EMPTY_LIST_MEMORY;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return EMPTY_LIST_MEMORY;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return EMPTY_LIST_MEMORY;
  }
  const row = parsed as { expanded?: unknown; scroll?: unknown; selected?: unknown };
  const expanded = Array.isArray(row.expanded)
    ? row.expanded.filter((k): k is string => typeof k === "string")
    : [];
  const scroll =
    typeof row.scroll === "number" && Number.isFinite(row.scroll) && row.scroll > 0
      ? row.scroll
      : 0;
  // Absent on anything written before 2026-08-18, and on any hand-edited row:
  // "" is the same answer as "nothing selected", so an older memory upgrades
  // silently rather than being thrown away for the two fields it does have.
  const selected = typeof row.selected === "string" ? row.selected : "";
  return { expanded, scroll, selected };
}

// ---- where a click goes ------------------------------------------------------
// schedule-lib.explorerUrl is the app's one answer to "open this session in the
// explorer, with the Claude pane on it". A message adds one thing: WHICH turn
// to land on, carried as its transcript record uuid so the chat can scroll to
// it. Extending that url rather than minting a second scheme is deliberate —
// two ways to open a chat is two ways for one of them to rot.

/** The query key the Claude pane reads to scroll a resumed chat to one turn. */
export const MESSAGE_ANCHOR_PARAM = "msg";

/** The task's thread, top of the chat. Null when the task has never run —
 * there is no session to open yet. */
export function taskHref(task: Task): string | null {
  if (!task.session_id) return null;
  return explorerUrl(task.target || task.project, task.session_id);
}

/** One message inside that thread. Falls back to the thread itself when the
 * transcript gave us no anchor — landing at the top of the right conversation
 * beats not opening at all. */
export function messageHref(task: Task, m: TaskMessage): string | null {
  const base = taskHref(task);
  if (!base) return null;
  if (!m.anchor) return base;
  return `${base}&${MESSAGE_ANCHOR_PARAM}=${encodeURIComponent(m.anchor)}`;
}

/**
 * Where a click on a message ROW goes, or null when it has nowhere to go — the
 * form every view that lists messages should ask in, because the calendar's
 * popover lists one kind the List never does.
 *
 * A PROJECTED occurrence (schedule-lib.isProjected) is cron arithmetic, not a
 * message: the server computed that it WILL happen and wrote nothing down. It
 * has no transcript record and therefore no anchor, so linking it would open a
 * conversation that has not happened — and a `msg=` built from its empty anchor
 * would be a pointer at nothing. It is inert.
 *
 * The other two cases are messageHref's own and are not re-decided here: a task
 * with no session yet is null, and a real message with an empty anchor falls
 * back to the top of the right thread.
 */
export function openMessageHref(task: Task, m: TaskMessage): string | null {
  if (isProjected(m)) return null;
  return messageHref(task, m);
}

// ---- cancelling a message that has not gone out ------------------------------
// A scheduled message the user no longer wants is a real capability, and the
// thread is the only place it is now offered: the calendar's Queued strip covers
// work that is already PAST DUE and waiting, which says nothing about a task
// scheduled for next Tuesday.
//
// Two rules decide it, and both are about honesty rather than convenience.
//
// 1. Only a `pending` message. `sending` is deliberately not cancellable
//    server-side (schedule.cancel) — the helper is away and the turn may have
//    started, so "cancelled" would be a claim nothing can make good on — and a
//    sent/missed/errored message has already had its whole life.
//
// 2. On an OCCURRENCE of a recurring rule, the id sent is the occurrence's own,
//    never its template's. This is the one place the thread's two affordances
//    deliberately disagree: Edit resolves an occurrence UP to its template
//    (changing "next Tuesday's run" means changing the rule, because there is
//    nowhere else for the change to live), while Cancel stays DOWN on the
//    occurrence. The server reads it exactly that way — cancelling a template
//    stops every further run, cancelling an occurrence skips that one and the
//    next materialisation pass carries on — so a Cancel that resolved upward
//    like Edit does would silently delete a schedule the user meant to skip one
//    run of. Since the two mean different things, the button says which one it
//    is doing: "Cancel" on a one-off, "Skip this run" on an occurrence.

export type CancelScope = "message" | "occurrence";

export interface CancelIntent {
  /** The schedule entry id to pass to cancelScheduledMessage — the message's
   * OWN entry, never a template it was materialized from. */
  id: string;
  scope: CancelScope;
  /** The button's accessible name, and what it says it will do. */
  label: string;
  /** The tooltip, which is where the consequence is spelled out in full. */
  title: string;
}

/** What Cancel would do to this message, or null when there is nothing to
 * cancel. Null is the answer for every message that has already gone out, and
 * for a chat message, which was delivered the moment it was typed. */
export function cancelIntent(m: TaskMessage): CancelIntent | null {
  if (m.state !== "pending" || !m.entry_id) return null;
  if (m.template_id)
    return {
      id: m.entry_id,
      scope: "occurrence",
      label: "Skip this run",
      title: "Skip this run — the repeat itself keeps going",
    };
  return {
    id: m.entry_id,
    scope: "message",
    label: "Cancel",
    title: "Cancel this scheduled message",
  };
}

/** Whether the row draws the affordance at all. */
export function canCancel(m: TaskMessage): boolean {
  return cancelIntent(m) !== null;
}

/**
 * WHICH ENTRY A MESSAGE ROW'S PRESS EDITS, or null when that press means the
 * transcript instead.
 *
 * The same principle the task row follows one level up: a message that has not
 * gone out is an INSTRUCTION, and the form is where an instruction is read and
 * changed; a message that has run is a TRANSCRIPT TURN, and the transcript is
 * where it is read (Akshil, 2026-08-17: "for multi-message tasks when i click on
 * the message, that should open the edit modal").
 *
 * SO THE SPLIT IS `state`, and it is the same predicate cancelIntent already
 * uses — deliberately the same, because the two questions have one answer: a
 * message the server would refuse to cancel is a message it would refuse to
 * edit. `pending` and nothing else. `sending` is mid-flight and already beyond
 * changing; `sent`, `missed`, `error` and `cancelled` have all had their whole
 * life, so a form over one would present a Save that means nothing. Note it is
 * `state`, not turnPhase: turnPhase reads `turn` and answers how the SESSION
 * replied, which is a question only a message that already went out can have.
 *
 * THE MESSAGE'S OWN ENTRY, never the task's `next_run_entry`: a repeating task
 * has several pending occurrences and the row pressed is the one the reader
 * means. Resolving an occurrence UP to its template is Scheduled.tsx's
 * `editEntry` — see the note above cancelIntent for why Edit resolves upward
 * while Cancel stays down.
 *
 * NULL ON A PENDING MESSAGE IS POSSIBLE, and then the press must fall through to
 * the transcript rather than open a blank form: a CHAT message carries no
 * `entry_id` at all (it was delivered the moment it was typed and the schedule
 * has no record of it), and a listing row from an older server may not carry one
 * either.
 */
export function messageEditEntry(m: TaskMessage): string | null {
  if (m.state !== "pending" || !m.entry_id) return null;
  return m.entry_id;
}

// ---- drag --------------------------------------------------------------------
// THE WHOLE DRAG MATRIX, and it follows from one sentence: a lane is what
// Claude's work is DOING, and the only thing a person decides about a task is
// whether to run it or to put it away. So there are exactly two moves, and
// three lanes a card may leave.
//
//   Upcoming    → In Progress  RUN IT NOW. Not a filing decision, an
//                              instruction (Akshil, 2026-08-16: "if I move a
//                              task from upcoming to in progress, don't change
//                              the time of it, but run it and run it at that
//                              point"). The server sends the pending message
//                              immediately and leaves its `due` alone, so the
//                              row keeps reading as the time it was MEANT to
//                              run and the thread honestly shows a run that
//                              happened early.
//               → Archive      CANCEL. Filing a task away calls off the work
//                              in it; a run still booked for tomorrow on a
//                              task somebody archived would un-archive itself.
//   In Progress → nowhere      LOCKED. In Progress is Claude's output, not a
//                              verdict a reader hands down: a card leaves this
//                              lane when the run ends and at no other moment.
//   Done        → Archive      and nothing else. "Not finished after all" is
//                              not a thing a drag can make true.
//   Failed      → In Progress  RETRY — the same run-now call, same
//                              precondition (something pending to fire).
//               → Archive
//   Archived    → nowhere      LOCKED — and the way back is not a gesture at
//                              all, it is ACTIVITY (Akshil, 2026-08-18: "if you
//                              want to move it to in progress or done, just
//                              type in a message inside that chat and it will
//                              automatically move"). A message that arrives
//                              after the filing drops the filing outright,
//                              server-side, and the task rejoins whichever lane
//                              it derives into. So there is no unarchive
//                              control anywhere, on either view.
//
// Nothing may be dropped INTO Upcoming (a task cannot be un-run), into Failed
// (failure is something that HAPPENED, and a lane you can drag a healthy task
// into is a lane whose count means nothing), or into Done (a run says that,
// not a reader).
//
// Legality follows from what each move NEEDS, never from what triage happens to
// be keyed by. Run-now needs a pending MESSAGE, so a scheduled task that has
// never run — no session id at all — may still be dragged into In Progress,
// while a pure-chat task with nothing pending may not. Archive needs only the
// task's key, because it is one server verb over the whole task
// (`POST /api/tasks/archive`) rather than a triage write keyed by session — so
// the never-run row can be filed away too, which is the case the old
// session-keyed rule could not reach.

/** The lanes a person may drop a card ON. Two, and the second is not a
 * synonym for the first: one starts work, the other ends it. */
export const DROP_LANES: BoardColumn[] = ["in_progress", "archived"];

/**
 * THE MATRIX ITSELF: where a card in each lane may go. The table above in
 * words, written down once so `dropLanes` reads it instead of reconstructing it.
 *
 * A TABLE AND NOT A PREDICATE, which is the correction (bugbot, PR #613). It
 * was "every unlocked lane may go anywhere in DROP_LANES, subject to a
 * precondition", and that quietly offered Done → In Progress to any done task
 * with something pending — which is not a corner case on this branch, it is the
 * COMMONEST done card there is: a recurring task whose last run finished and
 * whose next occurrence is booked now sits in Done by design (see
 * `_message_verdict`, server side). So the lane a person is most likely to drag
 * from was the one lane whose rules were wrong, and the drop would have fired a
 * real run.
 *
 * Re-running a task that finished is deliberately not a gesture. "Run this
 * again" on work that succeeded is an ask better made in the chat, where the
 * person can say what they want differently this time — the same reasoning
 * `taskRunIntent` gives for offering Re-send on a failed task and nowhere else.
 *
 * Every BoardColumn is a key, so a sixth lane is a type error here rather than a
 * lane that silently permits nothing (or everything).
 */
const LANE_EXITS: Record<BoardColumn, BoardColumn[]> = {
  // Run it early, or call it off.
  upcoming: ["in_progress", "archived"],
  // Locked: a run in flight is Claude's output, and it leaves this lane when it
  // ends, not when a card is dragged.
  in_progress: [],
  // Archive only. "Not finished after all" is not something a drag can make
  // true, and neither is "do it again".
  done: ["archived"],
  // Retry, or file it away.
  failed: ["in_progress", "archived"],
  // Locked: the way back out is activity, not a gesture. See archiveIntent.
  archived: [],
};

/**
 * The next run the ROW ITSELF names, when it names one: the server's `next_run`
 * (`min(at)` over every pending entry, epoch seconds) together with the entry
 * that run belongs to.
 *
 * Read as ONE fact because that is how the server writes them (tasks.py
 * `_next_run`) and either half alone is useless: a time nothing can fire, or an
 * id with no place in the order. The server refuses to name a run it cannot
 * name completely, so this is either both or neither.
 *
 * Null covers the two cases every caller treats identically — the fields are
 * absent (an older server) or zero (nothing pending) — and the answer for both
 * is "read the window instead", which is what they did before these existed.
 */
function namedNextRun(task: Task): { at: number; entryId: string } | null {
  const at = task.next_run ?? 0;
  const entryId = task.next_run_entry ?? "";
  if (!at || !entryId) return null;
  return { at, entryId };
}

/**
 * What a run-now press or drop actually sends. Not a TaskMessage, because the
 * message this fires is not always one the row is CARRYING: see runNowTarget.
 */
export interface RunTarget {
  /** What runScheduledNow is called with. The whole point of this object. */
  entryId: string;
  /**
   * Which message that is — "" when the run is one the row named without
   * holding (`next_run_entry`). The listing cannot number a message it did not
   * parse: MSG-n is a position in the whole thread, and the row's ids are
   * counted back from its total across the three it holds.
   *
   * Nothing in the run path needs it — the call sends `entryId` — so an empty
   * one costs a caller a sentence it could have said, never a wrong action.
   */
  messageId: string;
  /** When it is due, epoch seconds: the same instant nextRunAt sorts the lane
   * by, which is what makes the button and the order agree. */
  at: number;
}

/**
 * Which pending message a run-now press or drop fires: the EARLIEST due.
 *
 * A task can hold several pending messages — a recurring rule's next
 * occurrence sitting beside a one-off someone scheduled for Friday. The
 * earliest is the one the scheduler itself would have sent next, so running it
 * early is the only choice that does not reorder the thread: any other pick
 * would fire a later message first and leave an older one still pending behind
 * it. On an exact tie the OLDER message wins (the server's list is newest
 * first, so the later element of a tie is the one that has waited longer).
 *
 * TWO PLACES ARE READ, and the second is the point.
 *
 * The window — the three newest by `at` — used to be all of it, on the belief
 * that pending messages are due in the future and so sit at its head. On this
 * branch that is false: scheduling into the past is allowed and catch-up is
 * unbounded, so an OVERDUE pending is ordinary, and two sent runs plus next
 * month's occurrence push it out of the window entirely. The row's own
 * `next_run` / `next_run_entry` name that run, and this fires it — because
 * nextRunAt reads the same field to ORDER Upcoming by, and a card promoted to
 * the top of the lane whose button then sent some other message would make the
 * order a lie. The sort and the button widen together or neither does.
 *
 * `entry_id` (or `next_run_entry`) is required either way: it is what the call
 * sends, and a message without one cannot be fired at all.
 */
export function runNowTarget(task: Task): RunTarget | null {
  let held: RunTarget | null = null;
  for (const m of task.messages ?? []) {
    if (m.state !== "pending" || !m.entry_id) continue;
    if (!held || m.at <= held.at)
      held = { entryId: m.entry_id, messageId: m.message_id, at: m.at };
  }
  const named = namedNextRun(task);
  // Strictly earlier, so a run the row BOTH names and holds is fired as the
  // message it is — same entry either way, and that way it keeps its id.
  if (named && (!held || named.at < held.at))
    return { entryId: named.entryId, messageId: "", at: named.at };
  return held;
}

/** Whether this task has anything to run early at all. */
export function canRunNow(task: Task): boolean {
  return runNowTarget(task) !== null;
}

/**
 * WHICH SCHEDULE ENTRY A ONE-MESSAGE UPCOMING ROW'S PRESS EDITS, or null when
 * that row's press means something else.
 *
 * Such a row's interesting content is the instruction that HAS NOT RUN yet, and
 * the form is where that instruction lives (Akshil, 2026-08-17: "when i click on
 * upcoming tasks i think they should open up the edit modal" — then narrowed:
 * "this should be only for 1 message tasks"). A transcript is the wrong answer
 * for a row whose whole point is what happens next.
 *
 * THREE CONDITIONS, and each is somebody else's function so this adds no rule of
 * its own:
 *
 *   * the LANE, from taskColumn — the same function that files the card into the
 *     Board's lanes, so the List and the Board cannot disagree about what
 *     "Upcoming" means.
 *   * EXACTLY ONE MESSAGE, from soleMessage. Which is the case the user asked
 *     for and it lands where they meant: a task scheduled but never run has
 *     exactly one message — the pending one — so "one message and upcoming" IS
 *     the never-ran-yet row, and a one-off that has run once with its next
 *     occurrence pending is the same shape and the same answer. It also inherits
 *     soleMessage's isExpandable guard, so a REPEATING task with past runs is
 *     never this: its press stays the accordion, whatever its lane.
 *   * WHICH ENTRY, from runNowTarget — the earliest-due pending entry, reading
 *     the server's `next_run_entry` when the row names a run it does not hold.
 *     Deliberately the function run-now and the drag already ask: "the next one
 *     due" is the only run either gesture could mean, and a second function here
 *     would let Edit and Run now act on different ones. Only `entryId` is spent;
 *     Scheduled.tsx's `editEntry` is what resolves an occurrence to its template,
 *     because changing "tomorrow's run" of a repeating task means changing the
 *     rule.
 *
 * `held` is heldMessages, as soleMessage's own is — so the message this counts is
 * the message the row is drawing.
 *
 * NULL DESPITE THE LANE IS POSSIBLE and the caller must fall through rather than
 * open an empty form: runNowTarget answers null when the one message the row holds
 * is not pending or carries no `entry_id` (a chat message is delivered the moment
 * it is typed and the schedule has no record of it) AND the server named no next
 * run — an older server without the `next_run` fields.
 */
export function upcomingEditEntry(task: Task, held?: TaskMessage[]): string | null {
  if (taskColumn(task) !== "upcoming") return null;
  if (soleMessage(task, held) === null) return null;
  return runNowTarget(task)?.entryId ?? null;
}

/**
 * Whether the task READS as failed. Two things say so and the row shows the
 * same word for both — the `failed` lane, and the flag that repaints a Done
 * task's ring red (StatusIcon) — so both take the same verb on the button.
 */
export function isFailedTask(task: Task): boolean {
  return taskColumn(task) === "failed" || task.failed;
}

/**
 * The same move the drag makes, reachable without dragging (Akshil,
 * 2026-08-17: "for failed tasks do we want a rerun button... same for the
 * upcoming tasks. Do you think we should add a trigger now button... And if
 * that is the case we'll just update the run at instead of updating the at the
 * actual time it was scheduled").
 *
 * That last clause is confirmation, not a change: run-now moves `ran_at` and
 * never touches `due`, which is exactly what the drag path already does and
 * what ranNote above prints. NOTHING about the times moves here.
 *
 * WHICH message fires is not re-decided: runNowTarget is asked, the same
 * function dropAction asks, so the button and the drag can never pick
 * differently. Only the WORD differs — starting something early and restarting
 * something that broke are not the same sentence to a person, even though they
 * are one call to the server.
 *
 * Availability is simply "is there a pending message to claim", not the drag's
 * lane legality: dropLanes answers a question about DROP TARGETS (which lane
 * may receive this card), and a button has no lane. A task with nothing
 * pending — which is most failed tasks, since the run that broke has already
 * been spent — gets null here. That gap is what taskRunIntent below closes,
 * now that the server has a re-send verb; this function still answers only the
 * run-now question, so the drag and the drop can keep asking it.
 */
export interface RunNowIntent {
  /** What runScheduledNow is called with. */
  entryId: string;
  /** Which message that is — the same one the drag would have fired. */
  messageId: string;
  /** Whether this reads as a restart rather than an early start. */
  rerun: boolean;
  /** The button's accessible name, and the word it says. */
  label: string;
  /** The tooltip: the consequence, including the half a person would fear. */
  title: string;
}

export function runNowIntent(task: Task): RunNowIntent | null {
  const m = runNowTarget(task);
  if (!m) return null;
  const rerun = isFailedTask(task);
  return {
    entryId: m.entryId,
    messageId: m.messageId,
    rerun,
    label: rerun ? "Re-run" : "Run now",
    title: rerun
      ? "Re-run now — the scheduled time stays put"
      : "Run now — the scheduled time stays put",
  };
}

// ---- one button, two calls ---------------------------------------------------
// Re-run on a failed task used to be absent exactly when it was wanted. The
// common failure spends its message — the run went out and broke — so there was
// no PENDING entry left for run-now to claim, and the button that would have
// said "Re-run" was simply not drawn. The server now has the other verb
// (`POST /api/schedule/resend`, which sends the message AGAIN as a new one in
// the same thread), and this is where the choice between the two is made.
//
// It is a pure function and it lives beside runNowIntent deliberately: the
// component holds no rule about which call to make, and the run-now half is
// still runNowIntent — the same function dropAction asks — so the button and
// the drag cannot pick different messages.
//
// THE DRAG IS UNCHANGED. Dragging Upcoming → In Progress is still run-now and
// nothing else: a drop on a lane is a statement about where the card belongs,
// and turning "put this back in progress" into "send the whole message again"
// is not something a gesture can consent to. Re-sending is a button press with
// a word on it.

export type TaskRunKind = "run-now" | "resend";

export interface TaskRunIntent {
  /** Which call: runScheduledNow or resendScheduledMessage. */
  kind: TaskRunKind;
  /** The schedule entry id that call is given. For "resend" it is the entry
   * that ALREADY RAN — the server reads it, copies it, and leaves it alone. */
  entryId: string;
  /** Which message that is, for the caller that wants to say so. */
  messageId: string;
  /** Whether this reads as a restart rather than an early start. */
  rerun: boolean;
  /** The button's accessible name, and the word it says. */
  label: string;
  /** The tooltip: the consequence, including the half a person would fear. */
  title: string;
}

/**
 * Which message a re-send would copy: the newest one that WENT AND ENDED.
 *
 * `sent` and `error` are the two the server accepts (schedule.RESENDABLE), and
 * for the reason it gives — a message that never went has nothing to send
 * again. `pending` and `sending` are excluded here as well, but they never
 * reach this function: a task holding one takes the run-now branch above.
 *
 * A chat message carries no `entry_id` and is skipped, which is correct rather
 * than incidental: it was delivered the moment it was typed and the schedule
 * has no record of it to copy.
 *
 * Newest first, because the server's list is: re-asking means asking for the
 * LAST thing that was asked for, not the first.
 */
export function resendTarget(task: Task): TaskMessage | null {
  for (const m of task.messages ?? []) {
    if (!m.entry_id) continue;
    if (m.state === "sent" || m.state === "error") return m;
  }
  return null;
}

/**
 * What the task row's run button does — the whole decision, in one place.
 *
 * Order matters and says what the two verbs mean. A pending message is a
 * message the user already asked for and has not had yet, so bringing it
 * forward is the smaller, truer action and wins whenever it is available. Only
 * with nothing pending does a FAILED task fall through to re-sending, which
 * creates work that was not previously scheduled.
 *
 * Re-send is offered on a failed task and nowhere else. A Done task's run
 * finished; offering to silently re-run it would make a thread grow every time
 * someone leaned on a button, and "run this again" on work that succeeded is an
 * ask better made in the chat, where the user can say what they want differently
 * this time.
 */
export function taskRunIntent(task: Task): TaskRunIntent | null {
  const now = runNowIntent(task);
  if (now)
    return {
      kind: "run-now",
      entryId: now.entryId,
      messageId: now.messageId,
      rerun: now.rerun,
      label: now.label,
      title: now.title,
    };
  if (!isFailedTask(task)) return null;
  const m = resendTarget(task);
  if (!m) return null;
  return {
    kind: "resend",
    entryId: m.entry_id,
    messageId: m.message_id,
    rerun: true,
    label: "Re-run",
    // The tooltip carries the one thing a person needs to know before pressing
    // it: this does not rewrite the run that failed, it asks again in the same
    // conversation.
    title: "Re-run — sends this message again, as a new one in the same thread",
  };
}

/**
 * Which lanes this card may be dropped on. Empty ⇒ do not let it lift.
 *
 * `LANE_EXITS` decides where the card MAY go; the loop only drops a move whose
 * precondition is missing. There is one such precondition and it belongs to the
 * run: In Progress needs a pending MESSAGE to fire, not a session to file under
 * (see the note above), so a scheduled task that has never run may be dragged
 * there and a pure-chat task may not. Archive has no precondition — it is one
 * server verb over the task's own key.
 */
export function dropLanes(task: Task): BoardColumn[] {
  const here = taskColumn(task);
  const lanes: BoardColumn[] = [];
  for (const lane of LANE_EXITS[here]) {
    if (lane === "in_progress" && !canRunNow(task)) continue;
    lanes.push(lane);
  }
  return lanes;
}

export function isDraggable(task: Task): boolean {
  return dropLanes(task).length > 0;
}

/**
 * What a drop on `lane` actually DOES — the one place the two meanings are told
 * apart, so the Board's handler holds no rule of its own beyond which call to
 * make. Null when the drop is illegal, which is the same answer dropLanes gave
 * before the card lifted: the two agree because this asks it.
 *
 * `archive` carries no payload. It used to be a triage status, composed here
 * and sent to a session-keyed endpoint; it is now one verb over the whole task
 * (`api.archiveTask`, which cancels the work and files the session in one
 * request), and a verb with one meaning has nothing left to parameterise.
 */
export type DropAction =
  | { kind: "run"; entryId: string; messageId: string }
  | { kind: "archive" };

export function dropAction(task: Task, lane: BoardColumn): DropAction | null {
  if (!dropLanes(task).includes(lane)) return null;
  if (lane === "archived") return { kind: "archive" };
  const m = runNowTarget(task);
  return m ? { kind: "run", entryId: m.entryId, messageId: m.messageId } : null;
}

// ---- filing it away, without the drag ----------------------------------------
// "Can a task be deleted?" — no, and it never will be: a task IS a Claude
// session and this app does not destroy transcripts (D306). What it can be is
// ARCHIVED, which is the honest answer to that question — but only while
// archiving is something a person can actually reach. Until now it was one
// gesture on one view: drag the card onto the Archive lane, which starts
// COLLAPSED. "Switch to Board, expand a lane, drag" is not an affordance.
//
// So the same move gets a button, and the rule behind it lives here rather than
// in either component. It is deliberately not a second predicate: the whole
// decision is asked of dropAction, on the lane the Board's drag would have used,
// so the button and the drop cannot disagree about who may archive what. That
// is why this sits after dropAction instead of up beside runNowIntent — it is
// the same shape of function, and it is defined below the one function it is
// only a re-reading of.
//
// THERE IS NO UNARCHIVE CONTROL, on either view. It used to compute a way back
// — Archive → In Progress — because an action whose only direction is away is a
// trap; the row never drew it (SHOW_UNARCHIVE) and the Board did, which is two
// answers to one question. Archive is a LOCKED lane now (see the matrix above),
// so there is one answer.
//
// And the way back is not missing, it is somewhere better: ACTIVITY. Say
// something in that conversation and the task comes back on its own — the
// server drops the filing when a message arrives after it, so the card rejoins
// the lane its thread puts it in rather than a lane a button guessed at. A
// gesture would have had to guess: "back in play" says nothing about whether
// the work is done.
//
// Nothing is destroyed either way. The conversation is kept, the transcript is
// kept (D306), and Archive is a place to read them.

export interface ArchiveIntent {
  /** The lane this move puts the card in: the same lane the Board's drop would
   * have targeted, which is what makes the two agree by construction. */
  lane: BoardColumn;
  /** The button's accessible name, and the word it says. */
  label: string;
  /** The tooltip: what happens, and the thing a person deleting would fear. */
  title: string;
}

/**
 * Whether this task can be filed away, and what that says. Null exactly when
 * the Board would refuse the same drop — a task already in Archive, and one
 * that is mid-run — because that is the question this asks.
 *
 * A never-run task DOES get the button now. Archiving is one verb over a task
 * key (`api.archiveTask`), not a triage write keyed by session id, so the
 * `pending:<entry>` row that had no session to file is filed by cancelling its
 * work — which is what archiving a task that has not run has always meant.
 */
export function archiveIntent(task: Task): ArchiveIntent | null {
  const lane: BoardColumn = "archived";
  // The one question, asked of the one function that already answers it.
  const action = dropAction(task, lane);
  if (!action || action.kind !== "archive") return null;
  return {
    lane,
    label: "Archive",
    // Three clauses because a person reaching for Delete is asking all three:
    // where does it go, what happens to work already booked, and can I get it
    // back. The last one is the honest answer to a lane with no way out of it.
    title:
      "Archive — files this away and calls off any run still booked; the conversation is kept, and a new message in it brings the task back",
  };
}

// ---- clearing a task, without opening it -------------------------------------
// Read state is per MESSAGE and that is the right model (§7) — but per message
// was also the only way to CLEAR it, so "I have seen all of this" was one click
// per row, each one navigating away into a transcript (Akshil, 2026-08-17: "in
// list you add mark as read button on the task right next to archive or
// something so you don't have to open everything individually").
//
// So the task row gets the whole-task verb, and the server gets ONE call for it
// (api.markWholeTaskRead). Nothing about the per-message path changes: clicking
// a message still opens the transcript at that turn and still marks only that
// one.
//
// Offered only on a task that HAS something unread. Unlike Archive — which is
// about a task's place and is always a sensible thing to ask — this one is a
// no-op the moment the count is zero, and a button that does nothing on most
// rows is what makes the ones that do matter hard to find. The count it asks is
// the DISPLAYED one (taskUnread, so local marks count), which is what lets the
// button remove itself on its own press instead of a poll later.

export interface MarkReadIntent {
  /** How many messages this clears — the number the tooltip says. */
  unread: number;
  /** The button's accessible name, and the word it says. */
  label: string;
  /** The tooltip: how much this clears, since the row shows only three of it. */
  title: string;
}

export function markReadIntent(
  task: Task,
  read: Set<string>,
  held?: TaskMessage[],
): MarkReadIntent | null {
  const unread = taskUnread(task, read, held);
  if (unread <= 0) return null;
  return {
    unread,
    label: "Mark read",
    // The number matters here in a way it does not on the other actions: the row
    // lists three messages and the count can be 89, so "all of them" has to say
    // how many it is about to be.
    title: unread === 1
      ? "Mark read — clears the 1 unread message in this task"
      : `Mark read — clears all ${unread} unread messages in this task`,
  };
}

// ---- opening a thread --------------------------------------------------------
// A gesture that opens the conversation used to open it and mark nothing: the
// card (or the row) carried an unread pill, the press took the reader into the
// very thread that pill was pointing at, and the pill was still there when they
// came back (Akshil, 2026-08-17: "when i click from kanban on unread task it
// should register it read correct?"). Yes.
//
// This is deliberately NOT the Board's rule. It is the rule for OPENING A
// THREAD, and both gestures that do that ask it: the Board card's click and the
// List row's "Open chat" button. They go to the same place (taskHref) by the
// same gesture, so they must come back with the same badge — a mark that
// depended on which view you happened to be in would be a coin toss, not a
// rule. Hence the view-neutral name: one function, one behaviour, two callers.
//
// WHOLE-TASK, not one message, and that follows from the href: both link
// taskHref — the thread, with no per-turn anchor — so what the reader is shown is
// the conversation, not one turn of it. That is precisely the case
// api.markWholeTaskRead exists for, and it is the same call the List row's Mark
// read button makes. There is no second way to mark read here.
//
// The two things are ORDERED but not coupled: the mark is a side effect of
// opening, so a thread with nothing unread still opens, and a failed write must
// not cost the navigation (callers fire and forget — the press is leaving the
// page, exactly as the per-message path already argued).
//
// What this does NOT cover, and must not: the List task ROW's own click, which
// toggles the accordion and opens nothing (there is no "you have seen it" to
// infer from expanding a row), and a MESSAGE click, which lands on its own turn
// and therefore marks that one message.

export interface OpenThreadIntent {
  /** Where the press goes. Never empty: a gesture with nowhere to go has no
   * intent at all, so the caller cannot navigate to null. */
  href: string;
  /** Whether opening this also clears the task's unread. */
  markRead: boolean;
}

/**
 * What opening this task's thread does, or null when it does NOTHING — which is
 * the `pending:<entry>` case: a task that has never run has no session id (§5)
 * and therefore no conversation to open. That press was inert before this change
 * and stays inert, including the mark: marking a thread read on a press that
 * showed the reader nothing would clear a badge for messages they never saw.
 *
 * `unread` defaults to the server's count and may be passed as the DISPLAYED one
 * (taskUnread, so local marks count), which is what stops a second press on an
 * already-cleared task from posting again.
 */
export function openThreadIntent(
  task: Task,
  unread: number = task.unread,
): OpenThreadIntent | null {
  const href = taskHref(task);
  if (!href) return null;
  return { href, markRead: unread > 0 };
}

// ---- filtering ---------------------------------------------------------------

export interface TaskFilters {
  search: string;
  statuses: BoardColumn[];
  /** Project FOLDERS, full paths — the value `Task.project` carries. */
  projects: string[];
}

export const EMPTY_FILTERS: TaskFilters = { search: "", statuses: [], projects: [] };

export function hasActiveFilters(f: TaskFilters): boolean {
  return f.statuses.length > 0 || f.projects.length > 0 || f.search.trim() !== "";
}

/**
 * Does the list a view is DRAWING span more than one project?
 *
 * Which is the whole of the folder chip's reason to exist. With the Project filter
 * pinned to one folder — or a search that happens to narrow to one — every visible
 * row repeats the same word, and a column of identical chips is noise on the busiest
 * edge of the row (Akshil, 2026-08-17: "this looks like a lot of information on the
 * right side... both the folder and the time with the date, they are like too much
 * for me to handle"). So the chip is drawn only when it DISTINGUISHES rows.
 *
 * Asked of the ROWS THEMSELVES, deliberately, not of the filter's value: a search
 * for "roadmap" that leaves three rows in one project is the same page to read as
 * the filter set to that project, and a rule that consulted the control would only
 * be right about one of the two. It also means the answer is right for a view that
 * has no filter control at all.
 *
 * An empty list is `false` — there is nothing to distinguish — which is the
 * harmless answer either way, since nothing is drawn.
 */
export function spansProjects(tasks: Task[]): boolean {
  const seen = new Set<string>();
  for (const t of tasks) {
    seen.add(t.project);
    if (seen.size > 1) return true;
  }
  return false;
}

/** The project filter's own choices — "auto-detected from the set of folders
 * that have tasks" (§10), sorted so the menu does not reshuffle when the task
 * order does. */
export function projectOptions(tasks: Task[]): string[] {
  const seen = new Set<string>();
  for (const t of tasks) if (t.project) seen.add(t.project);
  return [...seen].sort((a, b) => a.localeCompare(b));
}

export function taskMatches(task: Task, filters: TaskFilters): boolean {
  if (filters.statuses.length && !filters.statuses.includes(taskColumn(task)))
    return false;
  if (filters.projects.length && !filters.projects.includes(task.project)) return false;
  const q = filters.search.trim().toLowerCase();
  if (!q) return true;
  return (
    task.title.toLowerCase().includes(q) ||
    // TASK-002 is a designed, printed identifier — searching it is how a
    // person uses it.
    task.task_id.toLowerCase().includes(q) ||
    task.project.toLowerCase().includes(q) ||
    task.target.toLowerCase().includes(q) ||
    // The bodies we hold. A thread the user has not expanded is only searched
    // as far as its three newest, which is the same window the row shows.
    (task.messages ?? []).some((m) => m.body.toLowerCase().includes(q)) ||
    // A session uuid is never PRINTED (it reads as a second, competing ID
    // scheme beside TASK-n) but stays findable: someone holding one from a log
    // or a URL can paste it in and land on the row.
    task.session_id.toLowerCase().includes(q)
  );
}

/** Filter without reordering. The server sorted this list, newest task first,
 * and re-sorting it here is how two views start disagreeing about "newest". */
export function filterTasks(tasks: Task[], filters: TaskFilters): Task[] {
  return tasks.filter((t) => taskMatches(t, filters));
}

// ---- per-lane order (the Board's one exception) -------------------------------
// See the exception named at the top of this file. Two keys and three
// directions, all built out of times the server sent.

/** The earliest pending `at` among the messages the row is CARRYING. Null when
 * the window holds nothing pending.
 *
 * Every pending message counts, not just the ones with an `entry_id`:
 * runNowTarget needs that field because it is what the call SENDS, and this only
 * needs to know when the thing happens.
 *
 * On its own this is a BOUND rather than an answer — see nextRunAt, which is why
 * it is not exported. */
function windowNextRun(task: Task): number | null {
  let best: number | null = null;
  for (const m of task.messages ?? []) {
    if (m.state !== "pending" || !m.at) continue;
    if (best === null || m.at < best) best = m.at;
  }
  return best;
}

/**
 * When this task NEXT runs — the row's own `next_run` where it has one, and the
 * window's earliest pending where it does not. Null when neither names a run.
 *
 * `at`, not `ran_at`, and that is not a slip — a pending message has never run,
 * so its `ran_at` is 0 and `at` is the only time it has. It is also the same
 * instant runNowTarget fires (both prefer the same field, and both fall back to
 * the same window), so the card at the top of Upcoming is the card whose Run now
 * button sends the message the lane's order is promising.
 *
 * WHY THE FIELD, since the window looks like it should be enough. It is not, and
 * the old note here was wrong about why: it said pending messages "are due in the
 * future, which puts them at the head of that window". On this branch scheduling
 * into the PAST is allowed and catch-up is unbounded, so a pending message whose
 * `at` has gone by is an ordinary state — and it is exactly the work that should
 * be read first. `task.messages` is the three newest by `at` (server: tasks.py
 * `_row`, which merges every entry with the transcript's prompts, sorts ASCENDING
 * by `at` and keeps the tail), so an overdue pending is pushed out of it by three
 * messages with later `at` — two sent runs and next month's occurrence will do it
 * — and reading the window alone then answers the LATER pending: an upper bound
 * that sorted the buried work behind everything it should have led.
 *
 * `next_run` closes that: `min(at)` over every pending ENTRY, taken on the server
 * before the tail is cut, where the whole set is already in hand. The window stays
 * as the fallback for an older server, and for a task the field says nothing about.
 *
 * Widening `task.messages` was the other way and is still the wrong one — another
 * row of tail held per session, on every poll, for every row, to fix a minority.
 *
 * Guessing remains deliberately unattempted where neither source knows. The client
 * can tell that a window is truncated (`message_count`) but not whether anything
 * pending hides in the part it cannot see, and promoting every long thread on that
 * suspicion would put a task due in October above one firing in ten minutes.
 */
export function nextRunAt(task: Task): number | null {
  const named = namedNextRun(task);
  const held = windowNextRun(task);
  if (named === null) return held;
  // The window can only beat the field in one case, and it is a real one: the
  // server names the earliest pending entry it can also FIRE, so a pending entry
  // with no readable id is skipped there and still seen here. It is unfireable
  // either way, so ordering by it changes nothing about which message the button
  // sends — and the earlier of two times is the honest one to sort by.
  return held !== null && held < named.at ? held : named.at;
}

/**
 * Whether a lane time has already gone by — work that should have happened.
 *
 * `at` is epoch SECONDS (the API's unit) and `now` is milliseconds, which is the
 * one thing worth being careful about here. Null is not overdue: a task with no
 * time at all has nothing to be late for.
 */
export function isPastDue(when: number | null, now: number = Date.now()): boolean {
  return when !== null && when * 1000 <= now;
}

/** The states that mean the message never went out, so it dates no run. A
 * `missed` one is deliberately NOT here: it was due and the run did not happen,
 * which is the event the Failed lane exists to show, and its `at` is the closest
 * thing to a time it has. */
const NEVER_RAN = new Set<TaskMessage["state"]>(["pending", "cancelled", "skipped"]);

/**
 * When this task LAST ran — the newest run in the window, by when it actually
 * happened. Null when nothing in the window has run.
 *
 * `ran_at` first, `at` only as the fallback, because the two part company: a
 * caught-up run has an `at` from Thursday and a `ran_at` from Saturday (see
 * api.TaskMessage), and Done ordered by `at` would file Saturday's run two days
 * back among work that finished before it.
 *
 * A MAX over the window rather than the first non-pending element, for the same
 * reason: the server's list is newest-first by `at`, and the caught-up case is
 * exactly the case where that is not newest-first by `ran_at`. Taking element
 * zero would inherit the bug the fallback exists to fix.
 */
export function lastRunAt(task: Task): number | null {
  let best: number | null = null;
  for (const m of task.messages ?? []) {
    if (NEVER_RAN.has(m.state)) continue;
    const when = m.ran_at || m.at;
    if (!when) continue;
    if (best === null || when > best) best = when;
  }
  return best;
}

/**
 * How one lane is ordered. `server` means exactly that: leave the list as the
 * server sent it (`last_active` descending) and sort nothing.
 */
export interface LaneSort {
  key: "next-run" | "last-run" | "server";
  dir: "asc" | "desc";
  /**
   * Work that is ALREADY PAST DUE sorts ahead of work that is not, before the
   * direction below is consulted at all.
   *
   * Ascending order happens to put a past time first anyway, and that is the
   * reason to write this down rather than the reason not to: the promise "the
   * overdue run is at the top" was resting on a coincidence between two
   * independent decisions, and the first person to reconsider `dir` would have
   * broken it without touching a line that mentions overdue work. On this branch
   * an overdue pending is a normal state — past scheduling is allowed and
   * catch-up is unbounded — so it is the lane's headline case and it says so.
   *
   * Within the bucket the direction still applies, which is what puts the MOST
   * overdue first: that is the one the scheduler will send next.
   */
  overdueFirst?: boolean;
}

/**
 * Every lane's order, in one map, keyed off BoardColumn so a lane cannot be
 * added to the board and forgotten here.
 *
 *   upcoming     next run, ASCENDING — soonest first, and OVERDUE first of all.
 *                The user's ask, and the only ascending lane on the board.
 *   in_progress  last run, descending. The freshest work sits at the top like
 *                every other settled lane, and for a task that is RUNNING the
 *                last run is the one that started it, so this reads as "most
 *                recently started first".
 *   done         last run, descending — "the recent runs will be on top".
 *   failed       last run, descending, same question.
 *   archived     the server's. Nothing scans Archive by time-to-run, and it is
 *                the one lane whose contents are not about when anything runs —
 *                it holds cancelled and skipped messages, which have no run to
 *                date. `last_active` (which the server has and a truncated
 *                message window may not) is the better key there, so the honest
 *                move is to leave the server's own order alone.
 */
export const LANE_SORTS: Record<BoardColumn, LaneSort> = {
  upcoming: { key: "next-run", dir: "asc", overdueFirst: true },
  in_progress: { key: "last-run", dir: "desc" },
  done: { key: "last-run", dir: "desc" },
  failed: { key: "last-run", dir: "desc" },
  archived: { key: "server", dir: "asc" },
};

/** The instant a lane orders this task by, or null when the task has none of
 * it. Null is a real answer, not a zero: zero is 1970 and would sort at one end
 * of the lane by accident rather than by decision. */
export function laneTime(task: Task, lane: BoardColumn): number | null {
  switch (LANE_SORTS[lane].key) {
    case "next-run":
      return nextRunAt(task);
    case "last-run":
      return lastRunAt(task);
    default:
      return null;
  }
}

// ---- the time a task ROW prints ----------------------------------------------

export type TaskWhenKind = "next" | "last" | "active" | "none";

/** What a row prints when the task has no timestamp of any kind. An em dash and
 * not a blank: the time is the last cell of every row, so an empty one reads as a
 * broken row rather than as an absent fact — and the column has to hold its width
 * or the folder chips beside it stop lining up. */
export const NO_TIME = "—";

export interface TaskWhen {
  /** The instant, epoch seconds. 0 on `none`, which is the one kind that names no
   * instant at all — and is why nothing may format this without checking. */
  at: number;
  /** Which time it is — the row prints it, the tooltip says which. */
  kind: TaskWhenKind;
  /** What the row prints: ONE relative unit ("30m ago", "in 2h"), the same
   * vocabulary a message row's time speaks — relativeWhen. */
  text: string;
  /** The tooltip: which run, and the absolute stamp — "Monday" is not an answer
   * to "which Monday?" (messageStamp, same reason the message rows carry one). */
  title: string;
}

/**
 * The time a task ROW shows beside its folder, on every task (Akshil,
 * 2026-08-17: "let's show the time as well for like besides the folder. Let's do
 * that for every task") — until now a time was only visible on a message row,
 * which meant a one-message task with nothing to expand showed none at all.
 *
 * WHICH time is not a new policy. It is LANE_SORTS', the map that already decides
 * what each lane is a column of: the lane sorted by `next-run` (Upcoming) is the
 * lane whose reader wants to know when the work happens, and every lane sorted by
 * `last-run` wants to know when it did. Reading the answer off that map rather
 * than off a second list of column names is what keeps the row and the lane it
 * sits in from disagreeing — and it means Archive, whose order is deliberately the
 * server's, falls through to "when it last ran", which is the only run it has.
 *
 * WHAT IT PRINTS is one relative unit and nothing else (relativeWhen): the row
 * used to end in a clock AND a date AND a folder, which is three things to read on
 * every line (Akshil, 2026-08-17). The absolute instant moves into `title`, where
 * it also gains the word for WHICH run it is — the ink cannot say that in one unit
 * and does not try.
 *
 * The OTHER run is the first fallback, not a blank: an Upcoming task whose pending
 * message is outside the window still shows the run it already made, and a Done
 * task that also has a repeat coming still has a time to show.
 *
 * AND `last_active` IS THE THIRD, which is the bug this now closes. Both run times
 * are derived from the three-message WINDOW (nextRunAt reads `next_run` or that
 * window; lastRunAt reads only the window), so a task whose window is EMPTY had
 * neither and the row printed nothing at all — a hole in the last column of an
 * otherwise full list (Akshil, 2026-08-18, on TASK-044, a `/clear`).
 *
 * An empty window is not an exotic state. A task IS a Claude session, and a session
 * whose transcript surfaces no prompt — one that holds only a slash command like
 * `/clear` — is a real row with a real id, a real folder and no messages under it.
 * The server had the answer the whole time and on the very same row: `last_active`
 * is the session's own clock (routers/tasks.py — the transcript's activity, or the
 * newest entry's `created` when nothing has run), which is exactly "when did
 * anything last happen here". Reading it is one field, and it is the field the
 * server itself sorts the list by, so the row and its position now agree.
 *
 * The word for it is "Active", not "Last run": nothing ran, and saying it did would
 * be a confident wrong answer of the kind the `at === 0` guard below refuses.
 *
 * NOTHING RETURNS NULL any more. A task with no timestamp of any kind — every
 * source zero — gets `kind: "none"` and prints NO_TIME, because the alternative was
 * a blank last cell that reads as a broken row. `at` stays 0 there and `title` says
 * so in words: 0 formats as 1970, so the one thing this must never do is hand a
 * zero to a formatter, which is why `none` is a KIND rather than a stamp.
 */
export function taskWhen(task: Task, now: number = Date.now()): TaskWhen {
  const nextFirst = LANE_SORTS[taskColumn(task)].key === "next-run";
  const next = nextRunAt(task);
  const last = lastRunAt(task);
  const runs: [TaskWhenKind, number | null][] = nextFirst
    ? [["next", next], ["last", last]]
    : [["last", last], ["next", next]];
  // `|| null` on the third: `last_active` is a float that is 0.0 for "never", and
  // 0 must fall through to `none` rather than be formatted as 1970.
  const order: [TaskWhenKind, number | null][] = [
    ...runs,
    ["active", task.last_active || null],
  ];
  const WORD: Record<TaskWhenKind, string> = {
    next: "Next run",
    last: "Last run",
    active: "Active",
    none: "",
  };
  for (const [kind, at] of order) {
    if (at === null) continue;
    return {
      at,
      kind,
      text: relativeWhen(at, now),
      title: `${WORD[kind]} ${messageStamp(at)}`,
    };
  }
  return {
    at: 0,
    kind: "none",
    text: NO_TIME,
    title: "No recorded activity yet",
  };
}

/**
 * One lane, in its own order. A new array; the input is never mutated (it is a
 * slice of the polled list, which React is still holding).
 *
 * TWO rules make this safe to run every 20 seconds:
 *
 * 1. TIES KEEP THE SERVER'S ORDER, by comparing the incoming index explicitly
 *    rather than trusting the sort to be stable. Two tasks that ran in the same
 *    minute must not trade places between polls — a card that moves on its own
 *    is worse than any ordering, and the lane re-renders on every poll. (The
 *    index passed is the position within the lane, which orders the same way as
 *    the position in the server's full list, since bucketing preserves it.)
 *
 * 2. A TASK WITH NO USABLE TIME GOES LAST, in both directions, and lands there
 *    by decision rather than by whatever `null` would coerce to. It is the
 *    honest place: the lane is sorted by a fact this card does not have, so it
 *    cannot claim a place among the cards that do — and the top of a lane is the
 *    slot that means something. Among themselves those cards keep the server's
 *    order, by rule 1.
 *
 * And one rule that is about WHAT the lane is for rather than about the poll:
 *
 * 3. PAST DUE COMES FIRST, on the lane that asks for it (LaneSort.overdueFirst).
 *    Read `now` once for the whole lane rather than per comparison, so the
 *    comparator cannot change its mind halfway through a sort that straddles a
 *    second — a comparator that is not consistent is a comparator with no defined
 *    output.
 *
 * Rule 3 used to carry a limit, and it is gone: an overdue pending pushed out of
 * the three-message window left the lane sorting by the later run it could see, so
 * the most urgent card could sit mid-lane. The row now names its next run
 * (`next_run`, server-side `min(at)` over every pending entry — see nextRunAt) and
 * runNowTarget fires that same run, so a card promoted here is a card whose Run
 * now sends what the order promised. Against an older server the window is the
 * fallback and the old bound is what remains: late, never early.
 */
export function sortLane(
  tasks: Task[],
  lane: BoardColumn,
  now: number = Date.now(),
): Task[] {
  if (LANE_SORTS[lane].key === "server") return tasks;
  const { dir, overdueFirst } = LANE_SORTS[lane];
  const rows = tasks.map((task, index) => {
    const when = laneTime(task, lane);
    return { task, index, when, late: overdueFirst === true && isPastDue(when, now) };
  });
  rows.sort((a, b) => {
    // Ahead of the direction, not a special case of it.
    if (a.late !== b.late) return a.late ? -1 : 1;
    if (a.when === null || b.when === null) {
      // Exactly one of them has a time: the one that does comes first.
      if (a.when !== b.when) return a.when === null ? 1 : -1;
    } else if (a.when !== b.when) {
      return dir === "asc" ? a.when - b.when : b.when - a.when;
    }
    return a.index - b.index;
  });
  return rows.map((r) => r.task);
}

/**
 * The board's lanes, each in ITS OWN order (LANE_SORTS above). Seeded from
 * BOARD_COLUMNS so a lane cannot exist on the board and be missing from this
 * map — an empty lane must still be an empty lane, not an undefined one.
 *
 * The sort lives here rather than in the Board so that a lane's contents and a
 * lane's order are decided in the same breath, by one function, and the
 * component holds no rule about either. It is also why the List is unaffected:
 * the List never calls this.
 *
 * `now` is read once here and handed to every lane, so the whole board is sorted
 * against ONE instant: two lanes that disagreed about what "past due" means would
 * be two answers to the same question in one render.
 */
export function groupByColumn(
  tasks: Task[],
  now: number = Date.now(),
): Map<BoardColumn, Task[]> {
  const map = new Map<BoardColumn, Task[]>(
    BOARD_COLUMNS.map((c) => [c.key, [] as Task[]]),
  );
  for (const task of tasks) map.get(taskColumn(task))?.push(task);
  for (const col of BOARD_COLUMNS) {
    map.set(col.key, sortLane(map.get(col.key)!, col.key, now));
  }
  return map;
}

// ---- which lanes are rolled up -----------------------------------------------
// A lane is either an open column or a 52px rail. Two things decide which, in
// this order:
//
// AN EMPTY LANE IS ALWAYS ROLLED UP, AND NOTHING ABOUT IT IS REMEMBERED. That is
// the whole of the empty case, and it is deliberately not a choice the reader
// can make stick: a column with nothing in it has nothing to show, so opening
// one is a PEEK — "is there really nothing here?" — and a peek is answered and
// over. Left persistable, it is the one setting a reader would make once and
// then be given four empty outlined columns by, for weeks, on a board they use
// to see what is running.
//
// So the two states are stored in two different places, on purpose:
//
//   * `choices` — the reader's answer for a lane WITH CARDS IN IT. localStorage,
//     survives reloads, and is what `laneCollapsed` below reads.
//   * the peek — a lane opened while empty. Component state in TaskBoard,
//     survives nothing: not a reload, not a remount, and not the lane filling up
//     and draining again. `laneRolledUp` is where the two meet.
//
// The consequence worth stating, because it is the one a reader will notice: a
// lane they had OPEN drains, and it rolls up. The expanded choice is not
// honoured on the way down and it is not deleted either — it is simply not what
// an empty lane is asked. Cards arrive, and the lane opens again on the choice
// that was always there.
//
// For a lane with cards, two things decide, in this order:
//
//   1. What the reader last chose for THAT lane. Explicit choices are the only
//      thing stored, so a lane nobody has ever touched keeps following the rule
//      below forever rather than being frozen at whatever it looked like the
//      first time the page was opened.
//   2. Otherwise: open.
//
// Archive used to be hard-coded closed. It is not special any more (Akshil,
// 2026-08-18) — an Archive with cards in it is a column like the others, and an
// Archive with none rolls up like the others.

/** The key the board's lane choices live under. Distinct from the array-shaped
 * key an earlier build wrote: that one recorded "collapsed now", defaults
 * included, which cannot be told apart from "the reader chose this". */
export const LANE_CHOICE_KEY = "fused-render:scheduled-board-lanes";

/** Lane → the reader's own answer to "collapsed?". Absent ⇒ never chosen. */
export type LaneChoices = Partial<Record<BoardColumn, boolean>>;

/** Same contract as parseListMemory: a stored string is untrusted input, and an
 * unreadable one means "no choices yet", never a thrown render. */
export function parseLaneChoices(raw: string | null): LaneChoices {
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
  const row = parsed as Record<string, unknown>;
  const out: LaneChoices = {};
  for (const col of BOARD_COLUMNS) {
    const v = row[col.key];
    if (typeof v === "boolean") out[col.key] = v;
  }
  return out;
}

/**
 * The PERSISTENT answer for one lane: what it looks like on a fresh render, with
 * nothing but the store to go on. `count` is how many cards it holds.
 *
 * Empty short-circuits, and that is the whole point — a stored choice is never
 * consulted for a lane with nothing in it, so nothing a reader does to an empty
 * lane can outlive the sitting, and a lane that drains reverts here rather than
 * honouring what it was set to when it still had work in it.
 */
export function laneCollapsed(
  lane: BoardColumn,
  count: number,
  choices: LaneChoices,
): boolean {
  if (count === 0) return true;
  const chosen = choices[lane];
  if (chosen !== undefined) return chosen;
  return false;
}

/**
 * What the board actually draws — `laneCollapsed` plus this sitting's peeks.
 *
 * `peeked` is the set of lanes the reader has opened WHILE EMPTY (TaskBoard
 * holds it in component state and persists none of it). It is consulted only in
 * the empty case: a peek is an answer to "is there really nothing here?", and it
 * has nothing to say about a lane that has cards. That is also what keeps a
 * stale peek from reopening a lane that filled up and drained again — the peek
 * is ignored for the whole time the lane has cards, and TaskBoard drops it in
 * that window.
 */
export function laneRolledUp(
  lane: BoardColumn,
  count: number,
  choices: LaneChoices,
  peeked: ReadonlySet<BoardColumn>,
): boolean {
  if (count === 0) return !peeked.has(lane);
  return laneCollapsed(lane, count, choices);
}

// ---- the List's order --------------------------------------------------------
// The List and the Board read the same fact — `taskColumn`, so a row that moves
// lane on the Board moves position here — but the List spends it as a SORT and
// nothing else (Akshil, 2026-08-18). No headers, no dividers, no counts: one
// flat list whose rows happen to arrive in status order.
//
// IT WAS HEADERS FIRST, and they were wrong on a list. The Board's lanes are a
// fixed frame, so a lane header is the frame's label and earns its ink; a list
// has no frame, so five headers over a few dozen rows are five interruptions in
// the one column a person is scanning — and the ORDER already says everything
// the headers were saying. Grouping you can see is a claim that the groups are
// navigable; grouping you can only feel is a claim about priority, which is what
// this actually is.
//
// THE ORDER IS NOT THE BOARD'S, and that difference stays. A board is read left
// to right as a pipeline (Upcoming, In Progress, Done, Failed, Archive); a list
// is read top to bottom as work owed, so the ranks that want a person's hands
// come first and the settled ones sink:
//
//   Upcoming → In Progress → Failed → Done → Archive
//
// Failed above Done because a broken run is still owed, and Done above Archive
// because Archive is not a status, it is where things go to stop being read.
//
// WITHIN a rank nothing is re-sorted: the server's order is the list's order and
// always has been (TaskList takes `tasks` "in the SERVER's order"). A stable
// bucketing keeps it, which is why this is a bucket-and-concat rather than a
// comparator — `Array.prototype.sort` is stable in every engine this ships to,
// but a comparator would also invite a second sort key later, and there is not
// one: rank, then whatever the server said.

/** Rank order, top to bottom. Every BoardColumn appears exactly once — the test
 * holds it to that, so a sixth lane cannot be silently unsortable. */
export const LIST_ORDER: BoardColumn[] = [
  "upcoming",
  "in_progress",
  "failed",
  "done",
  "archived",
];

/** The list's rows, in rank order, server order preserved inside each rank. A
 * new array; the input is never mutated (it is the polled list, which React is
 * still holding). */
export function sortByLane(tasks: Task[]): Task[] {
  const buckets = new Map<BoardColumn, Task[]>(
    LIST_ORDER.map((key) => [key, [] as Task[]]),
  );
  for (const task of tasks) buckets.get(taskColumn(task))?.push(task);
  return LIST_ORDER.flatMap((key) => buckets.get(key) ?? []);
}

// ---- "and it runs again on Tuesday" ------------------------------------------
// A recurring task whose last run finished sits in DONE now, not Upcoming: the
// output nobody has read is the thing that needs eyes, and a promise is not a
// verdict (server routers/tasks.py `_message_verdict`). That is the right lane
// and it drops one true fact off the row — that the task is not over.
//
// So the row says it. A CHIP, not a second time column: `taskWhen` already owns
// the row's one time and, on a settled task, that time is the last run. This is
// the other one, marked as such, in the same vocabulary (relativeWhen) with the
// absolute instant in the tooltip like every other time on the page.

export interface NextRunChip {
  /** Epoch seconds, so a caller can order or test by it. */
  at: number;
  /** What the chip prints: "next in 2h". */
  text: string;
  /** The tooltip: which run, and exactly when. */
  title: string;
}

/**
 * The next run worth mentioning ON TOP of the row's own time, or null.
 *
 * Two conditions, and both are about not saying the same thing twice:
 *
 *   * the row's time is NOT already the next run (`taskWhen`, which reads
 *     LANE_SORTS: Upcoming's rows are ordered and stamped by the run ahead, so
 *     a chip there would repeat the number beside it);
 *   * there IS a run ahead — `nextRunAt`, strictly in the future. A pending
 *     message whose time has passed is not news about what happens next, it is
 *     the overdue work the Upcoming lane already surfaces.
 */
export function nextRunChip(task: Task, now: number = Date.now()): NextRunChip | null {
  if (taskWhen(task, now).kind === "next") return null;
  const at = nextRunAt(task);
  if (at === null || at * 1000 <= now) return null;
  return {
    at,
    text: `next ${relativeWhen(at, now)}`,
    title: `Next run ${messageStamp(at)}`,
  };
}

// ---- what is happening RIGHT NOW ---------------------------------------------
// The List has the In Progress section and the Board has the In Progress lane;
// the calendar has neither, because a calendar is ordered by time and not by
// state. Its chips sat in the grid saying nothing about which of them was
// running at that moment — the one fact a person glancing at today most wants.
//
// So the chip gets the fact, and the RULE lives here rather than in the view:
// it is the same reading of `state` and `turn` the server's `_message_running`
// makes, and the same one messageTone collapses into `in_progress`. Three views
// asking three questions about "is this going?" is how they start disagreeing.

/** Is this message's own run in flight? `sending` is a send the scheduler has
 * spawned and not heard back from; `sent` with a turn that has not reported an
 * end is a turn still working (turnPhase — `unknown` is NOT running, it is a
 * watcher that stopped being able to tell). */
export function isMessageRunning(m: TaskMessage): boolean {
  if (m.state === "sending") return true;
  return m.state === "sent" && turnPhase(m.turn) === "running";
}

/**
 * Is THIS message the work this task is doing right now?
 *
 * Two ways, and the second is what a live chat turn needs — including the one
 * a live TRANSCRIPT cannot see. A message can say so itself (above), and
 * otherwise the newest message borrows the task's own verdict: `taskColumn`
 * reads `task.status`, which the server derives in `_status` from THREE
 * independent signals (`_message_running`, `live`, and `schedule.busy_sessions`)
 * — not just the two (`state`/`turn`, `task.live`) this function could see on
 * its own. A `sent` message whose turn the server has already rewritten to
 * `idle` still files the task `in_progress` while a scheduled send is in
 * flight (`busy_sessions`); asking `taskColumn` instead of re-deriving that
 * third signal here is what keeps the calendar chip agreeing with the List
 * and Board, which read the same `task.status`. Older messages in an
 * in-progress task are not running: their turns ended when the next prompt
 * arrived.
 */
export function isRunningNow(task: Task, m: TaskMessage): boolean {
  if (isMessageRunning(m)) return true;
  if (taskColumn(task) !== "in_progress") return false;
  const newest = task.messages?.[0];
  return !!newest && !!m.message_id && m.message_id === newest.message_id;
}

// ---- the sidebar's two-number summary of this page ----------------------------
// The Tasks entry in the global sidebar has to say two things without being the
// page: something is RUNNING, and something FINISHED that nobody has looked at.
// Both are read off the very rows the page draws (`taskColumn` — the server's
// status, narrowed, so the sidebar cannot invent a sixth state), never off a
// second endpoint of their own.
//
// WHY "done and unread" IS NOT JUST "unread". Unread exists on rows that are
// still going and on rows nobody ever expected to read; the signal being asked
// for here is the completion of work the reader was waiting on, which is exactly
// `done` + `unread > 0`. `failed` is deliberately NOT counted: it is a status of
// its own on this page (see taskColumn), and a green "go and look" mark over a
// run that broke would be the one place in the app where a hue disagreed with
// the ring the row wears (design-principles §1).

/** Where the sidebar's dismissal lives. Per COMPLETION, not per task — see
 *  TasksSeen. */
export const TASKS_SEEN_KEY = "fused-render:tasks-seen";

/**
 * Which completions the reader has already been shown, as `task key ->
 * last_active`.
 *
 * The value is what makes "the same completions" a checkable claim. A bare set
 * of keys would dismiss a task FOREVER — the second time it ran and finished,
 * the mark it earned would be swallowed by the first visit's dismissal. The
 * task's `last_active` moves with every new message, so a stored stamp that no
 * longer matches means "this is a different completion" and the mark comes back.
 *
 * A stamp rather than a global watermark for the same reason from the other
 * side: catch-up runs can finish out of order (Scheduled.tsx's docstring — work
 * that came due while the app was closed runs when it opens), and one
 * high-water mark would silently swallow every completion stamped before it.
 */
export type TasksSeen = Record<string, number>;

/** What came out of localStorage is a string written by SOMEONE ELSE (an older
 *  build, a hand-edited devtools row): anything unreadable degrades to "nothing
 *  dismissed" — one extra dot — rather than throwing inside a render. */
export function parseTasksSeen(raw: string | null): TasksSeen {
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
  const out: TasksSeen = {};
  for (const [key, at] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof at === "number" && Number.isFinite(at)) out[key] = at;
  }
  return out;
}

/** Has this task finished work nobody has looked at yet, given what has already
 *  been dismissed? */
export function isDoneUnread(task: Task, seen: TasksSeen): boolean {
  if (taskColumn(task) !== "done") return false;
  if (!(task.unread > 0)) return false;
  return seen[task.key] !== task.last_active;
}

export interface TasksPulse {
  /** Tasks whose work is in flight — the yellow half. */
  running: number;
  /** Tasks that finished and have not been looked at — the green half. */
  doneUnread: number;
}

export const EMPTY_TASKS_PULSE: TasksPulse = { running: 0, doneUnread: 0 };

/** The whole sidebar signal, from the rows the page already has. */
export function tasksPulse(tasks: Task[], seen: TasksSeen): TasksPulse {
  let running = 0;
  let doneUnread = 0;
  for (const t of tasks) {
    if (taskColumn(t) === "in_progress") running++;
    else if (isDoneUnread(t, seen)) doneUnread++;
  }
  return { running, doneUnread };
}

export function samePulse(a: TasksPulse, b: TasksPulse): boolean {
  return a.running === b.running && a.doneUnread === b.doneUnread;
}

/**
 * The dismissal a visit to /tasks earns: every DONE task on screen, stamped
 * with the completion that was on screen.
 *
 * Done tasks only, and only the ones in this answer — which is also the pruning
 * rule, so the row cannot grow without bound as tasks come and go. A running
 * task is deliberately not stamped: its completion has not happened yet, and
 * pre-dismissing it is how the one mark this feature exists for would never be
 * drawn.
 */
export function seenAfterVisit(tasks: Task[]): TasksSeen {
  const next: TasksSeen = {};
  for (const t of tasks) {
    if (taskColumn(t) === "done") next[t.key] = t.last_active;
  }
  return next;
}

export function sameSeen(a: TasksSeen, b: TasksSeen): boolean {
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((k) => a[k] === b[k]);
}

/** "2 running" — the expanded row's yellow readout. Plural because one is the
 *  common case and "1 running" is what a person would say out loud. */
export function runningLabel(n: number): string {
  return `${n} running`;
}

/** The collapsed dot's tooltip, and the expanded chip's — the sidebar's ONE
 *  sentence about the page, so the two modes cannot describe it differently. */
export function pulseTitle(pulse: TasksPulse): string {
  const parts: string[] = [];
  if (pulse.running > 0) parts.push(runningLabel(pulse.running));
  if (pulse.doneUnread > 0) parts.push(`${pulse.doneUnread} finished, not read`);
  return parts.join(" · ");
}
