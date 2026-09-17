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
// NEVER PRESENCE-SUPPRESSED (2026-09-17, second reversal): this branch used
// to also carry `source: taskSource(task)`, which let a finished task's
// POPUP get swallowed by `isPopupSuppressed` (jobs.ts) whenever the run's own
// chat/project was already on screen — "you're already looking at it" for a
// job that is still running. A finished Claude task is different: the run
// has ENDED, so "already looking at the chat" no longer means "already knows
// it's done" the way it does for an in-progress job's own page. Dropping
// `source` here means a finished task always pops, same as `in_progress ->
// blocked` already does. This also drops the `recent: true` flag from the
// now-removed "Recent" section (SPEC-quiet-notifications.md §4, reversed the
// same day — see DECISIONS-quiet-notifications.md): the row simply lands as
// an ordinary retained row in the one unified list.
import type { TaskPulseTask } from "@platform/lib/api";
import type { NotificationInput } from "@platform/lib/notifications";
import { labelForSource } from "@platform/lib/format";
import { taskColumn } from "@shell/tasks-lib";
import { taskHref } from "@shell/tasks-lib";
import { folderHref } from "@shell/schedule-lib";

/** Where a click on this task's own notification should land — the same
 *  fallback chain `attentionRows` already uses (a live session's chat, else
 *  the folder, else the Tasks page itself). */
export function taskDestination(task: TaskPulseTask): string {
  return taskHref(task) ?? folderHref(task) ?? "/tasks";
}

/** The "who made this" caption for a task's own notification — SAME
 *  project-first order `folderHref` (schedule-lib.ts) and `attentionRows`
 *  (tasks-lib.ts:4872) already settled on, not target-first: a task made
 *  from inside an app targets that app's own ENTRY PAGE
 *  (".../Transcripto/index.html"), so target-first here reproduces the
 *  exact "index" caption bug `attentionRows`'s own comment names. `project`
 *  names the containing app/folder; `target` is only a fallback for a task
 *  with no project at all. */
function taskCaption(task: TaskPulseTask): string {
  return labelForSource(task.project || task.target);
}

/** A task's own title, with a leading repeat of its own caption stripped —
 *  "Transcripto YouTube transcriber" next to a "Transcripto" eyebrow reads as
 *  the same word twice; "YouTube transcriber" under a "Transcripto" eyebrow
 *  reads as two different pieces of information. Deliberately narrow: only
 *  strips an actual PREFIX match (case-insensitive, followed by whitespace
 *  or punctuation), never touches a title that doesn't happen to start with
 *  its own caption — most tasks aren't named "<project> <description>", and
 *  this must never mangle those into something shorter and wrong. Falls back
 *  to the untouched title whenever there is no caption, no match, or the
 *  match would consume the whole title. */
function titleWithoutCaption(title: string, caption: string): string {
  if (!caption) return title;
  const escaped = caption.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const stripped = title.replace(new RegExp(`^${escaped}[\\s:-]+`, "i"), "");
  return stripped || title;
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
    // THIRD REVERSAL, 2026-09-17 — the code-review-round fix (see
    // DECISIONS-quiet-notifications.md's "notification card regression"
    // entry) restoring what the SECOND reversal above accidentally deleted.
    // Dropping `source` (rightly — see the header comment above) also
    // silently dropped the ONLY thing that fed the card's caption, because
    // `notifications.ts`'s `toStored` computed the caption from `source`
    // alone: no `source`, no caption, no matter how good `origin`
    // resolved. The card the user actually saw was a single bold line —
    // "why do you always want to make the notification smaller? ... I don't
    // want a single line of text" — with no creator context at all.
    // `notifications.ts` now has a separate `origin` field for exactly this:
    // "caption this row" without "suppress it when its page is open" (the
    // two `source` used to conflate). Set `origin` here, never `source` —
    // the whole point of the second reversal above stands.
    const caption = taskCaption(task);
    return {
      title: titleWithoutCaption(task.title || "A task", caption),
      // "Finished" moves OUT of the title and into `detail` (`.dl-model`,
      // MessageRowView's `secondary`) rather than staying baked into the
      // title string or landing in `status`: `status` is already spoken for
      // by MessageRowView's own "Happened N times" repeat-count line
      // (notifications.ts's `count`/`family` collapse), which must keep
      // working for a task that finishes more than once. `detail` is empty
      // for this call site otherwise, so it costs nothing and reads as a
      // real second line ("YouTube transcriber" / "Finished"), not a
      // repeated word wedged into the title.
      detail: "Finished",
      tone: "info",
      origin: caption || undefined,
      page: taskDestination(task),
    };
  }
  return null;
}
