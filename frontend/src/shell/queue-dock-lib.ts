// The rules for the queue's rows in the one bottom-right activity card, kept pure
// so they can be tested without a DOM — the same split platform/lib/schedule-toast.ts
// makes against scheduleEvents.ts: what a row SAYS lives here, the polling and the
// pixels live in QueueDock.tsx, and the card around it is DownloadManager's.
//
// The rule that matters most is not in this file, and that is the point: WHAT
// COUNTS AS QUEUED is the server's answer (`GET /api/schedule/queue` = past due
// and waiting to be claimed), never a filter applied here. A message scheduled
// for later today is not queued, it is scheduled, and nothing in this module may
// quietly promote it.
import type { ScheduledMessage } from "@platform/lib/api";
import { isRunning, SCHEDULE_JOB_PREFIX, type Job, type QueueCount } from "@platform/lib/jobs";
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
 * The rows this half STILL OWNS: `queueRows` minus any live row whose run the job
 * registry says has already ended.
 *
 * This is the handover, and it is here because the alternative is a duplicate. The
 * job half drops every run this half is drawing, terminal or not (`jobRows`), since a
 * rule that exempted terminal rows only held while two independent polls agreed —
 * /api/jobs about once a second, /api/schedule/queue every six — and in the seconds
 * between them the same run had a terminal job row and a live queue row. So `drawn`
 * wins unconditionally, and the price is that the outcome row cannot appear until
 * this half lets the run go.
 *
 * Waiting for the next queue read to let it go would put that price at up to a full
 * poll — and with the read failing or hanging, at "never": the last snapshot is
 * deliberately kept (an unreadable queue is not an empty one), so a stale live row
 * would sit there indefinitely with the finished run's outcome behind it. Instead the
 * row is retired against the JOB SNAPSHOT the card is already polling fast and hands
 * up through its slot (`QueueSlot.onJobs`), which is the same evidence the job half
 * is acting on. The two halves therefore hand over a render apart, whatever the queue
 * endpoint is doing.
 *
 * Three cases it must NOT retire, each of which would cost a row rather than save one:
 *
 * * a live row with no job record at all — the spawn has not written its first
 *   report yet, and "absent" is not "ended" (the row says "Running" and waits);
 * * a live row whose job is still `running`, stalled included: a stalled row is the
 *   app admitting it stopped hearing, not the turn ending, and the queue row is where
 *   its stop lives;
 * * a `queued` or `sending` row, whatever its job says. That job is the PREVIOUS run
 *   of a re-queued entry (run-now, resend) and this row is the current tense — the
 *   job half is already dropping the stale terminal row, so retiring this one too
 *   would leave the entry with no row anywhere.
 */
export function openRows(rows: QueueRow[], jobs: Job[]): QueueRow[] {
  if (rows.length === 0) return rows;
  const ended = new Set<string>();
  for (const job of jobs) {
    if (!job.id.startsWith(SCHEDULE_JOB_PREFIX) || isRunning(job)) continue;
    ended.add(job.id.slice(SCHEDULE_JOB_PREFIX.length));
  }
  if (ended.size === 0) return rows;
  return rows.filter((r) => !(r.role === "live" && ended.has(String(r.entry.id))));
}

/**
 * WHICH SCHEDULED RUNS THIS HALF IS DRAWING, by entry id — the card's other half
 * drops exactly these and keeps everything else (`jobRows`).
 *
 * Derived from the same `rows` array the components are rendered from, and that is
 * the whole point of the function existing: the two lists cannot disagree about
 * which runs are on screen, because there is one list. The job half used to be left
 * to guess — it dropped every running `sys:schedule:*` job on the assumption that a
 * row like these existed — and a failed `GET /api/schedule/queue` (or a bare mount
 * of the card, with no shell above it filling the slot) then left a run that was
 * genuinely executing with no row at all, and so no way to stop it. Empty here means
 * "this half is drawing nothing", which is the truth in both of those cases and
 * hands the run back to the job half rather than dropping it on the floor.
 *
 * Asked of what `openRows` returned, never of the raw rows: this half must not claim a
 * run it has already handed over, or the outcome row it is waiting for would be the
 * one thing `drawn` keeps hidden.
 *
 * EVERY role, not just `live`. `queued` and `sending` are the states before a job
 * row exists at all, so they normally cost nothing — but an entry that was re-queued
 * (run-now, resend) while its previous run's job row was still `running` would
 * otherwise be drawn twice, once in each half, in two different tenses. One list, one
 * rule: whatever this half has a row for, the other half does not.
 */
export function drawnIds(rows: QueueRow[]): string[] {
  return rows.map((r) => String(r.entry.id));
}

/**
 * What these rows contribute to the card's ONE header count, split the only way
 * the header can honestly word it.
 *
 * `queued` is the sole waiting state: past due, not claimed, still withdrawable —
 * so a card holding nothing else says "1 queued" rather than claiming something is
 * running. Both other roles count as running, `sending` included: the entry has
 * been claimed and the helper is away, which is why it has no cancel at all, and
 * calling that "queued" would describe a message that has already gone.
 */
export function queueCount(rows: QueueRow[]): QueueCount {
  let waiting = 0;
  let running = 0;
  for (const row of rows) {
    if (row.role === "queued") waiting += 1;
    else running += 1;
  }
  return { waiting, running };
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
  // Says why the row has no ✕. A claimed entry is a brief state with no control
  // at all, and a row that goes quiet without explaining itself reads as stuck —
  // so the sentence carries the reason the server would give if asked.
  if (row.role === "sending") return "Starting… · too late to cancel";
  // Past due by definition, so the stamp reads backwards ("2m ago") — which is
  // the useful thing to show: it says how long this has been waiting.
  const rel = relativeDue(row.entry.due);
  return rel ? `Queued · due ${rel}` : "Queued";
}

/**
 * Which cancel a row gets — and the one place that decides it, so a row's
 * control set follows the row through `queued` → `sending` → `live` instead of
 * being reasoned out again per call site.
 *
 * The rule is the SERVER'S, not this card's idea of what looks cancellable:
 * `schedule.cancel_queued` allows exactly `pending` → `cancelled`, so
 *
 * * **queued** — pending and past due — is the only row the queue can withdraw.
 *   It may still lose the race to the claim, and then the server says `refused`,
 *   which is a real answer to a real attempt.
 * * **sending** — claimed, helper away — gets NO cancel. The server refuses it
 *   every single time ("cancelled" would be a claim it cannot make good on), and
 *   a button whose only possible outcome is a refusal is worse than no button.
 * * **live** — a turn in flight — keeps its ✕, but that is the JOB REGISTRY's
 *   verb (`cancelJob("sys:schedule:<id>")`), which really stops the process.
 *   Un-sending a message and killing a run are different promises.
 */
export function rowCancelKind(row: QueueRow): "queued" | "job" | "none" {
  if (row.role === "live") return "job";
  if (row.role === "queued") return "queued";
  return "none";
}

/** How many rows Cancel all would actually take — asked of rowCancelKind rather
 *  than of role membership, so the count and the row buttons can never disagree.
 *  A live turn is not one (it has its own stop) and neither is a claimed one (the
 *  server refuses it), so the button never advertises work it cannot withdraw. */
export function withdrawableCount(rows: QueueRow[]): number {
  return rows.filter((r) => rowCancelKind(r) === "queued").length;
}

/** Whether the card shows Cancel all. Two or more withdrawable rows, because for
 *  a single one the row's own ✕ is the same action with a better name on it — and
 *  a card holding only live or claimed work shows nothing, since "all" would be
 *  a button for zero messages. The threshold lives here, next to the count, so
 *  the two cannot drift apart in the component. */
export function showCancelAll(rows: QueueRow[]): boolean {
  return withdrawableCount(rows) > 1;
}
