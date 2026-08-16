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
import { explorerUrl } from "./schedule-lib";
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
      // `sent` only means the turn STARTED. How it ended is the second fact.
      if (m.turn === "unknown")
        // Not "Running…": nothing is watching it any more, and saying
        // otherwise is the frozen-progress-bar lie.
        return { column: "done", failed: true, label: "Stopped reporting" };
      if (m.turn === "done" || m.turn === "idle")
        return { column: "done", failed: false, label: "Ran" };
      return { column: "in_progress", failed: false, label: "Running…" };
  }
}

/** The task's own column, narrowed. The server decides it (Task.status); this
 * only keeps a value a newer server invented off the board's floor. An
 * unreadable status lands in Done, not Archive: a task the client cannot read
 * still HAPPENED, and filing it away hides it behind a collapsed lane. */
export function taskColumn(task: Task): BoardColumn {
  const s = task.status;
  if (s === "upcoming" || s === "in_progress" || s === "done" || s === "archived")
    return s;
  return "done";
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
// The Board's drag writes triage.json through setSessionTriage, which is keyed
// by SESSION. A task that has not run has no session id (§5) — there is
// nothing to triage — so it must be non-draggable rather than draggable into a
// call that can only fail.

export const TRIAGE_LANES: BoardColumn[] = ["in_progress", "done", "archived"];

/** Which lanes this card may be dropped on. Empty ⇒ do not let it lift. */
export function dropLanes(task: Task): BoardColumn[] {
  if (!task.session_id) return [];
  const here = taskColumn(task);
  return TRIAGE_LANES.filter((lane) => lane !== here);
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

/** The board's lanes, each keeping the server's order. */
export function groupByColumn(tasks: Task[]): Map<BoardColumn, Task[]> {
  const map = new Map<BoardColumn, Task[]>([
    ["upcoming", []],
    ["in_progress", []],
    ["done", []],
    ["archived", []],
  ]);
  for (const task of tasks) map.get(taskColumn(task))?.push(task);
  return map;
}
