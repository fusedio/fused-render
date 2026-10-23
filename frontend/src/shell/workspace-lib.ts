// The Workspace view's decisions, pure (Workspace scope, unit 6).
//
// Everything the view CHOOSES lives here, away from the DOM: which rows it shows
// and in what order, what each row says holds it open, what a key means. None of
// it is shared with the other views on purpose. `sortForList`, `taskColumn` and
// the status-filter defaults keep today's behaviour, and this module is what
// stops a Workspace rule from leaking into them.
import type { Task, TaskHold } from "@platform/lib/api";
import type { PrState } from "@platform/lib/prStatus";
import { prKey, prRefsOf } from "@platform/lib/task-prs";
import { isDraftTask, usageLimitCaption } from "@shell/tasks-lib";

/** What the rows' markers need beyond the task itself. */
export interface WorkspaceFacts {
  /** Recorded-worktree git state by task key (`/api/tasks/holds`); a task with
   *  no worktree is absent. */
  holds: Record<string, TaskHold[]>;
  /** Has a holds answer arrived for the rows on screen? Until it has, an
   *  otherwise clear task must not be called clear. */
  holdsReady: boolean;
  /** The last holds read failed. */
  holdsFailed?: boolean;
  /** `usePrStatuses`: `owner/repo#n` to state (null: GitHub did not say). */
  prStates: Record<string, PrState | null | undefined>;
}

/** Every task that is not closed, newest first by CREATION time (`started`).
 *  Creation never changes, so a row never moves when its status does. */
export function workspaceTasks(tasks: readonly Task[]): Task[] {
  return tasks
    .filter((t) => t.status !== "archived" && !isDraftTask(t))
    .sort((a, b) => (b.started ?? 0) - (a.started ?? 0) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
}

/** The reader has to look: parked on a card, or stopped (failed, usage limit). */
export function needsYou(task: Pick<Task, "status">): boolean {
  return task.status === "needs_attention" || task.status === "blocked";
}

/** The status in today's words. */
export function statusWord(task: Task): string {
  switch (task.status) {
    case "upcoming":
      return "Upcoming";
    case "queued":
      return "Queued";
    case "in_progress":
      return "Running";
    case "needs_attention":
      return "Needs attention";
    case "blocked":
      if (task.blocked_reason === "usage_limit") return usageLimitCaption(task) || "Usage limit";
      if (task.blocked_reason === "failed") return "Failed";
      return "Blocked";
    case "archived":
      return "Archived";
    default:
      return "Done";
  }
}

const count = (n: number, one: string, many: string): string => `${n} ${n === 1 ? one : many}`;

/** Why this task cannot simply be closed, in a fixed order; [] means clear.
 *  Informational only: nothing here ever acts or blocks. */
export function holdReasons(task: Task, facts: WorkspaceFacts): string[] {
  const out: string[] = [];
  if (task.status === "in_progress") out.push("running");
  else if (needsYou(task)) out.push("needs you");
  else if (task.status === "queued") out.push("queued");
  if (task.unread > 0) out.push(`${task.unread} unread`);
  for (const ref of prRefsOf(task)) {
    const state = facts.prStates[prKey(ref)];
    if (state === "open" || state === "draft") out.push(`PR #${ref.number} ${state}`);
    else if (state !== "merged" && state !== "closed") out.push(`PR #${ref.number} unknown`);
  }
  const bg = task.background_jobs?.count ?? 0;
  if (bg > 0) out.push(count(bg, "bg job", "bg jobs"));
  if ((task.next_run ?? 0) > 0) out.push("run pending");

  const rows = facts.holds[task.key] ?? [];
  let dirty = 0;
  let unpushed = 0;
  let unknown = false;
  for (const w of rows) {
    if (w.dirty_files === null || w.unpushed === null || w.reasons.includes("unknown")) unknown = true;
    dirty += w.dirty_files ?? 0;
    unpushed += w.unpushed ?? 0;
  }
  if (dirty > 0) out.push(`${dirty} uncommitted`);
  if (unpushed > 0) out.push(`${unpushed} unpushed`);
  if (unknown) out.push("unknown");
  if (!facts.holdsReady && rows.length === 0) out.push(facts.holdsFailed ? "unknown" : "checking");
  return out;
}

/** The marker text: the reasons joined by a middle dot, or "clear". */
export function holdMarker(reasons: readonly string[]): string {
  return reasons.length ? reasons.join(" · ") : "clear";
}

/** j / k: one row down or up, clamped. With no selection (or one that has gone)
 *  down takes the first row and up the last. */
export function moveSelection(rows: readonly Task[], key: string | null, delta: 1 | -1): string | null {
  if (rows.length === 0) return null;
  const at = key === null ? -1 : rows.findIndex((t) => t.key === key);
  if (at < 0) return rows[delta > 0 ? 0 : rows.length - 1].key;
  return rows[Math.min(rows.length - 1, Math.max(0, at + delta))].key;
}

/** n: the next row after the selection that needs the reader, wrapping. */
export function nextNeedingYou(rows: readonly Task[], key: string | null): string | null {
  const at = key === null ? -1 : rows.findIndex((t) => t.key === key);
  for (let i = 1; i <= rows.length; i++) {
    const t = rows[(at + i) % rows.length];
    if (needsYou(t)) return t.key;
  }
  return null;
}

const norm = (s: string): string => s.toLowerCase().replace(/[\s-]+/g, "");

/** ⌘K / `/`: every task, open or closed, by title or T-number. Open ones first
 *  and closed ones after, each group newest first. Drafts are never results. */
export function searchTasks(tasks: readonly Task[], query: string): Task[] {
  const q = norm(query);
  const hit = (t: Task): boolean =>
    q === "" || norm(t.title).includes(q) || (t.task_id !== "" && norm(t.task_id).includes(q));
  const real = tasks.filter((t) => !isDraftTask(t) && hit(t));
  const byNew = (a: Task, b: Task): number => (b.started ?? 0) - (a.started ?? 0);
  const open = real.filter((t) => t.status !== "archived").sort(byNew);
  const closed = real.filter((t) => t.status === "archived").sort(byNew);
  return [...open, ...closed];
}

export type WorkspaceAction = "next" | "prev" | "open" | "close" | "new" | "search" | "needs" | "escape";

export interface KeyLike {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

/** What a key press means on the Workspace view, or null when it is not ours. */
export function workspaceKeyAction(
  e: KeyLike,
  ctx: { typing: boolean; paletteOpen: boolean },
): WorkspaceAction | null {
  if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k") return "search";
  if (ctx.typing || ctx.paletteOpen) return null;
  if (e.metaKey || e.ctrlKey || e.altKey) return null;
  switch (e.key) {
    case "j":
      return "next";
    case "k":
      return "prev";
    case "Enter":
      return "open";
    case "x":
      return "close";
    case "c":
      return "new";
    case "/":
      return "search";
    case "n":
      return "needs";
    case "Escape":
      return "escape";
    default:
      return null;
  }
}
