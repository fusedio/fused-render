// The scheduled-message toast rules — pure, so they can be tested without a DOM
// (the same split server-status.ts uses: rules here, polling and toasts in
// scheduleEvents.ts). Imports nothing at runtime; `ScheduleEvent` is a type-only
// import and is erased.
import type { ScheduleEvent } from "@platform/lib/api";

// Toast text is one line in a narrow column, so the prompt is clipped rather
// than wrapped into a paragraph nobody reads.
const LABEL_MAX = 60;

export function eventLabel(e: ScheduleEvent): string {
  const first = (e.message || "").trim().split("\n")[0];
  if (!first) return e.immediate ? "Task" : "Scheduled message";
  return first.length > LABEL_MAX ? `${first.slice(0, LABEL_MAX - 1)}…` : first;
}

// What one event becomes, as data — the decision table, kept pure and tested
// (server-status.ts's split: the rules here, the polling and the DOM below).
// A run that finished without incident produces no toast at all (`null`): the
// Tasks page is where task results live, and a plain success was never
// something the user asked to be interrupted about. Both remaining outcomes
// are surprises nothing else surfaces — tasks are gone from the Activity chip
// (D655) and excluded from Notifications routing on this branch — so both are
// errors that persist until acted on; there is no longer a lesser, self-
// dismissing tone to choose between.
export interface ScheduleToast {
  msg: string;
}

export function toastForEvent(e: ScheduleEvent): ScheduleToast | null {
  if (e.kind === "done") return null;
  const label = eventLabel(e);
  // An IMMEDIATE entry is a task the user ran (a New task with the when-row
  // untouched, a new app's scaffolding turn), not one they scheduled — so the
  // noun is "task" and the verb is about finishing, not about a schedule
  // having been honoured. Everything else about the toast is the same.
  const noun = e.immediate ? "Task" : "Scheduled message";
  // `missed` is not an app failure — nothing went wrong, the app just wasn't
  // running inside the catch-up window — but the user asked for something that
  // did not happen, so it still has to be said.
  const verb = e.kind === "missed" ? "was missed" : "failed";
  return { msg: `${noun} ${verb}: ${label}` };
}
