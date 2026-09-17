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
//
// SPEC-quiet-notifications.md §5 REVERSES this module's old rule for `done`
// (see DECISIONS-toasts-become-notifications.md for the writeup): a run that
// finished without incident used to produce no toast at all, on the theory
// that the Tasks page is where results live and a plain success is never
// something worth interrupting for. That theory only held while every window
// was assumed to be looking — "a successful run is news when you are not
// looking at it". `started` gets the same shape for the same reason: an
// unattended run's "your message went out" has nowhere else to surface.
//
// `started`/`done` are therefore `tone: "info"` — suppressible (via
// `source`, checked against presence in `scheduleEvents.ts`'s call to
// `notify()`) and never retained, i.e. "already seen" collapses them to
// nothing, same as any other transient toast. `failed`/`missed` keep the old
// `tone: "error"` shape: never suppressed, always retained, always actioned —
// both are surprises nothing else surfaces (tasks are gone from the Activity
// chip (D661) and excluded from Notifications routing on this branch).
export interface ScheduleToast {
  msg: string;
  tone: "error" | "info";
  /** Presence-suppression key for the `info` case — the run's own
   *  chat/project (`e.target`). Unset for `error`: failed/missed never
   *  suppress, per the table in SPEC-quiet-notifications.md §5. */
  source?: string;
}

export function toastForEvent(e: ScheduleEvent): ScheduleToast {
  const label = eventLabel(e);
  // An IMMEDIATE entry is a task the user ran (a New task with the when-row
  // untouched, a new app's scaffolding turn), not one they scheduled — so the
  // noun is "task" and the verb is about finishing, not about a schedule
  // having been honoured. Everything else about the toast is the same.
  const noun = e.immediate ? "Task" : "Scheduled message";
  if (e.kind === "started" || e.kind === "done") {
    const verb = e.kind === "started" ? "started" : "finished";
    return { msg: `${noun} ${verb}: ${label}`, tone: "info", source: e.target };
  }
  // `missed` is not an app failure — nothing went wrong, the app just wasn't
  // running inside the catch-up window — but the user asked for something that
  // did not happen, so it still has to be said.
  const verb = e.kind === "missed" ? "was missed" : "failed";
  return { msg: `${noun} ${verb}: ${label}`, tone: "error" };
}
