// The Tasks page's pure half — everything the List accordion and the Board
// decide, with no DOM and no React in it, so the rules are testable on their
// own (shell/tasks-lib.test.ts) and the components are left holding markup.
//
// The model is docs/superpowers/specs/2026-08-16-tasks-threads-messages-design.md:
//
//   PROJECT (a folder)
//   └─ TASK-002        one Claude session, one thread
//      ├─ MSG-003      newest first
//      ├─ MSG-002
//      └─ MSG-001
//
// The server hands us that shape already merged, already titled, already
// counted and already sorted (newest task first). Nothing here re-derives a
// status, re-orders a list or re-titles a row: the one place those are decided
// is the server, and a client that guesses a second answer is a client that
// disagrees with itself on the next poll.
import type { Task, TaskMessage } from "@platform/lib/api";
import { BOARD_COLUMNS, explorerUrl, isProjected, turnPhase } from "./schedule-lib";
import type { BoardColumn } from "./schedule-lib";

// How many messages a collapsed-then-expanded task shows before Show more.
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
 * The second half of a scheduled message's time, printed only when there IS a
 * second half: `at` is what was asked for and never moves, `ran_at` is when the
 * turn actually started, and they part company two ways —
 *
 *   * late, when the app was shut at the due minute and the run was caught up,
 *   * early, when someone dragged the task into In Progress and ran it now.
 *
 * The early case is the whole reason run-now leaves `due` alone (§2 of this
 * round's fixes): the schedule keeps saying 09:00 and the row says it ran at
 * 07:12, which is the truth. Rewriting `due` to the moment of the drag would
 * have made the row read as if 07:12 had always been the plan.
 */
export function ranNote(m: TaskMessage, now: number = Date.now()): string {
  if (!m.at || !m.ran_at) return "";
  if (Math.abs(m.ran_at - m.at) < RAN_SKEW_SECONDS) return "";
  return `ran ${messageTime(m.ran_at, now)}`;
}

/** The time cell's tooltip: the absolute stamp, and the run beside it when the
 * two differ. */
export function messageWhenTitle(m: TaskMessage, now: number = Date.now()): string {
  const due = messageStamp(m.at);
  if (!ranNote(m, now)) return due;
  return `Scheduled for ${due} · ran ${messageStamp(m.ran_at)}`;
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
 */
export function taskUnread(
  task: Task,
  read: Set<string>,
  loaded?: TaskMessage[],
): number {
  const known = loaded ?? task.messages ?? [];
  const cleared = known.filter(
    (m) => m.unread && read.has(readKey(task.key, m.message_id)),
  ).length;
  return Math.max(0, task.unread - cleared);
}

/**
 * What the LEADING slot of a TASK row draws — the counterpart of unreadMarker,
 * in the same column.
 *
 * The dot moved to the head of every MESSAGE row last round and the task's own
 * unread did not move with it: it stayed a pill at the far right of the row,
 * beside the folder chip, while the dots of its own thread led theirs. One
 * marker in two places, and a rail that started halfway down the task (Akshil,
 * 2026-08-17: "for the tasks you didn't bring this on the left side, only for
 * the messages you brought. This looks odd").
 *
 * So it leads too. A count cannot be a bare dot, so the DOT CARRIES THE NUMBER
 * — same hue, filled, grown from a circle into a short pill as the digits need
 * it, and centred on the very column the message dots sit in, so the rail is
 * straight whatever it says. Past the cap it prints "99+": the pill is a
 * marker, not a readout, and a three-digit number in a 16px slot is neither.
 *
 * `label` is the accessible name and the tooltip, and it always carries the
 * TRUE count — the cap is a drawing decision, not a rounding of the fact.
 */
export const UNREAD_COUNT_CAP = 99;

export interface UnreadCount {
  /** What the pill prints. */
  text: string;
  /** The accessible name / tooltip: the real number, uncapped. */
  label: string;
}

/** The task row's leading count, or null when there is nothing unread — in
 * which case the slot is still drawn, empty, exactly as a read message's is. */
export function unreadCount(count: number): UnreadCount | null {
  if (count <= 0) return null;
  return {
    text: count > UNREAD_COUNT_CAP ? `${UNREAD_COUNT_CAP}+` : String(count),
    label: `${count} unread`,
  };
}

// ---- the accordion -----------------------------------------------------------

export interface ThreadView {
  /** What the expanded task actually lists, newest first. */
  messages: TaskMessage[];
  /** Whether Show more is on offer — i.e. the thread has more than we hold. */
  more: boolean;
  /** How many are still unlisted, for the button's own wording. */
  hidden: number;
}

/**
 * What an expanded task shows.
 *
 * Before Show more that is `task.messages` — the three newest, already ordered
 * by the server. After it, the full thread REPLACES those three rather than
 * appending to them, so a message can never appear twice.
 *
 * `message_count` is the server's total, and the honest source for "is there
 * more?": the preview list alone cannot tell a thread of exactly three from a
 * thread of three hundred.
 */
export function threadView(task: Task, loaded?: TaskMessage[]): ThreadView {
  if (loaded) return { messages: loaded, more: false, hidden: 0 };
  const messages = (task.messages ?? []).slice(0, PREVIEW_MESSAGES);
  const hidden = Math.max(task.message_count - messages.length, 0);
  return { messages, more: hidden > 0, hidden };
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

// ---- drag --------------------------------------------------------------------
// A drop on the Board means one of TWO things, and which one is decided by
// where the card came from.
//
// 1. TRIAGE. Every other move writes triage.json through setSessionTriage,
//    which is keyed by SESSION. A task that has not run has no session id (§5)
//    — there is nothing to triage — so it must be non-draggable rather than
//    draggable into a call that can only fail.
//
// 2. RUN IT NOW. Upcoming → In Progress is not a filing decision, it is an
//    instruction (Akshil, 2026-08-16: "if I move a task from upcoming to in
//    progress, don't change the time of it, but run it and run it at that
//    point"). The server sends the pending message immediately and leaves its
//    `due` alone, so the row goes on reading as the time it was MEANT to run
//    and the thread honestly shows a run that happened early.
//
// The consequence for legality: a run-now drop needs a MESSAGE to fire, not a
// session to file under, so a scheduled task that has never run — no session
// id at all — may still be dragged into In Progress. And a pure-chat task,
// which has nothing pending anywhere in it, may not: that drop is refused by
// dropLanes before the card ever lifts, rather than by the server after it
// lands.
//
// FAILED, the fifth lane, is asymmetric on purpose:
//
//   * nothing may be dropped INTO it. Failure is something that HAPPENED, not
//     a verdict a person hands down, and a lane you can drag a healthy task
//     into is a lane whose count means nothing.
//   * out of it there are exactly two moves — In Progress re-runs the task
//     (the same run-now path, with the same precondition: something pending to
//     fire), and Archive files it away. Done is deliberately not offered: a run
//     that broke did not finish, and letting it be filed as Done would put the
//     lie back in the one place this lane exists to take it out of.

/** The lanes a person may drop a card ON. `failed` is not among them — see
 * above — and `upcoming` never was: a task cannot be un-run. */
export const TRIAGE_LANES: BoardColumn[] = ["in_progress", "done", "archived"];

/** Where a card may go once it is IN the failed lane. */
const OUT_OF_FAILED: BoardColumn[] = ["in_progress", "archived"];

/**
 * Which pending message a run-now drop fires: the EARLIEST due.
 *
 * A task can hold several pending messages — a recurring rule's next
 * occurrence sitting beside a one-off someone scheduled for Friday. The
 * earliest is the one the scheduler itself would have sent next, so running it
 * early is the only choice that does not reorder the thread: any other pick
 * would fire a later message first and leave an older one still pending behind
 * it. On an exact tie the OLDER message wins (the server's list is newest
 * first, so the later element of a tie is the one that has waited longer).
 *
 * Only what we HOLD is considered — the listing carries the three newest, and
 * pending messages are due in the future, which puts them at the head of that
 * window. `entry_id` is required because it is what the call actually sends;
 * a message without one cannot be fired.
 */
export function runNowTarget(task: Task): TaskMessage | null {
  let best: TaskMessage | null = null;
  for (const m of task.messages ?? []) {
    if (m.state !== "pending" || !m.entry_id) continue;
    if (!best || m.at <= best.at) best = m;
  }
  return best;
}

/** Whether this task has anything to run early at all. */
export function canRunNow(task: Task): boolean {
  return runNowTarget(task) !== null;
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
    entryId: m.entry_id,
    messageId: m.message_id,
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

/** Which lanes this card may be dropped on. Empty ⇒ do not let it lift. */
export function dropLanes(task: Task): BoardColumn[] {
  const here = taskColumn(task);
  const lanes: BoardColumn[] = [];
  for (const lane of TRIAGE_LANES) {
    if (lane === here) continue;
    if (here === "failed" && !OUT_OF_FAILED.includes(lane)) continue;
    // The run-now lane. Its precondition is a pending message, NOT a session:
    // see the note above. It is the same move from either side — Upcoming runs
    // the message early, Failed runs it again — and the same call makes both.
    if (lane === "in_progress" && (here === "upcoming" || here === "failed")) {
      if (canRunNow(task)) lanes.push(lane);
      continue;
    }
    if (task.session_id) lanes.push(lane);
  }
  return lanes;
}

export function isDraggable(task: Task): boolean {
  return dropLanes(task).length > 0;
}

/** setSessionTriage's own union — a lane that is not one of the three cannot
 * be sent, and this is what proves it to the type checker at the call site. */
export function triageStatus(
  lane: BoardColumn,
): "in_progress" | "done" | "archived" | null {
  return lane === "in_progress" || lane === "done" || lane === "archived" ? lane : null;
}

/**
 * What a drop on `lane` actually DOES — the one place the two meanings are told
 * apart, so the Board's handler holds no rule of its own beyond which call to
 * make. Null when the drop is illegal, which is the same answer dropLanes gave
 * before the card lifted: the two agree because this asks it.
 */
export type DropAction =
  | { kind: "run"; entryId: string; messageId: string }
  | { kind: "triage"; status: "in_progress" | "done" | "archived" };

export function dropAction(task: Task, lane: BoardColumn): DropAction | null {
  if (!dropLanes(task).includes(lane)) return null;
  const here = taskColumn(task);
  if (lane === "in_progress" && (here === "upcoming" || here === "failed")) {
    const m = runNowTarget(task);
    return m ? { kind: "run", entryId: m.entry_id, messageId: m.message_id } : null;
  }
  const status = triageStatus(lane);
  if (!status || !task.session_id) return null;
  return { kind: "triage", status };
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

/** The board's lanes, each keeping the server's order. Seeded from
 * BOARD_COLUMNS so a lane cannot exist on the board and be missing from this
 * map — an empty lane must still be an empty lane, not an undefined one. */
export function groupByColumn(tasks: Task[]): Map<BoardColumn, Task[]> {
  const map = new Map<BoardColumn, Task[]>(
    BOARD_COLUMNS.map((c) => [c.key, [] as Task[]]),
  );
  for (const task of tasks) map.get(taskColumn(task))?.push(task);
  return map;
}
