// The Workspace view's decisions, pure (Workspace scope, unit 6).
//
// Everything the view CHOOSES lives here, away from the DOM: which rows it shows
// and in what order, what each row says holds it open, what a key means. None of
// it is shared with the other views on purpose. `sortForList`, `taskColumn` and
// the status-filter defaults keep today's behaviour, and this module is what
// stops a Workspace rule from leaking into them.
import type { Task, TaskHold } from "@platform/lib/api";
import type { PrState } from "@platform/lib/prStatus";
import { prKey, prRefsOf, type PrRef } from "@platform/lib/task-prs";
import { cardTitleLine, isDraftTask, usageLimitCaption } from "@shell/tasks-lib";

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

/** The status word a card prints, or "" when the circle already says it. Only
 *  a task that is running or needs the reader gets one (Running, Needs
 *  attention, Failed, Usage limit, Blocked); Done, Stopped and the rest stay
 *  silent. */
export function cardStatusWord(task: Task): string {
  return task.status === "in_progress" || needsYou(task) ? statusWord(task) : "";
}

/** The hold reasons a card prints under its chips. Drops what the card already
 *  says elsewhere: "N unread" (the bold title), a PR that is open or draft and
 *  the bg-job count (their chips), and running / needs you when the status word
 *  is shown. Worktree facts, "unknown", "checking", "queued" and "run pending"
 *  stay. The task still counts as held: "clear" is decided on the full list. */
export function visibleReasons(reasons: readonly string[], task: Task): string[] {
  const word = cardStatusWord(task) !== "";
  return reasons.filter((r) => {
    if (/^\d+ unread$/.test(r)) return false;
    if (/^PR #\d+ (open|draft)$/.test(r)) return false;
    if (/^\d+ bg jobs?$/.test(r)) return false;
    if (word && (r === "running" || r === "needs you")) return false;
    return true;
  });
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

export type WorkspaceAction = "next" | "prev" | "open" | "close" | "check" | "new" | "search" | "needs" | "escape";

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
    case " ":
      return "check";
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

/** First `max` of `items` and how many were left out. */
export function capList<T>(items: readonly T[], max: number): { shown: T[]; more: number } {
  return { shown: items.slice(0, max), more: Math.max(0, items.length - max) };
}

export const MAX_PRS = 3;

/** `refs` with the open pull requests first, then drafts, then those whose state
 *  is not known yet, then closed and merged; each band keeps its own order. */
export function orderPrs<T extends PrRef>(
  refs: readonly T[],
  states: Readonly<Record<string, string | null | undefined>>,
): T[] {
  const rank = (r: T): number => {
    const st = states[prKey(r)];
    return st === "open" ? 0 : st === "draft" ? 1 : st === "closed" || st === "merged" ? 3 : 2;
  };
  return refs
    .map((r, i) => ({ r, i, k: rank(r) }))
    .sort((a, b) => a.k - b.k || a.i - b.i)
    .map((x) => x.r);
}

/** The card's one background-job badge: "1 job" / "N jobs" (the running count,
 *  not the titles), and a tooltip naming every titled job, one per line. */
export function bgJobBadge(task: Task): { label: string; title: string } | null {
  const count = task.background_jobs?.count ?? 0;
  if (count <= 0) return null;
  const titles = (task.background_jobs?.jobs ?? []).map((j) => (j.title ?? "").trim()).filter(Boolean);
  return {
    label: `${count} ${count === 1 ? "job" : "jobs"}`,
    title: titles.length > 0 ? titles.join("\n") : "Background jobs still running",
  };
}

/** The card's context line: the newest message (cardTitleLine, the List row's
 *  own rule), or "" when it would only repeat the title the card already shows. */
export function messagePreview(task: Task): string {
  const text = cardTitleLine(task).text.trim();
  if (!text) return "";
  return text === (task.title ?? "").trim() ? "" : text;
}

/** The branch a card shows: the recorded worktrees' branches first (deduped,
 *  in order), and only when there are none the head branch of the task's
 *  newest PR (`prHeads`, newest first). `more` counts the other branches. */
export function pickBranch(
  holds: readonly Pick<TaskHold, "branch">[],
  prHeads: readonly (string | null | undefined)[],
): { name: string; more: number; also: string[] } | null {
  const wt = [...new Set(holds.map((h) => h.branch).filter((b): b is string => !!b))];
  if (wt.length > 0) return { name: wt[0], more: wt.length - 1, also: wt.slice(1) };
  const head = prHeads.find((h): h is string => !!h);
  return head ? { name: head, more: 0, also: [] } : null;
}

export type WorkspaceGroupKey = "needs" | "running" | "review" | "idle" | "clear";

export const WORKSPACE_GROUPS: readonly { key: WorkspaceGroupKey; label: string }[] = [
  { key: "needs", label: "Needs you" },
  { key: "running", label: "Running" },
  { key: "review", label: "In review" },
  { key: "idle", label: "Open" },
  { key: "clear", label: "Safe to archive" },
];

/** The first group that matches: needs you (waiting, failed, usage limit),
 *  running, in review (a PR known open or draft), open (any hold remains), and
 *  safe to archive (no holds: the card's "safe to archive" badge, which is
 *  `holdReasons` empty). */
export function workspaceGroup(task: Task, facts: WorkspaceFacts): WorkspaceGroupKey {
  if (needsYou(task)) return "needs";
  if (task.status === "in_progress") return "running";
  for (const ref of prRefsOf(task)) {
    const state = facts.prStates[prKey(ref)];
    if (state === "open" || state === "draft") return "review";
  }
  return holdReasons(task, facts).length > 0 ? "idle" : "clear";
}

/** `rows` split into the groups above, in that order, empty groups left out;
 *  each keeps `rows`' order. */
export function groupTasks(
  rows: readonly Task[],
  facts: (task: Task) => WorkspaceFacts,
): { key: WorkspaceGroupKey; label: string; tasks: Task[] }[] {
  const by = new Map<WorkspaceGroupKey, Task[]>();
  for (const t of rows) {
    const k = workspaceGroup(t, facts(t));
    by.set(k, [...(by.get(k) ?? []), t]);
  }
  return WORKSPACE_GROUPS.filter((g) => by.has(g.key)).map((g) => ({ ...g, tasks: by.get(g.key)! }));
}

/** A group shows this many cards until its "Show N more" is pressed. */
export const GROUP_CAP = 8;

/** The cards a group draws: all of them once `expanded`, else the first
 *  GROUP_CAP; `more` is how many "Show N more" would add. */
export function visibleGroupTasks<T>(items: readonly T[], expanded: boolean): { shown: T[]; more: number } {
  return expanded ? { shown: [...items], more: 0 } : capList(items, GROUP_CAP);
}

/** Does a group start folded? Only "Safe to archive" does. A stored choice
 *  (`stored[key]`, true = collapsed) wins over this default. */
export function groupCollapsed(key: WorkspaceGroupKey, stored: Readonly<Record<string, boolean>>): boolean {
  return key in stored ? stored[key] : key === "clear";
}

export interface PortBadge {
  label: string;
  title: string;
  /** Set when the badge stands for one port, so the view can link to it. */
  port?: number;
}

/** The port badges a row shows (the server already dropped the dynamic range):
 *  1-2 ports individually; 3 or more show the lowest as "port A" plus "+N
 *  ports", with every port in that badge's title. */
export function portBadges(ports: number[]): PortBadge[] {
  const sorted = [...new Set(ports)].sort((a, b) => a - b);
  const one = (p: number): PortBadge => ({ label: `port ${p}`, title: `Open http://localhost:${p}`, port: p });
  if (sorted.length <= 2) return sorted.map(one);
  const rest = sorted.slice(1);
  return [one(sorted[0]), { label: `+${rest.length} ports`, title: sorted.join(", ") }];
}
