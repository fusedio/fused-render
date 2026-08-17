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
  if (!first) return "Scheduled message";
  return first.length > LABEL_MAX ? `${first.slice(0, LABEL_MAX - 1)}…` : first;
}

// What one event becomes, as data — the decision table, kept pure and tested
// (server-status.ts's split: the rules here, the polling and the DOM below).
// `needsAttention` is both "paint it as an error" and "persist until acted on",
// because those are the same question asked twice: a toast that vanishes on a
// timer is one the user is not required to see.
export interface ScheduleToast {
  msg: string;
  tone: "error" | "info";
  needsAttention: boolean;
}

export function toastForEvent(e: ScheduleEvent): ScheduleToast {
  const label = eventLabel(e);
  if (e.kind === "done") {
    return { msg: `Scheduled message ran: ${label}`, tone: "info", needsAttention: false };
  }
  // `missed` is not an app failure — nothing went wrong, the app just wasn't
  // running inside the catch-up window — but the user asked for something that
  // did not happen, so it is not an info either.
  const verb = e.kind === "missed" ? "was missed" : "failed";
  return { msg: `Scheduled message ${verb}: ${label}`, tone: "error", needsAttention: true };
}
