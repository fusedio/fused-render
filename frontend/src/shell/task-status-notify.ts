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
// `in_progress -> done` ("Task finished") mirrors schedule-toast.ts's own
// `done` reversal: suppressible via `source` (the run's own chat/project)
// when that's already on screen, never retained — a plain "it's over"
// confirmation is not something to hunt for again.
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
    };
  }
  return null;
}
