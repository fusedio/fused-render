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
// `needsAttention` is both "persist until acted on" and "give it an action",
// because those are the same question asked twice: a toast that vanishes on a
// timer is one the user is not required to see. It is NOT the same as the tone —
// see the `attention` kind below, which is the one thing here that genuinely
// needs a person and is not a failure.
export interface ScheduleToast {
  msg: string;
  tone: "error" | "info";
  needsAttention: boolean;
  /**
   * WHERE the toast's action goes, when that is somewhere finer than /tasks: the
   * conversation this event happened in. Null for every kind whose news is a row
   * on the page — a run that failed or was missed is over, and the row carries
   * the reason, the target and the run id, which is more than the thread says.
   *
   * The two halves rather than a URL, because a URL cannot be built here:
   * `explorerUrl` lives in shell and platform may not reach up
   * (frontend/scripts/check-boundaries.mjs). The caller passes the builder in,
   * exactly as it passes `onOutcome` in, for the same reason.
   */
  open: { target: string; sessionId: string } | null;
}

export function toastForEvent(e: ScheduleEvent): ScheduleToast {
  const label = eventLabel(e);
  // A run that is STILL GOING and cannot go any further by itself: it has raised
  // a permission or question card and nobody has answered it.
  //
  // `info`, not `error` (nothing has gone wrong — the run is doing exactly what
  // it should, which is refusing to act without an answer), and persistent all
  // the same: the ask does not expire, and the person is the only thing that can
  // end it. That pairing is why `needsAttention` is a field of its own rather
  // than `tone === "error"`.
  //
  // The action opens the CHAT and not /tasks (Akshil, 2026-09-03: "when we click
  // we go to the page and unblock the task"). The card is IN the thread; the
  // Tasks page can only point at the thread, which puts one more click between a
  // person and the one thing they are being asked for.
  if (e.kind === "attention") {
    return {
      msg: `Task needs your input: ${label}`,
      tone: "info",
      needsAttention: true,
      open: { target: e.target || "", sessionId: e.session_id || "" },
    };
  }
  // An IMMEDIATE entry is a task the user ran (a New task with the when-row
  // untouched, a new app's scaffolding turn), not one they scheduled — so the
  // noun is "task" and the verb is about finishing, not about a schedule
  // having been honoured. Everything else about the toast is the same.
  const noun = e.immediate ? "Task" : "Scheduled message";
  if (e.kind === "done") {
    return {
      msg: e.immediate ? `Task finished: ${label}` : `Scheduled message ran: ${label}`,
      tone: "info",
      needsAttention: false,
      open: null,
    };
  }
  // `missed` is not an app failure — nothing went wrong, the app just wasn't
  // running inside the catch-up window — but the user asked for something that
  // did not happen, so it is not an info either.
  const verb = e.kind === "missed" ? "was missed" : "failed";
  return {
    msg: `${noun} ${verb}: ${label}`,
    tone: "error",
    needsAttention: true,
    open: null,
  };
}
