// The queue dock's rules, kept pure so they can be tested without a DOM — the
// same split platform/lib/schedule-toast.ts makes against scheduleEvents.ts:
// what the card SAYS lives here, the polling and the pixels live in QueueDock.tsx.
//
// The rule that matters most is not in this file, and that is the point: WHAT
// COUNTS AS QUEUED is the server's answer (`GET /api/schedule/queue` = past due
// and waiting to be claimed), never a filter applied here. A message scheduled
// for later today is not queued, it is scheduled, and nothing in this module may
// quietly promote it.
import type { ScheduledMessage } from "@platform/lib/api";
import { relativeDue } from "@shell/schedule-lib";

export interface QueueRow {
  entry: ScheduledMessage;
  /** "live" — the turn is running; "sending" — claimed, spawning; "queued" —
   *  past due, still waiting to be claimed. What each allows differs: only a
   *  queued message can be withdrawn cleanly. */
  role: "live" | "sending" | "queued";
}

/**
 * The card's rows: what is running now, then what is about to run.
 *
 * Deduplicated by entry id, because an entry claimed between the moment the
 * server built one list and the next would otherwise appear twice in two
 * different tenses. First writer wins, and the lists are pushed in order of how
 * far along the work is, so the row shows the LATER state — the honest one for a
 * message that has already moved on.
 */
export function queueRows(
  live: ScheduledMessage[] | undefined,
  running: ScheduledMessage[] | undefined,
  queued: ScheduledMessage[] | undefined,
): QueueRow[] {
  const seen = new Set<string>();
  const rows: QueueRow[] = [];
  const push = (list: ScheduledMessage[] | undefined, role: QueueRow["role"]) => {
    for (const entry of list || []) {
      const id = String(entry?.id || "");
      if (!id || seen.has(id)) continue;
      seen.add(id);
      rows.push({ entry, role });
    }
  };
  push(live, "live");
  push(running, "sending");
  push(queued, "queued");
  return rows;
}

/**
 * What a row says under its title.
 *
 * A live row prefers the JOB REGISTRY's line — "waiting for permission",
 * "working · 4210 tokens" — because that is the string this whole card was asked
 * for: a run parked on a prompt used to be visible and unreachable. "Running" is
 * only the fallback for a turn whose reporter has not ticked yet.
 */
export function roleText(row: QueueRow, jobLine: string): string {
  if (row.role === "live") return jobLine || "Running";
  if (row.role === "sending") return "Starting…";
  // Past due by definition, so the stamp reads backwards ("2m ago") — which is
  // the useful thing to show: it says how long this has been waiting.
  const rel = relativeDue(row.entry.due);
  return rel ? `Queued · due ${rel}` : "Queued";
}

/**
 * Which cancel a row gets.
 *
 * Only a queued (or claimed) message can be withdrawn through the queue: the
 * server refuses one already handed to the sender, and says so. A LIVE turn is a
 * running process, and stopping it is the job registry's verb, not the queue's —
 * un-sending a message and killing a run are different promises.
 */
export function rowCancelKind(row: QueueRow): "queued" | "job" {
  return row.role === "live" ? "job" : "queued";
}

/** How many rows Cancel all would actually take. A live turn is not one of them,
 *  so the button never counts work it cannot withdraw. */
export function withdrawableCount(rows: QueueRow[]): number {
  return rows.filter((r) => r.role !== "live").length;
}
