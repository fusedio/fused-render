// §5 (SPEC-quiet-notifications.md) "interactive turns and needs-input" — the
// decision table, split from the hook that drives it (useTaskStatusNotify.ts)
// the same way schedule-toast.ts/scheduleEvents.ts split rules from polling —
// so the rules can be tested without a DOM.
//
// Derives from the EXISTING task-status poll (tasksPulse.ts's
// useTasksPulseRows) rather than adding a server channel, per the spec's own
// "Sources" instruction — `/api/tasks`+`/api/tasks/changes`
// (fused_render/server/routers/tasks.py `_status()`) already computes
// `needs_attention` as its rule 0 and `in_progress`/`blocked`/`done` for
// everything else. This file only watches for the three transitions the
// spec's table names:
//   - in_progress -> done            ("Task finished")
//   - in_progress -> blocked         ("Task failed")
//   - anything    -> needs_attention ("Task needs your input")
//
// NEEDS_ATTENTION IS ALREADY RETAINED (Akshil, 2026-09-03; tasks-lib.ts's
// `attentionRows`, wired into RepoUpdatesDock.tsx line ~1185) — a dedicated,
// always-current Notifications-panel row for every task parked on a
// question, with its own dismiss/undismiss lifecycle (RepoUpdatesDock.test.tsx,
// "the waiting-task row"). Routing this transition through notify()'s
// ordinary `tone: "error"` shape would ALWAYS-retain (notifications.ts's
// `isRetained`) a SECOND, separately-dismissible row for the exact same
// fact — a visible duplicate, not a second source of truth. So this one
// transition raises a plain notify() with no tone/tier (resolves to
// "transient": pops, does NOT retain, per notifications.ts's `resolveTier`/
// `isRetained`) — the one thing `attentionRows` cannot do on its own, an
// announcement at the MOMENT a task parks. `attentionRows` keeps covering
// "is still parked" for as long as that remains true.
//
// `in_progress -> blocked` ("Task failed") has no pre-existing row anywhere
// in the Notifications panel, so it keeps the ordinary `tone: "error"` shape
// (pop + retain, with a `page` so the row is clickable, per
// SPEC-actionable-notifications.md's "every row goes somewhere").
//
// `in_progress -> done` ("Task finished") IS RETAINED AND CLICKABLE — a
// REVERSAL of this file's own earlier position (until 2026-09-16 this
// comment argued a plain "it's over" confirmation "is not something to hunt
// for again" and the branch below returned no `page` at all, which
// `lib/notifications.ts`'s `isRetained` resolves to a transient popup that
// is never kept). The user, from a screenshot: "the user does want to open
// the app along with claude template to go back" — a finished run is
// exactly the moment someone wants to jump back into it, so losing the row
// the instant the popup's ~2.5s expire was the bug, not a feature. `page:
// taskDestination(task)` gives it the same destination
// `in_progress -> blocked` already carries, which alone makes
// `isRetained` keep it (`Boolean(input.action || input.page)`).
//
// STILL SUPPRESSIBLE via `source` (the run's own chat/project) when that's
// already on screen — being retained once shown is not the same as always
// showing it; D-A's "as far as each store honestly can" is untouched.
//
// `recent: true` is the other half of this reversal: a bare "done" carries
// no failure to act on, so it goes straight into the Notifications panel's
// folded §4 "Recent" section rather than sitting, unfolded, at the top of
// "Worth keeping" — a settled call (a success persisting is fine, a success
// shouting is not). Without this flag `RepoUpdatesDock.tsx`'s `messagesTrail`
// would draw it in full every time, which is the "shouting" this flag
// exists to avoid.
import type { TaskPulseTask } from "@platform/lib/api";
import type { NotificationInput } from "@platform/lib/notifications";
import { taskColumn } from "@shell/tasks-lib";
import { taskHref } from "@shell/tasks-lib";
import { folderHref } from "@shell/schedule-lib";

/** Where a click on this task's own notification should land — the same
 *  fallback chain `attentionRows` already uses (a live session's chat, else
 *  the folder, else the Tasks page itself). */
export function taskDestination(task: TaskPulseTask): string {
  return taskHref(task) ?? folderHref(task) ?? "/tasks";
}

/** The task's own chat/project — the presence-suppression key for "you're
 *  already looking at this" (SPEC-quiet-notifications.md §2a's `source`). */
function taskSource(task: TaskPulseTask): string | undefined {
  return task.target || task.project || undefined;
}

/**
 * The notify() input for ONE task's status transition, or `null` when this
 * pair of statuses is not one the spec names — including no change at all,
 * and `previous === undefined`, a task's first sighting.
 *
 * FIRST SIGHTING IS DELIBERATELY NEVER A TRANSITION: a task already parked,
 * already blocked, or already done the moment this document starts polling
 * is not something that just happened — `attentionRows` (and the Tasks page
 * itself) already show it, and treating a page load as a wave of transitions
 * would flood a fresh tab with popups for every row already on screen.
 */
export function notificationForTransition(
  previous: string | undefined,
  task: TaskPulseTask,
): NotificationInput | null {
  if (previous === undefined) return null;
  const column = taskColumn(task);
  if (column === previous) return null;

  if (column === "needs_attention") {
    return { title: `${task.title || "A task"} needs your input` };
  }
  if (previous === "in_progress" && column === "blocked") {
    return {
      title: `${task.title || "A task"} failed`,
      tone: "error",
      page: taskDestination(task),
    };
  }
  if (previous === "in_progress" && column === "done") {
    return {
      title: `${task.title || "A task"} finished`,
      tone: "info",
      source: taskSource(task),
      page: taskDestination(task),
      recent: true,
    };
  }
  return null;
}
