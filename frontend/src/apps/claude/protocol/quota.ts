// THE PLAN WINDOW, IN WORDS — what the card, the topbar pill and the comeback
// note say about a `Quota` (protocol/types.ts). Pure functions over an epoch and
// a clock, so the copy is pinned by tests rather than by a screenshot.
//
// The vocabulary is Claude Code's own: its TUI says "You've hit your session
// limit · resets 12:30am" and, while it waits, "Usage limit reached ·
// continuing automatically at 12:30am". A reader who has seen the CLI say it
// should recognise the same sentence here (Jakob's law), so these do not
// paraphrase it.
import type { Quota } from "./types";

/**
 * The fixed prompt the comeback sends. NOT the user's last message: the CLI's
 * own auto-continue "sends Claude a fixed prompt to pick the task up where it
 * stopped. It doesn't resend your last message" — resending would redo work
 * the turn had already done before the limit cut it off.
 */
export const CONTINUE_PROMPT =
  "Your usage limit has reset. Continue the task you were working on where it " +
  "stopped. Do not repeat steps that were already completed.";

/** The scheduled task's name in the Tasks list. */
export const CONTINUE_TITLE = "Continue after usage limit";

/** Seconds past the reset the comeback fires: the window reopens on the minute
 *  and a request landing exactly on it has been seen refused. */
export const CONTINUE_GRACE_S = 60;

/** Whether this poll ended on the plan limit — the one case the comeback is for. */
export function limitHit(quota: Quota | null | undefined): quota is Quota {
  return !!quota && quota.status === "rejected" && quota.resets_at > 0;
}

/** The ISO instant the comeback is due. */
export function continueDue(quota: Quota): string {
  return new Date((quota.resets_at + CONTINUE_GRACE_S) * 1000).toISOString();
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/**
 * "12:30am" / "3:45pm" — the CLI's own clock spelling, in the local zone. A
 * reset more than a day out (the weekly window) gets its weekday in front —
 * "Tue 3:11pm" — because a bare clock five days away reads as today.
 */
export function clockText(epochS: number, nowMs: number = Date.now()): string {
  const d = new Date(epochS * 1000);
  let h = d.getHours();
  const m = d.getMinutes();
  const suffix = h >= 12 ? "pm" : "am";
  h = h % 12;
  if (h === 0) h = 12;
  const clock = h + ":" + (m < 10 ? "0" : "") + m + suffix;
  return epochS * 1000 - nowMs > 24 * 3600 * 1000 ? DAYS[d.getDay()] + " " + clock : clock;
}

/** "in 2h 14m" / "in 3m" / "any moment now" — the distance to the reset. */
export function untilText(epochS: number, nowMs: number): string {
  const secs = epochS - nowMs / 1000;
  if (secs < 60) return "any moment now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return "in " + mins + "m";
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return "in " + h + "h" + (m ? " " + m + "m" : "");
}

/** The card's explanation for a limit hit, with the reset the CLI reported. */
export function limitExplain(quota: Quota, nowMs: number, scheduled: boolean): string {
  const when = clockText(quota.resets_at) + " (" + untilText(quota.resets_at, nowMs) + ")";
  const head = "Nothing is broken — your plan's usage is spent for now. It resets at " + when + ".";
  return scheduled
    ? head + " A follow-up is scheduled to continue this task then; cancel it from the row below."
    : head;
}

/** The note row the comeback leaves in the log — the CLI's own wait line. */
export function continueNote(quota: Quota): string {
  return "Usage limit reached · continuing automatically at " + clockText(quota.resets_at);
}

/** "5h" / "7d" — a window's short name for the pill. Unknown names pass through. */
export function windowShort(name: string): string {
  if (name === "five_hour") return "5h";
  if (name === "seven_day") return "7d";
  return name;
}

/**
 * The topbar pill, or "" when there is nothing to say. Shown ONLY on the CLI's
 * own `allowed_warning` — its threshold, not ours — so the header stays empty
 * for every ordinary turn. "5h 93% · resets 3:45pm".
 */
export function quotaPill(quota: Quota | null | undefined): string {
  if (!quota || quota.status !== "allowed_warning") return "";
  const win = quota.windows[quota.type];
  const util = quota.utilization ?? win?.utilization ?? null;
  if (util === null) return "";
  return (
    windowShort(quota.type) + " " + Math.round(util * 100) + "% · resets " + clockText(quota.resets_at)
  );
}

/** The pill's hover: every window the CLI reported, one per line. */
export function quotaTitle(quota: Quota): string {
  return Object.entries(quota.windows)
    .map(
      ([name, w]) =>
        windowShort(name) + " window: " + Math.round(w.utilization * 100) + "% used · resets " + clockText(w.resets_at),
    )
    .join("\n");
}
